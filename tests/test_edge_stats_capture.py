"""ADR-0030 A0 (issue #323) — used/rejected feedback CAPTURE into edge_stats.

CAPTURE ONLY: events land in the append-only ``edge_stats`` table and
influence nothing — no ranking, no status, no eligibility (APPLY is
#325). The used-report binds to the existing search call: the ids are
the citation ids already riding the search response.

I5-capture requirements, each as a test:

* scoped — only records visible to the calling agent under the same
  gates as search (A9 project guard + the ADR-0018 admissible default
  set; quarantine excluded absolutely);
* uniform-404 — nonexistent / cross-project / inadmissible /
  quarantined ids land in ONE ``out_of_scope`` bucket, the response
  carries no per-id reason (no existence oracle);
* ``event_id`` PK idempotency — an agent retry of the same report
  contributes no double weight;
* append-only audit trail — enforced by schema triggers (UPDATE and
  DELETE abort at the DB level), not by convention;
* volume-cap per principal — over-cap events are dropped, never error;
* bounded counter clamp from day one — derived counters cannot exceed
  ``EDGE_STATS_COUNTER_CLAMP`` however many rows exist;
* scope fields (project/agent) recorded from the FIRST event;
* default-off flag — flag off means zero writes and zero telemetry;
* failure isolation — a capture-side failure never fails the caller.

Review #338 hardening (N1-N4, the A1 pre-requisites):

* N1 — the id preimage covers principal + kind: cross-agent /
  cross-project / used-vs-rejected collisions under one logical report
  id no longer silently drop the second event;
* N2 — GLOBAL row cap (``EDGE_STATS_TOTAL_ROWS_CAP``) plus the ONE
  operator purge path (``purge_edge_stats_oldest``: single maintenance
  transaction, trigger recreated, meta audit stamp; never automatic
  eviction);
* N3 — the durable telemetry reads are ONE ``GROUP BY kind``
  aggregation, not three full scans;
* N4 — ``EDGE_STATS_KINDS`` is public API alongside the cap constants.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vesmaro.config import Settings
from vesmaro.manager import MemoryManager
from vesmaro.models import MemoryCreate, MemorySource, MemoryStatus
from vesmaro.storage.sqlite_store import (
    EDGE_STATS_KINDS,
    EDGE_STATS_LAST_PURGE_META_KEY,
    SQLiteStore,
)

PROJECT = "fb-proj"
AGENT = "fb-agent"


def _settings(tmp: Path, *, feedback: bool = False) -> Settings:
    settings = Settings(
        mnemos={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
        },
        search={"feedback_capture_enabled": feedback},
        scanner={"enabled": False},  # type: ignore[arg-type]
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    s = SQLiteStore(tmp_path / "edge_stats.db")
    yield s
    s.close()


@pytest.fixture
def manager(tmp_path: Path) -> Iterator[MemoryManager]:
    """Flag OFF — the shipped default."""
    mgr = MemoryManager(_settings(tmp_path))
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    yield mgr
    mgr.close()


@pytest.fixture
def manager_on(tmp_path: Path) -> Iterator[MemoryManager]:
    """Flag ON — the capture leg armed (explicit deployment opt-in)."""
    mgr = MemoryManager(_settings(tmp_path, feedback=True))
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    yield mgr
    mgr.close()


def _add(
    mgr: MemoryManager,
    content: str,
    *,
    status: MemoryStatus = MemoryStatus.PUBLISHED,
    project: str = PROJECT,
) -> object:
    data = MemoryCreate(
        content=content,
        tags=[f"project:{project}", f"agent:{AGENT}", "mnemos:test"],
        source=MemorySource.MCP,
        status=status,
    )
    return mgr.add(data, project=project, agent=AGENT)


def _rows(mgr: MemoryManager) -> list[sqlite3.Row]:
    return (
        mgr.sqlite._get_conn()
        .execute(
            "SELECT event_id, memory_id, kind, project, agent, created_at"
            " FROM edge_stats ORDER BY created_at, event_id"
        )
        .fetchall()
    )


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, dict[str, object]]:
    return {
        str(r[1]): {"type": str(r[2]), "notnull": int(r[3]), "dflt": r[4], "pk": int(r[5])}
        for r in conn.execute(f"PRAGMA table_info({table})")  # nosec B608 - test-local name
    }


def _object_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone() is not None
    )


# ── Schema: final shape from day one ──────────────────────────────────────────


class TestSchema:
    def test_columns_pk_and_defaults(self, store: SQLiteStore) -> None:
        cols = _columns(store._get_conn(), "edge_stats")
        assert set(cols) == {"event_id", "memory_id", "kind", "project", "agent", "created_at"}
        # event_id is THE primary key — idempotency per event.
        assert cols["event_id"]["pk"] == 1
        assert [c for c, v in cols.items() if v["pk"]] == ["event_id"]
        # Scope fields present and NOT NULL from the first event (the
        # retrofit-loses-boundary lesson — they cannot be backfilled).
        assert cols["project"]["notnull"] == 1
        assert cols["agent"]["notnull"] == 1
        assert cols["memory_id"]["notnull"] == 1
        assert cols["created_at"]["notnull"] == 1

    def test_indexes_present(self, store: SQLiteStore) -> None:
        conn = store._get_conn()
        assert _object_exists(conn, "idx_edge_stats_memory")
        assert _object_exists(conn, "idx_edge_stats_scope")

    def test_check_accepts_exactly_the_whitelist(self, store: SQLiteStore) -> None:
        """The SQL CHECK and EDGE_STATS_KINDS are the same set (DB-level
        sync, the _EDGE_KINDS rule), and anything else aborts."""
        assert set(EDGE_STATS_KINDS) == {"used", "rejected"}
        conn = store._get_conn()
        for kind in EDGE_STATS_KINDS:
            conn.execute(
                "INSERT INTO edge_stats (event_id, memory_id, kind, created_at) VALUES (?,?,?,?)",
                (f"ev-{kind}", "m-x", kind, "2026-09-16T00:00:00+00:00"),
            )
        assert conn.execute("SELECT COUNT(*) FROM edge_stats").fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            conn.execute(
                "INSERT INTO edge_stats (event_id, memory_id, kind, created_at) VALUES (?,?,?,?)",
                ("ev-bad", "m-x", "maybe", "2026-09-16T00:00:00+00:00"),
            )

    def test_no_foreign_key_on_memory_id(self, store: SQLiteStore) -> None:
        """The audit trail outlives its subject: a row for a memory id
        that does not exist in `memories` is storable at the DB level
        (visibility is the manager's I5 gate, not a FK), and deleting
        the subject never rewrites history."""
        store._get_conn().execute(
            "INSERT INTO edge_stats (event_id, memory_id, kind, created_at)"
            " VALUES ('e1', 'never-existed', 'used', '2026-09-16T00:00:00+00:00')"
        )
        assert store.count_edge_stats() == 1

    def test_legacy_db_gains_table_on_connect(self, tmp_path: Path) -> None:
        """A DB written before this change has no edge_stats; the next
        connect creates it empty (CREATE TABLE IF NOT EXISTS in the
        schema script — no migration entry needed, the
        memory_workflow_history pattern)."""
        db = tmp_path / "legacy.db"
        s = SQLiteStore(db)
        assert s.count_edge_stats() == 0  # force the lazy bootstrap
        s.close()
        conn = sqlite3.connect(str(db))
        conn.execute("DROP TABLE edge_stats")  # simulate the pre-#323 shape
        conn.commit()
        conn.close()

        s2 = SQLiteStore(db)
        assert "event_id" in _columns(s2._get_conn(), "edge_stats")
        assert s2.count_edge_stats() == 0
        s2.close()


# ── Append-only audit trail (DB-level guarantee) ─────────────────────────────


class TestAppendOnly:
    def test_update_aborts(self, store: SQLiteStore) -> None:
        conn = store._get_conn()
        conn.execute(
            "INSERT INTO edge_stats (event_id, memory_id, kind, created_at)"
            " VALUES ('e1', 'm-x', 'used', '2026-09-16T00:00:00+00:00')"
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE edge_stats SET kind = 'rejected' WHERE event_id = 'e1'")

    def test_delete_aborts(self, store: SQLiteStore) -> None:
        conn = store._get_conn()
        conn.execute(
            "INSERT INTO edge_stats (event_id, memory_id, kind, created_at)"
            " VALUES ('e1', 'm-x', 'used', '2026-09-16T00:00:00+00:00')"
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM edge_stats WHERE event_id = 'e1'")

    def test_memory_deletion_neither_fails_nor_rewrites(self, manager_on: MemoryManager) -> None:
        """Audit rows outlive their subject: deleting the cited memory
        succeeds and the event row stays (no FK, no cascade)."""
        m = _add(manager_on, "cited once, deleted later")
        assert (
            manager_on.report_search_feedback([m.id], kind="used", project=PROJECT)["captured"] == 1
        )
        manager_on.sqlite.delete(m.id)
        assert len(_rows(manager_on)) == 1


# ── Store: idempotency, volume-cap, clamp ─────────────────────────────────────


class TestStoreRecord:
    def test_insert_and_duplicate(self, store: SQLiteStore) -> None:
        assert store.record_edge_stat_event("e1", "m-a", kind="used") == "inserted"
        # Same event_id — an agent retry: no second row, no double weight.
        assert store.record_edge_stat_event("e1", "m-a", kind="used") == "duplicate"
        assert store.count_edge_stats() == 1

    def test_scope_fields_recorded_from_first_event(self, store: SQLiteStore) -> None:
        store.record_edge_stat_event("e1", "m-a", kind="used", project="p1", agent="ag1")
        row = (
            store._get_conn()
            .execute("SELECT memory_id, kind, project, agent, created_at FROM edge_stats")
            .fetchone()
        )
        assert tuple(row) == ("m-a", "used", "p1", "ag1", row["created_at"])
        assert row["created_at"]  # timestamped

    def test_unknown_kind_rejected(self, store: SQLiteStore) -> None:
        with pytest.raises(ValueError, match="unknown feedback kind"):
            store.record_edge_stat_event("e1", "m-a", kind="maybe")

    def test_empty_event_id_rejected(self, store: SQLiteStore) -> None:
        with pytest.raises(ValueError, match="event_id"):
            store.record_edge_stat_event("", "m-a", kind="used")

    def test_volume_cap_per_principal(
        self, store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Over-cap events are DROPPED (a normal outcome, never an error),
        and the cap is per (project, agent) bucket — a hot principal
        cannot wedge unbounded rows (storage-DoS / APPLY pre-poisoning)."""
        monkeypatch.setattr("vesmaro.storage.sqlite_store.EDGE_STATS_EVENTS_PER_PRINCIPAL_CAP", 3)
        for i in range(3):
            assert (
                store.record_edge_stat_event(f"e-{i}", "m-a", kind="used", project="p1", agent="a1")
                == "inserted"
            )
        assert (
            store.record_edge_stat_event("e-3", "m-a", kind="used", project="p1", agent="a1")
            == "cap_dropped"
        )
        # A different principal is unaffected — the cap is per bucket.
        assert (
            store.record_edge_stat_event("e-x", "m-a", kind="used", project="p2", agent="a1")
            == "inserted"
        )
        assert store.count_edge_stats() == 4

    def test_counters_clamped_from_day_one(
        self, store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Whatever the table holds, no derived counter exceeds
        EDGE_STATS_COUNTER_CLAMP — capture cannot drift before APPLY."""
        monkeypatch.setattr("vesmaro.storage.sqlite_store.EDGE_STATS_COUNTER_CLAMP", 5)
        for i in range(8):
            store.record_edge_stat_event(f"u-{i}", "m-a", kind="used", project="p1")
        for i in range(3):
            store.record_edge_stat_event(f"r-{i}", "m-a", kind="rejected", project="p1")
        assert store.get_edge_stats_counters("m-a") == {"used": 5, "rejected": 3}

    def test_counters_absent_kinds_zero(self, store: SQLiteStore) -> None:
        assert store.get_edge_stats_counters("m-unknown") == {"used": 0, "rejected": 0}

    def test_count_edge_stats_by_kind(self, store: SQLiteStore) -> None:
        store.record_edge_stat_event("e1", "m-a", kind="used")
        store.record_edge_stat_event("e2", "m-b", kind="rejected")
        assert store.count_edge_stats() == 2
        assert store.count_edge_stats(kind="used") == 1
        assert store.count_edge_stats(kind="rejected") == 1


# ── Manager: I5 gates, flag, isolation ────────────────────────────────────────


class TestFlagOff:
    def test_default_off_zero_capture(self, manager: MemoryManager) -> None:
        m = _add(manager, "visible and published")
        outcome = manager.report_search_feedback([m.id], kind="used", project=PROJECT)
        assert outcome["flag_enabled"] is False
        assert outcome["captured"] == 0
        assert len(_rows(manager)) == 0
        # Zero telemetry too — the disabled surface leaves no heatmap.
        stats = manager.feedback_capture_stats()
        assert stats["enabled"] is False
        assert stats["events_total"] == 0
        assert stats["since_restart"]["reports_total"] == 0

    def test_config_default_follows_owner_decision(self) -> None:
        """#440 «graphs on by default» flipped the default to True; the
        flag-off behavior itself is covered by the explicit-flag tests
        above. This pin follows the owner decision of 2026-09-28."""
        from vesmaro.config import SearchConfig

        assert SearchConfig().feedback_capture_enabled is True


class TestCaptureHappyPath:
    def test_used_event_lands_with_scope(self, manager_on: MemoryManager) -> None:
        m = _add(manager_on, "cited by the harness")
        outcome = manager_on.report_search_feedback(
            [m.id], kind="used", project=PROJECT, agent=AGENT, event_id="report-1"
        )
        assert outcome["captured"] == 1
        row = _rows(manager_on)[0]
        assert row["memory_id"] == m.id
        assert (row["kind"], row["project"], row["agent"]) == ("used", PROJECT, AGENT)

    def test_rejected_kind_captured(self, manager_on: MemoryManager) -> None:
        m = _add(manager_on, "dismissed citation")
        outcome = manager_on.report_search_feedback([m.id], kind="rejected", project=PROJECT)
        assert outcome["captured"] == 1
        assert _rows(manager_on)[0]["kind"] == "rejected"

    def test_unknown_kind_is_a_caller_bug(self, manager_on: MemoryManager) -> None:
        with pytest.raises(ValueError, match="unknown feedback kind"):
            manager_on.report_search_feedback(["m-x"], kind="maybe", project=PROJECT)

    def test_retry_with_same_event_id_no_double_weight(self, manager_on: MemoryManager) -> None:
        m1 = _add(manager_on, "first citation")
        m2 = _add(manager_on, "second citation")
        first = manager_on.report_search_feedback(
            [m1.id, m2.id], kind="used", project=PROJECT, event_id="report-1"
        )
        assert first["captured"] == 2
        # The agent retries the SAME report (at-least-once delivery).
        retry = manager_on.report_search_feedback(
            [m1.id, m2.id], kind="used", project=PROJECT, event_id="report-1"
        )
        assert retry["captured"] == 0
        assert retry["duplicates"] == 2
        assert len(_rows(manager_on)) == 2  # still two rows, not four

        # A different report id is a genuinely new event — it counts.
        later = manager_on.report_search_feedback(
            [m1.id], kind="used", project=PROJECT, event_id="report-2"
        )
        assert later["captured"] == 1

    def test_duplicate_ids_within_one_report_are_one_event(self, manager_on: MemoryManager) -> None:
        m = _add(manager_on, "same citation twice in one batch")
        outcome = manager_on.report_search_feedback(
            [m.id, m.id], kind="used", project=PROJECT, event_id="report-1"
        )
        assert outcome["reported"] == 1
        assert outcome["captured"] == 1
        assert len(_rows(manager_on)) == 1

    def test_counters_reflect_capture(self, manager_on: MemoryManager) -> None:
        m = _add(manager_on, "counted citation")
        manager_on.report_search_feedback([m.id], kind="used", project=PROJECT, event_id="r1")
        manager_on.report_search_feedback([m.id], kind="used", project=PROJECT, event_id="r2")
        manager_on.report_search_feedback([m.id], kind="rejected", project=PROJECT, event_id="r3")
        assert manager_on.sqlite.get_edge_stats_counters(m.id) == {"used": 2, "rejected": 1}


class TestScopingAndUniform404:
    def test_nonexistent_id_out_of_scope(self, manager_on: MemoryManager) -> None:
        outcome = manager_on.report_search_feedback(
            ["no-such-id"], kind="used", project=PROJECT, event_id="r1"
        )
        assert outcome["captured"] == 0
        assert outcome["out_of_scope"] == 1
        assert len(_rows(manager_on)) == 0

    def test_cross_project_id_out_of_scope(self, manager_on: MemoryManager) -> None:
        foreign = _add(manager_on, "lives in another project", project="other-proj")
        outcome = manager_on.report_search_feedback(
            [foreign.id], kind="used", project=PROJECT, event_id="r1"
        )
        assert outcome["captured"] == 0
        assert outcome["out_of_scope"] == 1

    def test_raw_status_id_out_of_scope(self, manager_on: MemoryManager) -> None:
        raw = _add(manager_on, "not yet published", status=MemoryStatus.RAW)
        outcome = manager_on.report_search_feedback(
            [raw.id], kind="used", project=PROJECT, event_id="r1"
        )
        assert outcome["captured"] == 0
        assert outcome["out_of_scope"] == 1

    def test_quarantined_id_out_of_scope(self, manager_on: MemoryManager) -> None:
        m = _add(manager_on, "later quarantined")
        manager_on.sqlite._get_conn().execute(
            "UPDATE memories SET pipeline_state = 'quarantined' WHERE id = ?", (m.id,)
        )
        manager_on.sqlite._get_conn().commit()
        outcome = manager_on.report_search_feedback(
            [m.id], kind="used", project=PROJECT, event_id="r1"
        )
        assert outcome["captured"] == 0
        assert outcome["out_of_scope"] == 1

    def test_no_existence_oracle_uniform_outcome(self, manager_on: MemoryManager) -> None:
        """THE I5 uniform-404 test: a nonexistent id, a cross-project id,
        a raw-status id and a quarantined id produce IDENTICAL report
        dicts — the response cannot distinguish "does not exist" from
        "exists but invisible", so it is not an existence oracle."""
        foreign = _add(manager_on, "foreign project row", project="other-proj")
        raw = _add(manager_on, "raw row", status=MemoryStatus.RAW)
        q = _add(manager_on, "to be quarantined")
        conn = manager_on.sqlite._get_conn()
        conn.execute("UPDATE memories SET pipeline_state = 'quarantined' WHERE id = ?", (q.id,))
        conn.commit()

        outcomes = [
            manager_on.report_search_feedback([mid], kind="used", project=PROJECT, event_id=f"r{i}")
            for i, mid in enumerate(["no-such-id", foreign.id, raw.id, q.id], start=1)
        ]
        assert len(outcomes) == 4
        first = outcomes[0]
        for other in outcomes[1:]:
            assert other == first  # byte-identical reports, no per-id reason
        assert first["out_of_scope"] == 1 and first["captured"] == 0
        assert len(_rows(manager_on)) == 0

    def test_global_mode_reports_cross_project(self, manager_on: MemoryManager) -> None:
        """project=None mirrors the search explicit-global mode: a row of
        another project IS reportable (the caller could have found it in
        a global search); the volume-cap bucket is the ('', '') one."""
        foreign = _add(manager_on, "global search finds me", project="other-proj")
        outcome = manager_on.report_search_feedback(
            [foreign.id], kind="used", project=None, event_id="r1"
        )
        assert outcome["captured"] == 1
        row = _rows(manager_on)[0]
        assert (row["project"], row["agent"]) == ("", "")

    def test_processed_status_visible(self, manager_on: MemoryManager) -> None:
        m = _add(manager_on, "processed rows are admissible", status=MemoryStatus.PROCESSED)
        outcome = manager_on.report_search_feedback([m.id], kind="used", project=PROJECT)
        assert outcome["captured"] == 1


class TestFailureIsolation:
    def test_store_failure_never_fails_caller(
        self, manager_on: MemoryManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        m = _add(manager_on, "would be captured")
        monkeypatch.setattr(
            manager_on.sqlite,
            "record_edge_stat_event",
            MagicMock(side_effect=sqlite3.OperationalError("disk full")),
        )
        outcome = manager_on.report_search_feedback(
            [m.id], kind="used", project=PROJECT, event_id="r1"
        )  # must not raise
        assert outcome["captured"] == 0
        assert manager_on.feedback_capture_stats()["since_restart"]["errors_total"] == 1

    def test_telemetry_read_failure_degrades(
        self, manager_on: MemoryManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # N3: the durable read surface is the single by-kind aggregation.
        monkeypatch.setattr(
            manager_on.sqlite,
            "count_edge_stats_by_kind",
            MagicMock(side_effect=sqlite3.OperationalError("locked")),
        )
        stats = manager_on.feedback_capture_stats()  # must not raise
        assert stats["events_total"] == 0


# ── Telemetry (the D-behavioral payoff A0-review reads) ──────────────────────


class TestTelemetry:
    def test_durable_totals_survive_manager_restart(self, tmp_path: Path) -> None:
        mgr = MemoryManager(_settings(tmp_path, feedback=True))
        mgr._embedder = MagicMock()
        mgr._embedder.embed.return_value = [0.1] * 384
        m = _add(mgr, "captured before restart")
        assert (
            mgr.report_search_feedback([m.id], kind="used", project=PROJECT, event_id="r1")[
                "captured"
            ]
            == 1
        )
        mgr.close()

        # Fresh process over the same DB: the durable counters persist.
        mgr2 = MemoryManager(_settings(tmp_path, feedback=True))
        stats = mgr2.feedback_capture_stats()
        assert stats["enabled"] is True
        assert stats["events_total"] == 1
        assert stats["captured_used_total"] == 1
        assert stats["captured_rejected_total"] == 0
        assert stats["since_restart"]["reports_total"] == 0  # window resets
        mgr2.close()

    def test_dashboard_carries_feedback_block(self, manager_on: MemoryManager) -> None:
        m = _add(manager_on, "counted by the dashboard")
        manager_on.report_search_feedback([m.id], kind="rejected", project=PROJECT)
        block = manager_on.dashboard_stats()["feedback_capture"]
        assert block == {
            "enabled": True,
            "events_total": 1,
            "captured_used_total": 0,
            "captured_rejected_total": 1,
        }


# ── Integration: the search-reading path end to end ──────────────────────────


class TestSearchIntegration:
    def test_search_citations_report_used_lands_with_scope(self, manager_on: MemoryManager) -> None:
        """The canonical flow: search → citation ids ride the response →
        the harness reports the consumed subset with a retry-stable
        event id → one row per citation, scoped, and the duplicate
        retry is ignored. 'used' is NEVER auto-inferred: a search
        WITHOUT a report captures nothing."""
        hit = _add(manager_on, "unique kyword alpha deployment notes")
        _add(manager_on, "different topic entirely windmills")

        results = manager_on.search("kyword alpha", limit=5, project=PROJECT)
        assert any(r.memory.id == hit.id for r in results)
        # No report yet — the search call itself captured nothing.
        assert len(_rows(manager_on)) == 0

        used_ids = [r.memory.id for r in results[:2]]
        first = manager_on.report_search_feedback(
            used_ids, kind="used", project=PROJECT, agent=AGENT, event_id="turn-42"
        )
        assert first["captured"] == len(used_ids)
        retry = manager_on.report_search_feedback(
            used_ids, kind="used", project=PROJECT, agent=AGENT, event_id="turn-42"
        )
        assert retry["duplicates"] == len(used_ids)

        rows = _rows(manager_on)
        assert len(rows) == len(used_ids)
        for row in rows:
            assert (row["kind"], row["project"], row["agent"]) == ("used", PROJECT, AGENT)
            assert row["memory_id"] in used_ids

    def test_zero_behavior_change_flag_off(self, manager: MemoryManager) -> None:
        """Flag off: search + a stray report change nothing — zero rows,
        zero ranking influence (this slice has none by construction),
        search results identical to a manager that never heard of
        edge_stats."""
        _add(manager, "flag off corpus row one")
        _add(manager, "flag off corpus row two")
        results = manager.search("corpus", limit=10, project=PROJECT)
        assert results
        manager.report_search_feedback(
            [r.memory.id for r in results], kind="used", project=PROJECT, event_id="r1"
        )
        assert len(_rows(manager)) == 0
        assert manager.feedback_capture_stats()["events_total"] == 0


# ── Review #338 N1: the id preimage covers principal + kind ──────────────────


class TestEventIdPreimage:
    """N1 — hashing only (event_id, memory_id) let two principals
    sharing one logical report id collide: the second agent's legitimate
    event was silently dropped as a "duplicate". The preimage now
    covers (event_id, memory_id, kind, project, agent) while staying
    retry-stable — idempotency remains the point."""

    def test_hash_retry_stable_and_field_sensitive(self) -> None:
        from vesmaro.manager import _derive_feedback_event_id as derive

        base = derive("r1", "m-a", kind="used", project="p", agent="ag")
        # Retry-stable: same tuple → same row id (the whole point).
        assert derive("r1", "m-a", kind="used", project="p", agent="ag") == base
        # Each preimage dimension moves the id.
        assert derive("r1", "m-a", kind="rejected", project="p", agent="ag") != base
        assert derive("r1", "m-a", kind="used", project="q", agent="ag") != base
        assert derive("r1", "m-a", kind="used", project="p", agent="ag2") != base
        assert derive("r2", "m-a", kind="used", project="p", agent="ag") != base
        # Length-prefix injectivity (the compute_event_key property): a
        # delimiter-containing id cannot spoof a different split.
        assert derive("ab", "c", kind="used", project="", agent="") != derive(
            "a", "bc", kind="used", project="", agent=""
        )

    def test_cross_agent_same_logical_id_both_land(self, manager_on: MemoryManager) -> None:
        """Two agents share the caller's logical report id (e.g. the same
        harness turn id reused across agents) — BOTH events land; the
        second is no longer a silent "duplicate" drop."""
        m = _add(manager_on, "cited by two agents")
        first = manager_on.report_search_feedback(
            [m.id], kind="used", project=None, agent="agent-one", event_id="turn-42"
        )
        second = manager_on.report_search_feedback(
            [m.id], kind="used", project=None, agent="agent-two", event_id="turn-42"
        )
        assert (first["captured"], second["captured"]) == (1, 1)
        assert (first["duplicates"], second["duplicates"]) == (0, 0)
        rows = _rows(manager_on)
        assert len(rows) == 2
        assert {r["agent"] for r in rows} == {"agent-one", "agent-two"}

    def test_cross_project_same_logical_id_both_land(self, manager_on: MemoryManager) -> None:
        """The project half of the principal: two projects, one logical
        report id — both events land."""
        m_a = _add(manager_on, "project a citation", project="proj-a")
        m_b = _add(manager_on, "project b citation", project="proj-b")
        first = manager_on.report_search_feedback(
            [m_a.id], kind="used", project="proj-a", agent=AGENT, event_id="turn-42"
        )
        second = manager_on.report_search_feedback(
            [m_b.id], kind="used", project="proj-b", agent=AGENT, event_id="turn-42"
        )
        assert (first["captured"], second["captured"]) == (1, 1)
        rows = _rows(manager_on)
        assert len(rows) == 2
        assert {r["project"] for r in rows} == {"proj-a", "proj-b"}

    def test_used_then_rejected_same_logical_id_both_land(self, manager_on: MemoryManager) -> None:
        """One caller id, both verdicts: 'used' then 'rejected' for the
        same citation under one logical report id are TWO events — the
        kind is in the preimage, the retraction is not swallowed."""
        m = _add(manager_on, "used first, rejected on reflection")
        used = manager_on.report_search_feedback(
            [m.id], kind="used", project=PROJECT, agent=AGENT, event_id="r1"
        )
        rejected = manager_on.report_search_feedback(
            [m.id], kind="rejected", project=PROJECT, agent=AGENT, event_id="r1"
        )
        assert (used["captured"], rejected["captured"]) == (1, 1)
        rows = _rows(manager_on)
        assert len(rows) == 2
        assert {r["kind"] for r in rows} == {"used", "rejected"}

    def test_retry_same_principal_same_kind_still_idempotent(
        self, manager_on: MemoryManager
    ) -> None:
        """N1 does not loosen idempotency: the SAME (event, memory,
        kind, principal) tuple retries to a duplicate — exactly one row."""
        m = _add(manager_on, "retried report")
        first = manager_on.report_search_feedback(
            [m.id], kind="used", project=PROJECT, agent=AGENT, event_id="r1"
        )
        retry = manager_on.report_search_feedback(
            [m.id], kind="used", project=PROJECT, agent=AGENT, event_id="r1"
        )
        assert (first["captured"], retry["duplicates"]) == (1, 1)
        assert len(_rows(manager_on)) == 1


# ── Review #338 N2: global row cap + the operator purge path ─────────────────


class TestGlobalRowsCap:
    def test_global_cap_drops_across_principals(
        self, store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The per-bucket cap bounds ONE identity; the global cap bounds
        the TABLE across all minted principals — over-cap events drop
        regardless of which principal reports them."""
        monkeypatch.setattr("vesmaro.storage.sqlite_store.EDGE_STATS_TOTAL_ROWS_CAP", 3)
        for i, p in enumerate(["p1", "p2", "p3"]):
            assert (
                store.record_edge_stat_event(f"e-{i}", "m-a", kind="used", project=p) == "inserted"
            )
        # A FOURTH principal, first event, per-bucket cap far away — the
        # global ceiling is what drops it.
        assert (
            store.record_edge_stat_event("e-x", "m-a", kind="used", project="p4") == "cap_dropped"
        )
        assert store.count_edge_stats() == 3

    def test_capture_resumes_after_operator_purge(
        self, store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The global cap is NOT automatic eviction: capture stays
        dropped until an operator reclaims rows — then it flows again."""
        monkeypatch.setattr("vesmaro.storage.sqlite_store.EDGE_STATS_TOTAL_ROWS_CAP", 2)
        assert store.record_edge_stat_event("e-1", "m-a", kind="used", project="p1") == "inserted"
        assert store.record_edge_stat_event("e-2", "m-b", kind="used", project="p1") == "inserted"
        assert (
            store.record_edge_stat_event("e-3", "m-c", kind="used", project="p2") == "cap_dropped"
        )
        purged = store.purge_edge_stats_oldest(keep_last=0, dry_run=False)
        assert purged["purged"] == 2
        # Same previously-dropped event, same id — now it lands.
        assert store.record_edge_stat_event("e-3", "m-c", kind="used", project="p2") == "inserted"


def _seed_direct(store: SQLiteStore, rows: list[tuple[str, str]]) -> None:
    """Insert rows with CONTROLLED created_at (bypasses the store method,
    which stamps now()) — purge ordering must be testable deterministically."""
    conn = store._get_conn()
    for event_id, created_at in rows:
        conn.execute(
            "INSERT INTO edge_stats (event_id, memory_id, kind, project, agent, created_at)"
            " VALUES (?, 'm-x', 'used', 'p', 'a', ?)",
            (event_id, created_at),
        )
    conn.commit()


class TestOperatorPurge:
    def test_dry_run_reports_and_writes_nothing(self, store: SQLiteStore) -> None:
        _seed_direct(store, [(f"e-{i}", f"2026-09-{10 + i}T00:00:00+00:00") for i in range(5)])
        result = store.purge_edge_stats_oldest(keep_last=2, dry_run=True)
        assert result == {"rows_before": 5, "purged": 3, "rows_after": 2, "dry_run": True}
        assert store.count_edge_stats() == 5  # nothing written
        assert store.get_meta(EDGE_STATS_LAST_PURGE_META_KEY) is None  # no audit stamp either

    def test_apply_purges_oldest_keeps_newest(self, store: SQLiteStore) -> None:
        _seed_direct(store, [(f"e-{i}", f"2026-09-{10 + i}T00:00:00+00:00") for i in range(5)])
        result = store.purge_edge_stats_oldest(keep_last=2, dry_run=False)
        assert result == {"rows_before": 5, "purged": 3, "rows_after": 2, "dry_run": False}
        kept = {r[0] for r in store._get_conn().execute("SELECT event_id FROM edge_stats")}
        assert kept == {"e-3", "e-4"}  # the NEWEST two survive

    def test_append_only_guards_survive_purge(self, store: SQLiteStore) -> None:
        """The purge transaction restores the DELETE trigger: after it,
        plain DELETE and UPDATE abort exactly as before (the audit trail
        is still a database guarantee — the operator path is the only
        exception, and it re-arms itself)."""
        _seed_direct(store, [("e-1", "2026-09-10T00:00:00+00:00")])
        store.purge_edge_stats_oldest(keep_last=1, dry_run=False)
        conn = store._get_conn()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM edge_stats WHERE event_id = 'e-1'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE edge_stats SET kind = 'rejected' WHERE event_id = 'e-1'")

    def test_purge_stamps_meta_audit_trail(self, store: SQLiteStore) -> None:
        """The compensating audit: the append-only table shrank, and the
        record of THAT shrink survives in meta (at/purged/keep_last)."""
        _seed_direct(store, [(f"e-{i}", f"2026-09-{10 + i}T00:00:00+00:00") for i in range(4)])
        store.purge_edge_stats_oldest(keep_last=2, dry_run=False)
        stamp = json.loads(store.get_meta(EDGE_STATS_LAST_PURGE_META_KEY) or "{}")
        assert stamp["purged"] == 2
        assert stamp["keep_last"] == 2
        assert stamp["at"]  # ISO timestamp of the operator action

    def test_negative_keep_last_rejected(self, store: SQLiteStore) -> None:
        with pytest.raises(ValueError, match="keep_last"):
            store.purge_edge_stats_oldest(keep_last=-1)

    def test_keep_last_beyond_total_is_noop(self, store: SQLiteStore) -> None:
        _seed_direct(store, [("e-1", "2026-09-10T00:00:00+00:00")])
        result = store.purge_edge_stats_oldest(keep_last=10, dry_run=False)
        assert result["purged"] == 0
        assert result["rows_after"] == 1

    def test_created_at_tie_broken_deterministically(self, store: SQLiteStore) -> None:
        """Same-timestamp rows order by insertion (rowid): the purge set
        is a deterministic function of the table state."""
        _seed_direct(
            store,
            [
                ("old-a", "2026-09-10T00:00:00+00:00"),
                ("old-b", "2026-09-10T00:00:00+00:00"),
                ("new-a", "2026-09-11T00:00:00+00:00"),
                ("new-b", "2026-09-11T00:00:00+00:00"),
            ],
        )
        result = store.purge_edge_stats_oldest(keep_last=2, dry_run=False)
        assert result["purged"] == 2
        kept = {r[0] for r in store._get_conn().execute("SELECT event_id FROM edge_stats")}
        assert kept == {"new-a", "new-b"}

    def test_mid_purge_failure_restores_trigger_and_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Review #338 round 2 MAJOR pin — the purge must be ONE
        transaction at the DRIVER level.

        The python sqlite3 driver (legacy isolation mode) opens implicit
        transactions for DML only; DDL alone AUTOCOMMITS. A purge that
        dropped the DELETE trigger outside an explicit transaction and
        then failed would leave the guard durably absent after rollback
        — plain DELETEs would succeed and the append-only audit trail
        would be unprotected. Simulate a crash AFTER the DROP/DELETE,
        BEFORE the commit: on reopen the trigger MUST exist, every row
        MUST be intact, a plain DELETE MUST still abort, and no purge
        audit stamp may exist.

        The reopen probe is a RAW sqlite3 connection, deliberately NOT
        ``SQLiteStore``: the store's connect-time bootstrap re-runs
        ``_DB_SCHEMA`` (all ``IF NOT EXISTS``) and would silently HEAL
        an absent trigger — masking exactly the durability bug this
        test pins. The raw connection observes the true on-disk state
        (a live server keeps its long-lived connection and never
        re-runs the schema, so until the next restart nothing would
        reinstall the guard)."""
        db = tmp_path / "purge-crash.db"
        store = SQLiteStore(db)
        _seed_direct(store, [(f"e-{i}", f"2026-09-{10 + i}T00:00:00+00:00") for i in range(4)])
        real_conn = store._get_conn()

        class _FailOnTriggerRecreate:
            """Connection proxy simulating process death at the exact
            mid-purge point (the trigger-recreation statement)."""

            def __init__(self, inner: sqlite3.Connection) -> None:
                self._inner = inner

            def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:  # type: ignore[assignment]
                if "CREATE TRIGGER" in sql:
                    raise sqlite3.OperationalError("injected mid-purge failure")
                return self._inner.execute(sql, params)

            def commit(self) -> None:
                self._inner.commit()

            def rollback(self) -> None:
                self._inner.rollback()

        monkeypatch.setattr(
            store,
            "_get_conn",
            lambda: _FailOnTriggerRecreate(real_conn),  # type: ignore[arg-type]
        )
        with pytest.raises(sqlite3.OperationalError, match="injected mid-purge failure"):
            store.purge_edge_stats_oldest(keep_last=2, dry_run=False)
        store.close()

        # RAW reopen — durability on disk, not this connection's
        # rollback state, and not the store's schema-healing bootstrap.
        raw = sqlite3.connect(str(db))
        try:
            assert _object_exists(raw, "edge_stats_no_delete")  # the guard never left
            assert raw.execute("SELECT COUNT(*) FROM edge_stats").fetchone()[0] == 4
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                raw.execute("DELETE FROM edge_stats WHERE event_id = 'e-0'")
            stamp = raw.execute(
                "SELECT value FROM meta WHERE key = ?", (EDGE_STATS_LAST_PURGE_META_KEY,)
            ).fetchone()
            assert stamp is None  # no false audit of a purge that did not land
        finally:
            raw.close()

    def test_delete_trigger_ddl_is_single_source(self) -> None:
        """Review #338 round 2 minor pin — the purge reinstalls the
        trigger from the SAME literal the schema installs
        (``_EDGE_STATS_NO_DELETE_TRIGGER_DDL``); no drifting second
        copy of the DDL may appear."""
        from vesmaro.storage import sqlite_store

        assert sqlite_store._EDGE_STATS_NO_DELETE_TRIGGER_DDL in sqlite_store._DB_SCHEMA


# ── Review #338 N3/N4: one-aggregation telemetry; public kinds constant ─────


class TestSingleAggregationTelemetry:
    def test_count_by_kind_one_query_exact_total(self, store: SQLiteStore) -> None:
        store.record_edge_stat_event("e1", "m-a", kind="used", project="p")
        store.record_edge_stat_event("e2", "m-b", kind="used", project="p")
        store.record_edge_stat_event("e3", "m-b", kind="rejected", project="p")
        assert store.count_edge_stats_by_kind() == {"used": 2, "rejected": 1}
        # The CHECK on kind guarantees the by-kind counts PARTITION the
        # table: their sum IS the total, no second counting pass.
        assert sum(store.count_edge_stats_by_kind().values()) == store.count_edge_stats()

    def test_count_by_kind_empty_table_zeroed(self, store: SQLiteStore) -> None:
        assert store.count_edge_stats_by_kind() == {"used": 0, "rejected": 0}

    def test_stats_read_issues_one_aggregation_no_full_scans(
        self, manager_on: MemoryManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """N3 regression pin: feedback_capture_stats must issue exactly
        ONE durable read (the GROUP BY) and must NOT fall back to the
        per-kind/full-table count scans it replaced."""
        m = _add(manager_on, "counted once, scanned once")
        manager_on.report_search_feedback([m.id], kind="used", project=PROJECT, event_id="r1")

        real = manager_on.sqlite.count_edge_stats_by_kind
        by_kind_spy = MagicMock(side_effect=real)
        full_scan_trap = MagicMock(side_effect=AssertionError("full-scan count_edge_stats used"))
        monkeypatch.setattr(manager_on.sqlite, "count_edge_stats_by_kind", by_kind_spy)
        monkeypatch.setattr(manager_on.sqlite, "count_edge_stats", full_scan_trap)

        stats = manager_on.feedback_capture_stats()
        assert by_kind_spy.call_count == 1
        assert full_scan_trap.call_count == 0
        assert stats["events_total"] == 1
        assert stats["captured_used_total"] == 1
        assert stats["captured_rejected_total"] == 0

    def test_kinds_constant_is_public_api(self) -> None:
        """N4: EDGE_STATS_KINDS is public (cross-module import surface)
        and the private spelling is gone — no private cross-module import."""
        from vesmaro.storage import sqlite_store

        assert frozenset({"used", "rejected"}) == sqlite_store.EDGE_STATS_KINDS
        assert not hasattr(sqlite_store, "_EDGE_STATS_KINDS")
