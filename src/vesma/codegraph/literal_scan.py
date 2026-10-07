"""Bounded literal-content scan for hybrid search_graph (wave W-H, ADR-0032).

The symbol index is shapes-only (PG1): string literals — tool names,
route paths, env-var names — are invisible to ``search_graph``. The
literal fallback is a READ-ONLY, confinement-bound scan of the
REGISTERED project root that runs ONLY on an empty symbol result and
never writes anything to the index.

Discipline:

* **Surface reuse** — the indexer's denylists (``file_surface``) prune
  the walk: dependency/build/vendored directories, dotfiles and
  secret-bearing file names (``.env`` / ``.key`` / ``.pem`` ...) are
  never opened. Symlinks are never followed (the PG2 surface rule).
* **Bounds** — hard caps on files scanned, per-file bytes, total
  matches and wall-clock time; every cap firing sets ``truncated`` so
  the caller can log honest incompleteness. Binary content (a NUL
  byte) is skipped, never decoded.
* **PG4 stays binding** — the scan ISSUES nothing by itself; the
  service redacts every row through the same secrets detector used at
  snippet issuance, and drops rows from poisoned paths (PG3). A scan
  that cannot complete safely degrades to no-fallback upstream — raw
  content never reaches a response.
"""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from dataclasses import dataclass

from vesma.codegraph.file_surface import (
    DENY_DIRS,
    DENY_NAME_PREFIXES,
    DENY_NAME_SUFFIXES,
    VENDORED_DIR_NAMES,
    dir_matches_exclude_globs,
)

#: Literal rows issued per fallback run (the service slices the first N
#: after redaction; the scan over-collects so detector drops still
#: leave a full page).
LITERAL_ROW_CAP = 20

#: The scan's own over-collection ceiling — bounds memory when a common
#: substring floods a large tree.
LITERAL_MATCH_CAP = 200

#: Per-file read ceiling: bigger files are skipped, not truncated.
LITERAL_MAX_FILE_BYTES = 1_000_000

#: Wall-clock budget for one fallback scan (~2s per the W-H contract).
LITERAL_SCAN_TIME_BUDGET_SEC = 2.0

#: Issued snippet length cap — bounds the token-contract row cost.
LITERAL_SNIPPET_MAX_CHARS = 240

#: Default file-count ceiling — the indexer's PG7 per-project ceiling.
LITERAL_MAX_FILES = 20_000


@dataclass(frozen=True, slots=True)
class LiteralMatch:
    """One literal-content hit: repo-relative POSIX path (PG1 — never
    absolute), 1-based line number and the trimmed line text."""

    path: str
    line: int
    snippet: str


def _denied_dir(name: str) -> bool:
    return name in DENY_DIRS or name in VENDORED_DIR_NAMES


def _denied_name(name: str) -> bool:
    return name.startswith(DENY_NAME_PREFIXES) or name.endswith(DENY_NAME_SUFFIXES)


def scan_literals(
    root: str,
    query: str,
    *,
    max_files: int = LITERAL_MAX_FILES,
    max_file_bytes: int = LITERAL_MAX_FILE_BYTES,
    time_budget_sec: float = LITERAL_SCAN_TIME_BUDGET_SEC,
    snippet_max_chars: int = LITERAL_SNIPPET_MAX_CHARS,
    match_cap: int = LITERAL_MATCH_CAP,
    exclude_globs: Sequence[str] = (),
) -> tuple[list[LiteralMatch], bool]:
    """Case-insensitive substring scan over text files under ``root``.

    Deterministic (sorted walk), read-only, bounded by ``max_files``
    files scanned, ``max_file_bytes`` per file, ``match_cap`` matches
    and the ``time_budget_sec`` wall clock. Returns ``(matches,
    truncated)`` — ``truncated`` is True when ANY cap fired early, so
    the caller never presents a capped scan as complete.
    ``exclude_globs`` mirrors ``CodeGraphConfig.exclude_globs`` so the
    fallback never surfaces content the indexer surface excludes.
    """
    needle = query.strip().casefold()
    if not needle:
        return [], False
    matches: list[LiteralMatch] = []
    truncated = False
    deadline = time.monotonic() + time_budget_sec
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        # Prune denied directories in place so os.walk skips them.
        dirnames[:] = sorted(
            d
            for d in dirnames
            if not _denied_dir(d) and not dir_matches_exclude_globs(rel_dir, d, exclude_globs)
        )
        for entry in sorted(filenames):
            if scanned >= max_files or len(matches) >= match_cap:
                truncated = True
                return matches, truncated
            if time.monotonic() > deadline:
                truncated = True
                return matches, truncated
            full = os.path.join(dirpath, entry)
            if os.path.islink(full):
                continue  # never followed, never read (PG2 surface rule)
            if _denied_name(entry):
                continue
            scanned += 1
            try:
                if os.path.getsize(full) > max_file_bytes:
                    continue
                with open(full, "rb") as fh:
                    data = fh.read(max_file_bytes + 1)
            except OSError:
                continue  # unreadable — not a literal source
            if len(data) > max_file_bytes or b"\0" in data:
                continue  # oversized or binary — skip whole file
            text = data.decode("utf-8", "replace")
            if needle not in text.casefold():
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            for i, line in enumerate(text.splitlines(), start=1):
                if needle in line.casefold():
                    matches.append(
                        LiteralMatch(
                            path=rel,
                            line=i,
                            snippet=line.strip()[:snippet_max_chars],
                        )
                    )
                    if len(matches) >= match_cap:
                        truncated = True
                        return matches, truncated
    return matches, truncated
