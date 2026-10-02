"""Repo hygiene tripwires (#337 — gate integrity after the rebrand).

One invariant that the canonical gates silently lost during the
``mnemos`` → ``vesmaro`` rename and the deploy waves:

1. **Venv canary (#335 class)** — the running pytest must come from THIS
   checkout's ``.venv``. The #335 incident had bare ``uv run pytest`` /
   ``make test`` fall through PATH to a foreign interpreter whose
   site-packages held a different vesma build: green locally, red (or
   silently wrong) in CI. ``tests/conftest.py`` pins *which code* is
   imported; this canary pins *which interpreter* runs it.
This check is an ordinary suite member: they ride every CI matrix leg and
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
