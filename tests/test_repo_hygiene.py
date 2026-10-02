"""Repo hygiene tripwires (#337 — gate integrity after the rebrand).

Two invariants that the canonical gates silently lost during the
``mnemos`` → ``vesmaro`` rename and the deploy waves:

1. **Venv canary (#335 class)** — the running pytest must come from THIS
   checkout's ``.venv``. The #335 incident had bare ``uv run pytest`` /
   ``make test`` fall through PATH to a foreign interpreter whose
   site-packages held a different vesma build: green locally, red (or
   silently wrong) in CI. ``tests/conftest.py`` pins *which code* is
   imported; this canary pins *which interpreter* runs it.
2. **Shim-only tripwire (#337 item 4)** — ``src/mnemos/`` must contain
   ONLY the dual-import shim ``__init__.py`` (ADR-0031). Every gate that
   once pointed at ``src/mnemos`` was measuring the shim, not the engine;
   new files landing there would re-create that blindness silently.

Both checks are ordinary suite members: they ride every CI matrix leg and
every local canonical run, which is strictly stronger than a one-line CI
step (issue #337 fix direction 4).
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def _running_pytest_path() -> Path:
    """Resolve the running pytest package once per session."""
    return Path(pytest.__file__).resolve()


def test_pytest_runs_from_repo_venv(_running_pytest_path: Path) -> None:
    """The executing pytest must live inside this checkout's .venv.

    Fail-loud tripwire for the #335 PATH-fallthrough class: a bare
    ``pytest``/``uv run pytest`` that resolved a global or foreign-venv
    interpreter produces phantom results (different vesma build, different
    plugins). The canonical invocations — ``uv sync`` + ``.venv/bin/pytest``,
    ``make bootstrap``, ``scripts/local-ci.sh``, CI's ``uv venv`` — all put
    the tools in ``<checkout>/.venv`` and pass this check.
    """
    repo_venv = (REPO_ROOT / ".venv").resolve()
    running = _running_pytest_path
    assert repo_venv in running.parents, (
        f"pytest is running from a foreign environment: {running} is not "
        f"inside {repo_venv}. Recreate the canonical env and invoke it "
        f"explicitly: `uv sync --python 3.12` then `.venv/bin/pytest ...` "
        f"(or `make bootstrap`). A bare `pytest` on PATH may be a global "
        f"install — do not use it to adjudicate gates (#335)."
    )


def test_mnemos_dir_holds_only_dual_import_shim() -> None:
    """src/mnemos/ must contain ONLY the ADR-0031 shim file.

    The canonical package is ``src/vesma/``; ``src/mnemos/`` exists solely
    for the dual-import compatibility window (retires no earlier than 6.0).
    Any other file there would (a) be invisible to the retargeted mypy gate
    blind spot and (b) signal that new code is again being added under the
    deprecated prefix.
    """
    shim_dir = REPO_ROOT / "src" / "mnemos"
    assert shim_dir.is_dir(), "src/mnemos/ vanished — shim contract (ADR-0031) broken"
    files = sorted(
        str(p.relative_to(shim_dir))
        for p in shim_dir.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    )
    assert files == ["__init__.py"], (
        f"src/mnemos/ must hold only the shim __init__.py (ADR-0031), found: "
        f"{files}. New code belongs in src/vesma/ (issue #337 tripwire)."
    )
