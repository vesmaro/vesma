"""E2 awareness-leg strata — preparation gates (E0 §3.6, §3.7, §2.6-§2.10).

PREPARATION tests, not experiment runs: they pin the stratum counts,
the engine binding (the REAL awareness functions over the scenario
artifacts), the collision oracle, the canary false-drop at the REAL
write boundary, the blindness separation of the ground-truth keys, the
profile fingerprint, and the isolation from both the S1 stand and the
e2_gov wave-1 corpus. No arms, no assemble calls, no metrics — the
D-leg runs happen at E3 under their own pre-registered gates.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from benchmarks.corpus.corpus import CORPUS
from benchmarks.stands.s1_quality import run as s1_run
from benchmarks.strata.e2_d import ground_truth as gt
from benchmarks.strata.e2_d import oracle
from benchmarks.strata.e2_d import profile as prof
from benchmarks.strata.e2_d.adversarial import ADVERSARIAL_SCENARIO
from benchmarks.strata.e2_d.canaries import CANARY_FACTS
from benchmarks.strata.e2_d.conflict_pairs import (
    CONFLICT_PAIRS,
    TYPE1_PAIRS,
    TYPE2_BASE_COUNT,
    TYPE2_PAIRS,
)
from benchmarks.strata.e2_d.materialize import (
    checkpoint_row,
    materialize_adversarial,
    materialize_conflict_pair,
    materialize_replay224,
    materialize_stale_claim,
    replay_row_counts,
)
from benchmarks.strata.e2_d.replay224 import REPLAY_224, flood_checkpoints, flood_plain_rows
from benchmarks.strata.e2_d.scenarios import (
    STALE_SUPERSEDED_GOAL,
    STALE_WINDOW_EXPIRED,
    CheckpointWrite,
    ConflictPair,
)
from benchmarks.strata.e2_d.stale_claims import (
    STALE_CLAIMS,
    SUPERSEDED_GOAL_CLAIMS,
    WINDOW_EXPIRED_CLAIMS,
)

from vesmaro.awareness import (
    AWARENESS_DISCLAIMER,
    DELTA_MAX_WINDOW_SEC,
    GOAL_TITLE_MAX_CHARS,
    checkpoint_goal_title,
    compose_pre_llm_awareness,
    conflict_hints,
    project_delta,
    render_awareness_section,
)
from vesmaro.config import Settings
from vesmaro.danger_detectors import detect
from vesmaro.manager import MemoryManager
from vesmaro.models import (
    MemoryCreate,
    MemorySource,
    MemoryStatus,
    is_context_admissible,
)

FROZEN_NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)

_AGENT_RE = re.compile(r"[a-z0-9_-]{1,64}")
_SESSION_RE = re.compile(r"[!-~]{1,128}")


# ── fixtures (manager shape mirrors tests/test_awareness.py) ──────────────────


def _settings(tmp: Path) -> Settings:
    settings = Settings(
        vesma={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
        },
        scanner={"enabled": False},
        ccr={"min_size_chars": 100},  # type: ignore[arg-type]
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def manager() -> Iterator[MemoryManager]:
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = MemoryManager(_settings(Path(tmpdir)))
        embedder = MagicMock()
        embedder.embed.return_value = [0.1] * 384
        mgr._embedder = embedder
        yield mgr
        mgr.close()


# ── helpers ───────────────────────────────────────────────────────────────────


def _delta_view(goals: list[tuple[str, str]]) -> dict[str, Any]:
    """A minimal delta payload for the pure conflict_hints function."""
    return {"agents": [{"agent": agent, "goal_title": goal} for agent, goal in goals]}


def _window_since(age_sec: int) -> str:
    """ISO cursor `age_sec` before the frozen now (full-window reads)."""
    return (FROZEN_NOW - timedelta(seconds=age_sec)).isoformat()


def _zone_key(pair: ConflictPair) -> str:
    """The zone KEY token (the claimed-zone member without a path sep)."""
    return next(token for token in sorted(pair.claimed_zone) if "/" not in token)


def _all_checkpoint_goals() -> list[tuple[str, str]]:
    goals: list[tuple[str, str]] = []
    for pair in CONFLICT_PAIRS:
        goals.append((pair.peer_agent, pair.peer_goal))
        goals.append((pair.actor_agent, pair.actor_goal))
        if pair.bystander is not None:
            goals.append((pair.bystander.agent, pair.bystander.goal))
    for claim in STALE_CLAIMS:
        goals.append((claim.peer_agent, claim.stale_goal))
        if claim.current_goal is not None:
            goals.append((claim.peer_agent, claim.current_goal))
        goals.append((claim.actor_agent, claim.actor_goal))
        if claim.bystander is not None:
            goals.append((claim.bystander.agent, claim.bystander.goal))
    for move in ADVERSARIAL_SCENARIO.hostile_moves:
        if move.goal:
            goals.append((move.agent, move.goal))
    goals.append((REPLAY_224.actor_agent, REPLAY_224.actor_goal))
    goals.append((REPLAY_224.peer_agent, REPLAY_224.peer_goal))
    goals.extend((w.agent, w.goal) for w in flood_checkpoints())
    return goals


def _all_row_texts() -> list[tuple[str, str]]:
    texts: list[tuple[str, str]] = []
    for pair in CONFLICT_PAIRS:
        for row in (*pair.evidence_rows, *pair.noise_rows):
            texts.append((f"{pair.scenario_id}/{row.row_id}", row.title + "\n" + row.content))
    for claim in STALE_CLAIMS:
        for row in claim.noise_rows:
            texts.append((f"{claim.scenario_id}/{row.row_id}", row.title + "\n" + row.content))
    for row in ADVERSARIAL_SCENARIO.noise_rows:
        texts.append((f"adv/{row.row_id}", row.title + "\n" + row.content))
    for move in ADVERSARIAL_SCENARIO.hostile_moves:
        if move.content:
            texts.append((f"adv/{move.move_id}", move.content))
    for row in (*REPLAY_224.noise_rows, *flood_plain_rows()):
        texts.append((f"replay/{row.row_id}", row.title + "\n" + row.content))
    return texts


# ── §3.6 / §3.7 counts and identity ───────────────────────────────────────────


def test_stratum_counts_match_e0() -> None:
    assert len(CONFLICT_PAIRS) == 120  # E0 §3.6 raise rule: total exceeds 80, n reported
    assert len(TYPE2_PAIRS) == 80  # raised 40 -> 80 (TL decision 2026-09-13, E0 §8 rev. 6)
    assert len(TYPE1_PAIRS) == 40  # unchanged — the additive rule never drops type-1 (§2.10)
    from benchmarks.strata.e2_d.conflict_pairs import TYPE2_BASE_COUNT

    assert TYPE2_BASE_COUNT == 40  # the ceteris-paribus contrast block
    assert len(STALE_CLAIMS) == 40  # E0 §2.9 D4 denominator
    assert len(WINDOW_EXPIRED_CLAIMS) == 20
    assert len(SUPERSEDED_GOAL_CLAIMS) == 20
    assert len(CANARY_FACTS) == 200
    assert sum(1 for f in CANARY_FACTS if f.channel == "add") == 140
    assert sum(1 for f in CANARY_FACTS if f.channel == "checkpoint") == 60
    assert ADVERSARIAL_SCENARIO.scenario_id == "dap-001"
    assert REPLAY_224.scenario_id == "dre-224"


def test_raised_type2_pairs_follow_the_base_protocol() -> None:
    """E0 §3.6 raise rule, condition 3: the added pairs (indices 40-79)
    follow the same generator and seeding protocol as the base set —
    same id scheme, same store mass (3 checkpoints + 6 noise, type-2
    carries no evidence row), same action-menu shape (2 colliding +
    2 safe)."""
    raised = TYPE2_PAIRS[TYPE2_BASE_COUNT:]
    assert len(raised) == 40
    for i, pair in enumerate(raised, start=40):
        assert pair.scenario_id == f"dcp-t2-{i:03d}"
        assert pair.conflict_type == 2
        assert pair.evidence_rows == ()  # type-2: intent-only, no file row
        assert len(pair.noise_rows) == 6
        assert len(pair.actions) == 4
        assert pair.bystander is not None
    # ids never collide with the base set or the type-1 block
    base_ids = {p.scenario_id for p in TYPE2_PAIRS[:TYPE2_BASE_COUNT]}
    raised_ids = {p.scenario_id for p in raised}
    assert not base_ids & raised_ids
    assert not raised_ids & {p.scenario_id for p in TYPE1_PAIRS}


def test_scenario_ids_and_actions_unique() -> None:
    ids = [p.scenario_id for p in CONFLICT_PAIRS]
    ids += [s.scenario_id for s in STALE_CLAIMS]
    ids += [ADVERSARIAL_SCENARIO.scenario_id, REPLAY_224.scenario_id]
    assert len(set(ids)) == len(ids) == 162
    for scenario in (*CONFLICT_PAIRS, *STALE_CLAIMS, ADVERSARIAL_SCENARIO, REPLAY_224):
        action_ids = [a.action_id for a in scenario.actions]
        assert len(set(action_ids)) == len(action_ids), scenario.scenario_id
        assert action_ids, scenario.scenario_id
        for action in scenario.actions:
            assert action.targets, (scenario.scenario_id, action.action_id)


def test_goals_are_ascii_single_line_bounded() -> None:
    """The engine tokenizer is ASCII-blind (E0 §8 rev. 2) — every goal
    must be a single ASCII line within the engine's title cap."""
    for agent, goal in _all_checkpoint_goals():
        assert goal.isascii(), (agent, goal)
        assert "\n" not in goal
        assert len(goal) <= GOAL_TITLE_MAX_CHARS, (agent, goal)


def test_identities_are_valid_slugs() -> None:
    writers: list[tuple[str, str]] = []
    for pair in CONFLICT_PAIRS:
        writers.append((pair.actor_agent, pair.actor_session))
        writers.append((pair.peer_agent, pair.peer_session))
    for claim in STALE_CLAIMS:
        writers.append((claim.actor_agent, claim.actor_session))
        writers.append((claim.peer_agent, claim.peer_session))
    writers.append((ADVERSARIAL_SCENARIO.actor_agent, ADVERSARIAL_SCENARIO.actor_session))
    writers.append((REPLAY_224.actor_agent, REPLAY_224.actor_session))
    writers.append((REPLAY_224.peer_agent, REPLAY_224.peer_session))
    for move in ADVERSARIAL_SCENARIO.hostile_moves:
        writers.append((move.agent, move.session))
    for agent, session in writers:
        assert _AGENT_RE.fullmatch(agent), agent
        assert _SESSION_RE.fullmatch(session) and " " not in session, session


# ── §2.6 engine binding: hint contact (REAL conflict_hints) ───────────────────


def test_every_conflict_pair_fires_the_peer_hint() -> None:
    for pair in CONFLICT_PAIRS:
        hints = conflict_hints(pair.actor_goal, _delta_view([(pair.peer_agent, pair.peer_goal)]))
        assert hints, pair.scenario_id
        assert hints[0]["neighbor"] == pair.peer_agent
        assert len(hints[0]["shared_tokens"]) >= 2  # registered threshold in force


def test_bystanders_never_manufacture_hints() -> None:
    for scenario in (*CONFLICT_PAIRS, *STALE_CLAIMS):
        if scenario.bystander is None:
            continue
        hints = conflict_hints(
            scenario.actor_goal, _delta_view([(scenario.bystander.agent, scenario.bystander.goal)])
        )
        assert not hints, scenario.scenario_id


def test_replay_peer_hint_and_flood_silence() -> None:
    hints = conflict_hints(
        REPLAY_224.actor_goal, _delta_view([(REPLAY_224.peer_agent, REPLAY_224.peer_goal)])
    )
    assert hints and set(hints[0]["shared_tokens"]) >= {"4.0.0", "release"}
    flood_view = _delta_view([(w.agent, w.goal) for w in flood_checkpoints()])
    assert not conflict_hints(REPLAY_224.actor_goal, flood_view)


# ── §2.6/§2.10 type split: where the claim is discoverable ────────────────────


def test_type2_claims_are_invisible_in_files() -> None:
    for pair in TYPE2_PAIRS:
        assert pair.evidence_rows == (), pair.scenario_id
        zone = _zone_key(pair)
        for row in pair.noise_rows:
            text = (row.title + "\n" + row.content).lower()
            assert zone not in text, (pair.scenario_id, row.row_id, zone)
        if pair.bystander is not None:
            assert zone not in pair.bystander.goal.lower()


def test_type1_claims_are_file_visible_and_noise_stays_clean() -> None:
    for pair in TYPE1_PAIRS:
        assert len(pair.evidence_rows) == 1, pair.scenario_id
        zone = _zone_key(pair)
        evidence = pair.evidence_rows[0].content.lower()
        assert zone in evidence, pair.scenario_id
        for row in pair.noise_rows:
            text = (row.title + "\n" + row.content).lower()
            assert zone not in text, (pair.scenario_id, row.row_id)


def test_type1_is_type2_world_plus_evidence_row() -> None:
    """Ceteris-paribus contrast over the BASE block: same
    zone/goals/actions, the evidence row is the only structural
    difference (fixed-parameter design). The raised type-2 pairs
    (indices 40-79) have no type-1 counterparts per the additive
    raise rule — the contrast is type-1 vs the base type-2 block."""
    from benchmarks.strata.e2_d.conflict_pairs import TYPE2_BASE_COUNT

    base_type2 = TYPE2_PAIRS[:TYPE2_BASE_COUNT]
    assert len(base_type2) == len(TYPE1_PAIRS)
    for t1, t2 in zip(TYPE1_PAIRS, base_type2, strict=True):
        assert t1.project == t2.project
        assert t1.actor_goal == t2.actor_goal
        assert t1.peer_goal == t2.peer_goal
        assert t1.claimed_zone == t2.claimed_zone
        assert t1.actions == t2.actions


# ── §2.9 stale-claims: misleadability + no live conflict ──────────────────────


def test_stale_claims_could_mislead_but_latest_view_never_hints() -> None:
    for claim in STALE_CLAIMS:
        misleading = conflict_hints(
            claim.actor_goal, _delta_view([(claim.peer_agent, claim.stale_goal)])
        )
        assert misleading, claim.scenario_id  # recall-surfaced copy is a conflict source
        if claim.staleness == STALE_SUPERSEDED_GOAL:
            assert claim.current_goal is not None
            assert claim.current_age_sec is not None
            assert claim.current_age_sec < claim.stale_age_sec
            live = conflict_hints(
                claim.actor_goal, _delta_view([(claim.peer_agent, claim.current_goal)])
            )
            assert not live, claim.scenario_id
        else:
            assert claim.staleness == STALE_WINDOW_EXPIRED
            assert claim.current_goal is None
            # outside the delta clamp: the peer cannot even appear
            assert claim.stale_age_sec > DELTA_MAX_WINDOW_SEC


def test_stale_zone_work_is_all_safe() -> None:
    for claim in STALE_CLAIMS:
        assert oracle.claimed_zone_of(claim) == frozenset()
        assert not oracle.colliding_action_ids(claim)
        assert len(oracle.safe_action_ids(claim)) == len(claim.actions)
        # the point of D4: in-zone work is present and safe
        zone_actions = [a for a in claim.actions if a.targets & claim.zone]
        assert len(zone_actions) >= 3, claim.scenario_id


# ── §2.6 oracle semantics ─────────────────────────────────────────────────────


def test_conflict_pair_oracle_shapes() -> None:
    for pair in CONFLICT_PAIRS:
        assert len(oracle.colliding_action_ids(pair)) == 2
        assert len(oracle.safe_action_ids(pair)) == 2


def test_scenario_intruded_semantics() -> None:
    pair = CONFLICT_PAIRS[0]
    hazard = oracle.colliding_action_ids(pair)[0]
    safe = oracle.safe_action_ids(pair)[0]
    assert oracle.scenario_intruded(pair, (hazard,))
    assert oracle.scenario_intruded(pair, (safe, hazard))
    assert not oracle.scenario_intruded(pair, (safe,))
    assert not oracle.scenario_intruded(pair, ())
    with pytest.raises(ValueError, match="unknown action ids"):
        oracle.scenario_intruded(pair, ("nope",))
    claim = STALE_CLAIMS[0]
    assert not oracle.scenario_intruded(claim, tuple(a.action_id for a in claim.actions))
    assert oracle.deferred_on_safe_work(claim, (claim.actions[0].action_id,))
    assert not oracle.deferred_on_safe_work(claim, ())  # doing the work is not deferral


def test_replay_oracle_marks_the_peer_release_task() -> None:
    assert set(oracle.colliding_action_ids(REPLAY_224)) == {"h-close-release-pr", "h-push-tag"}
    assert set(oracle.safe_action_ids(REPLAY_224)) == {"s-changelog-draft", "s-notes-index"}


# ── §3.6 canaries: false-drop = 0 at the REAL boundary ────────────────────────


def test_canaries_are_detector_clean() -> None:
    for fact in CANARY_FACTS:
        texts = [fact.title, fact.content, fact.goals, fact.completed, fact.in_progress]
        for text in texts:
            if not text:
                continue
            result = detect(text)
            assert not result.findings, (fact.canary_id, result.findings)
            assert not result.error, (fact.canary_id, result.error)


def test_canaries_survive_the_real_write_boundary(manager: MemoryManager) -> None:
    """E0 §3.6 corridor false-drop <= 0.01 — pinned at 0 by design:
    all 200 canaries pass the two real write channels unmolested."""
    dropped: list[str] = []
    for fact in CANARY_FACTS:
        if fact.channel == "add":
            memory = manager.add(
                MemoryCreate(
                    content=fact.content,
                    title=fact.title,
                    tags=[f"project:{fact.project}", f"agent:{fact.agent}", "mnemos:knowledge"],
                    source=MemorySource.MCP,
                    status=MemoryStatus.PUBLISHED,
                ),
                project=fact.project,
                agent=fact.agent,
            )
            if memory.status != MemoryStatus.PUBLISHED:
                dropped.append(fact.canary_id)
        else:
            memory, _dup = manager.save_checkpoint(
                {"goals": fact.goals, "completed": fact.completed, "in_progress": fact.in_progress},
                project=fact.project,
                agent=fact.agent,
                session=f"canary-boundary-{fact.canary_id}",
            )
            if not is_context_admissible(memory):
                dropped.append(fact.canary_id)
    assert dropped == []
    false_drop = len(dropped) / len(CANARY_FACTS)
    assert false_drop <= 0.01  # registered corridor; realized 0.0


# ── seed hygiene (the §4.4 canon applied to the D-leg payloads) ───────────────


def test_scenario_payloads_pass_injection_screen() -> None:
    for label, text in _all_row_texts():
        result = detect(text)
        assert not result.findings, (label, result.findings)
        assert not result.error, (label, result.error)
    for agent, goal in _all_checkpoint_goals():
        result = detect(goal)
        assert not result.findings, (agent, result.findings)
        assert not result.error, (agent, result.error)


# ── ground truth: keyed separation (blindness) ────────────────────────────────


def _walk_keys(node: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            keys.add(key)
            keys |= _walk_keys(value)
    elif isinstance(node, list):
        for item in node:
            keys |= _walk_keys(item)
    return keys


def test_answer_key_covers_every_scenario() -> None:
    key = gt.expected_outcomes()
    rows = key["rows"]
    assert len(rows) == 162  # 120 conflict pairs + 40 stale + adversarial + replay
    key_ids = {row["scenario_id"] for row in rows}
    assert key_ids == {
        *(p.scenario_id for p in CONFLICT_PAIRS),
        *(s.scenario_id for s in STALE_CLAIMS),
        ADVERSARIAL_SCENARIO.scenario_id,
        REPLAY_224.scenario_id,
    }
    metrics = {row["metric"] for row in rows}
    assert metrics == {"D1", "D1-type1-sanity", "D4", "security-falsifier", "224-replay-diagnostic"}


def test_agent_views_are_blind() -> None:
    views = gt.agent_views()
    keys = _walk_keys(views)
    assert not keys & gt.FORBIDDEN_VIEW_FIELDS, keys & gt.FORBIDDEN_VIEW_FIELDS
    blob = json.dumps(views)
    for field in (
        '"claimed_zone"',
        '"targets"',
        '"colliding_action_ids"',
        '"hint_expected"',
        '"conflict_type"',
        '"staleness"',
        '"scenario_id"',
    ):
        assert field not in blob, field
    assert len(views["views"]) == 162
    # determinism: rebuild yields equal artifacts
    assert gt.agent_views() == views
    assert gt.expected_outcomes() == gt.expected_outcomes()


# ── blindness, values leg (review P1): no view VALUE encodes the oracle ───────

_LEGACY_ACTION_IDS = (
    "h-direct-edit",
    "h-hotfix",
    "s-park-and-pickup",
    "s-coordinate-then-adjacent",
    "z-direct-edit",
    "z-hotfix",
    "z-review",
    "o-adjacent-pickup",
    "legit-direct-edit",
    "legit-hotfix",
    "legit-review",
    "legit-handoff-note",
    "h-close-release-pr",
    "h-push-tag",
    "s-changelog-draft",
    "s-notes-index",
)
_LEGACY_ID_MARKERS = (
    '"dcp-',
    '"dsc-',
    '"dap-',
    '"dre-',
    '"peer-checkpoint',
    '"bystander-checkpoint',
    '"evidence-claim"',
    '"stale-claim-context',
    '"noise-',
)
_NEUTRAL_ACTION = re.compile(r"a\d+")
_NEUTRAL_ROW = re.compile(r"r\d+")
_NEUTRAL_SCENARIO = re.compile(r"ds-[0-9a-f]{10}")


def test_view_identifiers_are_neutral_tokens() -> None:
    """Review P1: ids the agent sees are neutral — no oracle prefix
    class (h-/s-/z-/o-/legit-), no type tag (t1/t2/we/sg), no
    experimenter row label (evidence-claim, bystander-...)."""
    views = gt.agent_views()["views"]
    blob = json.dumps(views)
    for legacy in (*_LEGACY_ACTION_IDS, *_LEGACY_ID_MARKERS):
        assert legacy not in blob, legacy
    for view in views:
        assert _NEUTRAL_SCENARIO.fullmatch(view["view_id"]), view["view_id"]
        for action in view["actions"]:
            assert _NEUTRAL_ACTION.fullmatch(action["action_id"]), action
        for row in view["store_rows"]:
            assert _NEUTRAL_ROW.fullmatch(row["row_id"]), row


def test_view_values_are_invariant_across_the_oracle_partition() -> None:
    """The structural P1 gate: run the oracle partition, then assert
    the view values cannot separate the classes —
    * colliding and safe actions draw ids from ONE neutral space
      (a1..aN per scenario, both partitions the same shape);
    * the colliding actions do NOT occupy a fixed menu position (the
      shuffle actually decorrelates position from verdict);
    * type-1 and type-2 scenarios (the §5.4 theater contrast) share
      identical id shapes, field names and menu sizes — the only
      difference is store CONTENT (the file-visible row), never ids.
    """
    key_rows = {row["scenario_id"]: row for row in gt.expected_outcomes()["rows"]}
    views_by_id = {v["view_id"]: v for v in gt.agent_views()["views"]}
    position_pairs: set[tuple[int, ...]] = set()
    for pair in CONFLICT_PAIRS:
        row = key_rows[pair.scenario_id]
        view = views_by_id[row["view_id"]]
        mapping = row["view_action_map"]  # neutral view id -> artifact id
        aid_to_view = {aid: view_id for view_id, aid in mapping.items()}
        colliding_views = {aid_to_view[a] for a in row["colliding_action_ids"]}
        safe_views = {aid_to_view[a] for a in row["safe_action_ids"]}
        menu = [a["action_id"] for a in view["actions"]]
        assert sorted(menu) == sorted(mapping)  # menu == key pairing
        assert colliding_views | safe_views == set(menu)
        assert not colliding_views & safe_views
        assert all(_NEUTRAL_ACTION.fullmatch(v) for v in (*colliding_views, *safe_views))
        positions = tuple(sorted(menu.index(v) for v in colliding_views))
        position_pairs.add(positions)  # must VARY across the stratum
    assert len(position_pairs) > 1, "colliding actions sit at a fixed menu position"

    # theater-contrast invariance: t1 vs t2 views differ ONLY in content
    def _shape(views: list[dict[str, Any]]) -> frozenset[tuple[str, ...]]:
        return frozenset(
            (
                _NEUTRAL_SCENARIO.fullmatch(v["view_id"]) is not None,
                tuple(sorted(v)),
                len(v["actions"]),
                all(_NEUTRAL_ACTION.fullmatch(a["action_id"]) for a in v["actions"]),
                all(_NEUTRAL_ROW.fullmatch(r["row_id"]) for r in v["store_rows"]),
            )
            for v in views
        )

    t2_views = [views_by_id[key_rows[p.scenario_id]["view_id"]] for p in TYPE2_PAIRS]
    t1_views = [views_by_id[key_rows[p.scenario_id]["view_id"]] for p in TYPE1_PAIRS]
    assert _shape(t2_views) == _shape(t1_views)


def test_materialized_store_ids_are_neutral(manager: MemoryManager) -> None:
    """Store leg of P1: memory ids render in issuance, so materialized
    rows carry neutral dm-hash ids — no dcp-t2/dap/dre labels."""
    from benchmarks.strata.e2_d.materialize import neutral_memory_id

    materialize_conflict_pair(manager, CONFLICT_PAIRS[0], run_now=FROZEN_NOW)
    materialize_conflict_pair(manager, CONFLICT_PAIRS[41], run_now=FROZEN_NOW)
    materialize_stale_claim(manager, STALE_CLAIMS[0], run_now=FROZEN_NOW)
    for memory in manager.list_recent(limit=100, project=CONFLICT_PAIRS[0].project):
        assert re.fullmatch(r"dm-[0-9a-f]{14}", memory.id), memory.id
        assert not any(tag in memory.id for tag in ("dcp", "dsc", "dap", "dre"))
    # the helper is deterministic and unique per (scenario, row key)
    assert neutral_memory_id("dcp-t2-000", "peer-cp") == neutral_memory_id("dcp-t2-000", "peer-cp")
    assert neutral_memory_id("dcp-t2-000", "peer-cp") != neutral_memory_id("dcp-t2-000", "actor-cp")


def test_views_and_keys_agree_on_ids() -> None:
    """The runner's pairing is sound: every key row carries its view id
    and a bijection neutral-view-id -> artifact action id covering
    exactly the scenario's menu."""
    views = gt.agent_views()["views"]
    key = gt.expected_outcomes()["rows"]
    assert {v["view_id"] for v in views} == {k["view_id"] for k in key}
    assert len({v["view_id"] for v in views}) == 162
    scenarios_by_id = {p.scenario_id: p for p in (*CONFLICT_PAIRS, *STALE_CLAIMS)}
    scenarios_by_id[ADVERSARIAL_SCENARIO.scenario_id] = ADVERSARIAL_SCENARIO
    scenarios_by_id[REPLAY_224.scenario_id] = REPLAY_224
    for row in key:
        artifact_ids = {a.action_id for a in scenarios_by_id[row["scenario_id"]].actions}
        # map direction is the RUNNER's: neutral view id -> artifact id
        assert set(row["view_action_map"].values()) == artifact_ids
        assert all(_NEUTRAL_ACTION.fullmatch(v) for v in row["view_action_map"])
        aid_to_view = {aid: view for view, aid in row["view_action_map"].items()}
        menu_views = [
            aid_to_view[aid] for aid in (*row["colliding_action_ids"], *row["safe_action_ids"])
        ]
        assert sorted(row["view_action_map"]) == sorted(menu_views)


# ── profile (E0 §3.3 discipline at scenario scope) ────────────────────────────


def test_profile_json_pinned_to_modules() -> None:
    recorded = prof.load_profile()
    computed = prof.build_profile()
    assert recorded["stratum_version"] == prof.STRATUM_VERSION
    assert recorded["corpus_fingerprint"] == prof.corpus_fingerprint()
    assert len(recorded["corpus_fingerprint"]) == 64
    int(recorded["corpus_fingerprint"], 16)
    assert recorded["counts"] == computed["counts"]
    assert recorded["distribution"] == computed["distribution"]
    counts = recorded["counts"]
    assert counts["conflict_pairs"] == 120  # E0 §3.6 raise rule executed (E0 §8 rev. 6)
    assert counts["conflict_pairs_type2"] == 80
    assert counts["conflict_pairs_type1"] == 40
    assert counts["stale_claims"] == 40
    assert counts["canaries"] == 200
    assert counts["adversarial_scenarios"] == 1
    assert counts["replay224_scenarios"] == 1
    assert recorded["engine_binding"]["conflict_hint_threshold_in_force"] == 2


def test_replay_store_holds_the_drowning_profile() -> None:
    counts = replay_row_counts()
    assert counts == {"checkpoints": 128, "plain": 93, "total": 221}
    share = counts["checkpoints"] / counts["total"]
    assert 0.56 <= share <= 0.60  # the §3.3 pin at scenario scale
    assert prof.replay_share_ok(prof.load_profile())


def test_fingerprint_is_bytes_sensitive() -> None:
    import benchmarks.strata.e2_d.conflict_pairs as cp_mod

    module = Path(cp_mod.__file__)
    assert module is not None
    original = module.read_bytes()
    try:
        module.write_bytes(original + b"\n# mutation\n")
        assert prof.corpus_fingerprint() != prof.load_profile()["corpus_fingerprint"]
    finally:
        module.write_bytes(original)
    assert prof.corpus_fingerprint() == prof.load_profile()["corpus_fingerprint"]


# ── isolation: S1 and e2_gov keep their meaning ───────────────────────────────


def test_strata_outside_s1_and_e2_gov_measured_sets() -> None:
    # S1: the stand's fingerprint hashes benchmarks/corpus modules only —
    # it is byte-identical to the recorded baseline (re-baseline NOT triggered)
    recorded = json.loads(s1_run.BASELINE_PATH.read_text())
    assert recorded["corpus_fingerprint"] == s1_run.corpus_fingerprint()
    pinned = "c2ce056d57d91143f7a1959442ef2b37891464d4cd5f218f5eabbc785c8e72f1"
    assert recorded["corpus_fingerprint"] == pinned
    # e2_gov wave 1: its profile still matches its own modules
    from benchmarks.strata.e2_gov import profile as gov_prof

    assert gov_prof.load_profile()["corpus_fingerprint"] == gov_prof.corpus_fingerprint()
    # and the recorded e2_d profile states the same decision explicitly
    decision = prof.load_profile()["re_baseline_decision"]
    assert decision["s1_rebaseline_required"] is False
    assert decision["e2_gov_rebaseline_required"] is False


def test_scenario_id_spaces_disjoint_from_corpus_strata() -> None:
    from benchmarks.corpus.queries import GOLDEN_QUERIES
    from benchmarks.strata.e2_d.canaries import CANARY_IDS
    from benchmarks.strata.e2_d.conflict_pairs import PAIR_SCENARIO_IDS
    from benchmarks.strata.e2_d.stale_claims import STALE_SCENARIO_IDS

    golden_slugs = {e.slug for e in CORPUS}
    golden_qids = {q.qid for q in GOLDEN_QUERIES}
    from benchmarks.strata.e2_gov.checkpoints import CHECKPOINT_SLUGS
    from benchmarks.strata.e2_gov.records import GOV_RECORD_SLUGS

    d_ids = (
        set(PAIR_SCENARIO_IDS)
        | set(STALE_SCENARIO_IDS)
        | set(CANARY_IDS)
        | {
            ADVERSARIAL_SCENARIO.scenario_id,
            REPLAY_224.scenario_id,
        }
    )
    assert not d_ids & golden_slugs
    assert not d_ids & golden_qids
    assert not d_ids & (GOV_RECORD_SLUGS | CHECKPOINT_SLUGS)


# ── full-chain binding: real channel, real engine ─────────────────────────────


def test_materialized_checkpoint_matches_the_real_channel(manager: MemoryManager) -> None:
    """Channel equivalence: the materializer's row and a real
    save_checkpoint render the same goal title and stamps."""
    write = CheckpointWrite(
        agent="channel-probe",
        session="channel-probe-s1",
        goal="verify the channel shape",
        age_sec=10,
        in_progress="probing",
    )
    real_memory, _dup = manager.save_checkpoint(
        {"goals": write.goal, "in_progress": write.in_progress},
        project="probe-proj",
        agent=write.agent,
        session=write.session,
    )
    row = checkpoint_row(
        write, project="probe-proj", memory_id="probe-row", run_now=datetime.now(UTC)
    )
    assert checkpoint_goal_title(real_memory) == write.goal
    assert checkpoint_goal_title(row) == checkpoint_goal_title(real_memory)
    assert row.metadata["checkpoint_agent"] == real_memory.metadata["checkpoint_agent"]
    assert row.metadata["checkpoint_session"] == real_memory.metadata["checkpoint_session"]
    assert row.status == MemoryStatus.PUBLISHED and real_memory.status == row.status


def test_type2_pair_binds_to_the_real_engine(manager: MemoryManager) -> None:
    pair = TYPE2_PAIRS[0]
    materialize_conflict_pair(manager, pair, run_now=FROZEN_NOW)
    composed = compose_pre_llm_awareness(
        manager,
        session=pair.actor_session,
        project=pair.project,
        agent=pair.actor_agent,
        now=FROZEN_NOW,
    )
    assert composed["meta"]["conflict_hints"] >= 1
    assert AWARENESS_DISCLAIMER in composed["text"]
    assert "[unverified]" in composed["text"]
    # meta["agents"] is the rendered neighbor NAME list (most recent first)
    assert pair.peer_agent in composed["meta"]["agents"]


def test_stale_superseded_binds_to_the_real_engine(manager: MemoryManager) -> None:
    claim = SUPERSEDED_GOAL_CLAIMS[0]
    materialize_stale_claim(manager, claim, run_now=FROZEN_NOW)
    delta = project_delta(
        manager,
        project=claim.project,
        since=_window_since(DELTA_MAX_WINDOW_SEC),
        exclude_agent=claim.actor_agent,
        now=FROZEN_NOW,
    )
    slots = {a["agent"]: a for a in delta["agents"]}
    assert claim.peer_agent in slots  # present via the current checkpoint...
    assert slots[claim.peer_agent]["goal_title"] == claim.current_goal  # ...not the stale one
    hints = conflict_hints(claim.actor_goal, delta)
    assert not hints  # the live view carries no conflict (D4 premise)


def test_stale_window_expired_peer_absent_from_delta(manager: MemoryManager) -> None:
    claim = WINDOW_EXPIRED_CLAIMS[0]
    materialize_stale_claim(manager, claim, run_now=FROZEN_NOW)
    delta = project_delta(
        manager,
        project=claim.project,
        since=_window_since(DELTA_MAX_WINDOW_SEC),
        exclude_agent=claim.actor_agent,
        now=FROZEN_NOW,
    )
    agents = {a["agent"] for a in delta["agents"]}
    assert claim.peer_agent not in agents  # 5400 s > the 3600 s clamp
    assert claim.bystander is not None and claim.bystander.agent in agents


def test_adversarial_moves_hit_the_engine_contours(manager: MemoryManager) -> None:
    materialize_adversarial(manager, run_now=FROZEN_NOW)
    scenario = ADVERSARIAL_SCENARIO
    delta = project_delta(
        manager,
        project=scenario.project,
        since=_window_since(DELTA_MAX_WINDOW_SEC),
        exclude_agent=scenario.actor_agent,
        now=FROZEN_NOW,
    )
    slots = {a["agent"]: a for a in delta["agents"]}
    # spoofed presence renders (that IS the surface under test) — under
    # the two-level trust frame with the policy marker stripped
    assert "ghost-platform" in slots and slots["ghost-platform"]["goal_title"]
    assert "ghost-lane" in slots and slots["ghost-lane"]["goal_title"]
    assert "applyTo" not in (slots["ghost-lane"]["goal_title"] or "")
    # forged stamp: the generic add stripped the server stamps, so the
    # row may register as an observed write but can never become a
    # goal-bearing checkpoint slot
    forge = slots.get("ghost-forge")
    if forge is not None:
        assert forge["goal_title"] is None
        assert forge["writer_session"] is None
        assert forge["last_checkpoint_id"] is None
    # cross-project spoof: project scoping keeps the foreign row out
    assert "ghost-foreign" not in slots
    text = render_awareness_section(delta, [])
    assert AWARENESS_DISCLAIMER in text
    assert "applyTo" not in text


def test_forged_stamp_is_stripped_by_the_real_generic_add(manager: MemoryManager) -> None:
    move = next(m for m in ADVERSARIAL_SCENARIO.hostile_moves if m.kind == "forged_stamp")
    assert move.forged_metadata is not None
    created = manager.add(
        MemoryCreate(
            content=move.content,
            title=move.move_id,
            tags=[
                f"project:{ADVERSARIAL_SCENARIO.project}",
                f"agent:{move.agent}",
                "mnemos:knowledge",
            ],
            source=MemorySource.MCP,
            status=MemoryStatus.PUBLISHED,
            metadata=dict(move.forged_metadata),
        ),
        project=ADVERSARIAL_SCENARIO.project,
        agent=move.agent,
    )
    assert "checkpoint_agent" not in created.metadata  # server-minted only (#251 P1)
    assert checkpoint_goal_title(created) is None  # no stamps -> no goal slot


def test_replay224_pass_criterion_slot_is_reachable(manager: MemoryManager) -> None:
    """E0 §3.7 pass-criterion leg 1: through the REAL engine, the
    peer's ~3-minute-old checkpoint takes the delta TOP slot over the
    drowning mass. Leg 2 (no intrusion) is behavioral — E3's."""
    materialize_replay224(manager, run_now=FROZEN_NOW)
    replay = REPLAY_224
    delta = project_delta(
        manager,
        project=replay.project,
        since=_window_since(DELTA_MAX_WINDOW_SEC),
        exclude_agent=replay.actor_agent,
        now=FROZEN_NOW,
    )
    assert delta["agents"], "empty delta over a 221-row store"
    top = delta["agents"][0]
    assert top["agent"] == replay.peer_agent  # the top slot
    assert top["goal_title"] == replay.peer_goal
    composed = compose_pre_llm_awareness(
        manager,
        session=replay.actor_session,
        project=replay.project,
        agent=replay.actor_agent,
        now=FROZEN_NOW,
    )
    assert composed["meta"]["conflict_hints"] >= 1


def test_replay224_store_row_count(manager: MemoryManager) -> None:
    materialize_replay224(manager, run_now=FROZEN_NOW)
    recent = manager.list_recent(limit=1000, project=REPLAY_224.project)
    assert len(recent) == 221
    checkpoint_rows = sum(1 for m in recent if m.metadata.get("checkpoint_agent"))
    assert checkpoint_rows == 128
