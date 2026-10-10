"""Real-user-store snapshot helpers for the suite-wide isolation guard.

The engine's user store lives under the real HOME (``~/.vesma``) and under
``$XDG_DATA_HOME`` (default ``~/.local/share``) ``/vesma``. The test suite
must never read *or* write it: every test runs with HOME / VESMA_CONFIG /
XDG_* redirected into its own tmp directory (the ``isolate_user_store_env``
autouse fixture in ``tests/conftest.py``).

This module carries the snapshot primitives used by the guard:

* ``store_roots()`` — the real store roots, resolved from the ambient
  environment. Call it BEFORE any HOME redirection is in effect (i.e. from
  a session-scoped fixture), while HOME still points at the real home.
* ``snapshot_tree()`` / ``snapshot_roots()`` — an lstat-based
  ``{path: (size, mtime_ns)}`` map of a tree. Cheap even for large stores
  (no content hashing), and it catches creation, modification, and deletion
  alike. Symlinks are recorded as links (lstat), never followed.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Name of the guard test — conftest sinks it to the end of the session.
USER_STORE_GUARD_TEST = "test_user_store_untouched_by_suite"

#: lstat signature of one filesystem entry: (size, mtime_ns).
Snapshot = dict[str, tuple[int, int]]


def store_roots() -> list[Path]:
    """Resolve the real user-store roots from the (still real) environment."""
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home) if data_home else Path.home() / ".local" / "share"
    return [Path.home() / ".vesma", base / "vesma"]


def _lstat_signature(path: Path) -> tuple[int, int]:
    st = path.lstat()
    return st.st_size, st.st_mtime_ns


def snapshot_tree(root: Path) -> Snapshot:
    """lstat snapshot of ``root`` — no symlink following, no content reads."""
    entries: Snapshot = {}
    if root.exists():
        entries[str(root)] = _lstat_signature(root)
    if root.is_dir():
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in (*sorted(dirnames), *sorted(filenames)):
                entry = Path(dirpath) / name
                try:
                    entries[str(entry)] = _lstat_signature(entry)
                except OSError:  # raced away mid-walk — snapshot stays partial
                    continue
    return entries


def snapshot_roots(roots: list[Path]) -> Snapshot:
    """Merge :func:`snapshot_tree` over every root into one map."""
    merged: Snapshot = {}
    for root in roots:
        merged.update(snapshot_tree(root))
    return merged
