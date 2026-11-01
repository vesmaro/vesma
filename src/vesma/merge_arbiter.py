"""The Choice-arbiter v2 (`vesma-cortex-merge-v2`) — member choice over a
duplicate cluster (W1-W3 engine wave, ADR-0033 native re-authoring).

The GCW integration package
(`vesma-cortex docs/experiments/merge-arbiter-v2-integration-package.md`)
is REFERENCE only; this module is the native vesma form of the ratified
contour. One primitive: ``keep ∈ {1..k} ∪ {ABSTAIN}`` over the members of
a cluster that ALREADY passed the is-dup gate (v42 untouched — the arbiter
is a separate contract and a separate pin, ADR-0002 R2 pin coupling).

Production contour (package §2 = prereg §5.1 + ADDENDUM-6, order fixed):

1. **CH-0** — byte-identity of bodies AND full metadata identity →
   keep-oldest tie-break, fold-eligible;
2. **CH-5** (¬CH-0) — the body difference is erased by norm1 →
   keep-oldest tie-break, fold-eligible;
3. **CH-2** — bodies and titles identical, metadata ``{tags, record_type,
   language}`` differs → REFUSAL (fold is FORBIDDEN);
4. **ZONE v1** — any unordered pair inside
   ``(cos ≤ 0.85 ∧ char5 ≤ 0.05) ∨ (zeps ≥ 3 ∧ char5 ≤ 0.20)`` → ABSTAIN.
   The zone is REFUSAL-ONLY: the keep side of cos/zeps is read nowhere
   (one-sidedness, ADDENDUM-6). Constants are frozen in the artifact.
5. **The model leg** for the remainder — LightGBM booster over 34 member
   features; ``keep = argmax e_i`` iff ``e(1) ≥ τ_hi`` and
   ``e(1) − e(2) ≥ τ_m``, else ABSTAIN. ABSTAIN means NO fold: a spare
   copy is cheap, a false fold is irreversible (error-cost asymmetry).

Feature contract (34, order frozen in the artifact — append-only): the 13
frozen pair features are the engine's own ``cortex_pair_features`` mirror
(imported, never re-implemented — the same discipline the cortex side
holds with ``cortex.features.pair``); the d/e/v blocks, ``e_cos`` and the
zeps block are re-authored here from the ratified reference. Member rows
are BIT-identical under any presentation permutation: per-other terms
accumulate in sorted-member-id order, and no feature reads a
serialization index.

Verify-before-load (the step1-root discipline): the bundled artifact is
accepted only when its sha256 equals the ratified pin
(``MERGE_ARTIFACT_SHA256``, TL-verified 2026-11-01), its declared feature
names equal the engine's frozen tuple, the zone block carries the
``zone-v1`` constants and the embedder pin equals the LIVE embedder
fingerprint (mismatch = recalibration event — the artifact was calibrated
on one embedding geometry and cannot silently run on another). Every
drift raises a typed :class:`MergeArbiterError`; the WIRING degrades
fail-open (the write path is never blocked) while the arbiter itself
refuses to run on unverified bytes.

The booster rides as the LightGBM v4 text dump embedded in the artifact
(``booster_model_string``). The engine evaluates it with the local dump
walker (:class:`LightGbmDump`) — zero new runtime dependencies (the V3
contract: Δdeps = 0); exact-equivalence against lightgbm itself is pinned
by the fixture test (tests/fixtures/merge_arbiter_lgbm_scores.jsonl,
generated with lightgbm 4.7.0 — see the fixture's provenance header).

The sanity exam of v2 is SPENT (single-shot ADOPT 2026-10-30, sealed
holdout): any recalibration is a NEW prereg cycle — out of this wave.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any, Final

import numpy as np

from vesma.decision_provider import (
    CORTEX_FEATURE_NAMES,
    CORTEX_MODEL_NAME,
    CanonRecordView,
    cortex_pair_features,
)

logger = logging.getLogger(__name__)

# ── Artifact identity (verify-before-load pins) ──────────────────────────────

#: Bundled artifact directory under ``vesma/models/`` — ships in the wheel
#: by the same mechanism as ``vesma-cortex-v1`` (package data).
MERGE_ARTIFACT_DIR: Final[str] = "vesma-cortex-merge-v2"

#: The artifact FILE name inside the bundle directory (a JSON document —
#: not the ONNX single-file shape of is-dup v42; the ratified v2 shape).
MERGE_ARTIFACT_FILE: Final[str] = "merge_arbiter_v2.json"

#: Ratified artifact sha256 — TL-verified 2026-11-01 against the cortex
#: line's frozen copy (ADOPT 2026-10-30). The loader refuses to parse any
#: other bytes (verify BEFORE json — the step1-root style).
MERGE_ARTIFACT_SHA256: Final[str] = (
    "60d1fe733937accf936776327f8f259fc2116aa6ffd011e81cd448a283bceefd"
)

#: Artifact identity string (the ``artifact`` field of the JSON document).
MERGE_MODEL_NAME: Final[str] = "vesma-cortex-merge-v2"

#: Size gate — the same self-containment discipline as the is-dup bundle
#: (inference-v1.md §4). The ratified v2 artifact is ~365 KB.
MERGE_MAX_ARTIFACT_BYTES: Final[int] = 5 * 1024 * 1024

#: The frozen ORDERED 34-feature contract of Choice v2 — field-for-field
#: mirror of the ratified artifact's ``feature_names`` (append-only, never
#: reorder). The 13 first entries are the frozen pair contract, identical
#: to :data:`vesma.decision_provider.CORTEX_FEATURE_NAMES`.
MERGE_FEATURE_NAMES: Final[tuple[str, ...]] = (
    # BLOCK P: frozen pair contract, mean-over-others (13)
    "cos_target",
    "char3_jaccard",
    "char4_jaccard",
    "char5_jaccard",
    "char3_containment",
    "char4_containment",
    "char5_containment",
    "tag_jaccard",
    "title_len_delta",
    "body_len_delta",
    "tag_count_delta",
    "type_match",
    "lang_match",
    # BLOCK D: directional text on the norm1 bodies (3)
    "d_other_in_mine",
    "d_mine_in_other",
    "d_rel_len",
    # BLOCK E: edit-region block on the norm2 machinery (6)
    "e_n_clusters",
    "e_chars_mine",
    "e_chars_other",
    "e_rel_chars",
    "e_digit_mine",
    "e_digit_other",
    # BLOCK V: envelope, created_at ISO-8601 UTC → epoch-sec + null flag (3)
    "v_created_epoch",
    "v_created_null",
    "v_created_delta_mean",
    # Choice v2 block (ADDENDUM-6): e_cos + zeps (9)
    "e_cos",
    "zeps_tok_body",
    "zeps_char5",
    "zeps_tok_title",
    "zeps_tags",
    "zeps_digits",
    "zeps_lang",
    "zeps_type",
    "zeps_sum",
)

#: Column indices inside the 34-wide row (pinned by the tuple above).
IDX_EPOCH: Final[int] = 22
IDX_NULL: Final[int] = 23
IDX_DELTA: Final[int] = 24
IDX_E_COS: Final[int] = 25
IDX_ZEPS_FLAGS: Final[int] = 26  # .. 32
IDX_ZEPS_SUM: Final[int] = 33

#: Number of mean-over-others terms that ride the pair vector (everything
#: before the own-value envelope columns).
N_PAIR_TERMS: Final[int] = 22

#: The frozen extractor's index of ``char5_jaccard`` — the axis the
#: RATIFIED zone-v1 rule and the zeps ``char5`` flag are defined on
#: (package §1/§2, framing §2.4). Read BY NAME, never by position: the
#: positional sibling ``char4_jaccard`` sits one index away and reading
#: it silently re-defines the ratified rule on the wrong axis (the
#: cascade-review F1 class — caught by the dedicated craft tests, which
#: separate the two axes).
CHAR5_JACCARD_IDX: Final[int] = CORTEX_FEATURE_NAMES.index("char5_jaccard")

#: The positional sibling — deliberately NOT read anywhere in the
#: contour; exported for the craft tests that pin the two axes apart.
CHAR4_JACCARD_IDX: Final[int] = CORTEX_FEATURE_NAMES.index("char4_jaccard")

#: The zone version literal the artifact must declare (constants frozen by
#: ratification; a different version is a NEW ratification, not a load).
MERGE_ZONE_VERSION: Final[str] = "zone-v1"


# ── Typed refusals (machine-parseable, own telemetry family per ADR-0002) ────


class MergeArbiterError(RuntimeError):
    """Base of the Choice-arbiter refusals.

    ``code`` is the machine-parseable latin ``MERGE-E-*`` string; the
    message STARTS with the code so a log line parses by prefix alone.
    The wiring is fail-open: any error logged with its code degrades THAT
    arbitration to a skip — ingest is never blocked. The LOAD-TIME codes
    additionally disable the arbiter for the process lifetime (the
    verify-before-load refusal).
    """

    code: str = "MERGE-E-LOAD"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")
        self.detail = detail


class MergeArbiterLoadError(MergeArbiterError):
    """The bundled artifact is missing, oversized or unreadable."""

    code = "MERGE-E-LOAD"


class MergeArbiterPinError(MergeArbiterError):
    """A pin mismatch: the artifact bytes are not the ratified sha256, or
    the ``embedder_pin`` differs from the LIVE embedder fingerprint
    (RECALIBRATION EVENT — never a silent re-point)."""

    code = "MERGE-E-PIN"


class MergeArbiterContractError(MergeArbiterError):
    """Structural contract drift: feature names, zone version/constants,
    thresholds, or a dump shape the walker cannot execute."""

    code = "MERGE-E-CONTRACT"


class MergeArbiterSchemaError(MergeArbiterError):
    """A request-time input violation: cluster too small, duplicate member
    ids, or a non-finite feature value (never clipped, never fed)."""

    code = "MERGE-E-SCHEMA"


class MergeArbiterInferError(MergeArbiterError):
    """The dump walk failed or the score did not decode to [0, 1]."""

    code = "MERGE-E-INFER"


class MergeArbiterGateError(MergeArbiterError):
    """The ``apply``-mode activation gate refused: the cluster's silver
    is-dup gate was NOT answered by the v42 leg (the answering provider's
    identity differs from :data:`MERGE_APPLY_GATE_PROVIDER` — e.g. the
    deterministic step rule, a degraded seam, or a foreign provider).
    Folding behind a foreign gate is FORBIDDEN by construction: the
    ratified composition is v42-gate → arbiter, and the template-twin
    class (live-store verification, 2026-11-01) is exactly what the v42
    leg refuses and the bare cosine rule admits. Advisory stays
    unaffected — this code refuses the FOLD only."""

    code = "MERGE-E-GATE"


#: The ONLY silver gate an ``apply``-mode fold may ride: the is-dup
#: verdict must come from the bundled v42 provider (its ``name``
#: identity). This is the code-level form of the ratification package's
#: «the arbiter receives a cluster ONLY after the silver is-dup gate» —
#: advisory runs on any provider, apply does not.
MERGE_APPLY_GATE_PROVIDER: Final[str] = CORTEX_MODEL_NAME


# ── Member input ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ArbiterMember:
    """One cluster member prepared for arbitration.

    ``view`` is the prepared :class:`CanonRecordView` (the SAME projection
    the decision seam consumes — envelope language/record_type only, no
    raw metadata). ``created_epoch``/``created_null`` carry the envelope
    A-null-pin: ISO-8601 UTC → epoch seconds plus the null flag (1.0 when
    the record carries no usable creation stamp — engine rows always do;
    the flag keeps the contract shape faithful).
    """

    id: str
    view: CanonRecordView
    created_epoch: float
    created_null: float = 0.0


@dataclass(frozen=True)
class ArbiterVerdict:
    """The arbiter's answer over one cluster — data, not action.

    ``verdict`` is ``keep`` or ``abstain``; ``leg`` names the §2 ladder
    leg that produced it (``ch0-tie-break``, ``ch5-tie-break``,
    ``ch2-refusal``, ``zone-v1``, ``model``). ``keeper_id`` is set only
    on ``keep``. ``scores`` carries the model leg's per-member eligibility
    scores (empty on the code legs). ``zone_hit`` carries the firing
    pair's ids and metrics on the zone leg. The verdict NEVER executes a
    system action by itself (policy §8.3 — the consolidation executor
    owns actions).
    """

    verdict: str
    leg: str
    ch_class: str
    keeper_id: str | None = None
    scores: dict[str, float] = field(default_factory=dict)
    e1: float | None = None
    e2: float | None = None
    zone_hit: dict[str, Any] | None = None


# ── The pinned conventions (exact mirrors of the ratified reference) ─────────


#: norm1 — lower + whitespace-collapse (the release/6.6.0 convention the
#: artifact's ``convention.embed_text`` pins).
def _norm1(s: str) -> str:
    return " ".join((s or "").split()).lower()


def _norm1_body(body: str) -> str:
    """B(r) = norm1(body) — the silver normalization surface. NOTE: the
    body-ONLY normalizer (no title) — deliberately different from the
    pair contract's title+body text; the d/e blocks read this surface."""
    return _norm1(body)


#: Mechanical pin of the norm2 hull clustering: non-equal opcodes within
#: a GAP-char run of equal text merge into ONE edit cluster (difflib
#: fragments a contiguous phrase edit around shared letters).
_NORM2_GAP: Final[int] = 12

#: digit tokens on norm1 bodies (the zeps digits flag + e_digit fractions).
_DIG_RE: Final[re.Pattern[str]] = re.compile(r"\d+(?:[.,:]\d+)*")

#: Embedding-text cap of the pinned convention.
_EMBED_TEXT_MAX_CHARS: Final[int] = 4096


def member_embed_text(view: CanonRecordView) -> str:
    """The pinned embed text: ``n1(title)\\nn1(body)\\nn1(space-joined
    tags)``, truncated at 4096 — the artifact's ``convention.embed_text``.
    Deliberately NOT the engine's search-text shape: the zone pair cos and
    e_cos measure THIS text only (two different conventions in one contour
    are forbidden by the ratification)."""
    tags_text = _norm1(" ".join(view.tags))
    return f"{_norm1(view.title)}\n{_norm1(view.body)}\n{tags_text}"[:_EMBED_TEXT_MAX_CHARS]


def _digset(body: str) -> frozenset[str]:
    return frozenset(_DIG_RE.findall(_norm1(body)))


def _token_set(s: str) -> frozenset[str]:
    return frozenset(_norm1(s).split())


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _digit_frac(text: str) -> float:
    if not text:
        return 0.0
    return sum(ch.isdigit() for ch in text) / len(text)


def _norm2_edit_clusters(body_a: str, body_b: str) -> list[tuple[int, int, int, int]]:
    """Clustered edit regions (hulls) of the norm2 delta between two
    norm1 bodies: non-equal ``difflib`` opcodes, merged while the equal
    run between them stays ≤ ``_NORM2_GAP`` chars on BOTH compared sides."""
    sm = SequenceMatcher(None, _norm1_body(body_a), _norm1_body(body_b), autojunk=False)
    clusters: list[list[int]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if clusters and i1 - clusters[-1][1] <= _NORM2_GAP and j1 - clusters[-1][3] <= _NORM2_GAP:
            c = clusters[-1]
            c[0], c[1] = min(c[0], i1), max(c[1], i2)
            c[2], c[3] = min(c[2], j1), max(c[3], j2)
        else:
            clusters.append([i1, i2, j1, j2])
    return [(c[0], c[1], c[2], c[3]) for c in clusters]


def _side_dict(view: CanonRecordView) -> dict[str, Any]:
    """The zeps pair shape: plain side fields (the reference's
    ``_side_dict``; language/record_type stay ``None``-able)."""
    return {
        "title": view.title,
        "body": view.body,
        "tags": list(view.tags),
        "language": view.language,
        "record_type": view.record_type,
    }


# ── The LightGBM v4 text-dump walker ─────────────────────────────────────────


@dataclass(frozen=True)
class LightGbmDump:
    """A parsed LightGBM ``booster_model_string`` (format v4) executable
    without the lightgbm runtime (Δdeps = 0, the V3 artifact contract).

    Scope guard: this walker executes exactly the dump shape the ratified
    v2 artifact carries — ``objective=binary sigmoid:1``, every split
    numerical (``decision_type=2`` — value ≤ threshold → left), zero
    categorical splits. Anything else is a contract refusal, not a
    best-effort interpretation. Missing-value routing is unreachable by
    construction: every feature row is validated finite before the walk
    (MERGE-E-SCHEMA), matching the training-side float64 contract."""

    num_feature: int
    sigmoid: float
    trees: tuple[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray], ...]
    #: per tree: (split_feature int32, threshold float64,
    #:            children int32 [left|right packed: child ≥ 0 internal,
    #:            negative −k → leaf k−1], leaf_value float64)

    @classmethod
    def parse(cls, model_string: str, expected_features: int) -> LightGbmDump:
        """Parse and structurally verify a v4 dump; contract drift refuses
        loud (:class:`MergeArbiterContractError`)."""
        header_obj = ""
        num_feature = -1
        objective = ""
        trees_raw: list[dict[str, list[str]]] = []
        current: dict[str, list[str]] | None = None
        for line in model_string.splitlines():
            if line.startswith("Tree="):
                current = {}
                trees_raw.append(current)
                continue
            if current is not None:
                key, sep, value = line.partition("=")
                if not sep:
                    continue  # blank separator line between trees
                current.setdefault(key, [value])
                continue
            header_obj += line + "\n"
            if line.startswith("max_feature_idx="):
                num_feature = int(line.split("=", 1)[1]) + 1
            elif line.startswith("objective="):
                objective = line.split("=", 1)[1]
        if num_feature != expected_features:
            raise MergeArbiterContractError(
                f"dump expects {num_feature} features, the frozen contract pins "
                f"{expected_features} (wrapper/graph desync)"
            )
        if not trees_raw:
            raise MergeArbiterContractError("dump carries no trees")

        match = re.fullmatch(r"binary sigmoid:([0-9.eE+-]+)", objective)
        if not match:
            raise MergeArbiterContractError(
                f"objective {objective!r} is not 'binary sigmoid:<s>' — the walker "
                "executes only the ratified binary objective shape"
            )
        sigmoid = float(match.group(1))
        if not (sigmoid > 0.0):
            raise MergeArbiterContractError(f"sigmoid {sigmoid!r} is not positive")

        trees: list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = []
        for idx, raw in enumerate(trees_raw):
            for required in (
                "num_leaves",
                "num_cat",
                "split_feature",
                "threshold",
                "decision_type",
                "left_child",
                "right_child",
                "leaf_value",
            ):
                if required not in raw:
                    raise MergeArbiterContractError(f"tree {idx}: missing dump key {required!r}")
            num_leaves = int(raw["num_leaves"][0])
            if int(raw["num_cat"][0]) != 0:
                raise MergeArbiterContractError(
                    f"tree {idx}: categorical splits (num_cat="
                    f"{raw['num_cat'][0]}) are outside the ratified shape"
                )
            decision_types = {v for entry in raw["decision_type"] for v in entry.split()}
            if decision_types != {"2"}:
                raise MergeArbiterContractError(
                    f"tree {idx}: decision_type set {sorted(decision_types)} != {{'2'}} "
                    "(only numerical less-or-equal splits are executable)"
                )
            split_feature = np.array(
                [int(v) for entry in raw["split_feature"] for v in entry.split()],
                dtype=np.int32,
            )
            threshold = np.array(
                [float(v) for entry in raw["threshold"] for v in entry.split()],
                dtype=np.float64,
            )
            children = np.array(
                [int(v) for entry in raw["left_child"] + raw["right_child"] for v in entry.split()],
                dtype=np.int32,
            ).reshape(2, -1)
            leaf_value = np.array(
                [float(v) for entry in raw["leaf_value"] for v in entry.split()],
                dtype=np.float64,
            )
            n_internal = num_leaves - 1
            if not (len(split_feature) == len(threshold) == n_internal):
                raise MergeArbiterContractError(
                    f"tree {idx}: {len(split_feature)} split features / "
                    f"{len(threshold)} thresholds for {n_internal} internal nodes"
                )
            if len(leaf_value) != num_leaves:
                raise MergeArbiterContractError(
                    f"tree {idx}: {len(leaf_value)} leaf values for {num_leaves} leaves"
                )
            if children.min() < -num_leaves:
                raise MergeArbiterContractError(
                    f"tree {idx}: child reference {-children.min()} exceeds the "
                    f"leaf count {num_leaves}"
                )
            trees.append((split_feature, threshold, children, leaf_value))
        return cls(num_feature=num_feature, sigmoid=sigmoid, trees=tuple(trees))

    def raw_score(self, row: np.ndarray) -> float:
        """Sum the leaf values of one walk per tree (float64, the
        training-side accumulation order)."""
        total = 0.0
        for split_feature, threshold, children, leaf_value in self.trees:
            node = 0
            while node >= 0:
                go_left = row[split_feature[node]] <= threshold[node]
                node = int(children[0 if go_left else 1, node])
            total += leaf_value[-node - 1]
        return total

    def predict_proba(self, row: np.ndarray) -> float:
        """Binary objective → P(positive) = sigmoid(raw). Numerically
        stable form; a non-finite result is an INFER refusal (no clip)."""
        raw = self.raw_score(row)
        if raw >= 0:
            probability = 1.0 / (1.0 + np.exp(-raw * self.sigmoid))
        else:
            exp_raw = np.exp(raw * self.sigmoid)
            probability = exp_raw / (1.0 + exp_raw)
        probability = float(probability)
        if np.isnan(probability) or probability < 0.0 or probability > 1.0:
            raise MergeArbiterInferError(
                f"score {probability!r} outside [0, 1] — decode error, no clip"
            )
        return probability


# ── The ladder (§2 order fixed) ───────────────────────────────────────────────


def classify_cluster(members: Sequence[ArbiterMember]) -> str:
    """The deterministic CH-class of the cluster (predicates, not words).

    Order is part of the ratified semantics: CH-0 first, CH-5 (¬CH-0),
    then CH-2. ``CH-3``/``CH-4`` need generator declarations — no
    production fact extractor exists, so a production cluster that passes
    all three predicates below classifies ``undefined`` and rides the
    zone/model legs (the reference behaves identically).
    """
    body_shas = {hashlib.sha256(m.view.body.encode("utf-8")).hexdigest() for m in members}
    titles = {m.view.title for m in members}
    tag_sets = {frozenset(m.view.tags) for m in members}
    record_types = {m.view.record_type for m in members}
    languages = {m.view.language for m in members}
    if (
        len(body_shas) == 1
        and len(titles) == 1
        and len(tag_sets) == 1
        and len(record_types) == 1
        and len(languages) == 1
    ):
        return "CH-0"
    norm_bodies = {_norm1_body(m.view.body) for m in members}
    if len(body_shas) > 1 and len(norm_bodies) == 1:
        return "CH-5"
    if (
        len(norm_bodies) == 1
        and len(titles) == 1
        and (len(tag_sets) > 1 or len(record_types) > 1 or len(languages) > 1)
    ):
        return "CH-2"
    return "undefined"


def keep_oldest(members: Sequence[ArbiterMember]) -> ArbiterMember:
    """The tie-break member: oldest creation stamp, then smallest id —
    never the serialization order."""
    return min(members, key=lambda m: (m.created_epoch, m.id))


def zeps_flags(
    side_a: dict[str, Any], side_b: dict[str, Any], eps: float, char5: float
) -> tuple[int, ...]:
    """The 7 eps-flags of the ratified framing (§2.4). ``char5`` arrives
    precomputed from the FROZEN pair extractor (its value does not depend
    on the measured-similarity argument, so the precomputation is exact)."""
    return (
        int(_jaccard(_token_set(side_a["body"]), _token_set(side_b["body"])) <= eps),
        int(char5 <= eps),
        int(len(_token_set(side_a["title"]) & _token_set(side_b["title"])) <= eps),
        int(_jaccard(frozenset(side_a["tags"]), frozenset(side_b["tags"])) <= eps),
        int(len(_digset(side_a["body"]) & _digset(side_b["body"])) == 0),
        int(side_a["language"] != side_b["language"]),
        int(side_a["record_type"] != side_b["record_type"]),
    )


def decide_keep(
    scores: dict[str, float],
    created: dict[str, float],
    tau_hi: float,
    tau_m: float,
) -> tuple[str, str | None, float, float]:
    """The model-leg rule: keep = argmax e_i iff e(1) ≥ τ_hi and
    e(1) − e(2) ≥ τ_m; else ABSTAIN. Exact-score ties break
    order-invariantly: oldest creation stamp, then smallest id — never
    the serialization index. e(2) is the runner-up among the NON-argmax
    members (a 1-member score map is structurally impossible — the
    cluster gate refuses k < 2 before the model leg)."""
    order = sorted(scores, key=lambda m: (-scores[m], created[m], m))
    top = order[0]
    second = order[1]
    e1, e2 = scores[top], scores[second]
    if e1 >= tau_hi and (e1 - e2) >= tau_m:
        return "keep", top, e1, e2
    return "abstain", None, e1, e2


# ── The arbiter ───────────────────────────────────────────────────────────────


def assert_cortex_merge_embedder_pin(pin: str, live_fingerprint: str) -> None:
    """The artifact's ``embedder_pin`` vs the LIVE embedder vintage
    (the VesmaProvider discipline). A mismatch is the loud recalibration
    refusal :class:`MergeArbiterPinError`, never a graceful degradation
    by itself."""
    if not live_fingerprint:
        raise MergeArbiterPinError(
            "embedder_pin unassertable — the live embedder fingerprint is empty"
        )
    if pin != live_fingerprint:
        raise MergeArbiterPinError(
            "embedder_pin mismatch — recalibration event, the merge-v2 "
            f"artifact was calibrated on {pin!r} but the live embedder "
            f"fingerprint is {live_fingerprint!r}"
        )


class MergeArbiter:
    """The bundled ``vesma-cortex-merge-v2`` Choice-arbiter.

    Eager load in the constructor (the VesmaProvider discipline): the
    constructor either raises a typed :class:`MergeArbiterError` or leaves
    an arbiter that already passed verify-before-load AND one smoke
    scoring. ``embed`` is the LIVE nano embed callable (the manager wires
    ``EmbeddingProvider.embed``); the embedder pin was asserted against
    the live fingerprint at construction, so the dot products measure the
    pinned geometry. No network anywhere: the module imports nothing that
    talks (the cortex network-isolation discipline).
    """

    name: Final[str] = MERGE_MODEL_NAME

    def __init__(
        self,
        embed: Callable[[str], Sequence[float]],
        embedder_fingerprint: str,
    ) -> None:
        """``embed`` is the LIVE nano embed callable; ``embedder_fingerprint``
        is the live embedder vintage the artifact pin is asserted against
        (e.g. ``config_fingerprint(EmbeddingConfig())``). An empty
        fingerprint fails the pin assert — an unassertable pin is a
        refusal, not an assumption."""
        self._embed = embed
        self._text_cache: dict[str, list[float]] = {}
        self._load(embedder_fingerprint)

    # ── verify-before-load ────────────────────────────────────────────────
    def _load(self, embedder_fingerprint: str) -> None:
        bundle = resource_files("vesma") / "models" / MERGE_ARTIFACT_DIR
        artifact_path = Path(str(bundle / MERGE_ARTIFACT_FILE))
        if not artifact_path.is_file():
            raise MergeArbiterLoadError(
                f"bundled artifact {MERGE_ARTIFACT_DIR!r} is missing "
                f"{MERGE_ARTIFACT_FILE} (looked at {artifact_path!s})"
            )
        size = artifact_path.stat().st_size
        if size > MERGE_MAX_ARTIFACT_BYTES:
            raise MergeArbiterLoadError(
                f"artifact is {size} bytes > gate {MERGE_MAX_ARTIFACT_BYTES}"
            )
        raw = artifact_path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != MERGE_ARTIFACT_SHA256:
            raise MergeArbiterPinError(
                f"artifact sha256 {digest!r} != ratified pin "
                f"{MERGE_ARTIFACT_SHA256!r} — the bytes are not the ADOPT "
                "verdict's artifact (recalibration-class refusal, never a "
                "silent re-point)"
            )
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MergeArbiterLoadError(f"artifact JSON unreadable: {exc}") from exc

        identity = str(doc.get("artifact", ""))
        if not identity.startswith(MERGE_MODEL_NAME):
            raise MergeArbiterContractError(
                f"artifact identity {identity!r} does not declare {MERGE_MODEL_NAME!r}"
            )
        feature_names = tuple(str(n) for n in doc.get("feature_names", ()))
        if feature_names != MERGE_FEATURE_NAMES:
            raise MergeArbiterContractError(
                f"feature contract drift: artifact pins {len(feature_names)} names, "
                f"wrapper pins {len(MERGE_FEATURE_NAMES)} (append-only contract)"
            )

        thresholds = doc.get("thresholds") or {}
        try:
            self.tau_hi = float(thresholds["tau_hi"])
            self.tau_m = float(thresholds["tau_m"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MergeArbiterContractError(f"thresholds unreadable: {exc}") from exc
        if not (0.0 < self.tau_m <= self.tau_hi <= 1.0):
            raise MergeArbiterContractError(
                f"thresholds out of range: tau_hi={self.tau_hi} tau_m={self.tau_m}"
            )

        self.similarity_constant = float(doc.get("similarity_constant", 1.0))

        zone = doc.get("zone_v1") or {}
        if str(zone.get("constants", {}).get("version", "")) != MERGE_ZONE_VERSION:
            raise MergeArbiterContractError(
                f"zone version {zone.get('constants', {}).get('version')!r} != "
                f"{MERGE_ZONE_VERSION!r} (a different zone is a NEW ratification)"
            )
        try:
            constants = zone["constants"]
            self.zone_tau_cos = float(constants["tau_cos"])
            self.zone_tau_lex = float(constants["tau_lex"])
            self.zone_tau_lex2 = float(constants["tau_lex2"])
            self.zone_zeps_min = int(constants["zeps_min"])
            self.zone_eps = float(constants["eps"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MergeArbiterContractError(f"zone constants unreadable: {exc}") from exc

        pins = doc.get("pins") or {}
        self.artifact_embedder_pin = str(pins.get("embedder_pin", ""))
        self.corpus_fingerprint = str((doc.get("corpus") or {}).get("corpus_fp_v2", ""))
        self.weights_sha256 = digest
        assert_cortex_merge_embedder_pin(self.artifact_embedder_pin, embedder_fingerprint)

        dump_string = doc.get("booster_model_string")
        if not isinstance(dump_string, str) or not dump_string:
            raise MergeArbiterContractError("artifact carries no booster_model_string")
        self._dump = LightGbmDump.parse(dump_string, len(MERGE_FEATURE_NAMES))

        self._smoke_score()
        logger.info(
            "vesma-cortex-merge ready: %s (features=%d, trees=%d, sha256=%s…, "
            "tau_hi=%s tau_m=%s, zone=%s)",
            MERGE_ARTIFACT_DIR,
            len(MERGE_FEATURE_NAMES),
            len(self._dump.trees),
            digest[:12],
            self.tau_hi,
            self.tau_m,
            MERGE_ZONE_VERSION,
        )

    def assert_embedder_pin(self, live_fingerprint: str) -> None:
        """The artifact's ``embedder_pin`` vs the LIVE embedder vintage.
        Kept as a public probe for the doctor/tests; the constructor
        already enforced the same assert at load."""
        assert_cortex_merge_embedder_pin(self.artifact_embedder_pin, live_fingerprint)

    def _smoke_score(self) -> None:
        """Startup smoke: one zeros row must decode to a finite [0, 1]
        probability (the shape/type/range check)."""
        probability = self._dump.predict_proba(np.zeros(len(MERGE_FEATURE_NAMES), dtype=np.float64))
        logger.debug("vesma-cortex-merge smoke probability=%.6f", probability)

    # ── embed leg (pinned convention + per-call cache) ────────────────────
    def _member_vector(self, view: CanonRecordView) -> list[float]:
        text = member_embed_text(view)
        vec = self._text_cache.get(text)
        if vec is None:
            vec = [float(x) for x in self._embed(text)]
            self._text_cache[text] = vec
        return vec

    def _cos(self, a: list[float], b: list[float]) -> float:
        # strict: a dimension mismatch is broken embedder geometry —
        # refuse loud, never silently truncate the dot product.
        return sum(x * y for x, y in zip(a, b, strict=True))

    # ── features (member-level, permutation-invariant) ────────────────────
    def _pair_member_terms(self, mi: ArbiterMember, mj: ArbiterMember) -> tuple[list[float], float]:
        """Per-pair terms of member i against member j: the 22-value vector
        (the 13 frozen pair features — reused, never re-implemented — plus
        the d/e blocks on the body surfaces) AND the pair's ``char5``
        value for the zeps leg.

        ``char5`` rides along from the SAME frozen 13-vector: the frozen
        extractor computes it from the n-gram sets alone — the
        measured-similarity argument feeds ``cos_target`` only — so the
        value is EXACTLY the one the ratified zone/zeps convention reads
        (a second extractor call per pair would return the identical
        number). Read BY NAME via :data:`CHAR5_JACCARD_IDX`, never by
        position."""
        values = list(cortex_pair_features(mi.view, mj.view, similarity=self.similarity_constant))
        char5 = values[CHAR5_JACCARD_IDX]

        bi = _norm1_body(mi.view.body)
        bj = _norm1_body(mj.view.body)
        values.append(1.0 if bj in bi else 0.0)  # d_other_in_mine
        values.append(1.0 if bi in bj else 0.0)  # d_mine_in_other
        li, lj = len(bi), len(bj)
        values.append((li - lj) / (li + lj) if (li + lj) else 0.0)  # d_rel_len

        hulls = _norm2_edit_clusters(mi.view.body, mj.view.body)
        values.append(float(len(hulls)))  # e_n_clusters
        mine = other = 0
        mine_txt = other_txt = ""
        for c1, c2, d1, d2 in hulls:
            mine += c2 - c1
            other += d2 - d1
            mine_txt += bi[c1:c2]
            other_txt += bj[d1:d2]
        values.append(float(mine))  # e_chars_mine
        values.append(float(other))  # e_chars_other
        values.append((mine - other) / (mine + other + 1e-9))  # e_rel_chars
        values.append(_digit_frac(mine_txt))  # e_digit_mine
        values.append(_digit_frac(other_txt))  # e_digit_other
        return values, char5

    def cluster_features(self, members: Sequence[ArbiterMember]) -> np.ndarray:
        """The (k, 34) member feature matrix — permutation-invariant by
        construction. Every row is a function of (the member SET, that
        member) only; per-other terms accumulate in SORTED-id order, so a
        member's row is BIT-identical under any presentation permutation
        (no float reassociation drift)."""
        k = len(members)
        width = len(MERGE_FEATURE_NAMES)
        order = sorted(range(k), key=lambda p: members[p].id)
        vectors = [self._member_vector(m.view) for m in members]
        sides = [_side_dict(m.view) for m in members]
        rows: np.ndarray[Any, Any] = np.zeros((k, width), dtype=np.float64)
        for i in range(k):
            acc: np.ndarray[Any, Any] = np.zeros(width, dtype=np.float64)
            for j in order:
                if j == i:
                    continue
                pair_terms, char5 = self._pair_member_terms(members[i], members[j])
                acc[:N_PAIR_TERMS] += pair_terms
                acc[IDX_DELTA] += members[i].created_epoch - members[j].created_epoch
                acc[IDX_E_COS] += self._cos(vectors[i], vectors[j])
                flags = zeps_flags(sides[i], sides[j], self.zone_eps, char5)
                for f, v in enumerate(flags):
                    acc[IDX_ZEPS_FLAGS + f] += float(v)
                acc[IDX_ZEPS_SUM] += float(sum(flags))
            n_others = k - 1
            acc[:N_PAIR_TERMS] /= n_others
            acc[IDX_DELTA] /= n_others
            acc[IDX_EPOCH] = members[i].created_epoch
            acc[IDX_NULL] = members[i].created_null
            acc[IDX_E_COS] /= n_others
            acc[IDX_ZEPS_FLAGS : IDX_ZEPS_FLAGS + 7] /= n_others
            acc[IDX_ZEPS_SUM] /= n_others
            rows[i] = acc
        return rows

    # ── the contour ────────────────────────────────────────────────────────
    def arbitrate(self, members: Sequence[ArbiterMember]) -> ArbiterVerdict:
        """Run the §2 contour over one cluster — data, never action."""
        ids = [m.id for m in members]
        if len(members) < 2:
            raise MergeArbiterSchemaError(
                f"a cluster of {len(members)} member(s) is not arbitrable (k ≥ 2)"
            )
        if len(set(ids)) != len(ids):
            raise MergeArbiterSchemaError(f"cluster carries duplicate member ids: {sorted(ids)}")

        ch_class = classify_cluster(members)
        if ch_class == "CH-0":
            keeper = keep_oldest(members)
            return ArbiterVerdict(
                verdict="keep", leg="ch0-tie-break", ch_class=ch_class, keeper_id=keeper.id
            )
        if ch_class == "CH-5":
            keeper = keep_oldest(members)
            return ArbiterVerdict(
                verdict="keep", leg="ch5-tie-break", ch_class=ch_class, keeper_id=keeper.id
            )
        if ch_class == "CH-2":
            return ArbiterVerdict(verdict="abstain", leg="ch2-refusal", ch_class=ch_class)

        zone_hit = self._zone_hit(members)
        if zone_hit is not None:
            return ArbiterVerdict(
                verdict="abstain",
                leg="zone-v1",
                ch_class=ch_class,
                zone_hit=zone_hit,
            )

        rows = self.cluster_features(members)
        if not np.isfinite(rows).all():
            raise MergeArbiterSchemaError(
                "feature matrix carries a non-finite value — refusing the walk "
                "(never clipped, never fed)"
            )
        scores = {m.id: self._dump.predict_proba(rows[p]) for p, m in enumerate(members)}
        created = {m.id: m.created_epoch for m in members}
        verdict, keeper_id, e1, e2 = decide_keep(scores, created, self.tau_hi, self.tau_m)
        return ArbiterVerdict(
            verdict=verdict,
            leg="model",
            ch_class=ch_class,
            keeper_id=keeper_id,
            scores={k: round(v, 6) for k, v in scores.items()},
            e1=round(e1, 6),
            e2=round(e2, 6),
        )

    def _zone_hit(self, members: Sequence[ArbiterMember]) -> dict[str, Any] | None:
        """Any unordered pair inside zone-v1 fires the refusal leg. The
        zone is ONE-SIDED: only the low sides of cos/char5/zeps are read;
        the keep side of any axis is read nowhere."""
        sides = [_side_dict(m.view) for m in members]
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                vi, vj = self._member_vector(members[i].view), self._member_vector(members[j].view)
                cos = self._cos(vi, vj)
                char5 = cortex_pair_features(members[i].view, members[j].view, similarity=0.0)[
                    CHAR5_JACCARD_IDX
                ]
                zeps = sum(zeps_flags(sides[i], sides[j], self.zone_eps, char5))
                if cos <= self.zone_tau_cos and char5 <= self.zone_tau_lex:
                    return {
                        "pair": [members[i].id, members[j].id],
                        "zone_leg": "cos",
                        "cos": round(cos, 6),
                        "char5": round(char5, 6),
                        "zeps": int(zeps),
                    }
                if zeps >= self.zone_zeps_min and char5 <= self.zone_tau_lex2:
                    return {
                        "pair": [members[i].id, members[j].id],
                        "zone_leg": "zeps",
                        "cos": round(cos, 6),
                        "char5": round(char5, 6),
                        "zeps": int(zeps),
                    }
        return None
