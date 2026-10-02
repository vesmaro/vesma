"""vesma-mesh gRPC client — dumb transport for the MnemosCore API (#105 M3).

Wraps the gRPC Unix-socket channel to ``mnemos-mesh`` and exposes the four
``MnemosCore`` RPCs (:rpc:`ListMemories`, :rpc:`WriteMemory`,
:rpc:`GetSubscriptionState`, :rpc:`Heartbeat`) as plain Python methods that
marshal :class:`~vesma.compact.CompactRecord` to/from the protobuf
``CompactRecord``.

Architectural invariants (ArchCom 2026-07-17 federation contract):
    * **Criterion 1 — dumb transport.** This client does NOT decrypt
      payloads, does NOT access the SQLite store, does NOT run moderation.
      It forwards already-redacted ``CompactRecord`` envelopes verbatim.
    * **Criterion 2 — moderation stays in Python.** The mesh never runs
      moderation; this client never calls it. Moderation happened at
      compact-payload build time (see :mod:`vesma.compact`).
    * **Criterion 6 — versioned API boundary.** The protobuf contract
      (``federation/proto/mnemos_core_api.proto``) is the versioned
      boundary. This client speaks ``vesma.core.v1``; future versions
      will be a separate client class.
    * **Criterion 11 — local-first preserved.** This client is a thin
      transport adapter; storage and moderation remain local to vesma.

Import strategy for generated stubs
------------------------------------
The gRPC Python plugin emits flat top-level imports
(``import mnemos_core_api_pb2``) inside the generated ``*_pb2_grpc.py``
files, and the generated directory (``federation/gen/python/``) is
gitignored and lives outside the ``vesma`` package tree. The shim
:mod:`vesma._mesh_gen` inserts the generated directory on ``sys.path``
once and re-exports the four generated modules under stable names
(``core_pb2``, ``core_pb2_grpc``, ``fed_pb2``). This module imports from
the shim and never touches ``sys.path`` itself.

Error model
-----------
Every RPC call is wrapped in ``try/except grpc.RpcError``. gRPC status
codes are mapped to typed exceptions so callers can branch without
inspecting raw gRPC codes:

    * :exc:`MeshUnavailableError` — ``UNAVAILABLE`` (socket down, peer
      crashed). Caller should back off and retry.
    * :exc:`MeshUnimplementedError` — ``UNIMPLEMENTED`` (M2 mesh stub, or
      RPC not yet wired). Caller should degrade gracefully.
    * :exc:`MeshError` — any other gRPC error. Carries the original
      :class:`grpc.RpcError` as ``__cause__``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import grpc

from vesma import _mesh_gen
from vesma.compact import CompactRecord

if TYPE_CHECKING:
    from types import TracebackType

logger = logging.getLogger(__name__)

__all__ = [
    "MeshClient",
    "MeshError",
    "MeshUnavailableError",
    "MeshUnimplementedError",
]


# ── Exceptions ────────────────────────────────────────────────────────────────


class MeshError(Exception):
    """Base error for all :class:`MeshClient` failures.

    Carries the originating :class:`grpc.RpcError` as ``__cause__`` when
    the failure was raised by the gRPC layer.
    """


class MeshUnavailableError(MeshError):
    """The mesh socket is unavailable (``grpc.StatusCode.UNAVAILABLE``).

    Typically means the ``mnemos-mesh`` process is not running, or the
    Unix socket path is wrong / inaccessible. Callers should back off and
    retry — this is a transient, infrastructure-level failure, not a
    protocol error.
    """


class MeshUnimplementedError(MeshError):
    """The RPC is not implemented on the peer (``grpc.StatusCode.UNIMPLEMENTED``).

    Expected from the M2 mesh stub and from any RPC not yet wired on the
    Go side. Callers should degrade gracefully (e.g. fall back to local
    :func:`mnemos_search`) rather than retrying — retrying an
    ``UNIMPLEMENTED`` RPC is a waste of budget.
    """


# ── Marshalling helpers ───────────────────────────────────────────────────────


def _compact_to_proto(record: CompactRecord) -> Any:
    """Marshal a :class:`CompactRecord` (Pydantic) to a protobuf ``CompactRecord``.

    Field-for-field mirror — the Pydantic model and the proto message share
    field names and order (contract §2.3, source of truth is
    :mod:`vesma.compact`). No transformation is applied: the record is
    already moderation-processed by the time it reaches this client.

    Returns the generated ``federation_pb2.CompactRecord`` instance. Typed
    as ``Any`` because the generated module is dynamically imported (see
    :mod:`vesma._mesh_gen`); callers pass it straight back into the gRPC
    stub, which also accepts ``Any``.
    """
    return _mesh_gen.fed_pb2.CompactRecord(
        id=record.id,
        type=record.type,
        title=record.title,
        summary=record.summary,
        key_points=list(record.key_points),
        tags=list(record.tags),
        source_agent=record.source_agent,
        timestamp=record.timestamp,
    )


def _compact_from_proto(pb_record: Any) -> CompactRecord:
    """Marshal a protobuf ``CompactRecord`` back to a :class:`CompactRecord`.

    Inverse of :func:`_compact_to_proto`. The proto ``repeated`` fields come
    back as protobuf scalars/containers; they are copied into plain
    ``list`` objects so the resulting Pydantic model is JSON-serialisable
    and behaves like any other :class:`CompactRecord` in the codebase.
    """
    return CompactRecord(
        id=pb_record.id,
        type=pb_record.type,
        title=pb_record.title,
        summary=pb_record.summary,
        key_points=list(pb_record.key_points),
        tags=list(pb_record.tags),
        source_agent=pb_record.source_agent,
        timestamp=pb_record.timestamp,
    )


# ── Client ───────────────────────────────────────────────────────────────────


class MeshClient:
    """Thin gRPC client for the ``mnemos-mesh`` MnemosCore service.

    Wraps a gRPC insecure channel over a Unix socket. All four RPCs are
    exposed as plain methods returning Python types (not protobuf
    messages). The client holds no state beyond the channel and the stub;
    it is safe to create one per long-lived caller or to use as a context
    manager.

    Args:
        socket_path: Filesystem path to the ``mnemos-mesh`` Unix socket
            (e.g. ``/run/mnemos/core.sock``). The socket must already exist
            when an RPC is invoked; the client does not create it.
        timeout: Per-call deadline in seconds, applied to every RPC via
            ``grpc.channel_ready_future``-free unary calls with a
            ``time.time() + timeout`` deadline. Defaults to 2.0s — short
            enough that a dead peer is noticed quickly, long enough for a
            local Unix-socket round trip.

    Usage::

        with MeshClient("/run/mnemos/core.sock") as client:
            healthy, version, uptime = client.heartbeat("peer-a")
            records = client.list_memories(projects=["vesma"])
    """

    def __init__(self, socket_path: str, *, timeout: float = 2.0) -> None:
        self._socket_path: str = socket_path
        self._timeout: float = float(timeout)
        # gRPC Unix-socket addressing: "unix:///path/to/sock". The triple
        # slash is the scheme:// separator with an empty authority. See
        # https://github.com/grpc/grpc/blob/master/doc/naming.md.
        self._channel: grpc.Channel = grpc.insecure_channel(f"unix://{socket_path}")
        self._stub: Any = _mesh_gen.core_pb2_grpc.MnemosCoreStub(self._channel)

    # ── Public RPCs ─────────────────────────────────────────────────────────

    def list_memories(
        self,
        *,
        projects: Sequence[str] | None = None,
        types: Sequence[str] | None = None,
        tags_include: Sequence[str] | None = None,
        tags_exclude: Sequence[str] | None = None,
        include_no_federate: bool = False,
        since: str = "",
        limit: int = 0,
    ) -> list[CompactRecord]:
        """Call ``MnemosCore.ListMemories`` and return ``CompactRecord`` bodies.

        Args mirror :class:`ListMemoriesRequest`. ``include_no_federate``
        defaults to ``False`` — records tagged ``mnemos:no-federate`` are
        excluded by Vesma before returning; the mesh never sees them
        (contract §2.2.1 layer 3).

        Returns:
            List of :class:`CompactRecord` (already moderation-processed).
            Empty list if Vesma has nothing to federate matching the filter.

        Raises:
            MeshUnavailableError: socket down / peer not running.
            MeshUnimplementedError: RPC not yet wired on the mesh (M2 stub).
            MeshError: any other gRPC failure.
        """
        request = _mesh_gen.core_pb2.ListMemoriesRequest(
            projects=list(projects) if projects is not None else [],
            types=list(types) if types is not None else [],
            tags_include=list(tags_include) if tags_include is not None else [],
            tags_exclude=list(tags_exclude) if tags_exclude is not None else [],
            include_no_federate=include_no_federate,
            since=since,
            limit=limit,
        )
        try:
            response = self._stub.ListMemories(request, timeout=self._timeout)
        except grpc.RpcError as exc:
            raise self._map_error(exc) from exc
        return [_compact_from_proto(r) for r in response.records]

    def write_memory(
        self,
        record: CompactRecord,
        *,
        import_mode: str = "MERGE",
        confirm: bool = False,
    ) -> str:
        """Call ``MnemosCore.WriteMemory`` and return the written record id.

        Idempotent by ``record.id`` (the ``fed:<source_agent>:<local_uuid>``
        prefix guarantees global uniqueness). Replaying the same record
        is safe.

        Args:
            record: The :class:`CompactRecord` to write. Already
                moderation-processed by the peer of origin; Vesma may
                apply its own import validation (#86) on top.
            import_mode: ``"MERGE"`` (default) or ``"RESTORE"``. Mirrors
                the :func:`mnemos_import` MCP tool modes. ``"RESTORE"`` is
                destructive and requires ``confirm=True`` (hard gate).
            confirm: Required ``True`` when ``import_mode="RESTORE"``,
                ignored otherwise. Vesma rejects ``RESTORE`` without
                ``confirm=True``.

        Returns:
            The written record id (echoed by Vesma for correlation with
            the mesh's pending queue). Same as ``record.id`` on success.

        Raises:
            MeshUnavailableError: socket down / peer not running.
            MeshUnimplementedError: RPC not yet wired on the mesh (M2 stub).
            MeshError: any other gRPC failure (e.g. Vesma refused the
                record via import validation).
        """
        mode_enum = self._import_mode_to_enum(import_mode)
        request = _mesh_gen.core_pb2.WriteMemoryRequest(
            record=_compact_to_proto(record),
            import_mode=mode_enum,
            confirm=confirm,
        )
        try:
            response = self._stub.WriteMemory(request, timeout=self._timeout)
        except grpc.RpcError as exc:
            raise self._map_error(exc) from exc
        written_id: str = response.written_id
        return written_id

    def get_subscription_state(
        self,
        peer_id: str,
        project_scope: str,
    ) -> tuple[str, int, str]:
        """Call ``MnemosCore.GetSubscriptionState`` and return the cursor.

        Used by the mesh on (re)connect to decide where to resume a
        subscription stream from.

        Args:
            peer_id: A2A id of the peer whose subscription state is requested.
            project_scope: Project scope the subscription is bound to.

        Returns:
            ``(cursor, last_rev, last_sync_timestamp)`` tuple. ``cursor`` is
            the opaque resume token (empty if no prior subscription
            existed). ``last_rev`` is the last watermark revision (0 means
            no prior state). ``last_sync_timestamp`` is an ISO 8601 UTC
            string used for staleness reporting.

        Raises:
            MeshUnavailableError: socket down / peer not running.
            MeshUnimplementedError: RPC not yet wired on the mesh (M2 stub).
            MeshError: any other gRPC failure.
        """
        request = _mesh_gen.core_pb2.GetSubscriptionStateRequest(
            peer_id=peer_id,
            project_scope=project_scope,
        )
        try:
            response = self._stub.GetSubscriptionState(request, timeout=self._timeout)
        except grpc.RpcError as exc:
            raise self._map_error(exc) from exc
        cursor: str = response.cursor
        last_rev: int = int(response.last_rev)
        last_sync_ts: str = response.last_sync_timestamp
        return (cursor, last_rev, last_sync_ts)

    def heartbeat(
        self,
        peer_id: str,
        component: str = "vesma",
    ) -> tuple[bool, str, int]:
        """Call ``MnemosCore.Heartbeat`` and return the liveness reply.

        Cheap unary call; failure to respond within ``timeout`` marks the
        peer component as unhealthy.

        Args:
            peer_id: A2A id of the probing peer.
            component: Which component is probing — ``"vesma"`` (default)
                or ``"mesh"``. Lets the receiver record provenance without
                guessing from the socket.

        Returns:
            ``(healthy, version, uptime_seconds)`` tuple. ``healthy`` is
            ``True`` if the responder is ready to serve. ``version`` is
            the responder's build version (e.g. ``"mnemos-mesh 0.1.0"``).
            ``uptime_seconds`` is the responder's uptime in seconds.

        Raises:
            MeshUnavailableError: socket down / peer not running.
            MeshUnimplementedError: RPC not yet wired on the mesh (M2 stub).
            MeshError: any other gRPC failure.
        """
        request = _mesh_gen.core_pb2.HeartbeatRequest(
            peer_id=peer_id,
            component=component,
        )
        try:
            response = self._stub.Heartbeat(request, timeout=self._timeout)
        except grpc.RpcError as exc:
            raise self._map_error(exc) from exc
        healthy: bool = bool(response.healthy)
        version: str = response.version
        uptime_s: int = int(response.uptime_seconds)
        return (healthy, version, uptime_s)

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the underlying gRPC channel.

        Idempotent — calling ``close()`` more than once is safe. After
        close, any subsequent RPC raises :exc:`MeshError` (gRPC returns
        ``CANCELLED`` on a closed channel).
        """
        self._channel.close()

    def __enter__(self) -> MeshClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ── Internals ───────────────────────────────────────────────────────────

    @staticmethod
    def _import_mode_to_enum(import_mode: str) -> int:
        """Map a string ``import_mode`` to the protobuf ``ImportMode`` enum value.

        Accepts the two well-formed modes (``"MERGE"`` / ``"RESTORE"``)
        and the sentinel ``"IMPORT_MODE_UNSPECIFIED"``. Any other value
        raises :exc:`ValueError` — the caller should not pass ad-hoc
        strings; this is a contract boundary.
        """
        enum_obj = _mesh_gen.core_pb2.ImportMode
        try:
            return int(enum_obj.Value(import_mode))
        except ValueError as exc:
            raise ValueError(
                f"invalid import_mode {import_mode!r}; expected 'MERGE' or 'RESTORE'"
            ) from exc

    @staticmethod
    def _map_error(exc: grpc.RpcError) -> MeshError:
        """Map a :class:`grpc.RpcError` to the appropriate :class:`MeshError`.

        Branches on ``exc.code()`` (the gRPC status code) so callers can
        ``except MeshUnavailableError`` / ``except MeshUnimplementedError`` without
        inspecting raw gRPC codes. The original ``exc`` is preserved as
        ``__cause__`` by the ``raise ... from`` at the call site.
        """
        code = exc.code()
        if code == grpc.StatusCode.UNAVAILABLE:
            return MeshUnavailableError(str(exc))
        if code == grpc.StatusCode.UNIMPLEMENTED:
            return MeshUnimplementedError(str(exc))
        return MeshError(str(exc))
