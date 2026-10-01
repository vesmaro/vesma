#!/usr/bin/env python
"""Canonical benchmark report generator — one wave, one artefact.

Reads every run report in ``benchmarks/reports/`` (``s1-*.json`` gate runs,
``nm1b-*.json`` / ``nm1-eval-*.json`` distillation evals) plus the canonical
``benchmarks/baselines/s1.json``, renders PNG charts (matplotlib, training
side only — ADR-0021) and writes a one-file English analysis:

    benchmarks/reports/canonical/<timestamp>-report.md
    benchmarks/reports/canonical/<timestamp>-metrics.png   (bar chart)
    benchmarks/reports/canonical/<timestamp>-epochs.png    (KD dynamics)
    benchmarks/reports/canonical/<timestamp>-rounds.png    (recall by wave)
    benchmarks/reports/canonical/<timestamp>-cosine.png    (distribution)

RETENTION POLICY (owner directive, round 3): after the canonical report is
written, the intermediate ``*.json`` run reports in ``benchmarks/reports/``
are DELETED — only ``canonical/`` (plus breadcrumbs) remains. ``--no-prune``
disables; ``--keep-last N`` (default 5) keeps the N newest run JSONs as
breadcrumbs. ``latest-prev.json`` is exempt (it is the BF-4 trend snapshot
``make bench-report`` reads back, not a run report).

Every chart is best-effort: missing data degrades to a textual note in the
report, never a crash (the fail-honest pattern of the rest of the bench
infrastructure).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

REPORTS_DIR = Path(__file__).resolve().parent
BASELINES_DIR = REPORTS_DIR.parent / "baselines"
CANONICAL_DIR = REPORTS_DIR / "canonical"
RUNS_DIR = REPO_ROOT / "training" / "runs"

#: Not run reports: BF-4 machinery the page generator reads back.
PRUNE_EXEMPT = frozenset({"latest-prev.json"})

_STAND_PREFIXES = ("s1-", "s2-", "s3-", "s4-")
_EVAL_PREFIXES = ("nm1b-", "nm1-eval-", "nm1-")


# ── loading ──────────────────────────────────────────────────────────────────


def load_run_reports(reports_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Every parseable *.json run report, oldest-first by mtime."""
    out: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(reports_dir.glob("*.json")):
        if path.name in PRUNE_EXEMPT:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warn: skipping unparsable {path.name}: {exc}", file=sys.stderr)
            continue
        if not isinstance(payload, dict):
            continue
        out.append((path, payload))
    out.sort(key=lambda pair: pair[0].stat().st_mtime)
    return out


def stand_reports(
    reports: list[tuple[Path, dict[str, Any]]], stand: str
) -> list[tuple[Path, dict[str, Any]]]:
    prefix = f"{stand}-"
    return [
        (path, payload)
        for path, payload in reports
        if path.name.startswith(prefix) and isinstance(payload.get("metrics"), dict)
    ]


def eval_reports(reports: list[tuple[Path, dict[str, Any]]]) -> list[tuple[Path, dict[str, Any]]]:
    """nm1b-/nm1- distillation eval reports (have a retrieval_proxy)."""
    return [
        (path, payload)
        for path, payload in reports
        if path.name.startswith(_EVAL_PREFIXES) and isinstance(payload.get("retrieval_proxy"), dict)
    ]


def freshest(
    reports: list[tuple[Path, dict[str, Any]]],
) -> tuple[Path, dict[str, Any]] | None:
    if not reports:
        return None
    return max(reports, key=lambda pair: pair[1].get("created") or "")


def load_baseline_s1(baselines_dir: Path) -> dict[str, Any] | None:
    path = baselines_dir / "s1.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warn: cannot read baseline {path}: {exc}", file=sys.stderr)
        return None


def load_epoch_metrics(runs_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Per-run epoch lines from training/runs/*/metrics.jsonl."""
    out: dict[str, list[dict[str, Any]]] = {}
    if not runs_dir.is_dir():
        return out
    for run in sorted(runs_dir.iterdir()):
        metrics = run / "metrics.jsonl"
        if not metrics.is_file():
            continue
        lines: list[dict[str, Any]] = []
        for raw in metrics.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(record.get("epoch"), int):
                lines.append(record)
        if lines:
            out[run.name] = sorted(lines, key=lambda r: r["epoch"])
    return out


# ── chart data extraction ────────────────────────────────────────────────────

_BAR_METRICS = (
    ("recall@5", ("recall_at_5", "recall@5")),
    ("precision@5", ("precision_at_5", "precision@5")),
    ("MRR", ("mrr",)),
    ("nDCG@10", ("ndcg_at_10", "ndcg@10")),
)


def _first_key(block: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = block.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def bar_matrix(
    baseline_s1: dict[str, Any] | None,
    s1_fresh: dict[str, Any] | None,
    nm1b: dict[str, Any] | None,
) -> dict[str, dict[str, float | None]]:
    """{model label: {metric: value|None}} for the grouped bar chart.

    Sources are honest about what each contour measures:
    - student (S1m) — the PRODUCTION embedder (vesma-embed-v1) measured by
      the s1 stand (freshest report, else the canonical baseline);
    - teacher / student-onnx / BM25 — the nm1b eval jig legs (recall@5 only).
    """
    matrix: dict[str, dict[str, float | None]] = {}
    s1m_src = None
    for candidate in (s1_fresh, baseline_s1):
        s1m = (candidate.get("metrics") or {}).get("s1m") if candidate else None
        if isinstance(s1m, dict) and s1m.get("status") == "measured":
            s1m_src = s1m
            break
    if s1m_src:
        m = s1m_src.get("metrics") or {}
        matrix["student (S1m, production)"] = {
            label: _first_key(m, keys) for label, keys in _BAR_METRICS
        }
    if nm1b:
        proxy = nm1b.get("retrieval_proxy") or {}
        teacher = proxy.get("teacher") or {}
        student = proxy.get("student-onnx") or {}
        bm25 = _first_key(teacher, ("bm25_recall@5",))
        if isinstance(teacher.get("recall@5"), (int, float)):
            matrix["teacher (eval jig)"] = {
                "recall@5": float(teacher["recall@5"]),
                "precision@5": None,
                "MRR": None,
                "nDCG@10": None,
            }
        if isinstance(student.get("recall@5"), (int, float)):
            matrix["student (ONNX, eval jig)"] = {
                "recall@5": float(student["recall@5"]),
                "precision@5": None,
                "MRR": None,
                "nDCG@10": None,
            }
        if bm25 is not None:
            matrix["BM25 (lexical)"] = {
                "recall@5": bm25,
                "precision@5": None,
                "MRR": None,
                "nDCG@10": None,
            }
    return matrix


def round_progression(
    evals: list[tuple[Path, dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Chronological recall@5 legs across eval reports (rounds/epochs)."""
    ordered = sorted(evals, key=lambda pair: pair[1].get("created") or "")
    rows: list[dict[str, Any]] = []
    for path, payload in ordered:
        proxy = payload.get("retrieval_proxy") or {}
        row: dict[str, Any] = {
            "label": payload.get("label") or path.stem,
            "created": payload.get("created") or "",
            "teacher": _first_key(proxy.get("teacher") or {}, ("recall@5",)),
            "student": _first_key(proxy.get("student-onnx") or {}, ("recall@5",)),
            "bm25": _first_key(proxy.get("teacher") or {}, ("bm25_recall@5",)),
        }
        if row["teacher"] is not None or row["student"] is not None:
            rows.append(row)
    return rows


def cosine_distribution(nm1b: dict[str, Any] | None) -> dict[str, float] | None:
    if not nm1b:
        return None
    cos = nm1b.get("cosine_student_vs_teacher") or {}
    dist = cos.get("distribution")
    if isinstance(dist, dict) and dist:
        return {str(k): float(v) for k, v in dist.items() if isinstance(v, (int, float))}
    return None


def traffic_lights(baselines_dir: Path, reports_dir: Path) -> dict[str, dict[str, Any]] | None:
    """F1-F7 evaluation reusing the BF-4 evaluators (benchmarks/report_page)."""
    try:
        from benchmarks import report_page
    except ImportError as exc:
        print(f"warn: report_page import failed: {exc}", file=sys.stderr)
        return None
    baselines = report_page.load_baselines(baselines_dir)
    if not any(stand in baselines for stand in ("s1", "s3", "s4")):
        return None
    fresh = report_page.load_fresh_reports(reports_dir, baselines)
    evaluators = getattr(report_page, "_FAMILY_EVALUATORS", None)
    domains = getattr(report_page, "_FAMILY_DOMAINS", {})
    if not evaluators:
        return None
    out: dict[str, dict[str, Any]] = {}
    for fid, evaluate in evaluators.items():
        try:
            section = evaluate(baselines, fresh)
        except Exception as exc:  # a family evaluator must never kill the page
            print(f"warn: {fid} evaluator failed: {exc}", file=sys.stderr)
            continue
        out[fid] = {"light": section.get("light"), "domain": domains.get(fid, "")}
    return out


# ── charts (matplotlib, optional) ─────────────────────────────────────────────


def _matplotlib() -> Any | None:
    try:
        import matplotlib

        matplotlib.use("Agg")  # headless
        return matplotlib
    except ImportError:
        return None


def chart_bar(matrix: dict[str, dict[str, float | None]], out: Path) -> bool:
    mpl = _matplotlib()
    if mpl is None:
        return False

    import matplotlib.pyplot as plt
    import numpy as np

    labels = list(matrix)
    metric_names = [label for label, _ in _BAR_METRICS]
    x = np.arange(len(metric_names))
    width = 0.8 / max(1, len(labels))
    fig, ax = plt.subplots(figsize=(10, 5.5))
    for i, label in enumerate(labels):
        values = [matrix[label].get(name) for name in metric_names]
        bars = ax.bar(
            x + i * width,
            [v if v is not None else 0.0 for v in values],
            width * 0.92,
            label=label,
        )
        for rect, v in zip(bars, values, strict=True):
            if v is not None:
                ax.annotate(
                    f"{v:.3f}",
                    (rect.get_x() + rect.get_width() / 2, rect.get_height()),
                    ha="center",
                    va="bottom",
                    fontsize=8,
                )
    ax.set_xticks(x + width * (len(labels) - 1) / 2)
    ax.set_xticklabels(metric_names)
    ax.set_ylabel("score")
    ax.set_ylim(0, 1.05)
    ax.set_title("Retrieval quality: student vs teacher vs BM25")
    ax.legend(fontsize=8, loc="upper right")
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return True


def chart_epochs(runs: dict[str, list[dict[str, Any]]], out: Path) -> bool:
    mpl = _matplotlib()
    if mpl is None or not runs:
        return False
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 5.5))
    plotted = False
    for run, lines in runs.items():
        epochs = [line["epoch"] for line in lines]
        cos = [((line.get("val_cosine") or {}).get("cos_sim_mean")) for line in lines]
        if any(c is not None for c in cos):
            ax.plot(
                epochs,
                [c if c is not None else float("nan") for c in cos],
                marker="o",
                label=f"{run}: val cos(student,teacher)",
            )
            plotted = True
    if not plotted:
        plt.close(fig)
        return False
    ax.set_xlabel("epoch")
    ax.set_ylabel("cos-sim mean (val)")
    ax.set_title("Distillation dynamics: student-teacher alignment by epoch")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return True


def chart_rounds(rows: list[dict[str, Any]], out: Path) -> bool:
    mpl = _matplotlib()
    if mpl is None or len(rows) < 2:
        return False
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 5.5))
    x = list(range(len(rows)))
    for leg, key in (("student (ONNX)", "student"), ("teacher", "teacher"), ("BM25", "bm25")):
        pts = [(i, row[key]) for i, row in enumerate(rows) if row.get(key) is not None]
        if len(pts) >= 2:
            ax.plot([p[0] for p in pts], [p[1] for p in pts], marker="o", label=leg)
    ax.set_xticks(x)
    ax.set_xticklabels([row["label"] for row in rows], rotation=20, ha="right", fontsize=8)
    ax.set_ylabel("recall@5 (eval jig)")
    ax.set_ylim(0, 1.0)
    ax.set_title("recall@5 across eval rounds")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return True


def chart_cosine(dist: dict[str, float], out: Path) -> bool:
    mpl = _matplotlib()
    if mpl is None:
        return False
    import matplotlib.pyplot as plt

    order = ["<0.80", "0.80-0.90", "0.90-0.95", "0.95-1.00"]
    keys = [k for k in order if k in dist] + [k for k in dist if k not in order]
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.bar(keys, [dist[k] for k in keys], color="#4c72b0")
    for i, k in enumerate(keys):
        ax.annotate(f"{dist[k]:.1%}", (i, dist[k]), ha="center", va="bottom", fontsize=9)
    ax.set_ylabel("share of val pairs")
    ax.set_title("cos(student, teacher) distribution (val, latest eval)")
    ax.set_ylim(0, 1.0)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return True


# ── retention ─────────────────────────────────────────────────────────────────


def prune_run_reports(reports_dir: Path, keep_last: int) -> list[str]:
    """Delete intermediate run *.json (owner directive); keep newest N.

    ``latest-prev.json`` is exempt (BF-4 trend snapshot). Canonical/
    subdirectories and *.md companions are not touched. Returns the removed
    file names (newest-first breadcrumbs stay behind).
    """
    candidates = [p for p in reports_dir.glob("*.json") if p.name not in PRUNE_EXEMPT]
    candidates.sort(key=lambda p: (p.stat().st_mtime, p.name))
    doomed = candidates[: max(0, len(candidates) - max(0, keep_last))]
    for path in doomed:
        path.unlink()
    return [p.name for p in reversed(doomed)]


# ── report text ───────────────────────────────────────────────────────────────

_LIGHT_ICON = {"red": "🔴", "yellow": "🟡", "green": "🟢"}


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    return f"{value:.4f}" if isinstance(value, float) else str(value)


def build_markdown(
    stamp: str,
    label: str,
    matrix: dict[str, dict[str, float | None]],
    runs: dict[str, list[dict[str, Any]]],
    rounds: list[dict[str, Any]],
    dist: dict[str, float] | None,
    lights: dict[str, dict[str, Any]] | None,
    charts: dict[str, bool],
    prune_note: str,
) -> str:
    out: list[str] = [
        f"# Canonical Benchmark Report — {label} ({stamp})",
        "",
        "Sources: freshest `s1-*.json` / `nm1b-*.json` runs from "
        "`benchmarks/reports/` + canonical `benchmarks/baselines/s1.json`.",
        "",
    ]

    out += ["## Retrieval metrics (student vs teacher vs BM25)", ""]
    if charts.get("metrics"):
        out.append(f"![metrics]({stamp}-metrics.png)")
        out.append("")
    if matrix:
        metric_names = [name for name, _ in _BAR_METRICS]
        out.append("| Model | " + " | ".join(metric_names) + " |")
        out.append("|---|" + "---|" * len(metric_names))
        for model, values in matrix.items():
            cells = [
                _fmt(values[name]) if values.get(name) is not None else "—" for name in metric_names
            ]
            out.append(f"| {model} | " + " | ".join(cells) + " |")
        out.append("")
        student = matrix.get("student (S1m, production)") or {}
        teacher_r = (matrix.get("teacher (eval jig)") or {}).get("recall@5")
        student_eval = (matrix.get("student (ONNX, eval jig)") or {}).get("recall@5")
        bm25 = (matrix.get("BM25 (lexical)") or {}).get("recall@5")
        analysis: list[str] = []
        if isinstance(student.get("recall@5"), float):
            analysis.append(
                f"- Production student (S1m, vesma-embed-v1): recall@5 "
                f"{student['recall@5']:.4f}, MRR {_fmt(student.get('MRR'))}, "
                f"nDCG@10 {_fmt(student.get('nDCG@10'))}."
            )
        if isinstance(teacher_r, float) and isinstance(bm25, float):
            verdict = "above" if teacher_r > bm25 else "not above"
            dense_vs_lex = "competitive with" if teacher_r > bm25 else "below"
            analysis.append(
                f"- Teacher on judged corpus: recall@5 {teacher_r:.4f} — {verdict} "
                f"the BM25 baseline ({bm25:.4f}); the dense embedder {dense_vs_lex} "
                f"lexical search."
            )
        if isinstance(student_eval, float) and isinstance(teacher_r, float):
            gap = teacher_r - student_eval
            analysis.append(
                f"- Distilled student trails teacher by {gap:.4f} recall@5 "
                f"({student_eval:.4f} vs {teacher_r:.4f}) — "
                + (
                    "the gap is the primary round-3 target."
                    if gap > 0.1
                    else "gap within 10% — corridor is close."
                )
            )
        out += [*analysis, ""]
    else:
        out += ["- no retrieval data — no runs found.", ""]

    out += ["## Distillation dynamics (by epoch)", ""]
    if charts.get("epochs"):
        out.append(f"![epochs]({stamp}-epochs.png)")
        out.append("")
    if runs:
        for run, lines in runs.items():
            first = lines[0].get("val_cosine") or {}
            last = lines[-1].get("val_cosine") or {}
            out.append(
                f"- `{run}`: {len(lines)} epochs, cos-sim mean "
                f"{_fmt(first.get('cos_sim_mean'))} → {_fmt(last.get('cos_sim_mean'))} "
                f"(KD-loss {lines[0].get('avg_kd_loss', float('nan')):.1f} → "
                f"{lines[-1].get('avg_kd_loss', float('nan')):.1f})."
            )
        out.append("")
    else:
        out += ["- `training/runs/*/metrics.jsonl` not found.", ""]

    out += ["## recall@5 across eval rounds", ""]
    if charts.get("rounds"):
        out.append(f"![rounds]({stamp}-rounds.png)")
        out.append("")
    if rounds:
        out.append("| Round | student | teacher | BM25 |")
        out.append("|---|---|---|---|")

        def _cell(value: Any) -> str:
            return _fmt(value) if value is not None else "—"

        for row in rounds:
            out.append(
                f"| {row['label']} | {_cell(row.get('student'))} "
                f"| {_cell(row.get('teacher'))} "
                f"| {_cell(row.get('bm25'))} |"
            )
        out.append("")
    else:
        out += ["- fewer than two eval runs — no trend to plot.", ""]

    out += ["## cos(student, teacher) distribution", ""]
    if dist and charts.get("cosine"):
        out.append(f"![cosine]({stamp}-cosine.png)")
        out.append("")
        hi = dist.get("0.95-1.00", 0.0)
        near = (
            "provisional threshold (≥0.95 by mean) is close."
            if hi >= 0.5
            else "far from threshold."
        )
        out.append(
            f"- {hi:.1%} of val pairs in the ≥0.95 corridor — {near}",
        )
        out.append("")
    else:
        out += [
            "- no data: freshest eval run did not measure cosine_student_vs_teacher "
            "(requires `--student-hf` + `--run-dir` on eval_distilled.py).",
            "",
        ]

    out += ["## F1-F7 (BF-4 traffic light)", ""]
    if lights:
        out.append("| Family | Domain | Light |")
        out.append("|---|---|---|")
        for fid, info in lights.items():
            icon = _LIGHT_ICON.get(str(info.get("light")), "🟡")
            out.append(f"| {fid} | {info.get('domain', '')} | {icon} |")
        reds = [fid for fid, info in lights.items() if info.get("light") == "red"]
        out += [
            "",
            (
                "**RED families present: " + ", ".join(reds) + " — resolve before the next wave.**"
                if reds
                else "No RED families; yellows are gaps/noise (see latest.md)."
            ),
            "",
        ]
    else:
        out += ["- BF-4 evaluators unavailable — see `benchmarks/reports/latest.md`.", ""]

    out += [
        "## Conclusions and recommendations",
        "",
        "- The numbers above are a point-in-time snapshot; regression/progress "
        "verdicts are made against baseline corridors (ADR-0020), not adjacent runs.",
        "- Round 3 is prepared: Qwen3-Embedding-0.6B teacher (instruct prefix "
        "+ last-token pooling), MRL heads 64/128/256/384, real corpus via "
        "`--from-mnemos-db` — see `training/README.md`.",
        "- The primary lever on the student/teacher gap is the corpus (real store "
        "data) and a stronger teacher; MRL provides dimension flexibility "
        "without retraining.",
        "",
        f"<!-- retention: {prune_note} -->",
        "",
    ]
    return "\n".join(out)


# ── main ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="canonical benchmark report generator")
    p.add_argument("--label", default="wave", help="human label recorded in the report")
    p.add_argument("--no-prune", action="store_true", help="keep intermediate run JSONs")
    p.add_argument(
        "--keep-last",
        type=int,
        default=5,
        help="breadcrumbs: newest N run JSONs kept when pruning (default 5)",
    )
    p.add_argument("--reports-dir", type=Path, default=REPORTS_DIR)
    p.add_argument("--baselines-dir", type=Path, default=BASELINES_DIR)
    p.add_argument("--runs-dir", type=Path, default=RUNS_DIR)
    p.add_argument("--out-dir", type=Path, default=CANONICAL_DIR)
    args = p.parse_args(argv)

    reports = load_run_reports(args.reports_dir)
    baseline_s1 = load_baseline_s1(args.baselines_dir)
    s1_fresh_payload = None
    s1_fresh = freshest(stand_reports(reports, "s1"))
    if s1_fresh is not None:
        s1_fresh_payload = s1_fresh[1]
    nm1b = freshest(eval_reports(reports))
    if not reports and baseline_s1 is None:
        print(
            "generate-report: no run reports and no s1 baseline — nothing to report",
            file=sys.stderr,
        )
        return 1

    matrix = bar_matrix(baseline_s1, s1_fresh_payload, nm1b[1] if nm1b else None)
    runs = load_epoch_metrics(args.runs_dir)
    rounds = round_progression(eval_reports(reports))
    dist = cosine_distribution(nm1b[1] if nm1b else None)
    lights = traffic_lights(args.baselines_dir, args.reports_dir)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    charts: dict[str, bool] = {}
    charts["metrics"] = chart_bar(matrix, args.out_dir / f"{stamp}-metrics.png")
    charts["epochs"] = chart_epochs(runs, args.out_dir / f"{stamp}-epochs.png")
    charts["rounds"] = chart_rounds(rounds, args.out_dir / f"{stamp}-rounds.png")
    charts["cosine"] = chart_cosine(dist, args.out_dir / f"{stamp}-cosine.png") if dist else False
    if not any(charts.values()):
        print(
            "warn: matplotlib unavailable — markdown-only report (see training/requirements.txt)",
            file=sys.stderr,
        )

    prune_note = "prune skipped (--no-prune)"
    if not args.no_prune:
        removed = prune_run_reports(args.reports_dir, args.keep_last)
        kept = [
            x.name for x in sorted(args.reports_dir.glob("*.json")) if x.name not in PRUNE_EXEMPT
        ]
        prune_note = f"pruned {len(removed)} run JSONs; breadcrumbs kept: " + (
            ", ".join(kept) if kept else "none"
        )
        print(f"generate-report: {prune_note}", file=sys.stderr)

    md_path = args.out_dir / f"{stamp}-report.md"
    md_path.write_text(
        build_markdown(stamp, args.label, matrix, runs, rounds, dist, lights, charts, prune_note),
        encoding="utf-8",
    )
    print(f"generate-report: canonical report -> {md_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
