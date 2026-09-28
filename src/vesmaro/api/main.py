"""FastAPI HTTP API for Mnemos.

Mirrors MCP tools as REST endpoints.
Loopback-bound by default (127.0.0.1) — do not expose externally without auth.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import sys
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.middleware.cors import CORSMiddleware

from vesmaro import __version__
from vesmaro.api.auth import router as auth_router
from vesmaro.api.auth_store import AuthStore
from vesmaro.api.federation import router as federation_router
from vesmaro.api.middleware import AuthMiddleware, VitalsVerbMiddleware
from vesmaro.api.rate_limit import limiter
from vesmaro.config import ApiConfig, Settings, load_settings
from vesmaro.context_rewrite import ContextRewriteRateLimitError
from vesmaro.hooks import HOOK_ACTIONS, dispatch_hook
from vesmaro.manager import MemoryManager, SessionAgentMismatchError
from vesmaro.models import (
    CHECKPOINT_FIELDS,
    AgentRecallQuery,
    FilterRequest,
    Memory,
    MemoryCreate,
    MemoryStatus,
    MemoryType,
    RuleIngestRequest,
    RuleRemoveRequest,
    SearchQuery,
    is_quarantined,
    validate_tag_contract,
)
from vesmaro.sessions import SessionStore
from vesmaro.sessions.api import router as sessions_router

logger = logging.getLogger(__name__)
_logger = logger  # backward-compat alias used by CORS/tags code paths
_manager: MemoryManager | None = None


def _setup_cors(application: FastAPI, settings: Settings) -> None:
    """Add CORSMiddleware when CORS is enabled and origins are configured.

    Strict default: if cors_enabled=False or cors_allow_origins is empty,
    no middleware is added and no cross-origin request is permitted.

    Security invariant: allow_origins=["*"] combined with
    allow_credentials=True is forbidden by the Fetch/CORS specification
    (a credential-bearing wildcard response is rejected by all compliant
    browsers and signals a misconfiguration).  This combination raises
    ValueError at startup rather than silently shipping a broken config.
    """
    cfg = settings.api
    if not cfg.cors_enabled or not cfg.cors_allow_origins:
        return
    if "*" in cfg.cors_allow_origins and cfg.cors_allow_credentials:
        raise ValueError(
            "CORS misconfiguration: cors_allow_origins=['*'] combined with "
            "cors_allow_credentials=True is forbidden by the CORS spec. "
            "Either restrict cors_allow_origins to explicit origins or set "
            "cors_allow_credentials=False."
        )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_allow_origins,
        allow_credentials=cfg.cors_allow_credentials,
        allow_methods=cfg.cors_allow_methods,
        allow_headers=cfg.cors_allow_headers,
    )
    _logger.info("CORS enabled for %d origin(s)", len(cfg.cors_allow_origins))


def get_manager(config: str | None = None) -> MemoryManager:
    """Return the process-wide :class:`MemoryManager` singleton.

    ``config`` is only consulted on the FIRST call (when the singleton
    is constructed); later calls return the cached instance and ignore
    it — same semantics as :func:`vesmaro.cli._manager.get_manager`.
    Added for the native mesh serve wiring (W2) so ``vesmaro serve
    --config …`` can seed the singleton MeshServer shares with the
    in-process HTTP app; callers that pass nothing keep the old
    config-discovery behaviour.
    """
    global _manager
    if _manager is None:
        _manager = MemoryManager(load_settings(config))
    return _manager


# ── Startup guard (ADR-0014 §Trust zones) ─────────────────────────────────────


def _is_loopback_host(host: str) -> bool:
    if host in {"localhost", "ip6-localhost"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_non_loopback_auth(api_cfg: ApiConfig) -> None:
    """Exit non-zero if a non-loopback bind is attempted without required auth config.

    Enforced at startup so a misconfigured ``auth_enabled: false`` server never
    becomes reachable from the network without credentials.
    """
    if _is_loopback_host(api_cfg.host):
        return

    missing: list[str] = []
    if not api_cfg.auth_enabled:
        missing.append("api.auth_enabled=true")
    if not api_cfg.totp_enabled:
        missing.append("api.totp_enabled=true")
    if not api_cfg.behind_tls_proxy:
        missing.append("api.behind_tls_proxy=true")

    if missing:
        print(
            f"FATAL: non-loopback bind ({api_cfg.host!r}) requires: "
            + ", ".join(missing)
            + ".  See docs/security.md for the remote setup guide.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # TOTP enabled but master key missing → refuse to start
    if api_cfg.totp_enabled and not api_cfg.totp_master_key.get_secret_value():
        print(
            "FATAL: api.totp_enabled=true but VESMARO_API__TOTP_MASTER_KEY is not set.",
            file=sys.stderr,
        )
        raise SystemExit(1)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    mgr = get_manager()  # warm up (also runs the DDL on the shared db file)
    settings = mgr.settings

    # T-AUTH: startup guard — must run before any state is exposed.
    _check_non_loopback_auth(settings.api)

    # Expose API config on app.state so AuthMiddleware can read it without
    # calling load_settings() on every request.
    application.state.api_config = settings.api

    # M16: own SessionStore lives on app.state so the A2A router can
    # pick it up.  Re-uses the same db_path as MemoryManager so the
    # schema and WAL are shared.
    store = SessionStore(settings.db_path)
    application.state.sessions_store = store

    # T-AUTH: AuthStore on app.state for auth router and middleware.
    auth_store = AuthStore(settings.db_path)
    application.state.auth_store = auth_store

    # Start the background processor so raw memories added via the HTTP
    # API are automatically clustered → synthesized → quality-gated →
    # published + vector-indexed. Without this, POST /memories leaves
    # memories in `raw` status forever (queue grows, last_processed_at
    # stays None) — the same bug previously fixed for the MCP server
    # (see CHANGELOG [2.3.0] "Background processor not running").
    mgr.start_background_processor()

    # Start the background secrets scanner (Layer 2 defence-in-depth,
    # #89). No-op when ``scanner.enabled`` is False. Runs on its own
    # daemon thread so it never blocks the HTTP request loop.
    from vesmaro.scanner_runtime import get_scanner

    scanner = get_scanner(mgr)
    scanner.start()

    # S2 phase 2 — background federation metadata poller (ADR-0021
    # Q10.2 poll-first, default-off). asyncio task on the serve loop:
    # the poller spends its life awaiting the mesh CLI subprocess, so
    # it must not occupy a thread. No-op when ``meta_poll.enabled`` is
    # False — without the config key the lifespan is byte-identical to
    # phase 1.
    meta_poller = None
    if settings.federation.meta_poll.enabled:
        from vesmaro.meta_poller import MetaPoller

        meta_poller = MetaPoller(mgr.sqlite, settings.federation)
        meta_poller.start()

    try:
        yield
    finally:
        if meta_poller is not None:
            await meta_poller.stop()
        scanner.stop()
        mgr.stop_background_processor()
        store.close()
        auth_store.close()
        if _manager is not None:
            _manager.close()


app = FastAPI(
    title="Mnemos",
    description="Standalone memory & knowledge server for AI agents.",
    version=__version__,
    lifespan=lifespan,
    # Bind only to loopback by default; controlled by uvicorn host arg
    docs_url="/docs",
    redoc_url="/redoc",
)

# T-AUTH: rate limiter state + exception handler
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

# T-AUTH: auth middleware (runs after CORS, before routes)
app.add_middleware(AuthMiddleware)
# ADR-0026 phase A2 — REST verb boundary (innermost, route-template only).
app.add_middleware(VitalsVerbMiddleware)


# ── Health ─────────────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ── Dashboard / metrics (mnemos-eyes) ─────────────────────────────────────────


@app.get("/api/v1/stats")
async def dashboard_stats() -> dict[str, Any]:
    """Structured JSON dashboard data for mnemos-eyes."""
    return get_manager().dashboard_stats()


@app.get("/api/v1/stats/timeseries")
async def stats_timeseries(
    metric: str = Query(default="memories_added"),
    range: str = Query(default="30d"),
    granularity: str = Query(default="day"),
) -> dict[str, Any]:
    """Temporal data for dashboard charts."""
    mgr = get_manager()
    # Parse range like "30d", "7d", "90d"
    days: int | None = None
    if range.endswith("d"):
        try:
            days = int(range[:-1])
        except ValueError:
            days = None
    elif range.endswith("h"):
        # Hour ranges not yet supported by the daily query; clamp to 1 day.
        days = 1
    if days is None or days <= 0:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid range '{range}'. Expected format like '30d' (positive integer + 'd').",
        )
    return mgr.timeseries(metric=metric, days=days, granularity=granularity)


def _prometheus_text(mgr: MemoryManager) -> str:
    """Render dashboard stats as Prometheus exposition text."""
    data = mgr.dashboard_stats()
    vol = data["volume"]
    filt = data["filter"]
    pipe = data["pipeline"]
    search = data["search"]
    vectors = data["vectors"]
    sessions = data["sessions"]
    lines: list[str] = []
    lines.append("# HELP mnemos_memories_total Total number of memories in storage")
    lines.append("# TYPE mnemos_memories_total gauge")
    lines.append(f"mnemos_memories_total {vol['memories_total']}")
    lines.append("# HELP mnemos_memories_by_status Memories by status")
    lines.append("# TYPE mnemos_memories_by_status gauge")
    for s, c in vol["by_status"].items():
        lines.append(f'mnemos_memories_by_status{{status="{s}"}} {c}')
    lines.append("# HELP mnemos_memories_by_project Memories by project")
    lines.append("# TYPE mnemos_memories_by_project gauge")
    for p, c in vol["by_project"].items():
        lines.append(f'mnemos_memories_by_project{{project="{p}"}} {c}')
    lines.append("# HELP mnemos_memories_by_agent Memories by agent")
    lines.append("# TYPE mnemos_memories_by_agent gauge")
    for a, c in vol["by_agent"].items():
        lines.append(f'mnemos_memories_by_agent{{agent="{a}"}} {c}')
    lines.append("# HELP mnemos_memories_by_type Memories by memory_type")
    lines.append("# TYPE mnemos_memories_by_type gauge")
    for t, c in vol["by_type"].items():
        lines.append(f'mnemos_memories_by_type{{type="{t}"}} {c}')
    lines.append("# HELP mnemos_filter_avg_reduction_pct Average filter reduction percentage")
    lines.append("# TYPE mnemos_filter_avg_reduction_pct gauge")
    lines.append(f"mnemos_filter_avg_reduction_pct {filt['avg_reduction_pct']}")
    lines.append("# HELP mnemos_filter_filtered_total Memories with clean_content populated")
    lines.append("# TYPE mnemos_filter_filtered_total gauge")
    lines.append(f"mnemos_filter_filtered_total {filt['filtered_total']}")
    lines.append("# HELP mnemos_pipeline_processed_total Total processed memories")
    lines.append("# TYPE mnemos_pipeline_processed_total counter")
    lines.append(f"mnemos_pipeline_processed_total {pipe['processed_total']}")
    lines.append("# HELP mnemos_pipeline_dlq_depth Current DLQ depth")
    lines.append("# TYPE mnemos_pipeline_dlq_depth gauge")
    lines.append(f"mnemos_pipeline_dlq_depth {pipe['dlq_depth']}")
    lines.append("# HELP mnemos_search_requests_total Total search requests since restart")
    lines.append("# TYPE mnemos_search_requests_total counter")
    lines.append(f"mnemos_search_requests_total {search['requests_total']}")
    lines.append(
        "# HELP mnemos_search_cross_project_requests_total "
        "Search requests in the explicit global (cross-project) mode since restart"
    )
    lines.append("# TYPE mnemos_search_cross_project_requests_total counter")
    lines.append(
        f"mnemos_search_cross_project_requests_total {search['cross_project_requests_total']}"
    )
    lines.append("# HELP mnemos_search_avg_latency_ms Average search latency in ms")
    lines.append("# TYPE mnemos_search_avg_latency_ms gauge")
    lines.append(f"mnemos_search_avg_latency_ms {search['avg_latency_ms']}")
    lines.append(
        "# HELP mnemos_search_graph_supersedes_enriched_requests_total "
        "Search requests whose page the unconditional supersedes leg enriched since restart"
    )
    lines.append("# TYPE mnemos_search_graph_supersedes_enriched_requests_total counter")
    lines.append(
        "mnemos_search_graph_supersedes_enriched_requests_total "
        f"{search['graph_supersedes_enriched_requests_total']}"
    )
    lines.append(
        "# HELP mnemos_search_graph_walk_enriched_requests_total "
        "Search requests whose page the flag-gated relates_to walk enriched since restart"
    )
    lines.append("# TYPE mnemos_search_graph_walk_enriched_requests_total counter")
    lines.append(
        "mnemos_search_graph_walk_enriched_requests_total "
        f"{search['graph_walk_enriched_requests_total']}"
    )
    # ADR-0030 A0 (issue #322) — relates_to auto-minting rate telemetry
    # (in-memory counters, since restart; '' buckets unscoped writes).
    graph = data["graph"]
    lines.append(
        "# HELP mnemos_graph_auto_dedupe_edges_total "
        "relates_to edges minted by the auto-dedupe rule since restart"
    )
    lines.append("# TYPE mnemos_graph_auto_dedupe_edges_total counter")
    lines.append(f"mnemos_graph_auto_dedupe_edges_total {graph['auto_dedupe_edges_total']}")
    lines.append(
        "# HELP mnemos_graph_auto_dedupe_edges_by_project "
        "Auto-minted relates_to edges by project since restart"
    )
    lines.append("# TYPE mnemos_graph_auto_dedupe_edges_by_project counter")
    for p, c in graph["auto_dedupe_edges_by_project"].items():
        lines.append(f'mnemos_graph_auto_dedupe_edges_by_project{{project="{p}"}} {c}')
    lines.append("# HELP mnemos_vectors_indexed_total Indexed vectors")
    lines.append("# TYPE mnemos_vectors_indexed_total gauge")
    lines.append(f"mnemos_vectors_indexed_total {vectors['indexed_total']}")
    lines.append("# HELP mnemos_sessions_active Active sessions (updated within 24h)")
    lines.append("# TYPE mnemos_sessions_active gauge")
    lines.append(f"mnemos_sessions_active {sessions['active']}")
    lines.append("# HELP mnemos_sessions_total Total sessions")
    lines.append("# TYPE mnemos_sessions_total gauge")
    lines.append(f"mnemos_sessions_total {sessions['total']}")
    return "\n".join(lines) + "\n"


@app.get("/api/v1/metrics")
async def prometheus_metrics() -> Response:
    """Prometheus text exposition format for Grafana/observability."""
    mgr = get_manager()
    text = _prometheus_text(mgr) + mgr.vitals_exposition()
    return Response(
        content=text,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.get("/metrics")
async def metrics() -> dict[str, Any]:
    """Legacy metrics endpoint — returns stats() JSON for backward compat.

    For Prometheus text format, use ``GET /api/v1/metrics``.
    """
    return get_manager().stats()


# ── Memories CRUD ──────────────────────────────────────────────────────────────


@app.post("/memories", response_model=Memory, status_code=201)
async def create_memory(data: MemoryCreate) -> Memory:
    mgr = get_manager()
    settings = mgr.settings

    tags = validate_tag_contract(data.tags, strict=settings.mnemos.strict_tag_contract)
    data.tags = tags
    project = next((t[len("project:") :] for t in tags if t.startswith("project:")), "")
    agent = next((t[len("agent:") :] for t in tags if t.startswith("agent:")), "")
    return mgr.add(data, project=project, agent=agent)


def _operator_context(request: Request | None) -> bool:
    """ADR-0019 §5 B2b amendment — is this request an OPERATOR context?

    ``quarantine_reason`` on the direct-access response is operator-only
    data (the retraction render itself is cause-neutral — CWE-209). The
    trust decision follows the T-AUTH conventions (ADR-0014):

    * ``api.auth_enabled=False`` (default) — the loopback single-operator
      deploy: the caller IS the operator, the field stays visible;
    * ``auth_enabled=True`` — only a request the AuthMiddleware admitted
      (``request.state.auth_session`` / ``auth_token`` set) is operator
      context; anything else loses the field (not the response).
    """
    if request is None:
        # No injected request ⇒ no admitted session ⇒ not operator context.
        # (Defensive: FastAPI injects the request in every real call path.)
        return False
    api_cfg = getattr(request.app.state, "api_config", None)
    if api_cfg is None or not getattr(api_cfg, "auth_enabled", False):
        return True
    state = getattr(request, "state", None)
    return (
        getattr(state, "auth_session", None) is not None
        or getattr(state, "auth_token", None) is not None
    )


@app.get("/memories/{memory_id}", response_model=Memory)
async def get_memory(memory_id: str, request: Request, include_raw: bool = False) -> Memory:
    """Read one memory by id (direct access).

    ADR-0019 §5 B2b: a quarantined row answers with the cause-neutral
    retraction render (see ``MemoryManager.get``); ``raw_content`` /
    ``clean_content`` / ``title`` are withheld on this channel. The
    ``quarantine_reason`` metadata field is visible only in an operator
    context (``_operator_context``) — the agent-facing response shape
    carries the retraction without the detector class.
    """
    mgr = get_manager()
    memory = mgr.get(memory_id)
    if memory is None:
        raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found")
    if is_quarantined(memory) and not _operator_context(request):
        memory = memory.model_copy(update={"quarantine_reason": None})
    if not include_raw:
        memory = memory.model_copy(update={"raw_content": None})
    return memory


@app.get("/memories", response_model=list[Memory])
async def list_memories(
    status: str | None = None,
    project: str | None = None,
    agent: str | None = None,
    tags: str | None = None,
    task: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = Query(default=20, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[Memory]:
    mgr = get_manager()
    status_enum: MemoryStatus | None = None
    if status:
        try:
            status_enum = MemoryStatus(status)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid status '{status}'. Valid: {[s.value for s in MemoryStatus]}",
            ) from exc
    tag_list: list[str] | None = None
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    # Ф2 (epic #308): the task boundary's ValueError (unsalvageable
    # slug / prefix-carrying value) maps to 400 like the recall twin —
    # the #407 twin discipline: the SAME error string, never a raw
    # traceback to the REST caller.
    try:
        return mgr.list_recent(
            limit=limit,
            offset=offset,
            project=project,
            agent=agent,
            task=task,
            status=status_enum,
            tags=tag_list,
            since=since,
            until=until,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ── Workflow lifecycle — nested under /memories/{id}/workflow (#96) ────────────
#
# Thin wrappers over MemoryManager.workflow_set / workflow_get /
# workflow_history. The state machine + 5 guardrails are enforced
# server-side in the manager — these endpoints MUST NOT re-validate, so
# there is exactly one enforcement path (the manager) that neither the
# MCP tool nor the REST layer can bypass.


class WorkflowSetRequest(BaseModel):
    """Body for ``POST /memories/{memory_id}/workflow``."""

    to: str
    actor: str
    reason: str = ""
    force: bool = False


@app.get("/memories/{memory_id}/workflow")
async def get_workflow(memory_id: str) -> dict[str, Any]:
    """Return the current workflow status + lock owner for a memory.

    ``workflow_status`` is normalised to ``open`` when the memory has
    never had its workflow set. Returns 404 when the memory does not exist.
    """
    mgr = get_manager()
    result = mgr.workflow_get(memory_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found")
    return result


@app.post("/memories/{memory_id}/workflow")
async def set_workflow(memory_id: str, req: WorkflowSetRequest) -> dict[str, Any]:
    """Transition a memory's workflow status through the state machine.

    Maps to ``workflow_set``. ``ValueError`` from the manager is a
    guardrail / state-machine violation → HTTP 409 (conflict). The manager
    is the single source of truth; no validation is duplicated here.
    """
    mgr = get_manager()
    try:
        return mgr.workflow_set(
            memory_id,
            req.to,
            actor=req.actor,
            reason=req.reason,
            force=req.force,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.delete("/memories/{memory_id}/workflow")
async def withdraw_workflow(
    memory_id: str,
    actor: str = Query(default="", description="Actor withdrawing the memory (free-form)."),
    reason: str = Query(default="DELETE withdraw", description="Reason for the withdrawal."),
    force: bool = Query(default=False, description="Override a lock held by another actor."),
) -> dict[str, Any]:
    """Cancel a memory's workflow by transitioning to the ``withdrawn`` state.

    ``DELETE`` is a **cancel / withdraw**, NOT a lock-release-to-resumable.
    It ends the workflow in ``withdrawn`` — a **terminal, irreversible**
    state from which no further transition is possible. The lock
    (``locked_by`` / ``locked_at``) is cleared as a side effect of reaching
    a terminal state, but the memory is NOT returned to a resumable
    ``open`` state. The state machine has no edge back to ``open`` from an
    active state, so this is the only cancellation path.

    Semantics:

    - To **finish** work normally, use ``POST .../workflow`` with
      ``to=done`` — that is the completion path.
    - ``DELETE`` is the **cancel / abandon** path: the workflow ends in
      ``withdrawn`` (terminal) and any held lock is released. Once
      withdrawn the memory cannot transition further.
    - ``force`` defaults to ``False``: the actor holding the lock can
      withdraw it without force. To override a lock held by a *different*
      actor, pass ``force=true`` (guardrail 4 — a reason is recorded).

    ``actor`` is required. A 409 is returned if the transition is
    forbidden (e.g. the memory is already terminal) or the lock is held
    by another actor without ``force``.
    """
    mgr = get_manager()
    if not actor:
        raise HTTPException(
            status_code=422,
            detail="actor query param is required to withdraw a memory's workflow",
        )
    try:
        return mgr.workflow_set(
            memory_id,
            "withdrawn",
            actor=actor,
            reason=reason,
            force=force,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# ── Search ─────────────────────────────────────────────────────────────────────


@app.post("/search")
async def search(query: SearchQuery) -> list[dict[str, Any]]:
    mgr = get_manager()
    # Ф2 (epic #308): the task boundary's ValueError (unsalvageable slug /
    # prefix-carrying value) maps to 400 like the recall/save twins — the
    # #407 twin discipline: the SAME error string, never a raw traceback.
    try:
        results = mgr.search(
            query=query.query,
            tags=query.tags,
            project=query.project,
            task=query.task,
            status=query.status,
            limit=query.limit,
            include_raw=query.include_raw,
            refined_only=query.refined_only,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    out = []
    for r in results:
        mem = r.memory
        content = mem.effective_content()
        if query.include_raw and mem.raw_content:
            content = mem.raw_content
        # ADR-0018 P1-b (M1 + review F1/F3): scan-at-issuance — BOTH echoed
        # strings (the exact content being issued, post raw_content swap,
        # and the title) are scanned/redacted per item; refuse mode drops
        # the item (fail-closed; drop log carries the memory id).
        scan = mgr.scan_issuance_item(
            content, title=mem.auto_title(), context=f"api:/search:{mem.id}"
        )
        if scan.refused:
            continue
        item = {
            "id": mem.id,
            "title": scan.title,
            "content": scan.content,
            "tags": mem.tags,
            "status": mem.status.value,
            "score": r.score,
            "search_type": r.search_type,
            "redactions": scan.redactions,
        }
        if scan.redactions:
            item["redacted_patterns"] = scan.redacted_patterns
        out.append(item)
    return out


# ── Per-agent recall (M3) ──────────────────────────────────────────────────────


@app.get("/recall/agent/{name}")
async def agent_recall(
    name: str,
    project: str | None = None,
    q: str | None = None,
    task: str | None = None,
    limit: int = Query(default=20, le=100),
) -> list[dict[str, Any]]:
    mgr = get_manager()
    # Ф2 review fix 1 (P1): the task boundary's ValueError (unsalvageable
    # slug / prefix-carrying value) maps to 400 like every other REST
    # twin of this wave — the #407 twin discipline; without the handler
    # the probe GET /recall/agent/x?task=my/task surfaced as a 500,
    # contradicting the documented 400.
    try:
        query = AgentRecallQuery(agent=name, project=project, query=q, task=task, limit=limit)
        results = mgr.agent_recall(query)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # ADR-0018 P1-b (M1 + review F1/F3): scan-at-issuance on BOTH echoed
    # strings (content and title) — same policy and per-item notes as
    # /search.
    out = []
    for r in results:
        scan = mgr.scan_issuance_item(
            r.memory.effective_content(),
            title=r.memory.auto_title(),
            context=f"api:/recall/agent:{r.memory.id}",
        )
        if scan.refused:
            continue
        item = {
            "id": r.memory.id,
            "title": scan.title,
            "content": scan.content,
            "tags": r.memory.tags,
            "created_at": r.memory.created_at.isoformat(),
            "redactions": scan.redactions,
        }
        if scan.redactions:
            item["redacted_patterns"] = scan.redacted_patterns
        out.append(item)
    return out


# ── Pipeline endpoints (M4) ────────────────────────────────────────────────────


@app.post("/process")
async def trigger_process(
    project: str | None = None,
    agent: str | None = None,
    limit: int = Query(default=100, le=500),
) -> dict[str, Any]:
    """Trigger end-to-end pipeline: cluster → synthesize → quality_gate → publish."""
    mgr = get_manager()
    summary = mgr.run_pipeline(project=project, agent=agent, limit=limit)
    return {"status": "ok", **summary}


@app.post("/reindex")
async def reindex_vectors(batch_size: int = Query(default=100, le=1000)) -> dict[str, Any]:
    """Rebuild the vector index for all published memories.

    Re-embeds every published memory and upserts into the vector store.
    Use after enabling embeddings or switching embedding models.
    """
    mgr = get_manager()
    result = mgr.rebuild_vector_index(batch_size=batch_size)
    return {"status": "ok", **result}


@app.post("/synthesize")
async def trigger_synthesize(cluster_id: str) -> dict[str, Any]:
    """Trigger LLM synthesis for a cluster."""
    mgr = get_manager()
    result = mgr.synthesize(cluster_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Cluster {cluster_id} not found or empty")
    return {
        "status": "ok",
        "draft_id": result.draft_id,
        "cluster_id": result.cluster_id,
        "source_coverage": result.source_coverage,
        "model_used": result.model_used,
    }


@app.post("/publish/{memory_id}")
async def publish_memory_endpoint(
    memory_id: str,
    skip_quality_check: bool = Query(default=False),
) -> dict[str, Any]:
    """Publish a memory to the vector index.

    When ``skip_quality_check=true``, bypasses the processed-status
    requirement so memories can be published directly from ``raw``
    status without an LLM pipeline. This enables search to work
    immediately in deployments without a configured LLM backend.

    ADR-0019 Phase A danger gate: publication ALWAYS passes the
    fail-closed danger-detector gate over the served projection and the
    title (prompt-injection patterns + high-confidence secrets).
    ``skip_quality_check`` does NOT exempt it — a positive signal or a
    scanner error refuses the publication (the record stays stored,
    zero-loss, and invisible); the endpoint then returns 400.
    """
    mgr = get_manager()
    result = mgr.publish(memory_id, skip_quality_check=skip_quality_check)
    if not result.published:
        raise HTTPException(
            status_code=400,
            detail=f"Publish failed for {memory_id} (status={result.previous_status})",
        )
    return {
        "status": "published",
        "memory_id": result.memory_id,
        "vector_indexed": result.vector_indexed,
    }


@app.post("/memories/{memory_id}/quarantine/release")
async def release_quarantine_endpoint(memory_id: str) -> dict[str, Any]:
    """Manually release a quarantined memory (ADR-0019 §5 terminality).

    Quarantine is terminal by design — this is the ONLY way out. The row
    returns to ``pipeline_state='failed'`` (not refined/pending): a
    quarantined projection requires review, and ``failed`` is the honest
    "needs a new cycle" resting state the daemon retries from with a
    fresh retry budget. 404 when the row is missing or not quarantined.
    """
    mgr = get_manager()
    if not mgr.release_quarantine(memory_id):
        raise HTTPException(
            status_code=404,
            detail=f"Memory {memory_id} not found or not quarantined",
        )
    return {"status": "released", "memory_id": memory_id, "pipeline_state": "failed"}


# ── DLQ (M5) ─────────────────────────────────────────────────────────────────


@app.get("/dlq")
async def list_dlq(
    task_label: str | None = None,
    ready_only: bool = False,
    limit: int = Query(default=50, le=500),
) -> list[dict[str, Any]]:
    """List Dead-Letter Queue entries."""
    mgr = get_manager()
    return mgr.dlq_list(task_label=task_label, ready_only=ready_only, limit=limit)


@app.post("/dlq/{dlq_id}/retry")
async def retry_dlq(dlq_id: str) -> dict[str, Any]:
    """Increment retry attempt for a DLQ entry."""
    mgr = get_manager()
    result = mgr.dlq_retry(dlq_id)
    return {"status": "retry_scheduled", "entry": result}


@app.delete("/dlq/{dlq_id}")
async def discard_dlq(dlq_id: str) -> dict[str, str]:
    """Permanently discard a DLQ entry."""
    mgr = get_manager()
    ok = mgr.dlq_discard(dlq_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"DLQ entry {dlq_id} not found")
    return {"status": "discarded", "dlq_id": dlq_id}


# ── Context Filter (M10) ─────────────────────────────────────────────────────


@app.post("/filter/{memory_id}")
async def apply_filter(memory_id: str, data: FilterRequest) -> dict[str, Any]:
    """Run the 5-stage context filter on a memory's raw_content.

    M1 (final review): issuance-gated twin — only `published`/`processed`
    memories are filterable into context (raw/archived refuse fail-closed),
    an optional caller `project` scope fails closed on mismatch, and the
    echoed `clean_content` is secret-scanned (refuse mode returns 403 with
    no content).
    """
    mgr = get_manager()
    result = mgr.issue_context_filter(
        memory_id,
        profile=data.profile,
        budget=data.budget,
        project=data.project,
        channel="api:/filter",
    )
    if result["status"] == "error":
        status_by_reason = {
            "not_found": 404,
            "no_content": 422,
            "status_gate": 422,
            "project_scope": 403,
            "refused": 403,
        }
        code = status_by_reason.get(str(result.get("reason")), 404)
        raise HTTPException(status_code=code, detail=result["error"])
    return result


# ── Tags aggregate (T-TAGS) ─────────────────────────────────────────────────


class TagCount(BaseModel):
    tag: str
    count: int


class TagsRenameRequest(BaseModel):
    """Request body for POST /tags/rename — mirrors ``mnemos_tags_rename``."""

    from_prefix: str
    to_prefix: str
    subtypes: list[str] | None = None
    dry_run: bool = True
    project: str | None = None
    agent: str | None = None
    invalid_subtypes_to_legacy: bool = False


@app.get("/tags", response_model=list[TagCount])
async def list_tags() -> list[TagCount]:
    """Return all tags with their memory counts, sorted by count descending."""
    mgr = get_manager()
    raw: dict[str, int] = mgr.list_tags()
    # Sort explicitly here rather than relying on the storage-layer ORDER BY
    # surviving the dict round-trip / cache: count descending, then tag
    # ascending as a stable, deterministic tie-breaker.
    ordered = sorted(raw.items(), key=lambda kv: (-kv[1], kv[0]))
    return [TagCount(tag=t, count=c) for t, c in ordered]


@app.post("/tags/rename")
async def rename_tags(req: TagsRenameRequest) -> dict[str, Any]:
    """Bulk rename tags matching ``from_prefix:<subtype>`` → ``to_prefix:<subtype>``.

    Mirrors the ``mnemos_tags_rename`` MCP tool and the ``mnemos tags rename``
    CLI command. Safe: uses ``update_fields`` (plain UPDATE) so the FTS5
    external-content index stays consistent. ``dry_run=true`` by default —
    nothing is written unless the caller explicitly sets ``dry_run=false``.
    """
    _track_http_call()
    mgr = get_manager()
    return mgr.tags_rename(
        from_prefix=req.from_prefix,
        to_prefix=req.to_prefix,
        subtypes=req.subtypes,
        dry_run=req.dry_run,
        project=req.project,
        agent=req.agent,
        invalid_subtypes_to_legacy=req.invalid_subtypes_to_legacy,
    )


# ── Traces (M6) ────────────────────────────────────────────────────────────────


@app.get("/traces")
async def list_traces(
    task_label: str | None = None,
    limit: int = Query(default=50, le=500),
) -> list[dict[str, Any]]:
    """Return pipeline trace records."""
    mgr = get_manager()
    rows = mgr.sqlite.list_traces(task_label=task_label, limit=limit)
    return [r.model_dump(mode="json") for r in rows]


# ── Path-scoped rules ingest (M8) ────────────────────────────────────────────


@app.post("/rules/ingest")
async def ingest_rules(data: RuleIngestRequest) -> dict[str, Any]:
    """Scan a directory for `*.instructions.md` files and ingest them as published memories."""
    mgr = get_manager()
    results = mgr.ingest_path_scoped_rules(
        data.rules_dir,
        project=data.project,
        agent=data.agent,
        pattern=data.pattern,
    )
    return {"status": "ok", "processed": len(results), "results": results}


@app.delete("/rules/ingest")
async def remove_rule(data: RuleRemoveRequest) -> dict[str, Any]:
    """Remove the Memory associated with a rule file."""
    mgr = get_manager()
    result = mgr.remove_path_scoped_rule(data.file_path)
    if not result["removed"]:
        raise HTTPException(status_code=404, detail=f"Rule for {data.file_path} not found")
    return {"status": "removed", **result}


# ── Auto-collect tracker (HTTP-local, mirrors MCP _checkpoint_tracker) ──────────

_auto_collect_tracker = {"calls_since_save": 0, "last_save_ts": 0.0}
_auto_collect_state = {
    "enabled": os.environ.get("VESMARO_AUTO_COLLECT", "").lower() in ("true", "1", "yes", "on"),
}
_auto_collect_lock = threading.Lock()


def _track_http_call(is_save: bool = False) -> None:
    """Track HTTP memory-work calls for the auto-collect signal vector."""
    with _auto_collect_lock:
        if is_save:
            _auto_collect_tracker["calls_since_save"] = 0
            _auto_collect_tracker["last_save_ts"] = time.monotonic()
        else:
            _auto_collect_tracker["calls_since_save"] += 1


def _http_remind_calls() -> int:
    return 6 if _auto_collect_state["enabled"] else 12


def _http_remind_secs() -> int:
    return 480 if _auto_collect_state["enabled"] else 900


# ── Session context (save/recall) ──────────────────────────────────────────────


class SaveContextRequest(BaseModel):
    """Request body for POST /context/save — mirrors ``mnemos_save_context``.

    Fields accept either a string or a list of strings. When a list is
    provided, items are joined with newlines to form the markdown section
    body. This matches the Hermes plugin schema which declares these as
    ``type: array, items: {type: string}`` and the MCP tool which accepts
    free-form strings (bullet lists).

    mnemos #251 D0 — optional ``agent``/``session`` are the validated
    identity channel (agent defaults to ``"user"``, today's behaviour);
    validation, session→agent binding and dedup live in
    ``MemoryManager.save_checkpoint`` (single authority).
    """

    project: str
    goals: str | list[str] | None = None
    completed: str | list[str] | None = None
    in_progress: str | list[str] | None = None
    decisions: str | list[str] | None = None
    context: str | list[str] | None = None
    agent: str | None = None
    session: str | None = None
    # Ф2 (epic #308, ADR-0027 Phase 2) — the write-side task switcher:
    # the bare slug, minted as the ``task:<slug>`` tag on the checkpoint
    # at the single manager save boundary (at most one task per record).
    # Normalized / fail-loud there (the #407 canon); ValueError → 400.
    task: str | None = None
    # vesmaro-canon v1.0.0 (ADR-0003 obligation 4) — the per-call
    # override of the server-configured checkpoint body language
    # (``mnemos.checkpoint_language``); validated at the manager
    # boundary (canon enum, ValueError → 400).
    language: Literal["ru", "en"] | None = None


class RecallContextRequest(BaseModel):
    """Request body for POST /context/recall — mirrors ``mnemos_recall_context``."""

    project: str
    query: str | None = None
    # Ф2 (epic #308): the read-side task switcher — byte-identical to the
    # checkpoint tag filter plus ``task:<slug>`` (the F1 arm-C surface);
    # normalized / fail-loud at the manager boundary (ValueError → 400).
    task: str | None = None
    limit: int = 5


class AssembleContextRequest(BaseModel):
    """Request body for POST /context/assemble — mirrors ``mnemos_assemble_context``.

    ADR-0017 D1 provider contract. ``mode`` carries both axes on one
    parameter: delivery (``sync`` default / ``async`` = store + handle) and
    contentType (``code`` / ``prose`` filter recall candidates by the
    content type captured at ingest). Boundary validation of the values
    lives in the manager (single authority); this endpoint maps a
    ``ValueError`` to HTTP 422.
    """

    session: str
    project: str
    file: str | None = None
    budget: int = Field(default=2048, ge=1, le=1_000_000)
    mode: str = "sync"
    expand_ccr: bool = False
    async_handle: str | None = None
    # A2 review F2 — pairs with session as the issuer context for the
    # strict-mode CCR expansion gate.
    agent: str | None = None


@app.post("/context/save", status_code=201)
async def save_context(req: SaveContextRequest) -> dict[str, Any]:
    """Save a session checkpoint memory tagged ``mnemos:checkpoint``.

    Mirrors the ``mnemos_save_context`` MCP tool: identity validation,
    session→agent binding (mismatch → 409), issuer-keyed dedup and the
    trivial-reject all live in ``MemoryManager.save_checkpoint``; this
    endpoint only maps errors to HTTP codes (ValueError → 400).
    """
    mgr = get_manager()
    fields: dict[str, str | None] = {}
    for field in CHECKPOINT_FIELDS:
        val = getattr(req, field)
        # Accept both str and list[str] — join lists with newlines.
        fields[field] = "\n".join(val) if isinstance(val, list) else val
    try:
        memory, duplicate = mgr.save_checkpoint(
            fields,
            project=req.project,
            agent=req.agent,
            session=req.session,
            task=req.task,
            memory_type=MemoryType.SESSION_CONTEXT,
            language=req.language,
        )
    except SessionAgentMismatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _track_http_call(is_save=True)
    # Response shape is additive: "duplicate" joins the legacy keys.
    return {
        "status": "saved",
        "id": str(memory.id),
        "title": memory.auto_title(),
        "duplicate": duplicate,
    }


@app.post("/context/recall")
async def recall_context(req: RecallContextRequest) -> dict[str, Any]:
    """Recall the most recent checkpoint memories for a project.

    Mirrors the ``mnemos_recall_context`` MCP tool: the project slug is
    normalized at the manager's query boundary (mnemos #400) and an
    unsalvageable slug raises ``ValueError`` there — mapped to HTTP 400
    here, mirroring the save twin's mapping above.
    """
    _track_http_call()
    mgr = get_manager()
    try:
        memories = mgr.recall_context(
            project=req.project, query=req.query, task=req.task, limit=req.limit
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not memories:
        return {
            "project": req.project,
            "checkpoints": [],
            "message": "No context found. Start by saving context with POST /context/save.",
        }
    # ADR-0018 P1-b review (F2a): channel symmetry — the MCP twin
    # mnemos_recall_context scans at issuance, so this endpoint does too
    # (both echoed strings: content and title; refuse mode drops the
    # checkpoint, logged with the memory id).
    checkpoints = []
    for m in memories:
        scan = mgr.scan_issuance_item(
            m.effective_content(),
            title=m.auto_title(),
            context=f"api:/context/recall:{m.id}",
        )
        if scan.refused:
            continue
        item = {
            "id": m.id,
            "title": scan.title,
            "content": scan.content,
            "tags": m.tags,
            "created_at": m.created_at.isoformat(),
            "redactions": scan.redactions,
        }
        if scan.redactions:
            item["redacted_patterns"] = scan.redacted_patterns
        checkpoints.append(item)
    return {"project": req.project, "checkpoints": checkpoints}


@app.post("/context/assemble")
async def assemble_context(req: AssembleContextRequest) -> dict[str, Any]:
    """Assemble the model-facing context block (ADR-0017 D1, mnemos #125).

    Mirrors the ``mnemos_assemble_context`` MCP tool over the same manager
    path: fixed pipeline (recall → optional CCR expansion → filter →
    MANDATORY secret scan → CacheAligner → token budget), provenance on
    every injected block, per-block redaction counts and token stats.
    ``mode='async'`` returns a handle envelope; pass ``async_handle`` on a
    later call to fetch the stored result. ``agent`` (A2 review F2) pairs
    with ``session`` as the issuer context for the strict-mode CCR
    expansion gate (``ccr.validate_markers``): without it a strict
    deployment skips expansion of issuer-stamped markers (the marker
    stays; legacy NULL-issuer rows still expand).
    """
    _track_http_call()
    mgr = get_manager()
    try:
        return mgr.assemble_context(
            session=req.session,
            project=req.project,
            file=req.file,
            budget=req.budget,
            mode=req.mode,
            expand_ccr=req.expand_ccr,
            async_handle=req.async_handle,
            agent=req.agent,
        )
    except ValueError as exc:
        # Boundary validation (session/project/mode/budget/async_handle).
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class ContextRewriteRequest(BaseModel):
    """Request body for POST /context/rewrite — mirrors ``mnemos_context_rewrite``."""

    content: str
    project: str
    agent: str
    session: str | None = None
    supersedes: str | None = None
    diff: str | None = None
    include_marker: bool = False


@app.post("/context/rewrite")
async def context_rewrite(req: ContextRewriteRequest) -> dict[str, Any]:
    """Handle one ``on_context_rewrite`` lifecycle event (ADR-0018, #125 W2).

    Mirrors the ``mnemos_context_rewrite`` MCP tool over the same manager
    path: the original of the replaced context block is stored to LTM via
    the normal knowledge pipeline (raw → published gating, write-path
    secret scan), idempotent by content-addressed event key, version-less
    (replacement lineage is an optional ``supersedes`` edge). HTTP 200 for
    both ``stored`` and ``deduplicated`` receipts — the event is
    idempotent, so re-delivery is not a new resource (201 would lie).
    """
    _track_http_call()
    mgr = get_manager()
    try:
        return mgr.context_rewrite(
            content=req.content,
            project=req.project,
            agent=req.agent,
            session=req.session,
            supersedes=req.supersedes,
            diff=req.diff,
            include_marker=req.include_marker,
        )
    except ContextRewriteRateLimitError as exc:
        # W2 review F1: backpressure maps to 429, not 500/422 — the
        # harness can distinguish "retry later" from "fix the payload".
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        # Boundary validation + size caps + tag-contract violations
        # (strict mode) + supersedes not found in this project.
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ── Lifecycle hooks (ADR-0017 D1 / ADR-0018, #125 Wave 3) ─────────────────────


class HooksRequest(BaseModel):
    """Request body for POST /hooks/{action} — mirrors ``mnemos_hooks``.

    One shared body for the three actions (they share the mandatory
    session/project/agent identity spine); per-action fields are
    validated by the hooks module, so an irrelevant field for the
    requested action is simply ignored. ``ValueError`` from the hooks
    boundary maps to HTTP 422.
    """

    session: str
    project: str
    agent: str
    # pre_llm_call
    context_hint: str | None = None
    file: str | None = None
    budget: int = Field(default=2048, ge=1, le=1_000_000)
    # pre_llm_call (ADR-0027 Phase 0, epic #308) — the bare task slug;
    # narrows recall to rows carrying task:<slug> (intersection
    # doctrine, tail-only). Ignored by the other actions.
    task: str | None = None
    # on_session_start
    limit: int = Field(default=5, ge=1, le=100)
    # post_tool_call
    tool_name: str | None = None
    output_text: str | None = None
    auto_compress: bool | None = None
    profile: str | None = None
    # pre_llm_call / on_session_start (mnemos #254 awareness composition)
    include_awareness: bool = False


@app.post("/hooks/{action}")
async def run_hook(action: str, req: HooksRequest) -> dict[str, Any]:
    """Run one lifecycle hook (ADR-0017 D1 / ADR-0018, #125 Wave 3).

    Mirrors the ``mnemos_hooks`` MCP tool over the same manager path via
    the shared ``dispatch_hook`` router: ``pre_llm_call`` (assemble the
    pre-model-call injection block, sync), ``on_session_start`` (recall
    recent checkpoints, scanned at issuance on this channel),
    ``post_tool_call`` (autocompression entry point — identity mandate
    A2 N2: the compress call always threads the caller's agent+session
    onto the cache row). Unknown action → 404 (resource-shaped path
    segment); hook boundary violations → 422.
    """
    _track_http_call()
    mgr = get_manager()
    if action not in HOOK_ACTIONS:
        valid = ", ".join(HOOK_ACTIONS)
        raise HTTPException(status_code=404, detail=f"unknown hook action; valid: {valid}")
    try:
        return dispatch_hook(
            mgr,
            action=action,
            session=req.session,
            project=req.project,
            agent=req.agent,
            context_hint=req.context_hint,
            file=req.file,
            budget=req.budget,
            task=req.task,
            limit=req.limit,
            tool_name=req.tool_name,
            output_text=req.output_text,
            auto_compress=req.auto_compress,
            profile=req.profile,
            include_awareness=req.include_awareness,
        )
    except ValueError as exc:
        # Per-hook boundary validation (identity, per-action args).
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ── Reversible content compression (CCR) ───────────────────────────────────────


class CompressRequest(BaseModel):
    """Request body for POST /compress — mirrors ``mnemos_compress``."""

    text: str
    profile: str | None = None
    project: str = ""
    # A2 (ArchCom 2026-08-27) — issuer ledger: caller identity recorded
    # on the cache row for strict marker provenance.
    agent: str | None = None
    session: str | None = None


class RetrieveRequest(BaseModel):
    """Request body for POST /retrieve — mirrors ``mnemos_retrieve``."""

    hash: str
    query: str | None = None
    snippet_count: int | None = None
    # ADR-0018 P1-a: optional project scopes the cache lookup — a hash
    # cached under another project is reported as not found.
    project: str | None = None
    # A2 (ArchCom 2026-08-27) — strict marker validation: marker metadata
    # (N from the marker) + caller identity (agent/session). Any of these
    # present makes the request marker-shaped; with validation on, a
    # failed check refuses the issuance (no content).
    validate_marker: bool | None = None
    original_chars: int | None = None
    agent: str | None = None
    session: str | None = None


@app.post("/compress")
async def compress_content(req: CompressRequest) -> dict[str, Any]:
    """Compress ``text`` via CCR and cache the original.

    Mirrors the ``mnemos_compress`` MCP tool. Returns the CCR result dict
    (compressed text, hash, sizes, reduction, marker, …). Optional
    ``agent``/``session`` record the caller as the cache entry issuer
    (A2 marker provenance).
    """
    _track_http_call()
    mgr = get_manager()
    return mgr.compress_content(
        req.text,
        profile=req.profile,
        project=req.project,
        agent=req.agent,
        session=req.session,
    )


@app.post("/retrieve")
async def retrieve_content(req: RetrieveRequest) -> dict[str, Any]:
    """Retrieve a CCR-cached original (or FTS5 snippets when ``query`` is set).

    Mirrors the ``mnemos_retrieve`` MCP tool. Issued content is scanned for
    secrets (ADR-0018 P0): matched spans are redacted in the response
    (``redactions`` counts them; ``redacted_patterns`` gives per-pattern
    counts); the stored original is never mutated. An optional ``project``
    scopes the lookup to that project's entries (ADR-0018 P1-a). A2 strict
    marker validation: ``validate_marker`` (or the ``ccr.validate_markers``
    knob) gates marker-shaped requests (``original_chars``/``agent``/
    ``session`` present) on existence + integrity + issuer provenance —
    a failed check returns ``refused=True`` with no content.
    """
    _track_http_call()
    mgr = get_manager()
    return mgr.retrieve_content(
        req.hash,
        query=req.query,
        snippet_count=req.snippet_count,
        project=req.project,
        validate_marker=req.validate_marker,
        original_chars=req.original_chars,
        agent=req.agent,
        session=req.session,
    )


# ── Auto-collect signal vector ─────────────────────────────────────────────────


@app.get("/auto-collect")
async def auto_collect_status() -> dict[str, Any]:
    """Compaction signal vector — mirrors ``mnemos_auto_collect_status``.

    Returns the in-process call counter / elapsed-time signals plus
    client-populated heuristic slots. The ``recommendation`` field is
    ``"save_checkpoint"`` when either signal exceeds its threshold, else
    ``"ok"``.
    """
    with _auto_collect_lock:
        calls = _auto_collect_tracker["calls_since_save"]
        elapsed = (
            time.monotonic() - _auto_collect_tracker["last_save_ts"]
            if _auto_collect_tracker["last_save_ts"]
            else 0.0
        )
    call_threshold = _http_remind_calls()
    secs_threshold = _http_remind_secs()
    call_triggered = calls >= call_threshold
    elapsed_triggered = elapsed > secs_threshold and calls > 0
    return {
        "auto_collect_enabled": _auto_collect_state["enabled"],
        "signals": {
            "call_counter": {
                "calls_since_save": calls,
                "threshold": call_threshold,
                "triggered": call_triggered,
            },
            "elapsed_secs": {
                "value": int(elapsed),
                "threshold": secs_threshold,
                "triggered": elapsed_triggered,
            },
            "context_size_heuristic": {"value": None, "note": "populated by client"},
            "summary_marker_detected": {"value": None, "note": "populated by client"},
            "reference_drop_heuristic": {"value": None, "note": "populated by client"},
        },
        "recommendation": ("save_checkpoint" if (call_triggered or elapsed_triggered) else "ok"),
        "next_reminder_in_calls": max(0, call_threshold - calls),
    }


# ── URL ingest ─────────────────────────────────────────────────────────────────


class IngestUrlRequest(BaseModel):
    """Request body for POST /ingest-url — mirrors ``mnemos_ingest_url``."""

    url: str
    tags: list[str]


@app.post("/ingest-url", status_code=201)
async def ingest_url(req: IngestUrlRequest) -> dict[str, Any]:
    """Fetch a web page, extract main text, and save it as a RAW memory.

    Mirrors the ``mnemos_ingest_url`` MCP tool. Credentials embedded in the
    URL are stripped before storage (OWASP A02). Tags are validated through
    the project's tag contract.
    """
    _track_http_call()
    mgr = get_manager()
    settings = mgr.settings
    url_clean = re.sub(r"(https?://)([^@]*@)", r"\1", req.url)
    tags = validate_tag_contract(req.tags, strict=settings.mnemos.strict_tag_contract)
    project = next((t[len("project:") :] for t in tags if t.startswith("project:")), "")
    # P1 repair (review round): the len("agent") slice dropped the ':'
    # and stored ':a' in the denormalised column for tag agent:a —
    # agent_recall then missed the row. REST twin of the MCP slice.
    agent = next((t[len("agent:") :] for t in tags if t.startswith("agent:")), "")
    memory = mgr.ingest_url(url_clean, tags=tags, project=project, agent=agent)
    return {"id": str(memory.id), "title": memory.auto_title(), "url": url_clean}


# ── Document ingest (ADR-0027 Phase 3, epic #308) ─────────────────────────────


class IngestDocumentRequest(BaseModel):
    """Request body for POST /ingest-document — mirrors ``mnemos_ingest_document``.

    ADR-0027 invariant 8: the ingested document is UNTRUSTED CONTENT.
    The chunks are born quarantined (excluded from every issuance path)
    and released per-chunk by the ADR-0019 Phase A danger sweep at
    ingest-completion. A re-ingest of the same ``doc_id`` replaces the
    chunks and bumps the doc-chunk cache version in the same
    transaction (invariant 4).
    """

    text: str
    doc_id: str
    tags: list[str]
    title: str | None = None
    source_url: str | None = None


@app.post("/ingest-document", status_code=201)
async def ingest_document(req: IngestDocumentRequest) -> dict[str, Any]:
    """Ingest a document as chunked, born-quarantined memory rows (Ф3).

    Mirrors the ``mnemos_ingest_document`` MCP tool. Chunks are born
    quarantined (ADR-0027 invariant 8) and swept at completion; a
    re-ingest of the same ``doc_id`` replaces the rows and bumps the
    ccr_cache doc-chunk version in the same transaction (invariant 4).
    ``POST /ingest-url`` keeps its single-row pre-Ф3 semantics — that
    boundary is deliberate (the existing tool is not retroactively
    quarantined).
    """
    _track_http_call()
    mgr = get_manager()
    settings = mgr.settings
    doc_id = req.doc_id.strip()
    if not doc_id:
        raise HTTPException(status_code=422, detail="doc_id must be a non-empty string")
    tags = validate_tag_contract(req.tags, strict=settings.mnemos.strict_tag_contract)
    project = next((t[len("project:") :] for t in tags if t.startswith("project:")), "")
    agent = next((t[len("agent:") :] for t in tags if t.startswith("agent:")), "")
    result = mgr.ingest_document(
        req.text,
        doc_id=doc_id,
        title=req.title,
        tags=tags,
        project=project,
        agent=agent,
        source_url=req.source_url,
    )
    return {
        "doc_id": result.doc_id,
        "chunks_total": result.chunks_total,
        "released": result.released,
        "quarantined": result.quarantined,
        "chunk_ids": list(result.memory_ids),
        "reingest": result.reingest,
        "cache_version": result.cache_version,
        "truncated": result.truncated,
    }


# ── File watcher (M8) ─────────────────────────────────────────────────────────


class WatchStartRequest(BaseModel):
    """Request body for POST /watch/start — mirrors ``mnemos_watch_start``."""

    paths: list[str] = []
    scan: bool = True
    include_rules: bool = False


@app.post("/watch/start")
async def watch_start(req: WatchStartRequest) -> dict[str, Any]:
    """Start the background vault watcher.

    Mirrors the ``mnemos_watch_start`` MCP tool. When ``paths`` is empty the
    current working directory is watched. When ``include_rules`` is true the
    watcher also ingests ``*.instructions.md`` rule files found under the
    watched paths.
    """
    _track_http_call()
    mgr = get_manager()
    paths = req.paths or [str(Path.cwd())]
    mgr.watch_start(paths=paths, scan=req.scan, include_rules=req.include_rules)
    return {
        "status": "started",
        "paths": paths,
        "scan": req.scan,
        "include_rules": req.include_rules,
    }


@app.post("/watch/stop")
async def watch_stop() -> dict[str, str]:
    """Stop the background vault watcher.

    Mirrors the ``mnemos_watch_stop`` MCP tool. Idempotent — returns
    ``{"status": "stopped"}`` whether or not a watcher was running.
    """
    _track_http_call()
    mgr = get_manager()
    mgr.watch_stop()
    return {"status": "stopped"}


@app.get("/watch/status")
async def watch_status() -> dict[str, Any]:
    """Return the current watcher status.

    Mirrors the ``mnemos_watch_status`` MCP tool. Returns ``{"running": bool}``.
    """
    _track_http_call()
    mgr = get_manager()
    return mgr.watch_status()


# ── Export / Import (M17 — backup/restore) ────────────────────────────────────


class ExportRequest(BaseModel):
    """Request body for POST /api/v1/export."""

    format: str = "json"  # json | sqlite
    compress: str = "none"  # none | gzip | zstd
    encrypt: bool = False
    project: str | None = None
    agent: str | None = None
    status: str | None = None
    tags: list[str] | None = None
    since: str | None = None
    until: str | None = None


@app.post("/api/v1/export")
async def api_export(
    req: ExportRequest,
    passphrase: str | None = Header(None, alias="X-Mnemos-Passphrase"),
) -> StreamingResponse:
    """Export memories and stream the resulting file as a download.

    Passphrase for encryption is read from the ``X-Mnemos-Passphrase``
    header (handled below) — kept out of the request body so it is not
    logged as a request parameter.
    """
    import io
    import json as _json

    from vesmaro.cli.export import (
        CompressMode,
        ExportFilter,
        ExportFormat,
        build_json_payload,
    )
    from vesmaro.models import MemoryStatus

    mgr = get_manager()

    try:
        fmt = ExportFormat(req.format)
        comp = CompressMode(req.compress)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    status_enum: MemoryStatus | None = None
    if req.status:
        try:
            status_enum = MemoryStatus(req.status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid status: {req.status}") from exc

    since_dt = _parse_iso(req.since)
    until_dt = _parse_iso(req.until)
    filt = ExportFilter(
        project=req.project,
        agent=req.agent,
        status=status_enum,
        tags=req.tags,
        since=since_dt,
        until=until_dt,
    )

    if fmt == ExportFormat.JSON:
        payload = build_json_payload(mgr, filt)
        raw = _json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        media = "application/json"
        suffix = "json"
    else:
        from vesmaro.cli.export import _build_sqlite_snapshot

        raw = _build_sqlite_snapshot(mgr)
        media = "application/gzip"
        suffix = "tar.gz"

    # Compression
    from vesmaro.cli.export import _compress

    payload_bytes, _warnings = _compress(raw, comp)
    if comp == CompressMode.GZIP and fmt == ExportFormat.JSON:
        media = "application/gzip"
        suffix = "json.gz"

    # Encryption
    if req.encrypt:
        if not passphrase:
            raise HTTPException(
                status_code=400,
                detail="Encryption requested but X-Mnemos-Passphrase header is missing.",
            )
        from vesmaro.cli.export import _encrypt

        payload_bytes = _encrypt(payload_bytes, passphrase)
        media = "application/octet-stream"
        suffix = "enc"

    filename = f"mnemos-export.{suffix}"
    return StreamingResponse(
        io.BytesIO(payload_bytes),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 date string, returning None on None/empty."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid ISO date: {value}") from exc


@app.post("/api/v1/import")
async def api_import(
    file: UploadFile = File(...),  # noqa: B008 — FastAPI idiom for multipart upload
    mode: str = Query(default="merge"),
    overwrite: bool = Query(default=False),
    confirm: bool = Query(default=False),
    dry_run: bool = Query(default=False),
    passphrase: str | None = Header(None, alias="X-Mnemos-Passphrase"),
) -> dict[str, Any]:
    """Import an export file uploaded as multipart form data.

    Returns a summary dict with ``imported``, ``skipped``, ``updated``,
    ``errors``, and ``warnings``. Encryption passphrase is read from the
    ``X-Mnemos-Passphrase`` header (kept out of the body / logs).
    """
    from vesmaro.cli.import_ import run_import

    mgr = get_manager()

    # Stream the upload into a temp file so run_import can read it.
    import tempfile

    with tempfile.NamedTemporaryFile(delete=False, suffix=f"-{file.filename or 'import'}") as tf:
        tf.write(await file.read())
        tmp_path = Path(tf.name)
    try:
        result = run_import(
            mgr,
            tmp_path,
            mode=mode,
            overwrite=overwrite,
            confirm=confirm,
            dry_run=dry_run,
            passphrase=passphrase,
        )
        return result.summary()
    finally:
        tmp_path.unlink(missing_ok=True)


# ── A2A Sessions API (M16) ──────────────────────────────────────────────────
# Mounted under ``/v1`` so the existing ``/memories``, ``/recall/*`` and
# ``/search`` routes are untouched.  The router reads its
# ``SessionStore`` from ``app.state.sessions_store`` (set in ``lifespan``)
# and falls back to a default ``load_settings()`` store when called
# outside the standard app (e.g. in a unit test that builds its own
# TestClient).
app.include_router(sessions_router, prefix="/v1")

# ── Auth API (T-AUTH, ADR-0014) ───────────────────────────────────────────────
app.include_router(auth_router)

# ── Federation API (Phase 2 mediated pull, contract §3.2) ────────────────────
# The federation router registers ``POST /api/v1/federation/pull`` — the
# B-side endpoint for the A→B mediated pull flow. It is a thin adapter over
# :func:`vesmaro.federation_server.handle_pull`; auth is per-peer bearer
# (ADR-0016). The router reads its MemoryManager / Settings / AccessLog
# from ``app.state`` (set by lifespan) or falls back to the module singletons.
app.include_router(federation_router)

# Apply CORS middleware based on current settings.
# Middleware must be registered before the first request (i.e., here at module
# load time).  Starlette raises RuntimeError if add_middleware is called after
# the app has started, so this CANNOT be moved into ``lifespan``.  Tests that
# need custom CORS settings must call _setup_cors(test_app, settings) on their
# own test_app before TestClient.
#
# MERGE CONTRACT with feat/api-auth (AuthMiddleware): Starlette applies
# middleware in REVERSE order of registration (last added = outermost).  CORS
# MUST be the outermost layer so that pre-flight ``OPTIONS`` requests are
# answered before AuthMiddleware can reject them as unauthenticated.
# ``app.add_middleware(AuthMiddleware)`` is registered earlier (just after app
# construction); this call stays at the bottom so CORS wraps it as outermost.
_setup_cors(app, load_settings())
