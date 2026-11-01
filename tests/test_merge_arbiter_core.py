"""The Choice-arbiter v2 core (``vesma.merge_arbiter``) — conformance.

The ratified contour is pinned here in its engine-native form: the
verify-before-load pins, the LGBM v4 dump walker (against the REAL
lightgbm scores frozen in the fixture), the §2 ladder legs, the zone-v1
one-sidedness, the mean-over-others permutation invariance, and the
keep-rule thresholds. Reference: the integration package §1-§2 and the
ADOPT report (vesma-cortex, 2026-10-30); ADR-0033 — the GCW package is
reference, this module is the native form.
"""

from __future__ import annotations

import hashlib
import json
import math
from importlib.resources import files as resource_files
from pathlib import Path

import numpy as np
import pytest

from vesma.decision_provider import (
    CORTEX_FEATURE_NAMES,
    CanonRecordView,
    cortex_feature_set_sha256,
    cortex_pair_features,
)
from vesma.merge_arbiter import (
    _EMBED_CACHE_MAX_ENTRIES,
    CHAR4_JACCARD_IDX,
    CHAR5_JACCARD_IDX,
    IDX_E_COS,
    IDX_EPOCH,
    IDX_NULL,
    IDX_ZEPS_SUM,
    MERGE_ARTIFACT_DIR,
    MERGE_ARTIFACT_FILE,
    MERGE_ARTIFACT_SHA256,
    MERGE_FEATURE_NAMES,
    MERGE_MODEL_NAME,
    ArbiterMember,
    ArbiterVerdict,
    LightGbmDump,
    MergeArbiter,
    MergeArbiterContractError,
    MergeArbiterLoadError,
    MergeArbiterPinError,
    MergeArbiterSchemaError,
    _side_dict,
    assert_cortex_merge_embedder_pin,
    decide_keep,
    keep_oldest,
    member_embed_text,
    zeps_flags,
)

PIN = "nano:sha256:3b752e0671a50da5c108cb50e49630a66c160f7683afedcf879e1880d84317ba"


def _norm_stub_embed(text: str) -> list[float]:
    """Deterministic stub embedder: the unit vector of the normalized
    text's hash — norm-identical texts share the vector (cos 1.0),
    disjoint texts are near-orthogonal. The SAME identity the vector
    store leg sees, so hook-level tests qualify near-dups coherently."""
    digest = hashlib.sha256(" ".join(text.split()).lower().encode("utf-8")).digest()
    vec = np.zeros(384)
    for i, b in enumerate(digest):
        vec[i % 384] += b / 255.0
    norm = float(np.linalg.norm(vec)) or 1.0
    return (vec / norm).tolist()


def _member(
    id: str,
    body: str,
    *,
    title: str = "note",
    tags: tuple[str, ...] = ("t1",),
    language: str | None = "ru",
    record_type: str | None = "note",
    epoch: float = 100.0,
    null: float = 0.0,
) -> ArbiterMember:
    return ArbiterMember(
        id=id,
        view=CanonRecordView(
            title=title, body=body, tags=tags, language=language, record_type=record_type
        ),
        created_epoch=epoch,
        created_null=null,
    )


@pytest.fixture(scope="module")
def arbiter() -> MergeArbiter:
    return MergeArbiter(_norm_stub_embed, PIN)


# ---------------------------------------------------------------------------
# 1. Verify-before-load
# ---------------------------------------------------------------------------


def test_bundled_artifact_passes_verify_before_load(arbiter: MergeArbiter) -> None:
    """The bundled bytes ARE the ratified artifact: load + smoke scoring
    succeed; the thresholds/zone constants come FROM the artifact."""
    assert arbiter.tau_hi == 0.90
    assert arbiter.tau_m == 0.50
    assert arbiter.zone_tau_cos == 0.85
    assert arbiter.zone_tau_lex == 0.05
    assert arbiter.zone_tau_lex2 == 0.20
    assert arbiter.zone_zeps_min == 3
    assert arbiter.zone_eps == 0.01
    assert arbiter.weights_sha256 == MERGE_ARTIFACT_SHA256
    assert arbiter.artifact_embedder_pin == PIN
    assert len(arbiter._dump.trees) == 200


def test_artifact_bytes_pin_is_the_ratified_sha256() -> None:
    """The pin constant matches the bundled FILE on disk (a swapped file
    with an unchanged constant is exactly the drift the loader refuses —
    this test pins the constant itself)."""
    bundle = resource_files("vesma") / "models" / MERGE_ARTIFACT_DIR
    raw = (Path(str(bundle)) / MERGE_ARTIFACT_FILE).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == MERGE_ARTIFACT_SHA256


def test_artifact_sha_mismatch_refuses_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vesma.merge_arbiter.MERGE_ARTIFACT_SHA256", "0" * 64)
    with pytest.raises(MergeArbiterPinError, match="not the ADOPT"):
        MergeArbiter(_norm_stub_embed, PIN)


def test_missing_artifact_refuses_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vesma.merge_arbiter.MERGE_ARTIFACT_DIR", "no-such-bundle")
    with pytest.raises(MergeArbiterLoadError, match="missing"):
        MergeArbiter(_norm_stub_embed, PIN)


def test_foreign_embedder_fingerprint_is_a_pin_refusal() -> None:
    with pytest.raises(MergeArbiterPinError, match="recalibration event"):
        MergeArbiter(_norm_stub_embed, "nano:sha256:deadbeef")


def test_empty_fingerprint_is_a_pin_refusal() -> None:
    with pytest.raises(MergeArbiterPinError, match="unassertable"):
        assert_cortex_merge_embedder_pin(PIN, "")


def test_artifact_identity_is_exact_not_a_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """S-3 (security audit): an artifact whose identity merely EXTENDS
    the ratified name (``vesma-cortex-merge-v2-evil``) is refused even
    when its bytes pass the sha256 pin — the identity check is EXACT
    equality, so a future pin loosening cannot reopen the hole."""
    bundle = tmp_path / "models" / MERGE_ARTIFACT_DIR
    bundle.mkdir(parents=True)
    raw = json.dumps({"artifact": MERGE_MODEL_NAME + "-evil"}).encode("utf-8")
    (bundle / MERGE_ARTIFACT_FILE).write_bytes(raw)
    monkeypatch.setattr("vesma.merge_arbiter.resource_files", lambda _pkg: tmp_path)
    monkeypatch.setattr(
        "vesma.merge_arbiter.MERGE_ARTIFACT_SHA256",
        hashlib.sha256(raw).hexdigest(),
    )
    with pytest.raises(MergeArbiterContractError, match="exact identity"):
        MergeArbiter(_norm_stub_embed, PIN)


def test_embed_cache_is_bounded(arbiter: MergeArbiter) -> None:
    """S-1 (security audit): the embed-text cache cannot grow with the
    process lifetime — after warming past the ceiling with unique texts
    the cache sits AT the ceiling, the cold (oldest) entries are
    evicted, the hot (newest) ones stay."""
    limit = _EMBED_CACHE_MAX_ENTRIES
    for i in range(limit + 50):
        arbiter._member_vector(
            CanonRecordView(title=f"cache probe {i}", body=f"unique filler body {i}")
        )
    assert len(arbiter._text_cache) <= limit
    assert len(arbiter._text_cache) == limit  # the ceiling is exact
    cold_key = member_embed_text(
        CanonRecordView(title="cache probe 0", body="unique filler body 0")
    )
    hot_key = member_embed_text(
        CanonRecordView(title=f"cache probe {limit + 49}", body=f"unique filler body {limit + 49}")
    )
    assert cold_key not in arbiter._text_cache  # LRU tail evicted
    assert hot_key in arbiter._text_cache  # the hot head survives


def test_feature_contract_is_frozen_and_reuses_the_pair_block() -> None:
    """34 names, append-only order; the first 13 ARE the frozen pair
    contract (imported, never re-implemented)."""
    assert len(MERGE_FEATURE_NAMES) == 34
    assert MERGE_FEATURE_NAMES[:13] == CORTEX_FEATURE_NAMES
    bundle = resource_files("vesma") / "models" / MERGE_ARTIFACT_DIR
    doc = json.loads((Path(str(bundle)) / MERGE_ARTIFACT_FILE).read_text("utf-8"))
    assert tuple(doc["feature_names"]) == MERGE_FEATURE_NAMES
    # The pair block's digest is the W42 wrapper's digest (same names).
    assert (
        hashlib.sha256("\n".join(CORTEX_FEATURE_NAMES).encode("utf-8")).hexdigest()
        == cortex_feature_set_sha256()
    )


def test_member_embed_text_is_the_pinned_convention() -> None:
    view = CanonRecordView(title="  Deploy   NOTE ", body="rollout  of\nvesma", tags=("A b", "c"))
    assert member_embed_text(view) == "deploy note\nrollout of vesma\na b c"
    assert len(member_embed_text(CanonRecordView(title="t", body="x" * 5000))) <= 4096


# ---------------------------------------------------------------------------
# 2. The LGBM v4 dump walker (exactness vs the real runtime)
# ---------------------------------------------------------------------------


def _fixture_rows() -> tuple[dict, list[dict]]:
    fixture = Path(__file__).parent / "fixtures" / "merge_arbiter_lgbm_scores.jsonl"
    lines = fixture.read_text("utf-8").splitlines()
    header = json.loads(lines[0])
    return header, [json.loads(line) for line in lines[1:]]


def test_walker_matches_real_lightgbm_on_pinned_fixture(arbiter: MergeArbiter) -> None:
    """The local dump walker reproduces lightgbm 4.7.0's predict() on 980
    pinned rows (uniform + boundary + band rows) within 1e-9 — the
    Δdeps=0 discipline's exactness proof. The fixture header carries the
    full provenance (generator script, versions, seed)."""
    header, rows = _fixture_rows()
    assert header["artifact_sha256"] == MERGE_ARTIFACT_SHA256
    assert header["lightgbm_version"] == "4.7.0"
    assert len(rows) == header["rows"] == 980
    worst = 0.0
    for row in rows:
        got = arbiter._dump.predict_proba(np.array(row["row"], dtype=np.float64))
        worst = max(worst, abs(got - row["lgbm"]))
    assert worst <= 1e-9, f"walker drifted vs lightgbm: max|delta|={worst}"


def test_walker_refuses_non_binary_objective() -> None:
    dump = (
        "tree\nversion=v4\nnum_class=1\nmax_feature_idx=0\nobjective=regression\n\n"
        "Tree=0\nnum_leaves=2\nnum_cat=0\nsplit_feature=0\nthreshold=0.5\n"
        "decision_type=2\nleft_child=-1\nright_child=-2\nleaf_value=0.1 0.2\n"
    )
    with pytest.raises(MergeArbiterContractError, match="objective"):
        LightGbmDump.parse(dump, expected_features=1)


def test_walker_refuses_wrong_feature_width() -> None:
    dump = (
        "tree\nversion=v4\nmax_feature_idx=4\nobjective=binary sigmoid:1\n\n"
        "Tree=0\nnum_leaves=2\nnum_cat=0\nsplit_feature=0\nthreshold=0.5\n"
        "decision_type=2\nleft_child=-1\nright_child=-2\nleaf_value=0.1 0.2\n"
    )
    with pytest.raises(MergeArbiterContractError, match="desync"):
        LightGbmDump.parse(dump, expected_features=34)


def test_walker_refuses_categorical_splits() -> None:
    dump = (
        "tree\nversion=v4\nmax_feature_idx=0\nobjective=binary sigmoid:1\n\n"
        "Tree=0\nnum_leaves=2\nnum_cat=1\nsplit_feature=0\nthreshold=0.5\n"
        "decision_type=2\nleft_child=-1\nright_child=-2\nleaf_value=0.1 0.2\n"
    )
    with pytest.raises(MergeArbiterContractError, match="categorical"):
        LightGbmDump.parse(dump, expected_features=1)


# ---------------------------------------------------------------------------
# 3. The §2 ladder
# ---------------------------------------------------------------------------


def test_ch0_byte_identity_keeps_oldest(arbiter: MergeArbiter) -> None:
    older = _member("b-old", "identical body", epoch=10.0)
    newer = _member("a-new", "identical body", epoch=999.0)
    verdict = arbiter.arbitrate([newer, older])
    assert (verdict.verdict, verdict.leg, verdict.ch_class) == (
        "keep",
        "ch0-tie-break",
        "CH-0",
    )
    assert verdict.keeper_id == "b-old"


def test_ch5_normalization_identity_keeps_oldest(arbiter: MergeArbiter) -> None:
    verdict = arbiter.arbitrate(
        [_member("b", "Hello   World", epoch=200.0), _member("a", "hello world", epoch=100.0)]
    )
    assert verdict.verdict == "keep"
    assert verdict.leg == "ch5-tie-break"
    assert verdict.keeper_id == "a"


def test_ch2_metadata_conflict_refuses_and_forbids_fold(
    arbiter: MergeArbiter,
) -> None:
    verdict = arbiter.arbitrate(
        [
            _member("b", "same body", tags=("x",), epoch=200.0),
            _member("a", "same body", tags=("y",), epoch=100.0),
        ]
    )
    assert (verdict.verdict, verdict.leg, verdict.ch_class) == (
        "abstain",
        "ch2-refusal",
        "CH-2",
    )
    assert verdict.keeper_id is None


def test_zone_cos_leg_fires_on_the_low_side_boundary() -> None:
    """cos exactly AT the frozen boundary (0.85, inclusive) with a
    lexically dead pair fires the refusal leg; the vector geometry is
    injected through a dedicated stub (unit vectors with dot 0.85)."""
    a = _member("A", "alpha bravo charlie delta", language="ru")
    b = _member("B", "yankee xray november kilo", language="en")
    cos_target = 0.85
    v_a = np.array([1.0, 0.0])
    v_b = np.array([cos_target, math.sqrt(1.0 - cos_target * cos_target)])
    vectors = {
        member_embed_text(a.view): v_a.tolist(),
        member_embed_text(b.view): v_b.tolist(),
    }

    def embed(text: str) -> list[float]:
        return vectors[text]

    zone_arbiter = MergeArbiter(embed, PIN)
    verdict = zone_arbiter.arbitrate([a, b])
    assert verdict.verdict == "abstain"
    assert verdict.leg == "zone-v1"
    assert verdict.zone_hit is not None
    assert verdict.zone_hit["zone_leg"] == "cos"
    assert verdict.zone_hit["cos"] == pytest.approx(0.85, abs=1e-9)


def test_zone_is_refusal_only_and_never_a_keep(
    arbiter: MergeArbiter,
) -> None:
    """One-sidedness: a zone-fire can only ever produce an ABSTAIN — the
    keep side of cos/zeps/char5 is read nowhere."""
    a = _member(
        "A",
        "Разговор о погоде и планах на вечер",
        language="ru",
    )
    b = _member("B", "Weekly sync notes and action items", language="en")
    for order in ([a, b], [b, a]):
        verdict = arbiter.arbitrate(order)
        assert verdict.verdict == "abstain"
        assert verdict.leg == "zone-v1"


# ── F1 (cascade review): the zone reads char5 BY NAME, never char4 ───────────

_LETTERS_A = "acegik"
_LETTERS_B = "bdfhjl"


def _filler_words(prefix: str, letters: str, n: int) -> list[str]:
    """Distinct 4-char words from a disjoint letter alphabet — filler
    n-grams never collide across the two sides (the m/n prefix and the
    disjoint suffix letters keep every filler gram one-sided)."""
    words: list[str] = []
    i = 0
    while len(words) < n:
        w = prefix + "".join(letters[(i >> (2 * k)) & 3] for k in range(3))
        i += 1
        if len(set(w)) == 4:
            words.append(w)
    return words


def _char_axis_pair(
    n_tokens: int,
    n_filler: int,
    *,
    language: tuple[str, str] = ("ru", "ru"),
    tags: tuple[tuple[str, ...], tuple[str, ...]] = (("tg",), ("tg",)),
    record_type: tuple[str, str] = ("note", "note"),
) -> tuple[ArbiterMember, ArbiterMember, float, float]:
    """A crafted pair whose char4 and char5 axes are SEPARATED: shared
    digit-tokens ride through both bodies (4-grams share through the bare
    token), but side A dot-glues the token run (no spaces — every A-side
    token 5-gram carries a period) while side B space-separates them, so
    the 5-gram sets stay near-disjoint; fillers live on disjoint
    alphabets. Returns the two members plus the pair's char4/char5 as the
    FROZEN extractor computes them — every test using this craft asserts
    the separation itself, so the construction is self-verifying."""
    tokens = [f"t{i:03d}" for i in range(n_tokens)]
    fa = _filler_words("m", _LETTERS_A, n_filler)
    fb = _filler_words("n", _LETTERS_B, n_filler)
    a_body = " ".join(fa) + " " + ".".join(tokens)
    b_body = (
        " ".join(fb[: n_filler // 2]) + " " + " ".join(tokens) + " " + " ".join(fb[n_filler // 2 :])
    )
    a = ArbiterMember(
        id="ca",
        view=CanonRecordView(
            title="shared title",
            body=a_body,
            tags=tags[0],
            language=language[0],
            record_type=record_type[0],
        ),
        created_epoch=100.0,
    )
    b = ArbiterMember(
        id="cb",
        view=CanonRecordView(
            title="shared title",
            body=b_body,
            tags=tags[1],
            language=language[1],
            record_type=record_type[1],
        ),
        created_epoch=200.0,
    )
    pair = cortex_pair_features(a.view, b.view, similarity=0.0)
    return a, b, float(pair[CHAR4_JACCARD_IDX]), float(pair[CHAR5_JACCARD_IDX])


def test_zone_cos_leg_gate_reads_char5_not_char4() -> None:
    """F1 (a): a pair with char4 > tau_lex ≥ char5 — many shared short
    fragments that stay 4-grams but never co-occur as 5-grams — MUST
    fire the zone's cos leg. On the positional-sibling bug (reading
    char4) the gate sees 0.15 > 0.05, no leg fires and the pair lands on
    the model leg — this test is RED there by construction."""
    a, b, char4, char5 = _char_axis_pair(100, 150)
    assert char4 > 0.05, f"craft lost the char4 separation: {char4}"
    assert char5 <= 0.05, f"craft lost the char5 separation: {char5}"
    # the zeps leg must stay silent here (zeps < 3 on BOTH readings), so
    # the only fire path is the cos gate
    cos_target = 0.85
    v_a = [1.0, 0.0]
    v_b = [cos_target, math.sqrt(1.0 - cos_target * cos_target)]
    vectors = {
        member_embed_text(a.view): v_a,
        member_embed_text(b.view): v_b,
    }

    def embed(text: str) -> list[float]:
        return vectors[text]

    verdict = MergeArbiter(embed, PIN).arbitrate([a, b])
    assert verdict.verdict == "abstain"
    assert verdict.leg == "zone-v1"
    assert verdict.zone_hit is not None
    assert verdict.zone_hit["zone_leg"] == "cos"


def test_zeps_leg_gate_and_char5_flag_read_char5_not_char4(
    arbiter: MergeArbiter,
) -> None:
    """F1 (b): the zeps mirror — the ``zeps_char5`` eps-flag and the
    tau_lex2 gate read the CHAR5 axis. The craft carries three metadata
    flags (language/tags/record_type) plus a near-zero char5, so the new
    code fires the zeps leg; on the char4 positional bug the flag is 0,
    the gate sees 0.24 > 0.20 and nothing fires — RED there."""
    a, b, char4, char5 = _char_axis_pair(
        400,
        150,
        language=("ru", "en"),
        tags=(("tg",), ("tgx",)),
        record_type=("note", "task"),
    )
    assert char4 > 0.20, f"craft lost the char4 separation: {char4}"
    assert char5 <= 0.20, f"craft lost the char5 separation: {char5}"
    # the eps-flag itself fires BY CHAR5 (≤ eps), never by the 4-gram axis
    flags = zeps_flags(_side_dict(a.view), _side_dict(b.view), eps=0.01, char5=char5)
    assert flags[1] == 1, f"zeps_char5 flag did not fire: char5={char5} flags={flags}"
    # same stub vector on both sides → cos 1.0: the cos leg cannot fire,
    # the zone must arrive through the zeps leg
    vectors = {
        member_embed_text(a.view): [1.0, 0.0],
        member_embed_text(b.view): [1.0, 0.0],
    }

    def embed(text: str) -> list[float]:
        return vectors[text]

    verdict = MergeArbiter(embed, PIN).arbitrate([a, b])
    assert verdict.verdict == "abstain"
    assert verdict.leg == "zone-v1"
    assert verdict.zone_hit is not None
    assert verdict.zone_hit["zone_leg"] == "zeps"


def test_high_overlap_pair_reaches_the_model_leg(arbiter: MergeArbiter) -> None:
    """A pair with near-identical bodies (char5 high, zeps low, identical
    metadata) escapes every code leg — different body bytes AND norm
    keep it out of CH-0/CH-5, high char5 blocks the zone's cos leg,
    zeps=1 stays under 3 — and lands on the model leg with k decoded
    scores in [0, 1]."""
    a = _member(
        "a",
        "Rollout plan for the 6.6 release train variant one",
        epoch=100.0,
    )
    b = _member(
        "b",
        "Rollout plan for the 6.6 release train variant two",
        epoch=200.0,
    )
    verdict = arbiter.arbitrate([a, b])
    assert verdict.leg == "model"
    assert verdict.ch_class == "undefined"
    assert set(verdict.scores) == {"a", "b"}
    assert all(0.0 <= v <= 1.0 for v in verdict.scores.values())
    assert verdict.e1 is not None and verdict.e2 is not None


def test_ladder_order_ch0_beats_ch2(arbiter: MergeArbiter) -> None:
    """Byte-identical bodies WITH full metadata identity is CH-0 even
    though a metadata-differing pair would refuse — the order is part of
    the ratified semantics."""
    verdict = arbiter.arbitrate(
        [
            _member("a", "same", tags=("t1",), epoch=100.0),
            _member("b", "same", tags=("t1",), epoch=5.0),
        ]
    )
    assert verdict.leg == "ch0-tie-break"


def test_keep_oldest_tie_break_is_never_serialization_order() -> None:
    members = [
        _member("zz", "b", epoch=500.0),
        _member("aa", "b", epoch=500.0),
        _member("mm", "b", epoch=1.0),
    ]
    assert keep_oldest(members).id == "mm"
    assert keep_oldest(list(reversed(members))).id == "mm"


# ---------------------------------------------------------------------------
# 4. The model-leg keep rule
# ---------------------------------------------------------------------------


def test_keep_rule_thresholds() -> None:
    created = {"a": 1.0, "b": 2.0}
    # exact boundaries: e1 >= tau_hi AND e1 - e2 >= tau_m
    assert decide_keep({"a": 0.90, "b": 0.40}, created, 0.90, 0.50)[0] == "keep"
    assert decide_keep({"a": 0.90, "b": 0.401}, created, 0.90, 0.50)[0] == "abstain"
    assert decide_keep({"a": 0.8999, "b": 0.10}, created, 0.90, 0.50)[0] == "abstain"
    assert decide_keep({"a": 0.99, "b": 0.49}, created, 0.90, 0.50)[0] == "keep"
    # an exact score tie can never keep (delta 0): the order stays
    # deterministic (sorted by id after the score/epoch keys), the
    # verdict abstains, and BOTH reported margins equal the tie score.
    verdict, keeper, e1, e2 = decide_keep({"b": 0.95, "a": 0.95}, {"b": 1.0, "a": 1.0}, 0.90, 0.50)
    assert (verdict, keeper) == ("abstain", None)
    assert (e1, e2) == (0.95, 0.95)


def test_small_cluster_refused(arbiter: MergeArbiter) -> None:
    with pytest.raises(MergeArbiterSchemaError, match="not arbitrable"):
        arbiter.arbitrate([_member("a", "solo")])


def test_duplicate_member_ids_refused(arbiter: MergeArbiter) -> None:
    with pytest.raises(MergeArbiterSchemaError, match="duplicate member ids"):
        arbiter.arbitrate([_member("a", "x"), _member("a", "y")])


def test_non_finite_features_never_reach_the_walk(
    arbiter: MergeArbiter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        MergeArbiter,
        "cluster_features",
        lambda self, members: np.full((len(members), 34), np.nan),
    )
    pair = [_member("a", "alpha"), _member("b", "bravo charlie")]
    with pytest.raises(MergeArbiterSchemaError, match="non-finite"):
        arbiter.arbitrate(pair)


# ---------------------------------------------------------------------------
# 5. Permutation invariance (bit-level, the ratified symmetry)
# ---------------------------------------------------------------------------


def test_member_rows_are_bit_identical_under_reversal(arbiter: MergeArbiter) -> None:
    cluster = [
        _member(f"m{i}", f"body content number {i} with some tail text", epoch=10.0 * i)
        for i in range(4)
    ]
    forward = arbiter.cluster_features(cluster)
    reversed_members = list(reversed(cluster))
    backward = arbiter.cluster_features(reversed_members)
    idx = {m.id: p for p, m in enumerate(cluster)}
    idx_r = {m.id: p for p, m in enumerate(reversed_members)}
    for m in cluster:
        assert np.array_equal(forward[idx[m.id]], backward[idx_r[m.id]]), m.id


def test_arbitration_is_order_invariant(arbiter: MergeArbiter) -> None:
    cluster = [
        _member("p1", "release checklist draft one", epoch=1.0),
        _member("p2", "release checklist draft one", epoch=2.0),
    ]
    v1 = arbiter.arbitrate(cluster)
    v2 = arbiter.arbitrate(list(reversed(cluster)))
    assert v1 == v2


def test_envelope_columns_carry_own_values_not_means(arbiter: MergeArbiter) -> None:
    members = [_member("a", "alpha", epoch=11.0), _member("b", "beta", epoch=22.0)]
    rows = arbiter.cluster_features(members)
    assert rows[0, IDX_EPOCH] == 11.0
    assert rows[1, IDX_EPOCH] == 22.0
    assert rows[0, IDX_NULL] == 0.0
    assert rows[0, IDX_ZEPS_SUM] >= 0.0  # mean of integer flags
    expected_cos = sum(
        x * y
        for x, y in zip(
            _norm_stub_embed(member_embed_text(members[0].view)),
            _norm_stub_embed(member_embed_text(members[1].view)),
            strict=True,
        )
    )
    assert float(rows[0, IDX_E_COS]) == pytest.approx(expected_cos)


def test_created_null_flag_rides_the_row(arbiter: MergeArbiter) -> None:
    members = [_member("a", "alpha", null=1.0), _member("b", "beta")]
    rows = arbiter.cluster_features(members)
    assert rows[0, IDX_NULL] == 1.0
    assert rows[1, IDX_NULL] == 0.0


def test_verdict_is_data_never_action() -> None:
    """The ArbiterVerdict carries no executor, no store handle, no
    callback — the §8.3 verdict/action split at the type level."""
    assert ArbiterVerdict.__dataclass_fields__.keys() == {
        "verdict",
        "leg",
        "ch_class",
        "keeper_id",
        "scores",
        "e1",
        "e2",
        "zone_hit",
    }
