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

import sys
from pathlib import Path

import typer

from vesma.config import (
    LegacyConfigError,
    LegacyStoreForkRefused,
    Settings,
    load_settings,
)
from vesma.manager import MemoryManager

_manager: MemoryManager | None = None


def load_settings_or_exit(config: str | Path | None = None) -> Settings:
    """CLI settings loader: the legacy-era guards render as ONE stderr line.

    The typed :class:`LegacyStoreForkRefused` (fork refusal, ADR-0044 gate 5)
    and :class:`LegacyConfigError` (legacy config detected) exceptions stay
    typed for doctor/API/tests, but the interactive CLI must answer with a
    single actionable line — never a rich traceback panel (this typer version
    pretty-prints any non-Exit exception, ClickException included).
    """
    try:
        return load_settings(config)
    except (LegacyConfigError, LegacyStoreForkRefused) as exc:
        print(f"vesma: {exc} [{exc.code}]", file=sys.stderr)
        raise typer.Exit(1) from exc


def get_manager(config: str | None = None) -> MemoryManager:
    """Return the process-wide :class:`MemoryManager` singleton.

    The first call constructs the manager from ``config`` (or the default
    config discovery path). Subsequent calls return the cached instance,
    ignoring the ``config`` argument — matching the original ``main.py``
    semantics.
    """
    global _manager
    if _manager is None:
        settings = load_settings_or_exit(config)
        _manager = MemoryManager(settings)
    return _manager


def reset_manager() -> None:
    """Clear the cached singleton. Used by tests that swap configs."""
    global _manager
    _manager = None
