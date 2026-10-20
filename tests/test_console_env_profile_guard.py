"""Guard for the conftest console env profile (2026-10-20 CI wave).

The conftest strips color-forcing variables (``FORCE_COLOR``,
``CLICOLOR_FORCE``, ``PY_COLORS``, ``GITHUB_ACTIONS``, ``TTY_COMPATIBLE``)
for the whole pytest process BEFORE any vesma/typer import: typer bakes
``FORCE_TERMINAL`` from the environment at ``typer.rich_utils`` import
time, and rich then injects ANSI spans into CliRunner output regardless of
any per-invocation ``env=`` pin — which is how 4 tests failed in CI while
passing locally (2026-10-20 CI-only wave).

This file fails loudly if the profile regresses (e.g. someone moves the
env surgery below a ``vesma`` import or drops a variable), the same way
``test_store_isolation_guard.py`` guards the store-home profile.
"""

from __future__ import annotations

import os

#: Mirrors ``_COLOR_FORCING_VARS`` in tests/conftest.py (conftest is not
#: importable as a module under pytest's rootdir-relative import). If you
#: add a variable to the conftest profile, add it here too.
_COLOR_FORCING_VARS: tuple[str, ...] = (
    "FORCE_COLOR",
    "CLICOLOR_FORCE",
    "PY_COLORS",
    "GITHUB_ACTIONS",
    "TTY_COMPATIBLE",
)


def test_console_env_profile_strips_color_forcing_vars() -> None:
    """No color-forcing variable may leak into the pytest process env."""
    leaked = [var for var in _COLOR_FORCING_VARS if var in os.environ]
    assert leaked == [], (
        "console env profile regressed: color-forcing variables leaked into "
        f"the pytest process: {leaked} — rich/typer will bake ANSI into "
        "CliRunner output and substring assertions will fail CI-only"
    )


def test_console_env_profile_sets_no_color() -> None:
    """NO_COLOR is set as defense-in-depth for direct env readers."""
    assert os.environ.get("NO_COLOR") != ""
