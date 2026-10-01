# Runbook: edge_stats maintenance (global cap & operator purge)

**🌐 Language / Язык:** English · [Русский](../../../ru/admin/runbooks/edge-stats-maintenance.md)

ADR-0030 A0 (issue #323) · review #338 N2

## What this is about

`edge_stats` is the append-only used/rejected feedback capture table
(I5): UPDATE and DELETE abort at the database level (schema triggers),
and capture volume is bounded by two caps enforced in
`record_edge_stat_event`:

| Guard | Constant | Effect when reached |
|---|---|---|
| Per-principal cap | `EDGE_STATS_EVENTS_PER_PRINCIPAL_CAP` = 10 000 | over-cap events from that `(project, agent)` bucket are dropped (`cap_dropped`), never an error |
| Global cap | `EDGE_STATS_TOTAL_ROWS_CAP` = 1 000 000 | over-cap events from ANY principal are dropped — capture resumes only after an operator purge |

There is **no automatic eviction**. Once the global cap is reached,
capture stays dropped until an operator reclaims rows via the purge
path below. Dropping audit rows is a deliberate operator decision, and
the decision itself is recorded (see the audit stamp below).

## When to run

- `edge-stats stats` shows the table approaching the global cap
  (telemetry / the `feedback_capture_stats` block reports growing
  `cap_dropped` counts);
- a storage-budget review decides the retention target.

## How to purge

```bash
# 1. Inspect the current state
vesma edge-stats stats

# 2. Dry run — reports what WOULD be dropped, writes nothing (default)
vesma edge-stats purge --keep-last 100000

# 3. Review the projection, then execute
vesma edge-stats purge --keep-last 100000 --apply
```

`--keep-last N` is required and has no default by design: retention is
an explicit operator statement. The NEWEST N rows (by `created_at`,
insertion order as tiebreak) survive; everything older is dropped.
`--keep-last 0` purges everything.

## What the purge guarantees

- Single maintenance transaction, opened with an explicit
  `BEGIN IMMEDIATE` before any DDL (the Python sqlite3 driver does not
  itself open a transaction for DDL): the DELETE trigger is dropped and
  recreated atomically with the row deletion — an aborted or crashed
  purge leaves the pre-purge world fully intact, and the append-only
  guarantee is never durably absent. Pinned by
  `test_mid_purge_failure_restores_trigger_and_rows`.
- The UPDATE trigger is never touched.
- The recreated DELETE trigger comes from the same shared DDL constant
  the schema installs (single source of truth — no drifting copy).
- The purge itself is stamped into the `meta` table
  (`edge_stats_last_purge`: timestamp, purged count, retention) — the
  compensating audit trail. `edge-stats stats` shows it as
  `last purge`.

## After the purge

- Capture resumes automatically (no restart needed) — the cap is
  checked per event.
- Re-check with `vesma edge-stats stats`.
