"""Filesystem hardening for the vesma home tree (cascade fix 2026-10-09).

The first touch of a fresh home used to inherit the process umask:
``~/.vesma/{data,vault,logs}`` landed 0755 and the memory database, the
WAL sidecars, the vault markdown, and the logs landed 0644 — every other
local account could READ the store. These helpers are the
``store_migration`` ``_harden_*`` discipline made shareable:

- creation goes through :func:`ensure_private_dir` — every created level
  is 0700 (``mkdir(mode=0o700)`` can only be narrowed further by the
  umask, never widened), including the intermediate parents that
  ``mkdir(parents=True)`` used to create at the umask default;
- existing paths are narrowed ONLY when they are wider than the private
  bar (never widened, never touched when already compliant);
- files the store writes (db, WAL/SHM sidecars, logs, vault markdown)
  go through :func:`harden_file` after creation — narrow-if-wider 0600;
- every chmod is best-effort: a failure (foreign owner, read-only fs)
  is logged at DEBUG, never raised — hardening must not turn a working
  store into a crashed one.
"""

from __future__ import annotations

import logging
import stat
from pathlib import Path
from typing import Final

#: Directory bar for everything under the vesma home.
PRIVATE_DIR_MODE: Final[int] = 0o700
#: File bar for store-owned data files (db, sidecars, logs, vault notes).
PRIVATE_FILE_MODE: Final[int] = 0o600

_log = logging.getLogger(__name__)


def _narrow(path: Path, mode: int, *, must_be: int) -> None:
    """Best-effort chmod ``mode`` when ``path`` is wider; silent no-op else."""
    try:
        st = path.stat()
    except OSError:
        return
    if (st.st_mode & 0o170000) != must_be:
        return  # wrong kind (file where a dir is expected etc.) — never touch
    if st.st_mode & 0o777 & ~mode:
        try:
            path.chmod(mode)
        except OSError as exc:
            _log.debug("fs_hardening: chmod %o on %s failed: %s", mode, path, exc)


def ensure_private_dir(path: Path) -> None:
    """``mkdir -p`` with 0700 on every created level; narrow wider existing.

    Drop-in replacement for ``path.mkdir(parents=True, exist_ok=True)`` on
    vesma-owned directories. Raises the same errors plain ``mkdir`` would
    for unusable paths (an existing non-directory raises
    :class:`NotADirectoryError`); only the permission narrowing itself is
    best-effort.
    """
    if path.exists():
        if not path.is_dir():
            raise NotADirectoryError(f"{path} exists and is not a directory")
        _narrow(path, PRIVATE_DIR_MODE, must_be=stat.S_IFDIR)
        return
    # Create the missing chain ourselves so intermediate levels get 0700
    # (mkdir(parents=True) creates them at the umask default — the leak).
    missing: list[Path] = []
    probe: Path = path
    while not probe.exists():
        missing.append(probe)
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    for segment in reversed(missing):
        try:
            segment.mkdir(mode=PRIVATE_DIR_MODE)
        except FileExistsError:
            continue  # a concurrent creator won the race — still 0700-bound
    # Narrow the whole chain: pre-existing levels may be wider than 0700
    # (an old install under a permissive umask), newly created ones are
    # already compliant and this is a no-op for them.
    for segment in (*reversed(missing), path):
        _narrow(segment, PRIVATE_DIR_MODE, must_be=stat.S_IFDIR)


def harden_file(path: Path) -> None:
    """Best-effort 0600 (narrow-if-wider) on a store-owned file.

    Safe to call after every write: a fresh file created under a permissive
    umask gets narrowed; an already-private file is not touched.
    """
    _narrow(path, PRIVATE_FILE_MODE, must_be=stat.S_IFREG)


__all__ = ["PRIVATE_DIR_MODE", "PRIVATE_FILE_MODE", "ensure_private_dir", "harden_file"]
