"""F1 task-scope runner — machinery gates over the REAL corpus (epic #308).

RUNNER-MACHINERY gates, not an experiment run: collect-only executes
every arm, validates the artifacts and persists NOTHING. The first
recorded F1 run stays an owner-gated deliberate decision after this
infrastructure merges (F1 §4.3; the hard constraint: NEVER --record in
this wave — the record-path tests below exercise ``record_run`` and the
CLI on THROWAWAY directories with a PATCHED collect, never the real
runs dir, never the real E-file).

Pins (F1 §4.3 / §4.5 / §2.8):

* dry run over the real corpus produces a WELL-FORMED manifest +
  per-query outcome tuples without persisting a run;
* the default invocation REFUSES to record (message + zero writes +
  the E-file untouched); ``--record`` writes write-once artifacts and
  appends the §9 run-ledger entry; a re-record fails loud;
* BLAKE2b fingerprint determinism (same seed → same fingerprint; any
  mutation → a different fingerprint);
* equal-budget enforcement (every assembly at 2048 — breach fails loud);
* V1-V6 executed and logged in the manifest; any breach = void run;
* determinism of a collect-only dry run: two collects over fresh stores
  → byte-identical outcome tuples and the identical content-addressed
  run id;
* the §6.5 anchor quartet lives in ``tests/test_f1_power.py``.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from benchmarks.experiments.f1_task_scope import corpus as f1_corpus
from benchmarks.experiments.f1_task_scope import ledger as f1_ledger
from benchmarks.experiments.f1_task_scope import runner

_QID_RE = {
    "t_gold": "ft-\\d{3}-(ph|pr)",
    "x_gold": "fx-\\d{3}-(ph|pr)",
    "l_neg": "fl-\\d{3}-(ph|pr)",
}


@pytest.fixture(scope="module")
def collected() -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """One full collect over the real corpus (all four arms)."""
    manifest, outcomes = runner.collect_run()
    yield manifest, outcomes


# ── corpus: shape, fingerprint, gold coverage (§3) ────────────────────────────


def test_corpus_shape_locked_at_registration() -> None:
    corpus = f1_corpus.build_corpus()
    counts = f1_corpus.segment_counts(corpus)
    assert counts == {
        "task_prose": 144,
        "task_code": 96,
        "task_doc": 120,
        "shared": 120,
        "checkpoint": 278,
        "misc": 194,
        "canary": 8,
    }
    assert len(corpus.rows) == 960
    task_total = counts["task_prose"] + counts["task_code"] + counts["task_doc"]
    assert task_total == 360  # task mass 37.5% of 960 (§3.4)


def test_corpus_fingerprint_deterministic_and_mutation_sensitive() -> None:
    first = f1_corpus.build_corpus()
    second = f1_corpus.build_corpus()
    assert f1_corpus.corpus_fingerprint(first) == f1_corpus.corpus_fingerprint(second)
    # any content mutation → a different fingerprint (§4.2)
    mutated_row = dataclasses.replace(first.rows[0], content=first.rows[0].content + " drift")
    mutated = dataclasses.replace(first, rows=(mutated_row, *first.rows[1:]))
    assert f1_corpus.corpus_fingerprint(mutated) != f1_corpus.corpus_fingerprint(first)
    # a gold reassignment (query-side mutation) → a different fingerprint
    q0 = first.queries[0]
    mutated_q = dataclasses.replace(
        first, queries=(dataclasses.replace(q0, gold_slug="other"), *first.queries[1:])
    )
    assert f1_corpus.corpus_fingerprint(mutated_q) != f1_corpus.corpus_fingerprint(first)


def test_task_display_committed_and_complete() -> None:
    assert set(f1_corpus.TASK_DISPLAY) == set(f1_corpus.TASKS)
    for slug, display in f1_corpus.TASK_DISPLAY.items():
        assert f1_corpus.TASK_TAG_RE.match(f"task:{slug}")
        assert display and display != slug


def test_analyzed_denominators_and_session_shape() -> None:
    corpus = f1_corpus.build_corpus()
    analyzed = corpus.analyzed_queries()
    per_stratum: dict[str, int] = {}
    for q in analyzed:
        per_stratum[q.stratum] = per_stratum.get(q.stratum, 0) + 1
    assert per_stratum == f1_corpus.ANALYZED_COUNTS  # 192 / 48 / 24
    for task in f1_corpus.TASKS:
        t_q = [q for q in analyzed if q.stratum == "t_gold" and q.current_task == task]
        x_q = [q for q in analyzed if q.stratum == "x_gold" and q.current_task == task]
        assert len(t_q) == 24 and len(x_q) == 6  # the G3b session shape


def test_lens_axis_contract_pinned_at_test_layer() -> None:
    """CURRENT lens behavior, pinned at the TEST layer (§8 entry 16 —
    the right layer): code-axis queries and L-neg MIXED queries activate
    the CODE lens; every other query never does (the trap family stays
    inert). A lens change that flips any activation fails HERE, at test
    time — while a run executed under a broadened lens still PRODUCES
    red corridor data instead of crashing the corpus build."""
    from vesma.lens import Lens, lens_active

    for q in f1_corpus.build_corpus().queries:
        active = lens_active(Lens.CODE, query=q.text)
        expected = q.axis == "code" or (q.stratum == "l_neg" and q.mixed)
        assert active == expected, q.qid


def test_lens_observation_counts_pinned_and_manifest_stamped(
    collected: tuple[dict, dict],
) -> None:
    """§8 entry 16: lens activation is OBSERVED, never build-asserted —
    the counts helper yields the current facts (trap 0 / mixed 8 /
    code-axis 72 over the analyzed set), the manifest stamps them per
    run under corpus.lens_observed, and verify_manifest enforces the
    schema. trap_activations == 0 is the current calibration; > 0 under
    a future lens is DATA (the trap corridor's red path), not a crash."""
    manifest, _ = collected
    observed = f1_corpus.lens_activation_observed()
    assert observed == {
        "trap_activations": 0,
        "mixed_activations": 8,
        "code_axis_activations": 72,
    }
    assert manifest["corpus"]["lens_observed"] == observed
    # negative: a lens_observed block with the wrong shape fails the
    # manifest schema (re-finalized so the sha/run-id guards pass)
    bad_core = {
        **runner._core(manifest),
        "corpus": {**manifest["corpus"], "lens_observed": {"trap_activations": 0}},
    }
    bad_manifest = runner.finalize_manifest(bad_core, manifest["invariants"])
    with pytest.raises(AssertionError, match="lens_observed"):
        runner.verify_manifest(bad_manifest)


def test_l_neg_mixed_stratum_is_falsifiable() -> None:
    """G4b falsifiability (§8 entry 13, the repair-wave core): at least
    one (registered: exactly 8) L-neg analyzed query ACTIVATES the lens
    AND its gold is prose-side — the active lens's code-only narrowing
    can drop it, so the §2.7b corridor measures instead of being
    structurally unfalsifiable (arm A ≡ A0 by the identity projection)."""
    from vesma.filter.pipeline import detect_profile
    from vesma.lens import Lens, lens_active

    corpus = f1_corpus.build_corpus()
    rows = corpus.rows_by_slug()
    l_neg = [q for q in corpus.analyzed_queries() if q.stratum == "l_neg"]
    activating = [q for q in l_neg if lens_active(Lens.CODE, query=q.text)]
    assert len(activating) == 8  # 4 mixed pairs x 2 phrasings
    for q in activating:
        assert q.mixed, f"{q.qid}: activating L-neg query must carry the mixed birth flag"
        gold = rows[q.gold_slug]
        # prose-side, cross-class gold: what the lens can drop
        assert gold.task is None and gold.segment == "shared"
        assert detect_profile(gold.content) != "code"


def test_l_neg_non_activating_majority_holds() -> None:
    """The registered must-not-activate trap family stays the L-neg
    majority: 16 of 24 analyzed queries never activate the lens."""
    from vesma.lens import Lens, lens_active

    corpus = f1_corpus.build_corpus()
    l_neg = [q for q in corpus.analyzed_queries() if q.stratum == "l_neg"]
    inert = [q for q in l_neg if not lens_active(Lens.CODE, query=q.text)]
    assert len(l_neg) == 24 and len(inert) == 16
    assert all(not q.mixed for q in inert)


def test_corpus_is_governance_free() -> None:
    """Repair 2d: no corpus row carries a governance-selecting tag — a
    governance row would be re-ranked by type_boost in _recall_stage
    but not in the raw mgr.search probes, breaking the G4a/V3
    probe-order equivalence."""
    from vesma.lanes import GOVERNANCE_TAGS

    for row in f1_corpus.build_corpus().rows:
        assert not GOVERNANCE_TAGS.intersection(row.tags), row.slug


def test_audit_subsample_frozen_rule() -> None:
    subsample = f1_corpus.audit_subsample()
    analyzed_total = sum(f1_corpus.ANALYZED_COUNTS.values())
    assert len(subsample) == 53  # ceil(20% of 264)
    assert len(subsample) == -(-analyzed_total * 20 // 100)
    again = f1_corpus.audit_subsample()
    assert [p["pair_id"] for p in subsample] == [p["pair_id"] for p in again]
    for pair in subsample:
        # arm-stripped by construction (§4.4): no arm ids, no provenance
        assert set(pair) == {
            "pair_id",
            "qid",
            "query_text",
            "gold_slug",
            "gold_title",
            "gold_content",
        }


# ── ledger: bootstrap state, replacements, hash coupling ──────────────────────


def test_committed_ledger_is_the_bootstrap_state() -> None:
    committed = f1_ledger.load_ledger()
    assert committed == f1_ledger.initial_ledger()
    assert committed["rejects"] == [] and committed["replacements"] == []
    active = f1_ledger.active_analyzed_queries(committed)
    assert len(active) == 264


def test_ledger_rejection_swaps_from_surplus_pool() -> None:
    corpus = f1_corpus.build_corpus()
    ledger = f1_ledger.initial_ledger()
    original_hash = f1_ledger.ledger_state_hash(ledger)
    t_analyzed = next(q for q in corpus.analyzed_queries() if q.stratum == "t_gold")
    t_surplus = next(q for q in corpus.queries if q.surplus and q.stratum == "t_gold")
    x_surplus = next(q for q in corpus.queries if q.surplus and q.stratum == "x_gold")
    # cross-stratum replacement is refused
    with pytest.raises(ValueError):
        f1_ledger.record_rejection(ledger, t_analyzed.qid, x_surplus.qid)
    updated = f1_ledger.record_rejection(ledger, t_analyzed.qid, t_surplus.qid)
    assert f1_ledger.ledger_state_hash(updated) != original_hash
    assert t_analyzed.qid not in {q.qid for q in f1_ledger.active_analyzed_queries(updated)}
    assert t_surplus.qid in {q.qid for q in f1_ledger.active_analyzed_queries(updated)}
    # the denominators restore exactly (192/48/24)
    per_stratum: dict[str, int] = {}
    for q in f1_ledger.active_analyzed_queries(updated):
        per_stratum[q.stratum] = per_stratum.get(q.stratum, 0) + 1
    assert per_stratum == f1_corpus.ANALYZED_COUNTS


# ── dry run over the real corpus ──────────────────────────────────────────────


def test_dry_run_manifest_well_formed(collected: tuple[dict, dict]) -> None:
    manifest, _ = collected
    runner.verify_manifest(manifest)  # raises on any drift
    assert manifest["experiment"] == "f1-task-scope"
    assert manifest["arm_order"] == ["A0", "C", "B", "A"]
    assert set(manifest["arms"]) == {"A0", "C", "B", "A"}
    assert manifest["equal_budget"] == 2048 and manifest["top_k"] == 5
    block = manifest["common_block"]
    assert block["lanes_enabled"] is False  # V2: the E3 verdict stands
    assert block["type_boost"] is True  # §1.3 common block
    assert block["budget"] == 2048 and block["scanner"] is False
    assert manifest["clock"]["run_now"] == runner.RUN_NOW.isoformat()
    assert manifest["corpus"]["seed"] == f1_corpus.SEED
    assert manifest["corpus"]["fingerprint_blake2b"] == f1_corpus.corpus_fingerprint()
    assert manifest["ledger"]["analyzed"] == f1_corpus.ANALYZED_COUNTS
    code = manifest["code"]
    assert code["mnemos_version"] and code["git_commit"] not in ("", None)


def test_dry_run_outcomes_tuples_complete(collected: tuple[dict, dict]) -> None:
    _, outcomes = collected
    runner.verify_outcomes(outcomes)  # schema + statistics ban
    import re as re_mod

    queries = outcomes["queries"]
    assert len(queries) == 264
    qids = [row["qid"] for row in queries]
    assert len(set(qids)) == 264
    for row in queries:
        assert re_mod.match(_QID_RE[row["stratum"]], row["qid"])
        assert set(row["arms"]) == {"A0", "C", "B", "A"}
        for arm in ("A0", "C", "B", "A"):
            arm_tuple = row["arms"][arm]
            assert isinstance(arm_tuple["hit"], bool)
            assert arm_tuple["tokens"] >= 0
            assert len(arm_tuple["block_slugs"]) <= 5
    # G4a probes recorded on arm A task-class tuples only
    for row in queries:
        a_tuple = row["arms"]["A"]
        for arm in ("A0", "C", "B"):
            assert row["arms"][arm]["lens_active"] is None
            assert row["arms"][arm]["pre_lens_gold_top5"] is None
        if row["query_class"] == "task":
            assert a_tuple["pre_lens_gold_top5"] is not None
            assert a_tuple["post_lens_gold_top5"] is not None
        else:
            assert a_tuple["lens_active"] is not None


def test_discordance_tallies_consistent_with_tuples(collected: tuple[dict, dict]) -> None:
    """The persisted McNemar inputs are counts over the raw tuples."""
    _, outcomes = collected
    by_qid = {row["qid"]: row for row in outcomes["queries"]}
    for stratum, comparisons in outcomes["discordance"].items():
        for name, tally in comparisons.items():
            first, second = name.split("_vs_")
            first_only = sum(
                1
                for row in outcomes["queries"]
                if row["stratum"] == stratum
                and row["arms"][first]["hit"]
                and not row["arms"][second]["hit"]
            )
            second_only = sum(
                1
                for row in outcomes["queries"]
                if row["stratum"] == stratum
                and row["arms"][second]["hit"]
                and not row["arms"][first]["hit"]
            )
            assert tally == {f"{first}_only": first_only, f"{second}_only": second_only}
            assert by_qid is not None  # pairing keys unique (checked above)


def test_invariants_v1_to_v6_executed_and_logged(collected: tuple[dict, dict]) -> None:
    manifest, _ = collected
    inv = manifest["invariants"]
    # V1: the fixed probe set — every lens-inactive cross query byte-identical
    assert inv["V1"]["probes"] == 40  # 24 shared/foreign prose + 16 L-neg traps
    assert inv["V1"]["identical"] == inv["V1"]["probes"]
    assert inv["V1"]["failures"] == []
    # V3 shadow: every lens-active cross query narrows order-preservingly
    assert inv["V3_shadow"]["probes"] == 32  # 24 X-gold code axis + 8 L-neg mixed
    assert inv["V3_shadow"]["subset_ok"] == 32
    assert inv["V3_shadow"]["code_only_ok"] == 32
    assert inv["V3_shadow"]["failures"] == []
    # V2/V4/V5/V6
    assert inv["V2"] == {"lanes_enabled": False, "arms": ["A0", "C", "B", "A"]}
    assert inv["V4"]["violations"] == 0 and inv["V4"]["canaries_inert"] == 8
    assert inv["V5"]["violations"] == 0 and inv["V5"]["rows_verified"] == 960
    assert inv["V6"]["manifest_verified"] is True
    assert inv["store_copy"]["digests_equal_across_arms"] is True
    assert len(inv["store_copy"]["content_digest_sha256"]) == 64


def test_g4b_trap_corridor_and_mixed_measure_split(collected: tuple[dict, dict]) -> None:
    """§8 entry 15 (TL decision, review round 2): the G4b corridor's
    scope and the descriptive activation-cost measure ride IN the
    artifact — every l_neg row carries substratum trap|mixed — and both
    are falsifiable in their own direction:

    * TRAP subset (16, the corridor): lens-inactive under current
      regexes ⇒ arm A runs the identity projection ⇒ hit(A) == hit(A0);
      if the regexes ever broaden onto a trap phrasing, A loses the
      trap gold and the corridor FAILS (the round-1 falsifiability).
    * MIXED subset (8, the descriptive measure): activating prose-gold
      queries — a behavior pin under CURRENT regexes, not a threshold:
      arm A drops exactly these 8 (the lens hard-excludes prose), while
      A0 reaches them (the measure is non-vacuous). Deterministic-red
      here is a REPORTED result, never a corridor verdict.
    """
    _, outcomes = collected
    l_neg = [row for row in outcomes["queries"] if row["stratum"] == "l_neg"]
    mixed = [row for row in l_neg if row["substratum"] == "mixed"]
    traps = [row for row in l_neg if row["substratum"] == "trap"]
    assert len(mixed) == 8 and len(traps) == 16
    # the committed flag agrees with the measured lens activation
    assert all(row["arms"]["A"]["lens_active"] for row in mixed)
    assert all(not row["arms"]["A"]["lens_active"] for row in traps)
    # behavior pin (current regexes): A drops exactly the 8 mixed…
    assert all(not row["arms"]["A"]["hit"] for row in mixed)
    assert any(row["arms"]["A0"]["hit"] for row in mixed)  # …and A0 reaches them
    # …while the trap corridor is A0-mirrored under current calibration
    assert all(row["arms"]["A"]["hit"] == row["arms"]["A0"]["hit"] for row in traps)


def test_substratum_flag_schema_enforced(collected: tuple[dict, dict]) -> None:
    """The substratum flag is schema, not convention: present on EVERY
    l_neg row (exact row-key set), absent on every other stratum's
    rows; the manifest pins the run's own composition (bootstrap:
    16 trap + 8 mixed); drift fails verify_* loud."""
    manifest, outcomes = collected
    assert manifest["corpus"]["l_neg_composition"] == {"trap": 16, "mixed": 8}
    for row in outcomes["queries"]:
        if row["stratum"] == "l_neg":
            assert row["substratum"] in ("trap", "mixed")
        else:
            assert "substratum" not in row
    # negative: dropping the flag from an l_neg row breaks the schema
    stripped = {
        **outcomes,
        "queries": [
            {k: v for k, v in row.items() if k != "substratum"}
            if row["stratum"] == "l_neg"
            else row
            for row in outcomes["queries"]
        ],
    }
    with pytest.raises(AssertionError, match="row keys"):
        runner.verify_outcomes(stripped)
    # negative: a bogus substratum value breaks the schema
    bogus = {
        **outcomes,
        "queries": [
            {**row, "substratum": "bogus"} if row["stratum"] == "l_neg" else row
            for row in outcomes["queries"]
        ],
    }
    with pytest.raises(AssertionError, match="substratum must be one of"):
        runner.verify_outcomes(bogus)
    # negative: substratum on a non-l_neg row breaks the schema
    foreign = {
        **outcomes,
        "queries": [
            {**row, "substratum": "trap"} if row["stratum"] == "t_gold" else row
            for row in outcomes["queries"]
        ],
    }
    with pytest.raises(AssertionError, match="row keys"):
        runner.verify_outcomes(foreign)
    # negative: a manifest composition that stops covering the stratum
    # (re-finalized so the sha/run-id guards pass and the composition
    # check itself is what fires)
    bad_core = {
        **runner._core(manifest),
        "corpus": {**manifest["corpus"], "l_neg_composition": {"trap": 16, "mixed": 7}},
    }
    bad_manifest = runner.finalize_manifest(bad_core, manifest["invariants"])
    with pytest.raises(AssertionError, match="l_neg_composition"):
        runner.verify_manifest(bad_manifest)


def test_arm_c_dual_token_basis(collected: tuple[dict, dict]) -> None:
    """Repair 2a: every arm tuple carries BOTH token bases. Arm C task
    tuples record the full formatted assembly (all budget-included
    blocks) alongside the registered top-5 basis — the basis comparable
    with the assembled arms' full estimates; on A0/B/A the assembled
    estimate already is the full basis, so the two fields are equal."""
    _, outcomes = collected
    for row in outcomes["queries"]:
        for arm in ("A0", "C", "B", "A"):
            assert row["arms"][arm]["tokens_full"] >= row["arms"][arm]["tokens"]
    c_task = [row for row in outcomes["queries"] if row["query_class"] == "task"]
    assert any(row["arms"]["C"]["tokens_full"] > row["arms"]["C"]["tokens"] for row in c_task)
    for row in outcomes["queries"]:
        for arm in ("A0", "B", "A"):
            assert row["arms"][arm]["tokens_full"] == row["arms"][arm]["tokens"]


def test_collect_is_deterministic_run_id_and_outcomes() -> None:
    """Two collects over fresh stores → identical run id + byte-identical
    outcome tuples (§4.2 determinism: seeded ids, frozen clock, clone
    discipline)."""
    first_manifest, first_outcomes = runner.collect_run()
    second_manifest, second_outcomes = runner.collect_run()
    assert first_manifest["run_id"] == second_manifest["run_id"]
    assert json.dumps(first_outcomes, sort_keys=True) == json.dumps(second_outcomes, sort_keys=True)
    # and the outcomes carry no wall-clock anywhere (tuples only)
    assert set(first_outcomes) <= {
        "spec",
        "pairing",
        "strata",
        "arm_order",
        "budget",
        "top_k",
        "queries",
        "discordance",
    }


# ── equal-budget enforcement (§4.1 — breach fails loud) ───────────────────────


def test_equal_budget_breach_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    corpus = f1_corpus.build_corpus()
    query = next(q for q in corpus.analyzed_queries() if q.stratum == "t_gold")
    slug_to_id = {row.slug: f"id-{i:04d}" for i, row in enumerate(corpus.rows)}

    class _StubManager:
        def close(self) -> None:
            return None

    monkeypatch.setattr(runner, "_open_arm_manager", lambda settings: _StubManager())
    monkeypatch.setattr(
        runner,
        "assemble_context",
        lambda *args, **kwargs: {"tokens": {"budget": 4096, "estimated": 1}},
    )
    with pytest.raises(AssertionError, match="equal-budget breach"):
        runner.execute_arm(
            "A0", corpus, (query,), slug_to_id, runner._store_settings(Path("/tmp/f1-unused"))
        )


# ── refuse-to-record default / --record write-once / §9 ledger ────────────────


def test_default_invocation_refuses_to_record(
    collected: tuple[dict, dict],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest, outcomes = collected
    doc_copy = tmp_path / "f1-task-scope.md"
    doc_copy.write_text(runner.DOC_PATH.read_text())
    monkeypatch.setattr(runner, "collect_run", lambda: (manifest, outcomes))
    rc = runner.main(["--runs-dir", str(tmp_path / "runs"), "--doc-path", str(doc_copy)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "REFUSING to record" in err
    assert not (tmp_path / "runs").exists()  # zero writes
    assert doc_copy.read_text() == runner.DOC_PATH.read_text()  # E-file untouched


def test_record_write_once_and_ledger_append(
    collected: tuple[dict, dict],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, outcomes = collected
    doc_copy = tmp_path / "f1-task-scope.md"
    doc_copy.write_text(runner.DOC_PATH.read_text())
    runs = tmp_path / "runs"
    monkeypatch.setattr(runner, "collect_run", lambda: (manifest, outcomes))

    # Post-record era (PR #412): the committed E-file already carries ONE
    # recorded run; this tmp-copy --record adds the NEXT entry. The pin
    # counts entries relative to the copy's baseline, not against zero.
    baseline_entries = doc_copy.read_text().count("— RUN — arms A0/C/B/A")
    rc = runner.main(["--record", "--runs-dir", str(runs), "--doc-path", str(doc_copy)])
    assert rc == 0
    run_dir = runs / str(manifest["run_id"])
    assert (run_dir / "manifest.json").exists()
    assert (run_dir / "outcomes.json").exists()
    doc_text = doc_copy.read_text()
    assert manifest["run_id"] in doc_text  # §9 entry appended
    # exactly ONE new ledger entry (the id appears twice inside it: the id
    # itself + the artifacts path — the E0 §9 entry shape)
    assert doc_text.count("— RUN — arms A0/C/B/A") == baseline_entries + 1

    # write-once: a second --record of the SAME id fails loud and does
    # not double-append the ledger
    rc = runner.main(["--record", "--runs-dir", str(runs), "--doc-path", str(doc_copy)])
    assert rc == 1
    assert doc_copy.read_text() == doc_text


def test_record_run_refuses_existing_directory(
    collected: tuple[dict, dict], tmp_path: Path
) -> None:
    manifest, outcomes = collected
    run_dir = runner.record_run(manifest, outcomes, tmp_path)
    assert run_dir.exists()
    with pytest.raises(FileExistsError, match="write-once"):
        runner.record_run(manifest, outcomes, tmp_path)


def test_run_ledger_entry_shape(collected: tuple[dict, dict], tmp_path: Path) -> None:
    manifest, _ = collected
    entry = runner.run_ledger_entry(manifest)
    split = manifest["corpus"]["l_neg_composition"]
    for expected in (
        manifest["run_id"],
        manifest["corpus"]["fingerprint_blake2b"],
        "budget 2048",
        "lanes off",
        f"L-neg split {split['trap']} trap + {split['mixed']} mixed",  # §8/15 in §9
    ):
        assert expected in entry
    doc_copy = tmp_path / "doc.md"
    doc_copy.write_text(runner.DOC_PATH.read_text())
    runner.append_run_ledger(manifest, doc_copy)
    appended = doc_copy.read_text()
    assert manifest["run_id"] in appended
    with pytest.raises(FileExistsError, match="already present"):
        runner.append_run_ledger(manifest, doc_copy)


# ── repair 2b: --record pre-validation + quarantine (no half-recorded state) ──


def test_record_prevalidates_ledger_and_writes_nothing(
    collected: tuple[dict, dict],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A non-appendable §9 (last section is not §9) fails BEFORE any
    artifact is written: rc 1, no runs dir, doc untouched."""
    manifest, outcomes = collected
    doc_copy = tmp_path / "f1-task-scope.md"
    doc_copy.write_text(runner.DOC_PATH.read_text() + "\n\n## 10. Later section\n")
    monkeypatch.setattr(runner, "collect_run", lambda: (manifest, outcomes))
    rc = runner.main(
        ["--record", "--runs-dir", str(tmp_path / "runs"), "--doc-path", str(doc_copy)]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "not appendable, nothing recorded" in err
    assert not (tmp_path / "runs").exists()  # zero writes
    assert doc_copy.read_text().endswith("## 10. Later section\n")  # untouched


def test_record_quarantines_run_dir_when_append_fails_after_artifacts(
    collected: tuple[dict, dict],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If the §9 append fails AFTER the artifacts exist (here: forced),
    the run dir is renamed <run_id>.UNLEDGERED and the run exits loud —
    never a silently half-recorded state."""
    manifest, outcomes = collected
    doc_copy = tmp_path / "f1-task-scope.md"
    doc_copy.write_text(runner.DOC_PATH.read_text())
    runs = tmp_path / "runs"
    monkeypatch.setattr(runner, "collect_run", lambda: (manifest, outcomes))

    def _boom(manifest: dict, doc_path: Path) -> Path:
        raise AssertionError("forced post-artifact append failure")

    monkeypatch.setattr(runner, "append_run_ledger", _boom)
    rc = runner.main(["--record", "--runs-dir", str(runs), "--doc-path", str(doc_copy)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "quarantined" in err
    run_id = str(manifest["run_id"])
    assert not (runs / run_id).exists()  # the plain run dir is GONE
    quarantine = runs / f"{run_id}.UNLEDGERED"
    assert (quarantine / "manifest.json").exists()  # …renamed aside, intact
    assert (quarantine / "outcomes.json").exists()
    assert doc_copy.read_text() == runner.DOC_PATH.read_text()  # no ledger write


# ── repair 2c: manifest schema exactness + the extended stat-key ban ──────────


def test_verify_manifest_rejects_extra_and_missing_keys(
    collected: tuple[dict, dict],
) -> None:
    """Exact key set: an unexpected top-level manifest key fails as
    loudly as a missing one."""
    manifest, _ = collected
    with pytest.raises(AssertionError, match="unexpected"):
        runner.verify_manifest({**manifest, "bogus_extra": 1})
    stripped = {k: v for k, v in manifest.items() if k != "clock"}
    with pytest.raises(AssertionError, match="missing"):
        runner.verify_manifest(stripped)


def test_verify_outcomes_accepts_recorded_artifact_and_rejects_drift(
    collected: tuple[dict, dict], tmp_path: Path
) -> None:
    """#382 round-trip: the WRITTEN artifact satisfies the runner's own
    exact-key contract — record_run on a throwaway dir → read
    outcomes.json back → verify_outcomes PASSES (the stamped linkage
    key ``run_id`` is schema-legal exactly once, at the top level only).
    Negatives: a hand-tampered extra top-level key, a duplicated
    linkage key deeper in the artifact, and a MISMATCHED run_id each
    fail loud."""
    manifest, outcomes = collected
    run_dir = runner.record_run(manifest, outcomes, tmp_path)
    on_disk = json.loads((run_dir / "outcomes.json").read_text())
    assert on_disk["run_id"] == manifest["run_id"]  # stamped exactly once
    runner.verify_outcomes(on_disk)  # the recorded shape re-verifies

    # the in-memory collect shape still verifies untouched
    runner.verify_outcomes(outcomes)

    # negative: a hand-tampered EXTRA top-level key still fails
    with pytest.raises(AssertionError, match="unexpected"):
        runner.verify_outcomes({**on_disk, "bogus_extra": 1})

    # negative: a run_id nested deeper (stat-scan layer) is NOT linkage —
    # it is an unexpected key under the exact tuple-key check
    nested = {
        **on_disk,
        "queries": [
            {**row, "arms": {**row["arms"], "A": {**row["arms"]["A"], "run_id": "x"}}}
            if row["stratum"] == "t_gold"
            else row
            for row in on_disk["queries"]
        ],
    }
    with pytest.raises(AssertionError, match="tuple keys"):
        runner.verify_outcomes(nested)

    # negative: a MISMATCHED top-level run_id breaks the manifest pairing
    # (fresh sub-dir — the first recorded run id owns tmp_path's root)
    with pytest.raises(AssertionError, match="run id mismatch"):
        runner.record_run(
            manifest,
            {**outcomes, "run_id": "f1-task-scope-other"},
            tmp_path / "second",
        )


def test_discordance_tally_keys_exactly_checked(collected: tuple[dict, dict]) -> None:
    """Round-2 tightening (PR #386 review P2-1): the discordance tally is
    the last dict layer with an exact-key check — the two tally keys are
    derived from the SAME registered comparison pair that names the
    comparison itself (_COMPARISONS as the single source of truth), so a
    tamper renaming a tally key (e.g. ``A_only`` → ``run_id``) fails
    loud with the comparison named. Positive control: the intact
    artifact still verifies."""
    _, outcomes = collected
    runner.verify_outcomes(outcomes)  # positive control
    first_stratum, first_comparisons = next(iter(outcomes["discordance"].items()))
    first_comparison = next(iter(first_comparisons))
    left, right = first_comparison.split("_vs_")

    # negative: a renamed tally key fails, naming the comparison
    renamed = {
        **outcomes,
        "discordance": {
            **outcomes["discordance"],
            first_stratum: {
                **first_comparisons,
                first_comparison: {"run_id": 0, f"{right}_only": 0},
            },
        },
    }
    with pytest.raises(
        AssertionError, match=rf"discordance tally keys on {first_stratum}/{first_comparison}"
    ):
        runner.verify_outcomes(renamed)
    # and the renamed keys are checked against the derived pair, not
    # merely count 2: {"run_id", f"{left}_only"} also fails
    renamed_left = {
        **outcomes,
        "discordance": {
            **outcomes["discordance"],
            first_stratum: {
                **first_comparisons,
                first_comparison: {f"{left}_only": 0, "run_id": 0},
            },
        },
    }
    with pytest.raises(AssertionError, match=f"{first_stratum}/{first_comparison}"):
        runner.verify_outcomes(renamed_left)

    # single source of truth: the expected tally pair for every
    # registered comparison is exactly {left}_only / {right}_only of its
    # own name — spot-check the derivation against _COMPARISONS directly
    for stratum, comps in runner._COMPARISONS.items():
        for first, second in comps:
            assert set(outcomes["discordance"][stratum][f"{first}_vs_{second}"]) == {
                f"{first}_only",
                f"{second}_only",
            }


def test_stat_key_ban_catches_bare_spellings(collected: tuple[dict, dict]) -> None:
    """The extended _STAT_KEY_RE fires on the bare statistic spellings
    (p / pval / power / p-value / ci95 / ci_95) inside the manifest
    scan, while sparing the artifact vocabulary (top_k, hybrid_alpha,
    the q3-capacity-audit slug class)."""
    manifest, _ = collected
    for stat_key in ("p", "pval", "power", "p-value", "ci95", "ci_95"):
        # nested (the top-level set is exact-checked first); the recursive
        # stat scan fires before the integrity-hash comparison
        nested = {**manifest, "runtime": {**manifest["runtime"], stat_key: 0.5}}
        with pytest.raises(AssertionError, match="statistical"):
            runner.verify_manifest(nested)

    pattern = runner._STAT_KEY_RE
    for key in (
        "p",
        "pval",
        "pvals",
        "pvalue",
        "p_value",
        "p-value",
        "power",
        "statistical_power",
        "ci95",
        "ci_95",
        "verdict",
        "significance",
    ):
        assert pattern.search(key), key
    for key in (
        "top_k",
        "hybrid_alpha",
        "q3-capacity-audit",
        "capacity",
        "recall_depth",
        "tokens_full",
        "lens_active",
        "steps",
        "maps",
    ):
        assert not pattern.search(key), key


# ── the frozen doc is untouched by everything above ───────────────────────────


def test_frozen_doc_sections_untouched_by_the_package() -> None:
    """The package never edits §1-§7: the only doc-touching code is
    append_run_ledger (EOF append into §9), which --record alone fires."""
    text = runner.DOC_PATH.read_text()
    assert "## 8. Amendment log" in text and "## 9. Run ledger" in text
    # Post-record era (PR #412): §9 carries the recorded run above the
    # historical empty-marker line (the runner appends at EOF of §9's
    # text, after the pre-record marker that stays as history); the
    # recorded entry is present and §9 remains the LAST section.
    assert "— RUN — arms A0/C/B/A" in text
    tail = text.rsplit("## 9. Run ledger", 1)[1]
    assert tail.rstrip().endswith("single-look analysis follows collection (§6.6).")
