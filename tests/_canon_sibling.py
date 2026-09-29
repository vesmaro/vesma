"""Shared worktree-safe resolver for the canon sibling repo.

Cascade review QA P2-2 (issue #433 follow-up): a ``__file__``-relative
sibling lookup silently degrades to fallback legs in ANY linked worktree
(the canon repo sits next to the PRIMARY checkout, not next to
``/tmp/...`` worktrees). Resolve it from git itself instead:

    git rev-parse --path-format=absolute --git-common-dir  → primary .git
    parent.parent of that                                   → primary repo root
    sibling                                                 → ../vesmaro-canon

Falls back to the historical ``__file__``-relative location (keeps
single-checkout layouts working when git is unavailable), and finally to
``None`` — callers then take their frozen-fallback leg. The pin protocol
(canon §9) stays honest: live when the sibling is findable, frozen when
it is not, never a silent wrong-source comparison.
"""

from __future__ import annotations

import subprocess  # nosec B404 — trusted local git, list args, no shell
from pathlib import Path

_CANON_SIBLING_NAME = "vesmaro-canon"


def engine_repo_root() -> Path:
    """The engine repo root of THIS test file (tests/..), resolved."""
    return Path(__file__).resolve().parent.parent


def canon_sibling_repo() -> Path | None:
    """Locate the canon sibling checkout, worktree-independent.

    Tries, in order: (1) next to the PRIMARY worktree of the engine repo
    (via ``--git-common-dir`` — correct from any linked worktree);
    (2) next to this test file's checkout (legacy single-checkout layout).
    Returns the canon repo dir when it looks like one (.git present),
    else ``None``.
    """
    for candidate in _candidate_roots():
        sibling = candidate / _CANON_SIBLING_NAME
        if (sibling / ".git").exists():
            return sibling
    return None


def canon_sibling_file(*relative: str) -> Path | None:
    """A file inside the canon sibling, or ``None`` when absent."""
    repo = canon_sibling_repo()
    if repo is None:
        return None
    candidate = repo.joinpath(*relative)
    return candidate if candidate.is_file() else None


def _candidate_roots() -> list[Path]:
    roots: list[Path] = []
    try:
        common = subprocess.run(  # nosec B603
            [
                "git",
                "-C",
                str(engine_repo_root()),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if common:
            common_path = Path(common)
            # «…/mnemos/.git» (normal checkout AND linked-worktree common
            # dir alike) → the canon sibling sits next to the REPO dir:
            # «…/Project-Mnemos/vesmaro-canon» (the #436 lesson — one
            # parent lands inside the engine repo, where no sibling lives).
            roots.append(
                common_path.parent.parent if common_path.name == ".git" else common_path.parent
            )
    except (OSError, subprocess.SubprocessError):
        pass
    roots.append(engine_repo_root().parent)
    roots.append(engine_repo_root())
    return roots
