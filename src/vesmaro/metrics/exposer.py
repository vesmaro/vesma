"""Prometheus hybrid exposition (phase A2; usage plane phase C).

Counters ``mnemos_verb_calls_total{surface,verb,status}`` and latency
quantiles ``mnemos_verb_latency_ms{verb,quantile="0.5|0.95"}`` — read
ONLY from ``verb_metrics_hourly`` (survive restarts; scrape never
touches raw rows). ``avg`` is deliberately absent — means hide tails.

RL-S2: the exposition reads only the project-less GLOBAL rollup rows —
project/endpoint/detector labels never cross to Prometheus, and
per-principal labels are banned everywhere. Host volume gauges
(memories_total & co) stay sourced from the host's dashboard_stats()
and are passed in as plain key/value pairs.

Phase C appends the usage-loop plane as GLOBAL gauges
(``mnemos_usage_*``): loop closure, wrong-tool routing, output tokens.
The kappa gate is carried into the exposition structurally —
``mnemos_usage_touched_share`` appears ONLY when
:func:`vesmaro.metrics.usage.kappa_calibration_pending` is False; while
the frozen pre-registration is unexecuted/failed (H-K0: not a v1
corridor metric) the series is absent, so a renderer can never promote
it by scraping (NO-DATA is not zero, and informational-only is not
promotable). touched analytics NEVER surface here otherwise.
"""

# ── PROVENANCE ────────────────────────────────────────────────────────
# Vendored from mnemos-vitals main (phase A2, 2026-09-22; phase C
# usage-plane block ported 2026-09-29, branch feat/phase-c-integration
# 38f9fb3). Master copy + methodology:
# ~/LABs/Projects/Project-Mnemos/mnemos-vitals. Sync rule: changes land
# there first, then are ported in the same wave (drift-guard tests on
# both sides must stay green).

from __future__ import annotations

import json
import sqlite3

from vesmaro.metrics.sink import MetricsStore
from vesmaro.metrics.usage import kappa_calibration_pending


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _usage_block(conn: sqlite3.Connection) -> list[str]:
    """Phase C usage-loop gauges — global counts only, NO-DATA absent.

    All series are project-less scalars read straight from the sidecar
    (RL-S2: no project/endpoint/detector/principal labels are even
    constructible — the source tables carry no such columns in this
    plane). Series whose denominator is zero are OMITTED, not zeroed:
    absent is the Prometheus-honest rendering of NO-DATA. The kappa
    gate is structural: ``mnemos_usage_touched_share`` (and its
    denominator) render only while calibration is NOT pending — while
    the frozen pre-registration stands (H-K0), the informational
    touched signal must not become scrapable, or the traffic-light
    renderer could read a scraped series and treat it as corridor
    evidence.
    """
    if kappa_calibration_pending():
        # touched series structurally absent — the gate cannot flip from data
        touched_lines: list[str] = []
    else:
        touched_lines = [
            "# HELP mnemos_usage_touched_share Mean share of injected blocks"
            " touched per reported call (kappa-calibrated).",
            "# TYPE mnemos_usage_touched_share gauge",
        ]
        rows = conn.execute(
            "SELECT u.metrics_id AS metrics_id, u.block_ids_touched_json AS touched_json,"
            " (SELECT COUNT(*) FROM injection_blocks b WHERE b.metrics_id = u.metrics_id)"
            " AS injected_n FROM usage_reports u ORDER BY u.id"
        ).fetchall()
        shares: list[float] = []
        for r in rows:
            if not r["injected_n"]:
                continue  # 0/0 undefined — excluded, never a zero
            try:
                touched = json.loads(r["touched_json"])
            except (ValueError, TypeError):
                continue  # unreadable garbage — excluded, never a zero
            if not isinstance(touched, list):
                continue
            injected = {f"{r['metrics_id']}:{i}" for i in range(r["injected_n"])}
            hits = len({t for t in touched if isinstance(t, str)} & injected)
            shares.append(hits / r["injected_n"])
        if shares:
            touched_lines.append(f"mnemos_usage_touched_share {sum(shares) / len(shares)}")
        else:
            touched_lines = []  # no measurable reports — NO-DATA stays absent

    total = conn.execute("SELECT COUNT(*) FROM assemble_metrics").fetchone()[0]
    if not total:
        return []  # no assemble calls at all — the whole family stays absent
    closed = conn.execute("SELECT COUNT(DISTINCT metrics_id) FROM usage_reports").fetchone()[0]
    reports = conn.execute("SELECT COUNT(*) FROM usage_reports").fetchone()[0]
    flagged = conn.execute(
        "SELECT COUNT(*) FROM usage_reports WHERE wrong_tool_flag = 1"
    ).fetchone()[0]
    tokens_out = conn.execute(
        "SELECT COALESCE(SUM(tokens_out), 0) FROM usage_reports WHERE tokens_out IS NOT NULL"
    ).fetchone()[0]
    # Header-per-series: a series with no data is OMITTED together with
    # its HELP/TYPE — the exposition never carries an orphan header.
    lines = [
        *(
            (
                "# HELP mnemos_usage_loop_rate Share of assemble calls that received"
                " >= 1 harness usage report.",
                "# TYPE mnemos_usage_loop_rate gauge",
                f"mnemos_usage_loop_rate {closed / total}",
            ),
            (
                "# HELP mnemos_usage_assemble_calls_total Assemble calls recorded.",
                "# TYPE mnemos_usage_assemble_calls_total counter",
                f"mnemos_usage_assemble_calls_total {int(total)}",
            ),
            (
                "# HELP mnemos_usage_closed_calls_total Assemble calls with"
                " a usage report (loop-closure numerator/denominator).",
                "# TYPE mnemos_usage_closed_calls_total counter",
                f"mnemos_usage_closed_calls_total {int(closed)}",
            ),
        ),
    ]
    if reports:
        lines += [
            (
                "# HELP mnemos_usage_reports_total Usage reports recorded.",
                "# TYPE mnemos_usage_reports_total counter",
                f"mnemos_usage_reports_total {int(reports)}",
            ),
            (
                "# HELP mnemos_usage_wrong_tool_rate Share of reported calls"
                " flagged as routed to the wrong tool.",
                "# TYPE mnemos_usage_wrong_tool_rate gauge",
                f"mnemos_usage_wrong_tool_rate {flagged / reports}",
            ),
        ]
        if tokens_out:
            lines.append(
                (
                    "# HELP mnemos_usage_tokens_out_total Output tokens reported"
                    " across usage reports.",
                    "# TYPE mnemos_usage_tokens_out_total counter",
                    f"mnemos_usage_tokens_out_total {int(tokens_out)}",
                )
            )
    return [line for group in lines for line in group] + touched_lines


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

        lines += _usage_block(conn)

        for name, value in sorted((gauges or {}).items()):
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {value}")
        return "\n".join(lines) + "\n"
    except sqlite3.Error:
        return ""


__all__ = ["render_exposition"]
