"""File surface for the code graph indexer (ADR-0032 §3.6 PG3, slice 2).

Order matters and is contract-fixed: DENYLIST first (dotfiles,
secret-bearing extensions, build/dependency directories, vendored
trees), THEN the extension allowlist derived from the language
registry. Symlinks are never followed and never indexed — a symlink is
rejected explicitly, not merely skipped by the walker (PG2/PG3: an
ambiguous edge in the surface is a rejection, not a judgement call).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch

from vesmaro.codegraph.languages import language_for_path

#: Directory names never entered (denylist, contract §3.6 PG3).
DENY_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "venv",
        ".venv",
        "node_modules",
        "dist",
        "build",
        "target",
        "site-packages",
        # Defect 2026-10-03: a git worktree checked out INSIDE a
        # registered root (``git worktree add wt/<name>`` convention)
        # was walked like first-party sources — duplicated symbols in
        # the graph and re-poisoned fixtures on every worktree wave.
        # A nested worktree is never first-party code.
        "wt",
    }
)

#: File-name patterns never indexed: dotfiles, env files, key/cert
#: material. Matched against the file NAME only, never the content.
DENY_NAME_PREFIXES: tuple[str, ...] = (".",)
DENY_NAME_SUFFIXES: tuple[str, ...] = (
    ".env",  # .env, .env.local, prod.env
    ".key",
    ".pem",
    ".p12",
    ".pfx",
)
#: Vendored third-party trees — indexed sources must be first-party.
VENDORED_DIR_NAMES: frozenset[str] = frozenset({"vendor", "vendored", "third_party", "3rdparty"})

#: Extra root-relative directory prefixes denied on top of names.
DENY_RELATIVE_PREFIXES: tuple[str, ...] = ()


def _is_denied_name(name: str) -> bool:
    if name.endswith(DENY_NAME_SUFFIXES):
        return True
    return name.startswith(DENY_NAME_PREFIXES)


def _is_denied_dir(name: str) -> bool:
    return name in DENY_DIRS or name in VENDORED_DIR_NAMES


def dir_matches_exclude_globs(parent_rel: str, name: str, globs: Iterable[str]) -> bool:
    """Would the directory ``name`` (under repo-relative ``parent_rel``)
    match any of the configured ``exclude_globs``?

    A bare glob (no ``/``) matches a directory NAME at any nesting
    depth (``wt`` prunes ``wt/`` and ``a/b/wt/`` alike); a glob with
    ``/`` matches the repo-relative directory path with ``fnmatch``
    semantics (``gen/**`` prunes everything under ``gen/``). Pure —
    no filesystem access.
    """
    for glob in globs:
        if not glob:
            continue
        if "/" not in glob:
            if fnmatch(name, glob):
                return True
        else:
            rel = name if parent_rel in ("", ".") else f"{parent_rel}/{name}"
            if fnmatch(rel, glob):
                return True
    return False


@dataclass(frozen=True, slots=True)
class SurfaceFile:
    """One candidate file that passed the surface filters.

    Attributes:
        rel_path: Repo-relative POSIX path (PG1 — never absolute).
        abs_path: Resolved absolute path for reading (never persisted).
    """

    rel_path: str
    abs_path: str


class FileSurface:
    """Walks a project root and yields the indexable file set.

    The walk is deterministic (sorted) so the graph and the
    ``files_indexed`` report are stable across runs on the same tree.

    ``exclude_globs`` (operator knob ``CodeGraphConfig.exclude_globs``)
    prunes directories ON TOP of the built-in ``DENY_DIRS`` denylist —
    it can never re-include a denied name.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        exclude_globs: Sequence[str] = (),
    ) -> None:
        self.root = os.path.abspath(root)
        self._exclude_globs = tuple(exclude_globs)

    def collect(self) -> list[SurfaceFile]:
        """Return the sorted list of surface-passing files.

        Symlinks are rejected explicitly (files) and not descended into
        (directories) — ``os.walk`` runs with ``followlinks=False`` and
        every symlink hit is dropped from the surface.
        """
        results: list[SurfaceFile] = []
        root = self.root
        globs = self._exclude_globs
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
            # Prune denied directories in place so os.walk skips them.
            dirnames[:] = sorted(
                d
                for d in dirnames
                if not _is_denied_dir(d) and not dir_matches_exclude_globs(rel_dir, d, globs)
            )
            for entry in sorted(filenames):
                full = os.path.join(dirpath, entry)
                if os.path.islink(full):
                    continue  # symlinks: never followed, never indexed
                if _is_denied_name(entry):
                    continue
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                if language_for_path(rel) is None:
                    continue  # extension allowlist (registry-derived)
                results.append(SurfaceFile(rel_path=rel, abs_path=full))
        return results


def surface_allows(
    rel_path: str,
    exclude_globs: Sequence[str] = (),
) -> bool:
    """Pure predicate form of the surface rules (staleness pre-check).

    Answers ``would the indexer consider this path`` without touching
    the filesystem — used by the cheap staleness report to classify
    files that exist on disk but were never indexed because of the
    denylist/allowlist/exclude-globs.
    """
    name = rel_path.rsplit("/", 1)[-1]
    if _is_denied_name(name):
        return False
    parts = rel_path.split("/")
    for i, part in enumerate(parts[:-1]):
        if _is_denied_dir(part):
            return False
        parent_rel = "/".join(parts[:i])
        if dir_matches_exclude_globs(parent_rel, part, exclude_globs):
            return False
    return language_for_path(rel_path) is not None
