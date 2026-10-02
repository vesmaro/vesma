"""MCP server for Vesma — exposes vesma_* memory tools to Copilot/LLM agents.

Tools: vesma_add (enforces Vesma TagContract), vesma_search, vesma_recall_context,
vesma_agent_recall (M3), vesma_auto_collect_status (per-signal compaction
vector, M7), and others. Auto-collect driven by VESMA_AUTO_COLLECT env var.

MCP SDK 2.x port (#185): the 1.x runtime-decorator API
(``@server.list_tools()`` / ``@server.call_tool()``) was removed in SDK 2.0
(``Server`` no longer exposes those attributes — pip consumers resolving
``mcp>=2`` crashed with ``AttributeError`` and the transport died silently).
The 2.x low-level ``Server`` registers handlers via constructor kwargs
(``on_list_tools`` / ``on_call_tool``); this module keeps the public handler
callables (``list_tools`` / ``call_tool``) importable with their pre-port
signatures for the test suite and thin adapter wrappers that translate
between the handler contract and the SDK request/response models.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

from vesmaro import __version__
from vesmaro.config import load_settings
from vesmaro.context_rewrite import ContextRewriteRateLimitError
from vesmaro.heartbeat import native_heartbeat_tail
from vesmaro.hooks import HOOK_ACTIONS, dispatch_hook
from vesmaro.models import (
    CHECKPOINT_FIELDS,
    AgentRecallQuery,
    MemoryCreate,
    MemorySource,
    MemoryStatus,
    MemoryType,
    TagContractError,
    validate_tag_contract,
)

logger = logging.getLogger(__name__)
_manager: Any = None  # MemoryManager — lazy init to avoid import-time side-effects

# ── Auto-collect mode ──────────────────────────────────────────────────────────
_auto_collect_state = {
    "enabled": os.environ.get("VESMA_AUTO_COLLECT", "").lower() in ("true", "1", "yes", "on"),
}

# ── Tool-name contract (6.0.0) ────────────────────────────────────────────────
# The server registers and accepts the canonical ``vesma_*`` names ONLY.
# 6.0.0 removed the legacy ``mnemos_*`` spellings and the VESMA_MCP_BRAND /
# VESMARO_MCP_BRAND manifest brand switch along with them.


# ── Auto-checkpoint tracking ───────────────────────────────────────────────────
_checkpoint_tracker = {
    "calls_since_save": 0,
    "last_save_ts": 0.0,
}


def _remind_calls() -> int:
    return 6 if _auto_collect_state["enabled"] else 12


def _remind_secs() -> int:
    return 480 if _auto_collect_state["enabled"] else 900


def get_manager() -> Any:
    global _manager
    if _manager is None:
        from vesmaro.manager import MemoryManager

        _manager = MemoryManager(load_settings())
    return _manager


def _detect_project() -> str:
    """Auto-detect project name from current working directory.

    vesma #400 — normalize at the entry point: a PascalCase folder name
    (``Project-Umbra``) must not become a different store key than the
    same project saved with a typed slug. Same normalization as the
    save/query boundaries (:func:`vesmaro.models.normalize_project_slug`).
    """
    from vesmaro.models import normalize_project_slug

    return normalize_project_slug(Path(os.getcwd()).name)


def _emit_codegraph_hint(mgr: Any, args: dict[str, Any]) -> None:
    """PG-0.5 native auto-index hint — the ONE MCP cut-in point.

    Fires on every dispatched tool call (the explicit ``project`` /
    ``project_id`` argument when the tool carries one, the cwd slug
    otherwise) and hands it to ``MemoryManager.codegraph_activity_hint``:
    a non-blocking queue put that may auto-register + background-index
    the project (ADR-0032 wave PG-0.5, owner directive 2026-09-29 —
    zero explicit calls, zero instructions). The hint NEVER raises and
    never blocks the tool call; REST is deliberately not a hint surface
    (no cwd to gate a registration on).
    """
    try:
        project = args.get("project") or args.get("project_id")
        if not isinstance(project, str) or not project.strip():
            project = _detect_project()
        agent = args.get("agent")
        if not isinstance(agent, str) or not agent.strip():
            agent = None
        session = args.get("session")
        if not isinstance(session, str) or not session.strip():
            session = None
        mgr.codegraph_activity_hint(project.strip(), cwd=os.getcwd(), agent=agent, session=session)
    except Exception:
        logger.debug("mcp: codegraph auto-index hint skipped", exc_info=True)


def _checkpoint_reminder() -> str | None:
    """Return a reminder string if it's time to save a checkpoint, else None."""
    calls = _checkpoint_tracker["calls_since_save"]
    elapsed = (
        time.monotonic() - _checkpoint_tracker["last_save_ts"]
        if _checkpoint_tracker["last_save_ts"]
        else 0.0
    )
    if calls >= _remind_calls() or (elapsed > _remind_secs() and calls > 0):
        return (
            f"\n\n⚠️ [vesma] {calls} tool calls since last checkpoint "
            f"({int(elapsed)}s ago). Consider calling vesma_save_context "
            f"to preserve your current progress."
        )
    return None


# ── #456: one-time server-updated notice ─────────────────────────────────────
#
# A mid-session server upgrade is invisible to the sessions it serves.
# On the FIRST tool dispatch after process start the dispatcher compares
# the store meta ``last_reported_server_version`` with the running
# ``__version__``: a difference appends ONE non-blocking line to that
# response and re-stamps the meta ONLY after the notice actually rode a
# response (#464 P3-1 — a failed dispatch keeps the notice pending for
# the next one instead of burning it). Off-resilient by contract — any
# store error skips the notice silently after one warning log, and the
# store check runs at most ONCE per process whatever happens (no
# per-dispatch store reads, no retry storm against a broken store).

SERVER_VERSION_META_KEY = "last_reported_server_version"

_server_update_state: dict[str, Any] = {"checked": False, "pending": None}


def _reset_server_update_state() -> None:
    """Test seam: re-arm the once-per-process version check."""
    _server_update_state["checked"] = False
    _server_update_state["pending"] = None


def _server_update_hint(mgr: Any) -> str | None:
    """The one-line upgrade notice, at most once per process (#456).

    ``None`` when: already checked this process (and nothing is pending
    delivery), the meta matches the running version, or the store is
    unavailable (fail-open — the notice is a courtesy, never a failure
    mode). A MISSING or non-string meta is a first contact: the
    baseline is written silently, nothing to compare yet.

    The meta re-stamp does NOT happen here (issue #464 P3-1): the
    notice is only RETURNED — if the dispatch that should carry it
    fails, it stays pending and the next dispatch returns it again (the
    notice is never burned unapplied). :func:`_commit_server_update_hint`
    stamps the meta only after the hint actually rode a response."""
    pending = _server_update_state["pending"]
    if pending is not None:
        return pending  # undelivered notice — retry until it lands
    if _server_update_state["checked"]:
        return None
    _server_update_state["checked"] = True  # one store check per process, whatever happens
    try:
        meta = mgr.sqlite.get_meta(SERVER_VERSION_META_KEY)
        if isinstance(meta, str) and meta == __version__:
            return None
        if isinstance(meta, str):
            hint = f"\n\nvesma server updated: {meta} → {__version__}"
            _server_update_state["pending"] = hint
            return hint
        mgr.sqlite.set_meta(SERVER_VERSION_META_KEY, __version__)
        return None
    except Exception:
        logger.warning("server-update notice skipped (store unavailable)", exc_info=True)
        return None


def _commit_server_update_hint(mgr: Any) -> None:
    """Re-stamp ``last_reported_server_version`` AFTER delivery (#464
    P3-1): the pending notice is dropped first (it rode a response —
    the at-most-once contract holds even if the store write fails), the
    meta write is best-effort bookkeeping (fail-open)."""
    if _server_update_state["pending"] is None:
        return
    _server_update_state["pending"] = None
    try:
        mgr.sqlite.set_meta(SERVER_VERSION_META_KEY, __version__)
    except Exception:
        logger.warning("server-update meta re-stamp skipped (store unavailable)", exc_info=True)


def _track_call(is_save: bool = False) -> None:
    if is_save:
        _checkpoint_tracker["calls_since_save"] = 0
        _checkpoint_tracker["last_save_ts"] = time.monotonic()
    else:
        _checkpoint_tracker["calls_since_save"] += 1


def _auto_collect_instructions(project: str) -> str:
    return (
        "\n\n---\n"
        "## 🔄 Auto-Collect Mode Active\n\n"
        "You MUST follow these rules for the entire session:\n\n"
        "1. **Session start**: You already called vesma_recall_context (good). "
        "Review the context above and continue from where you left off.\n"
        "2. **Save checkpoints**: Call `vesma_save_context` after completing meaningful work, "
        "before switching tasks, or when your context grows large.\n"
        "3. **Store knowledge**: Use `vesma_add` to save any discoveries, patterns, decisions, "
        "architecture insights, gotchas, or reusable knowledge. Tag with "
        f"`project:{project}` and relevant topic tags.\n"
        "4. **Search first**: Before complex work, use `vesma_search` to check if relevant "
        "context was stored in previous sessions.\n"
    )


# ── P1-7 Output token reduction: verbosity steering + effort routing ────────
# Inspired by headroom's output token reduction work. Original implementation.
# These are *hints* injected into tool result framing and passed through to
# the caller — they are not model config changes. New params are optional with
# defaults that preserve the exact pre-P1-7 behaviour (backward compatible).

_VERBOSITY_GUIDANCE: dict[str, str] = {
    "default": "",
    "terse": (
        "\n\n---\n*Output style: terse. Be brief. No preambles, no restated "
        "context, no ceremony. Lead with the result. Omit explanations the "
        "caller already has.*"
    ),
    "minimal": (
        "\n\n---\n*Output style: minimal. Facts only. No prose, no "
        "preambles, no framing. Return the data.*"
    ),
}

_EFFORT_GUIDANCE: dict[str, str] = {
    "low": "\n*Effort: low — routine step, minimal reasoning.*",
    "medium": "",
    "high": "\n*Effort: high — deliberate reasoning, verify before answering.*",
}

# Allowed value sets for validation. Kept in sync with the guidance dicts
# above. Used by _resolve_verbosity / _resolve_effort to detect caller typos
# (e.g. "verbose", "turbo") and fall back gracefully instead of silently
# coercing to an empty hint via a missing dict key.
_VALID_VERBOSITY: frozenset[str] = frozenset(_VERBOSITY_GUIDANCE.keys())
_VALID_EFFORT: frozenset[str] = frozenset(_EFFORT_GUIDANCE.keys())


def _resolve_verbosity(args: dict[str, Any], settings: Any) -> str:
    """Resolve the effective verbosity from args or config default.

    Invalid values (not in ``_VALID_VERBOSITY``) are logged at WARNING and
    fall back to the config default — graceful degradation, never raises.
    This prevents a caller typo (e.g. ``"verbose"``) from silently disabling
    steering via a missing dict key.
    """
    if not settings.output_style.enabled:
        return "default"
    raw = args.get("verbosity")
    if isinstance(raw, str):
        if raw in _VALID_VERBOSITY:
            return raw
        logger.warning(
            "Invalid verbosity %r (valid: %s); falling back to default.",
            raw,
            sorted(_VALID_VERBOSITY),
        )
        return str(settings.output_style.default_verbosity)
    return str(settings.output_style.default_verbosity)


def _resolve_effort(args: dict[str, Any], settings: Any) -> str:
    """Resolve the effective effort hint from args or config default.

    Invalid values (not in ``_VALID_EFFORT``) are logged at WARNING and
    fall back to the config default — graceful degradation, never raises.
    """
    if not settings.output_style.enabled:
        return "medium"
    raw = args.get("effort")
    if isinstance(raw, str):
        if raw in _VALID_EFFORT:
            return raw
        logger.warning(
            "Invalid effort %r (valid: %s); falling back to medium.",
            raw,
            sorted(_VALID_EFFORT),
        )
        return str(settings.output_style.default_effort)
    return str(settings.output_style.default_effort)


def _steering_suffix(args: dict[str, Any], settings: Any) -> str:
    """Build the verbosity + effort guidance suffix appended to tool output.

    Returns "" when verbosity is "default" and effort is "medium" (the
    no-op case), preserving the exact pre-P1-7 output for callers that do
    not pass the new params.
    """
    verbosity = _resolve_verbosity(args, settings)
    effort = _resolve_effort(args, settings)
    return _VERBOSITY_GUIDANCE.get(verbosity, "") + _EFFORT_GUIDANCE.get(effort, "")


# ── Tool listing ───────────────────────────────────────────────────────────────


async def list_tools() -> list[Tool]:
    """Manifest — the canonical ``vesma_*`` tools, unconditionally.

    6.0.0 removed the legacy ``mnemos_*`` spellings: the manifest carries
    the ``vesma_*`` names ONLY and the call path accepts nothing else
    (clients allowlisting ``vesma_*`` must switch to ``vesma_*``).
    """
    return await _canonical_tools()


#: Shared schema fragments of the 10 project-graph tools (ADR-0032 §3.3)
#: — identical across the manifest, so they are defined once.
_GRAPH_PROJECT_PROP: dict[str, str] = {
    "type": "string",
    "description": "Registered project id or name.",
}
_GRAPH_AGENT_PROP: dict[str, str] = {
    "type": "string",
    "description": "Caller agent id (PG7 attribution).",
}
_GRAPH_SESSION_PROP: dict[str, str] = {
    "type": "string",
    "description": "Caller session id (optional).",
}


def _graph_node_kinds() -> tuple[str, ...]:
    """Node kinds of the project graph (ADR-0032 schema) — a lazy import
    keeps the module import graph untouched when the codegraph slice is
    absent (the manifest builder calls it at request time only)."""
    from vesmaro.storage.code_graph_store import NODE_KINDS

    return NODE_KINDS


async def _canonical_tools() -> list[Tool]:
    """Return the tool manifest (39 tools — stable model-visible contract).

    Pre-2.x this was decorated with ``@server.list_tools()``; the port keeps
    the callable importable with the same zero-arg signature (the test suite
    and ``_on_list_tools`` adapter both call it directly).
    """
    _ac = _auto_collect_state["enabled"]

    _recall_desc = (
        (
            "🔄 [AUTO-COLLECT] MANDATORY: Call this at the START of EVERY conversation/session. "
            "Restores project context from long-term memory. Without this, you lose continuity. "
            "Also call after context window compression."
        )
        if _ac
        else (
            "Recall the latest session context for a project from long-term memory. "
            "Use at the START of every session, after context compression, "
            "or whenever you notice gaps in project state. "
            "Returns the most recent checkpoint with goals, progress, and decisions."
        )
    )

    _save_desc = (
        (
            "🔄 [AUTO-COLLECT] MANDATORY: Call this PROACTIVELY — after meaningful work, "
            "before ending a conversation, when context is large, or before switching tasks. "
            "Captures: goals, completed work, decisions, active files, architecture notes."
        )
        if _ac
        else (
            "Save current session context/checkpoint to long-term memory. "
            "Use PROACTIVELY to preserve: current goals, completed tasks, decisions made, "
            "active file paths, architecture notes. "
            "Call after completing significant work steps or before switching major tasks."
        )
    ) + (
        " Optional `agent`/`session` params are the validated identity channel: "
        "`agent` defaults to 'user'; presenting a `session` binds it to that agent "
        "server-side (first writer wins, later mismatches are rejected)."
    )

    _add_desc = (
        (
            "🔄 [AUTO-COLLECT] Proactively save discoveries, patterns, decisions, gotchas, "
            "and any reusable knowledge. Tags MUST include project:<slug>, agent:<slug>, "
            "and at least one mnemos:<subtype> tag."
        )
        if _ac
        else (
            "Add a new entry to long-term memory. "
            "Tags MUST include: project:<slug>, agent:<slug>, and mnemos:<subtype>. "
            "Valid vesma: subtypes: session, bug-pattern, learning, decision, rule, "
            "open-question, checkpoint, legacy."
        )
    )

    _search_desc = (
        (
            "🔄 [AUTO-COLLECT] Search long-term memory BEFORE doing complex work — "
            "check if relevant facts, decisions, or patterns were stored previously."
        )
        if _ac
        else (
            "Search long-term memory using semantic + full-text hybrid search (RRF). "
            "Only searches 'published' knowledge units by default. "
            "Add status filter to query raw/processing/processed entries."
        )
    )

    return [
        Tool(
            name="vesma_search",
            description=_search_desc,
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language search query"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by tags (optional)",
                    },
                    "project": {
                        "type": "string",
                        "description": "Restrict search to a project (optional)",
                    },
                    "task": {
                        "type": "string",
                        "description": (
                            "ADR-0027 Phase 2 (epic #308): optional task scope — "
                            "the BARE slug ([a-z0-9_-]{1,64}, no 'task:' prefix). "
                            "Byte-identical to tags=['task:<slug>'] (the F1 arm-C "
                            "surface): narrows results to entries of that task; "
                            "composes with tags= by intersection (both must hold)."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results (default: 10)",
                        "default": 10,
                    },
                    "include_raw": {
                        "type": "boolean",
                        "description": (
                            "When true, includes raw/processing entries in results "
                            "(default: false — only published/processed)."
                        ),
                        "default": False,
                    },
                    "refined_only": {
                        "type": "boolean",
                        "description": (
                            "ADR-0019: only entries whose served projection is "
                            "the refined one (pipeline_state='refined'); "
                            "legacy/NULL pipeline_state rows never match."
                        ),
                        "default": False,
                    },
                    "status": {
                        "type": "string",
                        "enum": ["raw", "processing", "processed", "published", "archived"],
                        "description": (
                            "Filter by memory status (optional). Overrides include_raw."
                        ),
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="vesma_add",
            description=_add_desc,
            input_schema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Text content to remember"},
                    "title": {
                        "type": "string",
                        "description": "Short title (auto-generated if omitted)",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Tags. REQUIRED: project:<slug>, agent:<slug>, mnemos:<subtype>. "
                            "Optional: task:<slug> (at most one — task scope, ADR-0027), "
                            "severity:, stack:, applyTo:, source: prefixes."
                        ),
                    },
                    "memory_type": {
                        "type": "string",
                        "enum": ["note", "fact", "snippet", "bookmark", "conversation"],
                        "default": "note",
                    },
                    "filter_profile": {
                        "type": "string",
                        "enum": ["log", "terminal", "code", "docs", "web", "default"],
                        "description": "Context Filter profile (M10). Auto-selected if omitted.",
                    },
                },
                "required": ["content", "tags"],
            },
        ),
        Tool(
            name="vesma_filter",
            description=(
                "Run or refresh the context filter on an existing memory. "
                "Useful when auto_filter was off, or to re-filter with a different profile. "
                "Issuance-gated: only published/processed memories are filterable into "
                "context, and the returned clean_content is secret-scanned."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "description": "ID of the memory to filter",
                    },
                    "profile": {
                        "type": "string",
                        "enum": ["log", "terminal", "code", "docs", "web", "default"],
                        "description": "Context Filter profile (auto-selected if omitted)",
                    },
                    "budget": {
                        "type": "integer",
                        "description": "Token budget for truncation (optional)",
                    },
                    "project": {
                        "type": "string",
                        "description": (
                            "Caller project slug — the memory must belong to it "
                            "(mismatch fails closed). Omit for operator semantics."
                        ),
                    },
                },
                "required": ["memory_id"],
            },
        ),
        Tool(
            name="vesma_agent_recall",
            description=(
                "Recall memories filtered by agent identity. "
                "Returns the most recent entries for a specific agent, "
                "optionally scoped to a project and/or a query. (M3)"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "description": "Agent slug (e.g. 'cr-security-reviewer')",
                    },
                    "project": {
                        "type": "string",
                        "description": "Optional project scope",
                    },
                    "task": {
                        "type": "string",
                        "description": (
                            "ADR-0027 Phase 2 (epic #308): optional task scope — "
                            "the BARE slug ([a-z0-9_-]{1,64}, no 'task:' prefix). "
                            "Byte-identical to a task:<slug> tag filter (the F1 "
                            "arm-C surface): narrows the agent's entries to one "
                            "task scope."
                        ),
                    },
                    "query": {
                        "type": "string",
                        "description": "Optional FTS/vector query within agent scope",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 20,
                        "description": "Max entries to return",
                    },
                },
                "required": ["agent"],
            },
        ),
        Tool(
            name="vesma_save_context",
            description=_save_desc,
            input_schema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name (auto-detected from cwd if omitted)",
                    },
                    "goals": {"type": "string", "description": "Current session goals"},
                    "completed": {"type": "string", "description": "What has been completed"},
                    "in_progress": {"type": "string", "description": "What is in progress"},
                    "decisions": {
                        "type": "string",
                        "description": "Key technical decisions and rationale",
                    },
                    "context": {
                        "type": "string",
                        "description": "Other critical context (file paths, architecture, gotchas)",
                    },
                    "agent": {
                        "type": "string",
                        "description": (
                            "Agent identity for this checkpoint — the validated "
                            "identity channel (defaults to 'user'). Must match the "
                            "server-side session→agent binding when a session id is "
                            "supplied."
                        ),
                    },
                    "session": {
                        "type": "string",
                        "description": (
                            "Optional session id binding this checkpoint to a "
                            "conversation. First presentation records the "
                            "session→agent binding server-side; later calls with "
                            "the same session but a different agent are rejected."
                        ),
                    },
                    "task": {
                        "type": "string",
                        "description": (
                            "ADR-0027 Phase 2 (epic #308): optional task scope — "
                            "the BARE slug ([a-z0-9_-]{1,64}, no 'task:' prefix). "
                            "Mints the task:<slug> tag on this checkpoint at the "
                            "save boundary (one mint point, at most one task per "
                            "record); recall it with task= on vesma_recall_context "
                            "/ vesma_search / vesma_list_recent."
                        ),
                    },
                    "language": {
                        "type": "string",
                        "enum": ["ru", "en"],
                        "description": (
                            "Primary language of the checkpoint body (vesma-canon "
                            "envelope). Omitted → the server config default; no "
                            "language guessing is performed."
                        ),
                    },
                },
            },
        ),
        Tool(
            name="vesma_recall_context",
            description=_recall_desc,
            input_schema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name (auto-detected from cwd if omitted)",
                    },
                    "query": {
                        "type": "string",
                        "description": "Optional: specific aspect to focus on",
                    },
                    "task": {
                        "type": "string",
                        "description": (
                            "ADR-0027 Phase 2 (epic #308): optional task scope — "
                            "the BARE slug ([a-z0-9_-]{1,64}, no 'task:' prefix). "
                            "Byte-identical to the checkpoint tag filter plus "
                            "task:<slug> (the F1 arm-C surface): returns only "
                            "checkpoints saved under that task."
                        ),
                    },
                },
            },
        ),
        Tool(
            name="vesma_list_recent",
            description="List the most recent memory entries.",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 10},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by tags (optional)",
                    },
                    "project": {"type": "string", "description": "Filter by project"},
                    "task": {
                        "type": "string",
                        "description": (
                            "ADR-0027 Phase 2 (epic #308): optional task scope — "
                            "the BARE slug ([a-z0-9_-]{1,64}, no 'task:' prefix). "
                            "Byte-identical to appending task:<slug> to tags (the "
                            "F1 arm-C surface); composes with tags= by intersection."
                        ),
                    },
                },
            },
        ),
        Tool(
            name="vesma_list_tags",
            description="List all tags in the memory with their counts.",
            input_schema={"type": "object", "properties": {}},
        ),
        Tool(
            name="vesma_tags_rename",
            description=(
                "Bulk rename tags matching from_prefix:<subtype> → "
                "to_prefix:<subtype> across existing memories. Safe: uses "
                "UPDATE (FTS5 stays consistent), dry_run=true by default, "
                "idempotent. Use to migrate gcw: → mnemos: tags."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "from_prefix": {
                        "type": "string",
                        "description": "Source prefix, e.g. 'gcw:'",
                    },
                    "to_prefix": {
                        "type": "string",
                        "description": "Target prefix, e.g. 'mnemos:'",
                    },
                    "subtypes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional whitelist of subtypes to rename",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "default": True,
                        "description": "Preview without writing (default true)",
                    },
                    "project": {
                        "type": "string",
                        "description": "Scope to a project slug (optional)",
                    },
                    "agent": {
                        "type": "string",
                        "description": "Scope to an agent slug (optional)",
                    },
                    "invalid_subtypes_to_legacy": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Rename invalid subtypes to <to_prefix>legacy instead of skipping them"
                        ),
                    },
                },
                "required": ["from_prefix", "to_prefix"],
            },
        ),
        Tool(
            name="vesma_tags",
            description=(
                "Bulk tag operations across memories: rename a prefix, "
                "remove tags, or add tags. Action-based dispatch — the "
                "grouped pilot tool (vesma #97). action='rename' is the "
                "same as vesma_tags_rename; 'remove' drops exact (or, "
                "with wildcard=true, prefix-matched) tags; 'add' appends "
                "tags to memories matching a project/agent filter."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["rename", "remove", "add"],
                        "description": (
                            "rename: from_prefix->to_prefix "
                            "(use from_prefix/to_prefix). "
                            "remove: drop tags matching the tags list "
                            "(exact match by default). "
                            "add: append tags to memories matching the "
                            "project/agent filter."
                        ),
                    },
                    "from_prefix": {
                        "type": "string",
                        "description": "Source prefix for rename (e.g. 'gcw:')",
                    },
                    "to_prefix": {
                        "type": "string",
                        "description": "Target prefix for rename (e.g. 'mnemos:')",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Tags to remove or add (exact match). "
                            "Required for action='remove' and 'add'."
                        ),
                    },
                    "subtypes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional whitelist filter (rename only)",
                    },
                    "wildcard": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Prefix match (remove) vs exact. rename is prefix-based by design."
                        ),
                    },
                    "dry_run": {
                        "type": "boolean",
                        "default": True,
                        "description": "Preview without writing (default true)",
                    },
                    "project": {
                        "type": "string",
                        "description": "Scope to a project slug (optional)",
                    },
                    "agent": {
                        "type": "string",
                        "description": "Scope to an agent slug (optional)",
                    },
                    "invalid_subtypes_to_legacy": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "rename: rename invalid subtypes to "
                            "<to_prefix>legacy instead of skipping them"
                        ),
                    },
                },
                "required": ["action"],
            },
        ),
        Tool(
            name="vesma_ingest_url",
            description="Fetch a web page, extract its content, and save to memory.",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch and ingest"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tags (must include project:, agent:, mnemos:)",
                    },
                },
                "required": ["url", "tags"],
            },
        ),
        Tool(
            name="vesma_ingest_document",
            description=(
                "Ingest a document as chunked memory rows (ADR-0027 Phase 3). "
                "Chunks are born quarantined (untrusted content) and released "
                "by a danger-sweep at ingest-completion; flagged chunks stay "
                "quarantined. NOT a replacement for vesma_ingest_url — that "
                "tool keeps its single-row semantics."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The full document text to chunk and ingest",
                    },
                    "doc_id": {
                        "type": "string",
                        "description": (
                            "Logical document identity (stable across re-ingest; "
                            "a re-ingest of the same doc_id replaces the chunks "
                            "and bumps the doc-chunk cache version in the same "
                            "transaction)"
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional document title",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tags (must include project:, agent:, mnemos:)",
                    },
                    "source_url": {
                        "type": "string",
                        "description": "Optional provenance URL for the document",
                    },
                },
                "required": ["text", "doc_id", "tags"],
            },
        ),
        Tool(
            name="vesma_watch_start",
            description=(
                "Register a project's code graph for the in-process watch poll "
                "(ADR-0032 §3.2): checks indexed files by mtime+size on an "
                "adaptive interval and reindexes on actual changes. Requires the "
                "operator flags code_graph.enabled and code_graph.watch, an "
                "existing index, and agent attribution. The former "
                "directory-watcher form was an unimplemented stub and is gone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Registered project id to watch",
                    },
                    "agent": {
                        "type": "string",
                        "description": "Caller identity (PG7 per-agent attribution)",
                    },
                    "session": {
                        "type": "string",
                        "description": "Optional session id for the audit trail",
                    },
                },
                "required": ["project_id", "agent"],
            },
        ),
        Tool(
            name="vesma_watch_stop",
            description=(
                "Stop one watch registration (by project_id) or ALL of them when "
                "omitted. Idempotent."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project to stop watching"}
                },
            },
        ),
        Tool(
            name="vesma_watch_status",
            description=(
                "Report active watch registrations and the last poll outcome per "
                "project (ADR-0032 watch poll)."
            ),
            input_schema={"type": "object", "properties": {}},
        ),
        Tool(
            name="vesma_auto_collect_status",
            description=(
                "Report current compaction-detection signal vector. "
                "Returns per-signal values + composite recommendation. (M7)"
            ),
            input_schema={"type": "object", "properties": {}},
        ),
        Tool(
            name="vesma_stats",
            description="Get Vesma health statistics and memory counts.",
            input_schema={"type": "object", "properties": {}},
        ),
        Tool(
            name="vesma_reprocess",
            description=(
                "Manually trigger the knowledge pipeline to process "
                "raw/processing entries into published knowledge. "
                "Use when vesma_stats shows a large queue_depth."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "agent": {"type": "string"},
                    "limit": {"type": "integer", "default": 100},
                },
            },
        ),
        Tool(
            name="vesma_compress",
            description=(
                "Compress large content (tool output, logs, JSON) with ZERO data "
                "loss. The original is cached in SQLite keyed by its hash; the "
                "compressed output embeds a marker so the LLM can call "
                "vesma_retrieve to fetch the full original back. 70-90% token "
                "reduction. Inspired by headroom's CCR (Apache 2.0)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Content to compress (>=500 chars to cache)",
                    },
                    "profile": {
                        "type": "string",
                        "enum": ["log", "terminal", "code", "docs", "web", "default"],
                        "description": "Filter profile (auto-detected if omitted)",
                    },
                    "project": {
                        "type": "string",
                        "description": "Project slug to scope the cache entry (optional)",
                    },
                    "agent": {
                        "type": "string",
                        "description": (
                            "A2 issuer ledger: your agent slug — recorded as "
                            "the cache entry issuer so strict marker "
                            "validation can later prove the marker was "
                            "minted in your context (optional)"
                        ),
                    },
                    "session": {
                        "type": "string",
                        "description": (
                            "A2 issuer ledger: your session id — recorded "
                            "with the agent slug as the issuer pair "
                            "(optional)"
                        ),
                    },
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="vesma_retrieve",
            description=(
                "Retrieve the original uncompressed content for a CCR marker hash. "
                "If query is omitted: returns the full original. If query is "
                "provided: returns FTS5-ranked snippets from within the cached "
                "original. Use the hash from a [compressed: <hash> | ...] marker. "
                "Issued content is scanned for secrets: matched spans are "
                "redacted (<REDACTED:<pattern>>) in the response, which reports "
                "the count via 'redactions' (0 when clean); the stored original "
                "is preserved unchanged. A2 strict marker validation: pass "
                "validate_marker=true with original_chars (N from the marker) "
                "and your agent/session to require existence + integrity + "
                "issuer-provenance checks before any content is issued "
                "(fail-closed refusal otherwise)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hash": {
                        "type": "string",
                        "description": "SHA-256 hash from a CCR marker",
                    },
                    "query": {
                        "type": "string",
                        "description": "Optional search query for snippet retrieval",
                    },
                    "snippet_count": {
                        "type": "integer",
                        "default": 5,
                        "description": "Number of snippets when query is provided",
                    },
                    "project": {
                        "type": "string",
                        "description": (
                            "Optional project slug: scope the lookup to this "
                            "project's entries — a hash cached under another "
                            "project is reported as not found"
                        ),
                    },
                    "validate_marker": {
                        "type": "boolean",
                        "description": (
                            "A2 strict mode: validate the marker (existence + "
                            "original_chars integrity + issuer provenance) "
                            "before issuing. Defaults to the "
                            "ccr.validate_markers config knob."
                        ),
                    },
                    "original_chars": {
                        "type": "integer",
                        "description": (
                            "N from the [compressed: <hash> | N→M chars] "
                            "marker — enables the integrity check"
                        ),
                    },
                    "agent": {
                        "type": "string",
                        "description": (
                            "Your agent slug — the trusted issuer context for the provenance check"
                        ),
                    },
                    "session": {
                        "type": "string",
                        "description": (
                            "Your session id — paired with agent as the trusted issuer context"
                        ),
                    },
                },
                "required": ["hash"],
            },
        ),
        Tool(
            name="vesma_align_prefix",
            description=(
                "P1-5 CacheAligner — relocate dynamic content (timestamps, UUIDs, "
                "session ids, tokens) to the end of text so the prefix stays "
                "byte-identical across requests and provider KV caches "
                "(Anthropic cache_control, OpenAI prefix caching) hit. Inspired "
                "by headroom's CacheAligner (Apache 2.0). Original implementation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "System-prompt-like text to stabilize",
                    },
                    "profile": {
                        "type": "string",
                        "enum": ["code", "docs", "default"],
                        "description": (
                            "Filter profile toggling which dynamic kinds are "
                            "extracted. 'code' skips bare tokens (avoids mangling "
                            "long identifiers). Default extracts all kinds."
                        ),
                    },
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="vesma_assemble_context",
            description=(
                "ADR-0017 D1 provider contract — assemble the model-facing "
                "context block for a pre-LLM-call injection. Fixed pipeline: "
                "hybrid RRF recall (published/processed only, the entry-"
                "invariant status gate) → optional CCR marker expansion → "
                "context filter → MANDATORY secret scan (redacted spans are "
                "counted per block; nothing enters the output unscanned) → "
                "CacheAligner → token budget. Every injected block carries a "
                "provenance line "
                "'[mnemos:<id> project=<slug> status=<status> origin=<source> "
                "pipeline=<phase> v=<n> retrieved=<iso>]' (ADR-0019: pipeline= "
                "is omitted on legacy NULL pipeline_state; origin renders the "
                "server-side source column; v is the marker version). "
                "mode: sync (default) / async (store the result, return a "
                "handle, fetch it on a later call via async_handle) / code / "
                "prose (filter recall candidates by stored content type). "
                "Returns the assembled text, per-block provenance + redaction "
                "counts, and token stats."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "session": {
                        "type": "string",
                        "description": (
                            "Caller's session identifier (echoed in the result; "
                            "identifies the assembly, not the memories)."
                        ),
                    },
                    "project": {
                        "type": "string",
                        "description": "Project slug scoping recall and CCR redemption.",
                    },
                    "file": {
                        "type": "string",
                        "description": (
                            "Optional file path: contributes recall query terms "
                            "and pins applyTo-scoped rule memories to the top."
                        ),
                    },
                    "agent": {
                        "type": "string",
                        "description": (
                            "A2: caller's agent slug — paired with session as "
                            "the issuer context for the strict-mode CCR marker "
                            "expansion gate (ccr.validate_markers); without it "
                            "strict deployments skip expansion of issuer-"
                            "stamped markers (the marker stays)."
                        ),
                    },
                    "budget": {
                        "type": "integer",
                        "default": 2048,
                        "description": "Token budget for the assembled block.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["sync", "async", "code", "prose"],
                        "default": "sync",
                        "description": (
                            "sync = return the assembled block now (default); "
                            "async = return a handle, fetch via async_handle; "
                            "code/prose = sync delivery + filter recall "
                            "candidates by stored content type."
                        ),
                    },
                    "expand_ccr": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Enable the optional CCR stage: expand inline "
                            "[compressed: <hash> | ...] markers found in "
                            "recalled content via project-scoped retrieval, "
                            "budget-aware (originals that would not fit stay "
                            "compressed)."
                        ),
                    },
                    "async_handle": {
                        "type": "string",
                        "description": (
                            "Fetch (and pop) a result stored by a previous mode='async' call."
                        ),
                    },
                },
                "required": ["session", "project"],
            },
        ),
        Tool(
            name="vesma_context_rewrite",
            description=(
                "ADR-0018 on_context_rewrite lifecycle event — the harness "
                "reports that it REWROTE a block of its working context: the "
                "original is stored to long-term memory losslessly (normal "
                "knowledge pipeline: enters raw, context-reachable only after "
                "the pipeline advances it to processed/published; write-path "
                "secret scan auto-tags mnemos:no-federate on a hit). "
                "IDEMPOTENT: the same event re-delivered performs no "
                "duplicate writes (content-addressed event key over "
                "project/agent/session/supersedes/content; the advisory diff "
                "is excluded — it is not load-bearing). VERSION-LESS: no "
                "ordering promise, no version chains — replacement lineage "
                "is a supersedes edge (optional 'supersedes' = memory id of "
                "the replaced block). Rehydrate goes through the EXISTING "
                "scanned/gated channels (vesma_retrieve / "
                "vesma_assemble_context). Set include_marker=true to also "
                "get the CCR compress marker for the original to keep in the "
                "window."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": (
                            "Original text of the replaced context block — "
                            "the source of truth, stored unchanged."
                        ),
                    },
                    "project": {
                        "type": "string",
                        "description": "Project slug (tag project:<slug>).",
                    },
                    "agent": {
                        "type": "string",
                        "description": "Agent slug (tag agent:<slug>).",
                    },
                    "session": {
                        "type": "string",
                        "description": (
                            "Optional session id — provenance metadata and "
                            "part of the idempotency key."
                        ),
                    },
                    "supersedes": {
                        "type": "string",
                        "description": (
                            "Optional memory id of the block being replaced — "
                            "creates the supersedes edge new → old."
                        ),
                    },
                    "diff": {
                        "type": "string",
                        "description": (
                            "Optional advisory was→becomes diff — stored as "
                            "metadata only, never load-bearing."
                        ),
                    },
                    "include_marker": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Also return the CCR compress marker for the "
                            "original (the caller keeps it in its window)."
                        ),
                    },
                },
                "required": ["content", "project", "agent"],
            },
        ),
        Tool(
            name="vesma_hooks",
            description=(
                "ADR-0017 D1 / ADR-0018 lifecycle hooks — the automation "
                "integration points, grouped behind action:enum (vesma #97 "
                "pattern). action='pre_llm_call': assemble the context block "
                "to INJECT before a model call (sync delivery; "
                "context_hint = what the call is about, used as the recall "
                "query; the ADR-0018 entry invariant — secret scan, "
                "provenance, status gate — runs inside the assemble "
                "pipeline). action='on_session_start': recall recent "
                "checkpoints for session bootstrap (content scanned at "
                "issuance on this channel). action='post_tool_call': the "
                "autocompression entry point — when auto_compress resolves "
                "true (per-call argument, else the hooks.auto_compress "
                "config knob, default false), the tool output is compressed "
                "via CCR and the marker-headed compressed_text is returned "
                "for the caller to SUBSTITUTE in its window. IDENTITY "
                "MANDATE (A2 register N2): session+project+agent are "
                "required on every call — post_tool_call compression always "
                "threads (agent, session) onto the cache row so strict "
                "marker validation can later prove provenance; "
                "identity-less compression would mint unverifiable rows."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["pre_llm_call", "on_session_start", "post_tool_call"],
                        "description": (
                            "pre_llm_call: assemble the pre-model-call "
                            "injection block. on_session_start: recall "
                            "recent checkpoints. post_tool_call: compress "
                            "tool output (autocompression, opt-in)."
                        ),
                    },
                    "session": {"type": "string", "description": "Caller session id."},
                    "project": {"type": "string", "description": "Project slug."},
                    "agent": {
                        "type": "string",
                        "description": "Caller agent slug (issuer identity).",
                    },
                    "context_hint": {
                        "type": "string",
                        "description": (
                            "pre_llm_call only: what the upcoming model call "
                            "is about — used as the recall query instead of "
                            "the derived project/file term."
                        ),
                    },
                    "file": {
                        "type": "string",
                        "description": (
                            "pre_llm_call only: optional file path (recall "
                            "terms + applyTo rule pinning)."
                        ),
                    },
                    "task": {
                        "type": "string",
                        "description": (
                            "pre_llm_call only (ADR-0027 Phase 0): optional "
                            "task scope — the bare task slug "
                            "([a-z0-9_-]{1,64}, no 'task:' prefix). Narrows "
                            "recall to rows tagged task:<slug> (intersection "
                            "doctrine: a task condition only narrows, never "
                            "widens; tail-only — pinned prefixes and the "
                            "provenance format are untouched)."
                        ),
                    },
                    "budget": {
                        "type": "integer",
                        "default": 2048,
                        "description": "pre_llm_call only: token budget.",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 5,
                        "description": "on_session_start only: checkpoint count.",
                    },
                    "tool_name": {
                        "type": "string",
                        "description": "post_tool_call only: the tool that ran.",
                    },
                    "output_text": {
                        "type": "string",
                        "description": ("post_tool_call only: the raw tool output to compress."),
                    },
                    "auto_compress": {
                        "type": "boolean",
                        "description": (
                            "post_tool_call only: per-call override of the "
                            "hooks.auto_compress knob."
                        ),
                    },
                    "profile": {
                        "type": "string",
                        "description": (
                            "post_tool_call only: optional filter profile hint for the compression."
                        ),
                    },
                    "include_awareness": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "pre_llm_call/on_session_start only (vesma #254): "
                            "compose the awareness delta/presence section — "
                            "appended LAST, never pinnable, cursor advances "
                            "on pre_llm_call only. Default false: output is "
                            "byte-identical to the pre-#254 shape."
                        ),
                    },
                },
                "required": ["action", "session", "project", "agent"],
            },
        ),
        Tool(
            name="vesma_awareness",
            description=(
                "Awareness pre-flight (vesma #254, R3): presence + delta + "
                "conflict-hints for PARALLEL sessions over one project. Call "
                "BEFORE risky operations (writes to a component/task another "
                "active session may have claimed — the PR #224 contract: a "
                "release closed by a parallel session that was invisible). "
                "action='pre_flight' returns server-observed neighbor "
                "activity (observed facts) and their self-reported goals "
                "(unverified peer claims, labeled separately) plus lexical "
                "conflict-hints against my last checkpoint goal, plus the "
                "swarm v0a/v0b operational picture ('picture': same-project "
                "peers — agent id, last activity, record count in the "
                "900s window, checkpoint presence; counts/ids/timestamps "
                "only, never any peer content; swarm v0b adds the peer's "
                "CLAIMED active task slug ('task', from its most recent "
                "task:-tagged row) — a self-reported claim rendered in a "
                "separate labeled [unverified] sub-section, never in the "
                "observed facts). Presence "
                "claims are self-reported by peers and unverified; do not "
                "abstain from work based on presence without operator "
                "coordination — awareness supplies DATA, decisions stay "
                "with the agent. Queries are rate-capped per (project, "
                "agent) — over-limit degrades to a 'rate-limited, retry "
                "later' line, never an error. action='record_abstention' logs an "
                "abstention-on-presence as an action with a reconstructable "
                "provenance chain (abstention -> delta-block -> "
                "checkpoint-id -> writer-session); pass basis_checkpoint_id "
                "from the pre-flight response. Pre-flight is READ-ONLY (the "
                "awareness cursor advances only via hooks pre_llm_call with "
                "include_awareness=true). Strictly project-scoped."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["pre_flight", "record_abstention"],
                        "description": (
                            "pre_flight: presence + delta + conflict-hints. "
                            "record_abstention: attribute an abstention "
                            "decision to the delta block it was based on."
                        ),
                    },
                    "session": {"type": "string", "description": "Caller session id."},
                    "project": {"type": "string", "description": "Project slug (required)."},
                    "agent": {"type": "string", "description": "Caller agent slug."},
                    "basis_checkpoint_id": {
                        "type": "string",
                        "description": (
                            "record_abstention only: the neighbor checkpoint "
                            "id the abstention is based on (from pre_flight)."
                        ),
                    },
                    "note": {
                        "type": "string",
                        "description": ("record_abstention only: short reason (no secrets)."),
                    },
                },
                "required": ["action", "session", "project", "agent"],
            },
        ),
        Tool(
            name="vesma_export",
            description=(
                "Export memories to a file (JSON or SQLite snapshot). Writes the "
                "result to disk and returns metadata only (path, memory_count, "
                "format, bytes) — the content is NOT returned inline (stdio "
                "transport limitation). Thin wrapper over the CLI export logic. "
                "Inherits #86 federation defence: excludes mnemos:no-federate "
                "records and redacts detected secrets in passing records. "
                "When encrypt=true the passphrase is read from the "
                "VESMA_EXPORT_PASSPHRASE environment variable — never pass the "
                "passphrase value in the tool arguments (it would appear in logs)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "format": {
                        "type": "string",
                        "enum": ["json", "sqlite"],
                        "default": "json",
                        "description": (
                            "json = metadata-only export with filters; "
                            "sqlite = full tar.gz snapshot (filters ignored)."
                        ),
                    },
                    "compress": {
                        "type": "string",
                        "enum": ["none", "gzip"],
                        "default": "none",
                        "description": "Compression mode (zstd is CLI-only).",
                    },
                    "project": {
                        "type": "string",
                        "description": "Filter by project slug (json only).",
                    },
                    "agent": {
                        "type": "string",
                        "description": "Filter by agent slug (json only).",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["raw", "processing", "processed", "published", "archived"],
                        "description": "Filter by memory status (json only).",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by tags (json only).",
                    },
                    "since": {
                        "type": "string",
                        "description": (
                            "ISO-8601 timestamp — only memories created on or "
                            "after this date (json only)."
                        ),
                    },
                    "until": {
                        "type": "string",
                        "description": (
                            "ISO-8601 timestamp — only memories created before "
                            "this date (json only)."
                        ),
                    },
                    "encrypt": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "When true, encrypt the output with the passphrase "
                            "from the VESMA_EXPORT_PASSPHRASE env var."
                        ),
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Absolute path where the export file is written.",
                    },
                },
                "required": ["output_path"],
            },
        ),
        Tool(
            name="vesma_import",
            description=(
                "Import memories from an export file (merge or restore mode). "
                "Thin wrapper over the CLI import logic. Inherits #86 import "
                "validation: rejects schema drift, oversized content, invalid "
                "tags; logs prompt-injection patterns at WARNING without blocking. "
                "Restore mode is destructive (wipes all existing data) and "
                "requires confirm=true. For encrypted inputs the passphrase is "
                "read from the environment variable NAMED by passphrase_env "
                "(never the value itself — passing the value in arguments would "
                "leak it into logs)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "source_path": {
                        "type": "string",
                        "description": "Absolute path to the export file to import.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["merge", "restore"],
                        "default": "merge",
                        "description": (
                            "merge = insert new / skip-or-overwrite existing; "
                            "restore = wipe all then import (requires confirm=true)."
                        ),
                    },
                    "overwrite": {
                        "type": "boolean",
                        "default": False,
                        "description": "Overwrite existing memories (merge mode only).",
                    },
                    "confirm": {
                        "type": "boolean",
                        "default": False,
                        "description": "Required true for restore mode (hard gate).",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "default": False,
                        "description": "Validate without writing; returns a validation report.",
                    },
                    "passphrase_env": {
                        "type": "string",
                        "description": (
                            "Name of the environment variable holding the "
                            "decryption passphrase (NOT the value)."
                        ),
                    },
                },
                "required": ["source_path"],
            },
        ),
        Tool(
            name="vesma_workflow",
            description=(
                "Workflow lifecycle management for a memory (vesma #96). "
                "Separates mutable workflow state (open/in-progress/blocked/"
                "resolved/done/withdrawn) from append-only tag classification. "
                "Action-based dispatch — same pattern as vesma_tags. "
                "'set' transitions the status through a server-enforced state "
                "machine (blocked->done is forbidden; terminal states are final), "
                "acquires/releases a lock, and records every transition in an "
                "audit log. 'get' returns the current status + lock owner. "
                "'history' returns the audit trail. Guardrails: stale-lock "
                "auto-release (>24h), idempotent transitions (no-op on same "
                "status), force-unlock (requires reason), per-memory rate limit."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["set", "get", "history"],
                        "description": (
                            "set: transition status (requires memory_id, to, actor). "
                            "get: return current status + lock. "
                            "history: return the audit trail."
                        ),
                    },
                    "memory_id": {
                        "type": "string",
                        "description": "Target memory id.",
                    },
                    "to": {
                        "type": "string",
                        "enum": [
                            "open",
                            "in-progress",
                            "blocked",
                            "resolved",
                            "done",
                            "withdrawn",
                        ],
                        "description": (
                            "Target status (action='set' only). blocked->done is "
                            "forbidden — a blocked memory must resolve first."
                        ),
                    },
                    "actor": {
                        "type": "string",
                        "description": (
                            "Free-form actor id (Phase 1 weak identity — NO "
                            "authn/authz). Required for action='set'."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "default": "",
                        "description": "Human-readable reason. Required when force=true.",
                    },
                    "force": {
                        "type": "boolean",
                        "default": False,
                        "description": "Override a lock held by another actor (requires reason).",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 50,
                        "description": "Max history rows (action='history' only).",
                    },
                },
                "required": ["action", "memory_id"],
            },
        ),
        # ── project graph, 10 tools (ADR-0032 §3.3 / contract §3.3) ────────
        # Master flag settings.code_graph.enabled (default OFF): the tools
        # stay in the manifest, every call answers a disabled error —
        # the same gate shape as auto-collect's description swap above.
        Tool(
            name="vesma_index_project",
            description=(
                "Index a REGISTERED project root into the shared project "
                "graph (ADR-0032). Full or incremental; serialized per "
                "project (a concurrent call gets 'in-progress' status). "
                "PG2: only a project registered in the projects table is "
                "accepted — arbitrary paths are refused. PG7: limits are "
                "fail-closed, the run is audited with your agent id. "
                "Returns the result plus a staleness summary."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Registered project id or unique name.",
                    },
                    "agent": _GRAPH_AGENT_PROP,
                    "session": _GRAPH_SESSION_PROP,
                    "incremental": {
                        "type": "boolean",
                        "default": True,
                        "description": "Skip work when nothing changed (default true).",
                    },
                    "reason": {"type": "string", "description": "Audit reason (optional)."},
                },
                "required": ["project_id", "agent"],
            },
        ),
        Tool(
            name="vesma_project_graph_status",
            description=(
                "Project-graph status for one registered project: node/edge/"
                "file volumes, freshness (fresh %, last_indexed_at), parse "
                "failures ('clean ≠ proof' — they stay visible) and the "
                "poisoned-file count (PG3). Read-only, audited."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": _GRAPH_PROJECT_PROP,
                    "agent": _GRAPH_AGENT_PROP,
                    "session": _GRAPH_SESSION_PROP,
                },
                "required": ["project_id", "agent"],
            },
        ),
        Tool(
            name="vesma_search_graph",
            description=(
                "Search the project graph by name / qualified name / path "
                "(substring; exact hits outrank prefix, prefix outranks "
                "substring). Token contract: max_output_tokens 128-1M "
                "(default 3200), whole-row drops, strictly advancing "
                "cursor, has_more; signatures are opt-in via "
                "include_signature. Read-only, audited per agent."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": _GRAPH_PROJECT_PROP,
                    "query": {"type": "string", "description": "Name/qname/path substring."},
                    "agent": _GRAPH_AGENT_PROP,
                    "session": _GRAPH_SESSION_PROP,
                    "kind": {
                        "type": "string",
                        "enum": list(_graph_node_kinds()),
                        "description": "Filter by node kind (optional).",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 50,
                        "description": "Max ranked rows per page.",
                    },
                    "cursor": {
                        "type": "integer",
                        "default": 0,
                        "description": "Page cursor from the previous call.",
                    },
                    "max_output_tokens": {
                        "type": "integer",
                        "default": 3200,
                        "description": "Output budget (128-1M); 4 UTF-8 bytes per token ceiling.",
                    },
                    "include_signature": {
                        "type": "boolean",
                        "default": False,
                        "description": "Opt-in detail flag: include signatures.",
                    },
                },
                "required": ["project_id", "query", "agent"],
            },
        ),
        Tool(
            name="vesma_trace_path",
            description=(
                "BFS over project_edges from one symbol (resolve by qname, "
                "unique — ambiguous refusals name search_graph). Depth ≤ 2, "
                "per-node fanout cap, total-work cap (the ADR-0030 walk "
                "discipline). Token contract applies."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": _GRAPH_PROJECT_PROP,
                    "qname": {
                        "type": "string",
                        "description": "Symbol qualified name (exact or unique tail).",
                    },
                    "agent": _GRAPH_AGENT_PROP,
                    "session": _GRAPH_SESSION_PROP,
                    "depth": {
                        "type": "integer",
                        "default": 2,
                        "description": "BFS depth, 1-2.",
                    },
                    "max_output_tokens": {
                        "type": "integer",
                        "default": 3200,
                        "description": "Output budget (128-1M).",
                    },
                },
                "required": ["project_id", "qname", "agent"],
            },
        ),
        Tool(
            name="vesma_get_file_outline",
            description=(
                "Symbol outline of one indexed file (kinds, names, qnames, "
                "line ranges; signatures included — shapes, never bodies, "
                "PG1). Path is repo-relative and must stay inside the "
                "registered root (PG2). Parse failures ride along as an "
                "honesty marker. Token contract applies."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": _GRAPH_PROJECT_PROP,
                    "path": {"type": "string", "description": "Repo-relative file path."},
                    "agent": _GRAPH_AGENT_PROP,
                    "session": _GRAPH_SESSION_PROP,
                    "cursor": {"type": "integer", "default": 0, "description": "Page cursor."},
                    "max_output_tokens": {
                        "type": "integer",
                        "default": 3200,
                        "description": "Output budget (128-1M).",
                    },
                },
                "required": ["project_id", "path", "agent"],
            },
        ),
        Tool(
            name="vesma_get_code_snippet",
            description=(
                "Read a line range FROM DISK for an indexed file (PG4): "
                "mtime+size+sha256 verified against the indexed record — a "
                "divergence yields a staleness marker, never content; the "
                "range is secret-scanned at issuance and ANY hit refuses "
                "fail-closed. Poisoned files (hit the secrets detector at "
                "index time) are refused permanently — only "
                "vesma_delete_graph_project clears them. No snippet cache. "
                "Token contract applies (whole-line drops)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": _GRAPH_PROJECT_PROP,
                    "path": {"type": "string", "description": "Repo-relative file path."},
                    "start_line": {"type": "integer", "description": "First line (1-based)."},
                    "end_line": {"type": "integer", "description": "Last line inclusive."},
                    "agent": _GRAPH_AGENT_PROP,
                    "session": _GRAPH_SESSION_PROP,
                    "max_output_tokens": {
                        "type": "integer",
                        "default": 3200,
                        "description": "Output budget (128-1M).",
                    },
                },
                "required": ["project_id", "path", "start_line", "end_line", "agent"],
            },
        ),
        Tool(
            name="vesma_check_graph_coverage",
            description=(
                "Batch coverage check: per-path verdict indexed / stale / "
                "parse-error / unindexed / poisoned (coverage honesty — "
                "trust is NOT here; verify with vesma_get_code_snippet)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": _GRAPH_PROJECT_PROP,
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Repo-relative paths to check.",
                    },
                    "agent": _GRAPH_AGENT_PROP,
                    "session": _GRAPH_SESSION_PROP,
                },
                "required": ["project_id", "paths", "agent"],
            },
        ),
        Tool(
            name="vesma_get_graph_schema",
            description=(
                "The project-graph contract card for agents: node/edge "
                "kinds, token contract, index and trace limits, schema "
                "version; an optional project adds its volumes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Registered project id or name (optional).",
                    },
                    "agent": _GRAPH_AGENT_PROP,
                    "session": _GRAPH_SESSION_PROP,
                },
                "required": ["agent"],
            },
        ),
        Tool(
            name="vesma_list_graph_projects",
            description=(
                "Registered projects joined with their index status "
                "(volumes, poisoned count, last_indexed_at); "
                "registered-but-never-indexed stays visible."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent": _GRAPH_AGENT_PROP,
                    "session": _GRAPH_SESSION_PROP,
                },
                "required": ["agent"],
            },
        ),
        Tool(
            name="vesma_delete_graph_project",
            description=(
                "Drop a project's graph INDEX (sidecar data — never the "
                "project entity in the main DB). The ONLY operation that "
                "clears the poisoned set (PG3 'forever'). Audited with a "
                "reason."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": _GRAPH_PROJECT_PROP,
                    "agent": _GRAPH_AGENT_PROP,
                    "session": _GRAPH_SESSION_PROP,
                    "reason": {"type": "string", "description": "Audit reason (optional)."},
                },
                "required": ["project_id", "agent"],
            },
        ),
        Tool(
            name="vesma_register_project",
            description=(
                "Register a project root for the code graph (#454) — the "
                "answer to 'not registered' refusals. The root must exist "
                "on disk, be absolute, carry a packaging manifest "
                "(pyproject.toml/setup.py/package.json/go.mod/Cargo.toml) "
                "or a .git, and not be $HOME/the filesystem root. "
                "Idempotent when the root is already registered (audit "
                "manual-register-reused). Attaches a root to an existing "
                "pathless project; a name already registered at ANOTHER "
                "root is refused (moved roots: 'vesma graph repoint'). "
                "Explicit registration does NOT count against "
                "auto_register_max_projects (that cap bounds the auto "
                "path only). Audited as manual-register."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Project id or name to register.",
                    },
                    "root": {
                        "type": "string",
                        "description": "Absolute path to the project root on disk.",
                    },
                    "agent": _GRAPH_AGENT_PROP,
                    "session": _GRAPH_SESSION_PROP,
                },
                "required": ["project_id", "root", "agent"],
            },
        ),
    ]


# ── Tool call handler ──────────────────────────────────────────────────────────


_KNOWN_TOOLS_CACHE: frozenset[str] | None = None


async def _known_tool_names() -> frozenset[str]:
    """Registered tool names — the verb cardinality allowlist (m2).

    A client-supplied unknown name collapses to the fixed verb
    ``unknown``: Prometheus label cardinality must stay bounded
    (RL-S2), the raw name is never stored. Cached after the first call.
    """
    global _KNOWN_TOOLS_CACHE
    if _KNOWN_TOOLS_CACHE is None:
        try:
            _KNOWN_TOOLS_CACHE = frozenset(t.name for t in await list_tools())
        except Exception:
            _KNOWN_TOOLS_CACHE = frozenset()
    return _KNOWN_TOOLS_CACHE


async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Time one MCP tool call, record it as a verb, attach the heartbeat.

    Thin shell over :func:`_call_tool_dispatch` — every tool, including
    ``vesma_assemble_context``, lands in the verb ledger here (the
    assemble row is a SEPARATE plane written inside the assemble
    handler: ledger says who/what/how-long, the domain table says what
    was assembled — two planes of fact, not double counting).

    W2a (ADR-0035): this is also the SINGLE injection point of the
    native awareness heartbeat — one appended ``TextContent`` after the
    handler (canary/on modes only; ``off``/``shadow`` leave the response
    bytes untouched). The heartbeat glue never raises and never alters
    the dispatch result.
    """
    import time as _time

    t0 = _time.monotonic()
    try:
        result = await _call_tool_dispatch(name, arguments)
    except Exception:
        with contextlib.suppress(Exception):  # telemetry never masks the error
            get_manager().record_verb_vitals(
                surface="mcp",
                verb=name if name in await _known_tool_names() else "unknown",
                status="error",
                latency_ms=(_time.monotonic() - t0) * 1000,
            )
        raise
    with contextlib.suppress(Exception):  # guest contract
        get_manager().record_verb_vitals(
            surface="mcp",
            verb=name if name in await _known_tool_names() else "unknown",
            status="ok",
            latency_ms=(_time.monotonic() - t0) * 1000,
        )
    # W2a (ADR-0035): native awareness heartbeat — the single injection
    # point. Returns the tail text (canary/on) or None (off / shadow /
    # suppressed); never raises, never touches `result` bytes. The
    # TextContent lives HERE — ADR-0023 keeps the mcp SDK inside this
    # module only.
    heartbeat_text = native_heartbeat_tail(name, arguments)
    if heartbeat_text is not None:
        result = [*result, TextContent(type="text", text=heartbeat_text)]
    return result


async def _call_tool_dispatch(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Dispatch a tool call and wrap the result in TextContent.

    Pre-2.x this was decorated with ``@server.call_tool()``; the port keeps
    the callable importable with the same ``(name, arguments)`` signature
    (the test suite drives it directly).

    6.0.0: the canonical ``vesma_*`` names dispatch as-is; no legacy
    ``mnemos_*`` normalisation exists any more — an unknown name falls
    through to the dispatch error reporting the name the caller used.
    """
    _track_call(is_save=(name == "vesma_save_context"))
    reminder = _checkpoint_reminder()
    # #456: the one-time upgrade notice rides the FIRST dispatch of the
    # process (checked once, fail-open on any store error).
    try:
        update_hint = _server_update_hint(get_manager())
    except Exception:
        update_hint = None

    try:
        result = await _dispatch(name, arguments)
    except TagContractError as exc:
        return [TextContent(type="text", text=f"❌ Tag contract violation:\n{exc}")]
    except Exception as exc:
        logger.exception("Tool %s failed: %s", name, exc)
        return [TextContent(type="text", text=f"❌ Error: {exc}")]

    text = (
        result if isinstance(result, str) else json.dumps(result, default=str, ensure_ascii=False)
    )
    if update_hint:
        text += update_hint
        # #464 P3-1: the meta re-stamp lands only when the notice
        # actually rode a response — a failed dispatch above left it
        # pending and the next dispatch delivers it instead.
        _commit_server_update_hint(get_manager())
    if reminder:
        text += reminder
    return [TextContent(type="text", text=text)]


# ── #84 federation export/import handlers ────────────────────────────────────
#
# Thin wrappers over cli/export.py::run_export and cli/import_.py::run_import.
# Both underlying functions are already clean callables (no Typer ctx), so no
# refactoring was required. The wrappers:
#   * validate the MCP arguments,
#   * read passphrases from the environment (never from args — per
#     sensitive-data.instructions.md args appear in MCP logs),
#   * write the export to disk and return metadata only (no inline content —
#     stdio transport cannot carry binary or large JSON inline),
#   * enforce the restore-mode confirm gate.


def _handle_awareness(mgr: Any, args: dict[str, Any]) -> dict[str, Any]:
    """``vesma_awareness`` action dispatch (vesma #254, R3).

    Boundary type guards (the ``vesma_hooks`` pattern): a malformed
    caller gets a clean error dict; ValueError from the awareness
    boundary (project=None fail-closed, bad cursor, bogus abstention
    basis) maps to the same shape. Thin wrapper over the
    ``vesmaro.awareness`` core (the one-core-over-three-surfaces rule).
    """
    from vesmaro.awareness import pre_flight_snapshot, record_abstention

    awr_action = args.get("action")
    if awr_action not in ("pre_flight", "record_abstention"):
        return {"error": "action must be one of: pre_flight, record_abstention"}
    awr_session = args.get("session")
    if not isinstance(awr_session, str) or not awr_session.strip():
        return {"error": "session is required and must be a non-empty string"}
    awr_project = args.get("project")
    if not isinstance(awr_project, str) or not awr_project.strip():
        return {"error": "project is required and must be a non-empty string"}
    awr_agent = args.get("agent")
    if not isinstance(awr_agent, str) or not awr_agent.strip():
        return {"error": "agent is required and must be a non-empty string"}
    try:
        if awr_action == "record_abstention":
            basis = args.get("basis_checkpoint_id")
            note = args.get("note")
            if not isinstance(basis, str) or not basis.strip():
                return {"error": "record_abstention requires 'basis_checkpoint_id'"}
            if note is not None and not isinstance(note, str):
                return {"error": "note must be a string when provided"}
            return record_abstention(
                mgr,
                project=awr_project,
                agent=awr_agent,
                session=awr_session,
                basis_checkpoint_id=basis,
                note=note,
            )
        result = pre_flight_snapshot(mgr, project=awr_project, agent=awr_agent, session=awr_session)
        result["note"] = (
            "read-only pre-flight; the awareness cursor advances only via "
            "hooks pre_llm_call with include_awareness=true"
        )
        return result
    except ValueError as exc:
        return {"error": str(exc)}


def _handle_export(mgr: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Dispatch helper for the ``vesma_export`` tool."""
    from vesmaro.cli.export import CompressMode, ExportFilter, ExportFormat, run_export

    output_path_str = args.get("output_path")
    if not output_path_str or not isinstance(output_path_str, str):
        return {"error": "output_path is required and must be a string"}
    output_path = Path(output_path_str)
    if not output_path.is_absolute():
        return {"error": f"output_path must be absolute, got: {output_path}"}

    fmt_str = args.get("format", "json")
    try:
        fmt = ExportFormat(fmt_str)
    except ValueError as exc:
        return {"error": f"Invalid format '{fmt_str}': {exc}"}

    compress_str = args.get("compress", "none")
    try:
        compress = CompressMode(compress_str)
    except ValueError as exc:
        return {"error": f"Invalid compress '{compress_str}': {exc}"}

    # ── Filters (json only; sqlite ignores them) ──────────────────────────
    status_str = args.get("status")
    status: MemoryStatus | None = None
    if status_str:
        try:
            status = MemoryStatus(status_str)
        except ValueError as exc:
            return {"error": f"Invalid status '{status_str}': {exc}"}

    since_raw = args.get("since")
    until_raw = args.get("until")
    since_dt = _parse_iso_arg(since_raw) if since_raw else None
    until_dt = _parse_iso_arg(until_raw) if until_raw else None
    if since_raw and since_dt is None:
        return {"error": f"Invalid since timestamp: {since_raw}"}
    if until_raw and until_dt is None:
        return {"error": f"Invalid until timestamp: {until_raw}"}

    filt = ExportFilter(
        project=args.get("project"),
        agent=args.get("agent"),
        status=status,
        tags=args.get("tags"),
        since=since_dt,
        until=until_dt,
    )

    # ── Encryption: passphrase from env, never from args ───────────────────
    encrypt = bool(args.get("encrypt", False))
    passphrase: str | None = None
    if encrypt:
        passphrase = os.environ.get("VESMA_EXPORT_PASSPHRASE")
        if not passphrase:
            return {
                "error": (
                    "encrypt=true but VESMA_EXPORT_PASSPHRASE environment "
                    "variable is not set or empty. Set it before calling "
                    "vesma_export — the passphrase value must never appear "
                    "in tool arguments."
                )
            }

    try:
        result = run_export(
            mgr,
            fmt=fmt,
            output=output_path,
            compress=compress,
            encrypt=encrypt,
            passphrase=passphrase,
            filt=filt,
        )
    except Exception as exc:  # surface a clean error to the caller
        return {"error": f"Export failed: {exc}"}

    return {
        "path": str(result.path),
        "memory_count": result.memory_count,
        "format": result.format.value,
        "compress": result.compress.value,
        "encrypted": result.encrypted,
        "bytes": result.bytes_written,
        "warnings": list(result.warnings),
    }


def _handle_import(mgr: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Dispatch helper for the ``vesma_import`` tool."""
    from vesmaro.cli.import_ import ImportMode, run_import

    source_path_str = args.get("source_path")
    if not source_path_str or not isinstance(source_path_str, str):
        return {"error": "source_path is required and must be a string"}
    source_path = Path(source_path_str)
    if not source_path.is_absolute():
        return {"error": f"source_path must be absolute, got: {source_path}"}

    mode = args.get("mode", "merge")
    if mode not in (ImportMode.MERGE, ImportMode.RESTORE):
        return {"error": f"Invalid mode '{mode}': must be 'merge' or 'restore'"}

    confirm = bool(args.get("confirm", False))
    # Hard gate: restore is destructive — refuse without explicit confirmation.
    if mode == ImportMode.RESTORE and not confirm:
        return {
            "error": (
                "Restore mode wipes all existing memories, vectors, and projects. "
                "Set confirm=true to acknowledge this and proceed."
            )
        }

    passphrase_env = args.get("passphrase_env")
    passphrase: str | None = None
    if passphrase_env:
        if not isinstance(passphrase_env, str) or not passphrase_env.isidentifier():
            return {
                "error": (
                    f"passphrase_env must be a valid environment variable name, "
                    f"got: {passphrase_env!r}"
                )
            }
        passphrase = os.environ.get(passphrase_env)
        if passphrase is None:
            return {
                "error": (
                    f"Environment variable {passphrase_env!r} (named by "
                    f"passphrase_env) is not set. The decryption passphrase "
                    f"must live in the environment, never in the tool arguments."
                )
            }

    try:
        result = run_import(
            mgr,
            source_path,
            mode=mode,
            overwrite=bool(args.get("overwrite", False)),
            confirm=confirm,
            dry_run=bool(args.get("dry_run", False)),
            passphrase=passphrase,
        )
    except Exception as exc:  # surface a clean error to the caller
        return {"error": f"Import failed: {exc}"}

    return {
        "mode": result.mode,
        "dry_run": result.dry_run,
        "imported": result.imported,
        "skipped": result.skipped,
        "updated": result.updated,
        "errors": list(result.errors),
        "warnings": list(result.warnings),
        "format_version": result.format_version,
        "mnemos_version": result.mnemos_version,
    }


def _parse_iso_arg(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp argument into an aware datetime.

    Returns ``None`` on a parse failure so the caller can emit a clean
    error instead of raising inside the dispatch path.
    """
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


async def _dispatch(name: str, args: dict[str, Any]) -> Any:
    mgr = get_manager()
    settings = mgr.settings
    # PG-0.5: the native auto-index hint rides EVERY dispatch — one
    # cut-in point, cheap (flag + queue put), failure-isolated below.
    _emit_codegraph_hint(mgr, args)

    # ── vesma_add ──────────────────────────────────────────────────────────
    if name == "vesma_add":
        raw_tags: list[str] = args.get("tags", [])
        # Enforce / patch TagContract
        tags = validate_tag_contract(
            raw_tags,
            strict=settings.mnemos.strict_tag_contract,
        )
        # Derive denormalised fields from validated tags
        project = next((t[len("project:") :] for t in tags if t.startswith("project:")), "")
        agent = next((t[len("agent:") :] for t in tags if t.startswith("agent:")), "")

        data = MemoryCreate(
            content=args["content"],
            title=args.get("title"),
            tags=tags,
            source=MemorySource.MCP,
            memory_type=MemoryType(args.get("memory_type", "note")),
            filter_profile=args.get("filter_profile"),
        )
        memory = mgr.add(data, project=project, agent=agent)
        # M10: report whether auto-filter ran and which profile was applied.
        # mgr.add() runs apply_context_filter internally when auto_filter is
        # enabled and reloads the memory, so filter_profile is populated on
        # success. On failure (non-fatal) filter_profile stays None.
        filtered = bool(
            settings.mnemos.auto_filter and memory.content and memory.filter_profile is not None
        )
        result = {
            "id": memory.id,
            "title": memory.auto_title(),
            "status": memory.status,
            "filtered": filtered,
            "filter_profile": memory.filter_profile,
        }
        _suffix = _steering_suffix(args, settings)
        if _suffix:
            result["_output_style_hint"] = _suffix
        return result

    # ── vesma_search ───────────────────────────────────────────────────────
    if name == "vesma_search":
        status_str = args.get("status")
        status: MemoryStatus | None = None
        if status_str:
            try:
                status = MemoryStatus(status_str)
            except ValueError:
                valid = ", ".join(s.value for s in MemoryStatus)
                return f"❌ Invalid status '{status_str}'. Valid values: {valid}"
        results = mgr.search(
            query=args["query"],
            tags=args.get("tags"),
            project=args.get("project"),
            task=args.get("task"),
            limit=args.get("limit", 10),
            include_raw=args.get("include_raw", False),
            status=status,
            refined_only=args.get("refined_only", False),
        )
        # ADR-0018 P1-b (M1 + review F1/F3): scan-at-issuance — BOTH echoed
        # strings (content and title; auto_title() derives from raw content)
        # are scanned/redacted per item; refuse mode drops the item
        # entirely (fail-closed); the drop log carries the memory id.
        _search_results = []
        for r in results:
            scan = mgr.scan_issuance_item(
                r.memory.effective_content(),
                title=r.memory.auto_title(),
                context=f"mcp:vesma_search:{r.memory.id}",
            )
            if scan.refused:
                continue
            item = {
                "id": r.memory.id,
                "title": scan.title,
                "content": scan.content,
                "tags": r.memory.tags,
                "score": r.score,
                "search_type": r.search_type,
                "status": r.memory.status,
                "redactions": scan.redactions,
            }
            if scan.redactions:
                item["redacted_patterns"] = scan.redacted_patterns
            _search_results.append(item)
        _suffix = _steering_suffix(args, settings)
        if _suffix:
            return {"results": _search_results, "_output_style_hint": _suffix}
        return _search_results

    # ── vesma_agent_recall (M3) ────────────────────────────────────────────
    if name == "vesma_agent_recall":
        recall_query = AgentRecallQuery(
            agent=args["agent"],
            project=args.get("project"),
            query=args.get("query"),
            task=args.get("task"),
            limit=args.get("limit", 20),
        )
        results = mgr.agent_recall(recall_query)
        # ADR-0018 P1-b (M1 + review F1/F3): scan-at-issuance on BOTH echoed
        # strings (content and title) — same policy as vesma_search.
        recalled = []
        for r in results:
            scan = mgr.scan_issuance_item(
                r.memory.effective_content(),
                title=r.memory.auto_title(),
                context=f"mcp:vesma_agent_recall:{r.memory.id}",
            )
            if scan.refused:
                continue
            item = {
                "id": r.memory.id,
                "title": scan.title,
                "content": scan.content,
                "tags": r.memory.tags,
                "created_at": r.memory.created_at.isoformat(),
                "status": r.memory.status,
                "redactions": scan.redactions,
            }
            if scan.redactions:
                item["redacted_patterns"] = scan.redacted_patterns
            recalled.append(item)
        return recalled

    # ── vesma_save_context ─────────────────────────────────────────────────
    if name == "vesma_save_context":
        project = args.get("project") or _detect_project()
        fields = {f: args.get(f) for f in CHECKPOINT_FIELDS}
        # canon v1.0.0: ``language`` is the per-call override of the
        # configured checkpoint body language (None → config default).
        # An invalid language (outside the canon enum) raises ValueError
        # in save_checkpoint — the SAME channel the identity/trivial-
        # reject ValueErrors already surface through (the dispatch-level
        # "❌ Error: ..." mapping, pre-W2 contract; do not intercept it
        # here or the SessionAgentMismatchError contract changes).
        memory, duplicate = mgr.save_checkpoint(
            fields,
            project=project,
            agent=args.get("agent"),
            session=args.get("session"),
            task=args.get("task"),
            # cascade QA P3-4 twin parity: REST /context/save stamps
            # SESSION_CONTEXT; the MCP twin now does the same (was the
            # NOTE default). Dedup is unaffected (hash = fields + agent
            # + project, no memory_type).
            memory_type=MemoryType.SESSION_CONTEXT,
            language=args.get("language"),
        )
        _track_call(is_save=True)
        instructions = _auto_collect_instructions(project) if _auto_collect_state["enabled"] else ""
        if duplicate:
            return (
                f"✅ Duplicate checkpoint (id={memory.id}, duplicate=true) — "
                f"identical checkpoint already stored, nothing new created.{instructions}"
            )
        return f"✅ Context saved (id={memory.id}).{instructions}"

    # ── vesma_recall_context ───────────────────────────────────────────────
    if name == "vesma_recall_context":
        project = args.get("project") or _detect_project()
        # Ф2 (epic #308): ValueError from the task boundary (unsalvageable
        # slug / prefix-carrying value) rides the SAME generic exception
        # path as every other manager boundary error on this surface —
        # the "❌ Error: ..." mapping below (the dispatch wrapper); do
        # not intercept it or the surface gains a second error contract.
        memories = mgr.recall_context(
            project=project, query=args.get("query"), task=args.get("task"), limit=5
        )
        if not memories:
            instructions = (
                _auto_collect_instructions(project) if _auto_collect_state["enabled"] else ""
            )
            return (
                f"No context found for project '{project}'. "
                f"Start by saving context with vesma_save_context.{instructions}"
                + _steering_suffix(args, settings)
            )
        out = [f"# Context for project '{project}'\n"]
        for m in memories:
            # ADR-0018 P1-b (M1 + review F3): scan-at-issuance on the echoed
            # content (this channel renders no titles); refuse mode drops
            # the memory's section, logged with the memory id.
            scan = mgr.scan_issuance(
                m.effective_content(),
                context=f"mcp:vesma_recall_context:{m.id}",
            )
            if scan.refused:
                continue
            out.append(f"---\n{scan.text}\n")
        instructions = _auto_collect_instructions(project) if _auto_collect_state["enabled"] else ""
        return "\n".join(out) + instructions + _steering_suffix(args, settings)

    # ── vesma_list_recent ──────────────────────────────────────────────────
    if name == "vesma_list_recent":
        memories = mgr.list_recent(
            limit=args.get("limit", 10),
            tags=args.get("tags"),
            project=args.get("project"),
            task=args.get("task"),
        )
        # ADR-0018 P1-b review (F1): this channel echoes titles
        # (auto_title() derives from raw content) and no content — the
        # title is scanned; refuse mode drops the row.
        listed = []
        for m in memories:
            scan = mgr.scan_issuance_item(
                None, title=m.auto_title(), context=f"mcp:vesma_list_recent:{m.id}"
            )
            if scan.refused:
                continue
            item = {
                "id": m.id,
                "title": scan.title,
                "tags": m.tags,
                "status": m.status,
                "created_at": m.created_at.isoformat(),
                "redactions": scan.redactions,
            }
            if scan.redactions:
                item["redacted_patterns"] = scan.redacted_patterns
            listed.append(item)
        return listed

    # ── vesma_list_tags ────────────────────────────────────────────────────
    if name == "vesma_list_tags":
        return mgr.list_tags()

    # ── vesma_tags (grouped: rename/remove/add) — pilot (vesma #97) ───────
    # Also serves as the backing dispatch for the legacy vesma_tags_rename
    # tool (non-breaking alias). When the LLM calls vesma_tags_rename we
    # inject action="rename" and fall through to the same handler.
    if name in ("vesma_tags", "vesma_tags_rename"):
        action = args.get("action")
        if name == "vesma_tags_rename":
            # Alias: legacy rename tool routes to the grouped rename path.
            # Force action='rename' AFTER merging args so a stray ``action``
            # key in a legacy rename call cannot leak through to the dispatcher.
            args = dict(args)
            args["action"] = "rename"
            action = "rename"
        if action == "rename":
            if not args.get("from_prefix") or not args.get("to_prefix"):
                return {
                    "error": "action='rename' requires 'from_prefix' and 'to_prefix' "
                    "(both must end with ':', e.g. 'gcw:' -> 'mnemos:')"
                }
            return mgr.tags_rename(
                from_prefix=args["from_prefix"],
                to_prefix=args["to_prefix"],
                subtypes=args.get("subtypes"),
                dry_run=args.get("dry_run", True),
                project=args.get("project"),
                agent=args.get("agent"),
                invalid_subtypes_to_legacy=args.get("invalid_subtypes_to_legacy", False),
            )
        if action == "remove":
            return mgr.tags_remove(
                tags=args.get("tags", []),
                wildcard=args.get("wildcard", False),
                dry_run=args.get("dry_run", True),
                project=args.get("project"),
                agent=args.get("agent"),
            )
        if action == "add":
            return mgr.tags_add(
                tags=args.get("tags", []),
                dry_run=args.get("dry_run", True),
                project=args.get("project"),
                agent=args.get("agent"),
            )
        return {"error": f"unknown action {action!r}. Valid actions: 'rename', 'remove', 'add'"}

    # ── vesma_workflow (grouped: set/get/history) — vesma #96 ────────────
    # Thin wrapper over MemoryManager.workflow_set / workflow_get /
    # workflow_history. The state machine + 5 guardrails are enforced
    # server-side in the manager, so this dispatch only translates
    # ValueError (guardrail violation) into a clean error dict — mirroring
    # how vesma_tags surfaces validation problems.
    if name == "vesma_workflow":
        action = args.get("action")
        memory_id = args.get("memory_id")
        if not memory_id:
            return {"error": "memory_id is required (the target memory id)"}
        if action == "set":
            to = args.get("to")
            actor = args.get("actor")
            if not to:
                return {"error": "action='set' requires 'to' (target workflow status)"}
            if not actor:
                return {"error": "action='set' requires 'actor' (free-form actor id)"}
            try:
                return mgr.workflow_set(
                    memory_id,
                    to,
                    actor=actor,
                    reason=args.get("reason", ""),
                    force=bool(args.get("force", False)),
                )
            except ValueError as exc:
                # Guardrail / state-machine violation — surface verbatim.
                return {"error": str(exc)}
        if action == "get":
            result = mgr.workflow_get(memory_id)
            if result is None:
                return {"error": f"memory {memory_id!r} not found"}
            return result
        if action == "history":
            return {
                "memory_id": memory_id,
                "history": mgr.workflow_history(memory_id, limit=int(args.get("limit", 50))),
            }
        return {"error": f"unknown action {action!r}. Valid actions: 'set', 'get', 'history'"}

    # ── vesma_stats ────────────────────────────────────────────────────────
    if name == "vesma_stats":
        return mgr.stats()
    # ── vesma_reprocess ─────────────────────────────────────────────────────
    if name == "vesma_reprocess":
        _project = args.get("project")
        _agent = args.get("agent")
        _limit = int(args.get("limit", 100))
        return mgr.run_pipeline(project=_project, agent=_agent, limit=_limit)
    # ── vesma_compress (P1-4 CCR) ───────────────────────────────────────────
    if name == "vesma_compress":
        # A2 issuer-ledger args: type-guarded at the boundary (the
        # vesma_context_rewrite pattern) so a malformed caller gets a
        # clean error dict instead of an AttributeError deep in the
        # manager's normalization.
        cp_agent = args.get("agent")
        cp_session = args.get("session")
        for label, value in (("agent", cp_agent), ("session", cp_session)):
            if value is not None and not isinstance(value, str):
                return {"error": f"{label} must be a string when provided"}
        return mgr.compress_content(
            args["text"],
            profile=args.get("profile"),
            project=args.get("project", "") or "",
            agent=cp_agent,
            session=cp_session,
        )
    # ── vesma_retrieve (P1-4 CCR) ───────────────────────────────────────────
    if name == "vesma_retrieve":
        # ADR-0018 P1-a: optional project scopes the cache lookup — a hash
        # cached under another project is reported as not found.
        # A2: marker metadata (original_chars/agent/session) + strict mode
        # run the marker-validation gate before any content is issued.
        # Boundary type guards (vesma_context_rewrite pattern).
        rt_agent = args.get("agent")
        rt_session = args.get("session")
        rt_chars = args.get("original_chars")
        for label, value in (("agent", rt_agent), ("session", rt_session)):
            if value is not None and not isinstance(value, str):
                return {"error": f"{label} must be a string when provided"}
        if rt_chars is not None and (not isinstance(rt_chars, int) or isinstance(rt_chars, bool)):
            return {"error": "original_chars must be an integer when provided"}
        # Review F3: no bool() coercion — the string "false" (or any
        # non-bool JSON value) is a boundary error, not a truthy opt-in.
        validate_marker_arg = args.get("validate_marker")
        if validate_marker_arg is not None and not isinstance(validate_marker_arg, bool):
            return {"error": "validate_marker must be a boolean when provided"}
        return mgr.retrieve_content(
            args["hash"],
            query=args.get("query"),
            snippet_count=args.get("snippet_count"),
            project=args.get("project"),
            validate_marker=validate_marker_arg,
            original_chars=rt_chars,
            agent=rt_agent,
            session=rt_session,
        )
    # ── vesma_filter (M10) ─────────────────────────────────────────────────
    if name == "vesma_filter":
        memory_id = args["memory_id"]
        filter_project_arg = args.get("project")
        if filter_project_arg is not None and not isinstance(filter_project_arg, str):
            return {"error": "project must be a string when provided"}
        # M1 (final review): the issuance twin — status gate (raw/archived
        # not filterable into context), optional caller-project scope, and
        # scan-at-issuance on the echoed clean_content (refuse mode drops
        # the content). apply_context_filter itself stays the maintenance
        # primitive (auto-filter on ingest, filter_all, CLI).
        result = mgr.issue_context_filter(
            memory_id,
            profile=args.get("profile"),
            budget=args.get("budget"),
            project=filter_project_arg,
            channel="mcp:vesma_filter",
        )
        if result.get("status") == "error":
            return result
        filter_item: dict[str, Any] = {
            "memory_id": memory_id,
            "profile": result["filter_profile"],
            "clean_content": result["clean_content"],
            "stats": result["stats"],
            "redactions": result["redactions"],
        }
        if result["redactions"]:
            filter_item["redacted_patterns"] = result["redacted_patterns"]
        return filter_item

    # ── vesma_ingest_url ───────────────────────────────────────────────────
    if name == "vesma_ingest_url":
        # Security: strip credentials from URL before storing (OWASP A02)
        import re as _re

        url = args["url"]
        url_clean = _re.sub(r"(https?://)([^@]*@)", r"\1", url)
        raw_tags = args.get("tags", [])
        tags = validate_tag_contract(
            raw_tags,
            strict=settings.mnemos.strict_tag_contract,
        )
        project = next((t[len("project:") :] for t in tags if t.startswith("project:")), "")
        agent = next((t[len("agent:") :] for t in tags if t.startswith("agent:")), "")
        memory = mgr.ingest_url(url_clean, tags=tags, project=project, agent=agent)
        # m2 (final review): auto_title() derives from the fetched page
        # content — the echoed title is scanned at issuance like every
        # other echoed string; refuse mode drops it (error shape, no echo).
        title_scan = mgr.scan_issuance_item(
            None, title=memory.auto_title(), context=f"mcp:vesma_ingest_url:{memory.id}"
        )
        if title_scan.refused:
            return {"error": f"issuance refused: {title_scan.reason}"}
        return {"id": memory.id, "title": title_scan.title, "url": url_clean}

    # ── vesma_ingest_document ──────────────────────────────────────────────
    if name == "vesma_ingest_document":
        # ADR-0027 Ф3 (epic #308): the DOCUMENT ingest — chunked,
        # born-quarantined, swept at completion. The single-URL
        # vesma_ingest_url above keeps its pre-Ф3 semantics untouched.
        raw_tags = args.get("tags", [])
        tags = validate_tag_contract(
            raw_tags,
            strict=settings.mnemos.strict_tag_contract,
        )
        project = next((t[len("project:") :] for t in tags if t.startswith("project:")), "")
        agent = next((t[len("agent:") :] for t in tags if t.startswith("agent:")), "")
        doc_id = str(args["doc_id"]).strip()
        if not doc_id:
            return {"error": "doc_id must be a non-empty string"}
        result = mgr.ingest_document(
            args["text"],
            doc_id=doc_id,
            title=args.get("title"),
            tags=tags,
            project=project,
            agent=agent,
            source_url=args.get("source_url"),
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

    # ── vesma_watch_* (project-graph poll, ADR-0032 §3.2) ──────────────────
    if name == "vesma_watch_start":
        from vesmaro.codegraph.service import (
            GraphDisabledError,
            GraphToolError,
        )

        legacy = [key for key in ("paths", "scan", "include_rules") if key in args]
        if legacy:
            return {
                "error": "watch_start is the project-graph poll registrar (ADR-0032 §3.2): "
                "pass project_id and agent. The M8 vault-watcher form "
                "(paths=/scan=/include_rules=) was never implemented and the "
                "stub it targeted is gone.",
                "code": "bad-request",
            }
        project_id = _graph_req_str(args, "project_id")
        if project_id is None:
            return {
                "error": "project_id is required and must be a non-empty string",
                "code": "bad-request",
            }
        watch_agent = _graph_req_str(args, "agent")
        if watch_agent is None:
            return {
                "error": "agent is required and must be a non-empty string "
                "(PG7 per-agent attribution is a binding)",
                "code": "attribution-required",
            }
        watch_session = args.get("session")
        if watch_session is not None and not isinstance(watch_session, str):
            return {"error": "session must be a string when provided", "code": "bad-request"}
        try:
            return mgr.watch_start(project_id, agent=watch_agent.strip(), session=watch_session)
        except GraphToolError as exc:
            payload = {"error": str(exc)}
            payload["code"] = "disabled" if isinstance(exc, GraphDisabledError) else "bad-request"
            return payload

    if name == "vesma_watch_stop":
        project_id = args.get("project_id")
        if project_id is not None and not isinstance(project_id, str):
            return {"error": "project_id must be a string when provided", "code": "bad-request"}
        mgr.watch_stop(project_id.strip() if isinstance(project_id, str) else None)
        return "✅ Watch stopped."

    if name == "vesma_watch_status":
        return mgr.watch_status()

    # ── vesma_auto_collect_status (M7) ─────────────────────────────────────
    if name == "vesma_auto_collect_status":
        calls = _checkpoint_tracker["calls_since_save"]
        elapsed = (
            time.monotonic() - _checkpoint_tracker["last_save_ts"]
            if _checkpoint_tracker["last_save_ts"]
            else 0.0
        )
        return {
            "auto_collect_enabled": _auto_collect_state["enabled"],
            "signals": {
                "call_counter": {
                    "calls_since_save": calls,
                    "threshold": _remind_calls(),
                    "triggered": calls >= _remind_calls(),
                },
                "elapsed_secs": {
                    "value": int(elapsed),
                    "threshold": _remind_secs(),
                    "triggered": elapsed > _remind_secs() and calls > 0,
                },
                # M7 additional signals (context-size, summary-marker, reference-drop)
                # are populated by the client plugin when it supplies those signals.
                "context_size_heuristic": {"value": None, "note": "populated by client (M7)"},
                "summary_marker_detected": {"value": None, "note": "populated by client (M7)"},
                "reference_drop_heuristic": {"value": None, "note": "populated by client (M7)"},
            },
            "recommendation": (
                "save_checkpoint"
                if (calls >= _remind_calls() or (elapsed > _remind_secs() and calls > 0))
                else "ok"
            ),
            "next_reminder_in_calls": max(0, _remind_calls() - calls),
        }

    # ── vesma_align_prefix (P1-5 CacheAligner) ──────────────────────────────
    if name == "vesma_align_prefix":
        return mgr.align_prefix(args["text"], profile=args.get("profile"))

    # ── vesma_assemble_context (ADR-0017 D1, #125) ─────────────────────────
    if name == "vesma_assemble_context":
        # Locals are suffixed: `project` is already bound as `str` by the
        # save/recall handlers above in this long dispatch function.
        asm_session = args.get("session")
        asm_project = args.get("project")
        if not isinstance(asm_session, str) or not asm_session.strip():
            return {"error": "session is required and must be a non-empty string"}
        if not isinstance(asm_project, str) or not asm_project.strip():
            return {"error": "project is required and must be a non-empty string"}
        asm_file = args.get("file")
        if asm_file is not None and not isinstance(asm_file, str):
            # Review F3: without the guard a non-str file reaches the
            # pipeline and dies as a TypeError echoing caller input.
            return {"error": "file must be a string when provided"}
        asm_agent = args.get("agent")
        if asm_agent is not None and not isinstance(asm_agent, str):
            return {"error": "agent must be a string when provided"}
        try:
            asm_result = mgr.assemble_context(
                session=asm_session,
                project=asm_project,
                file=asm_file,
                budget=int(args.get("budget", 2048)),
                mode=str(args.get("mode", "sync")),
                expand_ccr=bool(args.get("expand_ccr", False)),
                async_handle=args.get("async_handle"),
                agent=asm_agent,
            )
            # Vitals collection boundary (ADR-0026 phase A): record AFTER
            # the result exists — the pipeline itself stays write-free.
            # Non-fatal by contract; a no-op when the plane is disabled.
            mgr.record_assemble_vitals(asm_result)
            return asm_result
        except ValueError as exc:
            # Boundary validation (mode/budget/async_handle incl. the
            # session-bound handle check) — surface a clean error dict
            # instead of the generic exception path.
            return {"error": str(exc)}

    # ── vesma_context_rewrite (ADR-0018, #125 Wave 2) ──────────────────────
    if name == "vesma_context_rewrite":
        cr_content = args.get("content")
        cr_project = args.get("project")
        cr_agent = args.get("agent")
        if not isinstance(cr_content, str) or not cr_content.strip():
            return {"error": "content is required and must be a non-empty string"}
        if not isinstance(cr_project, str) or not cr_project.strip():
            return {"error": "project is required and must be a non-empty string"}
        if not isinstance(cr_agent, str) or not cr_agent.strip():
            return {"error": "agent is required and must be a non-empty string"}
        cr_session = args.get("session")
        cr_supersedes = args.get("supersedes")
        cr_diff = args.get("diff")
        optional_strs = (("session", cr_session), ("supersedes", cr_supersedes), ("diff", cr_diff))
        for label, value in optional_strs:
            if value is not None and (not isinstance(value, str) or not value.strip()):
                return {"error": f"{label} must be a non-empty string when provided"}
        try:
            return mgr.context_rewrite(
                content=cr_content,
                project=cr_project,
                agent=cr_agent,
                session=cr_session,
                supersedes=cr_supersedes,
                diff=cr_diff,
                include_marker=bool(args.get("include_marker", False)),
            )
        except ContextRewriteRateLimitError as exc:
            # W2 review F1: backpressure, not validation — a distinct,
            # machine-checkable flag so the harness can back off.
            return {"error": str(exc), "rate_limited": True}
        except ValueError as exc:
            # Boundary validation + size caps + tag-contract violations
            # (strict mode) + supersedes not found in project — clean
            # error dict, no trace echo.
            return {"error": str(exc)}

    # ── vesma_hooks (ADR-0017 D1 / ADR-0018 lifecycle hooks, #125 W3) ─────
    if name == "vesma_hooks":
        # Boundary type guards (vesma_context_rewrite pattern) so a
        # malformed caller gets a clean error dict instead of an
        # exception deep in the hooks. No bool() coercion — auto_compress
        # must be an actual bool, a truthy string is a boundary error.
        hk_action = args.get("action")
        if not isinstance(hk_action, str) or hk_action not in HOOK_ACTIONS:
            valid = ", ".join(HOOK_ACTIONS)
            return {"error": f"action must be one of: {valid}"}
        hk_session = args.get("session")
        if not isinstance(hk_session, str) or not hk_session.strip():
            return {"error": "session is required and must be a non-empty string"}
        hk_project = args.get("project")
        if not isinstance(hk_project, str) or not hk_project.strip():
            return {"error": "project is required and must be a non-empty string"}
        hk_agent = args.get("agent")
        if not isinstance(hk_agent, str) or not hk_agent.strip():
            return {"error": "agent is required and must be a non-empty string"}
        hk_optional_strs = (
            ("context_hint", args.get("context_hint")),
            ("file", args.get("file")),
            ("task", args.get("task")),
            ("tool_name", args.get("tool_name")),
            ("output_text", args.get("output_text")),
            ("profile", args.get("profile")),
        )
        for label, value in hk_optional_strs:
            if value is not None and not isinstance(value, str):
                return {"error": f"{label} must be a string when provided"}
        hk_budget = args.get("budget", 2048)
        if not isinstance(hk_budget, int) or isinstance(hk_budget, bool):
            return {"error": "budget must be an integer when provided"}
        hk_limit = args.get("limit", 5)
        if not isinstance(hk_limit, int) or isinstance(hk_limit, bool):
            return {"error": "limit must be an integer when provided"}
        hk_auto = args.get("auto_compress")
        if hk_auto is not None and not isinstance(hk_auto, bool):
            return {"error": "auto_compress must be a boolean when provided"}
        hk_awareness = args.get("include_awareness", False)
        if not isinstance(hk_awareness, bool):
            return {"error": "include_awareness must be a boolean when provided"}
        try:
            return dispatch_hook(
                mgr,
                action=hk_action,
                session=hk_session,
                project=hk_project,
                agent=hk_agent,
                context_hint=args.get("context_hint"),
                file=args.get("file"),
                budget=hk_budget,
                task=args.get("task"),
                limit=hk_limit,
                tool_name=args.get("tool_name"),
                output_text=args.get("output_text"),
                auto_compress=hk_auto,
                profile=args.get("profile"),
                include_awareness=hk_awareness,
            )
        except ValueError as exc:
            # Boundary + per-hook validation — clean error dict.
            return {"error": str(exc)}

    # ── vesma_awareness (vesma #254, R3 — awareness pre-flight) ──────────
    if name == "vesma_awareness":
        return _handle_awareness(mgr, args)

    # ── vesma_export (#84 federation export) ──────────────────────────────
    if name == "vesma_export":
        return _handle_export(mgr, args)

    # ── vesma_import (#84 federation import) ──────────────────────────────
    if name == "vesma_import":
        return _handle_import(mgr, args)

    # ── project graph (ADR-0032 §3.3 — the 10 tools) ───────────────────────
    if name in _GRAPH_TOOLS:
        return _handle_graph(name, mgr, args)

    return f"Unknown tool: {name}"


# ── project graph handlers (ADR-0032 PG-0 slice 4) ───────────────────────────
#
# The tools are thin MCP adapters over CodeGraphService (vesmaro.codegraph.
# service) — the policy layers (PG2 confinement, the §3.4 token contract,
# PG3 poisoning, PG4 issuance, PG7 attribution + audit) live in the service.
# Boundary guards here follow the vesma_hooks pattern: a malformed caller
# gets a clean error dict, never a traceback.

_GRAPH_TOOLS = frozenset(
    {
        "vesma_index_project",
        "vesma_project_graph_status",
        "vesma_search_graph",
        "vesma_trace_path",
        "vesma_get_file_outline",
        "vesma_get_code_snippet",
        "vesma_check_graph_coverage",
        "vesma_get_graph_schema",
        "vesma_list_graph_projects",
        "vesma_delete_graph_project",
        "vesma_register_project",
    }
)


def _graph_req_str(args: dict[str, Any], key: str) -> str | None:
    """A required non-empty string argument (None → boundary error)."""
    value = args.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _graph_req_int(args: dict[str, Any], key: str) -> int | None:
    """A required integer argument (None → boundary error)."""
    value = args.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _handle_graph(name: str, mgr: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Dispatch the project-graph tools (the ADR-0032 ten + #454
    ``vesma_register_project``) to CodeGraphService."""
    from vesmaro.codegraph.indexer import IndexLimitError
    from vesmaro.codegraph.service import (
        GraphAttributionError,
        GraphBudgetError,
        GraphConfinementError,
        GraphDisabledError,
        GraphToolError,
        get_graph_service,
    )

    agent = _graph_req_str(args, "agent")
    if agent is None:
        return {
            "error": "agent is required and must be a non-empty string "
            "(PG7 per-agent attribution is a binding)",
            "code": "attribution-required",
        }
    session = args.get("session")
    if session is not None and not isinstance(session, str):
        return {"error": "session must be a string when provided", "code": "bad-request"}

    def bad(key: str, kind: str) -> dict[str, Any]:
        return {"error": f"{key} is required and must be {kind}", "code": "bad-request"}

    common: dict[str, Any] = {"agent": agent.strip(), "session": session}
    try:
        if name == "vesma_index_project":
            incremental = args.get("incremental", True)
            if not isinstance(incremental, bool):
                return {"error": "incremental must be a boolean", "code": "bad-request"}
            project_id = _graph_req_str(args, "project_id")
            if project_id is None:
                return bad("project_id", "a non-empty string")
            return get_graph_service(mgr).index_project(
                project_id,
                incremental=incremental,
                reason=_optional_str(args.get("reason")),
                **common,
            )
        if name == "vesma_project_graph_status":
            project_id = _graph_req_str(args, "project_id")
            if project_id is None:
                return bad("project_id", "a non-empty string")
            return get_graph_service(mgr).status(project_id, **common)
        if name == "vesma_search_graph":
            project_id = _graph_req_str(args, "project_id")
            query = _graph_req_str(args, "query")
            if project_id is None or query is None:
                return bad("project_id, query", "non-empty strings")
            return get_graph_service(mgr).search_graph(
                project_id,
                query,
                kind=args.get("kind"),
                limit=args.get("limit", 50),
                cursor=args.get("cursor", 0),
                max_output_tokens=args.get("max_output_tokens"),
                include_signature=bool(args.get("include_signature", False)),
                **common,
            )
        if name == "vesma_trace_path":
            project_id = _graph_req_str(args, "project_id")
            qname = _graph_req_str(args, "qname")
            if project_id is None or qname is None:
                return bad("project_id, qname", "non-empty strings")
            return get_graph_service(mgr).trace_path(
                project_id,
                qname,
                depth=args.get("depth", 2),
                max_output_tokens=args.get("max_output_tokens"),
                **common,
            )
        if name == "vesma_get_file_outline":
            project_id = _graph_req_str(args, "project_id")
            path = _graph_req_str(args, "path")
            if project_id is None or path is None:
                return bad("project_id, path", "non-empty strings")
            return get_graph_service(mgr).get_file_outline(
                project_id,
                path,
                cursor=args.get("cursor", 0),
                max_output_tokens=args.get("max_output_tokens"),
                **common,
            )
        if name == "vesma_get_code_snippet":
            project_id = _graph_req_str(args, "project_id")
            path = _graph_req_str(args, "path")
            start_line = _graph_req_int(args, "start_line")
            end_line = _graph_req_int(args, "end_line")
            if project_id is None or path is None or start_line is None or end_line is None:
                return bad("project_id, path, start_line, end_line", "strings / integers")
            return get_graph_service(mgr).get_code_snippet(
                project_id,
                path,
                start_line,
                end_line,
                max_output_tokens=args.get("max_output_tokens"),
                **common,
            )
        if name == "vesma_check_graph_coverage":
            project_id = _graph_req_str(args, "project_id")
            paths = args.get("paths")
            if (
                project_id is None
                or not isinstance(paths, list)
                or not all(isinstance(p, str) for p in paths)
            ):
                return bad("project_id, paths", "a string and a list of strings")
            return get_graph_service(mgr).check_coverage(project_id, paths, **common)
        if name == "vesma_get_graph_schema":
            return get_graph_service(mgr).get_graph_schema(args.get("project_id"), **common)
        if name == "vesma_list_graph_projects":
            return get_graph_service(mgr).list_graph_projects(**common)
        if name == "vesma_register_project":
            project_id = _graph_req_str(args, "project_id")
            root = _graph_req_str(args, "root")
            if project_id is None or root is None:
                return bad("project_id, root", "non-empty strings")
            return get_graph_service(mgr).register_project(project_id, root, **common)
        # name == "vesma_delete_graph_project"
        project_id = _graph_req_str(args, "project_id")
        if project_id is None:
            return bad("project_id", "a non-empty string")
        return get_graph_service(mgr).delete_graph_project(
            project_id, reason=_optional_str(args.get("reason")), **common
        )
    except GraphToolError as exc:
        payload = {"error": str(exc)}
        if isinstance(exc, GraphDisabledError):
            payload["code"] = "disabled"
        elif isinstance(exc, GraphAttributionError):
            payload["code"] = "attribution-required"
        elif isinstance(exc, GraphConfinementError):
            payload["code"] = "confinement-refused"
        elif isinstance(exc, GraphBudgetError):
            payload["code"] = "budget-refused"
        else:
            payload["code"] = "refused"
        return payload
    except IndexLimitError as exc:
        # PG7 fail-closed: the whole index refused, the previous graph
        # survives — surfaced as a clean refusal, not a traceback.
        return {"error": str(exc), "code": "limit-refused"}


def _optional_str(raw: Any) -> str | None:
    """Pass-through for optional string args (None when absent)."""
    return raw if isinstance(raw, str) and raw.strip() else None


# ── MCP SDK 2.x server wiring (#185) ───────────────────────────────────────────
#
# SDK 2.x registers handlers as constructor kwargs instead of decorators.
# The thin adapters below translate between the SDK request/response models
# and the plain callables above (`list_tools` / `call_tool`), which stay
# importable with their pre-port signatures.

_CTX_T = Any  # ServerRequestContext — untyped boundary, same as 1.x decorators


async def _on_list_tools(ctx: _CTX_T, params: PaginatedRequestParams | None) -> ListToolsResult:
    """SDK 2.x ``on_list_tools`` handler — wraps :func:`list_tools`."""
    tools = await list_tools()
    return ListToolsResult(tools=tools)


async def _on_call_tool(ctx: _CTX_T, params: CallToolRequestParams) -> CallToolResult:
    """SDK 2.x ``on_call_tool`` handler — wraps :func:`call_tool`."""
    arguments = dict(params.arguments) if params.arguments else {}
    content: list[Any] = list(await call_tool(params.name, arguments))
    return CallToolResult(content=content)


server = Server(
    "vesma",
    version=__version__,
    on_list_tools=_on_list_tools,
    on_call_tool=_on_call_tool,
)


# ── Entry point ────────────────────────────────────────────────────────────────


async def main() -> None:
    """Run the Vesma MCP server over stdio."""
    from vesmaro.logging_setup import setup_logging

    settings = load_settings()
    setup_logging(settings)
    # Start the background processor so raw entries are automatically
    # clustered → synthesized → quality-gated → published.
    mgr = get_manager()
    mgr.start_background_processor()
    # Start the background secrets scanner (Layer 2 defence-in-depth,
    # #89). No-op when ``scanner.enabled`` is False. Runs on its own
    # daemon thread so it never blocks the MCP stdio loop.
    from vesmaro.scanner_runtime import get_scanner

    scanner = get_scanner(mgr)
    scanner.start()
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        scanner.stop()
        mgr.stop_background_processor()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
