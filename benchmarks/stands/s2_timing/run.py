"""S2 timing stand — smoke mode (local) + full nightly mode (ADR-0020 BF-2/BF-4).

S2 is the ONLY wall-clock domain in the benchmark framework. ADR-0020
gate policy §3: **S2 never blocks locally** — the R=1 smoke run is
informational; the full repeat-mode (``--repeats N``) runs nightly on a
quiet isolated machine and only there becomes a corridor concern.

Nightly semantics (BF-4, ADR-0020 §5):

* ``--repeats N`` (N ≥ 3) runs N full workload passes; the spread
  between the per-repeat p50/p95 IS the measured noise band — the
  machine measures its own noise first, then the numbers;
* **NOISE de-escalation**: a noise band wider than the corridor makes
  the median-vs-baseline comparison meaningless — status NOISE,
  de-escalated to report + ticket per ADR-0020 §5, exit code 0 (never a
  block); a tight-band median breach beyond the corridor ceiling is a
  REGRESSION and exits non-zero (the nightly gate role);
* ``--record-nightly`` is the ONLY path that writes
  ``benchmarks/baselines/s2.json`` — the S2 baseline is a property of
  the nightly quiet machine and is born there (a developer-laptop
  number would poison every future corridor). Overwriting an existing
  baseline additionally requires ``--force`` (re-baselining is
  event-driven per ADR-0020 — corpus x2 / pipeline change — never a
  calendar or convenience act).

Workload (fixed, ~10³ operations): one temp store, the deterministic
lexical embedder, then measured wraps around the four core verbs:

* ``add``          — ingest write (scanner + vault + sqlite + embed);
* ``search``       — the hybrid RRF path (FTS + vector leg);
* ``assemble``     — the fixed pipeline on a budget;
* ``refine_single``— one full refine cycle (claim → artifact → swap).

Smoke results land in ``benchmarks/reports/s2-smoke-<stamp>.json``;
nightly results in ``benchmarks/reports/s2-nightly-<stamp>.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.corpus.deterministic_embedder import LexicalHashEmbedder  # noqa: E402
from benchmarks.stands.s4_availability.fixture import fixture_settings  # noqa: E402
from vesma.manager import MemoryManager  # noqa: E402
from vesma.models import MemoryCreate, MemorySource, MemoryStatus  # noqa: E402
from vesma.pipeline.refine import refine_single  # noqa: E402

STAND_VERSION = "s2-smoke-1"
NIGHTLY_STAND_VERSION = "s2-nightly-1"
BASELINE_VERSION = 1
REPORTS_DIR = ROOT / "benchmarks" / "reports"
BASELINE_PATH = ROOT / "benchmarks" / "baselines" / "s2.json"

#: Workload shape (smoke): ~10³ ops total by default. ``--repeats N``
#: (nightly) multiplies the whole measurement pass.
DEFAULT_OPS = 250  # x4 verbs ~ 1e3 operations

#: The four F1 verbs (ADR-0020 family F1) the smoke covers.
VERBS: tuple[str, ...] = ("add", "search", "assemble", "refine_single")

#: A nightly record needs at least this many repeats — a noise band
#: from two points is a difference, not a band.
NIGHTLY_MIN_REPEATS = 3

#: Timing corridor: the nightly median p50/p95 may exceed the baseline
#: by at most this factor (a real REGRESSION breach beyond it). The
#: corridor WIDTH is the same factor expressed as a relative band —
#: a measured between-repeat noise band wider than that is NOISE.
REGRESSION_CEILING = 1.25
NOISE_RELATIVE_BAND = REGRESSION_CEILING - 1.0  # 0.25 == the corridor width

_TOKENS: dict[str, str] = {
    "a": "larkspire",
    "b": "glimmerford",
    "c": "thornwick",
}


def _timed(fn: Any, *args: Any, **kwargs: Any) -> tuple[float, Any]:
    """One wall-clock sample (monotonic; the ONLY clock in the stand)."""
    t0 = time.perf_counter()
    out = fn(*args, **kwargs)
    return time.perf_counter() - t0, out


def run_workload(ops: int) -> dict[str, list[float]]:
    """The fixed workload; returns per-verb wall-clock samples (seconds)."""
    with tempfile.TemporaryDirectory(prefix="mnemos-s2-") as tmp:
        root = Path(tmp)
        mgr = MemoryManager(fixture_settings(root))
        mgr._embedder = LexicalHashEmbedder()
        samples: dict[str, list[float]] = {v: [] for v in VERBS}
        try:
            for i in range(ops):
                token = _TOKENS["abc"[i % 3]]
                content = (
                    f"{token} workload entry {i}: the ingestion path takes a "
                    "moderately sized paragraph so the vault write, the "
                    "secrets scan and the FTS index all do real work; the "
                    "vector leg embeds title plus content plus tags."
                )
                dt, mem = _timed(
                    mgr.add,
                    data=MemoryCreate(
                        content=content,
                        title=f"{token} entry {i}",
                        tags=["project:s2-workload", "agent:s2-stand", "mnemos:rule"],
                        source=MemorySource.MCP,
                        status=MemoryStatus.PUBLISHED,
                    ),
                    project="s2-workload",
                    agent="s2-stand",
                )
                samples["add"].append(dt)
                if i % 3 == 2:  # every third entry completes a refine cycle
                    dt_r, _ = _timed(refine_single, mgr, mem.id)
                    samples["refine_single"].append(dt_r)
                if i % 2 == 0:  # search after every other write
                    dt_s, _ = _timed(mgr.search, query=token, limit=10)
                    samples["search"].append(dt_s)
            for i in range(ops // 5):
                dt_a, _ = _timed(
                    mgr.assemble_context,
                    session=f"s2-smoke-{i}",
                    project="s2-workload",
                    budget=2048,
                    query=_TOKENS["abc"[i % 3]],
                )
                samples["assemble"].append(dt_a)
        finally:
            mgr.close()
        return samples


def percentile(samples: list[float], p: float) -> float:
    """Nearest-rank percentile of a sample list (seconds)."""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    idx = max(0, min(len(ordered) - 1, round(p / 100 * len(ordered)) - 1))
    return ordered[idx]


def summarize(samples: dict[str, list[float]]) -> dict[str, Any]:
    """p50/p95 per verb + honest smoke-mode marks.

    R=1 (a single pass): the percentiles are IN-SAMPLE order statistics
    of one run — informative, never a corridor. ``--repeats N`` (N > 1)
    runs N full passes and reports the per-repeat p50/p95 spread so the
    nightly harness can measure its own noise band.
    """
    out: dict[str, Any] = {}
    for verb in VERBS:
        vs = samples.get(verb, [])
        if not vs:
            out[verb] = {"n": 0, "status": "no-samples"}
            continue
        out[verb] = {
            "n": len(vs),
            "p50_ms": round(percentile(vs, 50) * 1000, 3),
            "p95_ms": round(percentile(vs, 95) * 1000, 3),
            "max_ms": round(max(vs) * 1000, 3),
            "total_s": round(sum(vs), 3),
        }
    return out


def run(repeats: int) -> dict[str, Any]:
    """One S2 run: ``repeats`` full workload passes (1 = smoke)."""
    per_repeat: list[dict[str, Any]] = []
    started = datetime.now(UTC).isoformat(timespec="seconds")
    for r in range(repeats):
        samples = run_workload(DEFAULT_OPS)
        per_repeat.append(summarize(samples))
        print(
            f"s2: repeat {r + 1}/{repeats} — "
            + " ".join(
                f"{v}={per_repeat[-1].get(v, {}).get('p50_ms', 0):.1f}ms"
                for v in VERBS
                if per_repeat[-1].get(v, {}).get("n")
            ),
            file=sys.stderr,
        )
    mode = "nightly" if repeats > 1 else "smoke"
    return {
        "stand_version": NIGHTLY_STAND_VERSION if repeats > 1 else STAND_VERSION,
        "mode": mode,
        "gating": (
            "nightly corridor vs baselines/s2.json (NOISE de-escalated per ADR-0020 §5)"
            if repeats > 1
            else "informational — S2 never blocks locally (ADR-0020 §Gate policy 3)"
        ),
        "repeats": repeats,
        "ops_per_repeat": DEFAULT_OPS,
        "total_operations": DEFAULT_OPS * 4 * repeats,
        "created": started,
        "verb_metrics": per_repeat[0]
        if repeats == 1
        else {
            "note": (
                "per-repeat summaries in 'repeats'; the spread between "
                "repeat p50/p95 IS the measured noise band"
            ),
            "repeats": per_repeat,
        },
        "environment": {
            "python": ".".join(str(v) for v in sys.version_info[:3]),
            "deterministic_embedder": True,
            "note": (
                "timing on the deterministic lexical embedder — measures the "
                "pipeline, not any model"
            ),
        },
    }


# ── nightly analysis (BF-4): noise band → NOISE / PASS / REGRESSION ──────────


def _verb_series(per_repeat: list[dict[str, Any]], verb: str, key: str) -> list[float]:
    """One per-repeat series (e.g. all repeat p50s of ``add``)."""
    out: list[float] = []
    for rep in per_repeat:
        entry = rep.get(verb)
        if isinstance(entry, dict) and entry.get("n"):
            out.append(float(entry[key]))
    return out


def analyze_nightly(
    per_repeat: list[dict[str, Any]], baseline_metrics: dict[str, Any] | None
) -> dict[str, Any]:
    """Noise-band-first corridor analysis of a nightly run (pure, testable).

    Per verb (ADR-0020 §5, order matters — noise vetoes regression):

    1. the between-repeat band (max-min of repeat p50s, p95s) is the
       MEASURED noise; a relative band wider than the corridor width
       (``NOISE_RELATIVE_BAND``) → **NOISE**: the median-vs-baseline
       comparison is meaningless, status de-escalated to report +
       ticket — never a block;
    2. a tight-band run whose median p50/p95 exceeds the recorded
       baseline by more than ``REGRESSION_CEILING`` → **REGRESSION**
       (the nightly gate role — a real breach on a quiet machine);
    3. otherwise **PASS**.

    ``baseline_metrics is None`` (no recorded baseline — the expected
    state until the first ``--record-nightly``) downgrades step 2 to a
    band-only check: wide band still NOISE (the machine is too noisy to
    RECORD a baseline from), tight band PASS with
    ``baseline: "none"``.

    Statuses are PER VERB and independent: a tight-band REGRESSION on
    one verb blocks the nightly run even while another verb reports
    NOISE — the §5 de-escalation applies to the noisy comparison, not
    to the run as a whole.
    """
    verbs: dict[str, Any] = {}
    overall = "PASS"
    order = {"PASS": 0, "NOISE": 1, "REGRESSION": 2}
    for verb in VERBS:
        p50s = _verb_series(per_repeat, verb, "p50_ms")
        p95s = _verb_series(per_repeat, verb, "p95_ms")
        if not p50s:
            verbs[verb] = {"status": "no-samples"}
            continue
        med_p50 = statistics.median(p50s)
        med_p95 = statistics.median(p95s) if p95s else 0.0
        band_p50 = max(p50s) - min(p50s)
        band_p95 = (max(p95s) - min(p95s)) if p95s else 0.0
        rel_band = band_p50 / med_p50 if med_p50 > 0 else 0.0
        rel_band_p95 = band_p95 / med_p95 if med_p95 > 0 else 0.0
        entry: dict[str, Any] = {
            "median_p50_ms": round(med_p50, 3),
            "median_p95_ms": round(med_p95, 3),
            "band_p50_ms": round(band_p50, 3),
            "band_p95_ms": round(band_p95, 3),
            "relative_band": round(rel_band, 4),
            "relative_band_p95": round(rel_band_p95, 4),
        }
        if rel_band > NOISE_RELATIVE_BAND or rel_band_p95 > NOISE_RELATIVE_BAND:
            entry["status"] = "NOISE"
            entry["why"] = (
                f"noise band p50={rel_band:.1%} p95={rel_band_p95:.1%} wider "
                f"than the corridor width {NOISE_RELATIVE_BAND:.0%} — "
                "de-escalated to report + ticket (ADR-0020 §5)"
            )
        elif baseline_metrics is None:
            entry["status"] = "PASS"
            entry["baseline"] = "none — first --record-nightly pins the corridor"
        else:
            base = (baseline_metrics.get("verbs") or {}).get(verb) or {}
            base_p50 = float(base.get("p50_ms", 0.0))
            base_p95 = float(base.get("p95_ms", 0.0))
            entry["baseline_p50_ms"] = base_p50
            entry["ratio_p50"] = round(med_p50 / base_p50, 4) if base_p50 > 0 else None
            p50_breach = base_p50 > 0 and med_p50 > base_p50 * REGRESSION_CEILING
            p95_breach = base_p95 > 0 and med_p95 > base_p95 * REGRESSION_CEILING
            if p50_breach or p95_breach:
                entry["status"] = "REGRESSION"
                entry["why"] = (
                    f"median p50 {med_p50:.2f}ms > baseline {base_p50:.2f}ms x "
                    f"{REGRESSION_CEILING} on a quiet machine"
                )
            else:
                entry["status"] = "PASS"
        verbs[verb] = entry
        if order[entry["status"]] > order[overall]:
            overall = entry["status"]
    return {
        "baseline": "none" if baseline_metrics is None else "recorded",
        "verbs": verbs,
        "overall": overall,
        "exit_code": 1 if overall == "REGRESSION" else 0,
        "policy": (
            "NOISE → report + ticket, exit 0 (de-escalated, ADR-0020 §5); "
            "REGRESSION (tight band, median beyond "
            f"baseline x {REGRESSION_CEILING}) → exit 1"
        ),
    }


def workload_fingerprint() -> str:
    """sha256 over this module's bytes — the workload IS this file.

    An edit to the workload shape (verbs, ops, content mix) changes the
    fingerprint and fails the nightly gate until a same-PR
    ``--record-nightly --force`` re-baselines it (event-driven, never
    calendar-driven — mirrors the corpus fingerprints of S1/S3/S4).
    """
    assert __file__ is not None
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def build_nightly_baseline(report: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    """The s2.json payload from a nightly run (median values + bands)."""
    verbs: dict[str, Any] = {}
    for verb, entry in analysis["verbs"].items():
        if entry.get("status") == "no-samples":
            continue
        verbs[verb] = {
            "p50_ms": entry["median_p50_ms"],
            "p95_ms": entry["median_p95_ms"],
            "band_p50_ms": entry["band_p50_ms"],
            "band_p95_ms": entry["band_p95_ms"],
            "relative_band": entry["relative_band"],
        }
    return {
        "baseline_version": BASELINE_VERSION,
        "stand_version": NIGHTLY_STAND_VERSION,
        "workload_fingerprint": workload_fingerprint(),
        "created": report["created"],
        "metrics": {
            "repeats": report["repeats"],
            "ops_per_repeat": report["ops_per_repeat"],
            "verbs": verbs,
            "noise": {
                "max_relative_band": max((v["relative_band"] for v in verbs.values()), default=0.0),
                "note": (
                    "bands are the measured max-min spread of per-repeat "
                    "p50/p95 on the nightly machine (ADR-0020 §5)"
                ),
            },
        },
        "environment": report["environment"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="mnemos S2 timing stand (ADR-0020)")
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="full-workload repeats (1 = smoke/informational; nightly uses N>1 on a quiet machine)",
    )
    parser.add_argument(
        "--record-nightly",
        action="store_true",
        help=(
            "write benchmarks/baselines/s2.json from this NIGHTLY run "
            "(requires --repeats >= 3; the S2 baseline is born on the "
            "nightly machine only)"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "with --record-nightly: overwrite an existing s2.json "
            "(event-driven re-baseline only — corpus x2 / pipeline change)"
        ),
    )
    args = parser.parse_args(argv)
    if args.repeats < 1:
        parser.error("--repeats must be >= 1")
    if args.record_nightly and args.repeats < NIGHTLY_MIN_REPEATS:
        parser.error(
            f"--record-nightly needs at least --repeats {NIGHTLY_MIN_REPEATS} "
            "(a noise band from fewer points is not a band)"
        )
    if args.force and not args.record_nightly:
        parser.error("--force only makes sense together with --record-nightly")

    report = run(args.repeats)
    stamp = report["created"].replace(":", "").replace("-", "").replace("+", "_")
    prefix = "s2-nightly" if report["mode"] == "nightly" else "s2-smoke"

    analysis: dict[str, Any] | None = None
    if report["mode"] == "nightly":
        baseline_metrics = None
        if BASELINE_PATH.exists():
            recorded = json.loads(BASELINE_PATH.read_text())
            if recorded.get("workload_fingerprint") != workload_fingerprint():
                print(
                    "s2-nightly: FAIL — workload fingerprint differs from the "
                    "recorded baseline; re-baseline required (--record-nightly "
                    "--force) in the same PR as the workload change",
                    file=sys.stderr,
                )
                return 1
            baseline_metrics = recorded.get("metrics")
        analysis = analyze_nightly(report["verb_metrics"]["repeats"], baseline_metrics)
        report["nightly_analysis"] = analysis

    report_path = REPORTS_DIR / f"{prefix}-{stamp}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n")
    print(f"s2: report → {report_path}", file=sys.stderr)

    if report["mode"] == "smoke":
        m = report["verb_metrics"]
        print(
            "s2-smoke: PASS (informational) — "
            + " ".join(
                f"{v}: p50={m[v]['p50_ms']:.2f}ms p95={m[v]['p95_ms']:.2f}ms (n={m[v]['n']})"
                for v in VERBS
                if isinstance(m.get(v), dict) and m[v].get("n")
            )
        )
        return 0

    assert analysis is not None  # nightly mode always computes it
    if args.record_nightly:
        if BASELINE_PATH.exists() and not args.force:
            print(
                "s2-nightly: FAIL — baselines/s2.json already exists; an "
                "overwrite is an event-driven re-baseline (--record-nightly "
                "--force) per ADR-0020",
                file=sys.stderr,
            )
            return 1
        if analysis["overall"] == "NOISE":
            print(
                "s2-nightly: FAIL — overall=NOISE: too noisy to RECORD from "
                "(a noisy corridor is worse than none; re-run on a quieter "
                "machine per ADR-0020 §5)",
                file=sys.stderr,
            )
            return 1
        BASELINE_PATH.write_text(
            json.dumps(build_nightly_baseline(report, analysis), indent=2) + "\n"
        )
        print(f"s2-nightly: baseline recorded → {BASELINE_PATH}", file=sys.stderr)

    overall = analysis["overall"]
    for verb, entry in analysis["verbs"].items():
        if entry.get("status", "no-samples") == "no-samples":
            continue
        print(
            f"s2-nightly: {verb}: {entry['status']} — median p50 "
            f"{entry['median_p50_ms']:.2f}ms, band {entry['band_p50_ms']:.2f}ms "
            f"({entry['relative_band']:.1%})" + (f" — {entry['why']}" if "why" in entry else "")
        )
    if overall == "NOISE":
        print(
            "s2-nightly: NOISE — the machine was not quiet enough for these "
            "verbs; de-escalated to report + ticket per ADR-0020 §5 (not a block)"
        )
    elif overall == "REGRESSION":
        print("s2-nightly: REGRESSION — corridor breach on a quiet machine")
        return 1
    else:
        print(f"s2-nightly: PASS — {report['repeats']} repeats, corridors hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
