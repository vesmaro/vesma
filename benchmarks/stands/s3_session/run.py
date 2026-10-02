#!/usr/bin/env python
"""S3 long-lived-session stand — single-command runner (ADR-0020 BF-3).

Usage (from the repository root):

    python benchmarks/stands/s3_session/run.py            # gate mode
    python benchmarks/stands/s3_session/run.py --record   # write the baseline
    make bench-s3                                          # same, via make

The stand answers the COHERENCE question (ADR-0020 family F5 + the F3/F4
cuts ADR-0020 assigns to S3): does a memory keep serving an agent that
has been talking to it for hundreds of turns? A seeded simulation
(``scenario.py`` — the session as data, logical time only, no wall-clock
metric) replays a whole agent lifecycle against ONE long-lived manager:

* fact writes with unique markers, past-fact searches (exact phrase and
  paraphrase), budgeted ``assemble_context`` (the ``pre_llm_call``
  shape), periodic ``on_context_rewrite`` events, checkpoint → new
  session → ``recall_context`` round-trips;
* ``fact-retention@N,k`` (F5) — the share of facts written ~N turns ago
  that a top-k search still retrieves;
* ``recall-drift-over-session`` (F5) — the same early-fact sample
  probed at ~1/3 and at the end of the run; delta < 0 = degradation;
* ``checkpoint-return-integrity`` (F5) — the binary invariant: after
  every checkpoint/restore, every fact so far is still retrievable;
* ``sufficiency@task`` (F3/F2) — the share of a task's REQUIRED facts
  that land in the ASSEMBLED context block (not merely in search);
* ``context-growth-factor`` (F3) — assembled size at a fixed budget and
  fixed query, start vs end of session (a composition-purity stop
  signal per ADR-0020);
* ``stage-discard-profile`` (F4) — which assemble stage discarded how
  many blocks, aggregated from the assemble stage telemetry.

Gate policy (ADR-0020): the checkpoint invariant blocks ALWAYS;
retention / drift / sufficiency / growth carry derived corridors
(``baseline - max(0.02; 95% CI)`` floors, growth a +0.02 ceiling). The
stage-discard profile is recorded informational in baseline v1 (no
second wave to derive a corridor from yet). NOT wired into ``make
verify`` — S3 is nightly-class per ADR-0020 (``make bench-s3`` +
determinism smoke tests are the local surface). Baseline:
``benchmarks/baselines/s3.json`` (schema of s1/s4); reports:
``benchmarks/reports/s3-<stamp>.json``.

Determinism: BLAKE2b lexical embedder (no ONNX, no network), seeded
scenario, fixed op order, and NO wall-clock value enters any metric —
``created`` is run metadata. Memory ids (uuid4) never enter metrics:
every attribution goes through content markers.

The context-rewrite per-minute quotas are DISABLED in the stand
settings: they are wall-clock quotas and the stand compresses a
multi-hour logical session into seconds of real time — a faithful
logical workload would trip a quota designed for real minutes.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from statistics import NormalDist
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.corpus import deterministic_embedder as embedder_mod  # noqa: E402
from benchmarks.corpus.deterministic_embedder import LexicalHashEmbedder  # noqa: E402
from benchmarks.stands.s1_quality.model_contour import (  # noqa: E402
    model_fingerprint as production_model_fingerprint,
)
from benchmarks.stands.s3_session import scenario as scenario_mod  # noqa: E402
from benchmarks.stands.s3_session.scenario import (  # noqa: E402
    ANCHOR_TOPIC,
    AssembleTurn,
    CheckpointTurn,
    RewriteTurn,
    Scenario,
    ScenarioConfig,
    SearchProbe,
    WriteFact,
    build_scenario,
)
from vesmaro.config import Settings  # noqa: E402
from vesmaro.manager import MemoryManager  # noqa: E402
from vesmaro.models import (  # noqa: E402
    MemoryCreate,
    MemorySource,
    MemoryStatus,
    MemoryType,
)

STAND_VERSION = "s3-1"
BASELINE_VERSION = 1
BASELINE_PATH = ROOT / "benchmarks" / "baselines" / "s3.json"
REPORTS_DIR = ROOT / "benchmarks" / "reports"

#: ADR-0020 corridor rule (mirrors the S1/S4 runners).
CORRIDOR_FLOOR = 0.02

_PROJECT = "s3-session"
_AGENT = "s3-agent"

DEFAULT_CONFIG = ScenarioConfig()


def session_settings(root: Path) -> Settings:
    """Settings of the simulated session's store rooted at ``root``.

    Production defaults everywhere except: no background scanner (the
    stand owns every write), and the context-rewrite per-minute quotas
    disabled — see the module docstring for the logical-time rationale.
    """
    settings = Settings.model_validate(
        {
            "vesma": {
                "vault_path": str(root / "vault"),
                "data_dir": str(root / "data"),
                "db_name": "s3-session.db",
                "context_rewrite_rate_limit_per_minute": 0,
                "context_rewrite_project_rate_limit_per_minute": 0,
            },
            "scanner": {"enabled": False},
        }
    )
    settings.resolve_paths()
    return settings


def corpus_fingerprint() -> str:
    """sha256 over the scenario-defining module bytes (fixed order).

    The S3 "corpus" is the scenario generator itself — an edit to the
    world vocabularies or the op scheduling changes every metric and
    must fail the gate until a same-PR ``--record`` (ADR-0020
    event-driven re-baselining).
    """
    digest = hashlib.sha256()
    for module in (scenario_mod, embedder_mod):
        assert module.__file__ is not None
        path = Path(module.__file__)
        digest.update(path.name.encode())
        digest.update(b"\x00")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _ci95(values: list[float]) -> float:
    """Half-width of the normal 95% CI of a mean over 0/1 outcomes."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return NormalDist().inv_cdf(0.975) * (var**0.5) / n**0.5


# ── Session execution ─────────────────────────────────────────────────────────


class _SessionRun:
    """Executes one scenario against one long-lived manager."""

    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.retention: list[dict[str, Any]] = []  # {age, mode, hit}
        self.drift: dict[str, list[int]] = {"drift_early": [], "drift_late": []}
        self.checkpoints: list[dict[str, Any]] = []
        self.tasks: list[dict[str, Any]] = []
        self.growth: dict[str, dict[str, int]] = {}
        self.stage: dict[str, Any] = {
            "assembles": 0,
            "recall_candidates_total": 0,
            "content_type_filtered_total": 0,
            "scan_blocks_refused": 0,
            "budget_blocks_included": 0,
            "budget_blocks_skipped": 0,
            "filter_profiles": {},
            "align_blocks_aligned": 0,
            "align_moved_chars": 0,
        }
        self.rewrites = {"events": 0, "markers_minted": 0}
        self.epoch = 0  # a checkpoint/restore opens a NEW session epoch
        self._fact_by_marker = {f.marker: f for f in scenario.facts}

    # -- primitives -------------------------------------------------------

    def _search_hit(self, marker: str, query: str, mgr: MemoryManager, k: int) -> bool:
        results = mgr.search(query, project=_PROJECT, limit=k)
        return any(marker in r.memory.effective_content() for r in results[:k])

    def _record_assemble(self, block: dict[str, Any]) -> None:
        stats = block["stats"]
        st = self.stage
        st["assembles"] += 1
        st["recall_candidates_total"] += int(stats["recall"]["candidates"])
        st["content_type_filtered_total"] += int(stats["recall"].get("content_type_filtered", 0))
        st["scan_blocks_refused"] += int(stats["scan"]["blocks_refused"])
        st["budget_blocks_included"] += int(stats["budget"]["blocks_included"])
        st["budget_blocks_skipped"] += int(stats["budget"]["blocks_skipped"])
        for profile, count in (stats["filter"].get("profiles") or {}).items():
            st["filter_profiles"][profile] = st["filter_profiles"].get(profile, 0) + int(count)
        st["align_blocks_aligned"] += int(stats["align"]["blocks_aligned"])
        st["align_moved_chars"] += int(stats["align"]["moved_chars"])

    # -- op handlers --------------------------------------------------------

    def _run_write(self, mgr: MemoryManager, op: WriteFact) -> None:
        memory = mgr.add(
            MemoryCreate(
                content=op.fact.content,
                tags=[f"project:{_PROJECT}", f"agent:{_AGENT}", "s3-fact"],
                source=MemorySource.MCP,
                status=MemoryStatus.PUBLISHED,
            ),
            project=_PROJECT,
            agent=_AGENT,
        )
        if memory.status != MemoryStatus.PUBLISHED:
            # A demoted direct-seed means the synthetic content tripped a
            # danger detector — a fixture bug, not a memory behaviour.
            raise AssertionError(
                f"fact fixture tripped the ingest gate ({op.fact.marker} → "
                f"{memory.status.value}); rewrite the synthetic content, never "
                "patch the gate"
            )

    def _run_search(self, mgr: MemoryManager, turn: int, op: SearchProbe, k: int) -> None:
        fact = self._fact_by_marker[op.fact_marker]
        query = fact.exact_query if op.mode == "exact" else fact.paraphrase_query
        hit = self._search_hit(op.fact_marker, query, mgr, k)
        if op.purpose == "retention":
            self.retention.append({"age": turn - fact.turn, "mode": op.mode, "hit": hit})
        else:  # drift_early / drift_late
            self.drift[op.purpose].append(1 if hit else 0)

    def _run_assemble(self, mgr: MemoryManager, turn: int, op: AssembleTurn) -> None:
        block = mgr.assemble_context(
            session=f"s3-epoch-{self.epoch}",
            project=_PROJECT,
            query=op.query,
            budget=op.budget,
        )
        self._record_assemble(block)
        if op.purpose == "task":
            found = sum(1 for m in op.required if m in block["text"])
            self.tasks.append({"turn": turn, "required": len(op.required), "found": found})
        elif op.purpose in ("growth_early", "growth_late"):
            self.growth[op.purpose] = {
                "tokens": int(block["tokens"]["estimated"]),
                "blocks": len(block["blocks"]),
                "chars": len(block["text"]),
            }

    def _run_rewrite(self, mgr: MemoryManager, op: RewriteTurn) -> None:
        receipt = mgr.context_rewrite(
            content=op.content,
            project=_PROJECT,
            agent=_AGENT,
            session=f"s3-epoch-{self.epoch}",
            include_marker=True,
        )
        self.rewrites["events"] += 1
        marker = receipt.get("ccr_marker", {})
        if marker.get("cached") is True and marker.get("hash"):
            self.rewrites["markers_minted"] += 1
        else:
            raise AssertionError(
                f"rewrite fixture broken: {op.marker} block was not CCR-cached "
                "— every later redemption would silently count as a miss; "
                "lengthen the block, do not lower ccr.min_size_chars"
            )

    def _run_checkpoint(self, mgr: MemoryManager, op: CheckpointTurn, k: int) -> None:
        # save — the mnemos_save_context semantics (MCP/REST parity)
        mgr.add(
            MemoryCreate(
                content=op.content,
                tags=[f"project:{_PROJECT}", "agent:user", "mnemos:checkpoint"],
                source=MemorySource.MCP,
                memory_type=MemoryType.SESSION_CONTEXT,
            ),
            project=_PROJECT,
            agent="user",
        )
        # restore — a NEW session asks for its checkpoints back
        restored = mgr.recall_context(project=_PROJECT, limit=5)
        restored_ok = any(op.checkpoint_id in m.effective_content() for m in restored)
        # every fact so far must STILL be retrievable after the round-trip
        misses = 0
        for marker in op.audit_markers:
            fact = self._fact_by_marker[marker]
            if not self._search_hit(marker, fact.exact_query, mgr, k):
                misses += 1
        self.checkpoints.append(
            {
                "checkpoint_id": op.checkpoint_id,
                "restored": restored_ok,
                "probed": len(op.audit_markers),
                "misses": misses,
            }
        )
        self.epoch += 1  # the restore opens a new session epoch

    # -- driver -------------------------------------------------------------

    def run(self, mgr: MemoryManager) -> None:
        k = self.scenario.config.k
        for turn, op in enumerate(self.scenario.ops):
            if isinstance(op, WriteFact):
                self._run_write(mgr, op)
            elif isinstance(op, SearchProbe):
                self._run_search(mgr, turn, op, k)
            elif isinstance(op, AssembleTurn):
                self._run_assemble(mgr, turn, op)
            elif isinstance(op, RewriteTurn):
                self._run_rewrite(mgr, op)
            elif isinstance(op, CheckpointTurn):
                self._run_checkpoint(mgr, op, k)
            else:  # pragma: no cover — the scenario emits a closed op set
                raise TypeError(f"unknown turn op: {type(op).__name__}")


# ── Metric assembly ───────────────────────────────────────────────────────────


def _rate_entry(outcomes: list[int]) -> dict[str, Any]:
    """probes / hits / rate / ci95 over a 0/1 outcome list."""
    n = len(outcomes)
    if n == 0:
        return {"probes": 0, "hits": 0, "rate": None, "ci95": None}
    hits = sum(outcomes)
    return {
        "probes": n,
        "hits": hits,
        "rate": round(hits / n, 6),
        "ci95": round(_ci95([float(o) for o in outcomes]), 6),
    }


def _bucket_of(age: int, ages: tuple[int, ...]) -> int:
    return min(ages, key=lambda n: (abs(age - n), n))


def build_metrics(scenario: Scenario, run: _SessionRun) -> dict[str, Any]:
    cfg = scenario.config

    # fact-retention@N,k — histogram by age + exact/paraphrase split
    by_age: dict[str, list[int]] = {str(n): [] for n in cfg.ages}
    by_mode: dict[str, list[int]] = {"exact": [], "paraphrase": []}
    all_outcomes: list[int] = []
    age_sum: dict[str, int] = {str(n): 0 for n in cfg.ages}
    for entry in run.retention:
        outcome = 1 if entry["hit"] else 0
        all_outcomes.append(outcome)
        by_mode[entry["mode"]].append(outcome)
        bucket = str(_bucket_of(entry["age"], cfg.ages))
        by_age[bucket].append(outcome)
        age_sum[bucket] += entry["age"]
    by_age_metrics = {
        bucket: {
            **_rate_entry(outcomes),
            "mean_age": round(age_sum[bucket] / len(outcomes), 1) if outcomes else None,
        }
        for bucket, outcomes in by_age.items()
    }

    # recall-drift — the same early-fact sample, early vs late
    early, late = run.drift["drift_early"], run.drift["drift_late"]
    drift = {
        "sample": min(len(early), len(late)),
        "early": _rate_entry(early),
        "late": _rate_entry(late),
        "delta": None,
        "note": (
            "delta = late - early over the SAME early-fact sample; negative "
            "values are degradation (F5 recall-drift-over-session)"
        ),
    }
    if early and late and drift["early"]["rate"] is not None and drift["late"]["rate"] is not None:
        drift["delta"] = round(drift["late"]["rate"] - drift["early"]["rate"], 6)

    # checkpoint-return-integrity — the binary invariant (= 1.000)
    probed = sum(c["probed"] for c in run.checkpoints)
    misses = sum(c["misses"] for c in run.checkpoints)
    restores_ok = sum(1 for c in run.checkpoints if c["restored"])
    integrity_ok = bool(run.checkpoints) and misses == 0 and restores_ok == len(run.checkpoints)
    integrity = {
        "value": 1.0 if integrity_ok else 0.0,
        "expect": 1.0,
        "ok": integrity_ok,
        "checkpoints": len(run.checkpoints),
        "restores_ok": restores_ok,
        "facts_probed": probed,
        "misses": misses,
        "note": (
            "after every save_context → recall_context round-trip every fact "
            "so far is re-probed by search; any loss breaks the invariant"
        ),
    }

    # sufficiency@task — required facts IN the assembled block
    required_total = sum(t["required"] for t in run.tasks)
    found_total = sum(t["found"] for t in run.tasks)
    sufficiency = {
        "tasks": len(run.tasks),
        "required": required_total,
        "found": found_total,
        "rate": round(found_total / required_total, 6) if required_total else None,
        "per_task": [
            {
                "turn": t["turn"],
                "required": t["required"],
                "found": t["found"],
            }
            for t in run.tasks
        ],
    }

    # context-growth-factor — fixed budget + query, start vs end
    early_g = run.growth.get("growth_early")
    late_g = run.growth.get("growth_late")
    growth: dict[str, Any] = {
        "budget": cfg.growth_budget,
        "query": ANCHOR_TOPIC,
        "early": early_g,
        "late": late_g,
        "factor": None,
        "note": (
            "assembled size at a FIXED budget and query (anchor topic) at ~10% "
            "vs the final turn; growth beyond the recorded factor is a "
            "composition-purity regression (ADR-0020 F3 stop-signal)"
        ),
    }
    if early_g and late_g and early_g["tokens"] > 0:
        growth["factor"] = round(late_g["tokens"] / early_g["tokens"], 6)

    return {
        "scenario": {
            "seed": cfg.seed,
            "turns": cfg.turns,
            "k": cfg.k,
            "ages": list(cfg.ages),
            "paraphrase_share": cfg.paraphrase_share,
            "facts": len(scenario.facts),
            "operations": dict(scenario.op_counts),
        },
        "fact_retention": {
            "k": cfg.k,
            "overall": _rate_entry(all_outcomes),
            "by_age": by_age_metrics,
            "by_mode": {mode: _rate_entry(v) for mode, v in by_mode.items()},
        },
        "recall_drift": drift,
        "checkpoint_return_integrity": integrity,
        "sufficiency_at_task": sufficiency,
        "context_growth": growth,
        "stage_discard_profile": {
            **run.stage,
            "note": (
                "informational in baseline v1 (ADR-0020 F4): which assemble "
                "stage discarded how many blocks; corridors attach after a "
                "second measured wave"
            ),
        },
        "rewrite_events": dict(run.rewrites),
    }


def run_measurement(config: ScenarioConfig | None = None) -> dict[str, Any]:
    """One complete deterministic S3 pass. Returns the ``metrics`` dict."""
    cfg = config or DEFAULT_CONFIG
    scenario = build_scenario(cfg)
    with tempfile.TemporaryDirectory(prefix="mnemos-s3-") as tmp:
        mgr = MemoryManager(session_settings(Path(tmp)))
        mgr._embedder = LexicalHashEmbedder()  # deterministic, no ONNX/network
        run = _SessionRun(scenario)
        try:
            run.run(mgr)
        finally:
            mgr.close()
    return build_metrics(scenario, run)


# ── Baseline + gate ───────────────────────────────────────────────────────────


def build_baseline(metrics: dict[str, Any]) -> dict[str, Any]:
    """Canonical baseline JSON (schema of s1/s4 per ADR-0020 §Canonical)."""
    return {
        "baseline_version": BASELINE_VERSION,
        "stand_version": STAND_VERSION,
        "corpus_fingerprint": corpus_fingerprint(),
        # The PRODUCTION embedder fingerprint (ADR-0021 convention) —
        # identifier-only: the run itself executes on the deterministic
        # lexical embedder; s3.json pins which model class the scenario
        # semantics were authored against (same posture as s4.json).
        "model_fingerprint": production_model_fingerprint(),
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "metrics": metrics,
        "environment": {
            "python": ".".join(str(v) for v in sys.version_info[:3]),
            "deterministic_embedder": True,
        },
    }


def _floor_failures(
    failures: list[str], label: str, current: float, base: float, margin: float
) -> None:
    floor = base - margin
    if current < floor:
        failures.append(
            f"{label}={current:.4f} below corridor {floor:.4f} "
            f"(baseline {base:.4f} - max(0.02; ci {margin:.4f}))"
        )


def gate_check(current: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """ADR-0020 S3 gate: invariant exact, corridors derived.

    * ``checkpoint-return-integrity = 1.000`` blocks ALWAYS (a lost fact
      across a checkpoint round-trip is the F5 headline defect);
    * fact-retention (overall + per age bin) and sufficiency@task carry
      floor corridors; recall-drift a delta floor; context-growth a
      +0.02 ceiling;
    * a scenario-fingerprint or scenario-axes mismatch demands a
      same-PR ``--record`` (event-driven re-baselining).
    """
    failures: list[str] = []

    if baseline.get("corpus_fingerprint") != corpus_fingerprint():
        failures.append(
            "scenario fingerprint differs from the baseline — re-baseline "
            "required (--record) per ADR-0020 event-driven triggers"
        )

    base = baseline.get("metrics") or {}
    base_scenario = base.get("scenario") or {}
    for axis in ("seed", "turns"):
        if base_scenario.get(axis) != current["scenario"][axis]:
            failures.append(
                f"scenario axis {axis} differs from the recorded baseline "
                f"({base_scenario.get(axis)!r} → {current['scenario'][axis]!r}) "
                "— corridors only compare identical sessions; re-record"
            )

    inv = current["checkpoint_return_integrity"]
    if not inv["ok"]:
        failures.append(
            f"checkpoint-return-integrity={inv['value']} != 1.000 — "
            f"{inv['misses']} fact losses over {inv['checkpoints']} "
            "checkpoint/restore round-trips"
        )

    # fact-retention corridors (overall + per age bin)
    cur_ret = current["fact_retention"]
    base_ret = base.get("fact_retention") or {}
    cur_over, base_over = cur_ret["overall"], base_ret.get("overall") or {}
    if base_over.get("rate") is not None:
        if cur_over["rate"] is None:
            failures.append("fact-retention starved (no probes) — scenario regression")
        else:
            _floor_failures(
                failures,
                "fact-retention@N,k",
                float(cur_over["rate"]),
                float(base_over["rate"]),
                max(CORRIDOR_FLOOR, float(base_over.get("ci95") or 0.0)),
            )
    for bucket, cur_entry in cur_ret["by_age"].items():
        base_entry = (base_ret.get("by_age") or {}).get(bucket) or {}
        if base_entry.get("rate") is None:
            continue
        if cur_entry["rate"] is None:
            failures.append(
                f"fact-retention age bucket {bucket} starved "
                f"(baseline had {base_entry['probes']} probes) — scenario regression"
            )
            continue
        _floor_failures(
            failures,
            f"fact-retention@{bucket},k",
            float(cur_entry["rate"]),
            float(base_entry["rate"]),
            max(CORRIDOR_FLOOR, float(base_entry.get("ci95") or 0.0)),
        )

    # recall-drift delta floor
    cur_drift = current["recall_drift"]
    base_drift = base.get("recall_drift") or {}
    if base_drift.get("delta") is not None:
        if cur_drift["delta"] is None:
            failures.append("recall-drift not measurable (missing probe windows)")
        else:
            floor = float(base_drift["delta"]) - CORRIDOR_FLOOR
            if float(cur_drift["delta"]) < floor:
                failures.append(
                    f"recall-drift delta={cur_drift['delta']:+.4f} below corridor "
                    f"{floor:+.4f} (baseline {float(base_drift['delta']):+.4f} "
                    "- 0.02) — old-fact retrievability degraded over the session"
                )

    # sufficiency@task floor
    cur_suf = current["sufficiency_at_task"]
    base_suf = base.get("sufficiency_at_task") or {}
    if base_suf.get("rate") is not None:
        if cur_suf["rate"] is None:
            failures.append("sufficiency@task starved (no tasks placed)")
        else:
            _floor_failures(
                failures,
                "sufficiency@task",
                float(cur_suf["rate"]),
                float(base_suf["rate"]),
                CORRIDOR_FLOOR,
            )

    # context-growth ceiling (HIGHER is worse — the stop-signal direction)
    cur_g = current["context_growth"]
    base_g = base.get("context_growth") or {}
    if base_g.get("factor") is not None:
        if cur_g["factor"] is None:
            failures.append("context-growth-factor not measurable (missing growth probes)")
        else:
            ceiling = float(base_g["factor"]) + CORRIDOR_FLOOR
            if float(cur_g["factor"]) > ceiling:
                failures.append(
                    f"context-growth-factor={cur_g['factor']:.4f} above ceiling "
                    f"{ceiling:.4f} (baseline {float(base_g['factor']):.4f} + 0.02) "
                    "— assembled size crept upward with session length"
                )

    # stage-discard-profile: informational in v1 (recorded, not gated)

    return {"pass": not failures, "failures": failures}


# ── CLI ───────────────────────────────────────────────────────────────────────


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")


def _print_summary(metrics: dict[str, Any]) -> None:
    ret = metrics["fact_retention"]
    buckets = " ".join(
        f"@{b}={e['rate']:.3f}(n={e['probes']})"
        for b, e in ret["by_age"].items()
        if e["rate"] is not None
    )
    drift = metrics["recall_drift"]
    suf = metrics["sufficiency_at_task"]
    growth = metrics["context_growth"]
    inv = metrics["checkpoint_return_integrity"]
    print(
        "s3: retention "
        f"overall={ret['overall']['rate']:.4f} {buckets} | "
        f"drift={drift['delta']:+.4f} | "
        f"sufficiency={suf['rate'] if suf['rate'] is not None else float('nan'):.4f} | "
        f"growth-factor={growth['factor'] if growth['factor'] is not None else float('nan'):.4f} | "
        f"checkpoint-integrity={inv['value']:.3f}"
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="mnemos S3 session stand (ADR-0020)")
    parser.add_argument(
        "--record",
        action="store_true",
        help="write benchmarks/baselines/s3.json from this run",
    )
    parser.add_argument(
        "--turns",
        type=int,
        default=None,
        help="session length in turns (default: the recorded nightly shape)",
    )
    parser.add_argument("--seed", type=int, default=None, help="scenario seed")
    parser.add_argument("--quiet", action="store_true", help="only the gate verdict")
    args = parser.parse_args(argv)

    overrides: dict[str, Any] = {}
    if args.turns is not None:
        overrides["turns"] = args.turns
    if args.seed is not None:
        overrides["seed"] = args.seed
    config = ScenarioConfig(**overrides) if overrides else DEFAULT_CONFIG

    print(
        f"s3: simulating {config.turns} turns (seed={config.seed}, "
        "deterministic, logical time only)…",
        file=sys.stderr,
    )
    metrics = run_measurement(config)
    baseline = build_baseline(metrics)

    if args.record or not BASELINE_PATH.exists():
        _write_json(BASELINE_PATH, baseline)
        verdict: dict[str, Any] = {"pass": True, "failures": [], "mode": "record"}
        print(f"s3: baseline recorded → {BASELINE_PATH}", file=sys.stderr)
    else:
        recorded = json.loads(BASELINE_PATH.read_text())
        verdict = gate_check(metrics, recorded)
        stamp = baseline["created"].replace(":", "").replace("-", "").replace("+", "_")
        report_path = REPORTS_DIR / f"s3-{stamp}.json"
        _write_json(report_path, {**baseline, "gate": verdict})
        print(f"s3: report → {report_path}", file=sys.stderr)

    if verdict["pass"]:
        if not args.quiet:
            print("s3: gate PASS — invariant holds, corridors hold")
            _print_summary(metrics)
        return 0
    for failure in verdict["failures"]:
        print(f"s3: FAIL — {failure}", file=sys.stderr)
    print("s3: gate FAIL", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
