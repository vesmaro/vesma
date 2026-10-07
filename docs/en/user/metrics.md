<!-- vesma-integration: v2.0.0 -->
# Dashboard Metrics

**🌐 Language / Язык:** English · [Русский](../../ru/user/metrics.md)

> Three HTTP endpoints power the [`vesma-eyes`](https://github.com/vesmaro/vesma-eyes) dashboard and external
> observability stacks: structured JSON stats, temporal timeseries, and
> Prometheus text exposition. Plus extended `GET /memories` filters for
> list views.

---

## Overview

Vesma exposes three metrics endpoints for the `vesma-eyes` frontend and
for external observability tools (Grafana, Prometheus, dashboards):

| Endpoint | Format | Purpose |
|----------|--------|---------|
| `GET /api/v1/stats` | JSON | Structured dashboard data (volume, filter, pipeline, search, vectors, sessions) |
| `GET /api/v1/stats/timeseries` | JSON | Temporal data for charts (memories added per day) |
| `GET /api/v1/metrics` | Prometheus text | Exposition format for Grafana / Prometheus scraping |
| `GET /metrics` | JSON | Legacy alias — returns `stats()` JSON (backward compat) |

The `GET /memories` endpoint is also extended with filters for dashboard
list views (status, project, agent, tags, since, until, limit, offset).

---

## `GET /api/v1/stats`

Returns a structured JSON object aggregating the current state of the
memory store. This is the primary endpoint for the `vesma-eyes`
dashboard.

**Response** (`200 OK`):

```json
{
  "version": "5.6.2",
  "timestamp": "2026-06-20T14:30:00+00:00",
  "volume": {
    "memories_total": 1248,
    "by_status": {"published": 980, "processed": 210, "raw": 58},
    "by_project": {"vesma": 540, "umbra": 708},
    "by_agent": {"tech-lead": 620, "code-reviewer": 628},
    "by_type": {"note": 900, "decision": 200, "trace": 148}
  },
  "filter": {
    "auto_filter": true,
    "filtered_total": 1100,
    "unfiltered_total": 148,
    "avg_reduction_pct": 42.5,
    "by_profile": {"log": 30, "code": 20, "docs": 15, "default": 35}
  },
  "pipeline": {
    "processed_total": 1190,
    "failed_total": 3,
    "dlq_depth": 3,
    "last_run": null
  },
  "search": {
    "requests_total": 87,
    "cross_project_requests_total": 3,
    "avg_latency_ms": 12.4,
    "avg_results": 5.2
  },
  "vectors": {
    "indexed_total": 980
  },
  "sessions": {
    "active": 4,
    "total": 12
  }
}
```

### Sections

| Section | Fields | Source |
|---------|--------|--------|
| `volume` | `memories_total`, `by_status`, `by_project`, `by_agent`, `by_type` | SQLite aggregates |
| `filter` | `auto_filter`, `filtered_total`, `unfiltered_total`, `avg_reduction_pct`, `by_profile` | Context Filter stats |
| `pipeline` | `processed_total`, `failed_total`, `dlq_depth`, `last_run` | Status counts + DLQ |
| `search` | `requests_total`, `cross_project_requests_total`, `avg_latency_ms`, `avg_results` | In-memory instrumentation |
| `vectors` | `indexed_total` | Vector store count |
| `sessions` | `active` (updated within 24h), `total` | Sessions table |

> **Search instrumentation is in-memory.** The `requests_total`,
> `cross_project_requests_total`, `avg_latency_ms`, and `avg_results`
> counters reset on every server restart. This is an accepted trade-off
> for the dashboard — for persistent metrics, scrape
> `GET /api/v1/metrics` with Prometheus.
>
> **`cross_project_requests_total`** (ArchCom 2026-08-27, A9): searches
> that ran in the explicit global mode (`project` not passed / empty) —
> cross-project by definition. Every project-scoped search stays out of
> this counter.

---

## `GET /api/v1/stats/timeseries`

Returns temporal data for dashboard charts. Currently supports
`memories_added` (daily counts from SQLite).

**Query parameters**:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `metric` | string | `memories_added` | Metric to plot (only `memories_added` supported) |
| `range` | string | `30d` | Time range, format `<N>d` (e.g. `7d`, `30d`, `90d`) |
| `granularity` | string | `day` | Bucket size (only `day` supported; accepted for forward-compat) |

**Example**:

```http
GET /api/v1/stats/timeseries?metric=memories_added&range=30d&granularity=day
```

**Response** (`200 OK`):

```json
{
  "granularity": "day",
  "range": "30d",
  "series": [
    {
      "metric": "memories_added",
      "points": [
        {"timestamp": "2026-05-22", "value": 12},
        {"timestamp": "2026-05-23", "value": 8},
        {"timestamp": "2026-05-24", "value": 0}
      ]
    }
  ]
}
```

**Errors**:

| Status | Cause |
|--------|-------|
| `422` | Invalid `range` format (expected `<positive-integer>d`) |

Unsupported `metric` values return an empty `points` array with a note —
they do not raise an error, so the dashboard can degrade gracefully.

---

## `GET /api/v1/metrics`

Returns Prometheus text exposition format for scraping by Grafana /
Prometheus. Content-Type is `text/plain; version=0.0.4; charset=utf-8`.

**Available metrics** (the names carry the legacy `mnemos_` prefix —
format-stable identifiers, not a brand statement; renaming them would
break existing dashboards):

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `mnemos_memories_total` | gauge | — | Total memories in storage |
| `mnemos_memories_by_status` | gauge | `status` | Memories by status |
| `mnemos_memories_by_project` | gauge | `project` | Memories by project |
| `mnemos_memories_by_agent` | gauge | `agent` | Memories by agent |
| `mnemos_memories_by_type` | gauge | `type` | Memories by memory_type |
| `mnemos_filter_avg_reduction_pct` | gauge | — | Average filter reduction percentage |
| `mnemos_filter_filtered_total` | gauge | — | Memories with `clean_content` populated |
| `mnemos_pipeline_processed_total` | counter | — | Total processed memories |
| `mnemos_pipeline_dlq_depth` | gauge | — | Current DLQ depth |
| `mnemos_search_requests_total` | counter | — | Search requests since restart |
| `mnemos_search_cross_project_requests_total` | counter | — | Cross-project (explicit global-mode) requests since restart |
| `mnemos_search_avg_latency_ms` | gauge | — | Average search latency in ms |
| `mnemos_search_graph_supersedes_enriched_requests_total` | counter | — | Searches enriched from the graph (since restart) |
| `mnemos_search_graph_walk_enriched_requests_total` | counter | — | Searches enriched by a graph walk (since restart) |
| `mnemos_graph_auto_dedupe_edges_total` | counter | — | `relates_to` edges dropped by auto-dedupe (since restart) |
| `mnemos_graph_auto_dedupe_edges_by_project` | counter | `project` | Auto-dedupe-dropped `relates_to` edges, per project |
| `mnemos_vectors_indexed_total` | gauge | — | Indexed vectors |
| `mnemos_sessions_active` | gauge | — | Active sessions (updated within 24h) |
| `mnemos_sessions_total` | gauge | — | Total sessions |

**Example output** (excerpt):

```prometheus
# HELP mnemos_memories_total Total number of memories in storage
# TYPE mnemos_memories_total gauge
mnemos_memories_total 1248
# HELP mnemos_memories_by_status Memories by status
# TYPE mnemos_memories_by_status gauge
mnemos_memories_by_status{status="published"} 980
mnemos_memories_by_status{status="processed"} 210
```

### Prometheus scrape config

```yaml
scrape_configs:
  - job_name: vesma
    static_configs:
      - targets: ["localhost:8787"]
    metrics_path: /api/v1/metrics
```

**Authentication (#249):** the metrics endpoints bypass auth **only on
loopback binds** (a local Prometheus agent scraping `localhost` needs no
credentials). On non-loopback binds (`api.host` other than a loopback
address) both `/api/v1/metrics` and `/metrics` require an authenticated
session — the exposition exports `mnemos_memories_by_project` /
`mnemos_memories_by_agent` gauges, so unauthenticated remote access is a
reconnaissance-grade surface (CWE-200). A remote scraper authenticates
with a bearer header:

```yaml
scrape_configs:
  - job_name: vesma
    static_configs:
      - targets: ["vesma.example.internal:8787"]
    metrics_path: /api/v1/metrics
    authorization:
      type: Bearer
      credentials: <mnk_ token created with --no-totp>
```

### Legacy `GET /metrics`

The older `GET /metrics` endpoint returns `stats()` JSON (not Prometheus
text). It is kept for backward compatibility. For Prometheus scraping,
use `GET /api/v1/metrics`.

---

## `GET /memories` — extended filters

The memories list endpoint supports filters for dashboard list views and
paginated browsing.

**Query parameters**:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `status` | string | (all) | `raw`, `processing`, `processed`, `published`, `archived` |
| `project` | string | (all) | Project slug |
| `agent` | string | (all) | Agent slug |
| `tags` | string | (all) | Comma-separated tags (AND logic) |
| `since` | string | (all) | ISO datetime lower bound |
| `until` | string | (all) | ISO datetime upper bound |
| `limit` | int | `20` | Max results (≤ 500) |
| `offset` | int | `0` | Pagination offset (≥ 0) |
| `task` | string | (all) | Task scope — the bare slug (ADR-0027; byte-identical to the `task:<slug>` tag) |

**Example**:

```http
GET /memories?project=vesma&status=published&tags=mnemos:decision&limit=10&offset=20
```

**Errors**:

| Status | Cause |
|--------|-------|
| `422` | Invalid `status` value (valid: `raw`, `processing`, `processed`, `published`, `archived`) |
| `400` | Unsalvageable `task` slug (the manager boundary's error string passes through verbatim, #407) |

---

## How vesma-eyes consumes these endpoints

The `vesma-eyes` frontend (L1 read-only viewer) uses the metrics
endpoints as follows. Stack note: Vesma serves these endpoints itself
(FastAPI/uvicorn — the same process as `vesma serve`); `vesma-eyes` is a
separate frontend project that only consumes them over HTTP — no Node.js
runtime ships with, or is required by, Vesma.

| Dashboard widget | Endpoint | Section / param |
|------------------|----------|-----------------|
| Volume cards (total, by status) | `GET /api/v1/stats` | `volume.memories_total`, `volume.by_status` |
| Project / agent breakdown | `GET /api/v1/stats` | `volume.by_project`, `volume.by_agent` |
| Filter effectiveness | `GET /api/v1/stats` | `filter.avg_reduction_pct`, `filter.by_profile` |
| Pipeline health (DLQ) | `GET /api/v1/stats` | `pipeline.dlq_depth`, `pipeline.processed_total` |
| Search performance | `GET /api/v1/stats` | `search.avg_latency_ms`, `search.requests_total` |
| Memories-over-time chart | `GET /api/v1/stats/timeseries` | `metric=memories_added&range=30d` |
| Memory list with filters | `GET /memories` | `?project=…&status=…&tags=…&limit=…&offset=…` |
| External Grafana dashboard | `GET /api/v1/metrics` | Prometheus scrape |

---

## See also

- [HTTP API Reference](http-api.md) — every endpoint.
- [CLI Reference](cli-reference.md) — `vesma stats`, `vesma logs`.
- [Context Filter](context-filter.md) — the `filter` section in stats.