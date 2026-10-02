#!/usr/bin/env python
"""E3 lanes runner — legs A/B/B0 over the G-gov/G-neg strata (E0 §1.1).

Usage (from the repository root):

    python benchmarks/experiments/e3_lanes/runner.py             # collect-only
    python benchmarks/experiments/e3_lanes/runner.py --record    # write the run

THE DEFAULT INVOCATION REFUSES TO RECORD RESULTS. Collect-only executes
every leg over the real strata, prints the run manifest and the paired
outcome summary, and persists NOTHING: the first real E3 run is a
deliberate human decision taken AFTER this infrastructure merges (E0's
anti-HARKing window stays closed until then). ``--record`` is the
explicit opt-in; recorded runs are WRITE-ONCE (a run directory is never
overwritten — a second ``--record`` of the same content-addressed run
id fails loud).

Legs (E0 §1.1, one treatment each — LanesConfig enforces exclusivity):

* **A**  — control: ``LanesConfig.enabled=false``, ``type_boost=false``
  (the pre-E1 code path, byte-identical).
* **B0** — trivial anti-confounding leg: ``type_boost=true`` — governance
  rows boosted at recall (``lanes.B0_TYPE_BOOST_FACTOR``), one ranking
  line, zero meta-level. No engine surface existed for B0 before this
  runner wave; the minimal boost is registered here, pre-run.
* **B**  — treatment: ``LanesConfig.enabled=true`` — deterministic
  rules/decisions lanes + governance pinned into the lane-ordered
  budget sort.

What is measured and persisted (E0 §2.3/§2.4/§6.1 — nothing else):

* **G-gov** (n = 96 analyzed queries under the CURRENT ledger state):
  per-query binary outcome — the seeded gold record in the top-5 blocks
  of ``assemble_context`` (governance-recall@5). The pairing key is the
  qid itself (``gg-NNN-ph`` / ``gg-NNN-pr``, two phrasings per record).
* **G-neg** (n = 24): per-query false-insertion flag — any
  governance-class block in the top-5 (governance-noise-rate, E0 §2.4b).
* Per-pair triples (A, B0, B) and discordance tallies per comparison —
  the exact-McNemar inputs. NO statistics are computed at run time: no
  p-values, no CIs, no verdicts (single-look analysis, E0 §6.6).

Run manifest: content-addressed over the deterministic core — ledger
state hash (replacements change the analyzed qid set), corpus
fingerprints (e2-gov + the S1 pin covering the golden 81 inside the
combined build), code version (package version + VERSION file + git
commit + dirty flag), leg configuration, equal budget (E0 §4.1). Run
artifacts live under ``benchmarks/experiments/e3_lanes/runs/<run_id>/``
(gitignored: run artifacts stay on disk and are committed deliberately
by the E3 report wave when citing them — the tree stays clean between
record and archival, so ``git_dirty`` in future run cores stays stable).

E0 §6.6 run-ledger note: appending the run-ledger entry to the E0
document is part of the deliberate first-run step (the document is
binding and is not touched by this module); the recorded manifest here
carries everything that entry needs.
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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.strata.e2_gov import ground_truth as gt  # noqa: E402
from benchmarks.strata.e2_gov import profile as prof  # noqa: E402
from benchmarks.strata.e2_gov.loader import fresh_experimental_manager  # noqa: E402
from benchmarks.strata.e2_gov.queries import NEG_QUERIES  # noqa: E402
from benchmarks.strata.e2_gov.records import GOV_RECORDS  # noqa: E402

# `mnemos_version` spelling kept for manifest compatibility (recorded verbatim in run manifests).
from vesmaro import __version__ as mnemos_version  # noqa: E402
from vesmaro.assemble import DEFAULT_BUDGET, assemble_context  # noqa: E402
from vesmaro.lanes import B0_TYPE_BOOST_FACTOR  # noqa: E402

RUNNER_VERSION = "e3-lanes-runner-2"
#: Manifest schema version that started pinning ``retrieval.hybrid_alpha``
#: (probe finding 6, issue #300). Older recorded runs predate the key and
#: stay valid as history — verify_manifest gates on this, not on a blanket
#: required-field check that would retroactively invalidate them.
PINNED_RETRIEVAL_FROM = "e3-lanes-runner-2"
E0_SPEC = "docs/experiments/e0-meta-level.md §1.1, §2.3, §2.4, §4.1, §6.1, §6.6"
EXPERIMENT = "e3-lanes"

#: E0 §4.1 equal-budget: every leg assembles under the SAME token budget.
E3_TOKEN_BUDGET: int = DEFAULT_BUDGET
#: E0 §2.3 — the @5 of governance-recall@5.
TOP_K: int = 5

RUNS_DIR = Path(__file__).resolve().parent / "runs"


# ── legs (E0 §1.1) ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LegDefinition:
    """One leg = exactly one treatment (A / B0 / B)."""

    leg: str
    lanes_enabled: bool
    type_boost: bool

    def config_dict(self) -> dict[str, Any]:
        cfg: dict[str, Any] = {
            "lanes_enabled": self.lanes_enabled,
            "type_boost": self.type_boost,
        }
        if self.type_boost:
            cfg["b0_type_boost_factor"] = B0_TYPE_BOOST_FACTOR
        return cfg


#: Fixed leg order: control → trivial → treatment (E0 §1.1 table order).
LEGS: tuple[LegDefinition, ...] = (
    LegDefinition(leg="A", lanes_enabled=False, type_boost=False),
    LegDefinition(leg="B0", lanes_enabled=False, type_boost=True),
    LegDefinition(leg="B", lanes_enabled=True, type_boost=False),
)


# ── hashes and code version ───────────────────────────────────────────────────


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def ledger_state_hash(ledger: dict[str, Any]) -> str:
    """sha256 over the canonical ledger state.

    Replacements change the analyzed record set (hence the analyzed qid
    set) — this hash couples every run manifest to the exact
    adjudication state it ran under (issue #277).
    """
    return _sha256(_canonical_json(ledger))


def _git_state() -> dict[str, Any]:
    """Best-effort git provenance; ``none`` when git or the repo is absent."""
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
    """Code + platform provenance of the manifest core.

    Platform versions are part of the identity (review P3): the RRF
    recall rides FTS5 bm25 ranking from the LINKED libsqlite3, and the
    same git commit can behave differently across interpreter/sqlite
    builds — ``sys.version`` is whitespace-normalized to one line.
    """
    version_file = (ROOT / "VERSION").read_text().strip()
    return {
        "mnemos_version": mnemos_version,
        "version_file": version_file,
        "python_version": " ".join(sys.version.split()),
        "sqlite_version": sqlite3.sqlite_version,
        **_git_state(),
    }


# ── manifest ───────────────────────────────────────────────────────────────────


def build_manifest(ledger: dict[str, Any]) -> dict[str, Any]:
    """The run manifest core — no leg execution, pure state capture.

    The deterministic core (everything except ``created`` / ``run_id`` /
    ``manifest_sha256``) is content-addressed: identical ledger, corpus,
    code and leg configuration produce the identical core hash, hence
    the identical run id — write-once recording then refuses accidental
    duplicates.
    """
    analyzed = gt.active_analyzed_queries(ledger)  # asserts the 96 denominator
    return {
        "runner_version": RUNNER_VERSION,
        "experiment": EXPERIMENT,
        "e0_spec": E0_SPEC,
        "legs": {leg.leg: leg.config_dict() for leg in LEGS},
        "equal_budget": E3_TOKEN_BUDGET,
        "top_k": TOP_K,
        "retrieval": {
            # The fusion weight the leg searches run under — the config
            # default, not a leg parameter (probe finding 6, #300).
            "hybrid_alpha": _leg_hybrid_alpha(),
        },
        "denominator": gt.ANALYZED_DENOMINATOR,
        "ledger": {
            "artifact": "benchmarks/strata/e2_gov/adjudication_ledger.json",
            "state_sha256": ledger_state_hash(ledger),
            "rejects": len(ledger["rejects"]),
            "replacements": len(ledger["replacements"]),
            "analyzed_records": len(ledger["analyzed_records"]),
        },
        "corpus": {
            "combined_total": len(prof.experimental_corpus()),
            "e2_gov_corpus_fingerprint": prof.corpus_fingerprint(),
            "e2_gov_stratum_version": prof.STRATUM_VERSION,
            "s1_corpus_fingerprint": _s1_corpus_fingerprint(),
        },
        "code": _code_version(),
        "queries": {
            "analyzed": len(analyzed),
            "neg": len(NEG_QUERIES),
            "active_qid_set_sha256": _sha256(_canonical_json(sorted(q.qid for q in analyzed))),
        },
    }


def _s1_corpus_fingerprint() -> str:
    """The S1 corpus fingerprint — covers the golden 81 inside the
    combined experimental build (byte-pinned upstream, c2ce056d…)."""
    from benchmarks.stands.s1_quality import run as s1_run

    return s1_run.corpus_fingerprint()


def _leg_hybrid_alpha() -> float:
    """The RRF fusion weight every leg search runs under.

    Leg stores are built via ``fresh_experimental_manager`` →
    ``golden_settings``, which does not override ``search`` — the config
    default governs the fusion. Pinned into the manifest core (probe
    finding 6, issue #300) so a default alpha re-tune changes the
    content-addressed core hash instead of silently re-running under a
    different composition algorithm. The root argument is inert: only
    the search defaults are read, no store is ever built there.
    """
    from benchmarks.stands.s1_quality.harness import golden_settings

    return golden_settings(ROOT / ".manifest-pin").search.hybrid_alpha


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
        "legs",
        "equal_budget",
        "top_k",
        "denominator",
        "ledger",
        "corpus",
        "code",
        "queries",
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
    if set(manifest["legs"]) != {"A", "B0", "B"}:
        raise AssertionError(f"legs must be exactly A/B0/B, got {sorted(manifest['legs'])}")
    if manifest["denominator"] != 96 or manifest["queries"]["analyzed"] != 96:
        raise AssertionError("analyzed denominator must be 96 (E0 §3.1)")


# ── leg execution ─────────────────────────────────────────────────────────────


def _apply_leg_settings(mgr: Any, leg: LegDefinition) -> None:
    """Configure the manager for one leg and fail loud on any mismatch.

    Direct assignment overrides any env-loaded state
    (``VESMA_LANES__*``); the assert backstop catches a future config
    path that would ignore the assignment.
    """
    mgr.settings.lanes.enabled = leg.lanes_enabled
    mgr.settings.lanes.type_boost = leg.type_boost
    effective = (mgr.settings.lanes.enabled, mgr.settings.lanes.type_boost)
    if effective != (leg.lanes_enabled, leg.type_boost):
        raise AssertionError(
            f"leg {leg.leg}: effective lanes config {effective} != "
            f"assigned ({leg.lanes_enabled}, {leg.type_boost})"
        )


def run_leg(leg: LegDefinition, ledger: dict[str, Any], root: Path) -> dict[str, Any]:
    """Execute one leg over the real strata under its own fresh manager.

    Per-leg manager isolation mirrors the S1m discipline: each leg
    ingests the identical combined corpus into its own store (fixed
    order, deterministic embedder, scanner off) — no shared vector
    state between legs.
    """
    analyzed = gt.active_analyzed_queries(ledger)
    with fresh_experimental_manager(root) as (mgr, slug_to_id):
        _apply_leg_settings(mgr, leg)
        session = f"e3-{EXPERIMENT}-leg-{leg.leg}"

        gov_rows: list[dict[str, Any]] = []
        for query in analyzed:
            result = assemble_context(
                mgr,
                session=session,
                project=query.project,
                query=query.text,
                budget=E3_TOKEN_BUDGET,
            )
            top_ids = {b["memory_id"] for b in result["blocks"][:TOP_K]}
            gov_rows.append(
                {
                    "qid": query.qid,
                    "family": query.family,
                    "record_slug": query.record_slug,
                    "hit": slug_to_id[query.record_slug] in top_ids,
                }
            )

        governance_id_to_slug = {slug_to_id[record.slug]: record.slug for record in GOV_RECORDS}
        neg_rows: list[dict[str, Any]] = []
        for neg in NEG_QUERIES:
            result = assemble_context(
                mgr,
                session=session,
                project=neg.project,
                query=neg.text,
                budget=E3_TOKEN_BUDGET,
            )
            top_ids = {b["memory_id"] for b in result["blocks"][:TOP_K]}
            inserted = sorted(
                governance_id_to_slug[mid] for mid in top_ids & set(governance_id_to_slug)
            )
            neg_rows.append(
                {
                    "qid": neg.qid,
                    "false_insertion": bool(inserted),
                    "inserted_governance_slugs": inserted,
                }
            )

    return {
        "leg": leg.leg,
        "config": leg.config_dict(),
        "budget": E3_TOKEN_BUDGET,
        "g_gov": gov_rows,
        "g_neg": neg_rows,
    }


def run_legs(ledger: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    """Execute every leg (A, B0, B) in fixed order under isolated stores."""
    own_tmp: tempfile.TemporaryDirectory[str] | None = None
    if root is None:
        own_tmp = tempfile.TemporaryDirectory(prefix="mnemos-e3-")
        root = Path(own_tmp.name)
    try:
        return {leg.leg: run_leg(leg, ledger, root / f"leg-{leg.leg}") for leg in LEGS}
    finally:
        if own_tmp is not None:
            own_tmp.cleanup()


# ── paired outcomes (E0 §6.1) ─────────────────────────────────────────────────


def build_outcomes(ledger: dict[str, Any], leg_results: dict[str, Any]) -> dict[str, Any]:
    """Paired per-query outcomes + McNemar discordance tallies.

    Counts and proportions only — no p-values, no CIs, no verdicts at
    run time (E0 §6.6 single-look: the registered analysis runs once
    per leg after data collection completes, never inside the runner).
    """
    analyzed = gt.active_analyzed_queries(ledger)
    leg_names = [leg.leg for leg in LEGS]

    gov_by_leg_qid = {
        leg: {row["qid"]: row for row in leg_results[leg]["g_gov"]} for leg in leg_names
    }
    pairs: list[dict[str, Any]] = []
    for query in analyzed:
        entry: dict[str, Any] = {
            "qid": query.qid,
            "family": query.family,
            "record_slug": query.record_slug,
        }
        for leg in leg_names:
            row = gov_by_leg_qid[leg][query.qid]
            if row["record_slug"] != query.record_slug:
                raise AssertionError(
                    f"pairing broken at {query.qid}: leg {leg} scored "
                    f"{row['record_slug']} vs {query.record_slug}"
                )
            entry[leg] = row["hit"]
        pairs.append(entry)

    def _discordant(first: str, second: str) -> dict[str, int]:
        return {
            f"{first}_only": sum(1 for p in pairs if p[first] and not p[second]),
            f"{second}_only": sum(1 for p in pairs if p[second] and not p[first]),
        }

    leg_rates: dict[str, Any] = {}
    for leg in leg_names:
        hits = sum(1 for p in pairs if p[leg])
        neg_rows = leg_results[leg]["g_neg"]
        insertions = sum(1 for r in neg_rows if r["false_insertion"])
        leg_rates[leg] = {
            "governance_recall_at_5": hits / len(pairs),
            "governance_noise_rate": insertions / len(neg_rows),
            "g_gov_hits": hits,
            "g_neg_insertions": insertions,
        }

    return {
        "pairing": (
            "per-query McNemar pairs across legs on the identical corpus build "
            "(E0 §6.1); pairing key = G-gov qid (gg-NNN-ph/pr, two phrasings "
            "per analyzed record)"
        ),
        "denominator": len(pairs),
        "legs": leg_names,
        "g_gov": {leg: leg_results[leg]["g_gov"] for leg in leg_names},
        "g_neg": {leg: leg_results[leg]["g_neg"] for leg in leg_names},
        "pairs": pairs,
        "discordance": {
            "B_vs_A": _discordant("B", "A"),
            "B_vs_B0": _discordant("B", "B0"),
        },
        "leg_rates": leg_rates,
    }


#: The exact top-level key set of a outcomes artifact. The statistics
#: ban (E0 §6.6 — no p-values/CI/verdicts at run time) is SCHEMA, not
#: convention: an artifact outside this set cannot be recorded.
_OUTCOME_TOP_KEYS: frozenset[str] = frozenset(
    {
        "pairing",
        "denominator",
        "legs",
        "g_gov",
        "g_neg",
        "pairs",
        "discordance",
        "leg_rates",
    }
)

#: The exact per-leg key set inside ``leg_rates`` (proportions + counts
#: of the raw rows — nothing else).
_LEG_RATE_KEYS: frozenset[str] = frozenset(
    {
        "governance_recall_at_5",
        "governance_noise_rate",
        "g_gov_hits",
        "g_neg_insertions",
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
    top-level key set, the exact per-leg ``leg_rates`` key set, and a
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
    for leg, rates in outcomes["leg_rates"].items():
        if set(rates) != _LEG_RATE_KEYS:
            raise AssertionError(
                f"leg_rates[{leg!r}] keys must be exactly {sorted(_LEG_RATE_KEYS)} — "
                f"got {sorted(rates)}"
            )
    if outcomes["denominator"] != 96:
        raise AssertionError("outcomes denominator must be 96")
    if len(outcomes["pairs"]) != 96:
        raise AssertionError("pairs must cover the 96 analyzed queries")
    qids = [p["qid"] for p in outcomes["pairs"]]
    if len(set(qids)) != 96:
        raise AssertionError("pairing keys (qids) must be unique")
    for leg in outcomes["legs"]:
        if len(outcomes["g_gov"][leg]) != 96:
            raise AssertionError(f"leg {leg}: expected 96 G-gov rows")
        if len(outcomes["g_neg"][leg]) != 24:
            raise AssertionError(f"leg {leg}: expected 24 G-neg rows")


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


def collect_run(ledger: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Execute all legs and build (manifest, outcomes). Persists NOTHING.

    Default ledger = the committed ``adjudication_ledger.json`` (the
    adjudication state the run would run under — replacements, if any,
    change the analyzed qid set and the manifest's ledger hash).
    """
    state = ledger if ledger is not None else gt.load_ledger()
    manifest = finalize_manifest(build_manifest(state))
    leg_results = run_legs(state)
    outcomes = build_outcomes(state, leg_results)
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
        f"ledger: {manifest['ledger']['state_sha256'][:12]}… "
        f"(rejects={manifest['ledger']['rejects']}, "
        f"replacements={manifest['ledger']['replacements']})"
    )
    print(
        f"corpus: e2-gov={manifest['corpus']['e2_gov_corpus_fingerprint'][:12]}… "
        f"s1={manifest['corpus']['s1_corpus_fingerprint'][:12]}…"
    )
    for leg in outcomes["legs"]:
        rates = outcomes["leg_rates"][leg]
        print(
            f"leg {leg:>2}: governance-recall@5 = "
            f"{rates['governance_recall_at_5']:.4f} ({rates['g_gov_hits']}/96)   "
            f"governance-noise-rate = {rates['governance_noise_rate']:.4f} "
            f"({rates['g_neg_insertions']}/24)"
        )
    for comparison, tally in outcomes["discordance"].items():
        print(f"discordance {comparison}: {tally}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="E3 lanes runner — legs A/B/B0 over G-gov/G-neg (E0 §1.1)"
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help=(
            "write the run artifacts (write-once) under the runs directory; "
            "the DEFAULT refuses to record — the first real E3 run is a "
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
        "e3: collecting legs A/B/B0 over the G-gov/G-neg strata (deterministic)…", file=sys.stderr
    )
    manifest, outcomes = collect_run()

    if not args.quiet:
        _print_summary(manifest, outcomes)

    if not args.record:
        print(
            "e3: REFUSING to record — collect-only. The first real E3 run is a "
            "deliberate human decision (E0 anti-HARKing window); re-run with "
            "--record to persist this run.",
            file=sys.stderr,
        )
        return 0

    try:
        run_dir = record_run(manifest, outcomes, args.runs_dir)
    except FileExistsError as exc:
        print(f"e3: FAIL — {exc}", file=sys.stderr)
        return 1
    print(f"e3: run recorded (write-once) → {run_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
