"""Process-wide background scanner singleton.

Layer 2 of the federation defence-in-depth (ArchCom 2026-07-17 contract
§2.2.1). The scanner is constructed lazily from the same
:class:`~vesma.config.Settings` as the :class:`~vesma.manager.MemoryManager`
and started/stopped alongside the background processor in the MCP server
and HTTP API lifespans.

The singleton lives here (rather than on ``MemoryManager``) so the
scanner stays an optional, separately-configured component — disabling
it via ``scanner.enabled = False`` does not touch the manager.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from vesma.scanner import BackgroundScanner

if TYPE_CHECKING:
    from vesma.manager import MemoryManager

_scanner: BackgroundScanner | None = None
_scanner_lock = threading.Lock()


def get_scanner(manager: MemoryManager) -> BackgroundScanner:
    """Return the process-wide :class:`BackgroundScanner` singleton.

    Constructed on first call from ``manager.settings.scanner``.
    Subsequent calls return the cached instance, ignoring the
    ``manager`` argument (the scanner is bound to the first manager it
    saw, which is the process-wide singleton manager).

    Thread-safe: the check-and-assign is guarded by a module-level
    :class:`threading.Lock` so two concurrent startup paths (e.g. ASGI
    lifespan + a CLI ``scanner run`` in the same process) cannot both
    construct a scanner and leak the first.
    """
    global _scanner
    with _scanner_lock:
        if _scanner is None:
            _scanner = BackgroundScanner(manager, manager.settings.scanner)
        return _scanner


def reset_scanner() -> None:
    """Clear the cached singleton. Used by tests that swap configs."""
    global _scanner
    with _scanner_lock:
        _scanner = None
