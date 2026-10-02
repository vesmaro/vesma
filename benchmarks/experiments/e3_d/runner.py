#!/usr/bin/env python
"""E3 D-leg runner — treatment vs control over the D-scenario strata (E0 §1.3).

Usage (from the repository root):

    python benchmarks/experiments/e3_d/runner.py             # collect-only
    python benchmarks/experiments/e3_d/runner.py --record    # write the run

THE DEFAULT INVOCATION REFUSES TO RECORD RESULTS. Collect-only executes
every scenario in both arms over the REAL awareness engine, prints the
run manifest and the paired outcome summary, and persists NOTHING: the
first recorded D run is a deliberate human decision taken AFTER this
infrastructure merges (E0's anti-HARKing window stays closed until
then; E0 §6.6). ``--record`` is the explicit opt-in; recorded runs are
WRITE-ONCE (a run directory is never overwritten — a second
``--record`` of the same content-addressed run id fails loud).

Legs (E0 §1.3 — one treatment, control = same scenarios, awareness off):

* **control**   — ``include_awareness=False``: no awareness composition
  is rendered; the agent's context is the scenario view alone (the
  flag-off path is byte-identical to the pre-#254 shape).
* **treatment** — ``include_awareness=True``: the REAL
  ``vesma.awareness.compose_pre_llm_awareness`` composition (presence
  + delta + conflict hints, disclaimer frame, cursor roundtrip) is
  rendered FOR THE PROJECT and appended to the agent's context.

Per scenario and arm (the paired probe, E0 §6.1): a FRESH store is
materialized (``benchmarks/strata/e2_d/materialize.py`` — one isolated
world per (scenario, leg); identical MATERIALIZED store bytes across
arms — the treatment composition writes its ``awr:`` awareness cursor
per the registered composition, so post-composition store bytes differ
by that meta-table row alone), the
agent view is presented (neutral ds-hash/aN/rN ids), the PROBE POLICY
decides, the deterministic oracle scores, and abstention attribution is
recorded where the policy defers on the awareness layer.

── The probe policy (the registered INSTRUMENT) ─────────────────────

E0 registers the D outcomes ("the acting agent intrudes…", §2.6) but
left the acting agent itself unspecified for v0. The runner therefore
executes a SCRIPTED DETERMINISTIC PROBE POLICY — the policy is part of
the measurement instrument, registered here in code and in every run
manifest (``PROBE_POLICY``), an E0-amendment candidate REPORTED to the
orchestrator (this wave does not amend E0 §1-§7; §8 revision 6 is the
only authorized amendment). Definition (``goal-align-then-hazard-
abstain-v1``):

1. the policy reads the SAME context an agent would: the actor goal
   (the LIVE-SESSION goal from the scenario artifact — see the dap-001
   contract below), the view's store rows (newest first), and — in the
   treatment arm only — the rendered awareness section (the
   model-facing surface, parsed as rendered);
2. hazard tokens are extracted from exactly two source kinds: a
   FILE-BEARING store row (a row naming artifact paths — the E0 §2.10
   "files" leg; its hazard tokens are the row's path segments that
   also appear in the actor goal, i.e. file claims anchored to MY
   zone) and a CONFLICT-HINT line of the awareness section (the
   engine-curated, goal-anchored overlap signal). Unverified
   self-reported goal lines, presence lines, and the disclaimer frame
   NEVER create hazards — the fixed frame's own rule (E0 §5.4:
   unverified presence alone must not defer work);
3. actions are ranked by lexical overlap of their description with the
   actor goal (ties by neutral action id); the policy TAKES the first
   ranked action that is not DEFERRED and ABSTAINS from every ranked
   action it skipped. An action is deferred iff one of its
   description's artifact-path SEGMENTS carries a hazard token — the
   deferral semantics are "do not WRITE into a claimed zone"; prose
   mentions of a zone (park/coordinate actions) are not writes.

The policy is ARM-BLIND: one fixed rule, applied identically to both
arms; only its input differs (the awareness section renders in the
treatment arm alone). It is deterministic, so its outcomes are, too.
HONEST SCOPE: a lexical probe is not an LLM agent — its outcomes
validate the pipeline end-to-end and demonstrate the treatment's
signal pathway; they are NOT a behavioral D1/D4 measurement and must
never be reported as the confirmatory result (that requires the real
agent run, which the recorded-run decision gates).

── The dap-001 contract pin (#286 review residual) ──────────────────

The ADVERSARIAL scenario's actor goal comes from the runner's LIVE
SESSION, never from the store: ``materialize_adversarial`` writes NO
actor checkpoint, and the runner must not add one. The actor goal the
policy reads is the scenario artifact's goal (as an operator's tasking
would be), so the engine's hint layer — which reads "my goal" from MY
last STORE checkpoint — stays OFF for dap-001 (the registered
``hint_expected=False``): the spoofed claims reach the agent only
through the delta's [unverified] self-reported layer under the fixed
disclaimer frame, never as a conflict hint. Writing the actor goal
into the store would let the ghosts manufacture hints and fail the E0
§5.4 security falsifier BY CONSTRUCTION. Pinned by test.

── Artifacts (E0 §6.6: single-look, statistics-free at run time) ────

Run manifest: content-addressed over the deterministic core — stratum
fingerprint + counts (the raised 120-pair stratum, E0 §8 rev. 6), the
probe-policy registration, leg configuration, oracle binding, dap-001
contract, frozen scenario clock, code version (package + VERSION + git
+ python + sqlite). Outcomes: per-scenario paired binary outcomes (the
oracle's ``scenario_intruded`` / ``deferred_on_safe_work``), treatment
vs control discordance COUNTERS ONLY — NO p-values, NO CIs, NO
verdicts anywhere in the artifact (the ban is schema, review P2: exact
key allowlists + a recursive stat-key scan). The registered analysis
runs once, outside this runner, after the recorded run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.corpus.deterministic_embedder import LexicalHashEmbedder  # noqa: E402
from benchmarks.strata.e2_d import ground_truth as dgt  # noqa: E402
from benchmarks.strata.e2_d import oracle as doracle  # noqa: E402
from benchmarks.strata.e2_d import profile as dprof  # noqa: E402
from benchmarks.strata.e2_d.adversarial import ADVERSARIAL_SCENARIO  # noqa: E402
from benchmarks.strata.e2_d.conflict_pairs import TYPE1_PAIRS, TYPE2_PAIRS  # noqa: E402
from benchmarks.strata.e2_d.materialize import (  # noqa: E402
    materialize_adversarial,
    materialize_conflict_pair,
    materialize_replay224,
    materialize_stale_claim,
)
from benchmarks.strata.e2_d.replay224 import REPLAY_224  # noqa: E402
from benchmarks.strata.e2_d.scenarios import (  # noqa: E402
    AdversarialScenario,
    ConflictPair,
    Replay224Scenario,
    StaleClaim,
)
from benchmarks.strata.e2_d.stale_claims import STALE_CLAIMS  # noqa: E402

# `mnemos_version` spelling kept for manifest compatibility (recorded verbatim in run manifests).
from vesma import __version__ as mnemos_version  # noqa: E402
from vesma import awareness as awareness_mod  # noqa: E402
from vesma.awareness import DELTA_MAX_WINDOW_SEC, PRESENCE_WINDOW_SEC  # noqa: E402
from vesma.config import Settings  # noqa: E402
from vesma.manager import MemoryManager  # noqa: E402

RUNNER_VERSION = "e3-d-runner-2"
#: Manifest schema version that started pinning ``retrieval.hybrid_alpha``
#: (probe finding 6, issue #300). Older recorded runs predate the key and
#: stay valid as history — verify_manifest gates on this, not on a blanket
#: required-field check that would retroactively invalidate them.
PINNED_RETRIEVAL_FROM = "e3-d-runner-2"
E0_SPEC = "docs/experiments/e0-meta-level.md §1.3, §2.6-§2.10, §3.6, §3.7, §5.4, §6.1, §6.6"
EXPERIMENT = "e3-d"

#: The frozen scenario clock: every materialization and every awareness
#: composition runs at this instant, so the artifacts' relative ages
#: (the strata's determinism) map to absolute timestamps identically on
#: every collect — runs are reproducible and content-addressed.
RUN_NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)

RUNS_DIR = Path(__file__).resolve().parent / "runs"


# ── legs (E0 §1.3) ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LegDefinition:
    """One leg = exactly one include_awareness setting."""

    leg: str
    include_awareness: bool

    def config_dict(self) -> dict[str, Any]:
        return {"include_awareness": self.include_awareness}


#: Fixed leg order: control first, then treatment (E0 §1.3 table order).
LEGS: tuple[LegDefinition, ...] = (
    LegDefinition(leg="control", include_awareness=False),
    LegDefinition(leg="treatment", include_awareness=True),
)


# ── the probe policy (registered instrument — see module docstring) ───────────

PROBE_POLICY: dict[str, Any] = {
    "name": "goal-align-then-hazard-abstain-v1",
    "policy_class": "scripted deterministic probe (part of the instrument)",
    "registration_note": (
        "E0 left the v0 acting agent unspecified; registered here in code "
        "and in every manifest — an E0-amendment candidate reported to the "
        "orchestrator, not amended in this wave (E0 §8 rev. 6 is the only "
        "authorized amendment)"
    ),
    "token_re": r"[a-z0-9][a-z0-9_.\-]+",
    "stopwords": "fixed function-word set (runner.PROBE_STOPWORDS)",
    "hazard_sources": [
        "file-bearing store rows: artifact-path segments also present in the actor goal",
        "awareness conflict-hint lines: the engine's shared-token list",
    ],
    "non_hazards": (
        "unverified self-reported goal lines, presence lines, the disclaimer "
        "frame (E0 §5.4: unverified presence alone must not defer work)"
    ),
    "deferral_rule": (
        "an action is deferred iff one of its description's artifact-path "
        "SEGMENTS carries a hazard token (do not WRITE into a claimed zone; "
        "prose mentions are not writes)"
    ),
    "selection_rule": (
        "rank actions by description/goal token overlap (ties by neutral "
        "action id); take the first non-deferred action, abstain from the "
        "skipped ranked prefix"
    ),
    "arm_blindness": (
        "one fixed rule; only the input differs (the awareness section "
        "renders in the treatment arm only)"
    ),
}

_PROBE_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.\-]+")
_PROBE_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "to",
        "of",
        "in",
        "on",
        "for",
        "with",
        "by",
        "at",
        "is",
        "are",
        "be",
        "as",
        "from",
        "into",
        "under",
        "until",
        "up",
        "out",
        "own",
        "same",
        "other",
        "this",
        "that",
    }
)
#: Artifact-path extractor: a slash-bearing run without whitespace or
#: parentheses (stripped of trailing sentence punctuation).
_PROBE_PATH_RE = re.compile(r"[^\s()]+/[^\s()]+")
#: The engine's fixed hint-section header and hint-line render format
#: ("- <neighbor>: [unverified] shared ['a', 'b']"). The policy parses
#: the section EXACTLY as rendered; a render-format change trips the
#: runner's own tests (the treatment arm would lose its hints).
_PROBE_HINT_HEADER_RE = re.compile(r"^### conflict-hints")
_PROBE_HINT_TOKENS_RE = re.compile(r"shared \[([^\]]*)\]")


def _probe_tokens(text: str) -> frozenset[str]:
    """The policy's fixed tokenizer (lowercase, function words dropped)."""
    return frozenset(t for t in _PROBE_TOKEN_RE.findall(text.lower()) if t not in _PROBE_STOPWORDS)


def _path_segments(text: str) -> list[frozenset[str]]:
    """Segment sets of every artifact path named in ``text``."""
    segments: list[frozenset[str]] = []
    for raw in _PROBE_PATH_RE.findall(text):
        path = raw.strip(".,;:")
        parts = frozenset(s for s in path.split("/") if s)
        if len(parts) >= 2:
            segments.append(parts)
    return segments


@dataclass(frozen=True)
class HazardSource:
    """One hazard contribution: kind, attribution ref, hazard tokens."""

    kind: str  # "file_row" | "conflict_hint"
    ref: str  # the view row id / the neighbor agent
    tokens: frozenset[str]


@dataclass(frozen=True)
class ProbeDecision:
    """The policy's output (view-action space, arm-blind)."""

    chosen: tuple[str, ...]  # 0 or 1 neutral action ids
    abstained: tuple[str, ...]  # the skipped ranked prefix
    attribution: tuple[tuple[str, tuple[HazardSource, ...]], ...]
    hazards: tuple[HazardSource, ...]


def _row_hazard(row: dict[str, Any], goal_tokens: frozenset[str]) -> HazardSource | None:
    """File-bearing row → hazard over MY zone (path segments ∩ goal)."""
    text = f"{row.get('title', '')}\n{row.get('content', '')}"
    segments: set[str] = set()
    for segs in _path_segments(text):
        segments |= segs
    anchored = segments & goal_tokens
    if not anchored:
        return None
    return HazardSource(kind="file_row", ref=str(row["row_id"]), tokens=frozenset(anchored))


def _hint_hazards(awareness_text: str) -> list[HazardSource]:
    """Conflict-hint lines of the rendered section (the engine's format)."""
    hazards: list[HazardSource] = []
    in_hints = False
    for line in awareness_text.splitlines():
        if _PROBE_HINT_HEADER_RE.match(line):
            in_hints = True
            continue
        if in_hints and line.startswith("##"):
            break  # next section — the hints block ends here
        if not in_hints or not line.startswith("- "):
            continue
        neighbor, _sep, _rest = line[2:].partition(":")
        match = _PROBE_HINT_TOKENS_RE.search(line)
        if match is None:
            continue
        tokens = frozenset(t.strip().strip("'\"") for t in match.group(1).split(",") if t.strip())
        if tokens:
            hazards.append(HazardSource(kind="conflict_hint", ref=neighbor.strip(), tokens=tokens))
    return hazards


def _action_deferred(description: str, hazards: tuple[HazardSource, ...]) -> bool:
    """Deferred iff a hazard token sits inside one of the action's
    artifact-path SEGMENTS (a WRITE into the claimed zone)."""
    return any(segs & source.tokens for source in hazards for segs in _path_segments(description))


def probe_decide(
    *,
    goal: str,
    view_rows: list[dict[str, Any]],
    actions: list[dict[str, str]],
    awareness_text: str,
) -> ProbeDecision:
    """The registered probe policy (pure; see PROBE_POLICY + docstring)."""
    goal_tokens = _probe_tokens(goal)
    hazards: list[HazardSource] = []
    for row in view_rows:  # presentation order: newest first (r1..rN)
        hazard = _row_hazard(row, goal_tokens)
        if hazard is not None:
            hazards.append(hazard)
    hazards.extend(_hint_hazards(awareness_text))

    ranked = sorted(
        actions,
        key=lambda a: (-len(_probe_tokens(a["description"]) & goal_tokens), a["action_id"]),
    )
    chosen: tuple[str, ...] = ()
    abstained: list[str] = []
    attribution: list[tuple[str, tuple[HazardSource, ...]]] = []
    for action in ranked:
        deferred_by = tuple(
            source
            for source in hazards
            if source.tokens
            and any(segs & source.tokens for segs in _path_segments(action["description"]))
        )
        if not deferred_by:
            chosen = (action["action_id"],)
            break  # the policy takes the first non-deferred action
        abstained.append(action["action_id"])
        attribution.append((action["action_id"], deferred_by))
    return ProbeDecision(
        chosen=chosen,
        abstained=tuple(abstained),
        attribution=tuple(attribution),
        hazards=tuple(hazards),
    )


# ── scenario registry (uniform execution over the strata) ─────────────────────

Materializer = Callable[[MemoryManager], dict[str, str]]


@dataclass(frozen=True)
class ScenarioCase:
    """One executable scenario: artifact + blind view + materializer.

    ``live_session_goal`` is the dap-001 contract generalized: the goal
    the policy reads is the LIVE SESSION's tasking (the artifact's
    actor goal), never something read back from the store — for
    dap-001 the store holds no actor row at all, so the engine's hint
    layer (my goal = my last store checkpoint) stays off there by
    construction.
    """

    stratum: str  # "type2" | "type1" | "stale" | "adversarial" | "replay224"
    artifact: ConflictPair | StaleClaim | Replay224Scenario | AdversarialScenario
    view: dict[str, Any]  # the blind agent view (ground_truth)
    view_action_map: dict[str, str]  # artifact action id -> neutral view id
    materialize: Materializer  # writes THIS scenario's store; returns row-key map
    peer_agent: str | None  # the legitimate peer (abstention basis lookup)
    live_session_goal: str


def _conflict_case(pair: ConflictPair, stratum: str) -> ScenarioCase:
    def materialize(mgr: MemoryManager) -> dict[str, str]:
        return materialize_conflict_pair(mgr, pair, run_now=RUN_NOW)

    return ScenarioCase(
        stratum=stratum,
        artifact=pair,
        view=dgt.conflict_pair_agent_view(pair),
        view_action_map=dgt.action_view_map(pair.scenario_id, pair.actions),
        materialize=materialize,
        peer_agent=pair.peer_agent,
        live_session_goal=pair.actor_goal,
    )


def _stale_case(claim: StaleClaim) -> ScenarioCase:
    def materialize(mgr: MemoryManager) -> dict[str, str]:
        return materialize_stale_claim(mgr, claim, run_now=RUN_NOW)

    return ScenarioCase(
        stratum="stale",
        artifact=claim,
        view=dgt.stale_claim_agent_view(claim),
        view_action_map=dgt.action_view_map(claim.scenario_id, claim.actions),
        materialize=materialize,
        peer_agent=claim.peer_agent,
        live_session_goal=claim.actor_goal,
    )


def _adversarial_case() -> ScenarioCase:
    scenario = ADVERSARIAL_SCENARIO

    def materialize(mgr: MemoryManager) -> dict[str, str]:
        return materialize_adversarial(mgr, run_now=RUN_NOW)  # writes NO actor row

    return ScenarioCase(
        stratum="adversarial",
        artifact=scenario,
        view=dgt.adversarial_agent_view(),
        view_action_map=dgt.action_view_map(scenario.scenario_id, scenario.actions),
        materialize=materialize,
        peer_agent=None,  # no legitimate claimant exists — ghosts are spoofs
        live_session_goal=scenario.actor_goal,  # the dap-001 pin
    )


def _replay_case() -> ScenarioCase:
    def materialize(mgr: MemoryManager) -> dict[str, str]:
        return materialize_replay224(mgr, run_now=RUN_NOW)

    return ScenarioCase(
        stratum="replay224",
        artifact=REPLAY_224,
        view=dgt.replay224_agent_view(),
        view_action_map=dgt.action_view_map(REPLAY_224.scenario_id, REPLAY_224.actions),
        materialize=materialize,
        peer_agent=REPLAY_224.peer_agent,
        live_session_goal=REPLAY_224.actor_goal,
    )


#: The full registered scenario set in fixed order: raised type-2 block
#: (80, E0 §8 rev. 6), type-1 (40), stale-claims (40), adversarial, replay.
SCENARIO_CASES: tuple[ScenarioCase, ...] = (
    *(_conflict_case(pair, "type2") for pair in TYPE2_PAIRS),
    *(_conflict_case(pair, "type1") for pair in TYPE1_PAIRS),
    *(_stale_case(claim) for claim in STALE_CLAIMS),
    _adversarial_case(),
    _replay_case(),
)

SCENARIO_COUNTS: dict[str, int] = {
    "type2": len(TYPE2_PAIRS),
    "type1": len(TYPE1_PAIRS),
    "stale": len(STALE_CLAIMS),
    "adversarial": 1,
    "replay224": 1,
    "total": len(SCENARIO_CASES),
}

DAP_001_CONTRACT: dict[str, str] = {
    "pin": (
        "the adversarial scenario's actor goal comes from the runner's live "
        "session (the scenario artifact's tasking), never from the store — "
        "materialize_adversarial writes NO actor checkpoint and the runner "
        "must not add one"
    ),
    "consequence": (
        "the engine's hint layer (my goal = my last STORE checkpoint) stays "
        "off for dap-001, so spoofed claims reach the agent only via the "
        "delta's [unverified] self-reported layer; writing the actor goal "
        "into the store would let ghosts manufacture hints and fail the E0 "
        "§5.4 security falsifier by construction"
    ),
    "source": "#286 review residual; registered hint_expected=False (ground truth)",
}


# ── store construction (per scenario, per leg) ─────────────────────────────────


def _scenario_settings(root: Path) -> Settings:
    """Settings for a fresh isolated store in the strata-tests runtime
    shape. Shared by ``_fresh_manager`` and the manifest pin below so
    the pinned retrieval weight can never drift from what the legs
    actually run under."""
    settings = Settings(
        vesma={
            "vault_path": str(root / "vault"),
            "data_dir": str(root / "data"),
            "db_name": "scenario.db",
        },
        scanner={"enabled": False},
    )
    settings.resolve_paths()
    return settings


def _fresh_manager(root: Path) -> MemoryManager:
    """A fresh isolated store in the strata-tests runtime shape."""
    mgr = MemoryManager(_scenario_settings(root))
    mgr._embedder = LexicalHashEmbedder()  # deterministic; the forged-row add path
    return mgr


# ── hashes and code version (the e3_lanes discipline) ─────────────────────────


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _git_state() -> dict[str, Any]:
    try:
        commit = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return {"git_commit": commit, "git_dirty": bool(status.strip())}
    except (OSError, subprocess.CalledProcessError):
        return {"git_commit": "none", "git_dirty": None}


def _code_version() -> dict[str, Any]:
    version_file = (ROOT / "VERSION").read_text().strip()
    return {
        "mnemos_version": mnemos_version,
        "version_file": version_file,
        "python_version": " ".join(sys.version.split()),
        "sqlite_version": sqlite3.sqlite_version,
        **_git_state(),
    }


# ── manifest ───────────────────────────────────────────────────────────────────


def build_manifest() -> dict[str, Any]:
    """The run manifest core — no leg execution, pure state capture."""
    profile = dprof.load_profile()
    return {
        "runner_version": RUNNER_VERSION,
        "experiment": EXPERIMENT,
        "e0_spec": E0_SPEC,
        "legs": {leg.leg: leg.config_dict() for leg in LEGS},
        "retrieval": {
            # The RRF fusion weight the scenario stores run under — the
            # config default of the shared _scenario_settings shape, not
            # a leg parameter (probe finding 6, #300). The root argument
            # is inert: only the search defaults are read.
            "hybrid_alpha": _scenario_settings(ROOT / ".manifest-pin").search.hybrid_alpha,
        },
        "probe_policy": dict(PROBE_POLICY),
        "scenario_clock": {
            "run_now": RUN_NOW.isoformat(),
            "note": (
                "frozen clock — the artifacts' relative ages map identically on every collect"
            ),
        },
        "stratum": {
            "package": "benchmarks/strata/e2_d",
            "stratum_version": dprof.STRATUM_VERSION,
            "corpus_fingerprint": profile["corpus_fingerprint"],
            "counts": dict(SCENARIO_COUNTS),
            "engine_binding": {
                "conflict_hint_threshold_in_force": dprof.CONFLICT_HINT_THRESHOLD_IN_FORCE,
                "presence_window_sec": PRESENCE_WINDOW_SEC,
                "delta_max_window_sec": DELTA_MAX_WINDOW_SEC,
            },
            "raise_rule": (
                "type-2 raised 40 -> 80 per E0 §3.6 (TL decision 2026-09-13, "
                "E0 §8 pre-run revision 6); type-1 unchanged; total 120, n reported"
            ),
        },
        "oracle": {
            "module": "benchmarks/strata/e2_d/oracle.py",
            "outcomes": ["scenario_intruded", "deferred_on_safe_work"],
            "judgment": (
                "exact artifact-stem set intersection (E0 §8 rev. 4 item 1); "
                "binary per-scenario outcomes, identical across arms"
            ),
        },
        "dap_001_contract": dict(DAP_001_CONTRACT),
        "runtime": {
            "scanner_enabled": False,
            "embedder": "deterministic LexicalHashEmbedder",
            "store_isolation": (
                "fresh store per (scenario, leg) — identical materialized store "
                "bytes; the treatment composition writes its awr: cursor per "
                "the registered composition"
            ),
            "runtime_shape": "mirrors the strata engine-binding tests (tests/test_strata_e2_d.py)",
        },
        "code": _code_version(),
    }


def _core(manifest: dict[str, Any]) -> dict[str, Any]:
    excluded = ("created", "run_id", "manifest_sha256", "outcomes_sha256")
    return {k: v for k, v in manifest.items() if k not in excluded}


def manifest_core_hash(manifest: dict[str, Any]) -> str:
    """Content hash over the deterministic manifest core."""
    return _sha256(_canonical_json(_core(manifest)))


def finalize_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Stamp run id, creation time and the full-manifest integrity hash."""
    core_hash = manifest_core_hash(manifest)
    finalized = {
        **manifest,
        "run_id": f"{EXPERIMENT}-{core_hash[:12]}",
        "created": datetime.now(UTC).isoformat(),
    }
    integrity = _sha256(
        _canonical_json({k: v for k, v in finalized.items() if k != "manifest_sha256"})
    )
    return {**finalized, "manifest_sha256": integrity}


def verify_manifest(manifest: dict[str, Any]) -> None:
    """Fail loud when a manifest's integrity hash or structure is broken."""
    required = {
        "runner_version",
        "experiment",
        "e0_spec",
        "legs",
        "probe_policy",
        "scenario_clock",
        "stratum",
        "oracle",
        "dap_001_contract",
        "runtime",
        "code",
        "run_id",
        "created",
        "manifest_sha256",
    }
    missing = required - set(manifest)
    if missing:
        raise AssertionError(f"manifest missing fields: {sorted(missing)}")
    if manifest["runner_version"] == PINNED_RETRIEVAL_FROM and "retrieval" not in manifest:
        # Probe finding 6 (#300): from runner-2 the manifest core must pin
        # the fusion weight; runner-1 history predates the key and stays
        # valid as-is.
        raise AssertionError(
            f"runner_version {PINNED_RETRIEVAL_FROM} must pin retrieval.hybrid_alpha"
        )
    body = {k: v for k, v in manifest.items() if k not in ("manifest_sha256", "outcomes_sha256")}
    expected = _sha256(_canonical_json(body))
    if manifest["manifest_sha256"] != expected:
        raise AssertionError("manifest_sha256 does not match the manifest body")
    if manifest["run_id"] != f"{EXPERIMENT}-{manifest_core_hash(manifest)[:12]}":
        raise AssertionError("run_id does not match the manifest core hash")
    if set(manifest["legs"]) != {"control", "treatment"}:
        raise AssertionError(
            f"legs must be exactly control/treatment, got {sorted(manifest['legs'])}"
        )
    if manifest["legs"]["control"]["include_awareness"] is not False:
        raise AssertionError("control leg must have include_awareness=False")
    if manifest["legs"]["treatment"]["include_awareness"] is not True:
        raise AssertionError("treatment leg must have include_awareness=True")
    if manifest["probe_policy"]["name"] != PROBE_POLICY["name"]:
        raise AssertionError("probe policy name drifted from the registration")
    if manifest["stratum"]["counts"] != SCENARIO_COUNTS:
        raise AssertionError(f"scenario counts must be exactly {SCENARIO_COUNTS}")
    if manifest["stratum"]["counts"]["type2"] != 80:
        raise AssertionError("type-2 count must be 80 (the E0 §8 rev. 6 raise)")
    if manifest["scenario_clock"]["run_now"] != RUN_NOW.isoformat():
        raise AssertionError("scenario clock drifted from the frozen RUN_NOW")
    if manifest["stratum"]["corpus_fingerprint"] != dprof.corpus_fingerprint():
        raise AssertionError("stratum fingerprint drifted from the committed profile")


# ── leg execution ──────────────────────────────────────────────────────────────


def _abstention_basis(
    mgr: MemoryManager, case: ScenarioCase, hint_sources: tuple[HazardSource, ...]
) -> str | None:
    """The peer checkpoint id backing a hint-sourced abstention.

    The basis must be the checkpoint of the neighbor whose HINT deferred
    the action (the abstention provenance chain anchors to it); the
    lookup is server-side (the #251 stamps), never key-side guessing.
    """
    neighbors = {s.ref for s in hint_sources}
    if not neighbors or case.peer_agent not in neighbors:
        return None
    rows = mgr.list_recent(limit=50, project=case.artifact.project, agent=case.peer_agent)
    for row in rows:
        if row.metadata.get("checkpoint_agent"):
            return str(row.id)
    return None


def execute_arm(case: ScenarioCase, leg: LegDefinition, store_root: Path) -> dict[str, Any]:
    """One paired probe: one scenario, one arm, one fresh isolated store."""
    mgr = _fresh_manager(store_root)
    try:
        case.materialize(mgr)
        awareness_text = ""
        hint_count = 0
        if leg.include_awareness:
            # THE leg toggle (E0 §1.3): treatment renders the real
            # awareness composition for the actor; control renders none.
            composed = awareness_mod.compose_pre_llm_awareness(
                mgr,
                session=case.artifact.actor_session,
                project=case.artifact.project,
                agent=case.artifact.actor_agent,
                now=RUN_NOW,
            )
            awareness_text = str(composed["text"])
            hint_count = int(composed["meta"]["conflict_hints"])

        decision = probe_decide(
            goal=case.live_session_goal,
            view_rows=list(case.view["store_rows"]),
            actions=list(case.view["actions"]),
            awareness_text=awareness_text,
        )
        to_artifact = {view: aid for aid, view in case.view_action_map.items()}
        chosen_artifact = tuple(to_artifact[a] for a in decision.chosen)
        abstained_artifact = tuple(to_artifact[a] for a in decision.abstained)
        intruded = doracle.scenario_intruded(case.artifact, chosen_artifact)
        deferred = doracle.deferred_on_safe_work(case.artifact, abstained_artifact)

        abstention_chain: dict[str, Any] | None = None
        if leg.include_awareness and decision.attribution:
            # record_abstention fires ONLY for hint-sourced deferrals — the
            # engine's chain is presence-anchored (abstention → delta-block
            # → checkpoint → writer-session); a file-row deferral has no
            # presence basis and rides the outcomes attribution only.
            hint_sources = tuple(
                source
                for _aid, sources in decision.attribution
                for source in sources
                if source.kind == "conflict_hint"
            )
            basis = _abstention_basis(mgr, case, hint_sources)
            if basis is not None:
                recorded = awareness_mod.record_abstention(
                    mgr,
                    project=case.artifact.project,
                    agent=case.artifact.actor_agent,
                    session=case.artifact.actor_session,
                    basis_checkpoint_id=basis,
                    note=f"probe policy {PROBE_POLICY['name']}: hazard-flagged action deferred",
                    now=RUN_NOW,
                )
                abstention_chain = dict(recorded["chain"])

        return {
            "intruded": bool(intruded),
            "deferred": bool(deferred),
            "chosen": list(decision.chosen),
            "abstained": list(decision.abstained),
            "hints": hint_count,
            "abstention_attribution": [
                {
                    "action": aid,
                    "sources": [{"kind": s.kind, "ref": s.ref} for s in sources],
                }
                for aid, sources in decision.attribution
            ],
            "abstention_chain": abstention_chain,
        }
    finally:
        mgr.close()


def replay_peer_top_slot() -> bool:
    """E0 §3.7 pass-criterion leg 1 (engine-side, arm-independent)."""
    own_tmp = tempfile.TemporaryDirectory(prefix="mnemos-e3d-replay-")
    try:
        mgr = _fresh_manager(Path(own_tmp.name))
        try:
            materialize_replay224(mgr, run_now=RUN_NOW)
            delta = awareness_mod.project_delta(
                mgr,
                project=REPLAY_224.project,
                since=(RUN_NOW - timedelta(seconds=DELTA_MAX_WINDOW_SEC)).isoformat(),
                exclude_agent=REPLAY_224.actor_agent,
                now=RUN_NOW,
            )
            agents = delta.get("agents", [])
            return bool(agents) and agents[0]["agent"] == REPLAY_224.peer_agent
        finally:
            mgr.close()
    finally:
        own_tmp.cleanup()


def execute_legs(root: Path | None = None) -> dict[str, Any]:
    """Execute every scenario in both arms under isolated stores."""
    own_tmp: tempfile.TemporaryDirectory[str] | None = None
    if root is None:
        own_tmp = tempfile.TemporaryDirectory(prefix="mnemos-e3d-")
        root = Path(own_tmp.name)
    try:
        arms: dict[str, list[dict[str, Any]]] = {}
        for leg in LEGS:
            arms[leg.leg] = [
                execute_arm(case, leg, root / f"{leg.leg}-{case.view['view_id']}")
                for case in SCENARIO_CASES
            ]
        return {"arms": arms, "replay_peer_top_slot": replay_peer_top_slot()}
    finally:
        if own_tmp is not None:
            own_tmp.cleanup()


# ── paired outcomes (E0 §6.1 / §6.6: counters only, no statistics) ────────────


def build_outcomes(executed: dict[str, Any]) -> dict[str, Any]:
    """Paired per-scenario outcomes + treatment-vs-control tallies.

    Counts and proportions only — no p-values, no CIs, no verdicts at
    run time (E0 §6.6 single-look: the registered analysis runs once
    per leg after data collection completes, never inside the runner).
    """
    arms = executed["arms"]
    leg_names = [leg.leg for leg in LEGS]
    scenarios: list[dict[str, Any]] = []
    for index, case in enumerate(SCENARIO_CASES):
        scenarios.append(
            {
                "view_id": case.view["view_id"],
                "stratum": case.stratum,
                **{leg: dict(arms[leg][index]) for leg in leg_names},
            }
        )
    by_stratum = {
        stratum: [row for row in scenarios if row["stratum"] == stratum]
        for stratum in SCENARIO_COUNTS
        if stratum != "total"
    }

    def _count(stratum: str, leg: str, field: str) -> int:
        return sum(1 for row in by_stratum[stratum] if row[leg][field])

    def _discordant(stratum: str, field: str) -> dict[str, int]:
        rows = by_stratum[stratum]
        return {
            "treatment_only": sum(
                1 for r in rows if r["treatment"][field] and not r["control"][field]
            ),
            "control_only": sum(
                1 for r in rows if r["control"][field] and not r["treatment"][field]
            ),
        }

    arm_rates: dict[str, Any] = {}
    for leg in leg_names:
        arm_rates[leg] = {
            "type2_intruded": _count("type2", leg, "intruded"),
            "type2_intrusion_rate": _count("type2", leg, "intruded") / SCENARIO_COUNTS["type2"],
            "type1_intruded": _count("type1", leg, "intruded"),
            "type1_intrusion_rate": _count("type1", leg, "intruded") / SCENARIO_COUNTS["type1"],
            "stale_over_deferred": _count("stale", leg, "deferred"),
            "stale_over_deferral_rate": _count("stale", leg, "deferred") / SCENARIO_COUNTS["stale"],
            "replay_intruded": _count("replay224", leg, "intruded"),
            "adversarial_deferred": _count("adversarial", leg, "deferred"),
            "abstaining_scenarios": sum(1 for row in scenarios if row[leg]["abstained"]),
        }

    replay_rows = by_stratum["replay224"]
    adversarial_rows = by_stratum["adversarial"]
    diagnostics = {
        "replay224": {
            "peer_top_slot": bool(executed["replay_peer_top_slot"]),
            "intruded": {leg: bool(replay_rows[0][leg]["intruded"]) for leg in leg_names},
        },
        "adversarial": {
            "deferred": {leg: bool(adversarial_rows[0][leg]["deferred"]) for leg in leg_names},
            "note": (
                "E0 §5.4: any treatment-arm deferral attributable to the spoofed "
                "block is a security-contour FAIL regardless of D1/D4"
            ),
        },
    }

    return {
        "pairing": (
            "per-scenario McNemar pairs across arms on the identical scenario "
            "store build (E0 §6.1); pairing key = the neutral ds-hash view id"
        ),
        "denominators": dict(SCENARIO_COUNTS),
        "legs": leg_names,
        "scenarios": scenarios,
        "discordance": {
            "type2_intrusion": _discordant("type2", "intruded"),
            "type1_intrusion": _discordant("type1", "intruded"),
            "stale_over_deferral": _discordant("stale", "deferred"),
        },
        "arm_rates": arm_rates,
        "diagnostics": diagnostics,
    }


#: The exact top-level key set of an outcomes artifact. The statistics
#: ban (E0 §6.6 — no p-values/CI/verdicts at run time) is SCHEMA, not
#: convention: an artifact outside this set cannot be recorded.
_OUTCOME_TOP_KEYS: frozenset[str] = frozenset(
    {
        "pairing",
        "denominators",
        "legs",
        "scenarios",
        "discordance",
        "arm_rates",
        "diagnostics",
    }
)

#: The exact per-arm key set inside ``arm_rates`` (counts + proportions
#: of the raw rows — nothing else).
_ARM_RATE_KEYS: frozenset[str] = frozenset(
    {
        "type2_intruded",
        "type2_intrusion_rate",
        "type1_intruded",
        "type1_intrusion_rate",
        "stale_over_deferred",
        "stale_over_deferral_rate",
        "replay_intruded",
        "adversarial_deferred",
        "abstaining_scenarios",
    }
)

#: The exact per-arm key set of a scenario row's arm payload.
_ARM_ROW_KEYS: frozenset[str] = frozenset(
    {
        "intruded",
        "deferred",
        "chosen",
        "abstained",
        "hints",
        "abstention_attribution",
        "abstention_chain",
    }
)

#: Any key matching this pattern ANYWHERE in the artifact (any depth,
#: dicts inside lists included) marks it as carrying statistics — refused.
#: Case-INSENSITIVE (review #294/#295: P_VALUE/CI95/Verdict must not ride
#: on a case flip), and the spellings close the pval/conf_int gap the
#: bare ``p.?value`` / ``ci\\d*`` alternation left open. ``ci[\W_]?\d+``
#: keeps the interval shape (ci95, ci_95) without false-firing on prose
#: keys that merely contain "ci" as letters; the word-bounded ``p`` /
#: ``pvals?`` arms close the lone ``"p": 0.03`` and ``"pval"`` rides.
_STAT_KEY_RE = re.compile(
    r"p.?values?|\bpvals?\b|\bp\b|power\b|ci[\W_]?\d+|verdict|signif"
    r"|conf(?:iden|[\W_]?int)",
    re.IGNORECASE,
)

_VIEW_ID_RE = re.compile(r"^ds-[0-9a-f]{10}$")


def _stat_key_offenders(node: Any, path: str) -> list[str]:
    """Every key path in the structure whose key matches _STAT_KEY_RE."""
    offenders: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            if _STAT_KEY_RE.search(str(key)):
                offenders.append(here)
            offenders.extend(_stat_key_offenders(value, here))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            offenders.extend(_stat_key_offenders(value, f"{path}[{index}]"))
    return offenders


def verify_outcomes(outcomes: dict[str, Any]) -> None:
    """Structural check of an outcomes artifact (fail loud on drift).

    The statistics ban is enforced by SCHEMA (review P2): the exact
    top-level key set, the exact per-arm and arm-rate key sets, and a
    recursive scan rejecting any key matching ``p.?value`` / ``ci\\d*`` /
    ``verdict`` / ``signif`` / ``confiden`` anywhere in the structure —
    the artifact is INCAPABLE of carrying statistics, not merely
    discouraged from it. (``record_run`` stamps the linkage field
    ``run_id`` onto the recorded copy AFTER this verification.)
    """
    missing = _OUTCOME_TOP_KEYS - set(outcomes)
    extra = set(outcomes) - _OUTCOME_TOP_KEYS
    if missing or extra:
        raise AssertionError(
            f"outcomes top-level keys must be exactly {sorted(_OUTCOME_TOP_KEYS)} — "
            f"missing={sorted(missing)}, unexpected={sorted(extra)}"
        )
    offenders = _stat_key_offenders(outcomes, "")
    if offenders:
        raise AssertionError(
            "outcomes artifact must not carry statistical keys "
            f"({_STAT_KEY_RE.pattern!r}) — forbidden at: {', '.join(offenders)}"
        )
    if outcomes["denominators"] != SCENARIO_COUNTS:
        raise AssertionError(f"denominators must be exactly {SCENARIO_COUNTS}")
    if outcomes["legs"] != [leg.leg for leg in LEGS]:
        raise AssertionError("legs must be exactly ['control', 'treatment']")
    scenarios = outcomes["scenarios"]
    if len(scenarios) != SCENARIO_COUNTS["total"]:
        raise AssertionError("scenarios must cover the full registered stratum")
    view_ids = [row["view_id"] for row in scenarios]
    if len(set(view_ids)) != len(view_ids):
        raise AssertionError("pairing keys (view ids) must be unique")
    if not all(_VIEW_ID_RE.match(v) for v in view_ids):
        raise AssertionError("pairing keys must be neutral ds-hash view ids")
    strata: dict[str, int] = {}
    for row in scenarios:
        strata[row["stratum"]] = strata.get(row["stratum"], 0) + 1
        for leg in outcomes["legs"]:
            if set(row[leg]) != _ARM_ROW_KEYS:
                raise AssertionError(
                    f"scenario {row['view_id']} arm {leg}: keys must be exactly "
                    f"{sorted(_ARM_ROW_KEYS)} — got {sorted(row[leg])}"
                )
            if not isinstance(row[leg]["intruded"], bool) or not isinstance(
                row[leg]["deferred"], bool
            ):
                raise AssertionError("arm outcomes must be binary booleans")
    expected_strata = {k: v for k, v in SCENARIO_COUNTS.items() if k != "total"}
    if strata != expected_strata:
        raise AssertionError(f"stratum coverage drifted: {strata}")
    for leg, rates in outcomes["arm_rates"].items():
        if set(rates) != _ARM_RATE_KEYS:
            raise AssertionError(
                f"arm_rates[{leg!r}] keys must be exactly {sorted(_ARM_RATE_KEYS)} — "
                f"got {sorted(rates)}"
            )
    if set(outcomes["discordance"]) != {
        "type2_intrusion",
        "type1_intrusion",
        "stale_over_deferral",
    }:
        raise AssertionError("discordance keys drifted")
    for tally in outcomes["discordance"].values():
        if set(tally) != {"treatment_only", "control_only"}:
            raise AssertionError("discordance tallies must be treatment/control only")
    if set(outcomes["diagnostics"]) != {"replay224", "adversarial"}:
        raise AssertionError("diagnostics keys drifted")


# ── collect / record ──────────────────────────────────────────────────────────


def verify_recorded(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Re-verify an ARCHIVED run directory (machine-checkable history).

    A recorded run persists ``manifest.json`` verbatim plus
    ``outcomes.json`` in its RECORDED shape — with the linkage field
    ``run_id`` stamped on (review #294/#295: the bare verify_* gates
    reject exactly that key, so a plain re-check could never pass an
    archived artifact). This helper loads both files, strips the
    stamped linkage key, and runs the same structural gates the live
    artifact passed: ``verify_manifest`` + ``verify_outcomes``. Raises
    ``AssertionError`` on drift, ``FileNotFoundError`` when the run
    directory is not a complete record.
    """
    manifest: dict[str, Any] = json.loads((run_dir / "manifest.json").read_text())
    recorded: dict[str, Any] = json.loads((run_dir / "outcomes.json").read_text())
    stamped = recorded.pop("run_id", None)
    if stamped != manifest["run_id"]:
        raise AssertionError(
            f"archived outcomes run_id {stamped!r} does not match the manifest's "
            f"run id {manifest['run_id']!r}"
        )
    verify_manifest(manifest)
    expected = manifest.get("outcomes_sha256")
    if expected is not None:  # runner-1 recorded runs predate the stamp
        actual = hashlib.sha256(((run_dir / "outcomes.json").read_text()).encode()).hexdigest()
        if actual != expected:
            raise AssertionError(f"archived outcomes content hash drift: {actual} != {expected}")
    verify_outcomes(recorded)
    return manifest, recorded


def collect_run() -> tuple[dict[str, Any], dict[str, Any]]:
    """Execute both arms and build (manifest, outcomes). Persists NOTHING."""
    manifest = finalize_manifest(build_manifest())
    executed = execute_legs()
    outcomes = build_outcomes(executed)
    verify_manifest(manifest)
    verify_outcomes(outcomes)
    return manifest, outcomes


def record_run(manifest: dict[str, Any], outcomes: dict[str, Any], runs_dir: Path) -> Path:
    """Write the run artifacts — WRITE-ONCE, ATOMIC, never overwritten.

    Atomicity (review P3): both files are staged into a hidden
    ``.tmp-<run_id>`` directory and moved into place with one ``rename``
    — a crash mid-write leaves at most a staging dir (cleaned on the
    next attempt), never a partial run directory that write-once would
    then refuse forever.
    """
    run_id = str(manifest["run_id"])
    run_dir: Path = runs_dir / run_id
    if run_dir.exists():
        raise FileExistsError(
            f"run {run_id} already recorded at {run_dir} — "
            "recorded runs are write-once; a re-record is a new run state"
        )
    verify_manifest(manifest)
    # Linkage BEFORE the schema gate (review #294/#295): verify_outcomes
    # rejects ``run_id`` as a top-level key, so this check placed after it
    # was unreachable dead code. A caller may pass the recorded shape
    # (outcomes carrying the manifest's run_id) — but only with the
    # MATCHING id; a foreign one is refused here, by name.
    if outcomes.get("run_id") not in (None, manifest["run_id"]):
        raise AssertionError(
            f"outcomes/manifest run id mismatch: {outcomes.get('run_id')!r} "
            f"is not the manifest's run id {manifest['run_id']!r}"
        )
    verify_outcomes(outcomes)
    recorded = {**outcomes, "run_id": manifest["run_id"]}
    manifest = {
        **manifest,
        # Content hash of the RECORDED outcomes (the run_id-stamped shape,
        # issue #295): verify_recorded re-checks it on reload, so a
        # tampered archived artifact fails loud — outcomes were the only
        # recorded file without an integrity link.
        "outcomes_sha256": hashlib.sha256(
            (json.dumps(recorded, indent=2) + "\n").encode()
        ).hexdigest(),
    }
    runs_dir.mkdir(parents=True, exist_ok=True)
    staging = runs_dir / f".tmp-{run_id}"
    if staging.exists():
        shutil.rmtree(staging)  # leftover of an earlier crashed attempt
    staging.mkdir()
    try:
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        (staging / "outcomes.json").write_text(json.dumps(recorded, indent=2) + "\n")
        try:
            staging.rename(run_dir)
        except OSError as exc:
            # rename onto a non-empty existing dir fails — the write-once
            # race lost; surface it as the same FileExistsError contract.
            raise FileExistsError(
                f"run {run_id} already recorded at {run_dir} (rename race): {exc}"
            ) from exc
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return run_dir


def _print_summary(manifest: dict[str, Any], outcomes: dict[str, Any]) -> None:
    print(f"run_id: {manifest['run_id']}")
    print(
        f"stratum: {manifest['stratum']['stratum_version']} "
        f"fingerprint={manifest['stratum']['corpus_fingerprint'][:12]}… "
        f"scenarios={manifest['stratum']['counts']['total']}"
    )
    rates = outcomes["arm_rates"]
    for leg in outcomes["legs"]:
        r = rates[leg]
        print(
            f"arm {leg:>9}: type2-intrusion {r['type2_intrusion_rate']:.4f} "
            f"({r['type2_intruded']}/{outcomes['denominators']['type2']})   "
            f"type1-intrusion {r['type1_intrusion_rate']:.4f} "
            f"({r['type1_intruded']}/{outcomes['denominators']['type1']})   "
            f"stale-over-deferral {r['stale_over_deferral_rate']:.4f} "
            f"({r['stale_over_deferred']}/{outcomes['denominators']['stale']})"
        )
    for comparison, tally in outcomes["discordance"].items():
        print(f"discordance {comparison}: {tally}")
    print(f"diagnostics: {json.dumps(outcomes['diagnostics'], sort_keys=True)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="E3 D-leg runner — treatment vs control over the D-scenario strata (E0 §1.3)"
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help=(
            "write the run artifacts (write-once) under the runs directory; "
            "the DEFAULT refuses to record — the first real D run is a "
            "deliberate decision after this infrastructure merges"
        ),
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=RUNS_DIR,
        help=f"runs directory (default: {RUNS_DIR})",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress the summary")
    args = parser.parse_args(argv)

    print(
        "e3-d: collecting arms control/treatment over the D-scenario strata "
        "(deterministic probe policy)…",
        file=sys.stderr,
    )
    manifest, outcomes = collect_run()

    if not args.quiet:
        _print_summary(manifest, outcomes)

    if not args.record:
        print(
            "e3-d: REFUSING to record — collect-only. The first real D run is a "
            "deliberate human decision (E0 anti-HARKing window); re-run with "
            "--record to persist this run.",
            file=sys.stderr,
        )
        return 0

    try:
        run_dir = record_run(manifest, outcomes, args.runs_dir)
    except FileExistsError as exc:
        print(f"e3-d: FAIL — {exc}", file=sys.stderr)
        return 1
    print(f"e3-d: run recorded (write-once) → {run_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
