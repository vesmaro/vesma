"""Native awareness heartbeat — the MCP ``call_tool`` injection glue.

ADR-0035 W2a wave 0 (shadow). The heartbeat rides EVERY MCP tool
response through a single injection point (``mcp_server.call_tool``);
this module is everything that glue needs beyond the pure
:func:`vesma.awareness.compose_heartbeat` contour: identity
extraction from tool arguments, the C13 deny-list, the mode ladder
(off / shadow / canary / on), the tail text, and the
ADR-0026-family event writes into the metrics sidecar (``tool_call``
for every dispatched call — the funnel denominator; ``peer_write`` on
write-class verbs; the compose's own ``delta_available`` /
``heartbeat_delivery`` / ``heartbeat_suppressed`` events). Zero peer
content in any event (CWE-359): counters, enums, tool ids and the
caller's identity slugs only.

Hard contract (the wrapper's side of ADR-0035):

* :func:`native_heartbeat_tail` NEVER raises — a heartbeat failure is
  suppressed with a warning, the tool call it rides is untouched (the
  ``_emit_codegraph_hint`` never-breaks-the-call discipline);
* ``mode=off`` (the default, the kill switch) is fully inert: no
  probe, no cursor, no events, no tail — engine behaviour
  byte-identical to the pre-ADR-0035 build (CI-pinned);
* ``shadow`` composes and records events but renders NOTHING — wave 0
  is the measuring wave;
* ``canary``/``on`` yield the tail text — ``call_tool`` appends it as
  ONE TextContent, the LAST element of the response (tail-LAST,
  ADR-0028);
* the deny-list surfaces (``vesma_assemble_context`` — it already
  composes the full picture, a tail there means double render and
  double cursor advance; export/import — the bulk transfer pair, the
  MCP legs of the federation class; ``mnemos_awareness`` /
  ``mnemos_hooks`` — the awareness surfaces themselves, cascade SEC-2)
  never carry the tail (but still land in the ``tool_call``
  denominator).

The REST leg carries no tail in v1 (a silent text tail would break the
typed JSON contract — ADR-0035 Configuration).
"""

from __future__ import annotations

import logging
from typing import Any, Final

from vesma.awareness import compose_heartbeat, sanitize_project_id

logger = logging.getLogger(__name__)

#: C13 deny-list — surfaces that NEVER carry the heartbeat tail.
HEARTBEAT_DENY_TOOLS: Final[frozenset[str]] = frozenset(
    {
        # Already composes the full awareness picture inside its own
        # handler: attaching the tail here is double render + double
        # cursor advance (the C13 ruling).
        "vesma_assemble_context",
        # Bulk transfer / federation class: a tail on a bulk export or
        # restore response is noise on a surface whose contract is
        # metadata-only.
        "vesma_export",
        "vesma_import",
        # The awareness surfaces themselves (cascade SEC-2, ADR-0035 W1):
        # ``vesma_awareness`` pre_flight IS the depth render, and under
        # canary/on a hooks pre_llm_call/on_session_start composes its own
        # awareness section by default (the mode-linked include_awareness
        # default) — a native tail on those responses is a double render
        # on one surface, the C13 assemble_context ruling verbatim. They
        # still land in the ``tool_call`` denominator.
        "vesma_awareness",
        "vesma_hooks",
    }
)

#: Write-class tools for the ``peer_write`` event (the freshness metric
#: numerator: peer_write.ts → delivery.ts). The row-creating surfaces
#: only — compress mints SYNTHESIZED projections the delta excludes,
#: tags/reprocess mutate rather than write new peer rows.
HEARTBEAT_WRITE_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "vesma_add",
        "vesma_save_context",
        "vesma_ingest_url",
        "vesma_ingest_document",
    }
)

#: Modes that append the TextContent. ``shadow`` composes and measures
#: but renders nothing; ``off`` never reaches this module at all.
HEARTBEAT_RENDERING_MODES: Final[frozenset[str]] = frozenset({"canary", "on"})


def _tag_value(args: dict[str, Any], prefix: str) -> str | None:
    """Identity from the tag list (the ``vesma_add`` convention)."""
    tags = args.get("tags")
    if not isinstance(tags, list):
        return None
    for tag in tags:
        if isinstance(tag, str) and tag.startswith(prefix):
            value = tag[len(prefix) :]
            if value.strip():
                return value
    return None


def _identity(args: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Extract ``(project, agent, session)`` from tool arguments.

    Explicit arguments first (``project``/``project_id``/``agent``/
    ``session``); the tag-derived legs (``project:``/``agent:``) cover
    the ``vesma_add`` convention where identity rides validated tags.
    NO cwd fallback, deliberately — unlike the codegraph indexing hint,
    the heartbeat keys PERSISTENT cursor state on ``(project, agent)``
    and must not mint it from a working-directory guess. A call without
    identity simply does not heartbeat (recorded only as a session-less
    ``tool_call`` in the events wave).
    """
    project = args.get("project") or args.get("project_id")
    if not isinstance(project, str) or not project.strip():
        project = _tag_value(args, "project:")
    agent = args.get("agent")
    if not isinstance(agent, str) or not agent.strip():
        agent = _tag_value(args, "agent:")
    session = args.get("session")
    if not isinstance(session, str) or not session.strip():
        session = None
    project = project.strip() if isinstance(project, str) and project.strip() else None
    agent = agent.strip() if isinstance(agent, str) and agent.strip() else None
    # C11 / cascade SEC-1: the project slug keys the persistent cursor,
    # the ledger and the event rows — it is client-supplied text and
    # never rides raw (a hostile slug must not forge envelope lines or
    # split event columns).
    project = sanitize_project_id(project) if project else None
    return project, agent, session


def _record_event(
    mgr: Any,
    kind: str,
    *,
    project: str | None,
    agent: str | None,
    session: str | None,
    meta: dict[str, Any] | None,
) -> None:
    """Persist one contour event through the vitals plane (non-fatal).

    The guest contract double-belted: the manager method already swallows
    everything; this belt exists so a mock/alt manager without the method
    (tests, exotic embeddings) degrades silently instead of killing the
    heartbeat — and with it the tool call it rides.
    """
    try:
        recorder = getattr(mgr, "record_awareness_event", None)
        if recorder is None:
            return
        recorder(kind=kind, project=project, agent=agent, session=session, meta=meta)
    except Exception:
        logger.warning(
            "awareness heartbeat: event record failed (non-fatal) kind=%s", kind, exc_info=True
        )


def native_heartbeat_tail(tool_name: str, args: dict[str, Any]) -> str | None:
    """One MCP response heartbeat — returns the tail TEXT or None.

    The single entry point ``mcp_server.call_tool`` calls with the
    CANONICAL tool name (aliases normalized) and the raw arguments, and
    wraps a non-None return into the response's last TextContent
    itself — the ADR-0023 isolation ruling keeps every mcp SDK import
    inside ``vesma.mcp_server``, so this module deals in plain text.
    Never raises; every failure mode degrades to ``None`` (no tail)
    with a warning — the tool call's bytes are sacred.
    """
    try:
        return _native_heartbeat_tail(tool_name, args)
    except Exception:
        logger.warning(
            "awareness heartbeat: unexpected failure suppressed (non-fatal), tool=%s",
            tool_name,
            exc_info=True,
        )
        return None


def _native_heartbeat_tail(tool_name: str, args: dict[str, Any]) -> str | None:
    from vesma.mcp_server import get_manager  # local: mcp_server imports this module

    mgr = get_manager()
    mode = getattr(getattr(mgr, "settings", None), "awareness", None)
    mode_value = getattr(mode, "native_heartbeat_mode", "off")
    if mode_value == "off":
        return None  # the kill switch: fully inert — no events either

    project, agent, session = _identity(args)

    # The funnel denominator first: EVERY dispatched call is a tool_call
    # event (deny-listed and identity-less calls included — delivery
    # rates are honest only against the full denominator). Write-class
    # verbs emit peer_write (the freshness numerator's start stamp).
    _record_event(
        mgr, "tool_call", project=project, agent=agent, session=session, meta={"tool": tool_name}
    )
    if tool_name in HEARTBEAT_WRITE_TOOLS:
        _record_event(
            mgr,
            "peer_write",
            project=project,
            agent=agent,
            session=session,
            meta={"tool": tool_name},
        )

    if tool_name in HEARTBEAT_DENY_TOOLS:
        return None
    if not (project and agent):
        return None

    result = compose_heartbeat(mgr, project=project, agent=agent, tool=tool_name)
    for kind, meta in result.get("events", []):
        _record_event(mgr, kind, project=project, agent=agent, session=session, meta=dict(meta))

    if mode_value not in HEARTBEAT_RENDERING_MODES:
        return None  # shadow: the contour ran (events recorded), nothing renders

    return result.get("text") or None
