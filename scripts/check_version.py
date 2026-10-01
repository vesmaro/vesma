#!/usr/bin/env python
"""check-version gate: __version__ must match the installed dist metadata.

Tries the canonical distribution name first (`vesma`, 5.0.0 rebrand #333),
falls back to the legacy `vesma` / `mnemos-memory-server` names for
pre-rename environments — the same lookup chain as `vesmaro.__init__`
and `tests/test_version.py`. A missing metadata is a hard error — the
gate is meaningless without an editable/installed dist.
"""

from __future__ import annotations

from contextlib import suppress
from importlib.metadata import PackageNotFoundError, version

from vesmaro import __version__


def _installed(name: str) -> str | None:
    with suppress(PackageNotFoundError):
        return version(name)
    return None


def main() -> None:
    v = _installed("vesma") or _installed("vesmaro") or _installed("mnemos-memory-server")
    if v is None:
        raise SystemExit(
            "check-version: no distribution metadata found for "
            "vesma (or legacy mnemos-memory-server) — install with `pip install -e .`"
        )
    assert __version__ == v, f"mismatch: __init__={__version__}, metadata={v}"
    print(f"✓ version {v} consistent")


if __name__ == "__main__":
    main()
