"""Shared singleton manager accessor for CLI subcommands.

Extracted from ``cli/main.py`` to break a circular import:
``main`` imports ``export_cmd`` / ``import_cmd`` at module load (to register
the Typer sub-apps), and those modules imported ``get_manager`` from
``main`` — forming a cycle that left mypy unable to resolve the sub-app
types. By moving the accessor here, subcommand modules import from this
leaf module instead, and ``main.py`` re-exports ``get_manager`` for
backward compatibility.
"""

from __future__ import annotations

from vesma.config import load_settings
from vesma.manager import MemoryManager

_manager: MemoryManager | None = None


def get_manager(config: str | None = None) -> MemoryManager:
    """Return the process-wide :class:`MemoryManager` singleton.

    The first call constructs the manager from ``config`` (or the default
    config discovery path). Subsequent calls return the cached instance,
    ignoring the ``config`` argument — matching the original ``main.py``
    semantics.
    """
    global _manager
    if _manager is None:
        settings = load_settings(config)
        _manager = MemoryManager(settings)
    return _manager


def reset_manager() -> None:
    """Clear the cached singleton. Used by tests that swap configs."""
    global _manager
    _manager = None
