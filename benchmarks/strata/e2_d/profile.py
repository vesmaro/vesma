"""E2 D-strata profile — fingerprint, counts, fixed parameters.

Same discipline as the e2_gov profile (E0 §3.3: "Corpus fingerprint
(seed, version, class counts) committed with E2; any corpus or
issuance-path change → event-driven re-baseline in the same PR"):
sha256 over the stratum-defining module BYTES in fixed order, committed
as ``profile.json`` next to the modules, pinned by test — any edit to
a stratum module without re-recording the profile fails the suite.

Scope statement (the §3.3 combined-profile discipline, resolved for
this wave): the D-strata are SCENARIO artifacts, not corpus entries —
E0 §3.3 extends distribution matching to the corpus extension and the
§3.4 multi-session scenario stores; §3.6 sizes the awareness strata as
pairs/claims/canaries with no distribution pin. This package therefore
does NOT touch the e2_gov combined corpus or its profile, and the S1
stand's measured corpus stays byte-identical (both asserted by test).
The one place the drowning condition is reproduced at registered
scale-language is the #224 replay store, which mirrors the live 58% ±
2 pp checkpoint share at scenario scale (57.9%).

Fixed parameters (E0-unspecified, fixed by these artifacts, REPORTED
to the orchestrator — not amendments): see
``FIXED_PARAMETERS`` below; each names its module and value.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from benchmarks.strata.e2_d import adversarial as adversarial_mod
from benchmarks.strata.e2_d import canaries as canaries_mod
from benchmarks.strata.e2_d import conflict_pairs as conflict_pairs_mod
from benchmarks.strata.e2_d import ground_truth as ground_truth_mod
from benchmarks.strata.e2_d import materialize as materialize_mod
from benchmarks.strata.e2_d import oracle as oracle_mod
from benchmarks.strata.e2_d import replay224 as replay224_mod
from benchmarks.strata.e2_d import scenarios as scenarios_mod
from benchmarks.strata.e2_d import stale_claims as stale_claims_mod

#: Bumped when the stratum's shape semantics change (a bump invalidates
#: the recorded profile and the pin test fails — never silent). d-2:
#: type-2 raised 40 -> 80 per the E0 §3.6 raise rule (TL decision
#: 2026-09-13, E0 §8 pre-run revision 6) — counts changed, generator
#: protocol unchanged.
STRATUM_VERSION = "e2-d-2"

PROFILE_PATH = Path(__file__).resolve().parent / "profile.json"

#: E0 §3.3 registered distribution pin (live store: 839 of 1457 = 58%).
CHECKPOINT_SHARE_TARGET = 0.58
CHECKPOINT_SHARE_TOLERANCE = 0.02

#: E0 §8 rev. 2 registered engine parameter in force for every D batch.
CONFLICT_HINT_THRESHOLD_IN_FORCE = 2

#: E0-unspecified parameters fixed by this artifact wave. Reported to
#: the orchestrator and registered pre-run in E0 §8 rev. 4 (measurement
#: instruments); E0 §1-§7 are NOT amended here.
FIXED_PARAMETERS: dict[str, str] = {
    "stratum_nature": (
        "D-strata are scenario artifacts (per-scenario stores), not corpus "
        "entries: the e2_gov combined corpus and profile are untouched and "
        "S1 stays byte-identical"
    ),
    "conflict_type_split": (
        "80 type-2 + 40 type-1 — the type-2 share raised from the "
        "floor-exact 40 by the E0 §3.6 raise rule (TL decision "
        "2026-09-13, E0 §8 pre-run revision 6); type-1 unchanged at 40 "
        "(the E0 floor is >=40 type-2, remainder type-1; total 120, "
        "n reported)"
    ),
    "conflict_type1_hint_policy": (
        "type-1 peers also carry lexically overlapping goals (uniform hint "
        "contact); the type split fixes where the claim is discoverable, "
        "not whether a goal exists"
    ),
    "conflict_store_mass": (
        "per conflict-pair store: exactly 3 checkpoints (actor + peer + an "
        "always-present bystander) + 1 evidence row (type-1 only) + 6 noise "
        "rows (conflict_pairs.py)"
    ),
    "action_menu_shape": (
        "2 colliding + 2 safe actions per conflict pair and in the #224 "
        "replay; 4 safe (0 colliding) per stale-claim and adversarial "
        "scenario — the menu size that scales the intrusion rate"
    ),
    "stale_mechanism_split": (
        "20 window_expired (5400 s) + 20 superseded_goal (3000 s stale / 150 s current)"
    ),
    "canary_channel_split": "140 generic-add (7 classes x 20) + 60 checkpoint (3 classes x 20)",
    "replay_flood_size": (
        "216 flood rows + 5 scenario rows = 221 total, 128 checkpoints = "
        "57.9% (the E0 §3.3 58% +-2pp live profile at scenario scale; E0 "
        "cites the live 839 without fixing a scenario size)"
    ),
    "oracle_token_system": (
        "claimed-zone artifact stems vs action targets, exact set "
        "intersection (oracle.py); lexical overlap is the engine's hint "
        "layer, never the judgment layer"
    ),
    "view_neutrality": (
        "agent-facing ids are neutral tokens (ds-hash / a1..aN shuffled / "
        "r1..rN by age; materialize mints neutral dm-hash store ids) — no "
        "type tag, oracle prefix, or experimenter label reaches a "
        "model-facing surface (review P1); pinned by test"
    ),
    "goal_charset": (
        "all scenario goals are single-line ASCII (the engine tokenizer is "
        "ASCII-blind — E0 §8 rev. 2); pinned by test"
    ),
}


def _stratum_modules() -> tuple[Any, ...]:
    return (
        scenarios_mod,
        conflict_pairs_mod,
        stale_claims_mod,
        canaries_mod,
        adversarial_mod,
        replay224_mod,
        oracle_mod,
        ground_truth_mod,
        # materialize joined the fingerprint set with the review-P1 fix
        # (neutral store ids): it shapes what a model-facing surface
        # renders, so a silent edit must trip the pin like any other
        # stratum module (review P3-c).
        materialize_mod,
    )


def corpus_fingerprint() -> str:
    """sha256 over the stratum-defining module bytes (fixed order)."""
    digest = hashlib.sha256()
    for module in _stratum_modules():
        assert module.__file__ is not None
        path = Path(module.__file__)
        digest.update(path.name.encode())
        digest.update(b"\x00")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _replay_row_counts() -> dict[str, int]:
    from benchmarks.strata.e2_d.materialize import replay_row_counts

    return replay_row_counts()


def _action_stats() -> dict[str, int]:
    collision_total = 0
    safe_total = 0
    for row in ground_truth_mod.expected_outcomes()["rows"]:
        collision_total += len(row["colliding_action_ids"])
        safe_total += len(row["safe_action_ids"])
    return {"colliding": collision_total, "safe": safe_total}


def build_profile() -> dict[str, Any]:
    """Compute the full profile dict (also the ``profile.json`` schema)."""
    replay_counts = _replay_row_counts()
    replay_share = replay_counts["checkpoints"] / replay_counts["total"]
    return {
        "stratum_version": STRATUM_VERSION,
        "e0_spec": "docs/experiments/e0-meta-level.md §2.6-§2.10, §3.6, §3.7, §5.4",
        "corpus_fingerprint": corpus_fingerprint(),
        "seed": "deterministic (index-driven combinatorics, no RNG)",
        "counts": {
            "conflict_pairs": len(conflict_pairs_mod.CONFLICT_PAIRS),
            "conflict_pairs_type2": len(conflict_pairs_mod.TYPE2_PAIRS),
            "conflict_pairs_type1": len(conflict_pairs_mod.TYPE1_PAIRS),
            "stale_claims": len(stale_claims_mod.STALE_CLAIMS),
            "stale_window_expired": len(stale_claims_mod.WINDOW_EXPIRED_CLAIMS),
            "stale_superseded_goal": len(stale_claims_mod.SUPERSEDED_GOAL_CLAIMS),
            "canaries": len(canaries_mod.CANARY_FACTS),
            "canaries_add_channel": sum(1 for f in canaries_mod.CANARY_FACTS if f.channel == "add"),
            "canaries_checkpoint_channel": sum(
                1 for f in canaries_mod.CANARY_FACTS if f.channel == "checkpoint"
            ),
            "adversarial_scenarios": 1,
            "replay224_scenarios": 1,
            "actions": _action_stats(),
        },
        "engine_binding": {
            "conflict_hint_threshold_in_force": CONFLICT_HINT_THRESHOLD_IN_FORCE,
            "presence_window_sec": 900,
            "delta_max_window_sec": 3600,
            "goal_charset": "ASCII (single line, <= 120 chars)",
            "note": (
                "threshold registered in E0 §8 rev. 2 exactly as implemented "
                "(vesma.awareness.CONFLICT_HINT_MIN_SHARED_TOKENS = 2)"
            ),
        },
        "distribution": {
            "replay224_checkpoint_share": round(replay_share, 6),
            "checkpoint_share_target": CHECKPOINT_SHARE_TARGET,
            "checkpoint_share_tolerance": CHECKPOINT_SHARE_TOLERANCE,
            "replay224_rows": replay_counts,
            "note": (
                "the D-strata are scenario stores, not corpus entries; the "
                "combined e2_gov corpus profile is untouched. The replay "
                "store alone mirrors the live checkpoint share (the E0 §3.7 "
                "drowning condition) at scenario scale"
            ),
        },
        "fixed_parameters": dict(FIXED_PARAMETERS),
        "re_baseline_decision": {
            "s1_rebaseline_required": False,
            "e2_gov_rebaseline_required": False,
            "reason": (
                "e2_d modules are outside both the S1 corpus_fingerprint "
                "module set and the e2_gov profile module set; both measured "
                "corpora stay byte-identical — asserted by test"
            ),
        },
    }


def record_profile() -> dict[str, Any]:
    """(Re)write the committed ``profile.json`` from the live modules."""
    profile = build_profile()
    PROFILE_PATH.write_text(json.dumps(profile, indent=2, sort_keys=False) + "\n")
    return profile


def load_profile() -> dict[str, Any]:
    profile: dict[str, Any] = json.loads(PROFILE_PATH.read_text())
    return profile


def replay_share_ok(profile: dict[str, Any] | None = None) -> bool:
    """The replay-store drowning pin: 58% checkpoints ± 2 pp."""
    prof = profile if profile is not None else build_profile()
    share = float(prof["distribution"]["replay224_checkpoint_share"])
    return abs(share - CHECKPOINT_SHARE_TARGET) <= CHECKPOINT_SHARE_TOLERANCE + 1e-9


def main() -> int:
    profile = record_profile()
    status = "OK" if replay_share_ok(profile) else "OUT OF TOLERANCE"
    print(
        f"e2-d profile: fingerprint={profile['corpus_fingerprint'][:12]}… "
        f"pairs={profile['counts']['conflict_pairs']} "
        f"stale={profile['counts']['stale_claims']} "
        f"canaries={profile['counts']['canaries']} "
        f"replay-checkpoints={profile['distribution']['replay224_checkpoint_share']:.4f} "
        f"({status}) → {PROFILE_PATH}"
    )
    return 0 if replay_share_ok(profile) else 1


if __name__ == "__main__":
    raise SystemExit(main())
