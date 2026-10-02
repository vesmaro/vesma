"""E2 lanes strata — corpus wave structural tests (E0 §3.1-§3.3, §4.3-§4.4).

These are PREPARATION gates, not experiment runs: they pin the corpus
counts, the distribution profile, the seed hygiene, the blind
adjudication structure, the replacement-ledger mechanics and the
isolation from the S1 stand's measured corpus. No leg logic, no
assemble calls, no metrics — E3 runs happen later under their own
pre-registered gates.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest
from benchmarks.corpus.corpus import CORPUS, GoldenEntry
from benchmarks.corpus.queries import GOLDEN_QUERIES
from benchmarks.stands.s1_quality import run as s1_run
from benchmarks.strata.e2_gov import ground_truth as gt
from benchmarks.strata.e2_gov import profile as prof
from benchmarks.strata.e2_gov import queries as q
from benchmarks.strata.e2_gov import records as rec
from benchmarks.strata.e2_gov.checkpoints import CHECKPOINT_ENTRIES
from benchmarks.strata.e2_gov.loader import fresh_experimental_manager, stratum_record_ids
from benchmarks.strata.e2_gov.profile import experimental_corpus
from benchmarks.strata.e2_gov.records import GOV_RECORDS

# ── §3.1 seeding budget and analyzed lock ─────────────────────────────────────


def test_gov_records_follow_e0_seeding_budget() -> None:
    assert len(GOV_RECORDS) == 100  # E0 §3.1: 60-100 permitted; full budget used
    tags = Counter(e.mnemos_tags[0] for e in GOV_RECORDS)
    assert tags == {"rule": 12, "decision": 88}
    # E0 §3.3: rules/decisions per live ratio (30:217 ≈ 0.1382)
    ratio = 12 / 88
    live = prof.LIVE_STORE_RULES / prof.LIVE_STORE_DECISIONS
    assert abs(ratio - live) / live < 0.05
    # E0 §4.4 seed hygiene: everything published, nothing planted, no secrets
    assert all(e.status == "published" for e in GOV_RECORDS)
    assert all(not e.planted for e in GOV_RECORDS)
    slugs = [e.slug for e in GOV_RECORDS]
    assert len(set(slugs)) == 100
    golden_projects = {"aurora-api", "vault-ui", "mnemos-core", "atlas-pipeline"}
    assert all(e.project in golden_projects for e in GOV_RECORDS)


def test_analyzed_and_pool_partition() -> None:
    analyzed = set(rec.ANALYZED_GOV_SLUGS)
    pool = set(rec.REPLACEMENT_POOL_SLUGS)
    assert len(analyzed) == 48  # E0 §3.1 lock: 48 records
    assert len(pool) == 52  # surplus = replacement pool
    assert analyzed | pool == set(rec.GOV_RECORD_SLUGS)
    assert not analyzed & pool
    analyzed_tags = Counter(
        rec.gov_record_by_slug(s).mnemos_tags[0] for s in rec.ANALYZED_GOV_SLUGS
    )
    # the analyzed 48 mirrors the governance shape: 6 rules + 42 decisions
    assert analyzed_tags == {"rule": 6, "decision": 42}


# ── §3.1 queries: 2 per record, ph verbatim, pr disjoint ─────────────────────


def _norm_tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _four_grams(text: str) -> set[tuple[str, ...]]:
    tokens = _norm_tokens(text)
    return {tuple(tokens[i : i + 4]) for i in range(len(tokens) - 3)}


def test_gov_query_pairs_match_e0_denominator() -> None:
    # every seeded record carries exactly one ph + one pr query
    assert len(q.GOV_QUERIES) == 200
    by_record: dict[str, list[q.GovQuery]] = {}
    for query in q.GOV_QUERIES:
        by_record.setdefault(query.record_slug, []).append(query)
    assert set(by_record) == set(rec.GOV_RECORD_SLUGS)
    for slug, pair in by_record.items():
        assert sorted(f.family for f in pair) == ["ph", "pr"], slug
    # the analyzed denominator: 48 records x 2 phrasings = 96 (E0 §3.1)
    assert len(q.ANALYZED_GOV_QUERIES) == 96
    assert len(q.ANALYZED_GOV_QIDS) == 96
    assert all(query.record_slug in set(rec.ANALYZED_GOV_SLUGS) for query in q.ANALYZED_GOV_QUERIES)
    assert len(q.GOV_QIDS) == 200  # qids unique across all definitions


def test_phrase_queries_are_verbatim_and_paraphrases_disjoint() -> None:
    for query in q.GOV_QUERIES:
        record = rec.gov_record_by_slug(query.record_slug)
        if query.family == "ph":
            assert query.text in record.content, (query.qid, query.text)
        else:
            overlap = _four_grams(query.text) & _four_grams(record.content)
            assert not overlap, (query.qid, overlap)


# ── §3.2 negative control ─────────────────────────────────────────────────────

_GOLDEN_BY_SLUG = {e.slug: e for e in CORPUS}
_GOVERNANCE_CLASSES = {"rule", "decision"}


def test_neg_queries_are_knowledge_only_by_construction() -> None:
    assert len(q.NEG_QUERIES) == 24  # E0 §3.2: ~24
    assert len(q.NEG_QIDS) == 24
    for neg in q.NEG_QUERIES:
        assert neg.expected, neg.qid
        for slug in neg.expected:
            entry = _GOLDEN_BY_SLUG.get(slug)
            assert entry is not None, (neg.qid, slug)
            # admissible (a judgment against a raw entry is unwinnable)
            assert entry.status in {"published", "processed"}, (neg.qid, slug)
            # knowledge content only — a governance-class gold would break
            # "false insertion by construction" (E0 §3.2, §2.4b)
            assert not set(entry.mnemos_tags) & _GOVERNANCE_CLASSES, (neg.qid, slug)


# ── §3.3 distribution-matched profile ────────────────────────────────────────


def test_combined_corpus_hits_checkpoint_pin() -> None:
    combined = experimental_corpus()
    assert len(combined) == 421  # 81 golden + 100 gov + 240 padding
    checkpoints = sum(1 for e in combined if "checkpoint" in e.mnemos_tags)
    assert checkpoints == 244  # 4 golden + 240 padding
    share = checkpoints / len(combined)
    # registered pin: 58% ± 2 pp (E0 §3.3, the tolerance is the E0 fill)
    assert 0.56 <= share <= 0.60
    assert share == pytest.approx(0.5796, abs=1e-4)


def test_profile_json_pinned_to_modules() -> None:
    recorded = prof.load_profile()
    computed = prof.build_profile()
    assert recorded["stratum_version"] == prof.STRATUM_VERSION
    assert recorded["corpus_fingerprint"] == prof.corpus_fingerprint()
    assert len(recorded["corpus_fingerprint"]) == 64
    int(recorded["corpus_fingerprint"], 16)
    assert recorded["counts"] == computed["counts"]
    assert recorded["distribution"] == computed["distribution"]
    assert prof.distribution_ok(recorded)
    counts = recorded["counts"]
    assert counts["gov_records"] == 100
    assert counts["analyzed_records"] == 48
    assert counts["analyzed_queries"] == 96
    assert counts["replacement_pool"] == 52
    assert counts["neg_queries"] == 24
    assert counts["checkpoint_padding"] == 240


def test_fingerprint_is_bytes_sensitive() -> None:
    """Mutating any stratum module byte changes the fingerprint (mutation check)."""
    import pathlib

    module = pathlib.Path(rec.__file__)
    original = module.read_bytes()
    try:
        module.write_bytes(original + b"\n# mutation\n")
        assert prof.corpus_fingerprint() != prof.load_profile()["corpus_fingerprint"]
    finally:
        module.write_bytes(original)
    assert prof.corpus_fingerprint() == prof.load_profile()["corpus_fingerprint"]


# ── §4.4 seed hygiene + injection screen (invariant) ─────────────────────────

_FORBIDDEN = (
    re.compile(r"\b(you|your|yours)\b", re.IGNORECASE),  # second person
    re.compile(r"applyto", re.IGNORECASE),  # mint/pin targeting syntax
    re.compile(r"\bsev(erity)?\s*[-:]?\s*\d", re.IGNORECASE),  # severity tags
    re.compile(r"\bp[0-3]\b", re.IGNORECASE),
)


@pytest.mark.parametrize("entry", [*GOV_RECORDS, *CHECKPOINT_ENTRIES], ids=lambda e: e.slug)
def test_seed_hygiene_declarative_prose(entry: GoldenEntry) -> None:
    for text in (entry.title, entry.content):
        for pattern in _FORBIDDEN:
            assert not pattern.search(text), (entry.slug, pattern.pattern, text)


@pytest.mark.parametrize("entry", [*GOV_RECORDS, *CHECKPOINT_ENTRIES], ids=lambda e: e.slug)
def test_seed_injection_screen_clean(entry: GoldenEntry) -> None:
    """E0 §6.3 invariant: seed injection-acceptance = 1.000 — the real
    mnemos danger detectors find nothing in any stratum seed."""
    from vesma.danger_detectors import detect

    result = detect(entry.content, title=entry.title)
    assert not result.findings, (entry.slug, result.findings)
    assert not result.error, (entry.slug, result.error)


# ── §4.3 blind adjudication structure ─────────────────────────────────────────


def test_worksheet_is_blind_and_keys_are_separate() -> None:
    worksheet, answer_key = gt.build_adjudication_worksheet()
    # judge-visible payload: nothing beyond the pair text fields
    allowed = {"pair_id", "query_text", "candidate_title", "candidate_content"}
    for pair in worksheet["pairs"]:
        assert set(pair) == allowed
    assert set(worksheet) == {
        "rubric",
        "instructions",
        "double_annotation_share",
        "kappa_floor",
        "pairs",
    }
    # Leg-stripping is a STRUCTURAL property: no leg/run/provenance/gold
    # FIELD may exist anywhere in the judge-visible artifact. Prose uses
    # of the word "leg" (e.g. "the vector leg" in mnemos retrieval
    # records) are corpus content, not leg-revealing provenance — E0
    # §4.3 strips the latter, not the English language.
    blob = json.dumps(worksheet)
    for field in (
        '"leg"',
        '"run_id"',
        '"qid"',
        '"candidate_slug"',
        '"intended_gold"',
        "project:",
        "agent:",
        "mnemos:",
    ):
        assert field not in blob, field
    # the registered rubric rides with the worksheet verbatim
    assert list(worksheet["rubric"]) == list(gt.ADJUDICATION_RUBRIC)
    assert worksheet["double_annotation_share"] == 0.2
    assert worksheet["kappa_floor"] == 0.6
    # answer key: separate artifact, complete and deterministic
    rows = answer_key["rows"]
    assert len(rows) == len(worksheet["pairs"]) == 96 * 3
    pair_ids = [r["pair_id"] for r in rows]
    assert len(set(pair_ids)) == len(pair_ids)
    golds = [r for r in rows if r["intended_gold"]]
    assert len(golds) == 96  # exactly one intended gold per analyzed query
    gold_qids = {r["qid"] for r in golds}
    assert gold_qids == q.ANALYZED_GOV_QIDS
    # determinism: rebuilding yields byte-identical artifacts
    again_ws, again_key = gt.build_adjudication_worksheet()
    assert again_ws == worksheet and again_key == answer_key


# ── §3.1/§4.4 replacement ledger ──────────────────────────────────────────────


def test_committed_ledger_is_pristine() -> None:
    ledger = gt.load_ledger()
    assert ledger == gt.initial_ledger()
    assert ledger["denominator"] == 96
    assert ledger["rejects"] == []
    assert ledger["replacements"] == []
    assert len(ledger["analyzed_records"]) == 48
    assert len(ledger["replacement_pool"]) == 52


def test_replacement_mechanics_hold_denominator() -> None:
    ledger = gt.initial_ledger()
    rejected = ledger["analyzed_records"][7]
    expected_replacement = ledger["replacement_pool"][0]
    amended, replacement = gt.record_rejection(
        ledger, rejected, "self-sufficiency fail", "2026-09-13"
    )
    assert replacement == expected_replacement
    assert len(amended["analyzed_records"]) == 48  # swap, not shrink
    assert expected_replacement in amended["analyzed_records"]
    assert rejected not in amended["analyzed_records"]
    assert amended["replacement_pool"][0] == ledger["replacement_pool"][1]
    assert len(gt.active_analyzed_queries(amended)) == 96  # E0 §3.1 denominator
    # the original ledger is untouched (functional, not in-place)
    assert gt.active_analyzed_queries(ledger) == q.ANALYZED_GOV_QUERIES
    assert ledger["rejects"] == []


def test_replacement_rejects_invalid_input() -> None:
    ledger = gt.initial_ledger()
    with pytest.raises(ValueError, match="not in the analyzed set"):
        gt.record_rejection(ledger, ledger["replacement_pool"][0], "x", "2026-09-13")
    amended, _ = gt.record_rejection(ledger, ledger["analyzed_records"][0], "r", "2026-09-13")
    # a rejected slug is REMOVED from the analyzed set by the swap, so
    # re-rejecting it lands in "not in the analyzed set" — the guard for
    # "already rejected" exists for a corrupted ledger (reject recorded
    # while the slug stayed analyzed); exercise that state explicitly.
    with pytest.raises(ValueError, match="not in the analyzed set"):
        gt.record_rejection(amended, ledger["analyzed_records"][0], "r", "2026-09-13")
    corrupted = {
        **ledger,
        "rejects": [{"record": ledger["analyzed_records"][0], "reason": "x", "decided_at": "d"}],
    }
    with pytest.raises(ValueError, match="already rejected"):
        gt.record_rejection(corrupted, ledger["analyzed_records"][0], "r", "2026-09-13")


def test_replacement_pool_exhaustion_is_loud() -> None:
    ledger = gt.initial_ledger()
    for _ in range(52):  # drain the whole pool
        ledger, _ = gt.record_rejection(ledger, ledger["analyzed_records"][0], "r", "d")
        assert len(gt.active_analyzed_queries(ledger)) == 96
    with pytest.raises(gt.PoolExhaustedError):
        gt.record_rejection(ledger, ledger["analyzed_records"][0], "r", "d")


# ── isolation from the S1 stand (bench-s1 keeps its meaning) ──────────────────


def test_strata_are_outside_the_s1_measured_corpus() -> None:
    golden_slugs = {e.slug for e in CORPUS}
    strata_slugs = rec.GOV_RECORD_SLUGS | {e.slug for e in CHECKPOINT_ENTRIES}
    assert not golden_slugs & strata_slugs
    golden_qids = {query.qid for query in GOLDEN_QUERIES}
    assert not golden_qids & (q.GOV_QIDS | q.NEG_QIDS)
    for query in GOLDEN_QUERIES:  # judgments never point into the strata
        assert not query.expected & strata_slugs


def test_s1_corpus_fingerprint_unchanged_this_wave() -> None:
    """The re-baseline decision for S1: NOT triggered. The strata modules
    are outside the S1 fingerprint module set, so the stand measures the
    byte-identical corpus it measured before this wave."""
    recorded = json.loads(s1_run.BASELINE_PATH.read_text())
    live = s1_run.corpus_fingerprint()
    assert recorded["corpus_fingerprint"] == live
    pinned = "c2ce056d57d91143f7a1959442ef2b37891464d4cd5f218f5eabbc785c8e72f1"
    assert recorded["corpus_fingerprint"] == pinned
    # and the recorded profile states the same decision explicitly
    decision = prof.load_profile()["re_baseline_decision"]
    assert decision["s1_rebaseline_required"] is False


# ── corpus loads (ingest smoke — no metrics, no legs) ─────────────────────────


def test_experimental_corpus_loads_into_fresh_manager() -> None:
    with fresh_experimental_manager() as (mgr, slug_to_id):
        assert len(slug_to_id) == 421
        stratum_ids = stratum_record_ids(slug_to_id)
        assert len(stratum_ids) == 100 + 240
        # load-level smoke: the store answers a seeded phrase query
        # through the ordinary search path (no ranking assertion — that
        # is an E3 measurement, not preparation)
        probe = q.ANALYZED_GOV_QUERIES[0]
        results = mgr.search(probe.text, project=probe.project, limit=10)
        assert isinstance(results, list)
        assert results, "fresh ingest returned nothing for a verbatim phrase query"


def test_loader_refuses_stratum_slug_masquerade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#277 P3 masquerade guard: ingest routes by slug membership in
    ``_GOLDEN_SLUGS``, so a stratum seed whose slug collided with a
    golden slug would silently take the golden-restore branch and dodge
    the fail-loud demotion screen. The loader now asserts disjointness
    AT the routing boundary (defense in depth under the test above)."""
    from benchmarks.strata.e2_gov import loader as loader_mod

    sneaky = frozenset({rec.GOV_RECORDS[0].slug})
    monkeypatch.setattr(loader_mod, "_GOLDEN_SLUGS", sneaky)
    with pytest.raises(AssertionError, match="masquerade"):
        loader_mod.build_experimental_manager(tmp_path)
