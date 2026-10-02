"""Federation server (B-side) — mediated pull endpoint (Phase 2).

ArchCom 2026-07-17 federation contract §3.2. An HTTP endpoint on the
B side that accepts mediated pull requests from a federated peer (A).
The flow is::

    A ──POST /api/v1/federation/pull──► B
    B: auth → rate limit → ACL → anti-correlation → search → moderate → trigger
    B ──PullResponse (trigger_code + sanitized records + ttl_class=ephemeral)──► A

This module is the **server flow** only — it is wired into the existing
FastAPI app (see :mod:`vesma.api.federation`) and reuses the Phase 1
prerequisite modules:

* :mod:`vesma.trigger_codes` — the five trigger codes (contract §9).
* :mod:`vesma.federation_access_log` — append-only audit log for
  anti-correlation tracking (contract §10, КП-5).
* :mod:`vesma.config` — :class:`FederationConfig` /
  :class:`PeerConfig` (per-peer ACL, ADR-0016).
* :mod:`vesma.moderation` — :func:`moderate` (no verdict cache, §0.п.6).
* :mod:`vesma.compact` — :func:`build_compact_record` for the sanitized
  response shape (contract §2.3).
* :mod:`vesma.manager` — :class:`MemoryManager.search` for the local
  search leg.

Design constraints (contract):

* **No verdict cache** (§0.п.6 отменён) — :func:`moderate` runs on every
  request, checking the current record state.
* **No plaintext query in the access log** — only SHA-256(topic) (КП-5).
* **Fail-closed** — empty ``peers`` / empty ``allowed_projects`` → refuse.
* **Ephemeral is policy, not technical** (§3.3) — the
  ``ttl_class="ephemeral"`` marker is a policy hint; the server does
  NOT enforce TTL on the A side.
* **mTLS cert pinning is stubbed** — mTLS termination is often at a
  reverse proxy; the stub verifies the fingerprint when a cert is
  presented to the ASGI app, but does not terminate TLS itself. See
  :func:`verify_mtls_fingerprint`.

Reference:
    - Contract §3.2: ``.archcom/sessions/2026-07-17-federation-contract.md``
    - Contract §3.3: ephemeral enforcement (policy layer)
    - Contract §9: trigger codes
    - Contract §10: federation_access_log
    - ADR-0016: ``docs/project/adr/0016-federation-threat-model.md``
"""

from __future__ import annotations

import contextlib
import hmac
import logging
import threading
import time
from collections import defaultdict, deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from vesma.compact import CompactRecord, build_compact_record
from vesma.config import PeerConfig, Settings
from vesma.federation_access_log import (
    AccessLogEntry,
    FederationAccessLog,
    hash_topic,
)
from vesma.manager import MemoryManager
from vesma.models import NO_FEDERATE_TAG
from vesma.moderation import moderate
from vesma.trigger_codes import TriggerCode

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

__all__ = [
    "PullRequest",
    "PullResponse",
    "PullResult",
    "RateLimitBucket",
    "RateLimiter",
    "handle_pull",
    "verify_mtls_fingerprint",
]

# Per ADR-0016 the server identifies itself as ``mnemos-B`` in the
# ``source_agent`` field of compact records it ships. The id is a
# deployment constant, not a per-request value — there is one B per
# instance. (Callers that need a different id pass ``self_agent_id``
# explicitly; the default is stable so the compact-record ``id`` prefix
# (``fed:mnemos-B:<uuid>``) is reproducible.)
DEFAULT_SELF_AGENT_ID = "mnemos-B"


# ── Pydantic I/O models ───────────────────────────────────────────────────────


class PullRequest(BaseModel):
    """Inbound pull request from a federated peer (A) — contract §3.2.

    Fields:
        peer_id: A2A id of the requesting peer (A). Used to look up the
            per-peer :class:`PeerConfig` and the per-peer rate-limit
            bucket.
        query: The query topic. B hashes it (SHA-256) for the access
            log and NEVER stores the plaintext topic (КП-5, §0.п.8).
        project_scope: Which project A wants to pull. Checked against
            ``PeerConfig.allowed_projects`` (subset of
            ``FederationConfig.shared_projects``).
        include_content: If ``False``, B returns metadata-only
            (Phase 2.5 hook — not built in Phase 2; the server accepts
            the flag but currently ignores it and always ships the
            compact record). Default ``True``.
    """

    peer_id: str = Field(..., min_length=1, max_length=256)
    query: str = Field(..., min_length=1, max_length=4096)
    project_scope: str = Field(..., min_length=1, max_length=256)
    include_content: bool = True


class PullResponse(BaseModel):
    """Outbound pull response from B to A — contract §3.2, §3.3, §9.

    Fields:
        trigger_code: One of the five :class:`TriggerCode` values. A
            dispatches on this (see :mod:`vesma.federation_client`).
        records: Sanitized compact records (see :class:`CompactRecord`).
            Empty for ``ALREADY_EXHAUSTED`` and ``REFUSED``. May be empty
            for ``EXHAUSTIVE`` when B has nothing on the topic (see
            ``Decision: EXHAUSTIVE with empty records`` below).
        ttl_class: Policy marker (contract §3.3). Always
            ``"ephemeral"`` in Phase 2 — the mediated-pull channel is
            ephemeral by policy; the A-side agent body is responsible
            for NOT calling ``mnemos_add`` on these records. The server
            does NOT enforce TTL — this is a policy hint, not a
            technical guarantee.
        peer_id: The B-side agent id that produced the records (for A's
            provenance check).
    """

    trigger_code: TriggerCode
    records: list[CompactRecord] = Field(default_factory=list)
    ttl_class: str = "ephemeral"
    peer_id: str = ""


class PullResult(BaseModel):
    """A-side result of a federation pull (see :mod:`vesma.federation_client`).

    Defined here (server module) because it is the shared return type
    the A2A handler imports from one place. The client returns it; the
    A2A handler dispatches on ``trigger_code`` + ``fell_back_to_local``.
    """

    trigger_code: TriggerCode
    records: list[CompactRecord] = Field(default_factory=list)
    fell_back_to_local: bool = False
    peer_id: str = ""


# ── mTLS fingerprint verification (stub) ──────────────────────────────────────


def verify_mtls_fingerprint(
    presented_fingerprint: str | None,
    expected_fingerprint: str | None,
) -> bool:
    """Verify a presented client-cert fingerprint against the pinned one.

    ADR-0016 mandates mTLS client-cert pinning per peer. In Phase 2 the
    mTLS termination is often at a reverse proxy (Caddy, nginx, Traefik)
    that strips the client cert before the request reaches the ASGI
    app, so this function is a **stub**: it enforces the pin only when
    BOTH a presented and an expected fingerprint are present. The two
    practical cases are:

    * ``expected_fingerprint is None`` → the operator opted out of
      pinning for this peer → return ``True`` (no enforcement).
    * ``presented_fingerprint is None`` (proxy stripped the cert) and
      ``expected_fingerprint`` is set → return ``False`` (fail-closed;
      the operator asked for pinning and got no cert to pin against).

    When both are present, the comparison is constant-time via
    :func:`hmac.compare_digest` to avoid a timing oracle.

    TODO: when Vesma terminates mTLS itself (ASGI middleware + an
    ssl context), read the client cert from the request connection
    and compute its SHA-256 fingerprint. Until then, the proxy MUST
    inject the fingerprint as a header (e.g. ``X-Client-Cert-Fingerprint``)
    that the route reads and passes here. The header name is not pinned
    here so the proxy layer stays pluggable.
    """
    if expected_fingerprint is None:
        # Operator opted out of pinning for this peer.
        return True
    if presented_fingerprint is None:
        # Operator asked for pinning, but no cert was presented
        # (proxy stripped it or the connection was not mTLS). Fail-closed.
        logger.warning(
            "federation_server: mTLS pinning configured for peer but no "
            "client cert fingerprint was presented — refusing (fail-closed)"
        )
        return False
    return hmac.compare_digest(
        presented_fingerprint.strip().lower(),
        expected_fingerprint.strip().lower(),
    )


# ── Per-peer rate limiter (in-memory, sliding window) ─────────────────────────


class RateLimitBucket:
    """One peer's sliding-window rate-limit bucket.

    Stores request timestamps in a deque and evicts entries older than
    the 60-second window on each ``check()``. Thread-safe via the
    parent :class:`RateLimiter`'s lock.
    """

    __slots__ = ("events", "window_sec")

    def __init__(self, *, window_sec: int = 60) -> None:
        self.window_sec = window_sec
        self.events: deque[float] = deque()

    def check(self, *, now: float, limit: int) -> bool:
        """Return ``True`` if a request is allowed under ``limit`` per window.

        Evicts expired entries first, then counts. A request is allowed
        if the count after eviction is strictly less than ``limit``
        (the current request is counted before the boolean is returned).
        """
        cutoff = now - self.window_sec
        while self.events and self.events[0] <= cutoff:
            self.events.popleft()
        if len(self.events) >= limit:
            return False
        self.events.append(now)
        return True


class RateLimiter:
    """Per-peer in-memory rate limiter — ``PeerConfig.rate_limit_per_minute``.

    Slowapi is already wired for the ``/auth/*`` surface, but its key
    function resolves the client IP, not the peer id. The federation
    pull endpoint keys on ``peer_id`` (the authenticated A2A id), not
    on the network peer — a single IP can legitimately carry multiple
    peers (e.g. a proxy in front of several mnemos-A instances), and a
    single peer can rotate IPs. A per-peer bucket keyed on ``peer_id``
    is the correct granularity. The limiter is process-local; for
    multi-worker deployments the operator should use an external
    limiter (Redis) — documented in ``docs/en/admin/federation.md``.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, RateLimitBucket] = defaultdict(
            lambda: RateLimitBucket(window_sec=60)
        )
        self._lock = threading.Lock()

    def check(self, peer_id: str, *, limit_per_minute: int, now: float | None = None) -> bool:
        """Return ``True`` if the peer is under its per-minute limit.

        ``now`` is for access-log timestamps only and is **not** used for
        the rate-window logic — :func:`handle_pull` may receive a
        ``datetime``-derived ``now`` (a different time scale than
        :func:`time.monotonic`), so mixing them in the same bucket would
        skew the 60-second window. The limiter always uses
        :func:`time.monotonic` internally for the bucket; the ``now``
        parameter is kept only for direct unit-test injection of
        monotonic-scale values.
        """
        ts = now if now is not None else time.monotonic()
        with self._lock:
            return self._buckets[peer_id].check(now=ts, limit=limit_per_minute)

    def reset(self, peer_id: str | None = None) -> None:
        """Clear buckets — used by tests to isolate rate-limit state."""
        with self._lock:
            if peer_id is None:
                self._buckets.clear()
            else:
                self._buckets.pop(peer_id, None)


# Module-level singleton — one limiter per federation-server process.
_limiter = RateLimiter()


# ── Auth helper ───────────────────────────────────────────────────────────────


def _resolve_peer_token(peer: PeerConfig) -> str | None:
    """Read the per-peer bearer token from the env var named in config.

    Per :class:`PeerConfig.bearer_token_env` we store the NAME of the
    env var, never the value (``sensitive-data.instructions.md``). The
    server reads the token at request time so a token rotation does
    not require a process restart. Returns ``None`` when the env var is
    unset or empty (fail-closed — treated as a token mismatch).
    """
    import os

    raw = os.environ.get(peer.bearer_token_env, "")
    return raw or None


# ── Main flow ─────────────────────────────────────────────────────────────────


def handle_pull(*args: Any, **kwargs: Any) -> tuple[PullResponse, int]:
    """Vitals boundary #7 (A2): time the pull, then delegate."""
    import time as _time

    t0 = _time.monotonic()
    try:
        result = _handle_pull_impl(*args, **kwargs)
    except Exception:
        mgr = kwargs.get("manager")
        if mgr is not None:
            with contextlib.suppress(Exception):
                mgr.record_verb_vitals(
                    surface="background",
                    verb="federation.pull",
                    status="error",
                    latency_ms=(_time.monotonic() - t0) * 1000,
                )
        raise
    mgr = kwargs.get("manager")
    if mgr is not None:
        with contextlib.suppress(Exception):
            mgr.record_verb_vitals(
                surface="background",
                verb="federation.pull",
                status="ok",
                latency_ms=(_time.monotonic() - t0) * 1000,
            )
    return result


def _handle_pull_impl(
    request: PullRequest,
    *,
    settings: Settings,
    manager: MemoryManager,
    access_log: FederationAccessLog,
    presented_token: str | None,
    presented_mtls_fingerprint: str | None = None,
    self_agent_id: str = DEFAULT_SELF_AGENT_ID,
    rate_limiter: RateLimiter | None = None,
    now: datetime | None = None,
) -> tuple[PullResponse, int]:
    """Execute the mediated-pull flow and return ``(response, http_status)``.

    This is the core server flow (contract §3.2). It is a pure function
    over its arguments (no globals touched except the module-level
    rate limiter, which is injectable via ``rate_limiter``) so it is
    fully testable without a running ASGI app.

    Steps:

    1. **Auth** — empty peers / unknown peer / token mismatch / mTLS
       mismatch → 403 (fail-closed).
    2. **Rate limit** — ``PeerConfig.rate_limit_per_minute`` → 429.
    3. **ACL** — ``project_scope`` in ``allowed_projects`` (or ``["*"]``)
       → 403 with ``REFUSED``.
    4. **Anti-correlation** — if the most recent access-log entry for
       this (peer, topic) has ``trigger_code=EXHAUSTIVE`` → return
       ``ALREADY_EXHAUSTED`` WITHOUT re-running search (contract §9).
    5. **Local search** — ``MemoryManager.search`` with
       ``project=project_scope``, exclude ``mnemos:no-federate`` records.
       Apply ``PeerConfig.allowed_types`` filter on results.
    6. **Moderation** — for each result, run :func:`moderate` (no verdict
       cache, §0.п.6). Refuse → exclude. Build :class:`CompactRecord` via
       :func:`build_compact_record`.
    7. **Trigger code selection** — see ``_select_trigger_code``.
    8. **Access log** — write an :class:`AccessLogEntry`.
    9. **Response** — :class:`PullResponse` with ``ttl_class="ephemeral"``.

    Note on the ``now`` parameter: it is used **only for access-log
    timestamps**. The :class:`RateLimiter` always uses
    :func:`time.monotonic` internally for the rate window — do NOT
    pass a ``datetime``-derived float to the limiter, the scales differ.
    """
    ts = now if now is not None else datetime.now(UTC)
    fed = settings.federation

    # ── 1. Auth (fail-closed) ──────────────────────────────────────────
    if not fed.peers:
        logger.info("federation_server: refused — no peers configured")
        return _refused_response(), 403
    peer = fed.peers.get(request.peer_id)
    if peer is None:
        logger.info(
            "federation_server: refused — peer_id=%s not in peers config",
            request.peer_id,
        )
        return _refused_response(), 403

    expected_token = _resolve_peer_token(peer)
    if (
        not expected_token
        or presented_token is None
        or not hmac.compare_digest(presented_token, expected_token)
    ):
        logger.info(
            "federation_server: refused — token mismatch for peer_id=%s",
            request.peer_id,
        )
        return _refused_response(), 403

    if not verify_mtls_fingerprint(presented_mtls_fingerprint, peer.mtls_cert_fingerprint):
        # Audit-log the mTLS-mismatch refusal (contract §10). The peer is
        # known and resolved (we are past the unknown-peer gate), but the
        # presented client cert does not match the pinned fingerprint — a
        # forensic signal (potential cert theft / MITM / mis-rotation).
        # Only this path is logged: no-peers / unknown-peer / token-mismatch
        # paths are NOT logged because the peer_id is untrusted there and
        # could be attacker-controlled noise.
        if peer.mtls_cert_fingerprint is not None:
            logger.warning(
                "federation_server: mTLS cert mismatch for peer_id=%s "
                "(pinned fingerprint did not match presented)",
                request.peer_id,
            )
            _log_access(
                access_log,
                peer_id=request.peer_id,
                topic=request.query,
                project_scope=request.project_scope,
                trigger_code=TriggerCode.REFUSED,
                record_ids=[],
                now=ts,
            )
        return _refused_response(), 403

    # ── 2. Rate limit ──────────────────────────────────────────────────
    limiter = rate_limiter if rate_limiter is not None else _limiter
    if not limiter.check(request.peer_id, limit_per_minute=peer.rate_limit_per_minute):
        logger.warning(
            "federation_server: rate limit exceeded for peer_id=%s",
            request.peer_id,
        )
        # Audit-log the post-auth refusal (contract §10): a rate-limited
        # peer is authenticated and known, so the refusal is a forensic
        # signal (peer is hammering B past its quota).
        _log_access(
            access_log,
            peer_id=request.peer_id,
            topic=request.query,
            project_scope=request.project_scope,
            trigger_code=TriggerCode.REFUSED,
            record_ids=[],
            now=ts,
        )
        return _refused_response(trigger_code=TriggerCode.REFUSED), 429

    # ── 3. ACL ─────────────────────────────────────────────────────────
    if not _acl_allows(peer, request.project_scope):
        logger.info(
            "federation_server: refused — project_scope=%s not allowed for peer_id=%s",
            request.project_scope,
            request.peer_id,
        )
        _log_access(
            access_log,
            peer_id=request.peer_id,
            topic=request.query,
            project_scope=request.project_scope,
            trigger_code=TriggerCode.REFUSED,
            record_ids=[],
            now=ts,
        )
        return _refused_response(), 403

    # ── 4. Anti-correlation (contract §9, §10) ────────────────────────
    topic_hash = hash_topic(request.query)
    prior = access_log.query(request.peer_id, topic_hash)
    if prior is not None and prior.trigger_code == TriggerCode.EXHAUSTIVE:
        # B already answered EXHAUSTIVE on this topic → ALREADY_EXHAUSTED.
        # Do NOT re-run search; do NOT re-ship sanitized content. Write a
        # new access-log entry with trigger_code=ALREADY_EXHAUSTED and
        # empty record_ids (contract §9).
        logger.info(
            "federation_server: ALREADY_EXHAUSTED for peer_id=%s topic_hash=%s",
            request.peer_id,
            topic_hash[:12],
        )
        _log_access(
            access_log,
            peer_id=request.peer_id,
            topic=request.query,
            project_scope=request.project_scope,
            trigger_code=TriggerCode.ALREADY_EXHAUSTED,
            record_ids=[],
            now=ts,
        )
        return (
            PullResponse(
                trigger_code=TriggerCode.ALREADY_EXHAUSTED,
                records=[],
                ttl_class="ephemeral",
                peer_id=self_agent_id,
            ),
            200,
        )

    # ── 5. Local search ───────────────────────────────────────────────
    results = manager.search(
        request.query,
        project=request.project_scope,
        limit=20,
    )

    # Apply PeerConfig.allowed_types filter on results (contract §3.2,
    # §6). Build compact records; moderation refuse → excluded.
    records: list[CompactRecord] = []
    refused_count = 0
    candidate_count = 0
    for sr in results:
        memory = sr.memory
        # Exclude no-federate records defensively (moderation would
        # refuse them anyway, but skip the moderation cost).
        if NO_FEDERATE_TAG in memory.tags:
            continue
        # Apply allowed_types filter (subset of mnemos:<subtype>).
        rec_type = _memory_type_for_filter(memory.tags)
        if not _type_allowed(peer, rec_type):
            continue
        candidate_count += 1
        # No verdict cache (§0.п.6): moderate on every request.
        mod_result = moderate(
            memory.content,
            tags=memory.tags,
            refuse_threshold=fed.moderation_refuse_threshold,
            mapping_ttl_hours=fed.moderation_mapping_ttl_hours,
        )
        rec = build_compact_record(
            memory,
            source_agent=self_agent_id,
            refuse_threshold=fed.moderation_refuse_threshold,
            moderation_result=mod_result,
        )
        if rec is None:
            refused_count += 1
            continue
        records.append(rec)

    # ── 7. Trigger code selection ────────────────────────────────────
    trigger_code = _select_trigger_code(
        candidate_count=candidate_count,
        refused_count=refused_count,
        records_count=len(records),
    )

    # ── 8. Access log ────────────────────────────────────────────────
    record_ids = [r.id for r in records]
    _log_access(
        access_log,
        peer_id=request.peer_id,
        topic=request.query,
        project_scope=request.project_scope,
        trigger_code=trigger_code,
        record_ids=record_ids,
        now=ts,
    )

    logger.info(
        "federation_server: pull peer_id=%s trigger=%s candidates=%d refused=%d shipped=%d",
        request.peer_id,
        trigger_code.value,
        candidate_count,
        refused_count,
        len(records),
    )

    # ── 9. Response ──────────────────────────────────────────────────
    return (
        PullResponse(
            trigger_code=trigger_code,
            records=records,
            ttl_class="ephemeral",
            peer_id=self_agent_id,
        ),
        200,
    )


# ── Trigger-code selection ────────────────────────────────────────────────────


def _select_trigger_code(
    *,
    candidate_count: int,
    refused_count: int,
    records_count: int,
) -> TriggerCode:
    """Pick the trigger code per contract §9.

    Decision table (documented — contract §9 leaves the "no records"
    case ambiguous; this is the documented choice):

    | Candidates | Refused | Shipped | Trigger code      |
    |------------|---------|---------|-------------------|
    | 0          | 0       | 0       | EXHAUSTIVE (empty) |
    | >0         | 0       | >0      | EXHAUSTIVE         |
    | >0         | >0      | >0      | PARTIAL            |
    | >0         | >0      | 0       | REFUSED            |

    Rationale per row:

    * ``0/0/0`` — "B has nothing on this topic" is a complete answer;
      A should not repeat the request (contract §9 recommendation).
    * ``>0/0/>0`` — all relevant records found and shipped.
    * ``>0/>0/>0`` — some records refused by moderation; more relevant
      content exists but B cannot ship it all.
    * ``>0/>0/0`` — all candidates refused; nothing was shipped. B had
      content but refused to share any of it. Per contract §9,
      ``REFUSED`` means "A should fall back to local search"; there is
      nothing for A to refine against (0 records), so ``PARTIAL``
      would be misleading. ``REFUSED`` is the correct signal: A does
      not get partial content to refine, it must fall back.

    The ``candidate_count == 0`` → ``EXHAUSTIVE`` (empty) choice is the
    contract §9 recommendation: "B has nothing on this topic" is a
    complete answer. Returning ``PARTIAL`` here would invite A to
    repeat the request verbatim, which contract §9 explicitly forbids
    for partial answers ("A may refine the request ... but should NOT
    repeat the exact same request"). ``EXHAUSTIVE`` with empty records
    tells A "there is nothing here, do not re-query" — the correct
    signal for an empty corpus on B.
    """
    if candidate_count == 0:
        # No records found at all → EXHAUSTIVE with empty records
        # (contract §9: "nothing here" is a complete answer).
        return TriggerCode.EXHAUSTIVE
    if refused_count == 0:
        # All relevant records found and shipped → EXHAUSTIVE.
        return TriggerCode.EXHAUSTIVE
    if records_count == 0:
        # All candidates refused, nothing shipped → REFUSED
        # (contract §9: A falls back to local search; 0 records means
        # nothing to refine, so PARTIAL would be misleading).
        return TriggerCode.REFUSED
    # Some records refused, some shipped → PARTIAL (contract §9).
    return TriggerCode.PARTIAL


# ── ACL helpers ───────────────────────────────────────────────────────────────


def _acl_allows(peer: PeerConfig, project_scope: str) -> bool:
    """Return ``True`` if ``project_scope`` is in the peer's allowed list.

    ``["*"]`` is the explicit wildcard. Empty list = none (fail-closed).
    """
    if not peer.allowed_projects:
        return False
    if "*" in peer.allowed_projects:
        return True
    return project_scope in peer.allowed_projects


def _memory_type_for_filter(tags: list[str]) -> str:
    """Return the ``mnemos:<subtype>`` value for a memory, or ``""`` if none.

    Mirrors :func:`vesma.compact.derive_record_type` but returns the raw
    subtype string (e.g. ``"decision"``) so it can be matched against
    ``PeerConfig.allowed_types``. ``mnemos:no-federate`` is skipped.
    """
    for tag in tags:
        if not tag.startswith("mnemos:"):
            continue
        suffix = tag[len("mnemos:") :]
        if suffix == "no-federate":
            continue
        return suffix
    return ""


def _type_allowed(peer: PeerConfig, rec_type: str) -> bool:
    """Return ``True`` if ``rec_type`` is in the peer's allowed types.

    ``["*"]`` is the explicit wildcard. Empty list = none (fail-closed).
    An empty ``rec_type`` (memory has no ``mnemos:<subtype>`` tag) is
    treated as ``session`` by :func:`derive_record_type` for compact
    records, but here we filter on the raw subtype — if a memory has
    no subtype tag, it is excluded by an explicit ``allowed_types``
    list (the operator said which types they allow; untyped records do
    not match). With ``["*"]`` the untyped record passes.
    """
    if not peer.allowed_types:
        return False
    if "*" in peer.allowed_types:
        return True
    return rec_type in peer.allowed_types


# ── Access-log helper ─────────────────────────────────────────────────────────


def _log_access(
    access_log: FederationAccessLog,
    *,
    peer_id: str,
    topic: str,
    project_scope: str,
    trigger_code: TriggerCode,
    record_ids: list[str],
    now: datetime,
) -> None:
    """Append one :class:`AccessLogEntry` to the audit log.

    The topic is hashed (SHA-256) before storage — the plaintext never
    enters the log (КП-5). Failures to append are logged but do NOT
    abort the request: the audit log is a leak surface and a
    defence-in-depth control, not a correctness gate. If we refused
    the request because the log was unwritable, we would turn a
    log-filesystem-full event into a federation outage — the wrong
    trade.
    """
    entry = AccessLogEntry(
        peer_id=peer_id,
        topic_hash=hash_topic(topic),
        timestamp=now,
        project_scope=project_scope,
        trigger_code=trigger_code,
        record_ids_accessed=list(record_ids),
    )
    try:
        access_log.append(entry)
    except OSError as exc:
        logger.error(
            "federation_server: access-log append failed (peer_id=%s, "
            "trigger=%s): %s — request continues, log is best-effort",
            peer_id,
            trigger_code.value,
            exc,
        )


# ── Response helpers ──────────────────────────────────────────────────────────


def _refused_response(trigger_code: TriggerCode = TriggerCode.REFUSED) -> PullResponse:
    """Build the canonical REFUSED response (empty records, ephemeral)."""
    return PullResponse(
        trigger_code=trigger_code,
        records=[],
        ttl_class="ephemeral",
        peer_id="",
    )
