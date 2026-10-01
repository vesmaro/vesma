"""ADR-0025 E1 — deterministic retrieval lanes (mnemos #253).

Lanes are a **recall SUB-STAGE** of ``assemble_context`` (``assemble.py``):
when ``LanesConfig.enabled`` is on, several candidate sources merge into
one ``_Candidate`` list inside the recall stage; downstream stages (ccr →
filter → scan → align → budget) never change — ``STAGE_ORDER`` stays the
same six stages and ``hooks.py`` is untouched. When the flag is off (the
default) the code path is IDENTICAL to the pre-E1 pipeline: no lane
queries run, and the assembled output is byte-identical (the ``lane``
block field is omitted entirely when off — a present-but-different key
set would already be an output change).

The lanes (ADR-0025 §Architecture):

* **rules / decisions** — deterministic SQL over the existing flat store
  via ``list_all(tags=...)`` (the pattern ``recall_context`` in
  ``manager.py`` already uses); no schema change, no new index.
* **knowledge** — the existing hybrid RRF recall, with governance-tagged
  rows EXCLUDED (they surface via their own deterministic lanes; leaving
  them in the RRF leg too would re-create the drowning lanes exist to
  fix and duplicate blocks).
* **checkpoints** — NOT a pre_llm_call lane. They stay on the existing
  ``on_session_start`` → ``recall_context`` bootstrap channel and are
  deliberately not pinned into ``pre_llm_call`` permanently (gap-fallback
  is an experimental variable, not v0 behavior).

Lane ordering (the H2 KV-cache surface): the budget stage sorts
candidates by ``(lane order, score desc)`` with a stable tiebreak — the
byte-stable prefix between assemblies of one session is the KV-cache
hypothesis H2 surface (E0 §2.2). Lane order is fixed:
``rules → decisions → knowledge``.

Tag contract: CLOSED. ``area:`` is NOT added to
``ALLOWED_OPTIONAL_PREFIXES``; the closed ``VESMARO_TAG_SUBTYPES`` set
keeps ``mnemos:rule`` / ``mnemos:decision`` as the only lane selectors.

── Cascade-ready contracts (fields only, no mechanics — R2) ──────────

* The lane enum includes ``synthesized`` from day one. NOTHING populates
  it in E1 — the collapse cascade will. ``synthesized`` is a collapsed
  projection of knowledge entries, so it sorts AFTER the pinned
  pre_llm_call prefix (never inside it — a derived segment outranking
  its sources would break the governance guarantee).
* ``collapse_level`` lives in memory **metadata** — the metadata key is
  exactly ``collapse_level``. Never a tag (the tag contract is closed),
  never a schema column (the store stays flat).
* Already landed in slice 1 and NOT redone here: the ``origin=``
  provenance segment (PR #262, ``build_provenance`` in ``assemble.py``)
  and the 4-component idempotency key
  ``hash(scope_key, prompt_version, model_version, input_set_hash)``
  (PR #260). The cascade composes with both.

── Awareness-ready contracts (fields only — R3) ───────────────────────

* **Per-agent delta slot** — CONVENTION for the future awareness module,
  no runtime code: one agent contributes AT MOST one delta block to an
  assembly. This is the structural anti-DoS bound (an agent cannot flood
  the context with N delta blocks) and the accounting unit for D1.
* **Awareness cursor keys** — ``awr:`` + the LENGTH-PREFIXED
  ``(project, agent, session)`` tuple (``awareness_cursor_key``; plain
  ``:``-joins alias when a session id contains ``:`` — #251 sessions
  are printable-ASCII and legally may) in the existing ``meta``
  key-value table, written with the UPSERT pattern
  (precedent: ``session_agent_binding``, PR #263). The tiny read/write
  helpers below are the ONLY runtime piece; the awareness module that
  consumes them arrives later.
* **Awareness renders LAST** — the awareness block composes at the tail
  of the assembled output, NEVER inside the pinned lane prefix (a
  dynamic per-agent block inside the prefix would destroy the byte-stable
  H2 surface). ``assert_foreign_lanes_tail_only`` is the assertion-style
  guard the budget stage runs POST-SORT when lanes are enabled:
  ``lane_sort_key`` already maps any foreign lane (``synthesized``
  today, the future awareness lane) into the deterministic tail, so the
  guard is defensive only — it fires if a future code path ever bypasses
  the sort and places a foreign lane before a pinned-lane block.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Final

from vesmaro.models import Memory, is_context_admissible
from vesmaro.traces import TraceRecorder

if TYPE_CHECKING:
    from vesmaro.manager import MemoryManager


class Lane(StrEnum):
    """Retrieval lanes. Open for the cascade — ``synthesized`` has no
    producer in E1 (see the module docstring)."""

    RULES = "rules"
    DECISIONS = "decisions"
    KNOWLEDGE = "knowledge"
    SYNTHESIZED = "synthesized"


#: The fixed pre_llm_call lane order (the pinned prefix). Checkpoints are
#: NOT a pre_llm_call lane — they ride ``on_session_start``.
PRE_LLM_PINNED_LANES: Final[tuple[Lane, ...]] = (Lane.RULES, Lane.DECISIONS, Lane.KNOWLEDGE)

#: Membership set for the pinned prefix (str values — ``_Candidate.lane``
#: is a plain ``str``).
PRE_LLM_LANE_VALUES: Final[frozenset[str]] = frozenset(lane.value for lane in PRE_LLM_PINNED_LANES)

#: Budget-stage sort index per lane value. Pinned lanes keep the fixed
#: order; ``synthesized`` sorts after the whole pinned prefix; any future
#: lane value (e.g. awareness) lands in the deterministic tail — it can
#: never enter the pinned prefix by construction.
_LANE_SORT_INDEX: Final[dict[str, int]] = {
    Lane.RULES.value: 0,
    Lane.DECISIONS.value: 1,
    Lane.KNOWLEDGE.value: 2,
    Lane.SYNTHESIZED.value: 3,
}

#: Sort position of a lane value outside every registered lane.
_TAIL_SORT_INDEX: Final[int] = len(_LANE_SORT_INDEX)

#: The governance tag selecting each deterministic lane (the closed M2
#: tag contract stays the only selector — no ``area:`` prefix is added).
GOVERNANCE_TAG_BY_LANE: Final[dict[Lane, str]] = {
    Lane.RULES: "mnemos:rule",
    Lane.DECISIONS: "mnemos:decision",
}

#: All governance-selecting tags (the knowledge-lane exclusion set).
GOVERNANCE_TAGS: Final[frozenset[str]] = frozenset(GOVERNANCE_TAG_BY_LANE.values())

#: Per-lane fetch bound for the deterministic SQL leg. Matches the
#: ``list_all`` default; explicit so lane determinism never depends on a
#: default drifting.
LANE_RECALL_LIMIT: Final[int] = 50

#: E0 §1.1 leg B0 — the trivial type-boost factor (issue #277, registered
#: pre-run, before any E3 leg run). "Type-boost of rules/decisions at
#: recall — one ranking line, zero meta-level": governance rows still
#: arrive through ordinary RRF recall only (no lane queries, no lane
#: ordering), but their scores are multiplied by this factor and the
#: candidates re-ranked by score in ``assemble._recall_stage``. Measured
#: RRF scores on the E2 corpus run ~0.011-0.016, so x10.0 lifts a
#: recalled governance row above every non-governance candidate — the
#: MAXIMAL lift a mere type boost can buy, which is the anti-confounding
#: point: whatever leg B still adds over B0 is the meta-level structure
#: (deterministic lane recall + byte-stable pinned prefix), not residual
#: rank boost. B0 cannot surface governance rows the RRF recall MISSED —
#: that recall reach is exactly B's structural contribution.
B0_TYPE_BOOST_FACTOR: Final[float] = 10.0

#: Meta-table key prefix for awareness cursors (R3 contract:
#: ``awr:{project}:{agent}:{session}``).
AWARENESS_CURSOR_PREFIX: Final[str] = "awr:"


def lane_sort_key(lane: str) -> tuple[int, str]:
    """Deterministic budget-stage sort key for one lane value.

    Pinned lanes map to their fixed order index; ``synthesized`` maps
    after the pinned prefix; unknown/future lane values map to the tail.
    The lane value itself is the second component so the tail order is
    fully deterministic even among unregistered lanes.
    """
    return (_LANE_SORT_INDEX.get(lane, _TAIL_SORT_INDEX), lane)


def assert_foreign_lanes_tail_only(lane_values: list[str]) -> None:
    """Awareness-ready invariant guard (R3) — no runtime effect today.

    Raises ``AssertionError`` when a lane value outside the pinned
    pre_llm_call order (``synthesized`` today; the future awareness
    lane) is ordered BEFORE a pinned-lane value: every foreign-lane
    block must render as a contiguous tail, never inside the pinned
    prefix (the byte-stable H2 surface). Defensive only, POST-SORT: the
    budget stage calls this right after the lane sort, whose
    ``lane_sort_key`` already maps foreign lanes into the tail — the
    guard exists to fire if a future code path bypasses the sort.
    """
    seen_foreign = False
    for value in lane_values:
        if value not in PRE_LLM_LANE_VALUES:
            seen_foreign = True
        elif seen_foreign:
            raise AssertionError(
                f"lanes invariant violated: pinned lane {value!r} ordered after a "
                "non-pinned lane — synthesized/awareness blocks must render as a "
                "contiguous tail, never inside the pinned prefix (ADR-0025 E1 / R3)"
            )


def is_governance(memory: Memory) -> bool:
    """True when the row carries a governance-selecting tag.

    The knowledge lane excludes these rows when lanes are enabled — they
    surface via their deterministic lanes instead (anti-drowning by
    construction, not by rank boosting).
    """
    return bool(GOVERNANCE_TAGS.intersection(memory.tags))


def deterministic_lane_results(mgr: MemoryManager, *, lane: Lane, project: str) -> list[Memory]:
    """Deterministic SQL lane query — the ``recall_context`` pattern.

    ``list_all(tags=..., project=...)`` over the flat store (no schema
    change, no ranking); rows are gated by ``is_context_admissible``
    (ADR-0018 status gate + the ADR-0019 §5 quarantine exception — the
    same entry invariant every LLM-bound path composes). Order is the
    SQL order (``created_at DESC``); ``created_at`` ties resolve by
    engine order (rowid) — deterministic for one store state, and the
    stable tiebreak in the budget stage preserves it. The shared
    ``list_all`` ORDER BY is the pre-existing ``recall_context``
    precedent — deliberately NOT changed by this spike.

    Only the governance lanes have a deterministic leg: ``knowledge``
    rides the existing RRF hybrid recall in ``assemble.py``, and
    ``synthesized`` has no producer in E1 — asking for either is a
    caller bug, raised here rather than silently returning ``[]``.
    """
    tag = GOVERNANCE_TAG_BY_LANE.get(lane)
    if tag is None:
        raise ValueError(f"lane {lane!r} has no deterministic SQL leg (rules/decisions only)")
    rows = mgr.sqlite.list_all(limit=LANE_RECALL_LIMIT, project=project, tags=[tag])
    return [m for m in rows if is_context_admissible(m)]


def governance_lanes_recall(
    mgr: MemoryManager, *, project: str
) -> tuple[list[tuple[Memory, Lane]], dict[str, int]]:
    """Run both deterministic lanes and record the lane telemetry trace.

    Returns ``(hits, counts)`` where each hit is ``(memory, lane)`` in
    lane order (rules first, then decisions) with the deterministic SQL
    order inside a lane. A row tagged BOTH ``mnemos:rule`` and
    ``mnemos:decision`` is contract-legal (``validate_tag_contract``
    requires "at least one" subtype, not uniqueness) — it is emitted
    ONCE, into the FIRST lane in pinned order (rules), so neither the
    assembled blocks nor the telemetry can double-count it. The caller
    (``_recall_stage``) turns hits into ``_Candidate``s — this module
    never touches the candidate model.

    Telemetry: one ``TraceRecorder`` row (task ``assemble_lanes``, step
    ``recall``) carrying only the per-lane EMITTED counts in
    ``rationale_summary`` — never content (≤200 chars, truncation is the
    Trace model's own validator). ``save_trace`` failures are non-fatal
    by the recorder's contract; a telemetry write can never fail an
    assembly.
    """
    counts: dict[str, int] = {Lane.RULES.value: 0, Lane.DECISIONS.value: 0}
    hits: list[tuple[Memory, Lane]] = []
    seen: set[str] = set()
    recorder = TraceRecorder(store=mgr.sqlite)
    with recorder.record("assemble_lanes", project, "recall") as trace:
        for lane in (Lane.RULES, Lane.DECISIONS):
            emitted = 0
            for memory in deterministic_lane_results(mgr, lane=lane, project=project):
                # Cross-lane dedup (review P2-1): first lane in pinned
                # order wins — emitting a dual-tagged row in both lanes
                # would duplicate the block and overcount telemetry.
                if memory.id in seen:
                    continue
                seen.add(memory.id)
                hits.append((memory, lane))
                emitted += 1
            counts[lane.value] = emitted
        trace.rationale_summary = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    return hits, counts


# ── Awareness cursor helpers (R3 — the only runtime awareness piece) ─────────


def awareness_cursor_key(*, project: str, agent: str, session: str) -> str:
    """Build the meta-table cursor key for ``(project, agent, session)``.

    Encoding: ``awr:{len(project)}:{project}:{len(agent)}:{agent}:{len(session)}:{session}``
    — each component is LENGTH-PREFIXED. A plain ``:``-join of the tuple
    is NOT collision-safe: ``#251`` session ids are "printable ASCII
    without spaces" and may legally contain ``:``, so the plain form
    aliases distinct tuples (review P3, e.g. ``(a, b:c, d)`` vs
    ``(a:b, c, d)`` both render ``awr:a:b:c:d``) — one agent could read
    or overwrite another agent's cursor. The length prefixes make the
    split unique.

    Components are validated non-empty at this boundary so a blank
    agent/session can never mint a malformed cursor key (the same
    boundary-validation discipline as ``save_checkpoint`` identity).
    """
    for label, value in (("project", project), ("agent", agent), ("session", session)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"awareness cursor component {label} must be a non-empty string")
    return (
        f"{AWARENESS_CURSOR_PREFIX}"
        f"{len(project)}:{project}:{len(agent)}:{agent}:{len(session)}:{session}"
    )


def read_awareness_cursor(
    mgr: MemoryManager, *, project: str, agent: str, session: str
) -> str | None:
    """Read the awareness cursor for ``(project, agent, session)``.

    ``None`` means no cursor was ever written — the future awareness
    module treats that as "nothing consumed yet" (full render, no delta).
    """
    return mgr.sqlite.get_meta(awareness_cursor_key(project=project, agent=agent, session=session))


def write_awareness_cursor(
    mgr: MemoryManager, *, project: str, agent: str, session: str, cursor: str
) -> None:
    """Persist the awareness cursor via the meta-table UPSERT pattern.

    ``set_meta`` is ``INSERT ... ON CONFLICT(key) DO UPDATE`` — a
    re-delivery or retry overwrites in place (idempotent write), the
    same migration-free surface the session→agent binding rides
    (PR #263). An empty cursor is rejected at the boundary: a cursor
    that points at nothing is a bug, not a "start over" signal.
    """
    if not isinstance(cursor, str) or not cursor.strip():
        raise ValueError("cursor must be a non-empty string (None-clear is not a write)")
    key = awareness_cursor_key(project=project, agent=agent, session=session)
    mgr.sqlite.set_meta(key, cursor)


# ── Native heartbeat cursor (ADR-0035 C14 — the awrh: 2-tuple namespace) ─────


#: Meta-table key prefix for the NATIVE heartbeat cursors (ADR-0035 C14):
#: ``awrh:{project}:{agent}`` — a SEPARATE namespace from ``awr:`` on
#: purpose. The legacy prefix length-prefixes a 3-tuple
#: ``(project, agent, session)``; a 2-tuple encoded into the same
#: namespace would alias at parse time (the length-prefix split of
#: ``awr:5:projA:3:abc`` is a valid 3-tuple prefix — a heartbeat key
#: must never be readable as a session-scoped cursor or vice versa).
#: The heartbeat cursor is keyed WITHOUT session (C14): delivery is
#: per-agent state, session churn must not reopen the delta window.
AWARENESS_HEARTBEAT_CURSOR_PREFIX: Final[str] = "awrh:"


def awareness_heartbeat_cursor_key(*, project: str, agent: str) -> str:
    """Build the meta-table cursor key for the heartbeat ``(project, agent)``.

    Encoding mirrors :func:`awareness_cursor_key` minus the session leg:
    ``awrh:{len(project)}:{project}:{len(agent)}:{agent}`` — each
    component LENGTH-PREFIXED (a plain ``:``-join aliases when an agent
    id legally contains ``:``). Components are validated non-empty at
    this boundary, same discipline as the 3-tuple builder.
    """
    for label, value in (("project", project), ("agent", agent)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"heartbeat cursor component {label} must be a non-empty string")
    return f"{AWARENESS_HEARTBEAT_CURSOR_PREFIX}{len(project)}:{project}:{len(agent)}:{agent}"


def read_awareness_heartbeat_cursor(mgr: MemoryManager, *, project: str, agent: str) -> str | None:
    """Read the heartbeat cursor for ``(project, agent)`` (None = never)."""
    return mgr.sqlite.get_meta(awareness_heartbeat_cursor_key(project=project, agent=agent))


def write_awareness_heartbeat_cursor(
    mgr: MemoryManager, *, project: str, agent: str, cursor: str
) -> None:
    """Persist the heartbeat cursor (same UPSERT/boundary rules as ``awr:``)."""
    if not isinstance(cursor, str) or not cursor.strip():
        raise ValueError("cursor must be a non-empty string (None-clear is not a write)")
    mgr.sqlite.set_meta(awareness_heartbeat_cursor_key(project=project, agent=agent), cursor)
