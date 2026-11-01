"""The consolidation executor (``vesma.consolidation``) — conformance.

The fold semantics of the W1-W3 wave, pinned: the Д6 append-only
carry-over order onto the keeper, the merged-by-arbiter marks on BOTH
sides, the Д3 ``relates_to`` link with provenance ``merge-arbiter``,
the tombstone through the store's OWN Stage-1 path (terminal state +
hash-chained journal + descendant quarantine), idempotent re-folds, the
honest pre-canon skip («provenance или не было»), and the observation
mints (W3 sighting, W2 split-from) whose shapes pass the Д2 validator.
"""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from vesma.canon_validate import validate_canon_record
from vesma.config import Settings
from vesma.consolidation import (
    MERGE_ARBITER_EDGE_WEIGHT,
    MERGE_ARBITER_PROVENANCE,
    ConsolidationError,
    ConsolidationExecutor,
    lineage_mark,
    mint_lineage_marks,
)
from vesma.manager import MemoryManager
from vesma.models import MemoryCreate, MemoryUpdate
from vesma.storage.sqlite_store import tombstones_visible


@pytest.fixture
def mgr() -> Iterator[MemoryManager]:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        settings = Settings(
            vesma={
                "vault_path": str(tmp / "vault"),
                "data_dir": str(tmp / "data"),
                "db_name": "test.db",
            },
            embedding={"provider": "onnx"},
            scanner={"enabled": False},
        )
        settings.resolve_paths()
        manager = MemoryManager(settings)
        yield manager
        manager.close()


def _row(mgr: MemoryManager, content: str, *, canon: bool = False):
    """One store row; ``canon=True`` mints a REAL server checkpoint
    envelope through the trusted minter (a client-supplied canon on the
    generic create path is stripped — the engine's own Д5 discipline,
    which this test suite must not bypass)."""
    if canon:
        session = "sess-" + hashlib.sha1(content.encode("utf-8")).hexdigest()[:10]
        memory, _dedup = mgr.save_checkpoint(
            {
                "goals": f"goal for {content}",
                "completed": f"done for {content}",
                "in_progress": f"wip for {content}",
                "decisions": f"decision for {content}",
                "context": content,
            },
            project="consol",
            agent="wave",
            session=session,
        )
        return memory
    return mgr.add(
        MemoryCreate(
            content=content,
            title=content.split()[0],
            tags=["project:consol"],
        ),
        project="consol",
        agent="wave",
    )


def _stored_canon(mgr: MemoryManager, memory_id: str) -> dict:
    from vesma.storage.sqlite_store import tombstones_visible as _tv

    with _tv():
        row = mgr.sqlite.get(memory_id, include_tombstoned=True)
    assert row is not None
    canon = row.metadata.get("canon")
    assert isinstance(canon, dict)
    return canon


def _marks(mgr: MemoryManager, memory_id: str) -> list[dict]:
    canon = _stored_canon(mgr, memory_id)
    return list(canon.get("lineage_marks") or [])


# ---------------------------------------------------------------------------
# 1. The fold — the system action
# ---------------------------------------------------------------------------


def test_fold_tombstones_folded_and_mints_both_marks_and_edge(
    mgr: MemoryManager,
) -> None:
    keeper = _row(mgr, "keeper record alpha", canon=True)
    folded = _row(mgr, "folded record beta", canon=True)
    executor = ConsolidationExecutor(mgr)
    report = executor.fold(keeper.id, [folded.id])

    row = report["folded"][0]
    assert row["status"] == "folded"
    assert row["journal_id"] and row["entry_hash"]
    # the store's OWN terminal state (the workflow machine, not a raw write)
    status = mgr.sqlite.get_workflow_status(folded.id)
    assert status is not None and status["workflow_status"] == "tombstoned"
    assert mgr.sqlite.journal_entry_count(folded.id) == 1
    # the Д3 durable link
    edges = mgr.sqlite.get_incoming_edges(folded.id, kind="relates_to")
    fold_edges = [
        e
        for e in edges
        if e["from_memory_id"] == keeper.id and e["provenance"] == MERGE_ARBITER_PROVENANCE
    ]
    assert len(fold_edges) == 1
    assert fold_edges[0]["kind"] == "relates_to"
    assert float(fold_edges[0]["weight"]) == MERGE_ARBITER_EDGE_WEIGHT
    # marks on BOTH sides
    keeper_marks = _marks(mgr, keeper.id)
    assert len(keeper_marks) == 1
    assert keeper_marks[0]["kind"] == "merged-by-arbiter"
    assert keeper_marks[0]["ref"] == folded.id
    folded_marks = _marks(mgr, folded.id)
    assert len(folded_marks) == 1
    assert folded_marks[0]["ref"] == keeper.id
    # the keeper is alive
    assert mgr.get(keeper.id) is not None


def test_fold_carries_over_folded_marks_in_d6_order(mgr: MemoryManager) -> None:
    """Д6: the accepting row's array GROWS by (a) the folded twin's own
    marks, then (b) the merged-by-arbiter entry — append-only order."""
    keeper = _row(mgr, "keeper gamma", canon=True)
    folded = _row(mgr, "folded delta", canon=True)
    executor = ConsolidationExecutor(mgr)
    # the folded row already carries a W3 sighting mark (server-minted
    # earlier in its life)
    executor.mint_sighting(folded.id, keeper.id)
    report = executor.fold(keeper.id, [folded.id])
    assert report["keeper_marks_appended"] == 2
    marks = _marks(mgr, keeper.id)
    kinds = [m["kind"] for m in marks]
    assert kinds == ["same-message-other-envelope", "merged-by-arbiter"]
    assert marks[0]["ref"] == keeper.id  # carried from the folded row
    assert marks[1]["ref"] == folded.id  # the fold entry, LAST


def test_refold_is_idempotent(mgr: MemoryManager) -> None:
    keeper = _row(mgr, "keeper epsilon", canon=True)
    folded = _row(mgr, "folded zeta", canon=True)
    executor = ConsolidationExecutor(mgr)
    first = executor.fold(keeper.id, [folded.id])
    assert first["folded"][0]["status"] == "folded"
    second = executor.fold(keeper.id, [folded.id])
    assert second["folded"][0]["status"] == "skipped-tombstoned"
    assert _marks(mgr, keeper.id) == _marks(mgr, keeper.id)
    assert len(_marks(mgr, keeper.id)) == 1  # no duplicate appended
    assert mgr.sqlite.journal_entry_count(folded.id) == 1


def test_fold_of_missing_id_reports_skip(mgr: MemoryManager) -> None:
    keeper = _row(mgr, "keeper eta", canon=True)
    executor = ConsolidationExecutor(mgr)
    report = executor.fold(keeper.id, ["00000000-missing-id-0000-000000000000"])
    assert report["folded"][0]["status"] == "skipped-missing"


def test_pre_canon_keeper_folds_without_marks_honestly(
    mgr: MemoryManager,
) -> None:
    """A pre-canon keeper cannot carry marks (canon §9, ADR-0037 Д1):
    the fold still happens (edge + tombstone + journal), and the report
    says the marks leg did NOT («provenance или не было»)."""
    keeper = _row(mgr, "keeper theta")  # no canon envelope
    folded = _row(mgr, "folded iota", canon=True)
    executor = ConsolidationExecutor(mgr)
    report = executor.fold(keeper.id, [folded.id])
    assert report["folded"][0]["status"] == "folded"
    assert report["keeper_marks_skipped_no_canon"] == 1
    assert report["keeper_marks_appended"] == 0
    # the folded row still carries ITS grave mark (it has a canon locus)
    assert _marks(mgr, folded.id)[0]["kind"] == "merged-by-arbiter"


def test_fold_refuses_tombstoned_keeper_and_self_fold(mgr: MemoryManager) -> None:
    keeper = _row(mgr, "keeper kappa", canon=True)
    folded = _row(mgr, "folded lambda", canon=True)
    executor = ConsolidationExecutor(mgr)
    executor.fold(keeper.id, [folded.id])
    with pytest.raises(ConsolidationError, match="already tombstoned"):
        executor.fold(folded.id, [keeper.id])
    with pytest.raises(ConsolidationError, match="same record"):
        executor.fold(keeper.id, [keeper.id])


def test_fold_quarantines_descendants_through_the_store_semantics(
    mgr: MemoryManager,
) -> None:
    """The tombstone rides the store's own Stage-1 delete: derived_from
    children quarantine (lineage-broken), the report carries the fallout."""
    parent = _row(mgr, "parent mu record", canon=True)
    child = _row(mgr, "child nu record", canon=True)
    mgr.sqlite.update_fields(child.id, derived_from=json_dumps([parent.id]))
    keeper = _row(mgr, "keeper xi record", canon=True)
    executor = ConsolidationExecutor(mgr)
    report = executor.fold(keeper.id, [parent.id])
    row = report["folded"][0]
    assert row["status"] == "folded"
    assert row["descendants_quarantined"] == [child.id]


def json_dumps(value: list[str]) -> str:
    import json

    return json.dumps(value)


# ---------------------------------------------------------------------------
# 2. Observation mints (W3 sighting, W2 split-from)
# ---------------------------------------------------------------------------


def test_sighting_mark_mints_and_passes_the_d2_validator(mgr: MemoryManager) -> None:
    original = _row(mgr, "original omicron", canon=True)
    sighting = _row(mgr, "sighting pi record", canon=True)
    executor = ConsolidationExecutor(mgr)
    assert executor.mint_sighting(sighting.id, original.id) == 1
    marks = _marks(mgr, sighting.id)
    assert len(marks) == 1
    assert marks[0]["kind"] == "same-message-other-envelope"
    assert marks[0]["ref"] == original.id
    # the whole envelope passes the engine's Д2 pass — no CANON-E-LINEAGE
    with tombstones_visible():
        row = mgr.sqlite.get(sighting.id, include_tombstoned=True)
    assert row is not None
    violations = validate_canon_record(
        content=row.effective_content(), title=row.title, metadata=row.metadata
    )
    assert not [v for v in violations if v.code == "CANON-E-LINEAGE"]
    # idempotent: a re-sighting appends nothing
    assert executor.mint_sighting(sighting.id, original.id) == 0


def test_split_mark_mints_without_a_production_trigger(mgr: MemoryManager) -> None:
    parent = _row(mgr, "parent rho", canon=True)
    child = _row(mgr, "child sigma record", canon=True)
    executor = ConsolidationExecutor(mgr)
    assert executor.mint_split(child.id, parent.id) == 1
    marks = _marks(mgr, child.id)
    assert marks[0]["kind"] == "split-from"
    assert marks[0]["ref"] == parent.id


def test_mint_on_pre_canon_row_is_an_honest_zero(mgr: MemoryManager) -> None:
    plain = _row(mgr, "plain tau row")  # no canon envelope
    other = _row(mgr, "other upsilon", canon=True)
    executor = ConsolidationExecutor(mgr)
    assert executor.mint_sighting(plain.id, other.id) == 0
    from vesma.storage.sqlite_store import tombstones_visible as _tv

    with _tv():
        row = mgr.sqlite.get(plain.id, include_tombstoned=True)
    assert row is not None
    assert "canon" not in row.metadata  # no envelope invented


def test_mint_refuses_unknown_record(mgr: MemoryManager) -> None:
    executor = ConsolidationExecutor(mgr)
    with pytest.raises(ConsolidationError, match="not found"):
        executor.mint_sighting("00000000-missing-id-0000-000000000000", "x")


# ---------------------------------------------------------------------------
# 3. Pure mint mechanics
# ---------------------------------------------------------------------------


def test_lineage_mark_validates_kind_and_ref() -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    mark = lineage_mark("merged-by-arbiter", now, "target-id")
    assert set(mark) == {"kind", "at", "ref"}  # additionalProperties false
    with pytest.raises(ConsolidationError, match="outside the ratified enum"):
        lineage_mark("folded-by", now, "x")
    with pytest.raises(ConsolidationError, match="empty ref"):
        lineage_mark("split-from", now, "")


def test_mint_lineage_marks_is_pure_and_dedups() -> None:
    metadata = {
        "canon": {"type": "task", "lineage_marks": [{"kind": "split-from", "at": "t0", "ref": "p"}]}
    }
    new_mark = {"kind": "merged-by-arbiter", "at": "t1", "ref": "f"}
    new_metadata, count = mint_lineage_marks(metadata, [new_mark, new_mark])
    assert count == 1
    marks = new_metadata["canon"]["lineage_marks"]
    assert len(marks) == 2
    assert marks[0]["ref"] == "p" and marks[1]["ref"] == "f"
    # the input was never mutated
    assert len(metadata["canon"]["lineage_marks"]) == 1
    # no canon locus → honest zero
    assert mint_lineage_marks({}, [new_mark]) == ({}, 0)
    # malformed existing array is tolerated, never trusted
    broken = {"canon": {"type": "task", "lineage_marks": "garbage"}}
    _, count = mint_lineage_marks(broken, [new_mark])
    assert count == 1


# ---------------------------------------------------------------------------
# 4. Д5 merge-back — server-minted marks survive a client update
# ---------------------------------------------------------------------------


def _client_task_envelope() -> dict:
    return {
        "schema_version": "1.0",
        "type": "task",
        "status": "open",
        "language": "en",
        "owner_slug": "wave",
    }


def test_client_update_preserves_server_minted_marks(mgr: MemoryManager) -> None:
    """A client metadata= replacement on a client-authored envelope must
    not wipe the row's fold history: the stored marks merge back UNDER
    the incoming canon, and a forged client array never resurrects."""
    row = _row(mgr, "merge-back row one", canon=True)
    executor = ConsolidationExecutor(mgr)
    folded = _row(mgr, "merge-back row two", canon=True)
    executor.fold(row.id, [folded.id])
    assert len(_marks(mgr, row.id)) == 1
    # a client update carrying BOTH a fresh envelope and a forged mark:
    # the envelope stands, the forged mark is stripped, the MINTED
    # history survives
    forged_canon = dict(_client_task_envelope())
    forged_canon["lineage_marks"] = [
        {"kind": "merged-by-arbiter", "at": "2026-01-01T00:00:00Z", "ref": "forged"}
    ]
    mgr.update(row.id, MemoryUpdate(metadata={"canon": forged_canon, "content_type": "prose"}))
    marks = _marks(mgr, row.id)
    assert len(marks) == 1
    assert marks[0]["ref"] == folded.id
    assert marks[0]["at"] != "2026-01-01T00:00:00Z"  # the stored entry, not the forged one


def test_client_update_with_a_plain_envelope_preserves_marks(
    mgr: MemoryManager,
) -> None:
    row = _row(mgr, "plain merge-back row", canon=True)
    executor = ConsolidationExecutor(mgr)
    folded = _row(mgr, "plain merge-back twin", canon=True)
    executor.fold(row.id, [folded.id])
    mgr.update(row.id, MemoryUpdate(metadata={"canon": dict(_client_task_envelope())}))
    assert len(_marks(mgr, row.id)) == 1
