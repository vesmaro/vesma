"""Store-level unit tests for the #359 idempotent mesh import.

Covers :meth:`vesma.storage.sqlite_store.SQLiteStore.find_federated_duplicate`
and :meth:`vesma.storage.sqlite_store.SQLiteStore.touch_last_fed_at`
directly against a tmp SQLite DB — no manager, no gRPC. The gRPC-level
behaviour (trigger codes, response ids) lives in
``tests/test_mesh_server.py::TestWriteMemoryIdempotency``.

Match-key contract under test (vesmaro #359):

* ``fed_id`` (metadata key minted by the mesh import path) — priority.
* ``title`` + ``source_agent`` (stored as ``title`` +
  ``metadata.fed_source_agent``) — fallback, both sides non-empty.
* Records without provenance (no fed_id AND no source_agent) never match.
* Plain API records (no federation metadata) never satisfy the fallback.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from vesma.models import Memory, MemorySource, MemoryStatus, MemoryType
from vesma.storage.sqlite_store import SQLiteStore


def _make_memory(
    mid: str,
    *,
    title: str = "test",
    metadata: dict[str, str] | None = None,
    agent: str = "fed-agent",
    created_at: datetime | None = None,
) -> Memory:
    """Build a minimal persisted-shape Memory row."""
    now = created_at or datetime.now(UTC)
    return Memory(
        id=mid,
        content="hello world",
        title=title,
        tags=["project:p359", "agent:fed-agent", "mnemos:decision"],
        source=MemorySource.MCP,
        source_url=None,
        memory_type=MemoryType.NOTE,
        created_at=now,
        updated_at=now,
        metadata=metadata or {},
        file_path=None,
        category=None,
        project="p359",
        agent=agent,
        status=MemoryStatus.RAW,
        quality_score=None,
        confidence=None,
        source_coverage=None,
        cluster_id=None,
        derived_from=[],
        embedding_id=None,
        raw_content=None,
        clean_content=None,
        filter_profile=None,
        filter_stats=None,
        filter_version=None,
    )


def _store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "fed_dedup.db")


class TestFindFederatedDuplicate:
    def test_fed_id_match_returns_row(self, tmp_path: Path) -> None:
        """A stored metadata.fed_id equal to the lookup key matches."""
        store = _store(tmp_path)
        store.save(
            _make_memory(
                "m-1", metadata={"fed_id": "fed:agent-a:uuid-1", "fed_source_agent": "agent-a"}
            )
        )
        hit = store.find_federated_duplicate(
            fed_id="fed:agent-a:uuid-1", title="other title", source_agent="agent-b"
        )
        assert hit is not None and hit.id == "m-1"

    def test_fed_id_takes_priority_over_fallback(self, tmp_path: Path) -> None:
        """When fed_id is given, only the fed_id branch runs — a row that
        would satisfy the title+source_agent fallback is NOT returned."""
        store = _store(tmp_path)
        store.save(
            _make_memory(
                "m-fallback", title="Shared title", metadata={"fed_source_agent": "agent-a"}
            )
        )
        store.save(
            _make_memory(
                "m-exact", title="Different title", metadata={"fed_id": "fed:agent-a:uuid-9"}
            )
        )
        hit = store.find_federated_duplicate(
            fed_id="fed:agent-a:uuid-9", title="Shared title", source_agent="agent-a"
        )
        assert hit is not None and hit.id == "m-exact"

    def test_fallback_title_and_source_agent(self, tmp_path: Path) -> None:
        """Without a fed_id, title + metadata.fed_source_agent matches."""
        store = _store(tmp_path)
        store.save(
            _make_memory(
                "m-2",
                title="Peer decision",
                metadata={"fed_id": "", "fed_source_agent": "agent-a"},
            )
        )
        hit = store.find_federated_duplicate(
            fed_id="", title="Peer decision", source_agent="agent-a"
        )
        assert hit is not None and hit.id == "m-2"

    def test_fallback_requires_both_sides_nonempty(self, tmp_path: Path) -> None:
        """Empty source_agent on the incoming side → no match attempt."""
        store = _store(tmp_path)
        store.save(
            _make_memory("m-3", title="Peer decision", metadata={"fed_source_agent": "agent-a"})
        )
        assert (
            store.find_federated_duplicate(fed_id="", title="Peer decision", source_agent="")
            is None
        )
        assert store.find_federated_duplicate(fed_id="", title="", source_agent="agent-a") is None

    def test_no_provenance_never_queries(self, tmp_path: Path) -> None:
        """No fed_id AND no source_agent → None even when a row exists."""
        store = _store(tmp_path)
        store.save(_make_memory("m-4", title="Anything", metadata={"fed_source_agent": "agent-a"}))
        assert store.find_federated_duplicate(fed_id="", title="Anything", source_agent="") is None

    def test_plain_api_record_never_matches_fallback(self, tmp_path: Path) -> None:
        """A row without federation metadata (a regular API record with the
        same title and agent column) must NOT satisfy the fallback — the
        #359 "не трогаем обычные API-записи" invariant on the stored side."""
        store = _store(tmp_path)
        store.save(_make_memory("m-api", title="Local note", metadata={}))
        assert (
            store.find_federated_duplicate(fed_id="", title="Local note", source_agent="fed-agent")
            is None
        )

    def test_earliest_match_wins(self, tmp_path: Path) -> None:
        """Duplicate fed_id rows → the earliest created_at is returned, so
        the storage id stays stable across replays."""
        store = _store(tmp_path)
        older = datetime.now(UTC) - timedelta(minutes=5)
        store.save(
            _make_memory(
                "m-newer", metadata={"fed_id": "fed:agent-a:dup"}, created_at=datetime.now(UTC)
            )
        )
        store.save(
            _make_memory("m-older", metadata={"fed_id": "fed:agent-a:dup"}, created_at=older)
        )
        hit = store.find_federated_duplicate(fed_id="fed:agent-a:dup", title="", source_agent="")
        assert hit is not None and hit.id == "m-older"


class TestTouchLastFedAt:
    def test_refreshes_existing_key_only(self, tmp_path: Path) -> None:
        """The key is refreshed in place; other metadata keys survive."""
        store = _store(tmp_path)
        store.save(
            _make_memory(
                "m-5",
                metadata={
                    "fed_id": "fed:agent-a:uuid-5",
                    "fed_source_agent": "agent-a",
                    "last_fed_at": "2026-01-01T00:00:00+00:00",
                },
            )
        )
        assert store.touch_last_fed_at("m-5") is True
        row = store.get("m-5")
        assert row is not None
        assert row.metadata["last_fed_at"] != "2026-01-01T00:00:00+00:00"
        assert row.metadata["fed_id"] == "fed:agent-a:uuid-5"
        assert row.metadata["fed_source_agent"] == "agent-a"
        # Content/title untouched — a duplicate is reported, never rewritten.
        assert row.content == "hello world"
        assert row.title == "test"

    def test_missing_key_is_untouched(self, tmp_path: Path) -> None:
        """A row without last_fed_at stays byte-identical; no key invented."""
        store = _store(tmp_path)
        store.save(_make_memory("m-6", metadata={"fed_id": "fed:agent-a:uuid-6"}))
        assert store.touch_last_fed_at("m-6") is False
        row = store.get("m-6")
        assert row is not None
        assert "last_fed_at" not in row.metadata
        assert row.metadata == {"fed_id": "fed:agent-a:uuid-6"}

    def test_unknown_id_is_noop(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.touch_last_fed_at("no-such-id") is False
