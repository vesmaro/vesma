"""Guard: a suite run leaves the real user store untouched (P1 hygiene).

The baseline snapshot is taken at session start (before the first test,
while HOME / XDG_* still point at the real environment) by the session-scoped
``real_user_store_baseline`` fixture in ``tests/conftest.py``. This test is
sunk to the very end of the session by ``pytest_collection_modifyitems``,
then the real store (~/.vesma, $XDG_DATA_HOME/vesma) is re-scanned: any
created, deleted, or modified path fails the suite — meaning the test
process (or something it spawned) reached the production user store.

On a machine where unrelated live processes (a running vesma server, for
instance) write to the store mid-run, the guard names the changed paths so
the operator can attribute them; on a clean CI runner it is deterministic.
"""

from __future__ import annotations

from pathlib import Path

from tests._user_store_guard import Snapshot, snapshot_roots

_LIST_LIMIT = 15


def _fmt(paths: list[str]) -> str:
    shown = ", ".join(paths[:_LIST_LIMIT])
    if len(paths) > _LIST_LIMIT:
        shown += f" … (+{len(paths) - _LIST_LIMIT} more)"
    return shown


def test_user_store_untouched_by_suite(
    real_user_store_baseline: tuple[list[Path], Snapshot],
) -> None:
    """Diff the real user store against its session-start snapshot."""
    roots, before = real_user_store_baseline
    after = snapshot_roots(roots)

    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    modified = sorted(p for p in set(before) & set(after) if before[p] != after[p])

    problems: list[str] = []
    if added:
        problems.append(f"created: {_fmt(added)}")
    if removed:
        problems.append(f"deleted: {_fmt(removed)}")
    if modified:
        problems.append(f"modified: {_fmt(modified)}")

    assert not problems, (
        "the test suite touched the real user store ("
        + ", ".join(str(r) for r in roots)
        + "): "
        + "; ".join(problems)
    )
