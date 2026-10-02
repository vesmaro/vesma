"""Prometheus hybrid exposition (phase A2).

Counters ``mnemos_verb_calls_total{surface,verb,status}`` and latency
quantiles ``mnemos_verb_latency_ms{verb,quantile="0.5|0.95"}`` — read
ONLY from ``verb_metrics_hourly`` (survive restarts; scrape never
touches raw rows). ``avg`` is deliberately absent — means hide tails.

RL-S2: the exposition reads only the project-less GLOBAL rollup rows —
project/endpoint/detector labels never cross to Prometheus, and
per-principal labels are banned everywhere. Host volume gauges
(memories_total & co) stay sourced from the host's dashboard_stats()
and are passed in as plain key/value pairs.
"""

# ── PROVENANCE ────────────────────────────────────────────────────────
# Vendored from mnemos-vitals main (phase A2, 2026-09-22). Master copy
# + methodology: ~/LABs/Projects/Project-Mnemos/mnemos-vitals. Sync rule:
# changes land there first, then are ported in the same wave
# (drift-guard tests on both sides must stay green).

from __future__ import annotations

import sqlite3

from vesma.metrics.sink import MetricsStore


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def render_exposition(
    store: MetricsStore,
    *,
    gauges: dict[str, float | int] | None = None,
) -> str:
    """Render the vitals plane in Prometheus text format. Non-fatal.

    Counters sum the GLOBAL rollup rows over all retained hours
    (monotonic while rows are retained); latency quantiles are the
    most recent rolled hour's global rows (a current snapshot, not an
    average of averages). Returns ``""`` when the sidecar is
    unavailable — an empty scrape, never an exception into the scraper.
    """
    conn = store._conn()
    if conn is None:
        return ""
    try:
        lines: list[str] = [
            "# HELP mnemos_verb_calls_total Verb calls by surface, verb and status.",
            "# TYPE mnemos_verb_calls_total counter",
        ]
        for r in conn.execute(
            "SELECT surface, verb, status, SUM(count) AS total FROM verb_metrics_hourly"
            " WHERE project IS NULL GROUP BY surface, verb, status ORDER BY surface, verb"
        ):
            lines.append(
                f'mnemos_verb_calls_total{{surface="{_escape_label(r["surface"])}",'
                f'verb="{_escape_label(r["verb"])}",'
                f'status="{_escape_label(r["status"])}"}} {int(r["total"] or 0)}'
            )

        lines += [
            "# HELP mnemos_verb_latency_ms Latency quantiles (ms) of the most recent rolled hour.",
            "# TYPE mnemos_verb_latency_ms gauge",
        ]
        latest = conn.execute("SELECT MAX(hour) FROM verb_metrics_hourly").fetchone()[0]
        if latest is not None:
            for r in conn.execute(
                "SELECT verb, p50_ms, p95_ms FROM verb_metrics_hourly"
                " WHERE hour = ? AND project IS NULL ORDER BY verb",
                (latest,),
            ):
                verb = _escape_label(r["verb"])
                lines.append(
                    f'mnemos_verb_latency_ms{{verb="{verb}",quantile="0.5"}} {r["p50_ms"]}'
                )
                lines.append(
                    f'mnemos_verb_latency_ms{{verb="{verb}",quantile="0.95"}} {r["p95_ms"]}'
                )

        for name, value in sorted((gauges or {}).items()):
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {value}")
        return "\n".join(lines) + "\n"
    except sqlite3.Error:
        return ""


__all__ = ["render_exposition"]
