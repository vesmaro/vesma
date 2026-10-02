"""MnemosCore gRPC server on Unix socket (#105 M4.0).

Implements the ``MnemosCore`` service defined in
``federation/proto/mnemos_core_api.proto`` as the mnemos-side counterpart
to :mod:`vesma.mesh_client` (the client that talks to the mesh binary).

Architectural role (ArchCom 2026-07-17 federation contract):
    * **Vesma is the source of truth** for storage AND moderation. This
      server owns the SQLite store and runs the moderation pipeline on
      both export (:rpc:`ListMemories`) and import (:rpc:`WriteMemory`).
    * **The mesh is a dumb carrier** (criterion 1). It forwards
      already-redacted :class:`~vesma.compact.CompactRecord` envelopes
      and does NOT duplicate the ACL. The ACL GATE lives here (Q4
      decision): this server returns ``PERMISSION_DENIED`` for any
      ``project_scope`` the caller is not allowed to access.
    * **Cursor ownership is split mint/persist** (Q3 + ADR-0020,
      archcom 2026-09-20). This server MINTS opaque
      ``ListMemoriesResponse.cursor`` checkpoints and validates
      ``resume_cursor`` (garbage → ``INVALID_ARGUMENT``); durable cursor
      PERSISTENCE stays mesh-side — the mesh echoes the token
      byte-for-byte and never parses it (criterion 1).
      :rpc:`GetSubscriptionState` still returns an empty cursor (Q3;
      ``SetSubscriptionState`` deferred by ADR-0020 rule 7).

Transport:
    Unix socket + gRPC (criterion 8). The socket path comes from
    :attr:`vesma.config.MeshConfig.socket_path`. The server creates
    the socket (unlike :class:`~vesma.mesh_client.MeshClient`, which
    connects to it); it is the listener for the mesh↔Vesma channel.

    W2.5 dual-mode (ADR-0019 option 1, ratified archcom 2026-09-20):
    when :attr:`vesma.config.MeshConfig.tcp` is enabled, the SAME
    grpcio server additionally listens on TCP with mesh-CA mTLS —
    :meth:`MeshServer.start` calls ``add_secure_port`` with
    ``RequireAndVerifyClientCert`` — for the standalone mesh Deployment.
    Default OFF; explicit opt-in only, no auto-fallback between
    transports (ADR-0019 rejects option 2).

Import strategy for generated stubs
-----------------------------------
Reuses :mod:`vesma._mesh_gen` (the same shim the client uses) to
import the generated ``mnemos_core_api_pb2_grpc`` /
``mnemos_core_api_pb2`` modules without touching ``sys.path`` here.

S2 meta-mirror (ADR-0021 Q10.2/Q10.3, chairman ruling 2026-09-20)
-------------------------------------------------------------------
:rpc:`SyncMetadata` serves metadata-only pages of the
``federation_index`` (the S2 poll-first export leg; body factored into
:meth:`VesmaCoreServicer.build_metadata_sync_response` for unit
testing), :rpc:`UpsertIndexEntries` imports peer metadata into the index
(S2 import leg, per-entry gate counters + fail-closed ACL). Both reuse
the ``FederationPeer`` wire messages — the mesh relays them verbatim.
:func:`_metadata_stream_event` fills the ``SubscribeStream.record``
oneof's ``metadata`` variant for the standing-subscription goal.

ACL model
---------
The ACL reuses :class:`vesma.config.PeerConfig.allowed_projects` from
the federation config. The mesh is authenticated as a single logical
peer (its A2A id) and carries the originating peer's ``project_scope``
in each request. The server checks ``project_scope`` against the
configured peer's ``allowed_projects`` (or the global ``["*"]``
wildcard) and returns ``PERMISSION_DENIED`` when the scope is
disallowed. Fail-closed: unknown peer or empty allow-list → refuse.

Agent data gate (W3 part 3, ADR-0018 variant (c) + ADR-0018-T §3)
------------------------------------------------------------------
The mesh's AgentGateway terminates agent-leg RPCs and TRANSLATES them
to core-leg ``ListMemories`` / ``ReadMemory`` / ``WriteMemory`` calls
stamped with three metadata keys: ``authorization: Bearer <token>``
(end-to-end token relay), ``x-mnemos-agent-id`` (the VALIDATED verdict
agent id), and ``x-mnemos-peer-id`` (the gateway node id — the same
identity the federation leg uses). This module re-validates the token
ON THE DATA PATH, per request (TM §3 / criterion 5: the mesh is
transport, not an ACL authority — defense-in-depth against a
compromised node calling the data RPCs directly):

* **Additive**: a request WITHOUT ``x-mnemos-agent-id`` takes the
  unchanged federation path (UDS/mTLS trust as before).
* **Presence switches on the gate**: missing bearer →
  ``UNAUTHENTICATED``; any token rejection (signature, expiry,
  revocation, unknown jti, node binding) → ``UNAUTHENTICATED``; a
  claimed agent id that does not match the verdict's ``sub`` →
  ``PERMISSION_DENIED`` (identity derives ONLY from the signature —
  TM T3/T4); a validation-backend failure → ``INTERNAL`` (fail-closed).
  The node-binding (``aud``) check runs against the calling node's
  ``x-mnemos-peer-id`` — the same value the gateway binds at issuance
  (single-hop topology, TM T1).
* **Effective scope = token scope ∩ transport-peer ACL** (TM §3),
  resolved only here. The transport-peer ACL is the existing
  fail-closed ``allowed_projects`` gate above (ADR-0016 per-peer ACL —
  the agent rides the same core leg, so it can never see more than
  that leg already permits); the token's ``project:`` grants can only
  NARROW it. The class grant gates the method: ``WriteMemory`` requires
  ``rw`` (a ``read`` token is refused regardless of the peer ACL);
  list/read are open to both ``read`` and ``rw`` under the same project
  filtering as the plain path — no filter bypass.
* **T7 logging**: agent_id / jti / coarse reason only — never token
  values, never record content.

Fail-closed contract (ACL hardening, vesma#371/#369 family):

* **No implicit "all".** The EFFECTIVE allowed set of a peer is its
  ``allowed_projects`` verbatim, or — for the explicit ``["*"]``
  wildcard — the global ``shared_projects`` union. An EMPTY effective
  set (unknown peer, empty allow-list, ``"*"`` with an empty
  ``shared_projects``) permits NOTHING: the data RPCs return
  ``PERMISSION_DENIED``. It never widens into an unfiltered query.
* **Unscoped = intersection.** A request without a project filter is
  served the intersection of the corpus with the peer's effective
  allowed set — never the whole corpus.
* **Every data path is gated before serve/write.** :rpc:`ListMemories`
  and :rpc:`WriteMemory` enforce the gate unconditionally (scoped AND
  unscoped); :rpc:`WriteMemory` additionally refuses records without a
  ``project:`` tag (an untagged record cannot be ACL'd).

Security notes:
    * The server binds a Unix socket with filesystem permissions.
      Default modes are ``0600`` socket / ``0700`` dir (vesma user
      only); ``mesh.socket_group_access: true`` switches to ``0660`` /
      ``0770`` for shared-volume deployments (fsGroup / compose
      ``user:``) where the mesh binary dials as a different uid in the
      same gid. The operator is responsible for the enclosing dir
      ownership.
    * No TLS on the Unix socket — local-only transport (criterion 11).
    * TCP leg (W2.5, ADR-0019): mTLS with the COMMON mesh CA. The server
      presents the mnemos-core identity leaf
      (:attr:`vesma.config.MeshTCPTLSConfig.cert_file`) and REQUIRES a
      client certificate chained to the mesh CA
      (:attr:`vesma.config.MeshTCPTLSConfig.ca_file`) — anonymous TLS
      is rejected at the handshake. When the peer's
      :attr:`vesma.config.PeerConfig.mtls_cert_fingerprint` is set,
      the presented client-cert fingerprint is additionally PINNED
      (``sha256:<hex>`` of the DER leaf, symmetric to the mesh's peer
      leg, ``vesma-mesh/internal/mtls``) — enforced in the servicer on
      every data RPC over TLS connections.
    * The secrets scanner runs on :rpc:`WriteMemory` via the
      :class:`~vesma.manager.MemoryManager.add` Layer 1 path; a
      detected secret auto-tags ``mnemos:no-federate`` so the record is
      excluded from future federation (defence-in-depth Layer 1).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import ssl
import threading
import time
from collections.abc import Sequence
from concurrent import futures
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import grpc
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from vesma import __version__ as _mnemos_version
from vesma import _mesh_gen
from vesma.agent_tokens import (
    AgentTokenStore,
    load_or_create_signing_key,
    scope_allows_write,
    signing_key_path,
    token_project_grants,
    validate_agent_token,
)
from vesma.compact import (
    CONTENT_STATE_AVAILABLE,
    METADATA_SCHEMA,
    CompactRecord,
    FederationIndexEntry,
    build_compact_record,
    canonical_metadata_timestamp,
    title_matches_blocklist,
)
from vesma.config import MeshTCPTLSConfig, PeerConfig, Settings
from vesma.federation_server import verify_mtls_fingerprint
from vesma.manager import MemoryManager
from vesma.models import NO_FEDERATE_TAG, MemoryCreate, MemorySource
from vesma.moderation import ModerationVerdict, moderate
from vesma.trigger_codes import TriggerCode

if TYPE_CHECKING:
    from types import TracebackType

logger = logging.getLogger(__name__)

__all__ = [
    "AgentIdentity",
    "MeshServer",
    "MeshTCPLegError",
    "VesmaCoreServicer",
]

#: Default page size for :rpc:`ListMemories` when the request omits ``limit``.
#:
#: Capped to keep a single response bounded — the mesh paginates via
#: ``has_more`` (contract §3.1). Mirrors the federation server's search
#: cap of 20.
_DEFAULT_PAGE_SIZE: int = 50

#: Hard ceiling on the page size a caller may request. A request with
#: ``limit > _MAX_PAGE_SIZE`` is clamped down. Prevents a misbehaving or
#: hostile mesh from requesting the entire corpus in one RPC.
_MAX_PAGE_SIZE: int = 500

#: Cursor format version (ADR-0020). The wire cursor is an OPAQUE string
#: owned by mnemos-core; the version tag inside the payload lets core
#: evolve the format later without a wire break — a core that mints v2
#: still rejects v1-and-older tokens only by choice, not by accident.
_CURSOR_VERSION: int = 1

#: Hard cap on the wire length of a resume cursor. A minted v1 token is
#: ~40 chars; anything longer than this is not one of ours (padding bomb,
#: foreign token) → :class:`CursorError` before any decode work.
_CURSOR_MAX_LEN: int = 128

#: SQLite rowid ceiling (``2**63 - 1``): passing a larger int as a bound
#: parameter raises ``sqlite3.OverflowError`` (an UNCAUGHT error would
#: kill the RPC with ``UNKNOWN``) — reject at the contract boundary with
#: ``INVALID_ARGUMENT`` instead (ADR-0020 rule 3).
_SQLITE_ROWID_MAX: int = 9223372036854775807


class CursorError(ValueError):
    """A resume cursor is malformed, foreign, or of an unknown format.

    Raised by :func:`_parse_resume_cursor`; the servicer maps it to
    ``INVALID_ARGUMENT`` (ADR-0020 rule 3: garbage is rejected loudly —
    never silently reinterpreted, never guessed; the mesh degrades to a
    fresh subscribe).
    """


class MeshTCPLegError(RuntimeError):
    """The mesh TCP leg failed to come up (ADR-0019 amendment 3c).

    Raised by :meth:`MeshServer.start` when ``add_secure_port`` fails
    (port in use, bad address) — STARTUP FAIL-FAST: silent degradation
    is forbidden. There is deliberately no k8s probe on 8790
    (amendment 3c); a CrashLoop is the visibility mechanism. The
    message names the bind address so the operator can tell port
    collision from misconfiguration at a glance.
    """


class CompactImportStatus(Enum):
    """Outcome of the shared in-process compact-record import path.

    Mirrors the :rpc:`WriteMemory` trigger-code taxonomy so transport
    callers (:rpc:`WriteMemory` itself) and in-process callers (the S2
    lazy-fetch command) classify outcomes identically — the Go pull's
    written/duplicate/gated split maps 1:1 onto these.
    """

    #: Content persisted via :meth:`MemoryManager.add` (proto: EXHAUSTIVE).
    WRITTEN = "written"
    #: #359/#362 duplicate — an existing row already carries this
    #: ``fed_id``; nothing was re-written (proto: ALREADY_EXHAUSTED).
    DUPLICATE = "duplicate"
    #: Refused by the ACL gate (empty effective set / no project tag /
    #: project not allowed) — proto: REFUSED + PERMISSION_DENIED.
    REFUSED_ACL = "refused_acl"
    #: Refused by mnemos-side moderation (proto: REFUSED, no gRPC error).
    REFUSED_MODERATION = "refused_moderation"


@dataclass(frozen=True, slots=True)
class CompactImportResult:
    """One :meth:`VesmaCoreServicer.import_compact_record` outcome.

    ``written_id`` carries the storage id on WRITTEN and the EXISTING
    id on DUPLICATE; ``reason`` is an operator-actionable refusal
    message (empty unless refused).
    """

    status: CompactImportStatus
    written_id: str = ""
    reason: str = ""


#: Prefix on pinned fingerprint strings — the same convention as the
#: mesh's peer leg (``vesma-mesh/internal/mtls.FingerprintPrefix``):
#: ``sha256:<hex-of-DER-leaf>``. Bare hex (no prefix) is accepted for
#: operator convenience and normalised before the constant-time compare.
_FINGERPRINT_PREFIX: str = "sha256:"

#: gRPC metadata key the mesh's AgentGateway stamps on every translated
#: core-leg data RPC (W3, ADR-0018 variant (c)): ``x-mnemos-agent-id``
#: carries the mnemos-VALIDATED verdict agent id — never a caller-asserted
#: identity (ADR-0018-T T3/T4). Its PRESENCE switches on the agent data
#: gate: a request without it takes the unchanged federation path.
_AGENT_ID_METADATA_KEY: str = "x-mnemos-agent-id"


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    """The authenticated agent identity resolved for one data RPC.

    Produced by :meth:`VesmaCoreServicer._agent_data_gate` (W3 part 3):
    every field comes from the re-validated token verdict — identity is
    derived ONLY from the signature (ADR-0018-T T3), never from the
    caller-asserted ``x-mnemos-agent-id`` (which is matched against the
    verdict and must equal it, else the RPC is refused).
    """

    #: Token ``sub`` — the mnemos-validated agent id (audit/log key).
    agent_id: str
    #: Token ``jti`` — per-request attribution in logs (ADR-0018-T T7).
    jti: str
    #: Canonical token scope grants (``read``/``rw`` + ``project:<slug>``).
    scope: tuple[str, ...]


def _narrow_by_agent_scope(allowed_projects: list[str], agent_scope: Sequence[str]) -> list[str]:
    """Intersect a peer's effective allowed set with the token's project grants.

    TM §3: the effective scope on the gateway path is ``token scope ∩
    transport-peer ACL``, resolved ONLY in Vesma (criterion 5). The
    transport-peer ACL here is the existing fail-closed
    :attr:`vesma.config.PeerConfig.allowed_projects` gate — the agent
    rides the same core leg the federation peer uses, so it is bounded by
    whatever that leg already permits. The token's ``project:<slug>``
    grants can only NARROW that set (an empty grant list narrows nothing:
    "all projects the effective scope allows", per the scope grammar); a
    ``"*"`` peer wildcard is already expanded to the ``shared_projects``
    union by the caller, so the intersection is always concrete slugs.
    An empty result permits nothing — the caller's existing fail-closed
    denial applies unchanged.
    """
    token_projects = token_project_grants(agent_scope)
    if not token_projects:
        return allowed_projects
    granted = set(token_projects)
    narrowed = [p for p in allowed_projects if p in granted]
    if not narrowed:
        # The peer's own set is non-empty but the token grants miss it
        # entirely — name the cause so the refusal is not misread as a
        # peer-ACL problem (correlate via the gate-ok log line).
        logger.info(
            "mesh_server: agent token project grants narrowed the effective "
            "allowed set to empty (grants=%s)",
            sorted(granted),
        )
    return narrowed


def _tcp_server_credentials(tls: MeshTCPTLSConfig) -> grpc.ServerCredentials:
    """Build the mTLS server credentials for the TCP leg (ADR-0019).

    Reads the PEM material from the configured paths (NEVER hardcoded —
    the paths are the chart's mount contract): the core-identity leaf +
    key for the server side, and the mesh-CA bundle as the client trust
    root with ``require_client_auth=True`` — i.e. gRPC's
    ``RequireAndVerifyClientCert``: every caller must present a
    certificate chaining to the mesh CA; anonymous TLS connections are
    rejected at the handshake.

    Raises:
        MeshTCPLegError: A file is unreadable or the PEM is invalid —
            startup fail-fast with the offending path in the message.
    """
    try:
        cert = Path(tls.cert_file).read_bytes()
        key = Path(tls.key_file).read_bytes()
        ca = Path(tls.ca_file).read_bytes()
    except OSError as exc:
        raise MeshTCPLegError(
            f"mesh tcp leg: cannot read TLS material "
            f"(cert={tls.cert_file!r} key={tls.key_file!r} ca={tls.ca_file!r}): {exc}"
        ) from exc
    try:
        return grpc.ssl_server_credentials(
            [(key, cert)],
            root_certificates=ca,
            require_client_auth=True,
        )
    except RuntimeError as exc:
        raise MeshTCPLegError(
            f"mesh tcp leg: invalid TLS material in "
            f"cert={tls.cert_file!r} / key={tls.key_file!r} / ca={tls.ca_file!r}: {exc}"
        ) from exc


def _mint_cursor(rowid: int) -> str:
    """Mint an opaque resume checkpoint for a delivered storage position.

    Format (CORE-PRIVATE — opaque to the mesh, do not change without
    bumping :data:`_CURSOR_VERSION`):
    ``base64url(json({"v": 1, "rowid": <int>}).encode("utf-8"))``, unpadded.

    Semantics: "records up to and including this storage rowid have been
    delivered for this filter". Resuming yields rows with ``rowid >``
    the checkpoint (see :func:`_parse_resume_cursor` and
    ``SQLiteStore.list_all_for_mesh``). ``rowid <= 0`` mints an EMPTY
    cursor ("nothing delivered yet" — byte-identical to a pre-ADR-0020
    core, so old meshes degrade to fresh-subscribe for free).
    """
    if rowid <= 0:
        return ""
    payload = json.dumps({"v": _CURSOR_VERSION, "rowid": int(rowid)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).rstrip(b"=").decode("ascii")


def _parse_resume_cursor(cursor: str) -> int:
    """Validate an opaque resume cursor and return its rowid checkpoint.

    Inverse of :func:`_mint_cursor`. Strict by contract (ADR-0020 rule 3):
    anything that is not EXACTLY a cursor this core format defines —
    non-base64, non-JSON, wrong structure, unknown ``v``, non-integer
    ``rowid``, ``rowid`` outside ``[1, 2**63 - 1]`` (SQLite cannot bind a
    larger value — an uncaught ``OverflowError`` would surface as
    ``UNKNOWN``), or a token longer than :data:`_CURSOR_MAX_LEN` —
    raises :class:`CursorError` → the RPC fails with ``INVALID_ARGUMENT``
    instead of returning a silently-empty page.
    """
    if len(cursor) > _CURSOR_MAX_LEN:
        raise CursorError(f"resume cursor too long ({len(cursor)} > {_CURSOR_MAX_LEN})")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = base64.urlsafe_b64decode(padded.encode("ascii"))
        decoded = json.loads(payload)
    except ValueError as exc:  # binascii.Error, UnicodeError, JSONDecodeError ⊂ ValueError
        raise CursorError(f"malformed resume cursor: {exc}") from exc
    if not isinstance(decoded, dict):
        raise CursorError("malformed resume cursor: not a core cursor object")
    version = decoded.get("v")
    if version != _CURSOR_VERSION:
        raise CursorError(f"unsupported resume cursor version: {version!r}")
    rowid = decoded.get("rowid")
    if (
        not isinstance(rowid, int)
        or isinstance(rowid, bool)
        or not (1 <= rowid <= _SQLITE_ROWID_MAX)
    ):
        raise CursorError(f"invalid resume cursor checkpoint: {rowid!r}")
    return rowid


def _trigger_code_to_proto(code: TriggerCode) -> Any:
    """Map a :class:`TriggerCode` to the generated ``TriggerCodes`` enum.

    The generated ``fed_pb2.TriggerCodes`` is an int enum with the same
    names as :class:`TriggerCode` (``EXHAUSTIVE`` / ``REFUSED`` / ...).
    We look up by name so the mapping survives enum reordering on the
    proto side as long as the names stay stable (they are part of the
    wire contract).
    """
    return getattr(_mesh_gen.fed_pb2.TriggerCodes, code.value)


def _compact_to_proto(record: CompactRecord, *, revision: int = 0) -> Any:
    """Marshal a :class:`CompactRecord` to the protobuf ``CompactRecord``.

    Mirrors :func:`vesma.mesh_client._compact_to_proto` so the server
    and client agree on the wire shape. The record is already
    moderation-processed by the time it reaches this layer.

    ``revision`` (ADR-0021 Q10.6) is the STORAGE revision of the source
    memory row (SQLite rowid) at export time — proto-side-only provenance
    the Pydantic envelope does not carry. ``0`` (default) = unknown.
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
        revision=int(revision),
    )


def _compact_from_proto(pb_record: Any) -> CompactRecord:
    """Marshal a protobuf ``CompactRecord`` back to :class:`CompactRecord`.

    Inverse of :func:`_compact_to_proto`. Copies protobuf repeated
    fields into plain ``list`` objects so the result is
    JSON-serialisable and behaves like any other :class:`CompactRecord`.
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


def _metadata_to_proto(entry: FederationIndexEntry) -> Any:
    """Marshal a :class:`FederationIndexEntry` to the protobuf
    ``MetadataRecord`` (S2 meta-mirror, ADR-0021 Q10.3).

    Wire fields exactly per ``federation.proto::MetadataRecord``: ``id``,
    ``type``, ``title``, ``tags``, ``project``, ``source_agent``,
    ``source_peer``, ``timestamp``, ``schema_version``, plus the additive
    ``origin_peer`` and ``content_state`` (fields 10/11, chairman ruling
    2026-09-20 — now first-class wire fields). ``received_at`` is
    storage-side only (when THIS core received the row) and never leaves
    the node.
    """
    return _mesh_gen.fed_pb2.MetadataRecord(
        id=entry.id,
        type=entry.type,
        title=entry.title,
        tags=list(entry.tags),
        project=entry.project,
        source_agent=entry.source_agent,
        source_peer=entry.source_peer,
        timestamp=entry.timestamp,
        schema_version=entry.schema_version,
        origin_peer=entry.origin_peer,
        content_state=entry.content_state,
    )


def _metadata_entry_from_proto(pb_record: Any, *, sender_peer_id: str) -> FederationIndexEntry:
    """Unmarshal a protobuf ``MetadataRecord`` for import (S2 import leg).

    Inverse of :func:`_metadata_to_proto`, with three import-side
    normalisations (the wire is untrusted):

    * ``origin_peer`` empty or ``"self"`` → the AUTHENTICATED sender's
      peer id: only the local core mints ``origin_peer='self'`` about its
      own corpus, so a foreign record claiming ``self`` (or omitting the
      field — proto3 default) is re-stamped to the sender. A foreign
      record must never enter the local origin namespace (it would
      corrupt ``purge_origin('self')`` and local/remote attribution).
    * ``content_state`` empty → ``available`` (proto3 default; the proto
      documents empty-as-available for older senders).
    * ``timestamp`` → the canonical UTC form via
      :func:`vesma.compact.canonical_metadata_timestamp` (review
      blocker 2): ISO-8601 is parsed, converted to UTC and stored in the
      fixed-width ``%Y-%m-%dT%H:%M:%S.%fZ`` form so the store's
      LWW-by-string comparison is chronologically honest — a ``+03:00``
      offset or a missing fraction must not make an older instant win,
      and a far-future stamp must not win forever.

    Raises:
        ValueError: the record violates the entry contract — a foreign
            ``schema_version`` pin (explicit only; empty defaults to the
            pinned version), or an unparseable / future-dated
            ``timestamp`` (beyond
            :data:`vesma.compact.TIMESTAMP_FUTURE_SLACK`). Construction
            of the entry may additionally raise
            :class:`pydantic.ValidationError` (unknown
            ``content_state``, empty ``id``, title > 256 chars). The RPC
            layer counts both in ``rejected_by_gate``; one bad entry
            never aborts a batch.
    """
    schema_version = str(pb_record.schema_version)
    if schema_version and schema_version != METADATA_SCHEMA:
        raise ValueError(f"foreign schema_version {schema_version!r}")
    origin_peer = str(pb_record.origin_peer)
    if origin_peer in ("", "self"):
        origin_peer = sender_peer_id
    return FederationIndexEntry(
        id=str(pb_record.id),
        type=str(pb_record.type),
        title=str(pb_record.title),
        tags=[str(t) for t in pb_record.tags],
        project=str(pb_record.project),
        source_agent=str(pb_record.source_agent),
        source_peer=str(pb_record.source_peer),
        origin_peer=origin_peer,
        content_state=str(pb_record.content_state) or CONTENT_STATE_AVAILABLE,
        timestamp=canonical_metadata_timestamp(str(pb_record.timestamp)),
        schema_version=METADATA_SCHEMA,
        received_at="",  # stamped by the store at upsert time
    )


def _metadata_stream_event(
    entry: FederationIndexEntry,
    *,
    event_type: int | None = None,
    cursor: str = "",
) -> Any:
    """Build a ``SubscribeStream`` element carrying the METADATA oneof variant.

    The phase-1 oneof contract (ADR-0021 ruling 3: "metadata-oneof enters
    S2 phase 1") lives on ``federation.proto::SubscribeStream`` —
    ``oneof record { CompactRecord compact = 2; MetadataRecord metadata
    = 3; }`` (verified against the proto: SyncMetadata's own messages
    carry NO oneof — ``MetadataSyncResponse.records`` is a plain
    ``repeated MetadataRecord``). This helper fills the ``metadata``
    variant so the standing-subscription goal (ruling 2) has a
    substrate-ready marshaller; the Go mesh owns the actual stream.

    Args:
        entry: The index row to advertise.
        event_type: ``SubscribeStream.EventType`` value; default
            ``RECORD_ADDED`` (the common metadata case — a record
            appeared on the origin).
        cursor: Resume token echo (empty for substrate-level events).
    """
    if event_type is None:
        event_type = _mesh_gen.fed_pb2.SubscribeStream.RECORD_ADDED
    return _mesh_gen.fed_pb2.SubscribeStream(
        event_type=event_type,
        metadata=_metadata_to_proto(entry),
        cursor=cursor,
    )


def _acl_allows(peer: PeerConfig, project_scope: str) -> bool:
    """Return ``True`` if ``project_scope`` is allowed for ``peer``.

    Mirrors :func:`vesma.federation_server._acl_allows` so the mesh↔
    Vesma ACL uses the same semantics as the HTTP federation pull path:
    ``["*"]`` is the explicit wildcard; empty list = none (fail-closed).
    """
    if not peer.allowed_projects:
        return False
    if "*" in peer.allowed_projects:
        return True
    return project_scope in peer.allowed_projects


def _resolve_peer(settings: Settings, peer_id: str) -> PeerConfig | None:
    """Look up a peer by A2A id in the federation config.

    Returns ``None`` when the peer is unknown — the caller treats this
    as ``PERMISSION_DENIED`` (fail-closed, mirroring
    :func:`vesma.federation_server.handle_pull` step 1).
    """
    return settings.federation.peers.get(peer_id)


def _clamp_page_limit(limit: int) -> int:
    """Clamp the requested page size to ``[_DEFAULT_PAGE_SIZE, _MAX_PAGE_SIZE]``.

    A request with ``limit <= 0`` (proto default) gets the default page
    size. A request above the ceiling is clamped down.
    """
    if limit <= 0:
        return _DEFAULT_PAGE_SIZE
    return min(limit, _MAX_PAGE_SIZE)


def _memory_type_for_filter(tags: list[str]) -> str:
    """Return the ``mnemos:<subtype>`` value for a memory, or ``""`` if none.

    Mirrors :func:`vesma.federation_server._memory_type_for_filter` so
    the type filter on :rpc:`ListMemories` uses the same semantics as
    the HTTP pull path. ``mnemos:no-federate`` is skipped.
    """
    for tag in tags:
        if not tag.startswith("mnemos:"):
            continue
        suffix = tag[len("mnemos:") :]
        if suffix == "no-federate":
            continue
        return suffix
    return ""


def _tag_value(tags: list[str], prefix: str) -> str:
    """Extract the value after ``prefix`` from the first matching tag.

    Returns ``""`` when no tag starts with ``prefix``. Used to parse
    ``project:<slug>`` and ``agent:<slug>`` from a compact record's tags
    without importing the full tag-contract validator.
    """
    for tag in tags:
        if tag.startswith(prefix):
            return tag[len(prefix) :]
    return ""


# ── Servicer ──────────────────────────────────────────────────────────────────


class VesmaCoreServicer:
    """gRPC servicer implementing the six ``MnemosCore`` RPCs.

    The servicer holds a reference to the :class:`MemoryManager` (for
    SQLite access + moderation) and the :class:`Settings` (for ACL +
    federation thresholds). It is stateless beyond those references —
    no per-RPC state, no STORED cursors (Q3: persistence is mesh-side;
    ADR-0020 cursors are minted per-response from storage positions, so
    core keeps no cursor state at all).

    The servicer is constructed by :class:`MeshServer` and registered on
    the gRPC server via ``add_MnemosCoreServicer_to_server``. It is safe
    to construct one servicer and register it on one server.
    """

    def __init__(
        self,
        manager: MemoryManager,
        *,
        settings: Settings,
        start_time: float | None = None,
    ) -> None:
        self._manager: MemoryManager = manager
        self._settings: Settings = settings
        self._start_time: float = start_time if start_time is not None else time.monotonic()
        # W3 part 2 (ValidateAgentToken): lazily-initialised agent-token
        # validation deps — see _token_validation_deps. None until the
        # first agent RPC arrives (first-use key mint, ADR-0018-T §3).
        self._token_store: AgentTokenStore | None = None
        self._token_key: Ed25519PrivateKey | None = None
        self._token_deps_lock: threading.Lock = threading.Lock()

    # ── ACL helper ─────────────────────────────────────────────────────────

    def _check_acl(
        self,
        peer_id: str,
        project_scope: str,
        context: grpc.ServicerContext[Any, Any],
    ) -> PeerConfig | None:
        """Resolve the peer and enforce the ACL GATE (Q4).

        Returns the :class:`PeerConfig` when access is allowed, or
        ``None`` after setting the gRPC status to ``PERMISSION_DENIED``
        when the peer is unknown or the scope is disallowed.

        Fail-closed: unknown peer, empty ``allowed_projects``, or a
        scope not in the allow-list all return ``None``.
        """
        peer = _resolve_peer(self._settings, peer_id)
        if peer is None:
            logger.info(
                "mesh_server: refused — peer_id=%s not in peers config",
                peer_id,
            )
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details(f"peer {peer_id!r} not configured")
            return None
        if not _acl_allows(peer, project_scope):
            logger.info(
                "mesh_server: refused — project_scope=%s not allowed for peer_id=%s",
                project_scope,
                peer_id,
            )
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details(f"ACL REFUSED: project_scope {project_scope!r} not allowed")
            return None
        return peer

    # ── TLS fingerprint pin (W2.5 TCP leg) ─────────────────────────────────

    def _enforce_tls_client_pin(
        self,
        peer: PeerConfig,
        context: grpc.ServicerContext[Any, Any],
    ) -> bool:
        """Enforce the pinned client-cert fingerprint on TLS connections.

        ADR-0019 auth model: the TLS layer (``RequireAndVerifyClientCert``)
        guarantees every TCP caller holds a mesh-CA certificate; this pin
        narrows it to THE pinned mesh node, symmetric to the mesh's peer
        leg (``sha256:<hex>`` of the DER leaf). Reuses the per-peer
        :attr:`vesma.config.PeerConfig.mtls_cert_fingerprint` (ADR-0016
        semantics) and the constant-time compare from
        :func:`vesma.federation_server.verify_mtls_fingerprint`.

        Scope:

        * Unix-socket connections (no TLS): skipped — the UDS leg is
          guarded by filesystem permissions (criterion 11), not certs.
        * ``mtls_cert_fingerprint is None``: skipped — the operator
          opted out of pinning (mesh-CA chain verification at the
          handshake still applies; the client cert is still mandatory).
        * TLS connection, pin set, fingerprint mismatch (or no peer
          cert visible): ``False`` — the caller has already set
          ``PERMISSION_DENIED``. Fail-closed.
        """
        pin = peer.mtls_cert_fingerprint
        if pin is None:
            return True
        auth = context.auth_context()
        if not auth or not auth.get("transport_security_type"):
            return True  # not a TLS connection (UDS leg) — pin is TCP-only
        pem_entries = auth.get("x509_pem_cert") or []
        if not pem_entries:
            logger.warning(
                "mesh_server: TLS connection without a client cert in auth_context "
                "for pinned peer_id — refusing (fail-closed)"
            )
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details("mTLS pinning configured but no client cert on the connection")
            return False
        try:
            der = ssl.PEM_cert_to_DER_cert(pem_entries[0].decode("ascii"))
        except (ValueError, UnicodeDecodeError) as exc:
            logger.warning("mesh_server: unparsable client cert on TLS leg (%s)", exc)
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details("client certificate could not be parsed")
            return False
        presented = hashlib.sha256(der).hexdigest()
        expected = pin.strip().lower()
        if expected.startswith(_FINGERPRINT_PREFIX):
            expected = expected[len(_FINGERPRINT_PREFIX) :]
        if not verify_mtls_fingerprint(presented, expected):
            logger.warning(
                "mesh_server: client-cert fingerprint PIN MISMATCH — refusing (fail-closed)"
            )
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details(
                "client certificate fingerprint does not match the pinned fingerprint"
            )
            return False
        return True

    # ── Agent data gate (W3 part 3, ADR-0018-T §3/§8) ────────────────────────

    @staticmethod
    def _claimed_agent_id_from_context(context: grpc.ServicerContext[Any, Any]) -> str | None:
        """Extract the caller-asserted agent id from gRPC metadata.

        The mesh's AgentGateway stamps ``x-mnemos-agent-id`` on every
        translated core-leg data RPC. Returns ``None`` when the key is
        absent — the ADDITIVE contract: a request without it is the plain
        federation path (mTLS/UDS trust, as before), never the agent
        gate. The value is an UNVERIFIED claim until
        :meth:`_agent_data_gate` matches it against the token verdict.
        """
        for key, value in context.invocation_metadata():
            if key.lower() == _AGENT_ID_METADATA_KEY:
                return str(value)
        return None

    def _agent_data_gate(
        self,
        context: grpc.ServicerContext[Any, Any],
        *,
        node_id: str,
        claimed_agent_id: str,
        rpc: str,
    ) -> AgentIdentity | None:
        """Re-validate the agent bearer token on the DATA path (per request).

        ADR-0018-T §3 (criterion 5): the gateway validated the token
        before translating the RPC, but Vesma RE-VALIDATES on every data
        request — the mesh is transport, not an ACL authority, and a
        compromised node must not be able to skip the gate by calling the
        data RPCs directly. Checks (fail-closed at each step):

        1. ``authorization`` metadata must carry a Bearer token → else
           ``UNAUTHENTICATED`` (no bearer on an agent request).
        2. :func:`vesma.agent_tokens.validate_agent_token` against the
           CALLING node id (``node_id`` = the ``x-mnemos-peer-id``
           transport identity — the same value the gateway binds as
           ``aud`` at issuance and passes to :rpc:`ValidateAgentToken`;
           single-hop topology, TM T1): signature, version, lifetime,
           node binding, jti denylist. Any rejection →
           ``UNAUTHENTICATED`` with the coarse reason category.
        3. The verdict's ``agent_id`` must EQUAL the claimed
           ``x-mnemos-agent-id`` (the gateway relays the verdict id
           verbatim; a mismatch is a spoof or a broken relay) →
           ``PERMISSION_DENIED``.
        4. A store/key initialisation failure, or a mid-validation
           runtime crash of the validator itself (e.g. a locked SQLite
           hit during the jti denylist lookup) → ``INTERNAL`` with a
           FIXED details string (the cause goes to the log only — an
           uncaught exception would surface as ``UNKNOWN`` with the
           raw error repr on the wire): never a fabricated pass, never
           a serving-thread crash.

        The class grant (``rw`` ⊃ ``read``) is enforced by the CALLER
        (WriteMemory requires ``rw``); the project grants narrow the
        peer's allowed set via :func:`_narrow_by_agent_scope` at the
        existing ACL checkpoint of each RPC.

        T7 logging: ``agent_id`` / ``jti`` / coarse reason only — never
        token values, never record content.

        Returns:
            The :class:`AgentIdentity` on success, or ``None`` after
            setting the gRPC status (the caller returns its empty
            refusal response).
        """
        token = self._bearer_from_context(context)
        if token is None:
            logger.info(
                "mesh_server: agent data gate rejected rpc=%s reason=no_bearer claimed_agent_id=%s",
                rpc,
                claimed_agent_id,
            )
            context.set_code(grpc.StatusCode.UNAUTHENTICATED)
            context.set_details(
                "agent data gate: authorization metadata (Bearer <token>) is required"
            )
            return None
        try:
            store, key = self._token_validation_deps()
        except Exception as exc:  # surfaced as INTERNAL, logged with cause
            logger.error("mesh_server: agent data gate deps unavailable rpc=%s (%s)", rpc, exc)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(f"agent token validation backend unavailable: {exc}")
            return None
        # P3 review nit: a mid-validation crash (e.g. sqlite OperationalError
        # on the jti lookup) must map to INTERNAL with a FIXED details string —
        # uncaught it would surface as UNKNOWN with the raw error repr on the
        # wire (leaking store paths to the mesh caller).
        try:
            verdict = validate_agent_token(token, node_id, store=store, key=key)
        except Exception as exc:  # surfaced as INTERNAL, logged with cause
            logger.error("mesh_server: agent data gate validation failed rpc=%s (%s)", rpc, exc)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details("agent token validation failed")
            return None
        if not verdict.valid:
            logger.info(
                "mesh_server: agent data gate rejected rpc=%s agent_id=%s jti=%s reason=%s",
                rpc,
                verdict.agent_id,
                verdict.jti,
                verdict.reason,
            )
            context.set_code(grpc.StatusCode.UNAUTHENTICATED)
            context.set_details(f"agent token rejected: {verdict.reason}")
            return None
        if verdict.agent_id != claimed_agent_id:
            logger.info(
                "mesh_server: agent data gate rejected rpc=%s reason=agent_id_mismatch "
                "agent_id=%s jti=%s",
                rpc,
                verdict.agent_id,
                verdict.jti,
            )
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details(
                "agent identity mismatch: x-mnemos-agent-id does not match the token subject"
            )
            return None
        logger.info(
            "mesh_server: agent data gate ok rpc=%s agent_id=%s jti=%s scope=%s",
            rpc,
            verdict.agent_id,
            verdict.jti,
            ",".join(verdict.scope),
        )
        return AgentIdentity(
            agent_id=verdict.agent_id or "",
            jti=verdict.jti or "",
            scope=tuple(verdict.scope),
        )

    # ── RPC: ListMemories ──────────────────────────────────────────────────

    def ListMemories(  # noqa: N802 -- gRPC servicer override; name dictated by generated core_pb2_grpc.MnemosCoreServicer
        self,
        request: Any,
        context: grpc.ServicerContext[Any, Any],
    ) -> Any:
        """Export moderation-processed :class:`CompactRecord` bodies.

        The mesh calls this on startup/refresh to materialise the local
        view of what Vesma is willing to federate. Steps (contract §3.1,
        ADR-0020 cursor contract):

        1. Resolve the caller's peer. Enforce the ACL on every request,
           scoped AND unscoped: each requested ``project`` must be in the
           peer's effective allowed set, and an unscoped request is
           intersected with that set. An empty effective set or an empty
           intersection → ``PERMISSION_DENIED`` — never an unfiltered
           query (fail-closed contract, vesparo#371/#369).
        2. W3 agent data gate (additive): when ``x-mnemos-agent-id`` is
           present, re-validate the bearer token per request
           (:meth:`_agent_data_gate`) and NARROW the effective set with
           the token's ``project:`` grants (TM §3). Without the key:
           unchanged federation behaviour.
        3. Validate ``resume_cursor`` when non-empty: garbage →
           ``INVALID_ARGUMENT`` (ADR-0020 rule 3 — never a silent empty
           page). A valid checkpoint takes PRIORITY over ``since`` and
           resumes strictly AFTER the checkpointed storage position.
        4. Query :class:`SQLiteStore.list_all_for_mesh` (rowid ASC walk)
           with the filter fields — one query over the ACL-intersected
           project set, so a page boundary is a project-global checkpoint.
        5. Exclude ``mnemos:no-federate`` records (defence-in-depth
           layer 3 — moderation would refuse them anyway).
        6. Build :class:`CompactRecord` via
           :func:`vesma.compact.build_compact_record` (runs moderation)
           and stamp ``revision`` with the source rowid (ADR-0021 Q10.6).
        7. Return a page with ``total`` + ``has_more`` + an opaque
           ``cursor`` checkpoint minted from the last delivered rowid
           (non-empty iff the page is non-empty; empty page → empty
           cursor, byte-identical to a pre-ADR-0020 core).
        """
        projects = list(request.projects) if request.projects else []
        project_scope = projects[0] if projects else ""
        # Resolve the peer: the mesh identifies itself via gRPC metadata
        # in production; for the unit-test path we fall back to the single
        # configured peer when exactly one exists. The per-project ACL
        # check still runs, so this fallback does not weaken security.
        peer_id = self._peer_id_from_context(context) or self._single_peer_id()
        if peer_id is None:
            logger.info("mesh_server: ListMemories refused — no peer identity")
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details("no peer identity and not exactly one peer configured")
            return _mesh_gen.core_pb2.ListMemoriesResponse(
                records=[], total=0, has_more=False, cursor=""
            )
        # W2.5 TCP leg: on TLS connections, pin the caller's client-cert
        # fingerprint to the configured peer (UDS calls skip this).
        pin_peer = _resolve_peer(self._settings, peer_id)
        if pin_peer is not None and not self._enforce_tls_client_pin(pin_peer, context):
            return _mesh_gen.core_pb2.ListMemoriesResponse(
                records=[], total=0, has_more=False, cursor=""
            )
        # W3 part 3 — agent data gate (additive, ADR-0018-T §3): the
        # presence of x-mnemos-agent-id marks a gateway-relayed agent
        # request → the bearer token is re-validated per request and the
        # agent identity comes ONLY from the verdict. Without the key:
        # the unchanged federation path.
        agent: AgentIdentity | None = None
        claimed_agent_id = self._claimed_agent_id_from_context(context)
        if claimed_agent_id is not None:
            agent = self._agent_data_gate(
                context, node_id=peer_id, claimed_agent_id=claimed_agent_id, rpc="ListMemories"
            )
            if agent is None:
                return _mesh_gen.core_pb2.ListMemoriesResponse(
                    records=[], total=0, has_more=False, cursor=""
                )
        if project_scope and self._check_acl(peer_id, project_scope, context) is None:
            return _mesh_gen.core_pb2.ListMemoriesResponse(
                records=[], total=0, has_more=False, cursor=""
            )

        # ACL GATE — every request, scoped OR unscoped (vesma#371/#369
        # hardening): the effective allowed set is resolved
        # UNCONDITIONALLY. An empty set (unknown peer, empty allow-list,
        # or "*" with an empty shared_projects) DENIES — it never widens
        # into an unfiltered query. An unscoped request intersects to the
        # peer's full allowed set (principle: unscoped = intersection).
        allowed_projects = self._allowed_projects_for_peer(peer_id)
        if agent is not None:
            # TM §3: effective scope = token scope ∩ transport-peer ACL —
            # the token's project grants can only NARROW the peer grant.
            allowed_projects = _narrow_by_agent_scope(allowed_projects, agent.scope)
        if not allowed_projects:
            logger.info(
                "mesh_server: ListMemories refused — empty effective allowed set for peer_id=%s",
                peer_id,
            )
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details(
                f"ACL REFUSED: peer {peer_id!r} has an empty effective allowed-projects set"
            )
            return _mesh_gen.core_pb2.ListMemoriesResponse(
                records=[], total=0, has_more=False, cursor=""
            )
        effective_projects = self._intersect_projects(projects, allowed_projects)
        if not effective_projects:
            # Fail-closed safety net: the store treats an empty project
            # list as "no filter", so an empty intersection must DENY —
            # never hand the store an unfiltered query.
            logger.info(
                "mesh_server: ListMemories refused — no requested project allowed for peer_id=%s",
                peer_id,
            )
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details("ACL REFUSED: no requested project is in the allowed set")
            return _mesh_gen.core_pb2.ListMemoriesResponse(
                records=[], total=0, has_more=False, cursor=""
            )

        # ADR-0020 resume path: validate the opaque checkpoint BEFORE any
        # query. Garbage/foreign cursors are rejected loudly so the mesh
        # degrades to a fresh subscribe (empty resume_cursor) instead of
        # silently misinterpreting the token.
        checkpoint_rowid = 0
        if request.resume_cursor:
            try:
                checkpoint_rowid = _parse_resume_cursor(request.resume_cursor)
            except CursorError as exc:
                logger.info("mesh_server: ListMemories rejected resume_cursor (%s)", exc)
                context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
                context.set_details(f"invalid resume_cursor: {exc}")
                return _mesh_gen.core_pb2.ListMemoriesResponse(
                    records=[], total=0, has_more=False, cursor=""
                )

        tags_include = list(request.tags_include) if request.tags_include else None
        tags_exclude = list(request.tags_exclude) if request.tags_exclude else None
        include_no_federate = bool(request.include_no_federate)
        # Resume takes priority over `since` (ADR-0020): a resuming caller
        # must not re-apply a stale timestamp bound on top of the
        # checkpoint — the checkpoint already encodes the position.
        since = None if checkpoint_rowid else (request.since or None)
        page_limit = _clamp_page_limit(int(request.limit))
        # Fetch one extra row to detect has_more without a second query.
        fetch_limit = page_limit + 1

        records: list[Any] = []
        total_seen = 0
        has_more = False
        last_delivered_rowid = 0
        # One query over the ACL-intersected project set — a single
        # rowid-ASC walk keeps every page boundary a valid project-global
        # resume checkpoint. ``effective_projects`` is guaranteed non-empty
        # by the gate above; the store treats an empty/None list as "no
        # filter", so it is never handed one.
        rows = self._manager.sqlite.list_all_for_mesh(
            limit=fetch_limit,
            projects=list(effective_projects),
            tags=tags_include,
            since=since,
            after_rowid=checkpoint_rowid,
        )
        for memory, rowid in rows:
            total_seen += 1
            if len(records) >= page_limit:
                has_more = True
                break
            # Defence-in-depth: exclude no-federate records unless the
            # caller explicitly opted in (operator debug only).
            if not include_no_federate and NO_FEDERATE_TAG in memory.tags:
                continue
            # Apply tags_exclude filter.
            if tags_exclude and any(t in memory.tags for t in tags_exclude):
                continue
            # Apply type filter if the request specifies types.
            if request.types:
                rec_type = _memory_type_for_filter(memory.tags)
                if rec_type not in request.types:
                    continue
            # Build compact record (runs moderation → may refuse).
            rec = build_compact_record(
                memory,
                source_agent=memory.agent or "unknown",
                refuse_threshold=self._settings.federation.moderation_refuse_threshold,
            )
            if rec is None:
                continue
            records.append(_compact_to_proto(rec, revision=rowid))
            last_delivered_rowid = rowid
        cursor = _mint_cursor(last_delivered_rowid)
        if checkpoint_rowid:
            logger.info(
                "mesh_server: ListMemories resume checkpoint=%d → %d records, cursor=%s",
                checkpoint_rowid,
                len(records),
                bool(cursor),
            )
        return _mesh_gen.core_pb2.ListMemoriesResponse(
            records=records,
            total=total_seen,
            has_more=has_more,
            cursor=cursor,
        )

    # ── RPC: ReadMemory ───────────────────────────────────────────────────

    def ReadMemory(  # noqa: N802 -- gRPC servicer override; name dictated by generated core_pb2_grpc.MnemosCoreServicer
        self,
        request: Any,
        context: grpc.ServicerContext[Any, Any],
    ) -> Any:
        """Fetch ONE moderation-processed record by id (W3-v1, ADR-0018 am.3).

        Serves the AgentGateway read path: the mesh forwards an
        AgentReadMemory RPC here (token relay in gRPC metadata; the
        agent-leg token gate itself is the W3 part-2 gateway wiring).
        Core-leg semantics mirror :meth:`ListMemories` on a single record:

        1. Resolve the caller's peer (same identity rules as ListMemories)
           and enforce the TLS pin on the TCP leg.
        2. W3 agent data gate (additive): when ``x-mnemos-agent-id`` is
           present, re-validate the bearer token per request
           (:meth:`_agent_data_gate`); on success the token's
           ``project:`` grants NARROW the effective set (TM §3).
           Without the key: unchanged federation behaviour.
        3. Parse the id: a ``fed:<agent>:<uuid>`` CompactRecord id carries
           the source ``memory.id`` as its tail; anything else is treated
           as a raw memory id (unit-test / operator path).
        4. Unknown id → ``NOT_FOUND``. ``mnemos:no-federate`` →
           ``NOT_FOUND`` too — the record's very existence is not
           disclosed (defence-in-depth layer 3).
        5. ACL GATE (fail-closed, every request): untagged records are
           never served on this path; the record's project must be in the
           peer's effective allowed set; an explicit ``request.project``
           must MATCH the record's project (narrowing only).
        6. Build the CompactRecord via moderation; a moderation refuse →
           ``REFUSED`` trigger code (the call itself succeeds — the code
           is the outcome, mirroring the write path).
        7. ``revision`` is stamped 0 (= "no revision information", the
           documented proto default): the by-id fetch does not walk the
           rowid list path; stamping a storage revision here would imply
           LWW semantics this leg does not participate in.
        """
        record_id = str(request.record_id or "")
        if not record_id:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("record_id must be non-empty")
            return _mesh_gen.core_pb2.ReadMemoryResponse(trigger_code=_mesh_gen.fed_pb2.REFUSED)

        peer_id = self._peer_id_from_context(context) or self._single_peer_id()
        if peer_id is None:
            logger.info("mesh_server: ReadMemory refused — no peer identity")
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details("no peer identity and not exactly one peer configured")
            return _mesh_gen.core_pb2.ReadMemoryResponse(trigger_code=_mesh_gen.fed_pb2.REFUSED)
        pin_peer = _resolve_peer(self._settings, peer_id)
        if pin_peer is not None and not self._enforce_tls_client_pin(pin_peer, context):
            return _mesh_gen.core_pb2.ReadMemoryResponse(trigger_code=_mesh_gen.fed_pb2.REFUSED)
        # W3 part 3 — agent data gate (additive; see ListMemories).
        agent: AgentIdentity | None = None
        claimed_agent_id = self._claimed_agent_id_from_context(context)
        if claimed_agent_id is not None:
            agent = self._agent_data_gate(
                context, node_id=peer_id, claimed_agent_id=claimed_agent_id, rpc="ReadMemory"
            )
            if agent is None:
                return _mesh_gen.core_pb2.ReadMemoryResponse(trigger_code=_mesh_gen.fed_pb2.REFUSED)

        memory_id = record_id.rsplit(":", 1)[-1] if record_id.startswith("fed:") else record_id
        memory = self._manager.get(memory_id)
        if memory is None:
            logger.info("mesh_server: ReadMemory miss — id=%s (peer_id=%s)", record_id, peer_id)
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"record {record_id!r} not found")
            return _mesh_gen.core_pb2.ReadMemoryResponse(trigger_code=_mesh_gen.fed_pb2.REFUSED)
        if NO_FEDERATE_TAG in memory.tags:
            # Same posture as the list path's exclusion — plus id-oracle
            # avoidance: a no-federate id is indistinguishable from absent.
            logger.info(
                "mesh_server: ReadMemory refused no-federate id=%s (peer_id=%s)",
                record_id,
                peer_id,
            )
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"record {record_id!r} not found")
            return _mesh_gen.core_pb2.ReadMemoryResponse(trigger_code=_mesh_gen.fed_pb2.REFUSED)

        project = _tag_value(memory.tags, "project:")
        allowed_projects = self._allowed_projects_for_peer(peer_id)
        if agent is not None:
            # TM §3: effective scope = token scope ∩ transport-peer ACL —
            # the token's project grants can only NARROW the peer grant.
            allowed_projects = _narrow_by_agent_scope(allowed_projects, agent.scope)
        if not allowed_projects or not project or project not in allowed_projects:
            logger.info(
                "mesh_server: ReadMemory refused — project=%s not in allowed set for peer_id=%s",
                project,
                peer_id,
            )
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details(f"ACL REFUSED: project {project!r} not allowed")
            return _mesh_gen.core_pb2.ReadMemoryResponse(trigger_code=_mesh_gen.fed_pb2.REFUSED)
        if request.project and request.project != project:
            logger.info(
                "mesh_server: ReadMemory refused — request.project=%s ≠ record project=%s",
                request.project,
                project,
            )
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details(
                f"ACL REFUSED: record does not belong to project {request.project!r}"
            )
            return _mesh_gen.core_pb2.ReadMemoryResponse(trigger_code=_mesh_gen.fed_pb2.REFUSED)

        rec = build_compact_record(
            memory,
            source_agent=memory.agent or "unknown",
            refuse_threshold=self._settings.federation.moderation_refuse_threshold,
        )
        if rec is None:
            logger.info(
                "mesh_server: ReadMemory moderation refused id=%s (peer_id=%s)",
                record_id,
                peer_id,
            )
            return _mesh_gen.core_pb2.ReadMemoryResponse(trigger_code=_mesh_gen.fed_pb2.REFUSED)
        return _mesh_gen.core_pb2.ReadMemoryResponse(
            record=_compact_to_proto(rec),
            trigger_code=_mesh_gen.fed_pb2.EXHAUSTIVE,
        )

    # ── RPC: ValidateAgentToken ──────────────────────────────────────────

    def ValidateAgentToken(  # noqa: N802 -- gRPC servicer override; name dictated by generated core_pb2_grpc.MnemosCoreServicer
        self,
        request: Any,
        context: grpc.ServicerContext[Any, Any],
    ) -> Any:
        """Validate an AgentGateway bearer token (W3 part 2, ADR-0018-T §3/§8).

        Thin wire wrapper around :func:`vesma.agent_tokens.validate_agent_token`
        — this RPC is the launch-condition-2 validation hook the mesh's
        AgentGateway calls on EVERY agent request before translating it to
        a data RPC. The token rides in the ``authorization`` metadata key
        (``Bearer <token>``), NEVER in the request message (ADR-0018-T §2:
        a message field could reach logs). The core leg itself is already
        authenticated (UDS filesystem isolation or mesh-CA mTLS), so only
        the mesh can reach this RPC.

        The verdict (not a bare bool) is returned so the gateway can
        rate-limit per agent and log a precise reject reason to its
        ``gateway_query`` category — Vesma keeps sole authority over
        signature verification, revocation, and (per data RPC) the
        effective-scope gate (criterion 5).

        Fail-closed mapping:

        * missing/empty ``authorization`` metadata, a non-Bearer scheme,
          or an empty ``gateway_node_id`` → ``INVALID_ARGUMENT`` (a
          malformed RPC from the mesh, not a token verdict);
        * a store/key initialisation failure (unreadable signing key,
          locked DB), or a mid-validation runtime crash of the
          validator (e.g. a locked SQLite hit during the jti denylist
          lookup) → ``INTERNAL`` with a FIXED details string (the
          cause goes to the log only — an uncaught exception would
          surface as ``UNKNOWN`` with the raw error repr on the wire)
          — never crash the serving thread, never return a fabricated
          ``valid=True``;
        * anything else → the honest verdict, including ``valid=False``
          with the per-check ``reason``.
        """
        node_id = str(request.gateway_node_id or "")
        if not node_id:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("gateway_node_id must be non-empty")
            return _mesh_gen.core_pb2.ValidateAgentTokenResponse()
        token = self._bearer_from_context(context)
        if token is None:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("authorization metadata (Bearer <token>) is required")
            return _mesh_gen.core_pb2.ValidateAgentTokenResponse()

        try:
            store, key = self._token_validation_deps()
        except Exception as exc:  # surfaced as INTERNAL, logged with cause
            logger.error("mesh_server: ValidateAgentToken deps unavailable (%s)", exc)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(f"token validation backend unavailable: {exc}")
            return _mesh_gen.core_pb2.ValidateAgentTokenResponse()

        # P3 review nit: same mapping as the data gate — a mid-validation
        # crash (e.g. sqlite OperationalError on the jti lookup) becomes
        # INTERNAL with a FIXED details string, cause logged only; uncaught
        # it would surface as UNKNOWN with the raw error repr on the wire.
        try:
            verdict = validate_agent_token(token, node_id, store=store, key=key)
        except Exception as exc:  # surfaced as INTERNAL, logged with cause
            logger.error("mesh_server: ValidateAgentToken validation failed (%s)", exc)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details("token validation failed")
            return _mesh_gen.core_pb2.ValidateAgentTokenResponse()
        if not verdict.valid:
            logger.info(
                "mesh_server: token rejected agent_id=%s jti=%s reason=%s",
                verdict.agent_id,
                verdict.jti,
                verdict.reason,
            )
        return _mesh_gen.core_pb2.ValidateAgentTokenResponse(
            valid=bool(verdict.valid),
            agent_id=verdict.agent_id or "",
            scope=list(verdict.scope),
            jti=verdict.jti or "",
            aud=verdict.aud or "",
            exp_ok=bool(verdict.exp_ok),
            revoked=bool(verdict.revoked),
            reason=verdict.reason or "",
        )

    def _token_validation_deps(self) -> tuple[AgentTokenStore, Ed25519PrivateKey]:
        """Lazily build the agent-token store + signing key (first use).

        Mirrors the part-1 CLI wiring: the store is the shared SQLite DB
        (``agent_tokens`` table, ``CREATE TABLE IF NOT EXISTS`` — safe
        next to the main schema) and the key lives at
        ``<data_dir>/agent-token-signing.key`` (config override wins),
        minted on first use at mode 0600. Lazy on purpose: a Vesma host
        that never serves agent RPCs never mints a key and never opens
        the registry — "first use" semantics per ADR-0018-T §3.

        Double-checked under a lock so concurrent first RPCs build the
        pair exactly once. Failures propagate to the caller (the RPC
        maps them to INTERNAL) — no half-initialised state is retained.
        """
        if self._token_store is None or self._token_key is None:
            with self._token_deps_lock:
                if self._token_store is None:
                    self._token_store = AgentTokenStore(self._settings.db_path)
                if self._token_key is None:
                    self._token_key = load_or_create_signing_key(
                        signing_key_path(
                            self._settings.vesma.data_dir,
                            override=self._settings.federation.agent_token_key_path,
                        )
                    )
        return self._token_store, self._token_key

    # ── RPC: WriteMemory ───────────────────────────────────────────────────

    def import_compact_record(
        self,
        compact: CompactRecord,
        *,
        peer_id: str,
        agent_scope: Sequence[str] | None = None,
    ) -> CompactImportResult:
        """Import one :class:`CompactRecord` under a peer identity (shared path).

        The transport-free core of :rpc:`WriteMemory`, extracted so the
        S2 lazy-fetch command (``vesma fetch``) imports through the
        VERY SAME mechanism — ACL gate, #359/#362 duplicate gate,
        moderation, :meth:`MemoryManager.add` (Layer 1 secrets scanner)
        — without spinning up a gRPC server. The RPC handler resolves
        the transport concerns (import-mode validation, RESTORE gate,
        peer identity from gRPC metadata, TLS pinning, the W3 agent
        data gate) and delegates here; callers that classify outcomes
        map :attr:`status` onto their own counters (the Go pull maps
        the proto trigger codes 1:1 onto written/duplicate/gated — see
        ``writeOutcome`` there).

        Order of gates (unchanged by the extraction):

        1. ACL GATE — fail-closed (vesparo#371/#369 family), BEFORE the
           duplicate gate, moderation, and any storage mutation:
           (a) an empty effective allowed set (unknown peer, empty
           allow-list, ``"*"`` with an empty shared_projects) denies;
           (b) a record WITHOUT a ``project:`` tag cannot be ACL'd —
           deny; (c) the record's project must be IN the effective
           allowed set (for a ``"*"`` peer that is the shared_projects
           union — symmetric with the read-side intersection).
           With ``agent_scope`` (the gateway-relayed agent token's
           scope, W3 part 3) the allowed set is additionally narrowed
           by the token's ``project:`` grants (TM §3 effective scope =
           token ∩ transport-peer ACL — narrowing only).
        2. #359 duplicate gate by ``fed_id`` (fallback ``title`` +
           ``source_agent``): a hit refreshes
           ``metadata.last_fed_at`` (when present) and reports
           :attr:`CompactImportStatus.DUPLICATE` with the EXISTING
           storage id — no re-write, so replays stay idempotent.
        3. Vesma's own moderation on the record's ``summary`` (#86
           defence-in-depth).
        4. Persist via :meth:`MemoryManager.add` → WRITTEN.

        Args:
            compact: The record to import (already validated by the
                caller — the RPC leg marshals it from proto, the
                lazy-fetch leg from the mesh CLI's JSON contract).
            peer_id: The ACL identity of the importer. On the RPC leg
                this is the mesh node calling in; on the lazy-fetch leg
                it is the record's ORIGIN peer (we trust an origin for
                exactly what its ``allowed_projects`` grants).
            agent_scope: The re-validated agent token's scope grants
                when the import arrived via the W3 gateway path
                (:class:`AgentIdentity.scope`), else ``None`` (federation
                / lazy-fetch — the gate is a no-op, unchanged behaviour).

        Returns:
            A :class:`CompactImportResult`; never raises for policy
            refusals (the caller decides how to surface them).
        """
        project = _tag_value(compact.tags, "project:")
        agent = _tag_value(compact.tags, "agent:") or compact.source_agent
        allowed_projects = self._allowed_projects_for_peer(peer_id)
        if agent_scope is not None:
            allowed_projects = _narrow_by_agent_scope(allowed_projects, agent_scope)
        if not allowed_projects:
            logger.info(
                "mesh_server: WriteMemory refused — empty effective allowed set for peer_id=%s",
                peer_id,
            )
            return CompactImportResult(
                status=CompactImportStatus.REFUSED_ACL,
                reason=f"ACL REFUSED: peer {peer_id!r} has an empty effective allowed-projects set",
            )
        if not project:
            logger.info(
                "mesh_server: WriteMemory refused — record fed_id=%s carries no project tag",
                compact.id,
            )
            return CompactImportResult(
                status=CompactImportStatus.REFUSED_ACL,
                reason="ACL REFUSED: record carries no project: tag — cannot be ACL'd",
            )
        if project not in allowed_projects:
            logger.info(
                "mesh_server: WriteMemory refused — project=%s not allowed for peer_id=%s",
                project,
                peer_id,
            )
            return CompactImportResult(
                status=CompactImportStatus.REFUSED_ACL,
                reason=f"ACL REFUSED: project {project!r} not allowed for peer {peer_id!r}",
            )
        # #359 idempotent import — duplicate gate BEFORE moderation and
        # create. The mesh replays one-shot pulls (vesma-mesh #34);
        # without this gate every replay would mint a fresh row. Placed
        # after the ACL (fail-closed security first) and before the
        # moderation call: a duplicate performs no write, so the
        # moderation-on-write gate has nothing to gate and replays stay
        # cheap (no redaction-mapping churn).
        duplicate = self._manager.sqlite.find_federated_duplicate(
            fed_id=compact.id,
            title=compact.title,
            source_agent=compact.source_agent,
        )
        if duplicate is not None:
            # v1 decision (#359): found = duplicate, no content re-write.
            # Refresh metadata.last_fed_at when present (seeded at first
            # import) so operators can see federation freshness; return
            # the EXISTING storage id so the mesh correlates the replay
            # with the row that is already there.
            self._manager.sqlite.touch_last_fed_at(duplicate.id)
            logger.info(
                "mesh_server: WriteMemory duplicate — existing_id=%s fed_id=%s → ALREADY_EXHAUSTED",
                duplicate.id,
                compact.id,
            )
            return CompactImportResult(
                status=CompactImportStatus.DUPLICATE,
                written_id=duplicate.id,
            )
        # Vesma's own moderation on the compact summary (#86 import
        # validation). The peer already moderated, but Vesma re-checks
        # on import — defence-in-depth.
        mod_result = moderate(
            compact.summary,
            tags=compact.tags,
            refuse_threshold=self._settings.federation.moderation_refuse_threshold,
            mapping_ttl_hours=self._settings.federation.moderation_mapping_ttl_hours,
        )
        if mod_result.verdict == ModerationVerdict.REFUSE:
            logger.info("mesh_server: WriteMemory refused — moderation REFUSE")
            return CompactImportResult(
                status=CompactImportStatus.REFUSED_MODERATION,
                reason="moderation REFUSE",
            )
        content = mod_result.sanitized_content or compact.summary
        # Persist. MemoryManager.add runs the Layer 1 secrets scanner.
        # The compact id is NOT reused as the storage id — Vesma
        # generates its own id (the compact id is a federation envelope
        # id). The compact id is stored in metadata for traceability.
        data = MemoryCreate(
            content=content,
            title=compact.title or None,
            tags=list(compact.tags),
            source=MemorySource.MCP,
            metadata={
                "fed_id": compact.id,
                "fed_source_agent": compact.source_agent,
                # #359: seeded at first import so dedup replays can
                # refresh it (touch_last_fed_at only updates a key that
                # exists — it never invents one).
                "last_fed_at": datetime.now(UTC).isoformat(),
            },
        )
        memory = self._manager.add(
            data, project=project, agent=agent, mint_relates_to=False
        )  # #322 review M2: mesh ingest is machine traffic, not minting fuel
        logger.info(
            "mesh_server: WriteMemory wrote id=%s fed_id=%s project=%s",
            memory.id,
            compact.id,
            project,
        )
        return CompactImportResult(
            status=CompactImportStatus.WRITTEN,
            written_id=memory.id,
        )

    def WriteMemory(  # noqa: N802 -- gRPC servicer override; name dictated by generated core_pb2_grpc.MnemosCoreServicer
        self,
        request: Any,
        context: grpc.ServicerContext[Any, Any],
    ) -> Any:
        """Import a :class:`CompactRecord` from a peer into vesma.

        Steps (contract §3.1, #86 import validation, #359 idempotency):

        1. Validate the request: ``import_mode`` must be MERGE or
           RESTORE; RESTORE requires ``confirm=True`` (hard gate).
        2. Resolve the peer from gRPC metadata (single-peer fallback for
           tests) and enforce the W2.5 TLS client-cert pin.
        3. W3 agent data gate (additive): when ``x-mnemos-agent-id`` is
           present, re-validate the bearer token per request
           (:meth:`_agent_data_gate`) and require the ``rw`` class
           grant — a ``read``-scoped token is refused with
           ``PERMISSION_DENIED`` regardless of the peer ACL. Without
           the key: unchanged federation behaviour.
        4. Delegate to :meth:`import_compact_record` — the shared
           in-process import path (ACL gate → #359 duplicate gate →
           moderation → :meth:`MemoryManager.add`, with the token's
           ``project:`` grants narrowing the allowed set via
           ``agent_scope``); the result maps
           onto the response trigger code (``EXHAUSTIVE`` on clean
           merge, ``ALREADY_EXHAUSTED`` on a duplicate, ``REFUSED`` on
           an ACL or moderation refusal, with ``PERMISSION_DENIED`` set
           for ACL refusals).
        """
        import_mode = int(request.import_mode)
        # Validate import_mode (UNSPECIFIED is rejected).
        if import_mode == int(_mesh_gen.core_pb2.ImportMode.IMPORT_MODE_UNSPECIFIED):
            logger.info("mesh_server: WriteMemory refused — UNSPECIFIED import_mode")
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("import_mode must be MERGE or RESTORE")
            return _mesh_gen.core_pb2.WriteMemoryResponse(
                written_id="",
                mode_applied=_mesh_gen.core_pb2.ImportMode.IMPORT_MODE_UNSPECIFIED,
                trigger_code=_trigger_code_to_proto(TriggerCode.REFUSED),
            )
        # RESTORE hard gate (mnemos-operations §1).
        if import_mode == int(_mesh_gen.core_pb2.ImportMode.RESTORE) and not bool(request.confirm):
            logger.warning("mesh_server: WriteMemory refused — RESTORE without confirm=True")
            context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
            context.set_details("RESTORE requires confirm=True (hard gate)")
            return _mesh_gen.core_pb2.WriteMemoryResponse(
                written_id="",
                mode_applied=_mesh_gen.core_pb2.ImportMode.MERGE,
                trigger_code=_trigger_code_to_proto(TriggerCode.REFUSED),
            )
        # Downgrade RESTORE→MERGE on the mesh↔Vesma path: the mesh is
        # transport, not an operator disaster-recovery tool. The applied
        # mode is recorded in the response so the mesh surfaces it to
        # the operator.
        mode_applied = _mesh_gen.core_pb2.ImportMode.MERGE
        if import_mode == int(_mesh_gen.core_pb2.ImportMode.RESTORE):
            logger.info("mesh_server: downgrading RESTORE→MERGE on mesh↔Vesma path")

        pb_record = request.record
        compact = _compact_from_proto(pb_record)
        # ACL: the caller is the mesh peer, identified via gRPC metadata
        # in production. For the single-peer unit-test path we fall back
        # to the only configured peer (same as ListMemories). The record's
        # ``source_agent`` is the *origin* agent on the remote Vesma node — it
        # is NOT a federation peer and must not be used as the ACL identity.
        peer_id = self._peer_id_from_context(context) or self._single_peer_id()
        if peer_id is None:
            logger.info("mesh_server: WriteMemory refused — no peer identity")
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details("no peer identity and not exactly one peer configured")
            return _mesh_gen.core_pb2.WriteMemoryResponse(
                written_id="",
                mode_applied=mode_applied,
                trigger_code=_trigger_code_to_proto(TriggerCode.REFUSED),
            )
        # W2.5 TCP leg: on TLS connections, pin the caller's client-cert
        # fingerprint to the configured peer (UDS calls skip this).
        pin_peer = _resolve_peer(self._settings, peer_id)
        if pin_peer is not None and not self._enforce_tls_client_pin(pin_peer, context):
            return _mesh_gen.core_pb2.WriteMemoryResponse(
                written_id="",
                mode_applied=mode_applied,
                trigger_code=_trigger_code_to_proto(TriggerCode.REFUSED),
            )
        # W3 part 3 — agent data gate (additive; see ListMemories), plus
        # the WRITE class grant: ``rw`` ⊃ ``read`` — a read-scoped agent
        # token must not import records, no matter what the peer ACL says.
        agent: AgentIdentity | None = None
        claimed_agent_id = self._claimed_agent_id_from_context(context)
        if claimed_agent_id is not None:
            agent = self._agent_data_gate(
                context, node_id=peer_id, claimed_agent_id=claimed_agent_id, rpc="WriteMemory"
            )
            if agent is None:
                return _mesh_gen.core_pb2.WriteMemoryResponse(
                    written_id="",
                    mode_applied=mode_applied,
                    trigger_code=_trigger_code_to_proto(TriggerCode.REFUSED),
                )
            if not scope_allows_write(agent.scope):
                logger.info(
                    "mesh_server: WriteMemory refused — agent scope lacks rw (agent_id=%s jti=%s)",
                    agent.agent_id,
                    agent.jti,
                )
                context.set_code(grpc.StatusCode.PERMISSION_DENIED)
                context.set_details("agent token scope is read-only — WriteMemory requires rw")
                return _mesh_gen.core_pb2.WriteMemoryResponse(
                    written_id="",
                    mode_applied=mode_applied,
                    trigger_code=_trigger_code_to_proto(TriggerCode.REFUSED),
                )
        result = self.import_compact_record(
            compact,
            peer_id=peer_id,
            agent_scope=agent.scope if agent is not None else None,
        )
        if result.status is CompactImportStatus.REFUSED_ACL:
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details(result.reason)
            return _mesh_gen.core_pb2.WriteMemoryResponse(
                written_id="",
                mode_applied=mode_applied,
                trigger_code=_trigger_code_to_proto(TriggerCode.REFUSED),
            )
        if result.status is CompactImportStatus.REFUSED_MODERATION:
            return _mesh_gen.core_pb2.WriteMemoryResponse(
                written_id="",
                mode_applied=mode_applied,
                trigger_code=_trigger_code_to_proto(TriggerCode.REFUSED),
            )
        if result.status is CompactImportStatus.DUPLICATE:
            return _mesh_gen.core_pb2.WriteMemoryResponse(
                written_id=result.written_id,
                mode_applied=mode_applied,
                trigger_code=_trigger_code_to_proto(TriggerCode.ALREADY_EXHAUSTED),
            )
        return _mesh_gen.core_pb2.WriteMemoryResponse(
            written_id=result.written_id,
            mode_applied=mode_applied,
            trigger_code=_trigger_code_to_proto(TriggerCode.EXHAUSTIVE),
        )

    # ── RPC: GetSubscriptionState ─────────────────────────────────────────

    def GetSubscriptionState(  # noqa: N802 -- gRPC servicer override; name dictated by generated core_pb2_grpc.MnemosCoreServicer
        self,
        request: Any,
        context: grpc.ServicerContext[Any, Any],
    ) -> Any:
        """Return the subscription cursor state for a peer+project.

        Per Q3 decision, MnemosCore does NOT persist subscription
        cursors — that is mesh-side (M5). This RPC returns an empty
        cursor and ``last_rev=0`` so the mesh starts a fresh
        :rpc:`Subscribe` stream on (re)connect. The ACL is still
        enforced: a disallowed scope returns ``PERMISSION_DENIED`` and
        an empty response.

        Identity resolution matches ListMemories/WriteMemory (review
        MINOR): gRPC metadata, or the single configured peer — NEVER the
        caller-asserted ``request.peer_id`` (kept on the wire for
        informational correlation only), which was an ACL oracle over
        arbitrary peer ids.
        """
        peer_id = self._peer_id_from_context(context) or self._single_peer_id()
        if peer_id is None:
            logger.info("mesh_server: GetSubscriptionState refused — no peer identity")
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details("no peer identity and not exactly one peer configured")
            return _mesh_gen.core_pb2.GetSubscriptionStateResponse(
                cursor="", last_rev=0, last_sync_timestamp=""
            )
        # W2.5 TCP leg: on TLS connections, pin the caller's client-cert
        # fingerprint to the configured peer (UDS calls skip this).
        pin_peer = _resolve_peer(self._settings, peer_id)
        if pin_peer is not None and not self._enforce_tls_client_pin(pin_peer, context):
            return _mesh_gen.core_pb2.GetSubscriptionStateResponse(
                cursor="", last_rev=0, last_sync_timestamp=""
            )
        if self._check_acl(peer_id, request.project_scope, context) is None:
            return _mesh_gen.core_pb2.GetSubscriptionStateResponse(
                cursor="", last_rev=0, last_sync_timestamp=""
            )
        # M4: no persisted cursor. The mesh starts fresh.
        return _mesh_gen.core_pb2.GetSubscriptionStateResponse(
            cursor="",
            last_rev=0,
            last_sync_timestamp="",
        )

    # ── S2 metadata sync (ADR-0021 Q10.2/Q10.3 — chairman ruling 2026-09-20) ──

    def build_metadata_sync_response(self, request: Any, *, peer_id: str | None = None) -> Any:
        """Build a ``MetadataSyncResponse`` from the ``federation_index``.

        The body of :rpc:`SyncMetadata` (S2 export leg, poll-first —
        ADR-0021 ruling 2). Kept as a separate method so the semantics
        are unit-testable without a gRPC server; the RPC wrapper
        (:meth:`SyncMetadata`) resolves the peer identity from the
        connection and maps ACL denials to ``PERMISSION_DENIED``.

        Semantics (``federation.proto::MetadataSyncRequest/Response``):

        * ``since_rev`` is the peer's watermark, mapped onto the index
          rowid space — the ADR-0020 rowid-ASC cursor MECHANIC (same as
          :meth:`ListMemories` / ``list_all_for_mesh``), expressed as a
          plain int64 because the SyncMetadata wire contract has no
          opaque-token field. Rows with ``rowid > since_rev`` are
          served; ``latest_rev`` echoes the rowid of the last row this
          page consumed (delivered OR filtered) so a re-poll resumes
          exactly after it — stateless pagination, no server-side cursor
          state (ADR-0020: core keeps no cursor state at all). An empty
          page parks ``latest_rev`` at the SCOPE HEAD — the max rowid
          under the effective-projects filter
          (:meth:`SQLiteStore.index_head`, vesma-mesh#46) — instead of
          echoing ``since_rev``: nothing undelivered exists beyond the
          head, and a MIN-aggregating poller (the mesh CLI folds the
          per-scope ``latest_rev`` into one watermark) otherwise sticks
          at the old checkpoint and re-delivers the data scope every
          tick. ``max(since_rev, head)`` keeps the never-regress
          invariant for watermarks minted over a wider scope.
        * ``limit`` (0 = core default 50, clamped to the hard ceiling)
          sizes the page; the wrapper fetches one extra row to detect
          ``has_more`` without a second query.
        * ``project_scope`` + ``peer_id`` run the same fail-closed ACL
          as :rpc:`ListMemories`: the effective allowed set is resolved
          unconditionally; a scoped request must be inside it, an
          unscoped request intersects with it. Denial via the BUILDER
          (standalone use) → empty records + ``trigger_code=REFUSED``;
          denial via the RPC wrapper → ``PERMISSION_DENIED``.
        * ``filter`` (tag set) intersects with each row's tags (proto
          semantics: "entries whose tags intersect this set"); empty =
          no filter.
        * Q10.9 export gates: rows whose title matches the configured
          ``index_title_blocklist`` are dropped from the page (their
          rowids still advance ``latest_rev``, so a blocked row never
          wedges a poll loop); ``mnemos:no-federate`` rows are excluded
          in SQL (they should not exist — the upsert gate refuses them;
          serve-side belt-and-braces).
        * The response is metadata-ONLY: no CompactRecord bodies, no
          summary — an index row is itself an inference surface, and
          content stays behind :rpc:`ListMemories` / :rpc:`WriteMemory`.

        Args:
            request: A ``fed_pb2.MetadataSyncRequest`` (real generated
                message — the same shape the wire contract defines).
            peer_id: The AUTHENTICATED peer identity (from the gRPC
                context in the RPC path). When ``None`` the builder
                falls back to ``request.peer_id`` / the single
                configured peer (standalone/test path only — a real
                caller must never trust the caller-asserted field).

        Returns:
            A ``fed_pb2.MetadataSyncResponse``.

        Raises:
            CursorError: ``since_rev`` is negative or beyond the SQLite
                rowid ceiling — the ADR-0020 rule-3 analog (garbage
                checkpoints are rejected loudly, never guessed); the RPC
                wrapper maps this to ``INVALID_ARGUMENT``.
        """
        since_rev = int(request.since_rev)
        if since_rev < 0 or since_rev > _SQLITE_ROWID_MAX:
            raise CursorError(f"since_rev out of range [0, {_SQLITE_ROWID_MAX}]: {since_rev}")

        def _refused() -> Any:
            return _mesh_gen.fed_pb2.MetadataSyncResponse(
                records=[],
                latest_rev=since_rev,
                has_more=False,
                trigger_code=_trigger_code_to_proto(TriggerCode.REFUSED),
            )

        resolved_peer = peer_id or str(request.peer_id) or self._single_peer_id()
        if resolved_peer is None:
            logger.info("mesh_server: SyncMetadata refused — no peer identity")
            return _refused()
        allowed_projects = self._allowed_projects_for_peer(resolved_peer)
        if not allowed_projects:
            logger.info(
                "mesh_server: SyncMetadata refused — empty effective allowed set for peer_id=%s",
                resolved_peer,
            )
            return _refused()
        project_scope = str(request.project_scope)
        if project_scope:
            if not self._intersect_projects([project_scope], allowed_projects):
                logger.info(
                    "mesh_server: SyncMetadata refused — project_scope=%s "
                    "not allowed for peer_id=%s",
                    project_scope,
                    resolved_peer,
                )
                return _refused()
            effective_projects: list[str] | None = [project_scope]
        else:
            effective_projects = list(allowed_projects)

        tag_filter = [str(t) for t in request.filter]
        title_blocklist = self._settings.federation.index_title_blocklist
        page_limit = _clamp_page_limit(int(request.limit))
        rows = self._manager.sqlite.list_index(
            limit=page_limit + 1,
            projects=effective_projects,
            after_rowid=since_rev,
        )
        has_more = len(rows) > page_limit
        records: list[Any] = []
        latest_rev = since_rev
        blocked = 0
        for entry, rowid in rows[:page_limit]:
            latest_rev = rowid
            if tag_filter and not any(t in entry.tags for t in tag_filter):
                continue
            if title_blocklist and title_matches_blocklist(entry.title, title_blocklist):
                blocked += 1
                continue
            records.append(_metadata_to_proto(entry))
        if not rows:
            # vesma-mesh#46/#49: an EMPTY page parks the watermark at
            # the PEER head — max rowid over the peer's whole allowed
            # set — not the scope-filtered head. The mesh CLI folds
            # per-scope latest_rev into one MIN watermark, so a scoped
            # head of 0 on an empty scope pins the aggregate to 0
            # forever (live poller finding #49). Nothing this peer may
            # see exists beyond the allowed-set head, so parking there
            # skips nothing for ANY scope; max() keeps the never-regress
            # invariant when the head sits below a watermark minted over
            # a wider scope.
            latest_rev = max(
                since_rev, self._manager.sqlite.index_head(projects=list(allowed_projects))
            )
        logger.info(
            "mesh_server: index sync served entries=%d peer=%s since_rev=%d "
            "latest_rev=%d has_more=%s title_blocked=%d",
            len(records),
            resolved_peer,
            since_rev,
            latest_rev,
            has_more,
            blocked,
        )
        return _mesh_gen.fed_pb2.MetadataSyncResponse(
            records=records,
            latest_rev=latest_rev,
            has_more=has_more,
            trigger_code=_trigger_code_to_proto(TriggerCode.EXHAUSTIVE),
        )

    # ── RPC: SyncMetadata ──────────────────────────────────────────────────

    def SyncMetadata(  # noqa: N802 -- gRPC servicer override; name dictated by generated core_pb2_grpc.MnemosCoreServicer
        self,
        request: Any,
        context: grpc.ServicerContext[Any, Any],
    ) -> Any:
        """Serve a metadata-only page of the ``federation_index`` (S2 export).

        Chairman ruling 2026-09-20 (ADR-0021 Q10.2 poll-first): the
        mesh↔Vesma export leg. The mesh relays the FederationPeer wire
        messages to its peer leg verbatim — metadata only, no content.

        Steps (mirrors :rpc:`ListMemories`):

        1. Resolve the peer from gRPC metadata (single-peer fallback for
           tests) — NEVER the caller-asserted ``request.peer_id``.
        2. W2.5 TCP leg: pin the client-cert fingerprint on TLS
           connections (UDS calls skip this).
        3. ACL GATE — every request, scoped OR unscoped (fail-closed):
           empty effective set or a disallowed ``project_scope`` →
           ``PERMISSION_DENIED``.
        4. Delegate to :meth:`build_metadata_sync_response`; a garbage
           ``since_rev`` surfaces as ``INVALID_ARGUMENT`` (ADR-0020
           rule 3).
        """
        peer_id = self._peer_id_from_context(context) or self._single_peer_id()
        if peer_id is None:
            logger.info("mesh_server: SyncMetadata refused — no peer identity")
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details("no peer identity and not exactly one peer configured")
            return _mesh_gen.fed_pb2.MetadataSyncResponse(
                records=[], latest_rev=int(request.since_rev), has_more=False
            )
        pin_peer = _resolve_peer(self._settings, peer_id)
        if pin_peer is not None and not self._enforce_tls_client_pin(pin_peer, context):
            return _mesh_gen.fed_pb2.MetadataSyncResponse(
                records=[], latest_rev=int(request.since_rev), has_more=False
            )
        allowed_projects = self._allowed_projects_for_peer(peer_id)
        if not allowed_projects:
            logger.info(
                "mesh_server: SyncMetadata refused — empty effective allowed set for peer_id=%s",
                peer_id,
            )
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details(
                f"ACL REFUSED: peer {peer_id!r} has an empty effective allowed-projects set"
            )
            return _mesh_gen.fed_pb2.MetadataSyncResponse(
                records=[], latest_rev=int(request.since_rev), has_more=False
            )
        project_scope = str(request.project_scope)
        if project_scope and project_scope not in allowed_projects:
            logger.info(
                "mesh_server: SyncMetadata refused — project_scope=%s not allowed for peer_id=%s",
                project_scope,
                peer_id,
            )
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details(f"ACL REFUSED: project_scope {project_scope!r} not allowed")
            return _mesh_gen.fed_pb2.MetadataSyncResponse(
                records=[], latest_rev=int(request.since_rev), has_more=False
            )
        try:
            return self.build_metadata_sync_response(request, peer_id=peer_id)
        except CursorError as exc:
            logger.info("mesh_server: SyncMetadata rejected since_rev (%s)", exc)
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(f"invalid since_rev: {exc}")
            return _mesh_gen.fed_pb2.MetadataSyncResponse(records=[], latest_rev=0, has_more=False)

    # ── RPC: UpsertIndexEntries ────────────────────────────────────────────

    def UpsertIndexEntries(  # noqa: N802 -- gRPC servicer override; name dictated by generated core_pb2_grpc.MnemosCoreServicer
        self,
        request: Any,
        context: grpc.ServicerContext[Any, Any],
    ) -> Any:
        """Import peer metadata entries into ``federation_index`` (S2 leg).

        Chairman ruling 2026-09-20: a SEPARATE RPC from WriteMemory —
        index-only semantics (WriteMemory = content, UpsertIndexEntries
        = metadata); the ACL model is the same fail-closed per-peer
        gate on the write path.

        Steps (mirrors :rpc:`WriteMemory`):

        1. Resolve the peer from gRPC metadata (single-peer fallback
           for tests); enforce the TLS pin on TLS connections.
        2. ACL GATE (fail-closed, whole-RPC): empty effective set →
           ``PERMISSION_DENIED``. Per entry: an entry WITHOUT a
           ``project`` cannot be ACL'd → deny; an entry whose project
           is outside the effective allowed set → deny. Authorization
           violations abort the batch (the peer is misbehaving — write
           path, same posture as WriteMemory).
        3. Boundary validation + Q10.9 gates (per entry, COUNTED — one
           bad entry never aborts the batch): schema validation
           (unknown ``content_state``, oversized title, foreign
           ``schema_version``, empty id, unparseable or future-dated
           ``timestamp`` — canonicalised to UTC on the way in, review
           blocker 2), ``mnemos:no-federate`` tag, title blocklist →
           ``rejected_by_gate``.
        4. Origin hygiene: ``origin_peer`` empty/``"self"`` on the wire
           is re-stamped to the AUTHENTICATED sender id.
        5. Upsert via ``upsert_index_entries`` (LWW-by-timestamp, Q10.6;
           the storage gates re-apply as defence-in-depth). The store
           enforces the origin-mutation ruling (review blocker 1,
           CWE-284): an EXISTING row is only mutable by its origin —
           the authenticated sender must equal the stored
           ``origin_peer`` (a transit re-send or a foreign ``self``
           claim against another origin's id is refused into
           ``rejected_by_gate``; NEW ids with an explicit foreign
           origin remain importable — that is transit). Entries that
           lose LWW to a newer stored row are neither accepted nor
           rejected (silently superseded; counted in the log line).
        """
        peer_id = self._peer_id_from_context(context) or self._single_peer_id()
        if peer_id is None:
            logger.info("mesh_server: UpsertIndexEntries refused — no peer identity")
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details("no peer identity and not exactly one peer configured")
            return _mesh_gen.core_pb2.UpsertIndexEntriesResponse(accepted=0, rejected_by_gate=0)
        pin_peer = _resolve_peer(self._settings, peer_id)
        if pin_peer is not None and not self._enforce_tls_client_pin(pin_peer, context):
            return _mesh_gen.core_pb2.UpsertIndexEntriesResponse(accepted=0, rejected_by_gate=0)
        allowed_projects = self._allowed_projects_for_peer(peer_id)
        if not allowed_projects:
            logger.info(
                "mesh_server: UpsertIndexEntries refused — empty effective set for peer_id=%s",
                peer_id,
            )
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details(
                f"ACL REFUSED: peer {peer_id!r} has an empty effective allowed-projects set"
            )
            return _mesh_gen.core_pb2.UpsertIndexEntriesResponse(accepted=0, rejected_by_gate=0)

        title_blocklist = self._settings.federation.index_title_blocklist
        clean: list[FederationIndexEntry] = []
        rejected = 0
        for pb_record in request.entries:
            try:
                entry = _metadata_entry_from_proto(pb_record, sender_peer_id=peer_id)
            except (ValidationError, ValueError) as exc:
                rejected += 1
                logger.info(
                    "mesh_server: UpsertIndexEntries rejected entry (%s)",
                    exc,
                )
                continue
            # Write-path ACL BEFORE any write (WriteMemory mirror):
            # authorization violations abort the whole RPC.
            if not entry.project:
                logger.info(
                    "mesh_server: UpsertIndexEntries refused — entry id=%s carries no project",
                    entry.id,
                )
                context.set_code(grpc.StatusCode.PERMISSION_DENIED)
                context.set_details("ACL REFUSED: entry carries no project — cannot be ACL'd")
                return _mesh_gen.core_pb2.UpsertIndexEntriesResponse(
                    accepted=0, rejected_by_gate=rejected
                )
            if entry.project not in allowed_projects:
                logger.info(
                    "mesh_server: UpsertIndexEntries refused — project=%s not allowed "
                    "for peer_id=%s",
                    entry.project,
                    peer_id,
                )
                context.set_code(grpc.StatusCode.PERMISSION_DENIED)
                context.set_details(
                    f"ACL REFUSED: project {entry.project!r} not allowed for peer {peer_id!r}"
                )
                return _mesh_gen.core_pb2.UpsertIndexEntriesResponse(
                    accepted=0, rejected_by_gate=rejected
                )
            # Q10.9 import gates — counted, never abort the batch.
            if NO_FEDERATE_TAG in entry.tags:
                rejected += 1
                logger.info(
                    "mesh_server: UpsertIndexEntries rejected entry id=%s — no-federate tag",
                    entry.id,
                )
                continue
            if title_blocklist and title_matches_blocklist(entry.title, title_blocklist):
                rejected += 1
                logger.info(
                    "mesh_server: UpsertIndexEntries rejected entry id=%s — title blocklist",
                    entry.id,
                )
                continue
            clean.append(entry)

        stats = self._manager.sqlite.upsert_index_entries(
            clean, sender_peer_id=peer_id, title_blocklist=title_blocklist
        )
        rejected_by_gate = rejected + stats.refused
        logger.info(
            "mesh_server: index upsert accepted=%d rejected_by_gate=%d stale=%d total=%d peer=%s",
            stats.written,
            rejected_by_gate,
            stats.stale,
            len(request.entries),
            peer_id,
        )
        return _mesh_gen.core_pb2.UpsertIndexEntriesResponse(
            accepted=stats.written,
            rejected_by_gate=rejected_by_gate,
            trigger_code=_trigger_code_to_proto(TriggerCode.EXHAUSTIVE),
        )

    # ── RPC: Heartbeat ─────────────────────────────────────────────────────

    def Heartbeat(  # noqa: N802 -- gRPC servicer override; name dictated by generated core_pb2_grpc.MnemosCoreServicer
        self,
        request: Any,
        context: grpc.ServicerContext[Any, Any],
    ) -> Any:
        """Return liveness + version + uptime.

        Cheap unary call. The ACL is NOT enforced on heartbeat — it is
        a liveness probe, not a data RPC, and refusing it would prevent
        the mesh from detecting a healthy vesma.
        """
        uptime = int(time.monotonic() - self._start_time)
        return _mesh_gen.core_pb2.HeartbeatResponse(
            healthy=True,
            version=f"vesma {_mnemos_version}",
            uptime_seconds=uptime,
        )

    # ── Helpers ───────────────────────────────────────────────────────────

    def _peer_id_from_context(self, context: grpc.ServicerContext[Any, Any]) -> str | None:
        """Extract the caller's peer id from gRPC metadata.

        The mesh sets the ``x-mnemos-peer-id`` metadata key on every
        call. Returns ``None`` when the key is absent (e.g. unit tests
        that do not set it).
        """
        metadata = context.invocation_metadata()
        for key, value in metadata:
            if key.lower() == "x-mnemos-peer-id":
                return str(value)
        return None

    @staticmethod
    def _bearer_from_context(context: grpc.ServicerContext[Any, Any]) -> str | None:
        """Extract the bearer token from ``authorization`` metadata.

        ADR-0018-T §2: the token rides in ``authorization:
        ``Bearer <token>`` — never in a message field. Accepts the
        scheme case-insensitively (``bearer``/``Bearer``) and returns
        ``None`` when the key is absent, the scheme is not Bearer, or
        the remainder is empty — the RPC maps that to INVALID_ARGUMENT.
        """
        for key, value in context.invocation_metadata():
            if key.lower() != "authorization":
                continue
            parts = str(value).strip().split(None, 1)
            if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
                return parts[1].strip()
            return None
        return None

    def _single_peer_id(self) -> str | None:
        """Return the only configured peer id, or ``None`` if 0 or 2+ peers.

        Convenience for single-peer deployments and unit tests: when
        exactly one peer is configured, we can infer the identity
        without metadata. With 0 or 2+ peers, the caller MUST set the
        metadata.
        """
        peers = list(self._settings.federation.peers.keys())
        return peers[0] if len(peers) == 1 else None

    def _allowed_projects_for_peer(self, peer_id: str) -> list[str]:
        """Return the peer's EFFECTIVE allowed project set (fail-closed).

        Resolution rules (ACL hardening contract):

        * unknown peer → ``[]`` — the caller MUST deny (no implicit
          trust of an unconfigured identity);
        * ``"*" in allowed_projects`` → the global ``shared_projects``
          union (the explicit wildcard grants everything SHARED, nothing
          more); an empty union yields ``[]`` — the caller MUST deny;
        * otherwise → ``allowed_projects`` verbatim; an empty allow-list
          yields ``[]`` — the caller MUST deny.

        An empty result never means "no filter": every data-path caller
        treats it as ``PERMISSION_DENIED``.
        """
        peer = _resolve_peer(self._settings, peer_id)
        if peer is None:
            return []
        if "*" in peer.allowed_projects:
            return list(self._settings.federation.shared_projects)
        return list(peer.allowed_projects)

    @staticmethod
    def _intersect_projects(
        requested: Sequence[str],
        allowed: Sequence[str],
    ) -> list[str]:
        """Intersect the requested projects with the allowed set.

        ``allowed == []`` means "none" (fail-closed) → returns ``[]``.
        ``"*" in allowed`` means "all" → returns the requested list as-is.
        Otherwise returns the intersection.
        """
        allowed_list = list(allowed)
        if not allowed_list:
            return []
        if "*" in allowed_list:
            return list(requested)
        requested_set = set(requested)
        return (
            [p for p in allowed_list if p in requested_set] if requested_set else list(allowed_list)
        )


# ── Server lifecycle ─────────────────────────────────────────────────────────


class MeshServer:
    """Lifecycle wrapper around the gRPC ``MnemosCore`` server.

    Binds a Unix socket (always) and — when ``settings.mesh.tcp.enabled``
    is set — an additional mTLS TCP port on the SAME grpcio server
    (W2.5, ADR-0019 option 1). Registers the :class:`VesmaCoreServicer`
    and exposes :meth:`start` / :meth:`stop` for clean lifecycle control.
    Designed to be owned by the Vesma process (or a test fixture) and
    stopped on shutdown.

    A failed TCP bind raises :class:`MeshTCPLegError` from :meth:`start`
    (fail-fast, ADR-0019 amendment 3c) — the server never comes up
    half-alive.

    Args:
        socket_path: Filesystem path for the Unix socket. The server
            creates (or recreates) the socket file. Any existing socket
            file at this path is removed before binding so a restart
            does not get ``EADDRINUSE``.
        manager: The :class:`MemoryManager` backing storage + moderation.
        settings: The full :class:`Settings` (for ACL + federation
            thresholds).
        max_workers: gRPC thread pool size. Default 4 — the mesh↔Vesma
            channel is low-traffic (local Unix socket, batch sync); a
            large pool is wasteful.

    Usage::

        server = MeshServer("/run/mnemos/core.sock", manager, settings)
        server.start()
        try:
            ...
        finally:
            server.stop()
    """

    def __init__(
        self,
        socket_path: str,
        manager: MemoryManager,
        settings: Settings,
        *,
        max_workers: int = 4,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        self._socket_path: str = socket_path
        self._manager: MemoryManager = manager
        self._settings: Settings = settings
        self._max_workers: int = max_workers
        self._server: grpc.Server | None = None
        self._servicer: VesmaCoreServicer | None = None
        self._tcp_bound_port: int | None = None

    @property
    def socket_path(self) -> str:
        """The configured Unix socket path."""
        return self._socket_path

    @property
    def tcp_bound_port(self) -> int | None:
        """The actually-bound TCP port when the TCP leg is on, else ``None``.

        Differs from the configured ``mesh.tcp.port`` only for the
        ephemeral ``port: 0`` form (tests/diagnostics): gRPC returns the
        OS-assigned port and it is exposed here so a client can dial it.
        """
        return self._tcp_bound_port

    @property
    def is_running(self) -> bool:
        """``True`` when the server has been started and not yet stopped."""
        return self._server is not None

    @property
    def servicer(self) -> VesmaCoreServicer | None:
        """The active servicer (``None`` before :meth:`start` / after :meth:`stop`)."""
        return self._servicer

    def start(self) -> None:
        """Bind the Unix socket and start serving.

        Removes any stale socket file at :attr:`socket_path` first
        (otherwise gRPC gets ``EADDRINUSE`` on restart). Creates the
        parent directory so the socket is only accessible to the vesma
        user by default (mode ``0700`` dir / ``0600`` socket), or — when
        ``settings.mesh.socket_group_access`` is set — group-accessible
        modes (``0770`` dir / ``0660`` socket) for shared-volume
        deployments where the mesh binary dials from a different uid in
        the same gid (Kubernetes fsGroup, compose ``user:``). In both
        cases defence-in-depth: the operator is still responsible for
        the enclosing directory ownership.

        When ``settings.mesh.tcp.enabled`` is set, additionally opens the
        mTLS TCP leg (``add_secure_port`` with mesh-CA
        ``RequireAndVerifyClientCert`` — see
        :func:`_tcp_server_credentials`). A failed bind raises
        :class:`MeshTCPLegError` AFTER rolling the half-built server
        back — startup fail-fast, no silent degradation (ADR-0019
        amendment 3c).
        """
        if self._server is not None:
            raise RuntimeError("MeshServer already started")
        group_access = self._settings.mesh.socket_group_access
        dir_mode = 0o770 if group_access else 0o700
        sock_mode = 0o660 if group_access else 0o600
        sock_path = Path(self._socket_path)
        # Remove a stale socket file so a restart does not EADDRINUSE.
        if sock_path.exists() and sock_path.is_socket():
            sock_path.unlink()
        # Ensure the parent dir exists with restrictive perms.
        parent = sock_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(parent, dir_mode)
        except PermissionError:
            # Best-effort: if we cannot chmod the parent (e.g. /run),
            # the operator is responsible for the perms. Do not fail.
            logger.warning(
                "mesh_server: could not chmod parent %s — operator must secure it",
                parent,
            )
        self._servicer = VesmaCoreServicer(
            self._manager,
            settings=self._settings,
        )
        self._server = grpc.server(futures.ThreadPoolExecutor(max_workers=self._max_workers))
        _mesh_gen.core_pb2_grpc.add_MnemosCoreServicer_to_server(self._servicer, self._server)
        # gRPC Unix-socket addressing: "unix:///path/to/sock".
        self._server.add_insecure_port(f"unix://{self._socket_path}")
        # W2.5 TCP leg (ADR-0019 option 1): optional mTLS port on the SAME
        # grpcio server. Default OFF — with mesh.tcp.enabled false this
        # block is skipped entirely and the process opens NO TCP port.
        tcp = self._settings.mesh.tcp
        if tcp.enabled:
            addr = f"{tcp.bind}:{tcp.port}"
            try:
                creds = _tcp_server_credentials(tcp.tls)
            except Exception as exc:
                # Unreadable/broken PEM material: same rollback as a failed
                # bind — a half-started server must not survive (review N1).
                self._abort_failed_start()
                raise MeshTCPLegError(f"mesh tcp leg: invalid TLS material: {exc}") from exc
            try:
                # Recent grpcio raises on a failed bind; the >=1.62 floor
                # only returns 0 — BOTH paths must fail fast (3c).
                bound = self._server.add_secure_port(addr, creds)
            except RuntimeError as exc:
                self._abort_failed_start()
                raise MeshTCPLegError(f"mesh tcp leg: failed to bind {addr}: {exc}") from exc
            if bound == 0:
                self._abort_failed_start()
                raise MeshTCPLegError(
                    f"mesh tcp leg: add_secure_port({addr}) returned 0 — refusing to "
                    "start with a missing TCP leg (fail-fast, ADR-0019 amendment 3c)"
                )
            self._tcp_bound_port = int(bound)
            logger.info("mesh tcp leg listening on %s:%d", tcp.bind, self._tcp_bound_port)
        self._server.start()
        # Restrict the socket file perms (defence-in-depth: the socket
        # should only be accessible to the vesma user + the mesh).
        try:
            os.chmod(self._socket_path, sock_mode)
        except (PermissionError, FileNotFoundError):
            logger.warning(
                "mesh_server: could not chmod socket %s — operator must secure it",
                self._socket_path,
            )
        logger.info("mesh server listening on %s", self._socket_path)

    def _abort_failed_start(self) -> None:
        """Roll back a half-built server after a failed TCP-leg bind.

        Crash-path cleanup only: the original :class:`MeshTCPLegError` is
        re-raised by the caller — nothing is swallowed, the process dies
        visibly (amendment 3c: CrashLoop = visible).
        """
        if self._server is not None:
            # best-effort teardown next to a fatal error
            with contextlib.suppress(RuntimeError):
                self._server.stop(grace=0)
            self._server = None
            self._servicer = None
        self._tcp_bound_port = None
        with contextlib.suppress(PermissionError, FileNotFoundError):
            Path(self._socket_path).unlink(missing_ok=True)

    def stop(self, *, grace: float = 1.0) -> None:
        """Stop the server and release the socket.

        Args:
            grace: Grace period in seconds for in-flight RPCs to finish.
                Default 1.0s — short enough for a clean shutdown, long
                enough for a local Unix-socket round trip.
        """
        if self._server is None:
            return
        self._server.stop(grace=grace)
        self._server = None
        self._servicer = None
        self._tcp_bound_port = None
        # Best-effort socket cleanup. The OS reaps it when the process
        # exits, but removing it avoids a stale-file EADDRINUSE on the
        # next start (idempotent — no error if already gone).
        try:
            Path(self._socket_path).unlink(missing_ok=True)
        except PermissionError:
            logger.warning("mesh_server: could not remove socket %s", self._socket_path)
        logger.info("mesh_server: stopped")

    def __enter__(self) -> MeshServer:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()
