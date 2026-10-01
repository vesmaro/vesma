"""Unit tests for the MnemosCore gRPC server (#105 M4.0).

Exercises :class:`vesmaro.mesh_server.MeshServer` end-to-end over a real
gRPC Unix socket on a ``tmp_path`` — no mocks on the gRPC layer. The
tests cover:

* Server starts on a Unix socket and stops cleanly.
* :rpc:`Heartbeat` returns a non-empty version + ``healthy=True``.
* :rpc:`ListMemories` returns seeded records (moderation-processed).
* :rpc:`ListMemories` with a disallowed ``project_scope`` → ACL refuses
  (empty response, ``PERMISSION_DENIED``).
* :rpc:`ListMemories` cursor contract (ADR-0020): non-empty page mints a
  stable opaque cursor; empty page mints ""; resume by a fresh cursor
  returns EXACTLY the new records; resume_cursor takes priority over
  ``since``; garbage/foreign cursors → ``INVALID_ARGUMENT``; the legacy
  ``since`` path still filters; cursor-walk pagination partitions the
  corpus (no dupes, no gaps); records carry ``revision`` (storage rowid,
  ADR-0021 Q10.6) monotonic across the page.
* :rpc:`WriteMemory` writes to SQLite (verified via a direct DB query).
* :rpc:`WriteMemory` with a disallowed scope → ``PERMISSION_DENIED``.
* :rpc:`WriteMemory` idempotency (#359): replays return
  ``ALREADY_EXHAUSTED`` with the existing storage id (fed_id key,
  title+source_agent fallback, no match for provenance-less records).
* :rpc:`GetSubscriptionState` returns an empty cursor (M4: mesh-side
  persistence in M5) and enforces the ACL.
* ACL hardening fail-closed matrix (vesmaro#371/#369 family): unscoped
  requests intersect with the peer's allowed set; empty allow-list /
  wildcard-with-empty-shared / unknown peer → ``PERMISSION_DENIED`` on
  read AND write; untagged WriteMemory records are refused; wildcard
  read/write is bounded by ``shared_projects``.
* Server lifecycle (start/stop cleanly, socket cleanup, context manager).
* W3 agent data gate (ADR-0018-T §3, criterion 5): a request without
  ``x-mnemos-agent-id`` keeps the federation path unchanged (additive);
  with the key, the bearer is re-validated per request — a valid read
  token lists/reads but cannot write, an rw token writes, garbage /
  expired / revoked / cross-node (aud) tokens and a missing bearer →
  ``UNAUTHENTICATED``, a spoofed agent id → ``PERMISSION_DENIED``, and
  the token's ``project:`` grants NARROW the transport-peer ACL on both
  the list and write paths (TM §3 effective scope = token ∩ peer ACL).

The tests use the real :class:`MemoryManager` against a tmp SQLite store
so the moderation pipeline + Layer 1 secrets scanner run for real.
"""

from __future__ import annotations

import stat
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import grpc
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from vesmaro import _mesh_gen
from vesmaro.agent_tokens import (
    AgentTokenClaims,
    AgentTokenStore,
    encode_agent_token,
    issue_agent_token,
    load_or_create_signing_key,
    signing_key_path,
)
from vesmaro.compact import CompactRecord
from vesmaro.config import FederationConfig, PeerConfig, Settings
from vesmaro.manager import MemoryManager
from vesmaro.mesh_server import MeshServer
from vesmaro.models import MemoryCreate, MemorySource

# ── Constants ────────────────────────────────────────────────────────────────

#: Test project slug — the peer is allowed to access this project only.
_PROJECT = "test-project"

#: A second project the peer is NOT allowed to access — for ACL refusal.
_PROJECT_DENIED = "project-secret"

#: A second SHAREABLE project — for the ACL-hardening matrix (seeded, but
#: visible to the peer only when its allowed set / shared union says so).
_PROJECT_OTHER = "project-other"

#: A2A id of the single configured peer (the mesh in these tests).
_PEER_ID = "mnemos-A"

#: Agent slug used in seeded memories + imported records.
_AGENT = "gcw-test-agent"

#: Bearer token env var name — value is irrelevant for the mesh server
#: (auth is via the Unix socket + filesystem perms, not bearer tokens),
#: but :class:`PeerConfig` requires the field.
_TOKEN_ENV = "VESMARO_FED_PEER_TEST_TOKEN"


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _settings_with_peer(
    tmp_path: Path,
    *,
    allow_wildcard: bool = False,
    allowed: list[str] | None = None,
    shared: list[str] | None = None,
) -> Settings:
    """Build a :class:`Settings` with one configured peer + isolated store.

    ``allowed``/``shared`` override the peer's ``allowed_projects`` and
    the global ``shared_projects`` (ACL-hardening matrix tests); the
    defaults reproduce the historical single-project fixture.
    """
    if allowed is None:
        allowed = ["*"] if allow_wildcard else [_PROJECT]
    if shared is None:
        shared = [_PROJECT]
    # Pydantic coerces dict kwargs into the nested config models at
    # runtime; the cast keeps mypy --strict happy without changing behaviour.
    settings = Settings(
        **{  # type: ignore[arg-type]  # pydantic dict→model coercion
            "mnemos": {
                "vault_path": str(tmp_path / "vault"),
                "data_dir": str(tmp_path / "data"),
                "db_name": "test_mesh_server.db",
            },
            "embedding": {"provider": "onnx"},
            "scanner": {"enabled": False},
            "federation": FederationConfig(
                shared_projects=shared,
                peers={
                    _PEER_ID: PeerConfig(
                        bearer_token_env=_TOKEN_ENV,
                        allowed_projects=allowed,
                        allowed_types=["decision", "learning"],
                        rate_limit_per_minute=600,
                    ),
                },
            ),
        }
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings with a single peer allowed to access ``_PROJECT`` only."""
    return _settings_with_peer(tmp_path)


@pytest.fixture
def manager(settings: Settings) -> Generator[MemoryManager, None, None]:
    """Real :class:`MemoryManager` against a tmp SQLite store.

    The embedder is mocked so the tests do not require the ONNX runtime.
    """
    mgr = MemoryManager(settings)
    # Stub the embedder so MemoryManager construction + search work
    # without the real ONNX model download.
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    yield mgr
    mgr.close()


@pytest.fixture
def seeded_manager(manager: MemoryManager) -> MemoryManager:
    """Manager with two seeded memories (one decision, one learning)."""
    manager.add(
        MemoryCreate(
            content="We chose bearer+TOTP 2FA for remote sessions.",
            title="ADR-0014 auth decision",
            tags=[f"project:{_PROJECT}", f"agent:{_AGENT}", "mnemos:decision"],
            source=MemorySource.MANUAL,
        ),
        project=_PROJECT,
        agent=_AGENT,
    )
    manager.add(
        MemoryCreate(
            content="Rate-limiting slowapi needs a reset between test runs.",
            title="Rate-limit fixture gotcha",
            tags=[f"project:{_PROJECT}", f"agent:{_AGENT}", "mnemos:learning"],
            source=MemorySource.MANUAL,
        ),
        project=_PROJECT,
        agent=_AGENT,
    )
    return manager


@pytest.fixture
def server(
    tmp_path: Path,
    settings: Settings,
    seeded_manager: MemoryManager,
) -> Generator[MeshServer, None, None]:
    """Start a :class:`MeshServer` on a tmp Unix socket and stop it after."""
    socket_path = str(tmp_path / "core.sock")
    srv = MeshServer(socket_path, seeded_manager, settings, max_workers=2)
    srv.start()
    yield srv
    srv.stop(grace=0.5)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _channel(server: MeshServer) -> grpc.Channel:
    """Open an insecure gRPC channel to the running server's Unix socket."""
    return grpc.insecure_channel(f"unix://{server.socket_path}")


def _stub(server: MeshServer) -> Any:
    """Build a ``MnemosCoreStub`` against the running server."""
    return _mesh_gen.core_pb2_grpc.MnemosCoreStub(_channel(server))


def _wait_for_server(server: MeshServer, timeout: float = 2.0) -> None:
    """Block until the server's gRPC channel is ready (or timeout)."""
    channel = _channel(server)
    grpc.channel_ready_future(channel).result(timeout=timeout)


def _make_compact_record(
    *,
    record_id: str = "fed:gcw-test-agent:abc-123",
    project: str = _PROJECT,
    agent: str = _AGENT,
    title: str = "Imported decision",
    summary: str = "A decision imported from a peer via the mesh.",
    tags: list[str] | None = None,
) -> CompactRecord:
    """Build a :class:`CompactRecord` for :rpc:`WriteMemory` tests."""
    if tags is None:
        tags = [f"project:{project}", f"agent:{agent}", "mnemos:decision"]
    return CompactRecord(
        id=record_id,
        type="decision",
        title=title,
        summary=summary,
        key_points=["point one"],
        tags=tags,
        source_agent=agent,
        timestamp="2026-07-22T10:00:00Z",
    )


def _to_proto_record(record: CompactRecord) -> Any:
    """Marshal a :class:`CompactRecord` to the protobuf message for the request."""
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


# ── Lifecycle ────────────────────────────────────────────────────────────────


class TestServerLifecycle:
    def test_server_starts_and_stops_cleanly(
        self,
        tmp_path: Path,
        settings: Settings,
        manager: MemoryManager,
    ) -> None:
        """MeshServer.start() creates the socket; stop() removes it."""
        socket_path = str(tmp_path / "lifecycle.sock")
        srv = MeshServer(socket_path, manager, settings, max_workers=2)
        assert not srv.is_running
        srv.start()
        assert srv.is_running
        assert Path(socket_path).exists()
        srv.stop(grace=0.1)
        assert not srv.is_running
        assert not Path(socket_path).exists()

    def test_context_manager_starts_and_stops(
        self,
        tmp_path: Path,
        settings: Settings,
        manager: MemoryManager,
    ) -> None:
        """MeshServer works as a context manager (start on enter, stop on exit)."""
        socket_path = str(tmp_path / "ctx.sock")
        with MeshServer(socket_path, manager, settings, max_workers=2) as srv:
            assert srv.is_running
            assert Path(socket_path).exists()
        assert not srv.is_running

    def test_double_start_raises(
        self,
        tmp_path: Path,
        settings: Settings,
        manager: MemoryManager,
    ) -> None:
        """Calling start() twice raises RuntimeError (no double-bind)."""
        socket_path = str(tmp_path / "double.sock")
        srv = MeshServer(socket_path, manager, settings, max_workers=2)
        srv.start()
        try:
            with pytest.raises(RuntimeError, match="already started"):
                srv.start()
        finally:
            srv.stop(grace=0.1)

    def test_stop_without_start_is_noop(
        self,
        tmp_path: Path,
        settings: Settings,
        manager: MemoryManager,
    ) -> None:
        """stop() before start() is a safe no-op."""
        socket_path = str(tmp_path / "noop.sock")
        srv = MeshServer(socket_path, manager, settings, max_workers=2)
        srv.stop()  # must not raise


class TestSocketPermissions:
    """Socket/dir modes for the two deployment profiles (W2 wiring).

    Default: owner-only (0600 socket / 0700 dir) — unchanged behaviour.
    ``mesh.socket_group_access: true``: group rw (0660/0770) so a mesh
    binary running as a different uid in the same gid (fsGroup, compose
    ``user:``) can dial the shared-volume socket.
    """

    def test_default_modes_are_owner_only(
        self,
        tmp_path: Path,
        settings: Settings,
        manager: MemoryManager,
    ) -> None:
        socket_path = str(tmp_path / "sock" / "perms_default.sock")
        srv = MeshServer(socket_path, manager, settings, max_workers=2)
        srv.start()
        try:
            assert stat.S_IMODE(Path(socket_path).stat().st_mode) == 0o600
            assert stat.S_IMODE(Path(socket_path).parent.stat().st_mode) == 0o700
        finally:
            srv.stop(grace=0.1)

    def test_group_access_modes(
        self,
        tmp_path: Path,
        settings: Settings,
        manager: MemoryManager,
    ) -> None:
        settings.mesh.socket_group_access = True
        socket_path = str(tmp_path / "sock" / "perms_group.sock")
        srv = MeshServer(socket_path, manager, settings, max_workers=2)
        srv.start()
        try:
            assert stat.S_IMODE(Path(socket_path).stat().st_mode) == 0o660
            assert stat.S_IMODE(Path(socket_path).parent.stat().st_mode) == 0o770
        finally:
            srv.stop(grace=0.1)


# ── Heartbeat ─────────────────────────────────────────────────────────────────


class TestHeartbeat:
    def test_heartbeat_returns_nonempty_version(self, server: MeshServer) -> None:
        """Heartbeat returns healthy=True + a non-empty version string."""
        _wait_for_server(server)
        stub = _stub(server)
        response = stub.Heartbeat(
            _mesh_gen.core_pb2.HeartbeatRequest(peer_id=_PEER_ID, component="mesh"),
            timeout=2.0,
        )
        assert response.healthy is True
        assert response.version
        assert response.version.startswith("vesma ")

    def test_heartbeat_uptime_is_nonnegative(self, server: MeshServer) -> None:
        """Uptime seconds is >= 0 for a freshly started server."""
        _wait_for_server(server)
        stub = _stub(server)
        response = stub.Heartbeat(
            _mesh_gen.core_pb2.HeartbeatRequest(peer_id=_PEER_ID, component="mesh"),
            timeout=2.0,
        )
        assert response.uptime_seconds >= 0


# ── ListMemories ─────────────────────────────────────────────────────────────


class TestListMemories:
    def test_list_returns_seeded_memories(self, server: MeshServer) -> None:
        """ListMemories returns the seeded compact records (moderation-processed)."""
        _wait_for_server(server)
        stub = _stub(server)
        response = stub.ListMemories(
            _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT]),
            timeout=2.0,
        )
        assert response.total >= 2
        assert len(response.records) >= 2
        # Each record is a CompactRecord with an id, type, title.
        for rec in response.records:
            assert rec.id
            assert rec.type in {"decision", "learning", "session"}
            assert rec.title

    def test_list_acl_denied_scope_refuses(self, server: MeshServer) -> None:
        """ListMemories with a disallowed project → PERMISSION_DENIED."""
        _wait_for_server(server)
        stub = _stub(server)
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.ListMemories(
                _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT_DENIED]),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED

    def test_list_filter_by_type(self, server: MeshServer) -> None:
        """ListMemories with types=["decision"] returns only decision records."""
        _wait_for_server(server)
        stub = _stub(server)
        response = stub.ListMemories(
            _mesh_gen.core_pb2.ListMemoriesRequest(
                projects=[_PROJECT],
                types=["decision"],
            ),
            timeout=2.0,
        )
        assert len(response.records) >= 1
        for rec in response.records:
            assert rec.type == "decision"

    def test_list_no_federate_excluded_by_default(self, server: MeshServer) -> None:
        """Records tagged mnemos:no-federate are excluded by default."""
        _wait_for_server(server)
        # Seed a no-federate record directly in the store.
        servicer = server.servicer
        assert servicer is not None
        servicer._manager.add(
            MemoryCreate(
                content="secret access key AKIAEXAMPLE123",
                title="Should be excluded",
                tags=[
                    f"project:{_PROJECT}",
                    f"agent:{_AGENT}",
                    "mnemos:decision",
                    "mnemos:no-federate",
                ],
                source=MemorySource.MANUAL,
            ),
            project=_PROJECT,
            agent=_AGENT,
        )
        stub = _stub(server)
        response = stub.ListMemories(
            _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT]),
            timeout=2.0,
        )
        for rec in response.records:
            assert "mnemos:no-federate" not in list(rec.tags)


# ── ListMemories cursor contract (ADR-0020) ──────────────────────────────────


class TestListMemoriesCursors:
    """ADR-0020: core mints opaque cursors, resumes exactly, rejects garbage."""

    def _list(self, server: MeshServer, **kwargs: Any) -> Any:
        """Call ListMemories on the running server with optional fields."""
        _wait_for_server(server)
        stub = _stub(server)
        return stub.ListMemories(
            _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT], **kwargs),
            timeout=2.0,
        )

    @staticmethod
    def _seed(servicer: Any, title: str, *, tag: str = "mnemos:decision") -> None:
        """Add one federable memory through the servicer's manager."""
        servicer._manager.add(
            MemoryCreate(
                content=f"Cursor-test content for {title}.",
                title=title,
                tags=[f"project:{_PROJECT}", f"agent:{_AGENT}", tag],
                source=MemorySource.MANUAL,
            ),
            project=_PROJECT,
            agent=_AGENT,
        )

    def test_mints_nonempty_stable_cursor(self, server: MeshServer) -> None:
        """Non-empty response → non-empty cursor; a repeated call mints the
        same cursor (deterministic mint from the same storage position)."""
        first = self._list(server)
        assert len(first.records) >= 2
        assert first.cursor, "non-empty page must mint a non-empty cursor"
        second = self._list(server)
        assert second.cursor == first.cursor

    def test_empty_page_mints_empty_cursor(self, server: MeshServer) -> None:
        """A filter matching nothing → 0 records and an EMPTY cursor
        (byte-identical to a pre-ADR-0020 core: old-mesh fresh-subscribe)."""
        response = self._list(server, types=["nonexistent-type"])
        assert len(response.records) == 0
        assert response.cursor == ""
        assert response.has_more is False

    def test_revision_stamped_monotonic(self, server: MeshServer) -> None:
        """Every exported record carries revision > 0 (the storage rowid,
        ADR-0021 Q10.6) and revisions strictly increase across the page
        (rowid-ASC forward walk)."""
        response = self._list(server)
        assert len(response.records) >= 2
        revisions = [rec.revision for rec in response.records]
        assert all(r > 0 for r in revisions)
        assert revisions == sorted(revisions)
        assert len(set(revisions)) == len(revisions)

    def test_resume_returns_exactly_new_records(self, server: MeshServer) -> None:
        """Resume by a fresh cursor → EXACTLY the records added after the
        checkpoint (no dupes of the delivered set, no gaps)."""
        baseline = self._list(server)
        assert len(baseline.records) >= 2
        served_ids = {rec.id for rec in baseline.records}
        servicer = server.servicer
        assert servicer is not None
        for i in range(3):
            self._seed(servicer, f"Post-checkpoint decision {i}")
        resumed = self._list(server, resume_cursor=baseline.cursor)
        new_ids = {rec.id for rec in resumed.records}
        assert len(resumed.records) == 3
        assert new_ids.isdisjoint(served_ids)
        assert resumed.cursor and resumed.cursor != baseline.cursor
        # Fully caught up: resuming by the newest cursor → nothing new.
        caught_up = self._list(server, resume_cursor=resumed.cursor)
        assert len(caught_up.records) == 0
        assert caught_up.cursor == ""

    def test_resume_priority_over_since(self, server: MeshServer) -> None:
        """A non-empty resume_cursor IGNORES `since` (ADR-0020: checkpoint
        takes priority) — even a since in the far future must not zero the
        resumed page."""
        baseline = self._list(server)
        servicer = server.servicer
        assert servicer is not None
        self._seed(servicer, "Priority-over-since decision")
        resumed = self._list(
            server,
            resume_cursor=baseline.cursor,
            since="2999-01-01T00:00:00",
        )
        assert len(resumed.records) == 1

    def test_garbage_resume_cursor_rejected(self, server: MeshServer) -> None:
        """Malformed/foreign cursors → INVALID_ARGUMENT, never a silent
        empty page (ADR-0020 rule 3).

        Shapes covered: non-base64 garbage; base64 of non-JSON; a valid
        token with an unknown format version; a non-integer rowid; a
        rowid ABOVE the SQLite 2^63-1 ceiling (uncaught it would raise
        OverflowError and kill the RPC with UNKNOWN — review E1); and an
        oversized token (> 128 chars) rejected before any decode work.
        """
        import base64
        import json as _json

        def _token(payload: bytes) -> str:
            return base64.urlsafe_b64encode(payload).rstrip(b"=").decode()

        wrong_version = _token(_json.dumps({"v": 2, "rowid": 1}).encode())
        not_an_int = _token(_json.dumps({"v": 1, "rowid": "one"}).encode())
        not_json = _token(b"not json at all")
        rowid_overflow = _token(_json.dumps({"v": 1, "rowid": 2**63}).encode())
        oversized = "A" * 129
        for garbage in (
            "garbage",
            not_json,
            wrong_version,
            not_an_int,
            rowid_overflow,
            oversized,
        ):
            _wait_for_server(server)
            stub = _stub(server)
            with pytest.raises(grpc.RpcError) as exc_info:
                stub.ListMemories(
                    _mesh_gen.core_pb2.ListMemoriesRequest(
                        projects=[_PROJECT],
                        resume_cursor=garbage,
                    ),
                    timeout=2.0,
                )
            assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT, garbage

    def test_since_path_still_filters(self, server: MeshServer) -> None:
        """Legacy `since` behaviour is unchanged (pre-ADR-0020 meshes):
        a far-past bound returns the full set, a far-future bound returns
        nothing (with an empty cursor)."""
        all_records = self._list(server, since="2000-01-01T00:00:00")
        assert len(all_records.records) >= 2
        none_records = self._list(server, since="2999-01-01T00:00:00")
        assert len(none_records.records) == 0
        assert none_records.cursor == ""

    def test_cursor_pagination_round_trip(self, server: MeshServer) -> None:
        """Full sync by walking resume cursors: pages partition the corpus
        exactly (no dupes, no gaps) and the walk terminates."""
        servicer = server.servicer
        assert servicer is not None
        for i in range(5):
            self._seed(servicer, f"Pagination decision {i}")
        seen: list[str] = []
        cursor = ""
        pages = 0
        while True:
            response = self._list(server, limit=2, resume_cursor=cursor)
            seen.extend(rec.id for rec in response.records)
            pages += 1
            if not response.has_more:
                break
            assert response.cursor, "has_more page must mint a continuation cursor"
            cursor = response.cursor
            assert pages <= 10, "pagination did not terminate"
        total_expected = len(self._list(server).records)
        assert pages >= 3  # 5+2 seeded records at limit=2 must span >= 3 pages
        assert len(seen) == total_expected
        assert len(set(seen)) == len(seen), "pages must not overlap"


# ── WriteMemory ──────────────────────────────────────────────────────────────


class TestWriteMemory:
    def test_write_persists_to_sqlite(self, server: MeshServer) -> None:
        """WriteMemory writes to SQLite — verified via a direct DB query."""
        _wait_for_server(server)
        stub = _stub(server)
        record = _make_compact_record(summary="A clean decision from a peer.")
        response = stub.WriteMemory(
            _mesh_gen.core_pb2.WriteMemoryRequest(
                record=_to_proto_record(record),
                import_mode=_mesh_gen.core_pb2.ImportMode.MERGE,
            ),
            timeout=2.0,
        )
        assert response.written_id
        assert response.trigger_code == _mesh_gen.fed_pb2.TriggerCodes.EXHAUSTIVE
        # Verify the memory landed in SQLite.
        servicer = server.servicer
        assert servicer is not None
        memory = servicer._manager.sqlite.get(response.written_id)
        assert memory is not None
        assert memory.project == _PROJECT
        assert memory.agent == _AGENT
        # The federation id is stored in metadata for traceability.
        assert memory.metadata.get("fed_id") == record.id

    def test_write_acl_denied_returns_refused(self, server: MeshServer) -> None:
        """WriteMemory with a disallowed project → PERMISSION_DENIED."""
        _wait_for_server(server)
        stub = _stub(server)
        record = _make_compact_record(project=_PROJECT_DENIED)
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.WriteMemory(
                _mesh_gen.core_pb2.WriteMemoryRequest(
                    record=_to_proto_record(record),
                    import_mode=_mesh_gen.core_pb2.ImportMode.MERGE,
                ),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED

    def test_write_unspecified_mode_rejected(self, server: MeshServer) -> None:
        """WriteMemory with UNSPECIFIED import_mode → INVALID_ARGUMENT."""
        _wait_for_server(server)
        stub = _stub(server)
        record = _make_compact_record()
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.WriteMemory(
                _mesh_gen.core_pb2.WriteMemoryRequest(
                    record=_to_proto_record(record),
                    import_mode=_mesh_gen.core_pb2.ImportMode.IMPORT_MODE_UNSPECIFIED,
                ),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT

    def test_write_restore_without_confirm_rejected(self, server: MeshServer) -> None:
        """RESTORE without confirm=True → FAILED_PRECONDITION (hard gate)."""
        _wait_for_server(server)
        stub = _stub(server)
        record = _make_compact_record()
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.WriteMemory(
                _mesh_gen.core_pb2.WriteMemoryRequest(
                    record=_to_proto_record(record),
                    import_mode=_mesh_gen.core_pb2.ImportMode.RESTORE,
                    confirm=False,
                ),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.FAILED_PRECONDITION


# ── WriteMemory idempotency (vesmaro #359) ───────────────────────────────────


class TestWriteMemoryIdempotency:
    """Replayed one-shot pulls (mnemos-mesh #34) must not duplicate rows.

    The mesh classifies each response by trigger_code: EXHAUSTIVE →
    Written, ALREADY_EXHAUSTED → Duplicates. These tests pin the core
    side of that contract (#359).
    """

    def _write(self, server: MeshServer, record: CompactRecord) -> Any:
        """One WriteMemory(MERGE, confirm=false) call — the mesh pull shape."""
        _wait_for_server(server)
        stub = _stub(server)
        return stub.WriteMemory(
            _mesh_gen.core_pb2.WriteMemoryRequest(
                record=_to_proto_record(record),
                import_mode=_mesh_gen.core_pb2.ImportMode.MERGE,
                confirm=False,
            ),
            timeout=2.0,
        )

    def _rows_with_fed_id(self, server: MeshServer, fed_id: str) -> list[Any]:
        """All stored memories whose metadata.fed_id equals fed_id."""
        servicer = server.servicer
        assert servicer is not None
        return [
            m
            for m in servicer._manager.sqlite.list_all(limit=1000)
            if m.metadata.get("fed_id") == fed_id
        ]

    def test_replay_same_fed_id_returns_already_exhausted_same_id(self, server: MeshServer) -> None:
        """Same record twice: EXHAUSTIVE then ALREADY_EXHAUSTED, same
        storage id, exactly one stored row."""
        record = _make_compact_record(summary="Idempotent replay candidate.")
        first = self._write(server, record)
        assert first.trigger_code == _mesh_gen.fed_pb2.TriggerCodes.EXHAUSTIVE
        second = self._write(server, record)
        assert second.trigger_code == _mesh_gen.fed_pb2.TriggerCodes.ALREADY_EXHAUSTED
        assert second.written_id == first.written_id
        assert second.mode_applied == _mesh_gen.core_pb2.ImportMode.MERGE
        rows = self._rows_with_fed_id(server, record.id)
        assert len(rows) == 1

    def test_triple_replay_mesh_scenario(self, server: MeshServer) -> None:
        """The mnemos-mesh #34 one-shot pull replay, end to end: N replays
        → 1 Written + (N-1) Duplicates in mesh Stats terms, one row."""
        record = _make_compact_record(summary="Mesh replay scenario.")
        ids = set()
        outcomes = []
        for _ in range(3):
            resp = self._write(server, record)
            ids.add(resp.written_id)
            outcomes.append(resp.trigger_code)
        assert outcomes == [
            _mesh_gen.fed_pb2.TriggerCodes.EXHAUSTIVE,
            _mesh_gen.fed_pb2.TriggerCodes.ALREADY_EXHAUSTED,
            _mesh_gen.fed_pb2.TriggerCodes.ALREADY_EXHAUSTED,
        ]
        assert len(ids) == 1
        assert len(self._rows_with_fed_id(server, record.id)) == 1

    def test_duplicate_refreshes_last_fed_at_without_rewrite(self, server: MeshServer) -> None:
        """A replay refreshes metadata.last_fed_at; content/title stay."""
        record = _make_compact_record(summary="Freshness touch candidate.")
        first = self._write(server, record)
        servicer = server.servicer
        assert servicer is not None
        before = servicer._manager.sqlite.get(first.written_id)
        assert before is not None
        assert "last_fed_at" in before.metadata  # seeded at first import (#359)
        ts_before = before.metadata["last_fed_at"]
        self._write(server, record)
        after = servicer._manager.sqlite.get(first.written_id)
        assert after is not None
        assert after.metadata["last_fed_at"] > ts_before
        # No rewrite: the stored projection is untouched.
        assert after.content == before.content
        assert after.title == before.title

    def test_fallback_title_source_agent_without_fed_id(self, server: MeshServer) -> None:
        """Records arriving without a fed id dedup on title+source_agent."""
        record = _make_compact_record(
            record_id="", summary="No-fed-id record body.", title="No-fed-id title"
        )
        first = self._write(server, record)
        assert first.trigger_code == _mesh_gen.fed_pb2.TriggerCodes.EXHAUSTIVE
        second = self._write(server, record)
        assert second.trigger_code == _mesh_gen.fed_pb2.TriggerCodes.ALREADY_EXHAUSTED
        assert second.written_id == first.written_id

    def test_no_provenance_records_are_never_deduped(self, server: MeshServer) -> None:
        """No fed_id AND no source_agent → plain API semantics: every call
        creates a fresh row (обычные API-записи не трогаем)."""
        record = _make_compact_record(
            record_id="",
            agent="",
            summary="Provenance-less body.",
            title="No provenance",
            tags=[f"project:{_PROJECT}", "mnemos:decision"],
        )
        assert record.source_agent == ""
        first = self._write(server, record)
        second = self._write(server, record)
        assert first.trigger_code == _mesh_gen.fed_pb2.TriggerCodes.EXHAUSTIVE
        assert second.trigger_code == _mesh_gen.fed_pb2.TriggerCodes.EXHAUSTIVE
        assert second.written_id != first.written_id

    def test_different_fed_id_creates_new_record(self, server: MeshServer) -> None:
        """Same title+source_agent but a different fed_id → a new row (the
        fed_id branch is the priority key and never falls through)."""
        record_a = _make_compact_record(
            record_id="fed:gcw-test-agent:aaa", summary="Body A.", title="Shared headline"
        )
        record_b = _make_compact_record(
            record_id="fed:gcw-test-agent:bbb", summary="Body B.", title="Shared headline"
        )
        first = self._write(server, record_a)
        second = self._write(server, record_b)
        assert first.trigger_code == _mesh_gen.fed_pb2.TriggerCodes.EXHAUSTIVE
        assert second.trigger_code == _mesh_gen.fed_pb2.TriggerCodes.EXHAUSTIVE
        assert second.written_id != first.written_id

    def test_different_title_creates_new_record(self, server: MeshServer) -> None:
        """Fallback path: same source_agent, different title → new row."""
        record_a = _make_compact_record(record_id="", summary="Body A.", title="Headline one")
        record_b = _make_compact_record(record_id="", summary="Body B.", title="Headline two")
        first = self._write(server, record_a)
        second = self._write(server, record_b)
        assert first.trigger_code == _mesh_gen.fed_pb2.TriggerCodes.EXHAUSTIVE
        assert second.trigger_code == _mesh_gen.fed_pb2.TriggerCodes.EXHAUSTIVE
        assert second.written_id != first.written_id

    def test_regular_api_record_not_matched_by_fallback(self, server: MeshServer) -> None:
        """A federated record sharing title+agent with a LOCAL API memory
        (no federation metadata) still imports — the fallback only matches
        previously imported federated rows."""
        # The seeded fixture has a local "ADR-0014 auth decision" memory
        # authored by _AGENT without any fed_* metadata.
        record = _make_compact_record(
            record_id="",
            summary="Federated namesake of a local note.",
            title="ADR-0014 auth decision",
        )
        resp = self._write(server, record)
        assert resp.trigger_code == _mesh_gen.fed_pb2.TriggerCodes.EXHAUSTIVE
        servicer = server.servicer
        assert servicer is not None
        namesakes = [
            m
            for m in servicer._manager.sqlite.list_all(limit=1000)
            if m.title == "ADR-0014 auth decision"
        ]
        assert len(namesakes) == 2  # the local seed + the federated import


# ── GetSubscriptionState ──────────────────────────────────────────────────────


class TestGetSubscriptionState:
    def test_subscription_state_returns_empty_cursor(self, server: MeshServer) -> None:
        """GetSubscriptionState returns an empty cursor (M4: mesh-side persistence in M5)."""
        _wait_for_server(server)
        stub = _stub(server)
        response = stub.GetSubscriptionState(
            _mesh_gen.core_pb2.GetSubscriptionStateRequest(
                peer_id=_PEER_ID,
                project_scope=_PROJECT,
            ),
            timeout=2.0,
        )
        assert response.cursor == ""
        assert response.last_rev == 0
        assert response.last_sync_timestamp == ""

    def test_subscription_state_acl_denied(self, server: MeshServer) -> None:
        """GetSubscriptionState with a disallowed scope → PERMISSION_DENIED."""
        _wait_for_server(server)
        stub = _stub(server)
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.GetSubscriptionState(
                _mesh_gen.core_pb2.GetSubscriptionStateRequest(
                    peer_id=_PEER_ID,
                    project_scope=_PROJECT_DENIED,
                ),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED

    def test_subscription_state_ignores_request_peer_id(self, server: MeshServer) -> None:
        """Review MINOR: ``request.peer_id`` is NOT an identity source.

        Pre-fix the RPC fell back to the caller-asserted
        ``request.peer_id`` — an ACL oracle over arbitrary peer ids (an
        unknown id was DENIED, a guessed-valid id passed). Identity now
        resolves like ListMemories/WriteMemory: metadata or the single
        configured peer — a spoofed ``request.peer_id`` must not change
        the outcome for an otherwise-allowed scope.
        """
        _wait_for_server(server)
        stub = _stub(server)
        response = stub.GetSubscriptionState(
            _mesh_gen.core_pb2.GetSubscriptionStateRequest(
                peer_id="mnemos-ghost",
                project_scope=_PROJECT,
            ),
            timeout=2.0,
        )
        assert response.cursor == ""
        assert response.last_rev == 0
        assert response.last_sync_timestamp == ""


# ── ACL hardening (vesmaro#371/#369 family) ───────────────────────────────────


#: gRPC metadata asserting an UNKNOWN peer id (identity spoof attempt).
_GHOST_METADATA = (("x-mnemos-peer-id", "mnemos-ghost"),)


@pytest.fixture
def acl_server_factory(tmp_path: Path) -> Generator[Any, None, None]:
    """Factory for ACL-matrix servers: one peer, N seeded projects.

    Yields a callable taking ``allowed`` / ``shared`` / ``seed_projects``
    (ACL-hardening matrix axes) that starts an isolated ``MeshServer``
    with one seeded federable memory per seed project. Multiple servers
    per test are supported (each gets its own subdir + socket). All
    servers and managers are torn down after the test.
    """
    started: list[tuple[MeshServer, MemoryManager]] = []

    def _make(
        *,
        allowed: list[str],
        shared: list[str],
        seed_projects: tuple[str, ...] = (_PROJECT, _PROJECT_OTHER),
    ) -> MeshServer:
        n = len(started)
        base = tmp_path / f"acl{n}"
        settings = _settings_with_peer(base, allowed=allowed, shared=shared)
        manager = MemoryManager(settings)
        mock_embedder = MagicMock()
        mock_embedder.embed.return_value = [0.1] * 384
        manager._embedder = mock_embedder
        for project in seed_projects:
            manager.add(
                MemoryCreate(
                    content=f"ACL-hardening seed record for {project}.",
                    title=f"Seed {project}",
                    tags=[f"project:{project}", f"agent:{_AGENT}", "mnemos:decision"],
                    source=MemorySource.MANUAL,
                ),
                project=project,
                agent=_AGENT,
            )
        srv = MeshServer(str(base / "core.sock"), manager, settings, max_workers=2)
        srv.start()
        started.append((srv, manager))
        return srv

    yield _make
    for srv, manager in started:
        srv.stop(grace=0.5)
        manager.close()


class TestAclHardeningFailClosed:
    """Regression matrix for the fail-open family (vesmaro#371/#369).

    Fix- principles ratified by these tests: (a) no implicit "all" — an
    empty effective allowed set (empty allow-list, or "*" with an empty
    shared_projects) denies reads AND writes; (b) an unscoped request is
    the intersection with the peer's allowed set, never the whole corpus;
    (c) every data path is ACL-gated before serve/write, unknown peers
    included.
    """

    def test_unscoped_request_returns_only_allowed_projects(self, acl_server_factory: Any) -> None:
        """(i) Unscoped ListMemories from a limited peer → ONLY its projects.

        The live #371/#369 pair: the peer's allowed set is [_PROJECT] while
        the store also holds _PROJECT_OTHER — an unscoped pull must return
        exactly the allowed project, never the foreign records.
        """
        server = acl_server_factory(allowed=[_PROJECT], shared=[_PROJECT, _PROJECT_OTHER])
        _wait_for_server(server)
        stub = _stub(server)
        response = stub.ListMemories(
            _mesh_gen.core_pb2.ListMemoriesRequest(),
            timeout=2.0,
        )
        assert len(response.records) == 1, "only the allowed project's record"
        for rec in response.records:
            assert f"project:{_PROJECT}" in list(rec.tags)
            assert f"project:{_PROJECT_OTHER}" not in list(rec.tags)

    def test_unscoped_request_wildcard_peer_returns_shared_union(
        self, acl_server_factory: Any
    ) -> None:
        """Wildcard peer + non-empty shared → unscoped pull = shared union."""
        server = acl_server_factory(allowed=["*"], shared=[_PROJECT, _PROJECT_OTHER])
        _wait_for_server(server)
        stub = _stub(server)
        response = stub.ListMemories(
            _mesh_gen.core_pb2.ListMemoriesRequest(),
            timeout=2.0,
        )
        assert len(response.records) == 2, "both shared projects are served"

    def test_empty_allowed_set_denies_read_and_write(self, acl_server_factory: Any) -> None:
        """(ii) Empty allowed_projects → PERMISSION_DENIED on read AND write."""
        server = acl_server_factory(allowed=[], shared=[_PROJECT], seed_projects=(_PROJECT,))
        _wait_for_server(server)
        stub = _stub(server)
        # Read: unscoped …
        with pytest.raises(grpc.RpcError) as unscoped:
            stub.ListMemories(_mesh_gen.core_pb2.ListMemoriesRequest(), timeout=2.0)
        assert unscoped.value.code() == grpc.StatusCode.PERMISSION_DENIED
        # … and scoped (the pre-existing membership deny stays).
        with pytest.raises(grpc.RpcError) as scoped:
            stub.ListMemories(
                _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT]), timeout=2.0
            )
        assert scoped.value.code() == grpc.StatusCode.PERMISSION_DENIED
        # Write: a tagged record the peer would otherwise accept.
        with pytest.raises(grpc.RpcError) as write:
            stub.WriteMemory(
                _mesh_gen.core_pb2.WriteMemoryRequest(
                    record=_to_proto_record(_make_compact_record()),
                    import_mode=_mesh_gen.core_pb2.ImportMode.MERGE,
                ),
                timeout=2.0,
            )
        assert write.value.code() == grpc.StatusCode.PERMISSION_DENIED

    def test_wildcard_with_empty_shared_denies_everything(self, acl_server_factory: Any) -> None:
        """(iii) '*' allow-list + empty shared_projects → DENIED, not 'all'.

        Pre-fix this was the #369 fail-open: the wildcard resolved to an
        empty shared union, the empty union became ``projects=None``, and
        the unscoped query returned EVERY project in the store.
        """
        server = acl_server_factory(allowed=["*"], shared=[], seed_projects=(_PROJECT,))
        _wait_for_server(server)
        stub = _stub(server)
        with pytest.raises(grpc.RpcError) as unscoped:
            stub.ListMemories(_mesh_gen.core_pb2.ListMemoriesRequest(), timeout=2.0)
        assert unscoped.value.code() == grpc.StatusCode.PERMISSION_DENIED
        with pytest.raises(grpc.RpcError) as scoped:
            stub.ListMemories(
                _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT]), timeout=2.0
            )
        assert scoped.value.code() == grpc.StatusCode.PERMISSION_DENIED
        with pytest.raises(grpc.RpcError) as write:
            stub.WriteMemory(
                _mesh_gen.core_pb2.WriteMemoryRequest(
                    record=_to_proto_record(_make_compact_record()),
                    import_mode=_mesh_gen.core_pb2.ImportMode.MERGE,
                ),
                timeout=2.0,
            )
        assert write.value.code() == grpc.StatusCode.PERMISSION_DENIED

    def test_unknown_peer_denied_read_and_write(self, acl_server_factory: Any) -> None:
        """(iv) Unknown peer (spoofed metadata) → DENIED on every data path.

        Pre-fix the unscoped read from an unknown peer skipped the ACL
        gate AND fell into the empty-allowed-set → unfiltered query — a
        full-corpus leak. The scoped read/write were already denied via
        the scope check; they stay denied.
        """
        server = acl_server_factory(
            allowed=[_PROJECT], shared=[_PROJECT], seed_projects=(_PROJECT,)
        )
        _wait_for_server(server)
        stub = _stub(server)
        with pytest.raises(grpc.RpcError) as unscoped:
            stub.ListMemories(
                _mesh_gen.core_pb2.ListMemoriesRequest(),
                timeout=2.0,
                metadata=_GHOST_METADATA,
            )
        assert unscoped.value.code() == grpc.StatusCode.PERMISSION_DENIED
        with pytest.raises(grpc.RpcError) as scoped:
            stub.ListMemories(
                _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT]),
                timeout=2.0,
                metadata=_GHOST_METADATA,
            )
        assert scoped.value.code() == grpc.StatusCode.PERMISSION_DENIED
        with pytest.raises(grpc.RpcError) as write:
            stub.WriteMemory(
                _mesh_gen.core_pb2.WriteMemoryRequest(
                    record=_to_proto_record(_make_compact_record()),
                    import_mode=_mesh_gen.core_pb2.ImportMode.MERGE,
                ),
                timeout=2.0,
                metadata=_GHOST_METADATA,
            )
        assert write.value.code() == grpc.StatusCode.PERMISSION_DENIED

    def test_untagged_record_write_denied_even_for_allowed_peer(
        self, acl_server_factory: Any
    ) -> None:
        """A record WITHOUT a project: tag cannot be ACL'd → DENIED.

        Pre-fix ``if project and …`` skipped the gate entirely and the
        record landed in the project-less namespace regardless of the
        peer's allow-list. Also asserts nothing was persisted.
        """
        server = acl_server_factory(
            allowed=[_PROJECT], shared=[_PROJECT], seed_projects=(_PROJECT,)
        )
        _wait_for_server(server)
        stub = _stub(server)
        record = _make_compact_record(tags=[f"agent:{_AGENT}", "mnemos:decision"])
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.WriteMemory(
                _mesh_gen.core_pb2.WriteMemoryRequest(
                    record=_to_proto_record(record),
                    import_mode=_mesh_gen.core_pb2.ImportMode.MERGE,
                ),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED
        servicer = server.servicer
        assert servicer is not None
        assert (
            servicer._manager.sqlite.find_federated_duplicate(
                fed_id=record.id,
                title=record.title,
                source_agent=record.source_agent,
            )
            is None
        ), "the refused record must not be persisted"

    def test_wildcard_write_to_non_shared_project_denied(self, acl_server_factory: Any) -> None:
        """Wildcard write is bounded by shared_projects (read/write symmetry).

        Pre-fix ``_acl_allows`` short-circuited '*' → True for ANY
        project, letting a wildcard peer write into projects the operator
        never shared. The read side never served those projects; now the
        write side matches.
        """
        server = acl_server_factory(allowed=["*"], shared=[_PROJECT], seed_projects=(_PROJECT,))
        _wait_for_server(server)
        stub = _stub(server)
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.WriteMemory(
                _mesh_gen.core_pb2.WriteMemoryRequest(
                    record=_to_proto_record(_make_compact_record(project=_PROJECT_OTHER)),
                    import_mode=_mesh_gen.core_pb2.ImportMode.MERGE,
                ),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED

    def test_wildcard_scoped_read_of_non_shared_project_denied(
        self, acl_server_factory: Any
    ) -> None:
        """Wildcard scoped read of a non-shared project → DENIED (safety net)."""
        server = acl_server_factory(allowed=["*"], shared=[_PROJECT], seed_projects=(_PROJECT,))
        _wait_for_server(server)
        stub = _stub(server)
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.ListMemories(
                _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT_OTHER]),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED

    def test_shared_projects_wildcard_rejected_at_config(self, tmp_path: Path) -> None:
        """Review MAJOR exploit (1): ``shared_projects=['*']`` is a config error.

        Pre-fix the wildcard flowed through the effective-set resolution
        into ``_intersect_projects`` (wildcard branch → requested
        verbatim), handing a scoped read ANY project while the write
        path stayed bounded — a read/write asymmetry. Refused at the
        config boundary: the server refuses to boot with such a config.
        """
        with pytest.raises(ValidationError, match=r"shared_projects.*\*"):
            _settings_with_peer(tmp_path, allowed=["*"], shared=["*"])

    def test_shared_projects_blank_slug_rejected_at_config(self, tmp_path: Path) -> None:
        """Review MAJOR exploit (2): ``shared_projects=['']`` is a config error.

        Pre-fix the blank slug became an effective entry of ``''``, and
        the SQL ``project IN ('')`` matched every UNTAGGED record (the
        memories column DEFAULTs to ``''``) — asymmetric with the
        WriteMemory untagged-record deny. Refused at the config boundary.
        """
        with pytest.raises(ValidationError, match="blank project slug"):
            _settings_with_peer(tmp_path, allowed=[_PROJECT], shared=[""])


# ── ReadMemory (W3-v1, ADR-0018 amendment 3) ─────────────────────────────────


class TestReadMemory:
    """Single-record fetch by id: mirror of the ListMemories gate matrix."""

    def _list_one_fed_id(self, server: MeshServer) -> str:
        """ListMemories once and return the first record id (a fed: id)."""
        _wait_for_server(server)
        stub = _stub(server)
        response = stub.ListMemories(
            _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT]),
            timeout=2.0,
        )
        assert response.records, "seeded records missing — fixture drift"
        return str(response.records[0].id)

    def test_read_returns_record_by_fed_id(self, server: MeshServer) -> None:
        """A fed:<agent>:<uuid> id from a prior page resolves to the record."""
        record_id = self._list_one_fed_id(server)
        stub = _stub(server)
        response = stub.ReadMemory(
            _mesh_gen.core_pb2.ReadMemoryRequest(record_id=record_id),
            timeout=2.0,
        )
        assert response.record.id == record_id
        assert response.trigger_code == _mesh_gen.fed_pb2.EXHAUSTIVE
        assert response.record.title

    def test_read_by_raw_memory_id(self, server: MeshServer) -> None:
        """A raw memory id (non-fed) resolves too (operator/unit path)."""
        record_id = self._list_one_fed_id(server)
        raw_id = record_id.rsplit(":", 1)[-1]
        stub = _stub(server)
        response = stub.ReadMemory(
            _mesh_gen.core_pb2.ReadMemoryRequest(record_id=raw_id),
            timeout=2.0,
        )
        assert response.record.id.endswith(raw_id)

    def test_read_unknown_id_not_found(self, server: MeshServer) -> None:
        _wait_for_server(server)
        stub = _stub(server)
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.ReadMemory(
                _mesh_gen.core_pb2.ReadMemoryRequest(record_id="fed:agent:does-not-exist"),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.NOT_FOUND

    def test_read_empty_id_invalid_argument(self, server: MeshServer) -> None:
        _wait_for_server(server)
        stub = _stub(server)
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.ReadMemory(_mesh_gen.core_pb2.ReadMemoryRequest(record_id=""), timeout=2.0)
        assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT

    def test_read_no_federate_is_not_found(self, server: MeshServer) -> None:
        """no-federate ids are indistinguishable from absent (no id oracle)."""
        servicer = server.servicer
        assert servicer is not None
        mem = servicer._manager.add(
            MemoryCreate(
                content="secret access key AKIAEXAMPLE123",
                title="Should never be readable",
                tags=[
                    f"project:{_PROJECT}",
                    f"agent:{_AGENT}",
                    "mnemos:decision",
                    "mnemos:no-federate",
                ],
                source=MemorySource.MANUAL,
            ),
            project=_PROJECT,
            agent=_AGENT,
        )
        stub = _stub(server)
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.ReadMemory(
                _mesh_gen.core_pb2.ReadMemoryRequest(record_id=f"fed:{_AGENT}:{mem.id}"),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.NOT_FOUND

    def test_read_denied_project_permission_denied(self, server: MeshServer) -> None:
        """A record in a project outside the allowed set → PERMISSION_DENIED."""
        servicer = server.servicer
        assert servicer is not None
        mem = servicer._manager.add(
            MemoryCreate(
                content="Cross-project record the peer must not see.",
                title="Denied project record",
                tags=[f"project:{_PROJECT_DENIED}", f"agent:{_AGENT}", "mnemos:decision"],
                source=MemorySource.MANUAL,
            ),
            project=_PROJECT_DENIED,
            agent=_AGENT,
        )
        stub = _stub(server)
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.ReadMemory(
                _mesh_gen.core_pb2.ReadMemoryRequest(record_id=f"fed:{_AGENT}:{mem.id}"),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED

    def test_read_project_narrowing_mismatch_denied(self, server: MeshServer) -> None:
        """request.project that does not match the record → PERMISSION_DENIED."""
        record_id = self._list_one_fed_id(server)
        stub = _stub(server)
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.ReadMemory(
                _mesh_gen.core_pb2.ReadMemoryRequest(record_id=record_id, project=_PROJECT_DENIED),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED

    def test_read_moderation_refuse_yields_refused_trigger(
        self, server: MeshServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A moderation refuse is a SUCCESS with trigger_code=REFUSED."""
        record_id = self._list_one_fed_id(server)
        monkeypatch.setattr(
            "vesmaro.mesh_server.build_compact_record", lambda *args, **kwargs: None
        )
        stub = _stub(server)
        response = stub.ReadMemory(
            _mesh_gen.core_pb2.ReadMemoryRequest(record_id=record_id),
            timeout=2.0,
        )
        assert response.trigger_code == _mesh_gen.fed_pb2.REFUSED
        assert not response.record.id


# ── ValidateAgentToken (W3 part 2, ADR-0018-T §3/§8 launch condition 2) ───────


#: Gateway node id used as the token `aud` in these tests (the mesh node
#: that would call this RPC on every agent request).
_GATEWAY_NODE = "mesh-node-1"


def _token_mint_deps(settings: Settings) -> tuple[AgentTokenStore, Any]:
    """Build the mint-side store + key mirroring the servicer's lazy deps.

    The key file lands at the SAME path ``_token_validation_deps``
    resolves (``<data_dir>/agent-token-signing.key``, no config
    override), so tokens minted here verify against the server's key.
    """
    settings.mnemos.data_dir.mkdir(parents=True, exist_ok=True)
    key = load_or_create_signing_key(
        signing_key_path(
            settings.mnemos.data_dir, override=settings.federation.agent_token_key_path
        )
    )
    return AgentTokenStore(settings.db_path), key


class TestValidateAgentToken:
    """Wire wrapper over validate_agent_token — verdict matrix, fail-closed."""

    def _call(
        self,
        server: MeshServer,
        token: str | None,
        *,
        node_id: str = _GATEWAY_NODE,
        scheme: str = "Bearer",
        timeout: float = 2.0,
    ) -> Any:
        """Invoke the RPC; token=None sends NO authorization metadata."""
        _wait_for_server(server)
        stub = _stub(server)
        metadata: list[tuple[str, str]] = [("x-mnemos-peer-id", _PEER_ID)]
        if token is not None:
            metadata.append(("authorization", f"{scheme} {token}"))
        return stub.ValidateAgentToken(
            _mesh_gen.core_pb2.ValidateAgentTokenRequest(gateway_node_id=node_id),
            metadata=metadata,
            timeout=timeout,
        )

    def test_valid_rw_token_full_verdict(self, server: MeshServer, settings: Settings) -> None:
        store, key = _token_mint_deps(settings)
        token, claims = issue_agent_token(
            agent_id="harness-a",
            node_id=_GATEWAY_NODE,
            scope_spec="rw",
            store=store,
            key=key,
        )
        response = self._call(server, token)
        assert response.valid is True
        assert response.agent_id == "harness-a"
        assert list(response.scope) == ["rw"]
        assert response.jti == claims.jti
        assert response.aud == _GATEWAY_NODE
        assert response.exp_ok is True
        assert response.revoked is False
        assert response.reason == ""

    def test_read_scope_propagates(self, server: MeshServer, settings: Settings) -> None:
        store, key = _token_mint_deps(settings)
        token, _claims = issue_agent_token(
            agent_id="harness-ro",
            node_id=_GATEWAY_NODE,
            scope_spec="read",
            store=store,
            key=key,
        )
        response = self._call(server, token)
        assert response.valid is True
        assert list(response.scope) == ["read"]

    def test_revoked_token_fails_closed(self, server: MeshServer, settings: Settings) -> None:
        import time as _time

        store, key = _token_mint_deps(settings)
        token, claims = issue_agent_token(
            agent_id="harness-b",
            node_id=_GATEWAY_NODE,
            scope_spec="read",
            store=store,
            key=key,
        )
        assert store.revoke_jti(claims.jti, now=int(_time.time()))
        response = self._call(server, token)
        assert response.valid is False
        assert response.revoked is True
        assert response.reason == "revoked"
        assert response.agent_id == "harness-b"

    def test_aud_mismatch_rejected(self, server: MeshServer, settings: Settings) -> None:
        """A token minted for ANOTHER gateway node is rejected (aud binding)."""
        store, key = _token_mint_deps(settings)
        token, _claims = issue_agent_token(
            agent_id="harness-c",
            node_id="mesh-node-OTHER",
            scope_spec="read",
            store=store,
            key=key,
        )
        response = self._call(server, token)
        assert response.valid is False
        assert response.reason == "aud_mismatch"

    def test_expired_token_rejected(self, server: MeshServer, settings: Settings) -> None:
        import time as _time

        store, key = _token_mint_deps(settings)
        now = int(_time.time())
        # Minted 2h ago with a 1h TTL — comfortably expired.
        token, _claims = issue_agent_token(
            agent_id="harness-d",
            node_id=_GATEWAY_NODE,
            scope_spec="read",
            store=store,
            key=key,
            ttl_hours=1.0,
            now=now - 7200,
        )
        response = self._call(server, token)
        assert response.valid is False
        assert response.reason == "expired"
        assert response.exp_ok is False

    def test_unknown_jti_fails_closed(self, server: MeshServer, settings: Settings) -> None:
        """A correctly-signed envelope with an UNREGISTERED jti is rejected."""
        import time as _time
        import uuid as _uuid

        _store, key = _token_mint_deps(settings)
        now = int(_time.time())
        claims = AgentTokenClaims(
            iss="mnemos",
            sub="harness-e",
            aud=_GATEWAY_NODE,
            scope=["read"],
            iat=now - 60,
            exp=now + 3600,
            jti=_uuid.uuid4().hex,
        )
        response = self._call(server, encode_agent_token(claims, key))
        assert response.valid is False
        assert response.reason == "unknown_jti"

    def test_garbage_token_is_malformed_not_crash(
        self, server: MeshServer, settings: Settings
    ) -> None:
        _token_mint_deps(settings)  # key exists — rejects on signature, not deps
        response = self._call(server, "not-a-token-at-all")
        assert response.valid is False
        assert response.reason in {"malformed", "bad_signature"}
        assert response.agent_id == ""

    def test_missing_authorization_metadata_is_invalid_argument(self, server: MeshServer) -> None:
        with pytest.raises(grpc.RpcError) as exc_info:
            self._call(server, None)
        assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT

    def test_non_bearer_scheme_is_invalid_argument(
        self, server: MeshServer, settings: Settings
    ) -> None:
        store, key = _token_mint_deps(settings)
        token, _claims = issue_agent_token(
            agent_id="harness-f",
            node_id=_GATEWAY_NODE,
            scope_spec="read",
            store=store,
            key=key,
        )
        with pytest.raises(grpc.RpcError) as exc_info:
            self._call(server, token, scheme="Basic")
        assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT

    def test_empty_gateway_node_id_is_invalid_argument(
        self, server: MeshServer, settings: Settings
    ) -> None:
        store, key = _token_mint_deps(settings)
        token, _claims = issue_agent_token(
            agent_id="harness-g",
            node_id=_GATEWAY_NODE,
            scope_spec="read",
            store=store,
            key=key,
        )
        with pytest.raises(grpc.RpcError) as exc_info:
            self._call(server, token, node_id="")
        assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT

    def test_signing_key_minted_0600_on_first_use(
        self, server: MeshServer, settings: Settings
    ) -> None:
        """First validated RPC mints the key file with owner-only perms."""
        store, key = _token_mint_deps(settings)
        token, _claims = issue_agent_token(
            agent_id="harness-h",
            node_id=_GATEWAY_NODE,
            scope_spec="read",
            store=store,
            key=key,
        )
        assert self._call(server, token).valid is True
        key_path = signing_key_path(
            settings.mnemos.data_dir, override=settings.federation.agent_token_key_path
        )
        assert key_path.exists()
        mode = stat.S_IMODE(key_path.stat().st_mode)
        assert mode == 0o600


# ── Agent data gate (W3 part 3, ADR-0018-T §3 — data-path re-validation) ─────


def _agent_metadata(
    token: str | None,
    agent_id: str,
    *,
    node: str = _PEER_ID,
) -> list[tuple[str, str]]:
    """Build the core-leg metadata triple the gateway stamps on translated
    agent RPCs (mnemos-mesh ``internal/gateway``): peer id + validated
    agent id + the end-to-end bearer relay.

    ``token=None`` omits the authorization key (the no-bearer case);
    ``node`` defaults to the configured peer id — the aud the tokens in
    these tests are minted for (single-hop: gateway node id == peer id).
    """
    md: list[tuple[str, str]] = [("x-mnemos-peer-id", node), ("x-mnemos-agent-id", agent_id)]
    if token is not None:
        md.append(("authorization", f"Bearer {token}"))
    return md


def _issue(settings: Settings, *, agent_id: str, scope: str, node: str = _PEER_ID) -> str:
    """Mint + register a token against the SAME store/key the servicer uses."""
    store, key = _token_mint_deps(settings)
    token, _claims = issue_agent_token(
        agent_id=agent_id,
        node_id=node,
        scope_spec=scope,
        store=store,
        key=key,
    )
    return token


@pytest.fixture
def multi_project_env(
    tmp_path: Path,
) -> Generator[tuple[MeshServer, Settings, MemoryManager], None, None]:
    """Server whose peer is allowed BOTH projects, one seed record in each.

    Isolates the TM §3 NARROWING axis from the peer-ACL axis: the peer
    alone would serve both projects, so any single-project visibility in
    the tests using this fixture comes from the TOKEN's project grants.
    """
    base = tmp_path / "mp"
    settings = _settings_with_peer(
        base, allowed=[_PROJECT, _PROJECT_OTHER], shared=[_PROJECT, _PROJECT_OTHER]
    )
    manager = MemoryManager(settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    manager._embedder = mock_embedder
    for project in (_PROJECT, _PROJECT_OTHER):
        manager.add(
            MemoryCreate(
                content=f"Agent-gate narrowing seed for {project}.",
                title=f"Seed {project}",
                tags=[f"project:{project}", f"agent:{_AGENT}", "mnemos:decision"],
                source=MemorySource.MANUAL,
            ),
            project=project,
            agent=_AGENT,
        )
    srv = MeshServer(str(base / "core.sock"), manager, settings, max_workers=2)
    srv.start()
    yield srv, settings, manager
    srv.stop(grace=0.5)
    manager.close()


class TestAgentDataGate:
    """Per-request token re-validation on ListMemories/ReadMemory/WriteMemory.

    Contract under test (ADR-0018-T §3, criterion 5): the presence of
    ``x-mnemos-agent-id`` switches on the gate — the bearer is
    re-validated per request, identity comes ONLY from the verdict, the
    effective scope is token ∩ transport-peer ACL, and a request WITHOUT
    the key keeps the unchanged federation behaviour (additive).
    """

    def test_no_agent_metadata_keeps_legacy_path(self, server: MeshServer) -> None:
        """No x-mnemos-agent-id → the plain federation path is unchanged."""
        _wait_for_server(server)
        stub = _stub(server)
        response = stub.ListMemories(
            _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT]),
            timeout=2.0,
        )
        assert response.total >= 2  # the seeded fixture records — served

    def test_read_agent_can_list_and_read(self, server: MeshServer, settings: Settings) -> None:
        """A valid read-scoped token serves List and Read (rw ⊃ read too)."""
        token = _issue(settings, agent_id="harness-ro", scope="read")
        _wait_for_server(server)
        stub = _stub(server)
        listed = stub.ListMemories(
            _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT]),
            metadata=_agent_metadata(token, "harness-ro"),
            timeout=2.0,
        )
        assert listed.records, "read agent must list the allowed project"
        record_id = str(listed.records[0].id)
        read = stub.ReadMemory(
            _mesh_gen.core_pb2.ReadMemoryRequest(record_id=record_id),
            metadata=_agent_metadata(token, "harness-ro"),
            timeout=2.0,
        )
        assert read.record.id == record_id
        assert read.trigger_code == _mesh_gen.fed_pb2.EXHAUSTIVE

    def test_read_agent_write_denied(self, server: MeshServer, settings: Settings) -> None:
        """WriteMemory with a read-scoped token → PERMISSION_DENIED, no write."""
        token = _issue(settings, agent_id="harness-ro", scope="read")
        _wait_for_server(server)
        stub = _stub(server)
        record = _make_compact_record(record_id="fed:harness-ro:gate-1")
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.WriteMemory(
                _mesh_gen.core_pb2.WriteMemoryRequest(
                    record=_to_proto_record(record),
                    import_mode=_mesh_gen.core_pb2.ImportMode.MERGE,
                ),
                metadata=_agent_metadata(token, "harness-ro"),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED
        servicer = server.servicer
        assert servicer is not None
        assert servicer._manager.sqlite.find_federated_duplicate(fed_id=record.id) is None

    def test_rw_agent_write_ok(self, server: MeshServer, settings: Settings) -> None:
        """An rw-scoped token imports a record (trigger EXHAUSTIVE + persisted)."""
        token = _issue(settings, agent_id="harness-rw", scope="rw")
        _wait_for_server(server)
        stub = _stub(server)
        record = _make_compact_record(record_id="fed:harness-rw:gate-2")
        response = stub.WriteMemory(
            _mesh_gen.core_pb2.WriteMemoryRequest(
                record=_to_proto_record(record),
                import_mode=_mesh_gen.core_pb2.ImportMode.MERGE,
            ),
            metadata=_agent_metadata(token, "harness-rw"),
            timeout=2.0,
        )
        assert response.trigger_code == _mesh_gen.fed_pb2.EXHAUSTIVE
        assert response.written_id
        servicer = server.servicer
        assert servicer is not None
        assert servicer._manager.get(response.written_id) is not None

    def test_garbage_token_unauthenticated(self, server: MeshServer, settings: Settings) -> None:
        """A non-envelope bearer → UNAUTHENTICATED (fail-closed, no crash)."""
        _token_mint_deps(settings)  # key exists — rejection is on signature
        _wait_for_server(server)
        stub = _stub(server)
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.ListMemories(
                _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT]),
                metadata=_agent_metadata("not-a-token-at-all", "harness-x"),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED

    def test_bad_signature_unauthenticated(self, server: MeshServer, settings: Settings) -> None:
        """A well-formed token signed by a FOREIGN key → UNAUTHENTICATED.

        The forge axis isolated: the envelope is minted against the REAL
        registry (its jti is genuinely registered, agent id / aud / scope
        are all genuine and even match the request metadata), so the ONLY
        failing check is the Ed25519 signature — possession of the store
        without the signing key must not pass step 1 of the check order.
        """
        store, _real_key = _token_mint_deps(settings)  # real key exists at the servicer path
        attacker_key = Ed25519PrivateKey.generate()
        token, _claims = issue_agent_token(
            agent_id="harness-forge",
            node_id=_PEER_ID,
            scope_spec="read",
            store=store,
            key=attacker_key,
        )
        _wait_for_server(server)
        stub = _stub(server)
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.ListMemories(
                _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT]),
                metadata=_agent_metadata(token, "harness-forge"),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED
        assert "bad_signature" in (exc_info.value.details() or "")

    def test_expired_token_unauthenticated(self, server: MeshServer, settings: Settings) -> None:
        """An expired token → UNAUTHENTICATED on the data path."""
        import time as _time

        store, key = _token_mint_deps(settings)
        token, _claims = issue_agent_token(
            agent_id="harness-old",
            node_id=_PEER_ID,
            scope_spec="read",
            store=store,
            key=key,
            ttl_hours=1.0,
            now=int(_time.time()) - 7200,
        )
        _wait_for_server(server)
        stub = _stub(server)
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.ReadMemory(
                _mesh_gen.core_pb2.ReadMemoryRequest(record_id="fed:a:b"),
                metadata=_agent_metadata(token, "harness-old"),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED

    def test_revoked_token_unauthenticated(self, server: MeshServer, settings: Settings) -> None:
        """A revoked jti → UNAUTHENTICATED (denylist checked per request)."""
        import time as _time

        store, key = _token_mint_deps(settings)
        token, claims = issue_agent_token(
            agent_id="harness-rev",
            node_id=_PEER_ID,
            scope_spec="read",
            store=store,
            key=key,
        )
        assert store.revoke_jti(claims.jti, now=int(_time.time()))
        _wait_for_server(server)
        stub = _stub(server)
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.ListMemories(
                _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT]),
                metadata=_agent_metadata(token, "harness-rev"),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED

    def test_spoofed_agent_id_permission_denied(
        self, server: MeshServer, settings: Settings
    ) -> None:
        """A VALID token for agent A claiming x-mnemos-agent-id=B → refused.

        Identity derives ONLY from the validated token (TM T3/T4): the
        claimed metadata id must EQUAL the verdict subject.
        """
        token = _issue(settings, agent_id="harness-real", scope="read")
        _wait_for_server(server)
        stub = _stub(server)
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.ListMemories(
                _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT]),
                metadata=_agent_metadata(token, "harness-impostor"),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED

    def test_missing_bearer_unauthenticated(self, server: MeshServer) -> None:
        """x-mnemos-agent-id WITHOUT an authorization bearer → refused."""
        _wait_for_server(server)
        stub = _stub(server)
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.ListMemories(
                _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT]),
                metadata=_agent_metadata(None, "harness-nobearer"),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED

    def test_non_bearer_scheme_unauthenticated(
        self, server: MeshServer, settings: Settings
    ) -> None:
        """A Basic-scheme authorization on an agent request → refused."""
        token = _issue(settings, agent_id="harness-scheme", scope="read")
        _wait_for_server(server)
        stub = _stub(server)
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.ListMemories(
                _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT]),
                metadata=[
                    ("x-mnemos-peer-id", _PEER_ID),
                    ("x-mnemos-agent-id", "harness-scheme"),
                    ("authorization", f"Basic {token}"),
                ],
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED

    def test_aud_mismatch_rejected(self, server: MeshServer, settings: Settings) -> None:
        """A token minted for ANOTHER gateway node → UNAUTHENTICATED.

        The data path binds aud to the CALLING node (x-mnemos-peer-id)
        — the same value the gateway binds at issuance — so a token
        replayed through a different node fails closed (TM T1).
        """
        token = _issue(settings, agent_id="harness-aud", scope="read", node="mesh-node-OTHER")
        _wait_for_server(server)
        stub = _stub(server)
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.ListMemories(
                _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT]),
                metadata=_agent_metadata(token, "harness-aud"),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED

    def test_token_project_grant_narrows_list(
        self, multi_project_env: tuple[MeshServer, Settings, MemoryManager]
    ) -> None:
        """Unscoped list with a project:other token → ONLY that project.

        The peer alone allows both projects (fixture); the single-project
        page proves the narrowing comes from the TOKEN (TM §3 effective
        scope = token ∩ transport-peer ACL).
        """
        server, settings, _manager = multi_project_env
        token = _issue(settings, agent_id="harness-narrow", scope=f"read,project:{_PROJECT_OTHER}")
        _wait_for_server(server)
        stub = _stub(server)
        response = stub.ListMemories(
            _mesh_gen.core_pb2.ListMemoriesRequest(),
            metadata=_agent_metadata(token, "harness-narrow"),
            timeout=2.0,
        )
        assert response.records, "the narrowed project must still be served"
        served_projects = {
            t[len("project:") :]
            for rec in response.records
            for t in rec.tags
            if t.startswith("project:")
        }
        assert served_projects == {_PROJECT_OTHER}

    def test_token_project_grant_blocks_other_projects(
        self, multi_project_env: tuple[MeshServer, Settings, MemoryManager]
    ) -> None:
        """A scoped list for a non-granted project → PERMISSION_DENIED, and a
        write into a non-granted project → REFUSED (narrowing on write too).
        """
        server, settings, _manager = multi_project_env
        token = _issue(settings, agent_id="harness-narrow", scope=f"rw,project:{_PROJECT_OTHER}")
        _wait_for_server(server)
        stub = _stub(server)
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.ListMemories(
                _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT]),
                metadata=_agent_metadata(token, "harness-narrow"),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED
        record = _make_compact_record(record_id="fed:harness-narrow:gate-3", project=_PROJECT)
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.WriteMemory(
                _mesh_gen.core_pb2.WriteMemoryRequest(
                    record=_to_proto_record(record),
                    import_mode=_mesh_gen.core_pb2.ImportMode.MERGE,
                ),
                metadata=_agent_metadata(token, "harness-narrow"),
                timeout=2.0,
            )
        assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED
