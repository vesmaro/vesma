"""Consolidation — server-minted lineage marks and the fold executor
(W1-W3, ADR-0037 wiring).

Policy §8.3 verdict/action split: an arbiter VERDICT is data, never a
system action. This module owns the ACTION side: minting the ratified
``lineage_marks`` kinds through the server-trusted path and executing a
fold decision against the store's own semantics. Every mark minted here
is server-minted or arbitration-minted — the SAME trust class as the
checkpoint stamps (ADR-0037 Д5: a client-supplied array is stripped on
the client-facing paths; only the surfaces in this module mint).

Storage locus (ADR-0037 Д1): marks live inside
``metadata.canon["lineage_marks"]`` of an engine ``Memory`` row. A row
WITHOUT a canon envelope is outside canon scope (canon §9 transitional
rule) and cannot carry marks — the executor SKIPS mark minting for such
rows and says so in the report («provenance или не было»): the durable
fold link is the ``relates_to`` edge with provenance
``merge-arbiter`` (Д3, OQ-2 minimum), which works for every row.

Fold semantics (package §3.2 + ADR-0037 D6):
  * the KEEP record stays alive;
  * every folded record's own marks are carried over onto the keeper
    FIRST (append-only, marks never deleted), then the keeper gains
    ``{kind: merged-by-arbiter, at: <fold ts>, ref: <folded id>}``;
  * the folded row itself gains the same ``merged-by-arbiter`` mark
    (ref → keeper) BEFORE its tombstone, so the mark rides the
    tombstoned row for the admin surfaces;
  * the tombstone goes through ``MemoryManager.delete_memory`` — the
    store's own Stage-1 semantics (terminal state, hash-chained journal,
    descendant quarantine), never a raw row write.

Advisory-first: nothing in this module runs from a verdict by itself.
The wiring (:mod:`vesma.manager`) calls the executor only when the
``merge_arbiter`` setting is ``apply``; ``advisory`` (the default) logs
and mints OBSERVATION marks only, never folds.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from vesma.canon_validate import LINEAGE_MARK_KINDS

logger = logging.getLogger(__name__)

#: Д3/OQ-2 minimum: the fold's durable link is a ``relates_to`` edge with
#: THIS provenance (a dedicated edge kind stays an open question for a
#: future wave — the weaker honest claim holds meanwhile).
MERGE_ARBITER_PROVENANCE: Final[str] = "merge-arbiter"

#: Fold edge weight — the raised-weight precedent of minted fuel
#: (graph_minting.AUTO_DEDUPE_EDGE_WEIGHT); the walk treats the fold link
#: as first-class affinity.
MERGE_ARBITER_EDGE_WEIGHT: Final[float] = 2.0

#: The actor identity recorded in the deletion journal (weak Phase-1
#: identity; the value must make the journal line self-describing).
ARBITER_ACTOR: Final[str] = "merge-arbiter"

#: The metadata.canon key carrying the marks array.
_LINEAGE_KEY: Final[str] = "lineage_marks"


class ConsolidationError(ValueError):
    """A fold request the executor refuses (structural, logged loud).

    Not a store failure: the store keeps running; THIS fold did not
    happen and the caller must not retry blindly."""


def lineage_mark(kind: str, at: datetime, ref: str) -> dict[str, str]:
    """One schema-valid mark (lineage_marks.schema.json per-item shape:
    ``kind`` in the ratified enum, ``at`` ISO-8601 with the mandatory
    time part, optional non-empty ``ref``; additionalProperties false)."""
    if kind not in LINEAGE_MARK_KINDS:
        raise ConsolidationError(
            f"mark kind {kind!r} is outside the ratified enum {sorted(LINEAGE_MARK_KINDS)}"
        )
    if not ref:
        raise ConsolidationError(f"mark kind {kind!r} carries an empty ref")
    return {"kind": kind, "at": at.isoformat(), "ref": ref}


def mint_lineage_marks(
    metadata: dict[str, Any], marks: Iterable[dict[str, str]]
) -> tuple[dict[str, Any], int]:
    """Pure: append ``marks`` to ``metadata.canon["lineage_marks"]``.

    Dedup guard for idempotent re-execution: a mark whose
    ``(kind, ref)`` pair is already present is skipped (the ``at`` stamp
    differs between runs on purpose — append-only history, never
    rewritten). Rows without a canon envelope return
    ``(metadata, 0)``: pre-canon rows cannot carry marks (canon §9,
    ADR-0037 Д1) — the caller reports the skip honestly.

    Returns ``(new_metadata_dict, appended_count)``; the input dict is
    never mutated.
    """
    canon = metadata.get("canon") if isinstance(metadata, dict) else None
    if not isinstance(canon, dict):
        return metadata, 0
    existing = canon.get(_LINEAGE_KEY)
    existing = list(existing) if isinstance(existing, list) else []
    seen = {(str(m.get("kind")), str(m.get("ref"))) for m in existing if isinstance(m, dict)}
    appended: list[dict[str, str]] = []
    for mark in marks:
        key = (str(mark.get("kind")), str(mark.get("ref")))
        if key in seen:
            continue
        appended.append(mark)
        seen.add(key)
    if not appended:
        return metadata, 0
    new_canon = dict(canon)
    new_canon[_LINEAGE_KEY] = existing + appended
    new_metadata = dict(metadata)
    new_metadata["canon"] = new_canon
    return new_metadata, len(appended)


def _write_metadata(store: Any, memory_id: str, metadata: dict[str, Any]) -> bool:
    """Persist a whole metadata dict through the store's whitelist
    updater (the trusted path — the same writer the checkpoint minter
    uses; the client strips never see this call)."""
    return bool(store.update_fields(memory_id, metadata=json.dumps(metadata, ensure_ascii=False)))


class ConsolidationExecutor:
    """The fold executor bound to a manager (store access through the
    manager's own surfaces — never a raw SQL side door)."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager

    # ── observation mints (safe in advisory mode) ─────────────────────────
    def mint_sighting(
        self, sighting_id: str, original_id: str, *, at: datetime | None = None
    ) -> int:
        """W3 — mint ``same-message-other-envelope`` on the SIGHTING row
        (ref → the already-present record). Observation-only: no fold,
        no status change. Returns the number of marks appended (0 when
        the row is pre-canon or the mark is already present)."""
        return self._append_mark(
            sighting_id,
            lineage_mark("same-message-other-envelope", at or datetime.now(UTC), original_id),
        )

    def mint_split(self, child_id: str, parent_id: str, *, at: datetime | None = None) -> int:
        """W2 — mint ``split-from`` on the CHILD row (ref → parent). The
        engine has no split operation today (ADR-0037: none in the
        store); this mint exists for the future split op and is exercised
        by tests — there is NO production trigger in this wave."""
        return self._append_mark(
            child_id, lineage_mark("split-from", at or datetime.now(UTC), parent_id)
        )

    def _append_mark(self, memory_id: str, mark: dict[str, str]) -> int:
        from vesma.storage.sqlite_store import tombstones_visible

        with tombstones_visible():
            memory = self._manager.sqlite.get(memory_id, include_tombstoned=True)
        if memory is None:
            raise ConsolidationError(f"memory {memory_id!r} not found")
        new_metadata, appended = mint_lineage_marks(dict(memory.metadata), [mark])
        if appended:
            _write_metadata(self._manager.sqlite, memory_id, new_metadata)
            logger.info(
                "lineage mark minted: id=%s kind=%s ref=%s",
                memory_id[:8],
                mark["kind"],
                mark["ref"][:8],
            )
        return appended

    # ── the fold (system action — apply mode ONLY) ────────────────────────
    def fold(
        self,
        keeper_id: str,
        folded_ids: Sequence[str],
        *,
        at: datetime | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """Execute a keep decision: the keeper stays; every folded record
        gets the marks carry-over (Д6), the ``merged-by-arbiter`` mark on
        both sides, the Д3 ``relates_to`` edge (provenance
        ``merge-arbiter``) and a tombstone through the store's own
        delete path. Idempotent: an already-tombstoned folded id is a
        reported skip, a re-fold appends nothing.

        NEVER called with an unanswered verdict — the wiring owns the
        verdict→action gate (policy §8.3); this method is the action
        itself.
        """
        from vesma.storage.sqlite_store import tombstones_visible

        fold_ts = at or datetime.now(UTC)
        with tombstones_visible():
            keeper = self._manager.sqlite.get(keeper_id, include_tombstoned=True)
        if keeper is None:
            raise ConsolidationError(f"keeper {keeper_id!r} not found")
        status = self._manager.sqlite.get_workflow_status(keeper_id)
        if status and status.get("workflow_status") == "tombstoned":
            raise ConsolidationError(
                f"keeper {keeper_id!r} is already tombstoned — folding into a grave"
            )

        report: dict[str, Any] = {
            "keeper_id": keeper_id,
            "folded": [],
            "at": fold_ts.isoformat(),
            "reason": reason,
        }
        keeper_marks_appended = 0
        keeper_marks_skipped_no_canon = 0

        for folded_id in folded_ids:
            if folded_id == keeper_id:
                raise ConsolidationError("keeper and folded id are the same record")
            with tombstones_visible():
                folded = self._manager.sqlite.get(folded_id, include_tombstoned=True)
            if folded is None:
                report["folded"].append({"memory_id": folded_id, "status": "skipped-missing"})
                continue
            folded_status = self._manager.sqlite.get_workflow_status(folded_id)
            if folded_status and folded_status.get("workflow_status") == "tombstoned":
                report["folded"].append({"memory_id": folded_id, "status": "skipped-tombstoned"})
                continue

            # Д6 carry-over onto the keeper: the folded row's own marks
            # first, then the merged-by-arbiter entry — append-only.
            folded_canon = folded.metadata.get("canon")
            folded_marks = []
            if isinstance(folded_canon, dict) and isinstance(folded_canon.get(_LINEAGE_KEY), list):
                folded_marks = list(folded_canon.get(_LINEAGE_KEY))
            keeper_canon = keeper.metadata.get("canon")
            carry = [m for m in folded_marks if isinstance(m, dict)]
            carry.append(lineage_mark("merged-by-arbiter", fold_ts, folded_id))
            new_keeper_metadata, appended = mint_lineage_marks(dict(keeper.metadata), carry)
            keeper_marks_appended += appended
            if not isinstance(keeper_canon, dict):
                # A pre-canon keeper cannot carry marks (canon §9) — the
                # edge + journal are the surviving provenance («не было»
                # is the honest report for the marks leg).
                keeper_marks_skipped_no_canon += 1
            if appended:
                _write_metadata(self._manager.sqlite, keeper_id, new_keeper_metadata)
                keeper = self._manager.sqlite.get(keeper_id, include_tombstoned=True)

            # The folded row carries its own grave mark BEFORE the
            # tombstone so the admin surfaces see WHY it is dead.
            folded_mark, folded_appended = mint_lineage_marks(
                dict(folded.metadata),
                [lineage_mark("merged-by-arbiter", fold_ts, keeper_id)],
            )
            if folded_appended:
                _write_metadata(self._manager.sqlite, folded_id, folded_mark)

            # Д3/OQ-2 minimum: the durable link.
            self._manager.add_memory_edge(
                keeper_id,
                folded_id,
                kind="relates_to",
                weight=MERGE_ARBITER_EDGE_WEIGHT,
                provenance=MERGE_ARBITER_PROVENANCE,
            )

            # The store's own Stage-1 tombstone (terminal state +
            # hash-chained journal + descendant quarantine).
            result = self._manager.delete_memory(
                folded_id,
                actor=ARBITER_ACTOR,
                reason=reason
                or (f"merged-by-arbiter: folded into {keeper_id} by vesma-cortex-merge-v2"),
            )
            report["folded"].append(
                {
                    "memory_id": folded_id,
                    "status": "folded",
                    "journal_id": result.get("journal_id"),
                    "entry_hash": result.get("entry_hash"),
                    "descendants_quarantined": result.get("descendants_quarantined", []),
                    "marks_on_keeper": appended,
                    "marks_on_folded": folded_appended,
                }
            )
            logger.info(
                "consolidation fold: keeper=%s folded=%s journal=%s",
                keeper_id[:8],
                folded_id[:8],
                (result.get("journal_id") or "")[:8],
            )

        report["keeper_marks_appended"] = keeper_marks_appended
        report["keeper_marks_skipped_no_canon"] = keeper_marks_skipped_no_canon
        return report
