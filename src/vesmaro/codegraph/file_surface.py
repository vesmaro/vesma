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
from dataclasses import dataclass

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
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = os.path.abspath(root)

    def collect(self) -> list[SurfaceFile]:
        """Return the sorted list of surface-passing files.

        Symlinks are rejected explicitly (files) and not descended into
        (directories) — ``os.walk`` runs with ``followlinks=False`` and
        every symlink hit is dropped from the surface.
        """
        results: list[SurfaceFile] = []
        root = self.root
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            # Prune denied directories in place so os.walk skips them.
            dirnames[:] = sorted(d for d in dirnames if not _is_denied_dir(d))
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


def surface_allows(rel_path: str) -> bool:
    """Pure predicate form of the surface rules (staleness pre-check).

    Answers ``would the indexer consider this path`` without touching
    the filesystem — used by the cheap staleness report to classify
    files that exist on disk but were never indexed because of the
    denylist/allowlist.
    """
    name = rel_path.rsplit("/", 1)[-1]
    if _is_denied_name(name):
        return False
    parts = rel_path.split("/")
    if any(_is_denied_dir(p) for p in parts[:-1]):
        return False
    return language_for_path(rel_path) is not None
