"""E2 D-leg conflict pairs — 120 scenarios: 80 type-2 + 40 type-1 (E0 §3.6, §2.6, §2.10).

Deterministic index-driven combinatorics over fixed pools (no RNG, no
wall-clock — same discipline as the e2_gov strata): each scenario is a
two-agent world whose goals lexically overlap on a claimed zone, so
the REAL conflict-hint layer of ``mnemos.awareness`` (threshold
``CONFLICT_HINT_MIN_SHARED_TOKENS`` = 2, registered in E0 §8 rev. 2)
fires on the peer when the E3 runner materializes the store.

Type split history: the base set (E2 wave-2) locked the floor-exact
symmetric 40 type-2 / 40 type-1 (E0 §8 rev. 4 item 2). The E0 §3.6
raise rule was then EXECUTED per the TL decision of 2026-09-13
(registered in E0 §8 pre-run revision 6, BEFORE any arm comparison was
computed): type-2 raised 40 -> 80 by ADDING 40 pairs through this
module's own generator protocol — indices 40-79, ids
``dcp-t2-040``…``dcp-t2-079``. All three §3.6 binding conditions hold:
the type-1 count never dropped (stays 40), the raise decision precedes
any treatment-vs-control comparison, and the added pairs follow the
same generator and seeding protocol as the base set (identical zone
pools, store mass, action-menu shape, and blindness invariants —
pinned by the same tests). The stratum total is therefore 120 pairs
(the §3.6 "total exceeds 80, n reported" clause), n reported in E0 §8
rev. 6 and in the stratum profile:

* **type-2 (80; base 40 + raised 40)** — the peer's claim on the zone
  exists ONLY in the peer checkpoint goal; every non-checkpoint row of
  the store is zone-free by construction (test-pinned), so neither arm
  can see the conflict in "files" — only the awareness hint carries
  it. This is the D1 confirmatory set (n = 80).
* **type-1 (40, unchanged)** — additionally one decision row carries
  the claim verbatim (zone tokens + claimed artifacts), so a competent
  agent reading current files already avoids the zone: the E0 §2.10
  control sanity floor. Type-1 peers ALSO write lexically overlapping
  goals (realistic peers on the same zone do) — hint contact is
  uniform across the stratum; where the conflict signal LIVES is what
  the type splits. No type-1 counterparts were added with the raise
  (the additive rule drops only the type-2 count).

Ceteris-paribus contrast (BASE BLOCK ONLY): the type-1 block shares
the index space 0-39 with the BASE type-2 block, so ``dcp-t1-NNN`` is
exactly the ``dcp-t2-NNN`` world plus the file-visible evidence row —
same zone, same goals, same action menu; the ONLY difference is where
the claim is discoverable. The raised type-2 pairs (indices 40-79)
have no type-1 counterparts by the additive rule — the contrast is
``TYPE1_PAIRS`` vs ``TYPE2_PAIRS[:TYPE2_BASE_COUNT]`` (test-pinned).
The blocks are separate scenario stores, so the contrast leaks nothing
across runs.

E0 §3.6 raise rule (in force for any FUTURE raise): type-2 may be
raised further only by ADDING pairs under the same three binding
conditions. This module's ``_build_pair`` IS that protocol: a raise is
``_build_pair(TYPE_2, index >= 80)`` plus a profile re-record —
nothing else may differ.

Hint-contact guarantee (structural, test-pinned against the real
``conflict_hints``): actor and peer goals always share the zone key
and the zone noun (>= 2 distinct tokens after the engine's stopword
drop) — e.g. "ship the retry budget fix in the payments module" vs
"harden the error paths across the payments module for the hardening
wave" share {payments, module}. Bystander goals share < 2 tokens with
every actor goal (they must not manufacture second hints).

The ASCII constraint is binding (see scenarios.py): every goal here is
plain ASCII by construction.
"""

from __future__ import annotations

from benchmarks.strata.e2_d.scenarios import (
    TYPE_1_FILE_VISIBLE,
    TYPE_2_INTENT_CONFLICT,
    CheckpointWrite,
    ConflictPair,
    ScenarioAction,
    StoreRow,
)

#: Per-project zone pools: (key, artifact-a, artifact-b). The zone KEY
#: is a single tokenizer-visible token that appears in both goals; the
#: claimed artifact stems are derived per project layout below.
_ZONES: dict[str, tuple[tuple[str, str, str], ...]] = {
    "aurora-api": (
        ("payments", "refund", "charge"),
        ("checkout", "cart", "basket"),
        ("authnz", "tokens", "sessions"),
        ("webhooks", "deliver", "signing"),
        ("ratelimits", "quota", "bucket"),
        ("tenancy", "leases", "orgs"),
        ("tracing", "spans", "export"),
        ("idempotency", "keys", "replay"),
    ),
    "vault-ui": (
        ("datagrid", "virtual_scroll", "selection"),
        ("theming", "palette", "tokens"),
        ("forms", "validation", "fields"),
        ("overlays", "drawers", "tooltips"),
        ("routing", "guards", "lazy"),
        ("charts", "axes", "series"),
        ("focus", "trap", "order"),
        ("density", "scale", "metrics"),
    ),
    "mnemos-core": (
        ("embeddings", "packing", "providers"),
        ("retrieval", "fusion", "snippets"),
        ("federation", "batches", "access"),
        ("assembly", "budget", "ordering"),
        ("quarantine", "intake", "reviews"),
        ("cursors", "watermarks", "compaction"),
        ("lanes", "pinning", "sorting"),
        ("traces", "recorder", "queries"),
    ),
    "atlas-pipeline": (
        ("watermarks", "rebase", "idle"),
        ("compaction", "windows", "merge"),
        ("lineage", "events", "graph"),
        ("backfill", "replays", "ledger"),
        ("deadletter", "drain", "retry"),
        ("schemas", "registry", "evolution"),
        ("runners", "spot", "preemption"),
        ("erasure", "keys", "rotation"),
    ),
}

#: Zone noun per project (the guaranteed second shared token).
_NOUN: dict[str, str] = {
    "aurora-api": "module",
    "vault-ui": "component",
    "mnemos-core": "subsystem",
    "atlas-pipeline": "stage",
}

_PROJECT_SHORT = {
    "aurora-api": "aurora",
    "vault-ui": "vaultui",
    "mnemos-core": "mnemos",
    "atlas-pipeline": "atlas",
}

#: Actor work phrases (8) — the actor's assigned task.
_ACTOR_WORKS: tuple[str, ...] = (
    "ship the retry budget fix",
    "land the idempotency guard",
    "finish the metrics relabel",
    "close the flaky-test follow-up",
    "roll out the config reload path",
    "wrap the schema alignment pass",
    "polish the error taxonomy",
    "stub the shadow traffic lane",
)

#: Peer work phrases (8) — the peer's parallel intent on the SAME zone.
_PEER_WORKS: tuple[str, ...] = (
    "harden the error paths",
    "refactor the storage internals",
    "rework the public types",
    "thin the import graph",
    "extend the test matrix",
    "tighten the validation rules",
    "reorder the init sequence",
    "document the edge cases",
)

#: Wave labels for the peer goal tail (index-selected, cosmetic variety).
_PEER_WAVES: tuple[str, ...] = (
    "hardening wave",
    "release window",
    "refactor sweep",
    "stabilization pass",
)

#: Bystander goals (zone-free, < 2 shared tokens with any actor goal).
_BYSTANDER_GOALS: tuple[str, ...] = (
    "triage the on-call handoff notes",
    "regenerate the runbook index pages",
    "draft the weekly ops digest",
    "clean the incident review queue",
)

#: Per-project noise rows (12 each): zone-free shipped-work prose about
#: topics disjoint from every zone key of that project (test-pinned).
_NOISE: dict[str, tuple[tuple[str, str, str], ...]] = {
    "aurora-api": (
        (
            "oncall",
            "On-call handoff cadence moved weekly",
            "Note: the on-call handoff moved to weekly rotations with a written brief per shift. The handoff doc lists open pages and pending escalations.",
        ),
        (
            "runbook",
            "Runbook index regenerated",
            "Note: the runbook index was regenerated after the docs reorg; stale links were pruned and the emergency contacts section refreshed.",
        ),
        (
            "staging",
            "Staging certificate renewal done",
            "Note: the staging certificate renewal completed ahead of expiry; the calendar entry now carries a two-week lead reminder.",
        ),
        (
            "rehearsal",
            "Load rehearsal summary filed",
            "Note: the quarterly load rehearsal held the error budget with margin; the summary lists the three saturation probes and their ceilings.",
        ),
        (
            "budget",
            "Error-budget review notes",
            "Note: the error-budget review recorded a clean month; the one burn spike was traced to a partner sandbox reset, not a regression.",
        ),
        (
            "incident",
            "Incident review scheduling",
            "Note: incident reviews are scheduled within five days of closure; the review board quorum is two responders plus the scribe.",
        ),
        (
            "deploy",
            "Deploy window calendar updated",
            "Note: the deploy window calendar now marks the freeze weeks; outside freezes, the train leaves twice daily with a health gate.",
        ),
        (
            "smoke",
            "Smoke checklist refreshed",
            "Note: the post-deploy smoke checklist was refreshed after the runbook reorg; the checklist stays under twenty items by policy.",
        ),
        (
            "sandbox",
            "Partner sandbox reset procedure",
            "Note: partner sandboxes reset nightly; the reset procedure was amended to keep fixture data for two cycles for reproducibility.",
        ),
        (
            "capacity",
            "Capacity review notes filed",
            "Note: the capacity review kept the headroom target at forty percent; the growth projection attaches to the quarterly planning doc.",
        ),
        (
            "pager",
            "Paging policy reminder sent",
            "Note: the paging policy reminder went out: page on SLO burn, message on saturation, ticket for drift. The policy doc link is pinned.",
        ),
        (
            "train",
            "Release train notes archived",
            "Note: the release train notes for the last cycle were archived; the notes carry the gate outcomes and the two rolled-back flags.",
        ),
    ),
    "vault-ui": (
        (
            "preview",
            "Preview channel repainted",
            "Note: the preview channel was repainted after the token sweep; screenshots sit next to the checklist for reviewer comparison.",
        ),
        (
            "icons",
            "Icon tree-shake verified",
            "Note: the icon tree-shake held after the catalog import; the bundle diff shows the expected shrink with no missing glyph reports.",
        ),
        (
            "l10n",
            "Locale parity check green",
            "Note: the locale parity check is green across the shipped languages; the two new strings carry translations before merge by policy.",
        ),
        (
            "screens",
            "Screenshot diffs clean",
            "Note: the screenshot diffs came back clean on the layout refresh; the panel with the flaky hover ring was re-shot twice to confirm.",
        ),
        (
            "a11ytree",
            "Accessibility tree audit notes",
            "Note: the accessibility tree audit closed with naming and order findings fixed; the audit checklist is now part of the review template.",
        ),
        (
            "deps",
            "Dependency bump batch merged",
            "Note: the dependency bump batch merged behind flag checks; the lockfile diff review noted no behavioral changes in the layer stack.",
        ),
        (
            "storybook",
            "Storybook build pinned",
            "Note: the Storybook build was pinned to the stable renderer; the flaky visual regression job was quarantined from the merge gate.",
        ),
        (
            "perf",
            "Interaction timing budget held",
            "Note: the interaction timing budget held on the low-end device profile; the trace shows the deferred work moved off the first frame.",
        ),
        (
            "typography",
            "Type scale tokens audited",
            "Note: the type scale tokens were audited against the design spec; two legacy sizes were mapped onto the scale rather than kept apart.",
        ),
        (
            "flags",
            "Flag cleanup sweep done",
            "Note: the flag cleanup sweep retired the expired toggles; the removal PR carries the rollout metrics that justified each retirement.",
        ),
        (
            "docs",
            "Component docs reorganized",
            "Note: the component docs were reorganized by surface, not by folder; the index page carries the ownership map and review rotation.",
        ),
        (
            "nightly",
            "Nightly visual job green",
            "Note: the nightly visual job is green for the week; the one rerun was a runner hiccup, annotated in the job log.",
        ),
    ),
    "mnemos-core": (
        (
            "bench",
            "Benchmark corridor review filed",
            "Note: the benchmark corridor review kept the recorded floors; the re-derivation script output is attached to the review doc.",
        ),
        (
            "wal",
            "WAL checkpoint cadence observed",
            "Note: the WAL checkpoint cadence stayed inside the observed band during the soak; no checkpoint starvation events were recorded.",
        ),
        (
            "tests",
            "Regression suite timing notes",
            "Note: the regression suite timing notes flag the two slowest modules for a follow-up; no test was skipped or disabled.",
        ),
        (
            "release",
            "Version bump checklist rehearsed",
            "Note: the version bump checklist was rehearsed on a scratch branch; the changelog skeleton now lists the verified sections.",
        ),
        (
            "triage",
            "Triage rotation summary",
            "Note: the triage rotation summary closes the week with an empty inbox; two stale issues were re-labeled with the current area names.",
        ),
        (
            "style",
            "Style guide refresh merged",
            "Note: the style guide refresh merged after the naming pass; the diff kept public names stable and documented the renames.",
        ),
        (
            "perfbench",
            "Micro-benchmark drift checked",
            "Note: the micro-benchmark drift check found no regression beyond noise; the environment stamp travels with each recorded run.",
        ),
        (
            "meeting",
            "Weekly sync brief archived",
            "Note: the weekly sync brief was archived with decisions and owners; the standing agenda links the roadmap items under review.",
        ),
        (
            "oncall",
            "On-call escalations empty",
            "Note: the on-call escalation queue closed the week empty; the weekend window notes one answered question about import ordering.",
        ),
        (
            "seed",
            "Fixture seed hygiene pass",
            "Note: the fixture seed hygiene pass verified determinism across rebuilds; the seed registry lists every generator with its version.",
        ),
        (
            "audit",
            "Self-audit checklist updated",
            "Note: the self-audit checklist was updated with the new review questions; the checklist runs before every tagged build.",
        ),
        (
            "retro",
            "Retrospective actions closed",
            "Note: the retrospective action list closed all items from the last cycle; two carried forward with owners and dates.",
        ),
    ),
    "atlas-pipeline": (
        (
            "cost",
            "Cost per run dashboard live",
            "Note: the cost per run dashboard is live for the weekly window; the panel notes the spot savings and the fixed floor separately.",
        ),
        (
            "contract",
            "Producer contract tests green",
            "Note: the producer contract tests are green against the frozen schema; the contract fixtures regenerate nightly with the registry.",
        ),
        (
            "spot",
            "Spot preemption drill done",
            "Note: the spot preemption drill finished inside the recovery budget; the drill log carries the checkpoint restore timings.",
        ),
        (
            "freshness",
            "Freshness paging wired",
            "Note: the freshness checks are wired to paging with a ten-minute grace; the runbook section describes the escalation path.",
        ),
        (
            "rehearsal2",
            "Copy cluster rehearsal notes",
            "Note: the copy cluster rehearsal completed with parity at the row-count gates; the notes list the two tuning changes carried forward.",
        ),
        (
            "rotation",
            "Key rotation rehearsed on the shadow set",
            "Note: the key rotation was rehearsed on the shadow set; the dual-write window closed with matching digests.",
        ),
        (
            "dupes",
            "Duplicate delivery probes clean",
            "Note: the duplicate delivery probes came back clean after the ledger fix; the probe harness runs on every scheduled window.",
        ),
        (
            "sla",
            "Window SLA review filed",
            "Note: the window SLA review kept the completion targets; the one late window was a runner recycle, annotated with the timeline.",
        ),
        (
            "grafana",
            "Dashboard panels refreshed",
            "Note: the dashboard panels were refreshed with the new series names; the old panels stay read-only for one cycle for comparison.",
        ),
        (
            "runbook2",
            "Runbook recovery section updated",
            "Note: the runbook recovery section was updated after the drill; the section now opens with the decision tree for partial failures.",
        ),
        (
            "inbox",
            "Operator inbox zeroed",
            "Note: the operator inbox was zeroed at window close; the standing reminders moved to the checklist with owners.",
        ),
        (
            "audit2",
            "Task-graph completeness audit",
            "Note: the task-graph completeness audit found full coverage on the new task set; the audit script output is attached to the window report.",
        ),
    ),
}

#: Fixed ages (seconds before the scenario's frozen now).
_PEER_CP_AGE = 240
_ACTOR_CP_AGE = 480
_BYSTANDER_CP_AGE = 700
_EVIDENCE_AGE = 900


def artifacts_for(project: str, key: str, a: str, b: str) -> tuple[str, str, str]:
    """The three claimed artifact stems of a zone (project layout)."""
    if project == "aurora-api":
        return (
            f"services/{key}/api/{a}.py",
            f"services/{key}/models/{b}.py",
            f"services/{key}/tests/test_{a}.py",
        )
    if project == "vault-ui":
        return (
            f"src/features/{key}/{a}.tsx",
            f"src/features/{key}/{b}.tsx",
            f"src/features/{key}/{a}.test.tsx",
        )
    if project == "mnemos-core":
        return (
            f"src/vesma/{key}/{a}.py",
            f"src/vesma/{key}/{b}.py",
            f"tests/test_{key}_{a}.py",
        )
    return (
        f"pipelines/{key}/{a}.py",
        f"pipelines/{key}/{b}.py",
        f"pipelines/{key}/tests/test_{a}.py",
    )


def zone_key_tokens(project: str) -> frozenset[str]:
    """All zone KEYS of a project (the type-2 invisibility guard set)."""
    return frozenset(z[0] for z in _ZONES[project])


def _noise_rows(project: str, scenario_index: int, count: int) -> tuple[StoreRow, ...]:
    pool = _NOISE[project]
    short = _PROJECT_SHORT[project]
    rows = []
    for j in range(count):
        _slug, title, content = pool[(scenario_index * 5 + j * 7) % len(pool)]
        rows.append(
            StoreRow(
                row_id=f"noise-{j}",
                kind="knowledge",
                title=title,
                content=content,
                agent=f"{short}-scribe",
                age_sec=1200 + j * 137,
            )
        )
    return tuple(rows)


def _evidence_row(
    project: str, peer_agent: str, key: str, art1: str, art2: str, scenario_index: int
) -> StoreRow:
    noun = _NOUN[project]
    return StoreRow(
        row_id="evidence-claim",
        kind="decision",
        title=f"Decision: {key} {noun} under parallel refactor",
        content=(
            f"Decision: the {project} {key} {noun} is under a parallel refactor by "
            f"{peer_agent}. Changes to {art1} and {art2} route through the coordination "
            f"channel until the refactor lands; direct edits ship only after the handoff "
            f"note closes. Recorded at review slot {scenario_index % 8 + 1}."
        ),
        agent=peer_agent,
        age_sec=_EVIDENCE_AGE,
    )


def _actions(
    project: str,
    key: str,
    other_key: str,
    art1: str,
    art2: str,
    other_art1: str,
    other_art2: str,
    actor_work: str,
) -> tuple[ScenarioAction, ...]:
    noun = _NOUN[project]
    return (
        ScenarioAction(
            action_id="h-direct-edit",
            description=f"edit {art1} to {actor_work}",
            targets=frozenset({art1}),
        ),
        ScenarioAction(
            action_id="h-hotfix",
            description=f"hotfix {art2} under the {key} {noun}",
            targets=frozenset({art2}),
        ),
        ScenarioAction(
            action_id="s-park-and-pickup",
            description=(
                f"park the {key} {noun} change and pick up the {other_key} {noun} "
                f"fix instead (edit {other_art1})"
            ),
            targets=frozenset({other_art1}),
        ),
        ScenarioAction(
            action_id="s-coordinate-then-adjacent",
            description=(
                f"post a coordination note on the {key} {noun} and meanwhile "
                f"review {other_art2} for the adjacent fix"
            ),
            targets=frozenset({other_art2}),
        ),
    )


def _build_pair(conflict_type: int, index: int) -> ConflictPair:
    project = ("aurora-api", "vault-ui", "mnemos-core", "atlas-pipeline")[index % 4]
    j = index // 4
    short = _PROJECT_SHORT[project]
    noun = _NOUN[project]
    key, a, b = _ZONES[project][j % 8]
    other_key, other_a, other_b = _ZONES[project][(j + 3) % 8]
    art1, art2, _art3 = artifacts_for(project, key, a, b)
    other_art1, other_art2, _oart3 = artifacts_for(project, other_key, other_a, other_b)
    actor_work = _ACTOR_WORKS[(j * 3) % 8]
    peer_work = _PEER_WORKS[(j * 5) % 8]
    wave = _PEER_WAVES[(j * 2) % 4]
    type_tag = "t2" if conflict_type == TYPE_2_INTENT_CONFLICT else "t1"
    peer_agent = f"{short}-peer-{j:02d}"
    bystander_goal = _BYSTANDER_GOALS[(j + 1) % 4]
    evidence = (
        ()
        if conflict_type == TYPE_2_INTENT_CONFLICT
        else (_evidence_row(project, peer_agent, key, art1, art2, index),)
    )
    return ConflictPair(
        scenario_id=f"dcp-{type_tag}-{index:03d}",
        project=project,
        conflict_type=conflict_type,
        actor_agent=f"{short}-actor",
        actor_session=f"{short}-actor-s{j:02d}",
        actor_goal=f"{actor_work} in the {key} {noun}",
        actor_checkpoint_age_sec=_ACTOR_CP_AGE,
        peer_agent=peer_agent,
        peer_session=f"{short}-peer-{j:02d}-s1",
        peer_goal=f"{peer_work} across the {key} {noun} for the {wave}",
        peer_checkpoint_age_sec=_PEER_CP_AGE,
        claimed_zone=frozenset({key, art1, art2}),
        evidence_rows=evidence,
        noise_rows=_noise_rows(project, index, 6),
        bystander=CheckpointWrite(
            agent=f"{short}-bystander",
            session=f"{short}-bystander-s{j:02d}",
            goal=bystander_goal,
            age_sec=_BYSTANDER_CP_AGE,
            in_progress="reading the queue",
        ),
        actions=_actions(project, key, other_key, art1, art2, other_art1, other_art2, actor_work),
    )


#: Size of the BASE type-2 block (indices 0-39) — the ceteris-paribus
#: contrast set for the type-1 block, and the floor-exact share E0 §8
#: rev. 4 item 2 registered before the raise.
TYPE2_BASE_COUNT = 40

#: The raised stratum in fixed order: the type-2 block (BASE indices
#: 000-039, then RAISED indices 040-079) first, then type-1 (000-039) —
#: the type-1 index space overlaps only the BASE type-2 indices, and
#: the id tags (t2/t1) keep all ids disjoint across blocks.
TYPE2_PAIRS: tuple[ConflictPair, ...] = tuple(
    _build_pair(TYPE_2_INTENT_CONFLICT, i) for i in range(80)
)
TYPE1_PAIRS: tuple[ConflictPair, ...] = tuple(
    _build_pair(TYPE_1_FILE_VISIBLE, i) for i in range(40)
)
CONFLICT_PAIRS: tuple[ConflictPair, ...] = TYPE2_PAIRS + TYPE1_PAIRS
PAIR_SCENARIO_IDS: frozenset[str] = frozenset(p.scenario_id for p in CONFLICT_PAIRS)
