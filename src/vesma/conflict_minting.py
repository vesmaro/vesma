"""Conflict-mint surface for the ``contradicts`` edge kind (duty #6).

Wave B6-conflict (ADR 0002 duty #6): the corpus gate closed 2026-11-02
(sealed holdout + g_gold PASS 48/48, committee fallback), so the edge
kind exists and an ADVISORY mint surface is wired. Production
auto-minting does NOT fire in this slice — the conflict-detector
artifact is not trained yet (training follows the migration per the
duty letter); the surface exists so a dev run can mint explicitly and
so the post-ADOPT auto-mint lands on a tested path.

Semantics carried from the ratified corpus design (package §2/§7):

* rank-only by default — weight stays at the store default 1.0; the
  edge warns assembly/ranking about conflicting sources and never
  suppresses or deletes a record (zero-loss canon, ADR-0019 §2b);
* provenance ``conflict-detector`` — the duty-family provenance value
  (parallels ``auto-dedupe``/``merge-arbiter``);
* NC-3 suppression — a pair already bound by ``supersedes`` (either
  direction) must NOT gain a conflict edge: staleness is supersede
  semantics, not a contradiction. The check runs on EVERY mint
  attempt; it cannot be switched off at the call site.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from vesma.storage.sqlite_store import SQLiteStore

#: The edge kind this surface mints (kept in lockstep with
#: ``sqlite_store._EDGE_KINDS``).
CONTRADICTS_EDGE_KIND: Final[str] = "contradicts"

#: Provenance recorded on minted conflict edges — the duty-family value
#: (mirrors ``auto-dedupe`` for the fuel mint and ``merge-arbiter`` for
#: the fold claim).
CONTRADICTS_PROVENANCE: Final[str] = "conflict-detector"

#: Rank-only default: the store default weight. A conflict warning
#: enters ranking but never restores eligibility (I3, ADR-0030).
CONTRADICTS_EDGE_WEIGHT: Final[float] = 1.0

#: Outcomes of a mint attempt (telemetry carries one line per attempt).
MINTED: Final[str] = "minted"
DUPLICATE: Final[str] = "duplicate"
SUPPRESSED_SUPERSEDES: Final[str] = "suppressed-supersedes"


def suppressed_by_supersedes(store: SQLiteStore, from_memory_id: str, to_memory_id: str) -> bool:
    """NC-3 suppression rule: the pair is already bound by ``supersedes``.

    Checked in BOTH directions — superseding is directional (new → old)
    while the conflict relation is symmetric-ish, and a pair that
    settled on supersede semantics must not grow a conflict warning
    regardless of which side the caller names first.
    """
    return store.has_memory_edge_between(from_memory_id, to_memory_id, kind="supersedes") or (
        store.has_memory_edge_between(to_memory_id, from_memory_id, kind="supersedes")
    )
