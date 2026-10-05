"""Component cache write API (specs/layout/v1 §3.9, checklist LY-12).

``~/.cache/vesma/<name>/`` (mode ``0750``) holds regenerable content only
and NEVER secrets. The contract places the enforcement at the ENGINE
WRITE-API level: values originating from env files carry the secret taint
at load time (:class:`vesmaro.service.envfile.TaintedValue`) and this API
refuses tainted values outright (LY-12) — today there is no cache writer
in the engine, so this guard exists BEFORE the first writer. Writes
bypassing the API (same-uid straight to the filesystem) are outside the
contract radius; the doctor checks own that surface (§3.10, DR-family).

Deleting the cache directory at any time never affects installation
correctness: entries here are regenerable by definition, and
:func:`put_under_cache` recreates the directory (explicit ``0750``,
umask-independent) on every call.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from pathlib import Path

from vesmaro.service.envfile import TaintedValue
from vesmaro.service.layout import cache_dir, ensure_dir

__all__ = ["CACHE_DIR_MODE", "CacheWriteRefusedError", "put_under_cache"]

CACHE_DIR_MODE = 0o750  # layout §3.9 (LY-12 table row)

# Component name — the CM §3.1 manifest name grammar: the cache dir is
# per-component, and a traversal name must be refused at the boundary.
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
# Cache entry key: a safe single path segment (no separators, no dotfiles).
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class CacheWriteRefusedError(Exception):
    """The cache write API refused a write (layout §3.9 / LY-12).

    The message names the component and key ONLY — the refused value is
    by definition suspect, so it is never echoed.
    """


def put_under_cache(
    name: str,
    key: str,
    value: str | bytes,
    *,
    tainted: bool = False,
) -> Path:
    """Write ONE regenerable cache entry; the single sanctioned cache writer.

    Refuses (LY-12 MUST):
    - values carrying the env-file secret taint — either declared via
      ``tainted=True`` or carried by the :class:`TaintedValue` type the
      env-file loader returns;
    - component names outside the manifest grammar or keys that are not a
      safe single path segment.

    Creates ``~/.cache/vesma/<name>/`` (``0750``, umask-independent) on
    demand and writes atomically (temp file + rename). Returns the entry
    path. Deleting the directory is always safe — the next call
    regenerates it.
    """
    if not _NAME_RE.match(name):
        raise CacheWriteRefusedError(
            f"cache write refused: component name {name!r} is not a valid "
            "component name (CM §3.1) — refusing to map it to a cache directory"
        )
    if not _KEY_RE.match(key):
        raise CacheWriteRefusedError(
            f"cache write refused for component {name!r}: key {key!r} is not a "
            "safe single path segment ([A-Za-z0-9][A-Za-z0-9._-]*, no separators)"
        )
    if tainted or isinstance(value, TaintedValue):
        raise CacheWriteRefusedError(
            f"cache write refused for component {name!r} key {key!r}: the value "
            "carries the secret taint (originates from an env file) — the "
            "component cache NEVER holds secrets (specs/layout/v1 §3.9, LY-12)"
        )
    payload = value.encode("utf-8") if isinstance(value, str) else value
    directory = ensure_dir(cache_dir(name), CACHE_DIR_MODE)
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.replace(tmp_name, directory / key)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return directory / key
