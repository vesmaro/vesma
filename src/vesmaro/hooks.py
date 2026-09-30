"""ADR-0017 D1 / ADR-0018 — server-side lifecycle hooks (mnemos #125, Wave 3).

Three integration points the harness / automation calls at the moments
of its own lifecycle. Each is a THIN wrapper over an existing manager
path — no hook owns retrieval, filtering, scanning, or compression
logic of its own:

* ``pre_llm_call``      → ``MemoryManager.assemble_context`` (W1 D1
  pipeline: recall → optional CCR → filter → MANDATORY scan → align →
  budget). Returns the assembled block; the harness injects ``text``
  into its prompt before the model call. Delivery is pinned to SYNC —
  the hook call must return something injectable now; async handles are
  manual orchestration via ``mnemos_assemble_context`` and are
  deliberately not exposed here.
* ``on_session_start``  → ``MemoryManager.recall_context`` (recent
  checkpoints). Returns session bootstrap state.
* ``post_tool_call``    → ``MemoryManager.compress_content`` (CCR) when
  autocompression is enabled — THE autocompression entry point
  (ADR-0018 Phase 1). Returns the CCR envelope; the harness substitutes
  ``compressed_text`` (marker-headed) for the raw tool output in its
  window.

Identity is REQUIRED on every hook call (``session`` + ``project`` +
``agent``). For ``post_tool_call`` this is not ergonomics — it is the
A2 register N2 MANDATE (ADR-0018 residual register, ArchCom
2026-08-27): an identity-less compress call mints a NULL-issuer cache
row, and NULL-issuer rows are UNVERIFIABLE under strict marker
validation — identity-less automation compression would silently brick
its own future rehydrations. Every ``compress_content`` call from this
module carries the caller's ``(agent, session)``; the hook boundary
refuses calls without them.

Entry invariant (ADR-0018): every LTM → context path passes secret
scan, provenance wrapper, and status gate — VERIFIED per hook, not
re-implemented:

* ``pre_llm_call`` — the scan/provenance/status stages live INSIDE
  ``assemble_context`` (``_scan_stage``, ``build_provenance``,
  ``CONTEXT_ADMISSIBLE_STATUSES`` recall gate); the hook adds nothing
  and removes nothing.
* ``on_session_start`` — ``recall_context`` returns raw stored rows
  (the recall path itself has no issuance scan), so THIS channel owns
  the boundary scan — mirroring ``mnemos_recall_context`` / POST
  ``/context/recall``: each checkpoint's content is
  ``scan_issuance``-scanned before it enters the response; refuse mode
  drops the checkpoint (logged with the memory id).
* ``post_tool_call`` — the compressed output is the CALLER'S content,
  not an LTM read (no entry-invariant path); the LTM write inside
  ``compress_content`` runs the Layer-1 store-time scan
  (``ccr_store`` verdict) and records the A2 issuer ledger from the
  threaded identity.

Configuration (``hooks.`` section, :class:`vesmaro.config.HooksConfig`):
minimal by design — two knobs: ``hooks.auto_compress`` (default False;
the per-call ``auto_compress`` argument overrides it per invocation) and
``hooks.max_output_chars`` (default 1,048,576 chars — the post_tool_call
size cap, W3 review F3). The read-only hooks need no enablement: they
add no capability the server surfaces do not already expose.

Surfaces: one grouped MCP tool ``mnemos_hooks`` with
``action: enum [pre_llm_call, on_session_start, post_tool_call]``
(the mnemos #97 action:enum pattern — NOT oneOf), and REST
``POST /hooks/{action}`` (one parametric route — the three actions
share the session/project/agent spine; three literal routes would
triplicate the same body model). Both call :func:`dispatch_hook`.

Awareness composition (mnemos #254, R3): ``pre_llm_call`` and
``on_session_start`` take ``include_awareness`` (default False) — the
awareness presence/delta section composes HERE, at the hook, never as
a seventh assemble stage. See :mod:`vesmaro.awareness` for the R3
security contour; with the flag off every output is byte-identical to
the pre-#254 shape.

Modes: ADR-0017 D1 names sync/async hook modes. This wave implements
SYNC only (the automation deployments W3 targets are synchronous
request/response); async hook delivery waits for a consumer that needs
it — flagged for ratification in the #125 report.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from vesmaro.manager import MemoryManager

logger = logging.getLogger(__name__)

#: Valid ``mnemos_hooks`` actions (the #97 action:enum surface).
HOOK_ACTIONS: Final[tuple[str, ...]] = (
    "pre_llm_call",
    "on_session_start",
    "post_tool_call",
)

#: Default checkpoint count for ``on_session_start``.
SESSION_START_LIMIT: int = 5


def _require_identity(session: str, project: str, agent: str) -> None:
    """Boundary validation shared by every hook (identity is mandatory)."""
    for label, value in (("session", session), ("project", project), ("agent", agent)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} is required and must be a non-empty string")


# ── pre_llm_call ──────────────────────────────────────────────────────────────


def pre_llm_call(
    mgr: MemoryManager,
    *,
    session: str,
    project: str,
    agent: str,
    context_hint: str | None = None,
    file: str | None = None,
    budget: int = 2048,
    task: str | None = None,
    include_awareness: bool = False,
) -> dict[str, Any]:
    """Assemble the context block to inject before the model call (sync).

    Thin wrapper over ``MemoryManager.assemble_context`` with the
    caller's identity threaded (``agent`` + ``session`` run the A2
    strict-mode CCR expansion gate under ``ccr.validate_markers``) and
    ``context_hint`` as the explicit recall query — what the upcoming
    model call is about, so recall finds semantically relevant entries
    instead of deriving a query from the project slug. Delivery is
    pinned to sync (see the module docstring).

    ``task`` (ADR-0027 Phase 0, epic #308) is the harness-passed task
    identifier — the bare task slug. It narrows recall to rows carrying
    ``task:<slug>`` (intersection doctrine; tail-only — every pinned
    prefix and the provenance format are untouched). ``None`` (default)
    keeps the pre-Phase-0 output byte-identical: no ``task`` key.

    The returned ``text`` is the injection suggestion: provenance-
    wrapped, filter-cleaned, secret-scanned, budget-bounded. The
    harness decides whether and where to inject it.

    ``include_awareness=True`` (mnemos #254, R3 — default False)
    composes the awareness delta section AFTER the assembled output:
    per-agent delta blocks are appended to ``blocks`` and the rendered
    section to ``text`` — awareness renders LAST, never inside the
    pinned lane prefix (the E1 guard is re-asserted over the composed
    list), and the awareness cursor advances to the consumed
    high-water mark. With the flag off the output is byte-identical to
    the pre-#254 shape: no ``awareness`` key, no extra blocks (pinned
    by tests).
    """
    _require_identity(session, project, agent)
    if context_hint is not None and not context_hint.strip():
        raise ValueError("context_hint must be a non-empty string when provided")

    # PG-0.5 (owner directive 2026-09-29): native auto-index hint — the
    # hook mirror of the MCP dispatcher cut-in. Non-blocking, never
    # raises: a hint must never break the model-call assembly.
    try:
        mgr.codegraph_activity_hint(project, cwd=os.getcwd(), agent=agent, session=session)
    except Exception:
        logger.debug("hooks.pre_llm_call: codegraph hint skipped", exc_info=True)

    result = mgr.assemble_context(
        session=session,
        project=project,
        file=file,
        budget=budget,
        mode="sync",
        agent=agent,
        query=context_hint,
        task=task,
    )
    # Vitals collection boundary (ADR-0026 phase A): record after the
    # result exists — non-fatal, no-op when the plane is disabled.
    mgr.record_assemble_vitals(result)
    result["hook"] = "pre_llm_call"
    result["injection"] = "prepend result['text'] to the model call prompt"
    if include_awareness:
        # Hooks composition (R3): awareness is NOT an assemble stage —
        # it composes at this hook, after the fixed six-stage pipeline.
        from vesmaro.awareness import assert_awareness_tail, compose_pre_llm_awareness

        awr = compose_pre_llm_awareness(mgr, session=session, project=project, agent=agent)
        blocks: list[dict[str, Any]] = [*result.get("blocks", []), *awr["blocks"]]
        assert_awareness_tail(blocks)
        result["blocks"] = blocks
        if awr["text"]:
            result["text"] = f"{result['text']}\n\n{awr['text']}" if result["text"] else awr["text"]
        result["awareness"] = awr["meta"]
    logger.info(
        "hooks.pre_llm_call: session=%s project=%s agent=%s hint=%s blocks=%d tokens=%d "
        "awareness=%s",
        session,
        project,
        agent,
        "yes" if context_hint else "no",
        len(result.get("blocks", [])),
        result.get("tokens", {}).get("estimated", 0),
        "on" if include_awareness else "off",
    )
    return result


# ── on_session_start ──────────────────────────────────────────────────────────


def on_session_start(
    mgr: MemoryManager,
    *,
    session: str,
    project: str,
    agent: str,
    limit: int = SESSION_START_LIMIT,
    include_awareness: bool = False,
) -> dict[str, Any]:
    """Recall the session bootstrap state — recent checkpoints/context.

    Thin wrapper over ``MemoryManager.recall_context``. The recall path
    returns raw stored rows, so THIS channel owns the entry-invariant
    boundary scan (mirroring ``mnemos_recall_context``): each
    checkpoint's content is ``scan_issuance``-scanned before it enters
    the response; refuse mode drops the checkpoint (logged with the
    memory id), redactions are counted per checkpoint.

    ``include_awareness=True`` (mnemos #254, R3 — default False) adds a
    ``presence`` section: server-observed neighbor activity in the
    presence window plus deterministic conflict-hints against my last
    checkpoint goal. Pure read — the awareness cursor is NOT touched
    here (consumption is the ``pre_llm_call`` composition's job).
    """
    _require_identity(session, project, agent)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError(f"limit must be an integer >= 1, got {limit!r}")

    memories = mgr.recall_context(project=project, limit=limit)
    checkpoints: list[dict[str, Any]] = []
    total_redactions = 0
    for m in memories:
        scan = mgr.scan_issuance(m.effective_content(), context=f"hooks:on_session_start:{m.id}")
        if scan.refused:
            continue
        item: dict[str, Any] = {
            "id": m.id,
            "content": scan.text,
            "created_at": m.created_at.isoformat(),
            "redactions": scan.redactions,
        }
        if scan.redactions:
            item["redacted_patterns"] = scan.redacted_patterns
        checkpoints.append(item)
        total_redactions += scan.redactions

    logger.info(
        "hooks.on_session_start: session=%s project=%s agent=%s recalled=%d "
        "issued=%d redactions=%d",
        session,
        project,
        agent,
        len(memories),
        len(checkpoints),
        total_redactions,
    )
    result: dict[str, Any] = {
        "hook": "on_session_start",
        "session": session,
        "project": project,
        "agent": agent,
        "checkpoints": checkpoints,
        "redactions": total_redactions,
    }
    if include_awareness:
        # C9 (swarm v0a review P2-1, issue #414): on_session_start is a
        # POLLABLE surface — the REST twin lets a harness re-request
        # this hook freely, so the picture cap must gate THIS path too
        # (the v0a claim that the calling surface already gated it held
        # for pre_llm_call, not for this hook). Refusal degrades to the
        # rate-limited line, never a hard error — the hook's response
        # contract stays shape-stable (module-level ``datetime`` keeps
        # the gate freezable the same way ``vesmaro.manager.datetime``
        # is in tests).
        from vesmaro.awareness import (
            AWARENESS_DISCLAIMER,
            PICTURE_RATE_LIMITED_LINE,
            _picture_rate_refused,
            compose_session_presence,
        )

        now_dt = datetime.now(UTC)
        if _picture_rate_refused(mgr, project=project, agent=agent, now_dt=now_dt):
            result["presence"] = {
                "hook": "on_session_start",
                "rate_limited": True,
                "text": PICTURE_RATE_LIMITED_LINE,
                "disclaimer": AWARENESS_DISCLAIMER,
            }
        else:
            result["presence"] = compose_session_presence(mgr, project=project, agent=agent)
    return result


# ── post_tool_call ────────────────────────────────────────────────────────────


def post_tool_call(
    mgr: MemoryManager,
    *,
    session: str,
    project: str,
    agent: str,
    tool_name: str,
    output_text: str,
    auto_compress: bool | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """The ADR-0018 autocompression entry point.

    When ``auto_compress`` resolves true (per-call argument, else the
    ``hooks.auto_compress`` config knob, default False), the tool output
    is compressed via ``MemoryManager.compress_content`` and the CCR
    envelope is returned — the harness substitutes ``compressed_text``
    (marker-headed) for the raw output in its window; the marker is the
    on-demand rehydrate handle (``mnemos_retrieve``).

    ⚠ N2 MANDATE (ADR-0018 residual register): the compress call ALWAYS
    carries the caller's ``(agent, session)`` — identity-less
    compression mints NULL-issuer cache rows that strict marker
    validation (``ccr.validate_markers``) refuses to redeem. The hook
    boundary REQUIRES both; there is no identity-less mode.

    Memory capture (ADR-0017 D1 "capture results as memories, opt-in")
    is deliberately NOT wired to a knob in this wave: an explicit
    ``MnemosSDK.remember`` call is strictly more controllable than an
    implicit write on every tool result. Flagged for ratification.

    Size cap (W3 review F3): ``hooks.max_output_chars`` (default
    1,048,576 chars — the context_rewrite caps convention) rejects an
    oversized ``output_text`` at the hook boundary BEFORE any write
    (``ValueError`` → 422 / MCP error dict); nothing reaches
    ``ccr_store`` / FTS. The cap applies REGARDLESS of the
    ``auto_compress`` resolution — the boundary rejects the payload
    before the mode is even consulted (a 10 MB output is rejected on an
    off-hook call too, so the harness learns the contract early). 0
    disables.
    """
    _require_identity(session, project, agent)
    if not isinstance(tool_name, str) or not tool_name.strip():
        raise ValueError("tool_name is required and must be a non-empty string")
    if not isinstance(output_text, str):
        raise ValueError("output_text is required and must be a string")
    max_chars = mgr.settings.hooks.max_output_chars
    if max_chars and len(output_text) > max_chars:
        logger.warning(
            "hooks.post_tool_call: output_text over cap (%d > %d) — rejected, "
            "no write; session=%s project=%s agent=%s tool=%s",
            len(output_text),
            max_chars,
            session,
            project,
            agent,
            tool_name,
        )
        raise ValueError(
            f"output_text exceeds hooks.max_output_chars ({len(output_text)} > {max_chars})"
        )

    enabled = mgr.settings.hooks.auto_compress if auto_compress is None else auto_compress
    if not enabled:
        logger.info(
            "hooks.post_tool_call: session=%s project=%s agent=%s tool=%s "
            "auto_compress=off — output returned as-is",
            session,
            project,
            agent,
            tool_name,
        )
        return {
            "hook": "post_tool_call",
            "session": session,
            "project": project,
            "agent": agent,
            "tool_name": tool_name,
            "auto_compress": False,
            "compressed": False,
            "note": (
                "autocompression off (pass auto_compress=true or set "
                "hooks.auto_compress); output_text unchanged"
            ),
        }

    ccr = mgr.compress_content(
        output_text,
        profile=profile,
        project=project,
        agent=agent,
        session=session,
    )
    logger.info(
        "hooks.post_tool_call: session=%s project=%s agent=%s tool=%s "
        "auto_compress=on cached=%s reduction_pct=%s dropped_items=%d",
        session,
        project,
        agent,
        tool_name,
        ccr.get("cached"),
        ccr.get("reduction_pct"),
        int(ccr.get("dropped_items", 0)),
    )
    return {
        "hook": "post_tool_call",
        "session": session,
        "project": project,
        "agent": agent,
        "tool_name": tool_name,
        "auto_compress": True,
        "compressed": bool(ccr.get("cached")),
        # Substitute instruction for the harness: the marker-headed
        # compressed body replaces the raw tool output in the window.
        "action": "substitute output_text with compressed_text in the window",
        "ccr": ccr,
        "compressed_text": ccr["compressed_text"],
        "marker": ccr["marker"],
    }


# ── Grouped dispatch (mnemos_hooks action:enum — the #97 pattern) ─────────────


def dispatch_hook(
    mgr: MemoryManager,
    *,
    action: str,
    session: str,
    project: str,
    agent: str,
    context_hint: str | None = None,
    file: str | None = None,
    budget: int = 2048,
    task: str | None = None,
    limit: int = SESSION_START_LIMIT,
    tool_name: str | None = None,
    output_text: str | None = None,
    auto_compress: bool | None = None,
    profile: str | None = None,
    include_awareness: bool = False,
) -> dict[str, Any]:
    """Route one ``mnemos_hooks`` action to its hook function.

    Single authority for the action surface shared by the MCP tool and
    the REST route; per-action arguments are validated inside each hook
    (``ValueError`` → MCP ``{"error": …}`` / REST 422 at the callers).
    ``task`` (ADR-0027 Phase 0) is a ``pre_llm_call``-only argument —
    irrelevant fields for the requested action are simply ignored.
    """
    if action == "pre_llm_call":
        return pre_llm_call(
            mgr,
            session=session,
            project=project,
            agent=agent,
            context_hint=context_hint,
            file=file,
            budget=budget,
            task=task,
            include_awareness=include_awareness,
        )
    if action == "on_session_start":
        return on_session_start(
            mgr,
            session=session,
            project=project,
            agent=agent,
            limit=limit,
            include_awareness=include_awareness,
        )
    if action == "post_tool_call":
        if tool_name is None or output_text is None:
            raise ValueError("action 'post_tool_call' requires 'tool_name' and 'output_text'")
        return post_tool_call(
            mgr,
            session=session,
            project=project,
            agent=agent,
            tool_name=tool_name,
            output_text=output_text,
            auto_compress=auto_compress,
            profile=profile,
        )
    valid = ", ".join(HOOK_ACTIONS)
    raise ValueError(f"unknown hook action {action!r}; valid actions: {valid}")
