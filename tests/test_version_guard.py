"""Version guard (#204) — VERSION file and pyproject.toml must agree.

Catches version drift between the two release markers BEFORE it reaches a
tag or a PyPI upload. Fail-loud, no fallbacks.

Also guards the import pin (#288): the suite must import THIS checkout's
``mnemos``, not a shadow install (user-site editable / .venv / another
checkout). The conftest front-pin enforces it at collection time; the
test below re-asserts the invariant so any session that collects this
file (e.g. one bypassing conftest via ``--noconftest``) still fails loud.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import mnemos
import vesma

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_version_file_matches_pyproject() -> None:
    """VERSION file must equal pyproject [project].version exactly."""
    version_file = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    pyproject_version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]
    assert version_file == pyproject_version, (
        f"version drift: VERSION={version_file!r} != "
        f"pyproject.version={pyproject_version!r} — bump both synchronously (#204)"
    )


def test_mnemos_import_provenance_pinned_to_checkout() -> None:
    """`import vesma` must resolve to THIS checkout's src/vesma (#288).

    A shadow import (user-site editable install / .venv / another checkout
    on PYTHONPATH) once made the suite silently test a stale build and
    produced 7 phantom TestSweeperVintageFingerprint failures. The
    conftest front-pin normally prevents this; this assert documents the
    invariant at test level and fails loud if the pin is ever bypassed.
    """
    resolved = Path(vesma.__file__).resolve()
    expected = (REPO_ROOT / "src" / "vesma" / "__init__.py").resolve()
    assert resolved == expected, (
        f"vesma shadow-imported from {resolved} — expected {expected}. "
        "The suite MUST run against this checkout's src/ (#288)."
    )
    # Dual-import period (ADR-0031): the mnemos shim resolves into this
    # checkout too — the shim file sits alongside the canonical package.
    shim = Path(mnemos.__file__).resolve()
    assert shim == (REPO_ROOT / "src" / "mnemos" / "__init__.py").resolve(), (
        f"mnemos shim resolved from {shim} — expected src/mnemos/__init__.py."
    )
