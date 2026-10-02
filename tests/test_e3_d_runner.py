"""E3 D-leg runner — smoke tests over the REAL strata (D-runner wave).

These are RUNNER-MACHINERY gates, not an experiment run: collect-only
executes both arms over the real D-scenario strata through the REAL
awareness engine, validates the artifacts and persists NOTHING. The
first recorded D run stays a deliberate human decision after this
infrastructure merges (E0 anti-HARKing window).

Pins:

* dry-run over the real scenarios produces a WELL-FORMED manifest +
  paired outcomes without persisting a run;
* the leg flag ACTUALLY toggles the composition path: treatment calls
  ``compose_pre_llm_awareness`` once per scenario, control never does;
* the dap-001 contract (#286 residual): the adversarial actor goal
  comes from the runner's live session, never from the store — no
  actor row exists, the hint layer stays OFF for dap-001, no
  spoof-attributable deferral in either arm (E0 §5.4);
* the abstention chain is reconstructable from the store's traces
  alone (abstention → delta-block → checkpoint-id → writer-session);
* the statistics ban is schema — smuggled stat keys anywhere in the
  outcomes artifact are refused, and ``record_run`` routes through the
  gate;
* refusal without ``--record`` (zero writes); ``--record`` writes
  write-once, atomic artifacts; a re-record fails loud;
* the oracle tally layer on synthetic probe outcomes (independent of
  the real probe's behavior).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from benchmarks.experiments.e3_d import runner
from benchmarks.strata.e2_d.adversarial import ADVERSARIAL_SCENARIO

from vesma.awareness import ABSTENTION_TASK_LABEL
from vesma.lanes import awareness_cursor_key

_TREATMENT = runner.LEGS[1]
_CONTROL = runner.LEGS[0]

_NEUTRAL_ACTION = re.compile(r"^a\d+$")


@pytest.fixture(scope="module")
def collected() -> Iterator[tuple[dict, dict]]:
    """One full collect over the real strata (both arms)."""
    manifest, outcomes = runner.collect_run()
    yield manifest, outcomes


# ── dry-run over the real strata ─────────────────────────────────────────────


def test_dry_run_manifest_well_formed(collected: tuple[dict, dict]) -> None:
    manifest, _ = collected
    runner.verify_manifest(manifest)  # raises on any drift
    assert manifest["experiment"] == "e3-d"
    assert manifest["legs"] == {
        "control": {"include_awareness": False},
        "treatment": {"include_awareness": True},
    }
    # the raised stratum is pinned inside the manifest (E0 §8 rev. 6)
    assert manifest["stratum"]["counts"] == {
        "type2": 80,
        "type1": 40,
        "stale": 40,
        "adversarial": 1,
        "replay224": 1,
        "total": 162,
    }
    assert manifest["stratum"]["stratum_version"] == "e2-d-2"
    assert len(manifest["stratum"]["corpus_fingerprint"]) == 64
    # the probe policy is REGISTERED in the manifest (the instrument)
    assert manifest["probe_policy"]["name"] == runner.PROBE_POLICY["name"]
    assert manifest["probe_policy"]["arm_blindness"]
    # the dap-001 contract pin rides every manifest
    for key in ("pin", "consequence", "source"):
        assert manifest["dap_001_contract"][key]
    # frozen scenario clock + engine binding
    assert manifest["scenario_clock"]["run_now"] == runner.RUN_NOW.isoformat()
    assert manifest["stratum"]["engine_binding"]["conflict_hint_threshold_in_force"] == 2
    code = manifest["code"]
    assert code["mnemos_version"] and code["version_file"]
    assert code["git_commit"] not in ("", None)


def test_dry_run_outcomes_paired_and_complete(collected: tuple[dict, dict]) -> None:
    _, outcomes = collected
    runner.verify_outcomes(outcomes)
    assert outcomes["legs"] == ["control", "treatment"]
    rows = outcomes["scenarios"]
    assert len(rows) == 162
    view_ids = [r["view_id"] for r in rows]
    assert len(set(view_ids)) == 162  # pairing keys unique, ds-hash ids
    assert all(re.fullmatch(r"ds-[0-9a-f]{10}", v) for v in view_ids)
    strata: dict[str, int] = {}
    for row in rows:
        strata[row["stratum"]] = strata.get(row["stratum"], 0) + 1
        for leg in ("control", "treatment"):
            assert isinstance(row[leg]["intruded"], bool)
            assert isinstance(row[leg]["deferred"], bool)
            # the agent's choices live in the NEUTRAL view space only
            for aid in (*row[leg]["chosen"], *row[leg]["abstained"]):
                assert _NEUTRAL_ACTION.fullmatch(aid), (row["view_id"], leg, aid)
    assert strata == {"type2": 80, "type1": 40, "stale": 40, "adversarial": 1, "replay224": 1}


def test_discordance_tallies_consistent_with_scenarios(
    collected: tuple[dict, dict],
) -> None:
    """The persisted McNemar inputs are counts over the raw pairs — no
    statistics beyond tallies at run time (E0 §6.1/§6.6)."""
    _, outcomes = collected
    rows = outcomes["scenarios"]
    for stratum, field, key in (
        ("type2", "intruded", "type2_intrusion"),
        ("type1", "intruded", "type1_intrusion"),
        ("stale", "deferred", "stale_over_deferral"),
    ):
        subset = [r for r in rows if r["stratum"] == stratum]
        assert outcomes["discordance"][key] == {
            "treatment_only": sum(
                1 for r in subset if r["treatment"][field] and not r["control"][field]
            ),
            "control_only": sum(
                1 for r in subset if r["control"][field] and not r["treatment"][field]
            ),
        }
    for leg, rates in outcomes["arm_rates"].items():
        hits = sum(1 for r in rows if r["stratum"] == "type2" and r[leg]["intruded"])
        assert rates["type2_intrusion_rate"] == hits / 80
    assert not any(k in json.dumps(outcomes) for k in ('"p_value"', '"ci95"', '"verdict"'))


def test_treatment_pathway_delivered(collected: tuple[dict, dict]) -> None:
    """Instrument-level sanity of the composition pathway: the
    treatment arm actually received the engine's hints on the type-2
    stratum, and none on the stale stratum (the registered live-view
    premise); the control arm renders no hints by construction."""
    _, outcomes = collected
    rows = {
        stratum: [r for r in outcomes["scenarios"] if r["stratum"] == stratum]
        for stratum in ("type2", "stale")
    }
    assert all(r["treatment"]["hints"] >= 1 for r in rows["type2"])
    assert all(r["control"]["hints"] == 0 for r in outcomes["scenarios"])
    assert all(r["treatment"]["hints"] == 0 for r in rows["stale"])


def test_collect_is_deterministic(collected: tuple[dict, dict]) -> None:
    """Same stratum + code + frozen clock → same run id and identical
    outcomes (the only run-unique field is the uuid trace id inside an
    abstention chain — a store pointer, not a measurement)."""
    first_manifest, first_outcomes = collected
    second_manifest, second_outcomes = runner.collect_run()
    assert second_manifest["run_id"] == first_manifest["run_id"]

    def _stable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        stripped = []
        for row in rows:
            row = dict(row)
            for leg in ("control", "treatment"):
                arm = dict(row[leg])
                if arm.get("abstention_chain"):
                    arm["abstention_chain"] = {
                        k: v for k, v in arm["abstention_chain"].items() if k != "abstention_trace"
                    }
                row[leg] = arm
            stripped.append(row)
        return stripped

    assert _stable(second_outcomes["scenarios"]) == _stable(first_outcomes["scenarios"])
    assert second_outcomes["discordance"] == first_outcomes["discordance"]


# ── the leg flag toggles the REAL composition path ────────────────────────────


def test_flag_toggles_include_awareness_in_composition_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Treatment: one REAL compose_pre_llm_awareness call per scenario.
    Control: zero calls — the include_awareness=False path renders no
    awareness anywhere (byte-identical agent context)."""
    real = runner.awareness_mod.compose_pre_llm_awareness
    calls: list[str] = []

    def counting(*args: Any, **kwargs: Any) -> Any:
        calls.append(str(kwargs.get("project")))
        return real(*args, **kwargs)

    monkeypatch.setattr(runner.awareness_mod, "compose_pre_llm_awareness", counting)
    sample = (
        runner.SCENARIO_CASES[0],  # type2
        runner.SCENARIO_CASES[80],  # type1
        runner.SCENARIO_CASES[120],  # stale
        runner.SCENARIO_CASES[160],  # adversarial
        runner.SCENARIO_CASES[161],  # replay
    )
    for case in sample:
        control = runner.execute_arm(case, _CONTROL, tmp_path / f"c-{case.view['view_id']}")
        assert control["hints"] == 0
    assert calls == []  # the control arm NEVER composes
    for case in sample:
        treatment = runner.execute_arm(case, _TREATMENT, tmp_path / f"t-{case.view['view_id']}")
        assert isinstance(treatment["hints"], int)
    assert len(calls) == 5  # exactly one REAL composition per scenario


# ── the dap-001 contract pin (#286 review residual) ───────────────────────────


def _ghost_goals(mgr: Any) -> list[tuple[str, str]]:
    """(agent, goal title) of every goal-bearing ghost checkpoint."""
    from vesma.awareness import checkpoint_goal_title

    goals: list[tuple[str, str]] = []
    for memory in mgr.list_recent(limit=100, project=ADVERSARIAL_SCENARIO.project):
        if memory.agent.startswith("ghost-"):
            title = checkpoint_goal_title(memory)
            if title:
                goals.append((memory.agent, title))
    return goals


def test_dap001_actor_goal_comes_from_the_live_session_not_the_store(
    tmp_path: Path,
) -> None:
    """The pin: the adversarial store holds NO actor row; the goal the
    policy reads is the artifact's live-session goal; the engine's hint
    layer (my goal = my last STORE checkpoint) therefore stays OFF —
    even though the ghost goals lexically overlap the artifact goal, a
    hint CANNOT fire — and no deferral is attributable to the spoof in
    either arm (E0 §5.4 security contour)."""
    from vesma.awareness import conflict_hints

    case = runner.SCENARIO_CASES[160]
    assert case.stratum == "adversarial"
    assert case.live_session_goal == ADVERSARIAL_SCENARIO.actor_goal

    mgr = runner._fresh_manager(tmp_path / "dap-store")
    try:
        case.materialize(mgr)
        recent = mgr.list_recent(limit=100, project=case.artifact.project)
        assert recent, "the adversarial store is empty"
        actor_rows = [m for m in recent if m.agent == ADVERSARIAL_SCENARIO.actor_agent]
        assert actor_rows == [], "dap-001 store must hold NO actor row (the pin)"
        # the hint layer is structurally off: the engine cannot see the
        # actor goal because it was never checkpointed into the store
        composed = runner.awareness_mod.compose_pre_llm_awareness(
            mgr,
            session=ADVERSARIAL_SCENARIO.actor_session,
            project=ADVERSARIAL_SCENARIO.project,
            agent=ADVERSARIAL_SCENARIO.actor_agent,
            now=runner.RUN_NOW,
        )
        assert composed["meta"]["conflict_hints"] == 0
        # ...yet the ghosts' goals DO lexically overlap the artifact goal
        # (a store-side actor checkpoint would have manufactured hints)
        ghosts = _ghost_goals(mgr)
        assert ghosts, "the ghosts did not land"
        assert conflict_hints(
            ADVERSARIAL_SCENARIO.actor_goal,
            {"agents": [{"agent": agent, "goal_title": goal} for agent, goal in ghosts]},
        ), "load-bearing check: ghost goals overlap the artifact goal"
    finally:
        mgr.close()

    for leg in runner.LEGS:
        arm = runner.execute_arm(case, leg, tmp_path / f"dap-{leg.leg}")
        assert arm["chosen"], f"{leg.leg}: the agent still does its legitimate work"
        assert not arm["deferred"], f"{leg.leg}: no spoof-attributable deferral (E0 §5.4)"
        assert arm["abstention_chain"] is None


# ── the abstention chain is reconstructable from the store ────────────────────


def test_abstention_chain_reconstructable_from_traces(tmp_path: Path) -> None:
    """E0 §7 / R3: abstention → delta-block → checkpoint-id →
    writer-session must be reconstructable from the trace store alone."""
    case = runner.SCENARIO_CASES[0]  # a type-2 pair: the treatment arm abstains
    arm = runner.execute_arm(case, _TREATMENT, tmp_path / "chain-store")
    chain = arm["abstention_chain"]
    assert chain is not None, "the type-2 treatment arm must record an abstention"

    mgr = runner._fresh_manager(tmp_path / "chain-store")  # reopen the same store
    try:
        traces = mgr.sqlite.list_traces(
            project=case.artifact.project, task_label=ABSTENTION_TASK_LABEL
        )
        assert traces, "no abstention trace persisted"
        trace = traces[0]
        # the trace row's item_id IS the neighbor checkpoint id (the chain)
        assert trace.item_id == chain["checkpoint_id"] == chain["delta_block_basis"]
        assert str(trace.id) == chain["abstention_trace"]
        # the rationale names BOTH legs: abstainer and writer session
        assert f"abstainer={case.artifact.actor_agent}" in trace.rationale_summary
        assert f"neighbor={chain['neighbor_agent']}" in trace.rationale_summary
        assert f"writer_session={chain['writer_session']}" in trace.rationale_summary
        # the basis row is the PEER's #251 checkpoint (server stamps)
        basis = mgr.sqlite.get(chain["checkpoint_id"])
        assert basis is not None and basis.agent == case.artifact.peer_agent
        assert basis.metadata.get("checkpoint_session") == chain["writer_session"], (
            "the chain's writer-session leg must match the basis row's server stamp"
        )
    finally:
        mgr.close()


# ── per-pair isolation, pinned (review #294/#295) ─────────────────────────────


def test_per_pair_isolation_row_mass_contract(tmp_path: Path) -> None:
    """Isolation is PINNED, not just structural: one control and one
    treatment store are re-opened and the materializer's row-mass
    contract is asserted — the two arms of a paired probe materialize
    IDENTICAL stores (same ids, same contents, same tag stamps), and
    the treatment arm's composition adds exactly its ``awr:`` cursor
    row (a meta-table entry, no memory row, no content drift).

    This is the machine-checkable form of the manifest's
    ``store_isolation`` wording (#295): identical MATERIALIZED store
    bytes; the treatment composition writes its awareness cursor per
    the registered composition.
    """
    case = runner.SCENARIO_CASES[0]  # a type-2 pair: the treatment arm composes
    pair = case.artifact
    control_dir = tmp_path / f"control-{case.view['view_id']}"
    treatment_dir = tmp_path / f"treatment-{case.view['view_id']}"
    runner.execute_arm(case, _CONTROL, control_dir)
    runner.execute_arm(case, _TREATMENT, treatment_dir)

    # Re-open each store fresh (what an auditor sees on disk).
    control_mgr = runner._fresh_manager(control_dir)
    treatment_mgr = runner._fresh_manager(treatment_dir)
    try:
        control_rows = control_mgr.list_recent(limit=1000, project=pair.project)
        treatment_rows = treatment_mgr.list_recent(limit=1000, project=pair.project)
        # ROW-MASS contract: the materializer writes the same row count
        # in both arms, same neutral ids, same contents — byte-identical
        # materialized stores (deterministic embedder, frozen clock).
        assert len(control_rows) == len(treatment_rows), (
            "the paired arms must materialize the same row mass"
        )
        assert [m.id for m in control_rows] == [m.id for m in treatment_rows]
        assert [(m.id, m.content, m.tags, m.agent) for m in control_rows] == [
            (m.id, m.content, m.tags, m.agent) for m in treatment_rows
        ]
        # The only composition write: the awareness cursor, and ONLY in
        # the treatment store — a meta-table row, not a memory row
        # (control stays cursor-less by construction). The cursor is the
        # high-water + 1µs exclusive bound stamped by the composition.
        awr_key = awareness_cursor_key(
            project=pair.project, agent=pair.actor_agent, session=pair.actor_session
        )
        assert control_mgr.sqlite.get_meta(awr_key) is None
        treatment_cursor = treatment_mgr.sqlite.get_meta(awr_key)
        assert treatment_cursor is not None, "the treatment composition must stamp its cursor"
        assert datetime.fromisoformat(treatment_cursor) <= runner.RUN_NOW.astimezone(UTC)
        # the trace store is EMPTY in both arms: the abstention trace is
        # the engine's composition-path record and lives in the
        # treatment store only, keyed to this probe's task label.
        assert not control_mgr.sqlite.list_traces(
            project=pair.project, task_label=ABSTENTION_TASK_LABEL
        )
    finally:
        control_mgr.close()
        treatment_mgr.close()


# ── the probe policy (unit pins on the registered instrument) ─────────────────


def _view_fixture(goal: str, actions: list[tuple[str, str]]) -> tuple[list[dict], list[dict]]:
    rows = [
        {
            "row_id": "r1",
            "kind": "knowledge",
            "title": "Decision: the payments module is under a parallel refactor",
            "content": (
                "Decision: changes to services/payments/api/refund.py and "
                "services/payments/models/charge.py route through the channel."
            ),
            "agent": "a-peer",
            "age_sec": 100,
        }
    ]
    menu = [{"action_id": aid, "description": desc} for aid, desc in actions]
    return rows, menu


def test_probe_policy_file_row_hazard_defers_only_zone_writes() -> None:
    rows, menu = _view_fixture(
        "ship the retry budget fix in the payments module",
        [
            ("a1", "edit services/payments/api/refund.py to ship the retry budget fix"),
            ("a2", "park the payments module change and pick up services/ratelimits/api/quota.py"),
        ],
    )
    decision = runner.probe_decide(
        goal="ship the retry budget fix in the payments module",
        view_rows=rows,
        actions=menu,
        awareness_text="",
    )
    assert decision.chosen == ("a2",)  # the zone WRITE is deferred, the park action taken
    assert decision.abstained == ("a1",)
    assert decision.attribution[0][1][0].kind == "file_row"


def test_probe_policy_hints_are_the_only_awareness_hazard() -> None:
    rows, menu = _view_fixture(
        "ship the retry budget fix in the payments module",
        [
            ("a1", "edit services/payments/api/refund.py to ship the retry budget fix"),
            ("a2", "review services/ratelimits/models/bucket.py for the adjacent fix"),
        ],
    )
    goal = "ship the retry budget fix in the payments module"
    # arm-blindness: same menu; the inputs differ one at a time
    unverified_only = (
        "## Peer awareness — delta since x (project p)\n"
        "presence claims are self-reported by peers and unverified\n\n"
        "### observed — server-recorded write events\n"
        "- a-peer: 1 entries, last x\n\n"
        "### self-reported — unverified peer claims\n"
        "- a-peer: [unverified] goal hold the payments module refactor\n"
    )
    no_hint = runner.probe_decide(
        goal=goal, view_rows=[], actions=menu, awareness_text=unverified_only
    )
    assert no_hint.chosen == ("a1",)  # [unverified] alone NEVER defers (E0 §5.4 frame)
    control = runner.probe_decide(goal=goal, view_rows=rows, actions=menu, awareness_text="")
    assert control.chosen == ("a2",)  # the file-visible row defers in the control arm too
    treatment_text = (
        unverified_only + "\n### conflict-hints — lexical overlap with my current goal\n"
        "- a-peer: [unverified] shared ['payments', 'module']\n"
    )
    treatment = runner.probe_decide(
        goal=goal, view_rows=[], actions=menu, awareness_text=treatment_text
    )
    assert treatment.chosen == ("a2",)  # the ENGINE hint (goal-anchored) defers the write
    assert treatment.abstained == ("a1",)
    assert treatment.attribution[0][1][0].kind == "conflict_hint"
    assert treatment.attribution[0][1][0].tokens == frozenset({"payments", "module"})


# ── the oracle tally layer on SYNTHETIC probe outcomes ────────────────────────


def test_build_outcomes_tallies_synthetic_probe_outcomes() -> None:
    """The tally layer is validated independently of the real probe:
    synthetic per-arm binaries must land in exactly the computed rates
    and discordance counters (treatment-vs-control only)."""
    synthetic: dict[str, list[dict[str, Any]]] = {"control": [], "treatment": []}
    # per stratum: (control_intruded, treatment_intruded, control_deferred, treatment_deferred)
    pattern = {
        "type2": (True, False, False, False),  # control intrudes, treatment does not
        "type1": (False, False, False, False),
        "stale": (False, False, False, True),  # treatment over-defers here (synthetic)
    }
    for case in runner.SCENARIO_CASES:
        p = pattern.get(case.stratum, (False, False, False, False))
        for leg in runner.LEGS:
            first = leg.leg == "control"
            synthetic[leg.leg].append(
                {
                    "intruded": p[0] if first else p[1],
                    "deferred": p[2] if first else p[3],
                    "chosen": ["a1"],
                    "abstained": [],
                    "hints": 0,
                    "abstention_attribution": [],
                    "abstention_chain": None,
                }
            )
    executed = {"arms": synthetic, "replay_peer_top_slot": False}
    outcomes = runner.build_outcomes(executed)
    runner.verify_outcomes(outcomes)
    assert outcomes["arm_rates"]["control"]["type2_intruded"] == 80
    assert outcomes["arm_rates"]["treatment"]["type2_intruded"] == 0
    assert outcomes["arm_rates"]["treatment"]["stale_over_deferred"] == 40
    assert outcomes["discordance"]["type2_intrusion"] == {
        "treatment_only": 0,
        "control_only": 80,
    }
    assert outcomes["discordance"]["stale_over_deferral"] == {
        "treatment_only": 40,
        "control_only": 0,
    }
    assert outcomes["diagnostics"]["replay224"]["peer_top_slot"] is False


# ── the statistics ban is SCHEMA, not convention (review P2) ──────────────────


def test_verify_outcomes_refuses_smuggled_stat_keys(
    collected: tuple[dict, dict],
) -> None:
    _, outcomes = collected
    runner.verify_outcomes(outcomes)  # sanity: the honest artifact passes

    top_level = {**outcomes, "p_value": 0.03}
    with pytest.raises(AssertionError, match="unexpected=\\['p_value'\\]"):
        runner.verify_outcomes(top_level)

    deep = {
        **outcomes,
        "scenarios": [
            {**outcomes["scenarios"][0], "ci95": 0.02},
            *outcomes["scenarios"][1:],
        ],
    }
    with pytest.raises(AssertionError, match=r"scenarios\[0\]\.ci95"):
        runner.verify_outcomes(deep)

    nested = {
        **outcomes,
        "arm_rates": {
            leg: {**rates, "significance": "high"} for leg, rates in outcomes["arm_rates"].items()
        },
    }
    with pytest.raises(AssertionError, match=r"arm_rates\.control\.significance"):
        runner.verify_outcomes(nested)

    arm_drift = {
        **outcomes,
        "scenarios": [
            {
                **outcomes["scenarios"][0],
                "control": {**outcomes["scenarios"][0]["control"], "verdict": "PASS"},
            },
            *outcomes["scenarios"][1:],
        ],
    }
    with pytest.raises(AssertionError, match=r"scenarios\[0\].*verdict"):
        runner.verify_outcomes(arm_drift)


def test_verify_outcomes_pins_exact_key_schemas(collected: tuple[dict, dict]) -> None:
    _, outcomes = collected
    missing = {k: v for k, v in outcomes.items() if k != "discordance"}
    with pytest.raises(AssertionError, match="missing=\\['discordance'\\]"):
        runner.verify_outcomes(missing)  # type: ignore[arg-type]
    wrong_denominators = {**outcomes, "denominators": {**outcomes["denominators"], "type2": 40}}
    with pytest.raises(AssertionError, match="denominators"):
        runner.verify_outcomes(wrong_denominators)


def test_record_refuses_smuggled_stat_artifact(
    tmp_path: Path, collected: tuple[dict, dict]
) -> None:
    manifest, outcomes = collected
    smuggled = {**outcomes, "p_value": 0.03}
    with pytest.raises(AssertionError, match="unexpected=\\['p_value'\\]"):
        runner.record_run(manifest, smuggled, tmp_path)  # type: ignore[arg-type]
    assert not list(tmp_path.iterdir())


# ── refusal and write-once recording ──────────────────────────────────────────


def test_default_invocation_refuses_to_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = runner.main(["--runs-dir", str(tmp_path), "--quiet"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "REFUSING to record" in err
    assert not list(tmp_path.iterdir()), "collect-only must not persist anything"


def test_record_writes_write_once_artifacts(tmp_path: Path, collected: tuple[dict, dict]) -> None:
    manifest, outcomes = collected
    run_dir = runner.record_run(manifest, outcomes, tmp_path)
    assert run_dir == tmp_path / manifest["run_id"]
    on_disk = json.loads((run_dir / "manifest.json").read_text())
    runner.verify_manifest(on_disk)
    assert on_disk["manifest_sha256"] == manifest["manifest_sha256"]
    outcomes_disk = json.loads((run_dir / "outcomes.json").read_text())
    assert outcomes_disk["run_id"] == manifest["run_id"]
    assert outcomes_disk["scenarios"] == outcomes["scenarios"]
    assert not list(tmp_path.glob(".tmp-*"))  # no staging dir lingers
    with pytest.raises(FileExistsError, match="write-once"):
        runner.record_run(manifest, outcomes, tmp_path)
    rc = runner.main(["--record", "--runs-dir", str(tmp_path), "--quiet"])
    assert rc == 1  # the CLI surfaces the refusal as a clean non-zero exit


def test_record_crash_mid_write_leaves_no_partial_run(
    tmp_path: Path,
    collected: tuple[dict, dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, outcomes = collected
    original = Path.write_text

    def crash_on_outcomes(self: Path, data: str, **kwargs: object) -> int:
        if self.name == "outcomes.json":
            raise OSError("simulated crash between staged writes")
        return original(self, data, **kwargs)  # type: ignore[arg-type, call-arg]

    monkeypatch.setattr(Path, "write_text", crash_on_outcomes)
    with pytest.raises(OSError, match="simulated crash"):
        runner.record_run(manifest, outcomes, tmp_path)
    monkeypatch.undo()
    assert not (tmp_path / manifest["run_id"]).exists(), "partial run dir leaked"
    assert not list(tmp_path.glob(".tmp-*")), "staging dir leaked"
    run_dir = runner.record_run(manifest, outcomes, tmp_path)  # nothing wedged
    assert (run_dir / "outcomes.json").exists()


def test_record_refuses_mismatched_artifacts(tmp_path: Path) -> None:
    manifest = runner.finalize_manifest(runner.build_manifest())
    tampered = {**manifest, "probe_policy": {**manifest["probe_policy"], "name": "rogue-v9"}}
    with pytest.raises(AssertionError):
        runner.record_run(tampered, {"scenarios": []}, tmp_path)  # type: ignore[arg-type]
    assert not list(tmp_path.iterdir())


def test_record_refuses_foreign_run_id_before_schema_gate(
    tmp_path: Path, collected: tuple[dict, dict]
) -> None:
    """Linkage BEFORE the schema gate (review #294/#295): an outcomes
    dict carrying a FOREIGN top-level run_id is refused by name at the
    linkage check — reachable now that it runs before verify_outcomes
    (it was unreachable dead code at the tail of record_run)."""
    manifest, outcomes = collected
    foreign = {**outcomes, "run_id": "e3-d-foreign-id"}
    with pytest.raises(AssertionError, match="run id mismatch"):
        runner.record_run(manifest, foreign, tmp_path)  # type: ignore[arg-type]
    assert not list(tmp_path.iterdir())


# ── archived artifacts stay machine-checkable (review #294/#295) ──────────────


def test_verify_recorded_roundtrip_on_archived_run(
    tmp_path: Path, collected: tuple[dict, dict]
) -> None:
    """verify_recorded re-verifies an archived run directory: the
    recorded outcomes carry the stamped ``run_id`` linkage key the bare
    verify_outcomes rejects, so the helper strips it and re-runs both
    gates — the archived artifact is machine-checkable, not frozen."""
    manifest, outcomes = collected
    run_dir = runner.record_run(manifest, outcomes, tmp_path)
    on_disk_manifest, on_disk_outcomes = runner.verify_recorded(run_dir)
    assert on_disk_manifest["manifest_sha256"] == manifest["manifest_sha256"]
    assert on_disk_outcomes["scenarios"] == outcomes["scenarios"]
    assert "run_id" not in on_disk_outcomes  # stripped linkage key

    # a tampered archived artifact fails LOUD: flip an outcome row and
    # the structural gate refuses the reload.
    on_disk = json.loads((run_dir / "outcomes.json").read_text())
    on_disk["scenarios"][0]["control"]["intruded"] = not on_disk["scenarios"][0]["control"][
        "intruded"
    ]
    (run_dir / "outcomes.json").write_text(json.dumps(on_disk, indent=2) + "\n")
    with pytest.raises(AssertionError):
        runner.verify_recorded(run_dir)


def test_verify_recorded_refuses_linkage_drift(
    tmp_path: Path, collected: tuple[dict, dict]
) -> None:
    manifest, outcomes = collected
    run_dir = runner.record_run(manifest, outcomes, tmp_path)
    recorded = json.loads((run_dir / "outcomes.json").read_text())
    recorded["run_id"] = "e3-d-other-run"  # tamper with the linkage stamp
    (run_dir / "outcomes.json").write_text(json.dumps(recorded, indent=2) + "\n")
    with pytest.raises(AssertionError, match="archived outcomes run_id"):
        runner.verify_recorded(run_dir)


# ── the statistics ban ignores case and spelling games (review #294/#295) ─────


def test_stat_key_scan_is_case_and_spelling_tight(collected: tuple[dict, dict]) -> None:
    """The recursive scan refuses the bypasses the case-sensitive
    ``p.?value|ci\\d*`` pattern let through: P_VALUE, CI95, Verdict,
    Pval, conf_int (any case), plus the registered lone-``p`` and
    ``power`` spellings. The honest artifact still passes."""
    _, outcomes = collected
    for key in (
        "P_VALUE",
        "pValue",
        "PValue",
        "CI95",
        "ci_95",
        "Verdict",
        "Pval",
        "CONF_INT",
        "conf_int",
        "Statistical_Power",
        "p",
        "pval",
    ):
        smuggled = {
            **outcomes,
            "scenarios": [
                {
                    **outcomes["scenarios"][0],
                    "control": {**outcomes["scenarios"][0]["control"], key: 0.03},
                },
                *outcomes["scenarios"][1:],
            ],
        }
        with pytest.raises(AssertionError, match="must not carry statistical keys"):
            runner.verify_outcomes(smuggled)


def test_verify_manifest_gates_retrieval_pin_from_runner_2(
    collected: tuple[dict, dict],
) -> None:
    """Regression (PR #411 review P1): the retrieval-pin gate is real —
    a runner-2+ manifest WITHOUT the retrieval key is refused, while a
    runner-1-stamped manifest without the key passes as history (the
    gate fires only on runner_version == PINNED_RETRIEVAL_FROM)."""
    manifest, _ = collected
    stripped = {k: v for k, v in manifest.items() if k != "retrieval"}
    # the stripped manifest must be internally consistent for the gate to
    # fire on the PIN, not on an integrity hash mismatch: re-derive the
    # core hash and the run_id from the stripped core.
    core = {
        k: v
        for k, v in stripped.items()
        if k not in ("created", "run_id", "manifest_sha256", "outcomes_sha256")
    }
    core_hash = runner._sha256(runner._canonical_json(core))
    stripped["run_id"] = f"e3-d-{core_hash[:12]}"
    body = {k: v for k, v in stripped.items() if k != "manifest_sha256"}
    stripped["manifest_sha256"] = runner._sha256(runner._canonical_json(body))
    with pytest.raises(AssertionError, match="must pin retrieval"):
        runner.verify_manifest(stripped)

    # runner-1-shaped history (runner_version != PINNED_RETRIEVAL_FROM)
    # with the retrieval key absent still passes — no retroactive rule
    # (re-derive the core hash: runner_version is part of the core).
    history = {**stripped, "runner_version": "e3-d-runner-1"}
    history_core = {
        k: v
        for k, v in history.items()
        if k not in ("created", "run_id", "manifest_sha256", "outcomes_sha256")
    }
    history["run_id"] = f"e3-d-{runner._sha256(runner._canonical_json(history_core))[:12]}"
    body1 = {k: v for k, v in history.items() if k != "manifest_sha256"}
    history["manifest_sha256"] = runner._sha256(runner._canonical_json(body1))
    runner.verify_manifest(history)  # does not raise
