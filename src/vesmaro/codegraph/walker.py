"""The shared BFS walker over ``project_edges`` (ADR-0038 M1, condition 1).

One walker, two surfaces: ``trace_path`` rides it with today's exact
semantics (default direction ``out`` — answers stay byte-identical) and
the M2 walk section will ride the same object. The discipline stays the
ADR-0030 walk pattern:

* **Bounded work** — the per-node FANOUT cap bounds hub symbols, the
  TOTAL-WORK cap bounds the whole BFS in visited nodes; both fire into
  the ``truncated`` flag, deterministically (no wall-clock timeout —
  the work caps replace it, ADR-0038 condition 3).
* **Fail-closed limits** — a depth outside ``[1, WALK_MAX_DEPTH]``, an
  unknown direction, a non-list edge-kind filter or an unknown edge
  kind are REFUSED before any store access, never clamped or coerced
  into a silently different walk.
* **Point queries** — every hop is one indexed point query per node
  (``idx_edges_from`` / ``idx_edges_to``), never a recursive CTE
  (ADR-0038 alternatives: point queries keep per-level control).
* **Project boundary at the followed endpoint** (ADR-0038 condition 5)
  — the walker enforces the current-project id prefix (``<len>:<key>#
  <path>#...``) on every candidate FOLLOW endpoint: an edge whose
  endpoint leaves the project is SKIPPED (never reported, never
  followed) and sets ``truncated``. ``get_node`` resolves ids
  globally — this walker-level check is the explicit protection the
  ADR made a required invariant of any new walk code. Real indexes
  never produce cross-project edges, so the default-direction answers
  stay byte-identical.
* **PG1 rows** — node rows carry metadata shapes only
  (:func:`node_row`): no ``signature`` (``api_key="..."``-class leaks
  live there — ADR-0038 condition 4), no literal content.

Cross-surface behavior (direction ``in``/``both`` + the kind filter)
is NEW surface (ADR-0038 M1): ``trace_path`` keeps the ``out`` default
so existing answers are byte-equal; the other directions and kinds are
opt-in parameters of every caller that needs them (M2's walk section
wires the rest).
"""

from __future__ import annotations

from typing import Any, Protocol

from vesmaro.storage.code_graph_store import EDGE_KINDS

#: BFS depth clamp (the ADR-0030 walk pattern; ADR-0038 condition 3
#: keeps it fail-closed). Shared by trace_path and the walk section.
WALK_MAX_DEPTH = 2

#: Per-node fanout cap: bounds hub symbols (the ``+1`` probe detects
#: overflow deterministically). Shared.
WALK_FANOUT_CAP = 32

#: Total-work cap over visited nodes: bounds the whole BFS. Shared.
WALK_TOTAL_WORK_CAP = 512

#: Walk directions: ``out`` (today's trace_path), ``in`` (incoming —
#: «who calls this»), ``both`` (an expansion of one node ALWAYS walks
#: its incoming leg first, then the outgoing leg — one fixed order,
#: no ambient nondeterminism).
WALK_DIRECTIONS = ("in", "out", "both")

#: Edge kinds allowed in a kind filter — the store's CHECK-enforced
#: vocabulary. A filter naming anything outside this tuple is refused
#: (fail-closed), not silently narrowed.
WALK_EDGE_KINDS: tuple[str, ...] = EDGE_KINDS


class WalkLimitError(ValueError):
    """A walk argument breached a fail-closed limit — the depth clamp,
    the direction vocabulary or the edge-kind vocabulary. Raised BEFORE
    any store access (a refusal, never a silent different walk);
    surface layers translate it into their own error contract."""


class WalkEdgeSource(Protocol):
    """The slice of ``CodeGraphStore`` the walker may use.

    Point-query discipline: edges are fetched per-node with a limit
    (``fanout_cap + 1`` detects overflow deterministically), filtered
    by endpoint and kind — index-hitting shapes only.
    """

    def get_edges(
        self,
        project: str,
        *,
        from_id: str | None = ...,
        to_id: str | None = ...,
        kind: str | None = ...,
        limit: int = ...,
    ) -> list[Any]: ...

    def get_node(self, node_id: str) -> Any | None: ...


def node_row(node: Any, depth: int) -> dict[str, Any]:
    """The PG1 row shape of one visited node: metadata shapes only —
    no ``signature`` (the ``api_key="..."``-class leak lives exactly
    there — ADR-0038 condition 4), no literal content. The key order is
    the byte-pinned trace_path row contract."""
    return {
        "id": node.id,
        "qname": node.qname,
        "kind": node.kind,
        "path": node.path,
        "start_line": node.start_line,
        "end_line": node.end_line,
        "depth": depth,
    }


def validate_walk_limits(
    depth: Any,
    direction: Any,
    edge_kinds: Any,
    *,
    max_depth: int = WALK_MAX_DEPTH,
) -> tuple[int, str, tuple[str, ...] | None]:
    """Fail-closed validation of the shared walk limits.

    Returns the validated ``(depth, direction, edge_kinds)`` triple;
    raises :class:`WalkLimitError` on ANY breach.

    ``edge_kinds`` may be ``None`` (no filter — all kinds) or a
    list/tuple of edge-kind names validated against
    :data:`WALK_EDGE_KINDS` and de-duplicated preserving the caller's
    order. A bare string, any other container (an unordered set would
    walk nondeterministically), an empty list (= «no filter», honest
    normalization) and unknown/non-string kinds are refused or
    normalized exactly as documented — nothing is silently widened.
    """
    if not isinstance(depth, int) or isinstance(depth, bool) or not 1 <= depth <= max_depth:
        raise WalkLimitError(f"depth must be an integer in [1, {max_depth}]")
    if not isinstance(direction, str) or direction not in WALK_DIRECTIONS:
        raise WalkLimitError(
            "direction must be one of: " + ", ".join(WALK_DIRECTIONS) + f", got {direction!r}"
        )
    if edge_kinds is None:
        return depth, direction, None
    if isinstance(edge_kinds, str) or not isinstance(edge_kinds, (list, tuple)):
        raise WalkLimitError(
            "edge_kinds, when provided, must be a list or tuple of edge kinds, "
            f"got {type(edge_kinds).__name__}"
        )
    if len(edge_kinds) == 0:
        return depth, direction, None
    seen: dict[str, None] = {}
    for kind in edge_kinds:
        if not isinstance(kind, str) or kind not in WALK_EDGE_KINDS:
            raise WalkLimitError(
                f"edge kind must be one of: {', '.join(WALK_EDGE_KINDS)}, got {kind!r}"
            )
        seen[kind] = None
    return depth, direction, tuple(seen)


def same_project_prefix(node_id: str, project: str) -> bool:
    """Whether a node id belongs to ``project`` (the canonical
    ``<len(project)>:<project>#<path>#<symbol>#<line>`` key — the
    length prefix makes a sibling name like ``miniproj2`` unable to
    forge membership; the ``#`` closes the name segment)."""
    return node_id.startswith(f"{len(project)}:{project}#")


def walk_quota(limit: Any) -> int:
    """The M2 walk quota: ``k = min(ceil(limit/5), limit//2)`` — a PURE
    function of the search ``limit`` (ADR-0038 condition 3; the ratified
    formula, no adaptivity). ``limit`` goes through the same
    non-negative-int gate the search already applies (raise-on-junk —
    the quota never silently clamps its input). The hard caps
    (fanout 32, total work 512) apply INDEPENDENTLY of ``k``: they
    bound the walk no matter how the quota resolves."""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise WalkLimitError(f"limit must be a non-negative integer, got {limit!r}")
    return min(-(-limit // 5), limit // 2)


def walk_bfs(
    store: WalkEdgeSource,
    project: str,
    start_id: str,
    node_meta: Any,
    *,
    depth: Any,
    direction: Any,
    edge_kinds: Any = None,
    fanout_cap: int = WALK_FANOUT_CAP,
    total_work_cap: int = WALK_TOTAL_WORK_CAP,
) -> dict[str, Any]:
    """The BFS core: from ``start_id`` over ``project_edges`` in the
    given ``direction`` (``in``/``out``/``both``) with an optional
    edge-kind filter (OR-semantics over the given kinds).

    Fail-closed limit validation (brief card vesma-pg1-walk-m2-section
    P3-b) lives HERE, not only at the service layer: ``depth``,
    ``direction`` and ``edge_kinds`` go through
    :func:`validate_walk_limits` at entry — a breach raises
    :class:`WalkLimitError` BEFORE any store access, so a caller that
    skips the service check can never smuggle a silently different
    walk into the BFS. Validated values are then re-typed (``depth``
    is provably ``int``, ``direction`` provably ``str``, ``edge_kinds``
    provably a kind tuple or None) — the ``Any`` annotations keep that
    contract honest for callers passing raw tool arguments.

    Returns live walk state: ``rows`` (the visited node rows sorted by
    ``(depth, qname)`` — byte-identical to trace_path's sort under the
    ``out`` default), ``edges`` (the traversed edges in visit order —
    every row keeps the trace contract keys ``from``/``to``/``kind``/
    ``provenance``, in-walks included) and ``truncated`` (any cap or
    project-boundary veto fired).

    The level loop is the EXACT ADR-0030 shape trace_path has always
    had: a frontier that cannot complete because the total-work cap is
    already reached sets ``truncated`` and stops expanding (the
    discovered next frontier still rides the level loop); the fanout
    ``+1`` probe sets ``truncated`` and slices the leg to the cap.
    """
    v_depth, v_direction, v_kinds = validate_walk_limits(
        depth, direction, edge_kinds, max_depth=WALK_MAX_DEPTH
    )
    visited: dict[str, dict[str, Any]] = {start_id: node_row(node_meta, 0)}
    edges: list[dict[str, Any]] = []
    truncated = False
    frontier = [start_id]
    for level in range(1, v_depth + 1):
        if not frontier or len(visited) >= total_work_cap:
            truncated = truncated or bool(frontier)
            break
        next_frontier: list[str] = []
        for node_id in frontier:
            if len(visited) >= total_work_cap:
                truncated = True
                break
            fanout, over_limit = _fetch_fanout(
                store,
                project,
                node_id,
                direction=v_direction,
                edge_kinds=v_kinds,
                fanout_cap=fanout_cap,
            )
            if over_limit:
                truncated = True
            for edge, follow_id in fanout:
                if not same_project_prefix(follow_id, project):
                    # ADR-0038 condition 5: the edge leaves the project
                    # — skipped WHOLE (never reported, never followed)
                    # and the truncation flag marks the boundary.
                    truncated = True
                    continue
                edges.append(_edge_row(edge))
                if follow_id in visited:
                    continue
                target = store.get_node(follow_id)
                if target is None:  # cascade promise not yet materialized
                    continue
                visited[follow_id] = node_row(target, level)
                next_frontier.append(follow_id)
        frontier = next_frontier
    rows = sorted(visited.values(), key=lambda r: (r["depth"], str(r["qname"])))
    return {"rows": rows, "edges": edges, "truncated": truncated}


def _edge_row(edge: Any) -> dict[str, Any]:
    return {
        "from": edge.from_id,
        "to": edge.to_id,
        "kind": edge.kind,
        "provenance": edge.provenance,
    }


def _fetch_fanout(
    store: WalkEdgeSource,
    project: str,
    node_id: str,
    *,
    direction: str,
    edge_kinds: tuple[str, ...] | None,
    fanout_cap: int,
) -> tuple[list[tuple[Any, str]], bool]:
    """One node's expansion as ``(edge, follow_endpoint)`` pairs, in the
    DETERMINISTIC walk order, plus the overflow flag.

    ``out`` → the endpoint index ``from_id`` (followed endpoint
    ``to_id``) — the EXACT single point query trace_path issues today,
    so byte-equality is structural; ``in`` → ``to_id`` (followed
    endpoint ``from_id``); ``both`` → the incoming leg first, then the
    outgoing leg, each leg probed and capped on its own.

    With a kind filter each kind rides its OWN capped point query in
    the caller's kind order (an edge has exactly one kind — legs never
    overlap, no dedupe needed). A leg returning ``fanout_cap + 1`` rows
    is sliced to the cap and reports the overflow.
    """
    legs: list[tuple[str, str]] = []
    if direction == "in":
        legs.append(("to_id", node_id))
    elif direction == "out":
        legs.append(("from_id", node_id))
    else:  # both — in first, then out, every leg capped on its own
        legs.append(("to_id", node_id))
        legs.append(("from_id", node_id))
    collected: list[tuple[Any, str]] = []
    over_limit = False
    for param, value in legs:
        base: dict[str, Any] = {"limit": fanout_cap + 1, param: value}
        if edge_kinds is None:
            fanout = store.get_edges(project, **base)
            if len(fanout) > fanout_cap:
                fanout = fanout[:fanout_cap]
                over_limit = True
            collected.extend((e, e.from_id if param == "to_id" else e.to_id) for e in fanout)
        else:
            for kind in edge_kinds:
                fanout = store.get_edges(project, kind=kind, **base)
                if len(fanout) > fanout_cap:
                    fanout = fanout[:fanout_cap]
                    over_limit = True
                collected.extend((e, e.from_id if param == "to_id" else e.to_id) for e in fanout)
    return collected, over_limit
