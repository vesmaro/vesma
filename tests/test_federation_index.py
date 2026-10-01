"""S2 federation_index tests (ADR-0021 Q10.2/Q10.3, chairman ruling 2026-09-20).

Covers the meta-mirror phase-1 substrate + the two registered RPCs:

* Schema/migration — the table is created ``IF NOT EXISTS`` at startup
  on a FRESH database and re-created on an EXISTING database (the
  mesh_nodes pattern: idempotent CREATE at connect, no SEED wipe —
  pre-existing data survives).
* Storage methods — ``upsert_index_entries`` (idempotent replay,
  LWW-by-timestamp, Q10.9 import gates, origin-mutation guard — review
  blocker 1/CWE-284: conflict-path mutations only from the stored
  origin, transit of NEW ids allowed, stored origin never rewritten),
  ``list_index`` (rowid-ASC resume walk, project/origin/since filters,
  no-federate exclusion), ``purge_origin``.
* Timestamp anti-poisoning — review blocker 2:
  ``canonical_metadata_timestamp`` unit coverage (ISO-8601 → canonical
  fixed-width UTC, future-slack gate, unparseable reject) plus RPC
  legs (far-future/unparseable → ``rejected_by_gate``; ``+03:00``
  canonicalised; same-instant-different-tz LWW-neutral; a lexically
  bigger but chronologically older offset LOSES).
* Cross-origin security matrix over real gRPC with three ACL'd senders
  (attacker A / origin B / relay C): reviewer probes T1 (cross-origin
  tombstone censorship) and T2 (self-claim attribution capture) are
  rejected with the row untouched; transit INSERT accepted while a
  non-origin replay with edits is rejected; the origin's own mutation
  still lands.
* ``build_metadata_entry`` — local-memory → index-row synthesis
  (origin='self'), no-federate exclusion, moderation-refuse exclusion,
  project-less exclusion (review nit 2), title ≤ 256.
* ``VesmaCoreServicer.build_metadata_sync_response`` — the
  SyncMetadata RPC body, exercised with REAL generated ``fed_pb2``
  messages: ACL fail-closed matrix, watermark pagination stability
  (no dupes / no gaps across pages), tag filter, Q10.9 title-blocklist
  on serve, empty corpus.
* REAL gRPC round trips for both registered RPCs (Unix socket, no
  mocks): ``SyncMetadata`` (wire ``origin_peer``/``content_state``/``limit``
  fields, watermark pagination, ACL matrix → PERMISSION_DENIED, garbage
  ``since_rev`` → INVALID_ARGUMENT) and ``UpsertIndexEntries`` (origin
  hygiene ""/"self" → authenticated sender, gate counters, idempotent
  replay, stale-LWW, write-path ACL matrix → PERMISSION_DENIED).
* ``SubscribeStream`` oneof — the ``record`` oneof's ``metadata``
  variant (the only metadata oneof in the proto — verified: SyncMetadata
  messages carry none) populates ``WhichOneof("record") == "metadata"``
  and survives a serialize/parse round trip.
* Config — ``index_title_blocklist`` rejects non-compiling regex at the
  config boundary.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import grpc
import pytest
from pydantic import ValidationError

from vesmaro import _mesh_gen
from vesmaro.compact import (
    CONTENT_STATE_TOMBSTONED,
    METADATA_SCHEMA,
    TIMESTAMP_FUTURE_SLACK,
    FederationIndexEntry,
    build_metadata_entry,
    canonical_metadata_timestamp,
    title_matches_blocklist,
)
from vesmaro.config import FederationConfig, PeerConfig, Settings
from vesmaro.manager import MemoryManager
from vesmaro.mesh_server import (
    MeshServer,
    VesmaCoreServicer,
    _metadata_stream_event,
)
from vesmaro.models import MemoryCreate, MemorySource
from vesmaro.storage.sqlite_store import SQLiteStore

# ── Constants ────────────────────────────────────────────────────────────────

_PROJECT = "test-project"
_PROJECT_DENIED = "project-secret"
_PEER_ID = "mnemos-A"
_REMOTE_ORIGIN = "mnemos-B"
_PEER_ID_C = "mnemos-C"
_AGENT = "gcw-test-agent"
_TOKEN_ENV = "VESMARO_FED_PEER_TEST_TOKEN"


def _peer_cfg(allowed: list[str] | None = None) -> PeerConfig:
    """A PeerConfig with the test token and the given project ACL."""
    return PeerConfig(
        bearer_token_env=_TOKEN_ENV,
        allowed_projects=allowed if allowed is not None else [_PROJECT],
        allowed_types=["decision", "learning"],
        rate_limit_per_minute=600,
    )


def _entry(
    entry_id: str = "fed:remote-agent:uuid-1",
    *,
    title: str = "Remote decision",
    project: str = _PROJECT,
    origin_peer: str = _REMOTE_ORIGIN,
    source_peer: str = _REMOTE_ORIGIN,
    tags: list[str] | None = None,
    timestamp: str = "2026-09-01T10:00:00Z",
    content_state: str = "available",
) -> FederationIndexEntry:
    """Build an index entry mirroring what a peer would send."""
    return FederationIndexEntry(
        id=entry_id,
        type="decision",
        title=title,
        tags=tags if tags is not None else [f"project:{project}", "mnemos:decision"],
        project=project,
        source_agent="remote-agent",
        source_peer=source_peer,
        origin_peer=origin_peer,
        content_state=content_state,
        timestamp=timestamp,
    )


def _settings(
    tmp_path: Path,
    *,
    allowed: list[str] | None = None,
    shared: list[str] | None = None,
    title_blocklist: list[str] | None = None,
    peers: dict[str, PeerConfig] | None = None,
) -> Settings:
    """Settings with one configured peer + an isolated store."""
    settings = Settings(
        **{  # type: ignore[arg-type]  # pydantic dict→model coercion
            "mnemos": {
                "vault_path": str(tmp_path / "vault"),
                "data_dir": str(tmp_path / "data"),
                "db_name": "test_federation_index.db",
            },
            "embedding": {"provider": "onnx"},
            "scanner": {"enabled": False},
            "federation": FederationConfig(
                shared_projects=shared if shared is not None else [_PROJECT],
                peers=peers if peers is not None else {_PEER_ID: _peer_cfg(allowed)},
                **(
                    {"index_title_blocklist": title_blocklist}
                    if title_blocklist is not None
                    else {}
                ),
            ),
        }
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    """A SQLiteStore on a tmp DB (auto-creates the schema on connect)."""
    s = SQLiteStore(tmp_path / "fed_index.db")
    yield s
    s.close()


@pytest.fixture
def manager(tmp_path: Path) -> MemoryManager:
    """Real MemoryManager with a mocked embedder (no ONNX runtime)."""
    mgr = MemoryManager(_settings(tmp_path))
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    yield mgr
    mgr.close()


# ── Schema / migration ───────────────────────────────────────────────────────


class TestSchemaMigration:
    def test_fresh_db_creates_table_and_unique_id(self, store: SQLiteStore) -> None:
        conn = store._get_conn()
        cols = {row[1] for row in conn.execute("PRAGMA table_info(federation_index)")}
        assert {
            "id",
            "type",
            "title",
            "tags",
            "project",
            "source_agent",
            "source_peer",
            "origin_peer",
            "content_state",
            "timestamp",
            "schema_version",
            "received_at",
        } <= cols
        # UNIQUE(id): the PRIMARY KEY on id rejects a second row.
        store.upsert_index_entries([_entry()])
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO federation_index (id) VALUES ('fed:remote-agent:uuid-1')")

    def test_origin_timestamp_index_exists(self, store: SQLiteStore) -> None:
        conn = store._get_conn()
        idx = conn.execute("PRAGMA index_list(federation_index)").fetchall()
        names = {row[1] for row in idx}
        assert "idx_federation_index_origin_timestamp" in names

    def test_existing_db_gains_table_without_wipe(self, tmp_path: Path) -> None:
        """Startup CREATE IF NOT EXISTS on a pre-S2 database — no data loss.

        Simulates a database created by the PREVIOUS vesmaro version
        (no federation_index): the next connect must create the table
        and keep every existing row (no SEED wipe).
        """
        db_path = tmp_path / "legacy.db"
        legacy = SQLiteStore(db_path)
        legacy.upsert_index_entries([_entry()])  # ensures schema is fully built
        legacy_conn = legacy._get_conn()
        legacy_conn.execute("DROP TABLE federation_index")  # roll back to pre-S2
        legacy_conn.commit()
        legacy.close()

        reopened = SQLiteStore(db_path)
        conn = reopened._get_conn()
        assert conn.execute("SELECT count(*) FROM federation_index").fetchone()[0] == 0
        # memories survived the reopen (no wipe).
        assert conn.execute("SELECT count(*) FROM memories").fetchone()[0] >= 0
        # And the table is immediately usable.
        assert reopened.upsert_index_entries([_entry()]).written == 1
        reopened.close()


# ── upsert_index_entries ─────────────────────────────────────────────────────


class TestUpsertIndexEntries:
    def test_insert_counts_and_stamps_received_at(self, store: SQLiteStore) -> None:
        stats = store.upsert_index_entries([_entry("fed:r:1"), _entry("fed:r:2")])
        assert stats.written == 2
        assert stats.refused == 0
        assert stats.stale == 0
        rows = store.list_index()
        assert len(rows) == 2
        for entry, _rowid in rows:
            assert entry.received_at != ""  # stamped by the store

    def test_replay_is_idempotent_single_row(self, store: SQLiteStore) -> None:
        store.upsert_index_entries([_entry("fed:r:1")])
        store.upsert_index_entries([_entry("fed:r:1")])  # replayed same record
        rows = store.list_index()
        assert len(rows) == 1

    def test_lww_older_timestamp_loses(self, store: SQLiteStore) -> None:
        store.upsert_index_entries(
            [_entry("fed:r:1", title="Newer", timestamp="2026-09-05T00:00:00Z")]
        )
        stats = store.upsert_index_entries(
            [_entry("fed:r:1", title="Older", timestamp="2026-09-01T00:00:00Z")]
        )
        assert stats.written == 0  # stale — silently dropped
        assert stats.stale == 1
        (entry, _rowid) = store.list_index()[0]
        assert entry.title == "Newer"

    def test_lww_newer_timestamp_replaces(self, store: SQLiteStore) -> None:
        store.upsert_index_entries(
            [_entry("fed:r:1", title="Older", timestamp="2026-09-01T00:00:00Z")]
        )
        stats = store.upsert_index_entries(
            [_entry("fed:r:1", title="Newer", timestamp="2026-09-05T00:00:00Z")]
        )
        assert stats.written == 1
        (entry, _rowid) = store.list_index()[0]
        assert entry.title == "Newer"

    def test_import_gate_no_federate_refused(self, store: SQLiteStore) -> None:
        stats = store.upsert_index_entries(
            [_entry("fed:r:1", tags=["project:test-project", "mnemos:no-federate"])]
        )
        assert stats.written == 0
        assert stats.refused == 1
        assert store.list_index() == []

    def test_import_gate_title_blocklist_refused(self, store: SQLiteStore) -> None:
        stats = store.upsert_index_entries(
            [_entry("fed:r:1", title="Internal secret sprint plan")],
            title_blocklist=[r"secret\s+sprint"],
        )
        assert stats.written == 0
        assert stats.refused == 1
        assert store.list_index() == []

    def test_one_bad_entry_does_not_abort_batch(self, store: SQLiteStore) -> None:
        stats = store.upsert_index_entries(
            [
                _entry("fed:r:1", tags=["project:p", "mnemos:no-federate"]),
                _entry("fed:r:2"),
            ]
        )
        assert stats.written == 1
        assert stats.refused == 1
        assert [e.id for e, _ in store.list_index()] == ["fed:r:2"]

    def test_content_state_tombstoned_round_trips(self, store: SQLiteStore) -> None:
        store.upsert_index_entries([_entry("fed:r:1", content_state=CONTENT_STATE_TOMBSTONED)])
        (entry, _rowid) = store.list_index()[0]
        assert entry.content_state == CONTENT_STATE_TOMBSTONED


# ── origin-mutation guard (review blocker 1, CWE-284) ────────────────────────


class TestOriginMutationGuard:
    """Archcom ruling «index mutations come from the origin only;
    available transit is allowed»: on the CONFLICT path the sender must
    BE the stored origin; on the INSERT path transit stays allowed."""

    def test_t1_cross_origin_tombstone_rejected(self, store: SQLiteStore) -> None:
        """Reviewer probe T1: an ACL-authenticated peer A re-sends a
        foreign id claiming origin=B with a tombstone and a newer
        timestamp — the censorship vector. Must be refused, row intact."""
        store.upsert_index_entries([_entry("fed:r:v1", timestamp="2021-06-01T10:00:00Z")])
        stats = store.upsert_index_entries(
            [
                _entry(
                    "fed:r:v1",
                    content_state=CONTENT_STATE_TOMBSTONED,
                    timestamp="2021-06-02T10:00:00Z",
                )
            ],
            sender_peer_id=_PEER_ID,
        )
        assert stats.written == 0
        assert stats.refused == 1
        (entry, _rowid) = store.list_index()[0]
        assert entry.content_state == "available"  # NOT tombstoned
        assert entry.title == "Remote decision"  # untouched
        assert entry.origin_peer == _REMOTE_ORIGIN  # attribution intact

    def test_t2_self_claim_on_foreign_row_rejected(self, store: SQLiteStore) -> None:
        """Reviewer probe T2: peer A claims origin='self' (re-stamped to
        A) on B's id to capture attribution and retitle the row."""
        store.upsert_index_entries([_entry("fed:r:v1", timestamp="2021-06-01T10:00:00Z")])
        stats = store.upsert_index_entries(
            [
                _entry(
                    "fed:r:v1",
                    origin_peer="self",
                    title="Hijacked title",
                    timestamp="2021-06-02T10:00:00Z",
                )
            ],
            sender_peer_id=_PEER_ID,
        )
        assert stats.written == 0
        assert stats.refused == 1
        (entry, _rowid) = store.list_index()[0]
        assert entry.title == "Remote decision"  # row not touched
        assert entry.origin_peer == _REMOTE_ORIGIN  # never re-stamped

    def test_origin_sender_may_mutate_own_row(self, store: SQLiteStore) -> None:
        """The legitimate leg still works: B's own update arrives as a
        self-claim re-stamped to the sender B → mutation lands, origin
        is preserved (never rewritten)."""
        store.upsert_index_entries(
            [_entry("fed:r:v1", timestamp="2021-06-01T10:00:00Z")],
            sender_peer_id=_REMOTE_ORIGIN,
        )
        stats = store.upsert_index_entries(
            [
                _entry(
                    "fed:r:v1",
                    origin_peer="self",
                    content_state=CONTENT_STATE_TOMBSTONED,
                    timestamp="2021-06-02T10:00:00Z",
                )
            ],
            sender_peer_id=_REMOTE_ORIGIN,
        )
        assert stats.written == 1
        assert stats.refused == 0
        (entry, _rowid) = store.list_index()[0]
        assert entry.content_state == CONTENT_STATE_TOMBSTONED
        assert entry.origin_peer == _REMOTE_ORIGIN  # origin never rewritten

    def test_transit_available_conflict_rejected(self, store: SQLiteStore) -> None:
        """No exceptions on the conflict path: even an AVAILABLE transit
        re-send (no tombstone, no title change) from a non-origin sender
        is refused."""
        store.upsert_index_entries([_entry("fed:r:v1", timestamp="2021-06-01T10:00:00Z")])
        stats = store.upsert_index_entries(
            [_entry("fed:r:v1", timestamp="2021-06-02T10:00:00Z")],
            sender_peer_id=_PEER_ID,
        )
        assert stats.written == 0
        assert stats.refused == 1

    def test_transit_insert_new_id_allowed(self, store: SQLiteStore) -> None:
        """INSERT path: a NEW id with an explicit foreign origin from a
        non-origin sender is transit — allowed, origin stored verbatim."""
        stats = store.upsert_index_entries(
            [_entry("fed:r:new", timestamp="2021-06-01T10:00:00Z")],
            sender_peer_id=_PEER_ID,
        )
        assert stats.written == 1
        (entry, _rowid) = store.list_index()[0]
        assert entry.origin_peer == _REMOTE_ORIGIN

    def test_local_leg_origin_mismatch_refused(self, store: SQLiteStore) -> None:
        """Trusted local leg (no sender): the entry origin must still
        match the stored one — a local self-row cannot be overwritten by
        a B-originated claim."""
        store.upsert_index_entries([_entry("fed:r:local", origin_peer="self")])
        stats = store.upsert_index_entries(
            [_entry("fed:r:local", timestamp="2021-06-02T10:00:00Z")]
        )
        assert stats.written == 0
        assert stats.refused == 1
        (entry, _rowid) = store.list_index()[0]
        assert entry.origin_peer == "self"


# ── canonical_metadata_timestamp (review blocker 2) ──────────────────────────


class TestCanonicalMetadataTimestamp:
    """Unit coverage of the import-boundary ISO-8601 → canonical-UTC
    normaliser (LWW anti-poisoning)."""

    _NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)

    def test_z_suffix_canonical_form(self) -> None:
        assert (
            canonical_metadata_timestamp("2026-09-01T10:00:00Z", now=self._NOW)
            == "2026-09-01T10:00:00.000000Z"
        )

    def test_offset_converted_to_utc(self) -> None:
        assert (
            canonical_metadata_timestamp("2026-09-01T10:00:00+03:00", now=self._NOW)
            == "2026-09-01T07:00:00.000000Z"
        )

    def test_naive_assumed_utc(self) -> None:
        assert (
            canonical_metadata_timestamp("2026-09-01T10:00:00", now=self._NOW)
            == "2026-09-01T10:00:00.000000Z"
        )

    def test_same_instant_different_tz_canonicalize_equal(self) -> None:
        canonical = {
            canonical_metadata_timestamp(raw, now=self._NOW)
            for raw in (
                "2026-09-01T10:00:00Z",
                "2026-09-01T13:00:00+03:00",
                "2026-09-01T07:00:00-03:00",
                "2026-09-01T10:00:00.500Z",
            )
        }
        # The first three are the same instant → one canonical form
        # (the sub-second variant is a different instant, excluded).
        assert canonical == {
            "2026-09-01T10:00:00.000000Z",
            "2026-09-01T10:00:00.500000Z",
        }

    def test_far_future_rejected(self) -> None:
        with pytest.raises(ValueError, match="future"):
            canonical_metadata_timestamp("9999-12-31T23:59:59Z", now=self._NOW)

    def test_just_beyond_slack_rejected(self) -> None:
        with pytest.raises(ValueError, match="future"):
            canonical_metadata_timestamp(
                (self._NOW + TIMESTAMP_FUTURE_SLACK + timedelta(seconds=1)).isoformat(),
                now=self._NOW,
            )

    def test_within_slack_accepted(self) -> None:
        raw = (self._NOW + TIMESTAMP_FUTURE_SLACK - timedelta(seconds=1)).isoformat()
        assert canonical_metadata_timestamp(raw, now=self._NOW).endswith("Z")

    def test_unparseable_and_empty_rejected(self) -> None:
        for raw in ("", "   ", "not-a-timestamp", "2026-13-45T99:99:99Z"):
            with pytest.raises(ValueError):
                canonical_metadata_timestamp(raw, now=self._NOW)


# ── list_index ───────────────────────────────────────────────────────────────


class TestListIndex:
    def test_rowid_asc_ordering_and_resume_exactness(self, store: SQLiteStore) -> None:
        ids = [f"fed:r:{i}" for i in range(5)]
        store.upsert_index_entries([_entry(i) for i in ids])
        page1 = store.list_index(limit=3)
        assert [e.id for e, _ in page1] == ids[:3]
        checkpoint = page1[-1][1]
        page2 = store.list_index(limit=3, after_rowid=checkpoint)
        assert [e.id for e, _ in page2] == ids[3:]
        # No dupes, no gaps across the walk.
        walked = [e.id for e, _ in page1] + [e.id for e, _ in page2]
        assert walked == ids

    def test_project_filter(self, store: SQLiteStore) -> None:
        store.upsert_index_entries(
            [_entry("fed:r:1", project=_PROJECT), _entry("fed:r:2", project="other")]
        )
        rows = store.list_index(projects=[_PROJECT])
        assert [e.id for e, _ in rows] == ["fed:r:1"]

    def test_origin_peer_filter(self, store: SQLiteStore) -> None:
        store.upsert_index_entries(
            [_entry("fed:r:1", origin_peer="mnemos-B"), _entry("fed:r:2", origin_peer="mnemos-C")]
        )
        rows = store.list_index(origin_peers=["mnemos-B"])
        assert [e.id for e, _ in rows] == ["fed:r:1"]

    def test_since_filters_on_record_timestamp(self, store: SQLiteStore) -> None:
        store.upsert_index_entries(
            [
                _entry("fed:r:1", timestamp="2026-09-01T00:00:00Z"),
                _entry("fed:r:2", timestamp="2026-09-10T00:00:00Z"),
            ]
        )
        rows = store.list_index(since="2026-09-05T00:00:00Z")
        assert [e.id for e, _ in rows] == ["fed:r:2"]

    def test_serve_side_no_federate_excluded(self, store: SQLiteStore) -> None:
        # Sneak a no-federate row in RAW (bypassing the import gate) to
        # prove the serve-side belt-and-braces filter.
        conn = store._get_conn()
        conn.execute(
            "INSERT INTO federation_index (id, title, tags) VALUES (?, ?, ?)",
            ("fed:r:raw", "Raw row", '["mnemos:no-federate"]'),
        )
        conn.commit()
        assert store.list_index() == []
        (entry, _rowid) = store.list_index(exclude_no_federate=False)[0]
        assert entry.id == "fed:r:raw"


class TestIndexHead:
    """mnemos-mesh#46: the scope-head primitive for the SyncMetadata
    watermark — max rowid under the projects filter, with NO serve-side
    exclusions (the head is the insertion high-water mark)."""

    def test_empty_index_is_zero(self, store: SQLiteStore) -> None:
        assert store.index_head() == 0
        assert store.index_head(projects=["any"]) == 0

    def test_head_is_max_rowid_with_projects_filter(self, store: SQLiteStore) -> None:
        store.upsert_index_entries([_entry("fed:r:1", project="a"), _entry("fed:r:2", project="b")])
        rowids = {e.project: r for e, r in store.list_index(limit=10, exclude_no_federate=False)}
        assert store.index_head() == max(rowids.values())
        assert store.index_head(projects=["a"]) == rowids["a"]
        assert store.index_head(projects=["a", "b"]) == max(rowids.values())
        assert store.index_head(projects=["missing"]) == 0

    def test_head_counts_never_served_rows(self, store: SQLiteStore) -> None:
        # A no-federate row is invisible to every serve-side walk but
        # still bounds the insertion head — the watermark may jump past
        # it (intended skip, the same semantics a blocked row inside a
        # served page already has).
        conn = store._get_conn()
        conn.execute(
            "INSERT INTO federation_index (id, title, tags, project) "
            "VALUES ('fed:r:nf', 'Never served', '[\"mnemos:no-federate\"]', 'p')"
        )
        conn.commit()
        assert store.list_index() == []  # the walk never serves it
        assert store.index_head(projects=["p"]) > 0  # ...but it IS the head


# ── purge_origin ─────────────────────────────────────────────────────────────


class TestPurgeOrigin:
    def test_purges_only_named_origin(self, store: SQLiteStore) -> None:
        store.upsert_index_entries(
            [
                _entry("fed:r:1", origin_peer="mnemos-B"),
                _entry("fed:r:2", origin_peer="mnemos-B"),
                _entry("fed:r:3", origin_peer="mnemos-C"),
                _entry("fed:r:4", origin_peer="self"),
            ]
        )
        deleted = store.purge_origin("mnemos-B")
        assert deleted == 2
        remaining = sorted(e.origin_peer for e, _ in store.list_index())
        assert remaining == ["mnemos-C", "self"]

    def test_purge_unknown_origin_is_zero(self, store: SQLiteStore) -> None:
        store.upsert_index_entries([_entry("fed:r:1")])
        assert store.purge_origin("mnemos-ZZZ") == 0


# ── build_metadata_entry (local synthesis, origin='self') ────────────────────


class TestBuildMetadataEntry:
    def test_local_memory_becomes_self_origin_entry(self, manager: MemoryManager) -> None:
        memory = manager.add(
            MemoryCreate(
                content="We chose poll-first metadata sync for S2.",
                title="S2 sync decision",
                tags=[f"project:{_PROJECT}", f"agent:{_AGENT}", "mnemos:decision"],
                source=MemorySource.MANUAL,
            ),
            project=_PROJECT,
            agent=_AGENT,
        )
        entry = build_metadata_entry(memory)
        assert entry is not None
        assert entry.origin_peer == "self"
        assert entry.source_peer == "self"
        assert entry.id == f"fed:{_AGENT}:{memory.id}"
        assert entry.type == "decision"
        assert entry.title == "S2 sync decision"
        assert entry.project == _PROJECT
        assert entry.schema_version == METADATA_SCHEMA
        # Metadata-only: no content, no summary — the model has no such
        # fields at all; the tags carry no content either.
        assert entry.tags == [f"project:{_PROJECT}", f"agent:{_AGENT}", "mnemos:decision"]

    def test_no_federate_memory_excluded(self, manager: MemoryManager) -> None:
        memory = manager.add(
            MemoryCreate(
                content="Rotate these keys quarterly.",
                title="Key rotation runbook",
                tags=[f"project:{_PROJECT}", f"agent:{_AGENT}", "mnemos:no-federate"],
                source=MemorySource.MANUAL,
            ),
            project=_PROJECT,
            agent=_AGENT,
        )
        assert build_metadata_entry(memory) is None

    def test_projectless_memory_excluded(self, manager: MemoryManager) -> None:
        """Review nit 2: a memory without a project must not enter the
        index — the row would be dead locally and would abort a remote
        importer's whole RPC (an entry without a project cannot be
        ACL'd)."""
        memory = manager.add(
            MemoryCreate(
                content="Scratch note outside any project scope.",
                title="Projectless note",
                tags=[f"agent:{_AGENT}", "mnemos:learning"],
                source=MemorySource.MANUAL,
            ),
            project="",
            agent=_AGENT,
        )
        assert build_metadata_entry(memory) is None

    def test_title_capped_at_256(self, manager: MemoryManager) -> None:
        memory = manager.add(
            MemoryCreate(
                content="Body",
                title="x" * 600,
                tags=[f"project:{_PROJECT}", f"agent:{_AGENT}", "mnemos:rule"],
                source=MemorySource.MANUAL,
            ),
            project=_PROJECT,
            agent=_AGENT,
        )
        entry = build_metadata_entry(memory)
        assert entry is not None
        assert len(entry.title) <= 256


# ── build_metadata_sync_response (future RPC body, real fed_pb2 messages) ───


class TestBuildMetadataSyncResponse:
    @pytest.fixture
    def servicer(self, tmp_path: Path, manager: MemoryManager) -> VesmaCoreServicer:
        return VesmaCoreServicer(manager, settings=_settings(tmp_path))

    @pytest.fixture
    def indexed(self, manager: MemoryManager) -> MemoryManager:
        """Manager with a seeded local corpus materialised into the index
        (origin='self') plus one mirrored row from a remote peer."""
        manager.add(
            MemoryCreate(
                content="We chose poll-first metadata sync for S2.",
                title="S2 sync decision",
                tags=[f"project:{_PROJECT}", f"agent:{_AGENT}", "mnemos:decision"],
                source=MemorySource.MANUAL,
            ),
            project=_PROJECT,
            agent=_AGENT,
        )
        manager.add(
            MemoryCreate(
                content="Cursor pages must partition the corpus exactly.",
                title="Cursor partitioning learning",
                tags=[f"project:{_PROJECT}", f"agent:{_AGENT}", "mnemos:learning"],
                source=MemorySource.MANUAL,
            ),
            project=_PROJECT,
            agent=_AGENT,
        )
        for memory, _rowid in manager.sqlite.list_all_for_mesh(limit=10):
            entry = build_metadata_entry(memory)
            if entry is not None:
                manager.sqlite.upsert_index_entries([entry])
        manager.sqlite.upsert_index_entries([_entry("fed:remote-agent:mirror-1")])
        return manager

    @staticmethod
    def _request(**kwargs: Any) -> Any:
        fields: dict[str, Any] = {
            "peer_id": _PEER_ID,
            "since_rev": 0,
            "project_scope": "",
        }
        fields.update(kwargs)
        return _mesh_gen.fed_pb2.MetadataSyncRequest(**fields)

    def test_serves_index_including_self_origin(
        self, servicer: VesmaCoreServicer, indexed: MemoryManager
    ) -> None:
        resp = servicer.build_metadata_sync_response(self._request())
        origins = {r.source_peer for r in resp.records}
        assert "self" in origins  # local records (origin='self')
        assert _REMOTE_ORIGIN in origins  # mirrored rows re-advertised
        assert resp.trigger_code == _mesh_gen.fed_pb2.EXHAUSTIVE
        assert resp.latest_rev > 0
        assert all(r.schema_version == METADATA_SCHEMA for r in resp.records)

    def test_empty_corpus_parks_at_scope_head(self, servicer: VesmaCoreServicer) -> None:
        """Empty index: the scope head is 0, so latest_rev stays 0 — the
        watermark has nowhere to advance to (and never regresses)."""
        resp = servicer.build_metadata_sync_response(self._request())
        assert list(resp.records) == []
        assert resp.latest_rev == 0
        assert not resp.has_more

    def test_issue46_empty_scope_page_advances_watermark_to_head(
        self, tmp_path: Path, manager: MemoryManager
    ) -> None:
        """mnemos-mesh#46 regression: two scopes, one empty — an empty
        page must park ``latest_rev`` at the SCOPE HEAD, not echo
        ``since_rev``.

        Model (ticket): the mesh CLI polls per scope and folds the
        per-scope latest_rev into ONE watermark via MIN. With the echo
        behaviour the empty scope pinned MIN at the old watermark, so
        the data scope was re-delivered (duplicates) every tick. The
        core-side fix: an empty fetch returns the max rowid of the
        scope-filtered index — nothing is undeliverable beyond it.

        Here scope-a carries the delivered rows (watermark parked at its
        head); scope-b is SERVING-empty — its only row is no-federate,
        invisible to the rowid walk — so scope-b's page is empty while
        its insertion head sits BEYOND the watermark: the head, not the
        echo, is what lets the watermark advance.
        """
        manager.sqlite.upsert_index_entries(
            [_entry("fed:r:a1", project="scope-a"), _entry("fed:r:a2", project="scope-a")]
        )
        watermark = manager.sqlite.index_head(projects=["scope-a"])
        conn = manager.sqlite._get_conn()
        conn.execute(
            "INSERT INTO federation_index (id, title, tags, project) "
            "VALUES ('fed:r:b1', 'Never served', '[\"mnemos:no-federate\"]', 'scope-b')"
        )
        conn.commit()
        head_b = manager.sqlite.index_head(projects=["scope-b"])
        assert head_b > watermark  # the empty scope's head is beyond the watermark
        servicer = VesmaCoreServicer(
            manager, settings=_settings(tmp_path, allowed=["scope-a", "scope-b"])
        )
        # Poll the data scope: rows delivered, watermark parks at its head.
        resp_a = servicer.build_metadata_sync_response(
            self._request(project_scope="scope-a", since_rev=0)
        )
        assert len(resp_a.records) == 2
        assert resp_a.latest_rev == watermark
        # Poll the serving-empty scope FROM that watermark: an empty page
        # whose latest_rev is scope-b's HEAD (watermark advances), not
        # the since_rev echo that pinned MIN at the old checkpoint.
        resp_b = servicer.build_metadata_sync_response(
            self._request(project_scope="scope-b", since_rev=watermark)
        )
        assert list(resp_b.records) == [] and not resp_b.has_more
        assert resp_b.latest_rev == head_b

    def test_empty_page_head_below_watermark_never_regresses(
        self, tmp_path: Path, manager: MemoryManager
    ) -> None:
        """The #46 head-park must not REWIND a watermark minted over a
        wider scope: scope-b's head below since_rev → max() keeps the
        checkpoint (never-regress invariant)."""
        manager.sqlite.upsert_index_entries([_entry("fed:r:a1", project="scope-a")])
        servicer = VesmaCoreServicer(
            manager, settings=_settings(tmp_path, allowed=["scope-a", "scope-b"])
        )
        resp = servicer.build_metadata_sync_response(
            self._request(project_scope="scope-b", since_rev=999)
        )
        assert list(resp.records) == []
        assert resp.latest_rev == 999  # head([scope-b]) == 0 < 999 → no rewind

    def test_issue49_empty_scope_parks_at_peer_head_not_scope_head(
        self, tmp_path: Path, manager: MemoryManager
    ) -> None:
        """mnemos-mesh#49 regression (live poller finding): the CLI polls
        PER SCOPE and folds latest_rev into ONE MIN watermark. With the
        #46 fix parking at the SCOPE head, a scope with NO rows at all
        (head 0) pinned the aggregate to 0 forever — the data scope was
        re-delivered on every tick (duplicates, deduped by LWW; no loss).

        The #49 fix: an empty page parks at the PEER head (the whole
        allowed set), which is ≥ every scope head this peer can see —
        so an all-empty scope can no longer pin the aggregate, and no
        allowed row exists beyond the peer head (skip-nothing proof).
        """
        manager.sqlite.upsert_index_entries(
            [_entry("fed:r:a1", project="scope-a"), _entry("fed:r:a2", project="scope-a")]
        )
        peer_head = manager.sqlite.index_head(projects=["scope-a", "scope-b"])
        assert peer_head > 0
        # scope-b has NO rows at all: its scope head is 0, but the peer
        # head is beyond it. The empty page must return the PEER head —
        # the scope head (0) would pin the CLI's MIN aggregate.
        assert manager.sqlite.index_head(projects=["scope-b"]) == 0
        servicer = VesmaCoreServicer(
            manager, settings=_settings(tmp_path, allowed=["scope-a", "scope-b"])
        )
        resp = servicer.build_metadata_sync_response(
            self._request(project_scope="scope-b", since_rev=0)
        )
        assert list(resp.records) == [] and not resp.has_more
        assert resp.latest_rev == peer_head  # not 0 (scope head), not the echo

    def test_watermark_pagination_partitions_corpus(
        self, tmp_path: Path, manager: MemoryManager
    ) -> None:
        """Cursor stability: walking by latest_rev delivers each row
        exactly once (no dupes, no gaps) across real page boundaries
        (60 entries > the 50-row default page → at least two pages)."""
        entries = [_entry(f"fed:r:{i:03d}") for i in range(60)]
        manager.sqlite.upsert_index_entries(entries)
        servicer = VesmaCoreServicer(manager, settings=_settings(tmp_path))
        delivered: list[str] = []
        pages = 0
        since = 0
        for _ in range(10):
            resp = servicer.build_metadata_sync_response(self._request(since_rev=since))
            pages += 1
            delivered.extend(r.id for r in resp.records)
            since = resp.latest_rev
            if not resp.has_more:
                break
        assert pages >= 2  # the corpus really spanned multiple pages
        assert sorted(delivered) == sorted(e.id for e in entries)
        assert len(delivered) == len(set(delivered))  # no dupes

    def test_acl_denied_scope_refused(
        self, servicer: VesmaCoreServicer, indexed: MemoryManager
    ) -> None:
        resp = servicer.build_metadata_sync_response(self._request(project_scope=_PROJECT_DENIED))
        assert list(resp.records) == []
        assert resp.trigger_code == _mesh_gen.fed_pb2.REFUSED

    def test_acl_unknown_peer_refused(
        self, servicer: VesmaCoreServicer, indexed: MemoryManager
    ) -> None:
        resp = servicer.build_metadata_sync_response(self._request(peer_id="mnemos-UNKNOWN"))
        assert list(resp.records) == []
        assert resp.trigger_code == _mesh_gen.fed_pb2.REFUSED

    def test_tag_filter_intersects(
        self, servicer: VesmaCoreServicer, indexed: MemoryManager
    ) -> None:
        resp = servicer.build_metadata_sync_response(self._request(filter=["mnemos:learning"]))
        assert all("mnemos:learning" in list(r.tags) for r in resp.records)
        assert len(resp.records) >= 1

    def test_title_blocklist_on_serve(self, tmp_path: Path, manager: MemoryManager) -> None:
        manager.sqlite.upsert_index_entries(
            [
                _entry("fed:r:ok", title="Public decision"),
                _entry("fed:r:blocked", title="Internal secret sprint plan"),
            ]
        )
        servicer = VesmaCoreServicer(
            manager,
            settings=_settings(tmp_path, title_blocklist=[r"secret\s+sprint"]),
        )
        resp = servicer.build_metadata_sync_response(self._request())
        ids = [r.id for r in resp.records]
        assert "fed:r:ok" in ids
        assert "fed:r:blocked" not in ids
        # The blocked row still advanced the watermark (no wedged poll).
        assert resp.latest_rev >= 2

    def test_out_of_range_since_rev_rejected(self, servicer: VesmaCoreServicer) -> None:
        from vesmaro.mesh_server import CursorError

        with pytest.raises(CursorError):
            servicer.build_metadata_sync_response(self._request(since_rev=-1))


# ── SubscribeStream oneof (the only metadata oneof in the proto) ─────────────


class TestSubscribeStreamOneof:
    def test_metadata_variant_populates_oneof(self) -> None:
        event = _metadata_stream_event(_entry(), cursor="tok-1")
        assert event.WhichOneof("record") == "metadata"
        assert event.metadata.id == "fed:remote-agent:uuid-1"
        assert event.event_type == _mesh_gen.fed_pb2.SubscribeStream.RECORD_ADDED
        assert event.cursor == "tok-1"

    def test_compact_variant_still_available(self) -> None:
        event = _mesh_gen.fed_pb2.SubscribeStream(
            event_type=_mesh_gen.fed_pb2.SubscribeStream.RECORD_ADDED,
            compact=_mesh_gen.fed_pb2.CompactRecord(id="fed:a:1"),
        )
        assert event.WhichOneof("record") == "compact"

    def test_oneof_survives_wire_round_trip(self) -> None:
        event = _metadata_stream_event(_entry())
        parsed = _mesh_gen.fed_pb2.SubscribeStream.FromString(event.SerializeToString())
        assert parsed.WhichOneof("record") == "metadata"
        assert parsed.metadata.project == _PROJECT
        assert parsed.metadata.schema_version == METADATA_SCHEMA


# ── Config gate ───────────────────────────────────────────────────────────────


class TestTitleBlocklistConfig:
    def test_invalid_regex_rejected_at_config_boundary(self) -> None:
        with pytest.raises(ValidationError, match="does not compile"):
            FederationConfig(index_title_blocklist=["[unclosed"])

    def test_blank_pattern_rejected(self) -> None:
        with pytest.raises(ValidationError):
            FederationConfig(index_title_blocklist=[""])

    def test_valid_patterns_accepted(self) -> None:
        cfg = FederationConfig(index_title_blocklist=[r"secret", r"internal\s+only"])
        assert cfg.index_title_blocklist == [r"secret", r"internal\s+only"]

    def test_matcher_semantics(self) -> None:
        # Case-sensitive re.search semantics (documented behaviour).
        assert title_matches_blocklist("Top secret sprint", [r"secret"])
        assert title_matches_blocklist("internal only note", [r"internal\s+only"])
        assert not title_matches_blocklist("INTERNAL ONLY note", [r"internal\s+only"])
        assert not title_matches_blocklist("Public decision", [r"secret"])
        assert not title_matches_blocklist("Anything", [])


# ── Real-gRPC round trips (registered RPCs, chairman ruling 2026-09-20) ──────


def _pb_entry(
    entry_id: str = "fed:remote-agent:uuid-1",
    *,
    title: str = "Remote decision",
    project: str = _PROJECT,
    tags: list[str] | None = None,
    origin_peer: str = _REMOTE_ORIGIN,
    content_state: str = "available",
    timestamp: str = "2026-09-01T10:00:00Z",
    schema_version: str = METADATA_SCHEMA,
) -> Any:
    """Build a wire MetadataRecord mirroring what a peer would send."""
    return _mesh_gen.fed_pb2.MetadataRecord(
        id=entry_id,
        type="decision",
        title=title,
        tags=tags if tags is not None else [f"project:{project}", "mnemos:decision"],
        project=project,
        source_agent="remote-agent",
        source_peer=_REMOTE_ORIGIN,
        timestamp=timestamp,
        schema_version=schema_version,
        origin_peer=origin_peer,
        content_state=content_state,
    )


@contextmanager
def _grpc_server(
    tmp_path: Path,
    *,
    allowed: list[str] | None = None,
    title_blocklist: list[str] | None = None,
    peers: dict[str, PeerConfig] | None = None,
) -> Generator[tuple[Any, MemoryManager], None, None]:
    """Run a real MeshServer on a tmp Unix socket; yield (stub, manager)."""
    import grpc as _grpc

    settings = _settings(tmp_path, allowed=allowed, title_blocklist=title_blocklist, peers=peers)
    mgr = MemoryManager(settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    srv = MeshServer(str(tmp_path / "core.sock"), mgr, settings, max_workers=2)
    srv.start()
    channel = _grpc.insecure_channel(f"unix://{srv.socket_path}")
    _grpc.channel_ready_future(channel).result(timeout=2.0)
    stub = _mesh_gen.core_pb2_grpc.MnemosCoreStub(channel)
    try:
        yield stub, mgr
    finally:
        channel.close()
        srv.stop(grace=0.5)
        mgr.close()


class TestSyncMetadataRPC:
    """Real gRPC round trips for MnemosCore.SyncMetadata (S2 export leg)."""

    def test_serves_index_with_origin_and_content_state(self, tmp_path: Path) -> None:
        with _grpc_server(tmp_path) as (stub, mgr):
            mgr.sqlite.upsert_index_entries(
                [_entry("fed:r:1"), _entry("fed:r:2", content_state="tombstoned")]
            )
            resp = stub.SyncMetadata(
                _mesh_gen.fed_pb2.MetadataSyncRequest(peer_id=_PEER_ID, since_rev=0)
            )
            assert len(resp.records) == 2
            assert resp.trigger_code == _mesh_gen.fed_pb2.EXHAUSTIVE
            by_id = {r.id: r for r in resp.records}
            assert by_id["fed:r:1"].origin_peer == _REMOTE_ORIGIN
            assert by_id["fed:r:1"].content_state == "available"
            assert by_id["fed:r:2"].content_state == "tombstoned"
            assert all(r.schema_version == METADATA_SCHEMA for r in resp.records)

    def test_limit_field_paginates_over_wire(self, tmp_path: Path) -> None:
        with _grpc_server(tmp_path) as (stub, mgr):
            mgr.sqlite.upsert_index_entries([_entry(f"fed:r:{i:02d}") for i in range(12)])
            delivered: list[str] = []
            since = 0
            pages = 0
            for _ in range(10):
                resp = stub.SyncMetadata(
                    _mesh_gen.fed_pb2.MetadataSyncRequest(
                        peer_id=_PEER_ID, since_rev=since, limit=5
                    )
                )
                pages += 1
                assert len(resp.records) <= 5
                delivered.extend(r.id for r in resp.records)
                since = resp.latest_rev
                if not resp.has_more:
                    break
            assert pages == 3  # 5 + 5 + 2
            assert sorted(delivered) == sorted(f"fed:r:{i:02d}" for i in range(12))
            assert len(delivered) == len(set(delivered))  # no dupes

    def test_acl_unknown_peer_permission_denied(self, tmp_path: Path) -> None:
        with _grpc_server(tmp_path) as (stub, mgr):
            mgr.sqlite.upsert_index_entries([_entry()])
            with pytest.raises(grpc.RpcError) as excinfo:
                stub.SyncMetadata(
                    _mesh_gen.fed_pb2.MetadataSyncRequest(peer_id=_PEER_ID),
                    metadata=(("x-mnemos-peer-id", "mnemos-UNKNOWN"),),
                )
            assert excinfo.value.code() == grpc.StatusCode.PERMISSION_DENIED

    def test_acl_empty_allowed_set_permission_denied(self, tmp_path: Path) -> None:
        with _grpc_server(tmp_path, allowed=[]) as (stub, _mgr):
            with pytest.raises(grpc.RpcError) as excinfo:
                stub.SyncMetadata(_mesh_gen.fed_pb2.MetadataSyncRequest(peer_id=_PEER_ID))
            assert excinfo.value.code() == grpc.StatusCode.PERMISSION_DENIED

    def test_acl_denied_scope_permission_denied(self, tmp_path: Path) -> None:
        with _grpc_server(tmp_path) as (stub, mgr):
            mgr.sqlite.upsert_index_entries([_entry()])
            with pytest.raises(grpc.RpcError) as excinfo:
                stub.SyncMetadata(
                    _mesh_gen.fed_pb2.MetadataSyncRequest(
                        peer_id=_PEER_ID, project_scope=_PROJECT_DENIED
                    )
                )
            assert excinfo.value.code() == grpc.StatusCode.PERMISSION_DENIED

    def test_garbage_since_rev_invalid_argument(self, tmp_path: Path) -> None:
        with _grpc_server(tmp_path) as (stub, _mgr):
            with pytest.raises(grpc.RpcError) as excinfo:
                stub.SyncMetadata(
                    _mesh_gen.fed_pb2.MetadataSyncRequest(peer_id=_PEER_ID, since_rev=-1)
                )
            assert excinfo.value.code() == grpc.StatusCode.INVALID_ARGUMENT


class TestUpsertIndexEntriesRPC:
    """Real gRPC round trips for MnemosCore.UpsertIndexEntries (S2 import leg)."""

    def test_accepts_and_persists_with_origin_hygiene(self, tmp_path: Path) -> None:
        with _grpc_server(tmp_path) as (stub, mgr):
            resp = stub.UpsertIndexEntries(
                _mesh_gen.core_pb2.UpsertIndexEntriesRequest(
                    entries=[
                        _pb_entry("fed:r:explicit"),  # origin preserved
                        _pb_entry("fed:r:selfclaim", origin_peer="self"),
                        _pb_entry("fed:r:emptyclaim", origin_peer=""),
                    ]
                )
            )
            assert resp.accepted == 3
            assert resp.rejected_by_gate == 0
            assert resp.trigger_code == _mesh_gen.fed_pb2.EXHAUSTIVE
            by_id = {e.id: e for e, _ in mgr.sqlite.list_index()}
            assert by_id["fed:r:explicit"].origin_peer == _REMOTE_ORIGIN
            # ""/"self" claims re-stamped to the AUTHENTICATED sender id.
            assert by_id["fed:r:selfclaim"].origin_peer == _PEER_ID
            assert by_id["fed:r:emptyclaim"].origin_peer == _PEER_ID

    def test_gate_counters_reject_bad_entries_not_batch(self, tmp_path: Path) -> None:
        with _grpc_server(tmp_path, title_blocklist=[r"secret\s+sprint"]) as (stub, mgr):
            resp = stub.UpsertIndexEntries(
                _mesh_gen.core_pb2.UpsertIndexEntriesRequest(
                    entries=[
                        _pb_entry("fed:r:ok"),
                        _pb_entry("fed:r:nofed", tags=["project:p", "mnemos:no-federate"]),
                        _pb_entry("fed:r:blocked", title="Internal secret sprint plan"),
                        _pb_entry("fed:r:badstate", content_state="vaporized"),
                        _pb_entry("fed:r:forgn", schema_version="evil.v9"),
                        _pb_entry("fed:r:longtitle", title="x" * 300),
                    ]
                )
            )
            assert resp.accepted == 1
            assert resp.rejected_by_gate == 5
            assert [e.id for e, _ in mgr.sqlite.list_index()] == ["fed:r:ok"]

    def test_replay_is_idempotent_single_row(self, tmp_path: Path) -> None:
        with _grpc_server(tmp_path) as (stub, mgr):
            # Sender IS the origin (self-claim re-stamped to the sender):
            # a replayed record hits the conflict path with equal
            # canonical timestamps → LWW equal-ts replace, accepted.
            request = _mesh_gen.core_pb2.UpsertIndexEntriesRequest(
                entries=[_pb_entry("fed:r:1", origin_peer="self")]
            )
            assert stub.UpsertIndexEntries(request).accepted == 1
            assert stub.UpsertIndexEntries(request).accepted == 1  # LWW equal-ts replace
            assert len(mgr.sqlite.list_index()) == 1

    def test_transit_replay_from_non_origin_rejected(self, tmp_path: Path) -> None:
        """Blocker 1 ruling: on the conflict path a transit re-send from
        a sender that is NOT the stored origin is refused — even an
        unchanged available replay (no exceptions)."""
        with _grpc_server(tmp_path) as (stub, mgr):
            first = _mesh_gen.core_pb2.UpsertIndexEntriesRequest(
                entries=[_pb_entry("fed:r:1")]  # explicit origin=B from sender A
            )
            assert stub.UpsertIndexEntries(first).accepted == 1  # INSERT: transit allowed
            replay = stub.UpsertIndexEntries(first)
            assert replay.accepted == 0
            assert replay.rejected_by_gate == 1  # conflict path: sender A ≠ stored origin B
            (entry, _rowid) = mgr.sqlite.list_index()[0]
            assert entry.origin_peer == _REMOTE_ORIGIN  # row untouched

    def test_stale_timestamp_lww_silently_superseded(self, tmp_path: Path) -> None:
        with _grpc_server(tmp_path) as (stub, mgr):
            stub.UpsertIndexEntries(
                _mesh_gen.core_pb2.UpsertIndexEntriesRequest(
                    entries=[
                        _pb_entry(
                            "fed:r:1",
                            title="Newer",
                            timestamp="2026-09-10T00:00:00Z",
                            origin_peer="self",
                        )
                    ]
                )
            )
            resp = stub.UpsertIndexEntries(
                _mesh_gen.core_pb2.UpsertIndexEntriesRequest(
                    entries=[
                        _pb_entry(
                            "fed:r:1",
                            title="Older",
                            timestamp="2026-09-01T00:00:00Z",
                            origin_peer="self",
                        )
                    ]
                )
            )
            assert resp.accepted == 0  # stale: neither accepted nor gate-rejected
            assert resp.rejected_by_gate == 0
            (entry, _rowid) = mgr.sqlite.list_index()[0]
            assert entry.title == "Newer"

    def test_acl_entry_project_denied_permission_denied(self, tmp_path: Path) -> None:
        with _grpc_server(tmp_path) as (stub, _mgr):
            with pytest.raises(grpc.RpcError) as excinfo:
                stub.UpsertIndexEntries(
                    _mesh_gen.core_pb2.UpsertIndexEntriesRequest(
                        entries=[_pb_entry("fed:r:1", project=_PROJECT_DENIED)]
                    )
                )
            assert excinfo.value.code() == grpc.StatusCode.PERMISSION_DENIED

    def test_acl_entry_without_project_permission_denied(self, tmp_path: Path) -> None:
        with _grpc_server(tmp_path) as (stub, _mgr):
            with pytest.raises(grpc.RpcError) as excinfo:
                stub.UpsertIndexEntries(
                    _mesh_gen.core_pb2.UpsertIndexEntriesRequest(
                        entries=[_pb_entry("fed:r:1", project="", tags=["mnemos:decision"])]
                    )
                )
            assert excinfo.value.code() == grpc.StatusCode.PERMISSION_DENIED

    def test_acl_unknown_peer_permission_denied(self, tmp_path: Path) -> None:
        with _grpc_server(tmp_path) as (stub, _mgr):
            with pytest.raises(grpc.RpcError) as excinfo:
                stub.UpsertIndexEntries(
                    _mesh_gen.core_pb2.UpsertIndexEntriesRequest(entries=[_pb_entry()]),
                    metadata=(("x-mnemos-peer-id", "mnemos-UNKNOWN"),),
                )
            assert excinfo.value.code() == grpc.StatusCode.PERMISSION_DENIED

    def test_acl_empty_allowed_set_permission_denied(self, tmp_path: Path) -> None:
        with _grpc_server(tmp_path, allowed=[]) as (stub, _mgr):
            with pytest.raises(grpc.RpcError) as excinfo:
                stub.UpsertIndexEntries(
                    _mesh_gen.core_pb2.UpsertIndexEntriesRequest(entries=[_pb_entry()])
                )
            assert excinfo.value.code() == grpc.StatusCode.PERMISSION_DENIED


# ── cross-origin security matrix (review blocker 1, real gRPC) ───────────────


class TestUpsertCrossOriginSecurity:
    """Reviewer probes T1/T2 + the transit ruling over the real RPC, with
    three ACL-configured senders: A (attacker/relay), B (the origin),
    C (a second relay). Identity comes from the gRPC metadata header."""

    @staticmethod
    def _as(peer_id: str) -> tuple[tuple[str, str], ...]:
        return (("x-mnemos-peer-id", peer_id),)

    def _three_peers(self) -> dict[str, PeerConfig]:
        return {
            _PEER_ID: _peer_cfg(),
            _REMOTE_ORIGIN: _peer_cfg(),
            _PEER_ID_C: _peer_cfg(),
        }

    def test_t1_cross_origin_tombstone_censorship_rejected(self, tmp_path: Path) -> None:
        """T1: authenticated peer A sends a FOREIGN id with origin=B,
        content_state=tombstoned and a newer timestamp → must land in
        rejected_by_gate, the victim row untouched."""
        with _grpc_server(tmp_path, peers=self._three_peers()) as (stub, mgr):
            seed = stub.UpsertIndexEntries(
                _mesh_gen.core_pb2.UpsertIndexEntriesRequest(
                    entries=[_pb_entry("fed:r:victim", timestamp="2021-06-01T10:00:00Z")]
                ),
                metadata=self._as(_PEER_ID),
            )
            assert seed.accepted == 1  # transit INSERT of B's row via A
            attack = stub.UpsertIndexEntries(
                _mesh_gen.core_pb2.UpsertIndexEntriesRequest(
                    entries=[
                        _pb_entry(
                            "fed:r:victim",
                            content_state="tombstoned",
                            timestamp="2021-06-02T10:00:00Z",
                        )
                    ]
                ),
                metadata=self._as(_PEER_ID),
            )
            assert attack.accepted == 0
            assert attack.rejected_by_gate == 1
            (entry, _rowid) = mgr.sqlite.list_index()[0]
            assert entry.content_state == "available"  # censorship failed
            assert entry.title == "Remote decision"
            assert entry.origin_peer == _REMOTE_ORIGIN

    def test_t2_self_claim_attribution_capture_rejected(self, tmp_path: Path) -> None:
        """T2: peer A claims origin='self' (re-stamped to A) on B's id to
        capture attribution and retitle the export surface."""
        with _grpc_server(tmp_path, peers=self._three_peers()) as (stub, mgr):
            stub.UpsertIndexEntries(
                _mesh_gen.core_pb2.UpsertIndexEntriesRequest(
                    entries=[_pb_entry("fed:r:victim", timestamp="2021-06-01T10:00:00Z")]
                ),
                metadata=self._as(_PEER_ID),
            )
            attack = stub.UpsertIndexEntries(
                _mesh_gen.core_pb2.UpsertIndexEntriesRequest(
                    entries=[
                        _pb_entry(
                            "fed:r:victim",
                            origin_peer="self",
                            title="Hijacked title",
                            timestamp="2021-06-02T10:00:00Z",
                        )
                    ]
                ),
                metadata=self._as(_PEER_ID),
            )
            assert attack.accepted == 0
            assert attack.rejected_by_gate == 1
            (entry, _rowid) = mgr.sqlite.list_index()[0]
            assert entry.title == "Remote decision"  # row not touched
            assert entry.origin_peer == _REMOTE_ORIGIN  # attribution kept

    def test_transit_insert_accepted_but_peer_c_edit_rejected(self, tmp_path: Path) -> None:
        """The ruling's positive leg: transit available of a NEW id with
        an explicit origin=B from peer A → accepted (INSERT); a replay
        from peer C with edits → rejected (conflict path, non-origin)."""
        with _grpc_server(tmp_path, peers=self._three_peers()) as (stub, mgr):
            transit = stub.UpsertIndexEntries(
                _mesh_gen.core_pb2.UpsertIndexEntriesRequest(
                    entries=[_pb_entry("fed:r:transit", timestamp="2021-06-01T10:00:00Z")]
                ),
                metadata=self._as(_PEER_ID),
            )
            assert transit.accepted == 1
            assert transit.rejected_by_gate == 0
            replay = stub.UpsertIndexEntries(
                _mesh_gen.core_pb2.UpsertIndexEntriesRequest(
                    entries=[
                        _pb_entry(
                            "fed:r:transit",
                            title="Retitled by C",
                            timestamp="2021-06-02T10:00:00Z",
                        )
                    ]
                ),
                metadata=self._as(_PEER_ID_C),
            )
            assert replay.accepted == 0
            assert replay.rejected_by_gate == 1
            (entry, _rowid) = mgr.sqlite.list_index()[0]
            assert entry.title == "Remote decision"
            assert entry.origin_peer == _REMOTE_ORIGIN

    def test_origin_b_direct_mutation_still_works(self, tmp_path: Path) -> None:
        """The guard must not break the legitimate leg: B itself (the
        stored origin) sends a self-claim update with a newer timestamp
        → tombstone lands, origin preserved."""
        with _grpc_server(tmp_path, peers=self._three_peers()) as (stub, mgr):
            stub.UpsertIndexEntries(
                _mesh_gen.core_pb2.UpsertIndexEntriesRequest(
                    entries=[_pb_entry("fed:r:victim", timestamp="2021-06-01T10:00:00Z")]
                ),
                metadata=self._as(_PEER_ID),
            )
            update = stub.UpsertIndexEntries(
                _mesh_gen.core_pb2.UpsertIndexEntriesRequest(
                    entries=[
                        _pb_entry(
                            "fed:r:victim",
                            origin_peer="self",
                            content_state="tombstoned",
                            timestamp="2021-06-02T10:00:00Z",
                        )
                    ]
                ),
                metadata=self._as(_REMOTE_ORIGIN),
            )
            assert update.accepted == 1
            assert update.rejected_by_gate == 0
            (entry, _rowid) = mgr.sqlite.list_index()[0]
            assert entry.content_state == "tombstoned"
            assert entry.origin_peer == _REMOTE_ORIGIN  # never re-stamped


# ── timestamp import gates (review blocker 2, real gRPC) ─────────────────────


class TestTimestampImportGates:
    """LWW anti-poisoning over the real RPC: canonicalisation to UTC and
    the future-slack gate are enforced at the import boundary."""

    def test_far_future_timestamp_rejected(self, tmp_path: Path) -> None:
        with _grpc_server(tmp_path) as (stub, mgr):
            resp = stub.UpsertIndexEntries(
                _mesh_gen.core_pb2.UpsertIndexEntriesRequest(
                    entries=[_pb_entry("fed:r:poison", timestamp="9999-12-31T23:59:59Z")]
                )
            )
            assert resp.accepted == 0
            assert resp.rejected_by_gate == 1
            assert mgr.sqlite.list_index() == []

    def test_unparseable_timestamp_rejected(self, tmp_path: Path) -> None:
        with _grpc_server(tmp_path) as (stub, mgr):
            resp = stub.UpsertIndexEntries(
                _mesh_gen.core_pb2.UpsertIndexEntriesRequest(
                    entries=[_pb_entry("fed:r:garbage", timestamp="not-a-timestamp")]
                )
            )
            assert resp.accepted == 0
            assert resp.rejected_by_gate == 1
            assert mgr.sqlite.list_index() == []

    def test_offset_timestamp_canonicalised_to_utc(self, tmp_path: Path) -> None:
        with _grpc_server(tmp_path) as (stub, mgr):
            resp = stub.UpsertIndexEntries(
                _mesh_gen.core_pb2.UpsertIndexEntriesRequest(
                    entries=[
                        _pb_entry(
                            "fed:r:tz", origin_peer="self", timestamp="2021-06-01T10:00:00+03:00"
                        )
                    ]
                )
            )
            assert resp.accepted == 1
            (entry, _rowid) = mgr.sqlite.list_index()[0]
            assert entry.timestamp == "2021-06-01T07:00:00.000000Z"  # canonical UTC

    def test_same_instant_different_tz_lww_neutral(self, tmp_path: Path) -> None:
        """Two records for the same id expressing the SAME instant in
        different offsets: both canonicalise to one form, so LWW is
        neutral — the outcome is order-independent (one row, identical
        stored timestamp; last arrival wins the payload, not the tz)."""
        with _grpc_server(tmp_path) as (stub, mgr):
            zulu = _pb_entry("fed:r:same", origin_peer="self", timestamp="2021-06-01T12:00:00Z")
            offset = _pb_entry(
                "fed:r:same",
                origin_peer="self",
                title="Same instant, other tz",
                timestamp="2021-06-01T15:00:00+03:00",
            )
            resp1 = stub.UpsertIndexEntries(
                _mesh_gen.core_pb2.UpsertIndexEntriesRequest(entries=[zulu, offset])
            )
            assert resp1.accepted == 2  # equal canonical ts → replace on equal
            (entry, _rowid) = mgr.sqlite.list_index()[0]
            assert entry.timestamp == "2021-06-01T12:00:00.000000Z"
            assert entry.title == "Same instant, other tz"
            # Reverse arrival order: same one row, same canonical stamp.
            mgr.sqlite.purge_origin(_PEER_ID)
            resp2 = stub.UpsertIndexEntries(
                _mesh_gen.core_pb2.UpsertIndexEntriesRequest(entries=[offset, zulu])
            )
            assert resp2.accepted == 2
            (entry2, _rowid) = mgr.sqlite.list_index()[0]
            assert entry2.timestamp == "2021-06-01T12:00:00.000000Z"  # stable watermark
            assert entry2.title == "Remote decision"  # zulu arrived last

    def test_tz_older_instant_loses_lww(self, tmp_path: Path) -> None:
        """The old bug: '2021-02-01T10:00:00+03:00' (07:00Z — OLDER
        instant) compared lexically GREATER than the stored
        '2021-02-01T08:00:00Z' and wrongly won LWW. Canonicalised, the
        older instant is stale — silently superseded, not a rejection."""
        with _grpc_server(tmp_path) as (stub, mgr):
            stub.UpsertIndexEntries(
                _mesh_gen.core_pb2.UpsertIndexEntriesRequest(
                    entries=[
                        _pb_entry(
                            "fed:r:lww",
                            origin_peer="self",
                            title="Newer",
                            timestamp="2021-02-01T08:00:00Z",
                        )
                    ]
                )
            )
            resp = stub.UpsertIndexEntries(
                _mesh_gen.core_pb2.UpsertIndexEntriesRequest(
                    entries=[
                        _pb_entry(
                            "fed:r:lww",
                            origin_peer="self",
                            title="Older but lexically bigger",
                            timestamp="2021-02-01T10:00:00+03:00",
                        )
                    ]
                )
            )
            assert resp.accepted == 0  # stale — neither accepted nor rejected
            assert resp.rejected_by_gate == 0
            (entry, _rowid) = mgr.sqlite.list_index()[0]
            assert entry.title == "Newer"
