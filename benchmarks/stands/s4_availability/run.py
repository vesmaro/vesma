#!/usr/bin/env python
"""S4 availability stand — single-command runner (ADR-0020, wave BF-2).

Usage (from the repository root):

    python benchmarks/stands/s4_availability/run.py            # gate mode
    python benchmarks/stands/s4_availability/run.py --record   # write the baseline
    make bench-s4                                               # same, via make

The stand answers the availability question — is the WHOLE memory
correctly reachable at any moment — with strictly reading, idempotent
probes over an ISOLATED copy of a representative store:

1. fixture wave (writes, fixture store only): the stand populates a
   fresh tmp store with the ADR-0019 populations (published / refined /
   failed / quarantined / raw) and mints one CCR marker;
2. copy wave (no writes): both store databases clone to the probe root
   through the SQLite **backup API** (never a file copy — WAL-unsafe);
3. probe wave (reads only, on the copy): search both legs per
   population, exclusion probes for quarantined/raw, get-by-id (the
   quarantined row must answer the §5 retraction render), list_recent
   (quarantined absent), assemble on a budget, marker parse + redeem;
4. read-only verification: the store CONTENT counters must be identical
   before and after the probe wave — a probe that wrote anything moved
   ``memories``/``embeddings`` and fails the run (the S4 mutation this
   gate exists to catch);
5. audit-marking wave (writes, on the COPY only): one ``actor=benchmark``
   quarantine release → re-quarantine round-trip through the real
   manager, mirroring the refine/publish audit convention (outcome codes
   in the log, counters only — never content).

Metrics (ADR-0020 F6): probe-pass-rate, memory-completeness (quarantine
NOT in the denominator — its unavailability is the correct outcome),
the paired invariant quarantine-exclusion = 1.000, embed-staleness.

Gate policy: invariants and the read-only invariant block always;
probe-pass-rate / memory-completeness carry corridors vs the recorded
baseline. NOT wired into ``make verify`` (BF-2 rides the nightly
contour per ADR-0020; the make target + smoke tests are the local
surface). Baseline: ``benchmarks/baselines/s4.json`` (schema of s1);
reports: ``benchmarks/reports/s4-<stamp>.json``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.corpus import corpus as corpus_mod  # noqa: E402
from benchmarks.corpus import danger_labels as danger_labels_mod  # noqa: E402
from benchmarks.corpus import deterministic_embedder as embedder_mod  # noqa: E402
from benchmarks.corpus import queries as queries_mod  # noqa: E402
from benchmarks.stands.s1_quality.model_contour import (  # noqa: E402
    model_fingerprint as production_model_fingerprint,
)
from benchmarks.stands.s4_availability import probes as probes_mod  # noqa: E402
from benchmarks.stands.s4_availability.fixture import (  # noqa: E402
    BASELINE_VERSION,
    STAND_VERSION,
    build_fixture,
    fixture_settings,
)
from benchmarks.stands.s4_availability.store_copy import (  # noqa: E402
    clone_store,
    store_fingerprint,
)
from vesma.models import PipelineState  # noqa: E402

BASELINE_PATH = ROOT / "benchmarks" / "baselines" / "s4.json"
REPORTS_DIR = ROOT / "benchmarks" / "reports"

#: ADR-0020 corridor rule (mirrors the S1 runner).
CORRIDOR_FLOOR = 0.02

#: The admissible populations of the completeness denominator. The
#: quarantined population is deliberately ABSENT: its unavailability is
#: the CORRECT outcome (ADR-0020 corrected formula) and the paired
#: invariant quarantine-exclusion = 1.000 gates that correctness.
ADMISSIBLE_POPULATIONS: tuple[str, ...] = ("published", "refined", "failed")


def corpus_fingerprint() -> str:
    """sha256 over the corpus-defining module bytes (fixed order).

    The same corpus family the S1 baseline pins — s4.json carries the
    identical fingerprint field with the same meaning.
    """
    digest = hashlib.sha256()
    for module in (corpus_mod, queries_mod, embedder_mod, danger_labels_mod):
        assert module.__file__ is not None
        path = Path(module.__file__)
        digest.update(path.name.encode())
        digest.update(b"\x00")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _content_counts(settings: Any) -> dict[str, int]:
    """Content-table counters only (CCR LRU counters legitimately move)."""
    counts = store_fingerprint(settings)
    return {k: v for k, v in counts.items() if k in ("memories", "embeddings")}


def _audit_marking_wave(probe_root: Path, ids: dict[str, str]) -> dict[str, Any]:
    """Audit-marked lifecycle round-trip ON THE COPY (actor=benchmark).

    Follows the refine/publish audit convention: outcome codes in the
    log lines, counters only — raw values never enter the log. The
    round-trip: release the quarantined row (terminal → failed, a fresh
    retry budget), then re-quarantine it through the REAL lane-(b)
    surface. Returns the captured audit lines as evidence with the
    run-local memory ids NORMALISED to their population slug — ids are
    per-run uuid4 (not a stand property), outcomes are.
    """
    audit_lines: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            audit_lines.append(record.getMessage())

    mgr = probes_mod._new_probe_manager(probe_root)
    handler = _Capture()
    logging.getLogger("mnemos.manager").addHandler(handler)
    logging.getLogger("mnemos.pipeline.refine").addHandler(handler)
    try:
        quarantined_id = ids["quarantined"]
        released = mgr.release_quarantine(quarantined_id, source="benchmark")
        requarantined = False
        if released:
            requarantined = mgr.quarantine_entry(
                quarantined_id, reason="secret", source="benchmark"
            )
        # Normalise per-run ids → population slugs (determinism: the
        # id prefix in the log is uuid-random, the OUTCOME is the fact).
        # uuid4 has no fixed prefix, so both the full id and the 8-char
        # log slice are normalised.
        id8 = quarantined_id[:8]
        normalised = [
            line.replace(f"id={quarantined_id}", "id=quarantined").replace(
                f"id={id8}", "id=quarantined"
            )
            for line in audit_lines
        ]
        return {
            "actor": "benchmark",
            "stand_version": STAND_VERSION,
            "released": released,
            "requarantined": requarantined,
            "lines": normalised,
        }
    finally:
        mgr.close()
        logging.getLogger("mnemos.manager").removeHandler(handler)
        logging.getLogger("mnemos.pipeline.refine").removeHandler(handler)


def _embed_staleness(probe_root: Path, quarantined_id: str) -> dict[str, Any]:
    """F6 embed-staleness on the copy (read-only re-derivation).

    Admissible refined rows whose stored vector metadata ``content_hash``
    differs from the hash of the CURRENT embedding input are stale —
    the same freshness key the heal sweeper re-derives. The quarantined
    row must be absent from the vector store entirely (its embed was
    removed at quarantine; a stale one must not keep the id warm).
    """
    from mnemos.manager import MemoryManager

    from benchmarks.corpus.deterministic_embedder import LexicalHashEmbedder

    mgr = MemoryManager(fixture_settings(probe_root))
    try:
        mgr._embedder = LexicalHashEmbedder()
        rows = mgr.sqlite.list_by_pipeline_state(PipelineState.REFINED, limit=1000)
        admissible = [m for m in rows if m.pipeline_state != PipelineState.QUARANTINED]
        metas = mgr.vectors.get_metadata([m.id for m in admissible]) if admissible else {}
        stale: list[str] = []
        checked = 0
        for mem in admissible:
            checked += 1
            meta = metas.get(mem.id)
            expected = MemoryManager._embed_content_hash(MemoryManager._embedding_text(mem))
            if meta is None or meta.get("content_hash") != expected:
                stale.append(mem.id)
        return {
            "checked_refined": checked,
            "stale": len(stale),
            "stale_ids": [i[:8] for i in stale],
            "quarantined_absent_from_vectors": not mgr.vectors.has(quarantined_id),
        }
    finally:
        mgr.close()


def run_measurement() -> dict[str, Any]:
    """One complete S4 pass. Returns the ``metrics`` dict.

    Every store the pass touches lives under one TemporaryDirectory —
    the fixture store, the probe copy, nothing escapes, nothing
    persists outside ``benchmarks/``.
    """
    with tempfile.TemporaryDirectory(prefix="mnemos-s4-") as tmp:
        root = Path(tmp)
        fixture_root = root / "fixture"
        probe_root = root / "probe"

        # ── wave 1: fixture (writes on the fixture store ONLY) ────────
        fixture_settings(fixture_root)  # materialise the layout
        mgr, ids = build_fixture(fixture_root)
        try:
            marker = probes_mod.mint_fixture_marker(mgr)
        finally:
            mgr.close()
        fixture_counts = _content_counts(fixture_settings(fixture_root))

        # ── wave 2: isolated copy via the SQLite backup API ───────────
        clone_store(fixture_settings(fixture_root), fixture_settings(probe_root))

        # ── wave 3: strictly reading probes (on the copy) ─────────────
        pre_counts = _content_counts(fixture_settings(probe_root))
        result = probes_mod.run_probes(probe_root, ids, marker)
        post_counts = _content_counts(fixture_settings(probe_root))
        read_only_ok = pre_counts == post_counts

        # ── wave 4: audit-marking wave (writes, copy only) ────────────
        audit = _audit_marking_wave(probe_root, ids)

        # ── F6 population counts from the copy's real tables ──────────
        conn = sqlite3.connect(f"file:{fixture_settings(probe_root).db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT pipeline_state, status FROM memories").fetchall()
        finally:
            conn.close()
        quarantined_n = sum(1 for r in rows if r[0] == PipelineState.QUARANTINED.value)
        raw_n = sum(1 for r in rows if r[1] == "raw")

    # ── F6: availability metrics ──────────────────────────────────────
    probe_list = result["probes"]

    def _probe(name: str) -> dict[str, Any] | None:
        return next((p for p in probe_list if p["probe"] == name), None)

    # memory-completeness = |retrievable admissible| / |admissible|.
    retrievable_admissible = sum(
        1 for pop in ADMISSIBLE_POPULATIONS if (_probe(f"search-fts:{pop}") or {}).get("found")
    )
    completeness = retrievable_admissible / len(ADMISSIBLE_POPULATIONS)

    # quarantine-exclusion (paired invariant = 1.000): the quarantined
    # row is excluded from BOTH search legs, retracted on get, absent
    # from list_recent. The F6 gate applies only while it holds.
    exclusion_probes = [
        p
        for p in probe_list
        if p["probe"] in ("search-excluded:quarantined", "get-by-id", "list_recent")
    ]
    exclusion_ok = bool(exclusion_probes) and all(p.get("pass") is True for p in exclusion_probes)
    quarantine_exclusion = 1.0 if exclusion_ok else 0.0

    pass_rate = result["pass_count"] / result["total"] if result["total"] else 0.0
    embed_stale = _embed_staleness(probe_root, ids["quarantined"])

    return {
        "probe_pass_rate": round(pass_rate, 6),
        "memory_completeness": round(completeness, 6),
        "quarantine_exclusion": {
            "value": quarantine_exclusion,
            "expect": 1.0,
            "ok": exclusion_ok,
        },
        "embed_staleness": embed_stale,
        "probes": result,
        "populations": {
            "fixture": len(ids),
            "quarantined_rows": quarantined_n,
            "raw_rows": raw_n,
            "fixture_content_counts": fixture_counts,
        },
        "read_only_invariant": {
            "ok": read_only_ok,
            "content_counts_before": pre_counts,
            "content_counts_after": post_counts,
            "note": (
                "a probe that writes moves memories/embeddings — the "
                "mutation the S4 read-only invariant exists to catch"
            ),
        },
        "audit_marking": audit,
    }


def gate_check(metrics: dict[str, Any], baseline: dict[str, Any] | None) -> dict[str, Any]:
    """ADR-0020 S4 gate: invariants exact, corridors derived, budget.

    * ``quarantine_exclusion = 1.000`` blocks ALWAYS (paired invariant —
      never carried over a re-baseline);
    * the probe read-only invariant blocks ALWAYS (a writing probe is a
      stand defect and a store hazard — the gonogo mutation);
    * ``probe_pass_rate`` / ``memory_completeness`` carry derived
      corridors vs the recorded baseline (floor = baseline - 0.02);
    * embed-staleness must be 0 while the paired invariant holds.
    """
    failures: list[str] = []
    qe = metrics["quarantine_exclusion"]
    if not qe["ok"]:
        failures.append(
            f"quarantine_exclusion={qe['value']} != 1.000 — the F6 gate "
            "applies only while the paired invariant holds (ADR-0020)"
        )
    ro = metrics["read_only_invariant"]
    if not ro["ok"]:
        failures.append(
            "probe read-only invariant broken: "
            f"{ro['content_counts_before']} → {ro['content_counts_after']}"
        )

    base = (baseline or {}).get("metrics") or {}
    for name, metric in (
        ("probe_pass_rate", metrics["probe_pass_rate"]),
        ("memory_completeness", metrics["memory_completeness"]),
    ):
        if name in base:
            floor = float(base[name]) - CORRIDOR_FLOOR
            if float(metric) < floor:
                failures.append(
                    f"{name}={metric:.4f} below corridor {floor:.4f} "
                    f"(baseline {float(base[name]):.4f})"
                )
    stale = metrics["embed_staleness"]
    if qe["ok"] and (stale["stale"] > 0 or not stale["quarantined_absent_from_vectors"]):
        failures.append(
            f"embed-staleness: {stale['stale']} stale embeds / "
            f"quarantined_absent_from_vectors={stale['quarantined_absent_from_vectors']}"
        )
    return {"pass": not failures, "failures": failures}


def build_baseline(metrics: dict[str, Any]) -> dict[str, Any]:
    """Canonical baseline JSON (schema of s1 per ADR-0020 §Canonical)."""
    return {
        "baseline_version": BASELINE_VERSION,
        "stand_version": STAND_VERSION,
        "corpus_fingerprint": corpus_fingerprint(),
        # The PRODUCTION embedder fingerprint (ADR-0021 convention) —
        # identifier-only here: the probes run the deterministic lexical
        # embedder, and s4.json pins which model class the corpus
        # semantics were authored against.
        "model_fingerprint": production_model_fingerprint(),
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "metrics": metrics,
        "environment": {
            "python": ".".join(str(v) for v in sys.version_info[:3]),
            "deterministic_embedder": True,
        },
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="mnemos S4 availability stand (ADR-0020)")
    parser.add_argument(
        "--record",
        action="store_true",
        help="write benchmarks/baselines/s4.json from this run",
    )
    parser.add_argument("--quiet", action="store_true", help="only the gate verdict")
    args = parser.parse_args(argv)

    print("s4: measuring (fixture → isolated copy → read-only probes)…", file=sys.stderr)
    metrics = run_measurement()
    baseline = build_baseline(metrics)

    if args.record or not BASELINE_PATH.exists():
        _write_json(BASELINE_PATH, baseline)
        verdict: dict[str, Any] = {"pass": True, "failures": [], "mode": "record"}
        print(f"s4: baseline recorded → {BASELINE_PATH}", file=sys.stderr)
    else:
        recorded = json.loads(BASELINE_PATH.read_text())
        verdict = gate_check(metrics, recorded)
        stamp = baseline["created"].replace(":", "").replace("-", "").replace("+", "_")
        report_path = REPORTS_DIR / f"s4-{stamp}.json"
        _write_json(report_path, {**baseline, "gate": verdict})
        print(f"s4: report → {report_path}", file=sys.stderr)

    if verdict["pass"]:
        if not args.quiet:
            print(
                "s4: gate PASS — "
                f"probe-pass-rate={metrics['probe_pass_rate']:.4f} "
                f"completeness={metrics['memory_completeness']:.4f} "
                f"quarantine-exclusion={metrics['quarantine_exclusion']['value']:.1f} "
                f"read-only={'ok' if metrics['read_only_invariant']['ok'] else 'BROKEN'}"
            )
        return 0
    for failure in verdict["failures"]:
        print(f"s4: FAIL — {failure}", file=sys.stderr)
    print("s4: gate FAIL", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
