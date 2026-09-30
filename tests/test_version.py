"""Test that __version__ is consistent with package metadata."""

from __future__ import annotations

from contextlib import suppress
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version

from mnemos import __version__


def test_version_is_string() -> None:
    """__version__ must be a non-empty string."""
    assert isinstance(__version__, str)
    assert len(__version__) > 0


def test_version_matches_metadata() -> None:
    """__version__ must match the version in pyproject.toml (via metadata).

    The distribution was renamed ``mnemos-memory-server`` → ``vesmaro``
    by the 5.0.0 rebrand (#333): the canonical lock env installs the
    editable under the NEW name, so the metadata lookup must use it (the
    legacy-name lookup was a pre-existing canonical-env failure, same
    rebrand-leftover family as the #337 lint errors — caught by the #335
    canonical-gate adjudication).

    Release-branch adaptation (4.3.0, pre-rebrand cut): the lookup tries
    the post-rebrand name first and falls back to the legacy name —
    identical to scripts/check_version.py — so the test holds on both
    sides of the rename.
    """

    def _installed(name: str) -> str | None:
        with suppress(PackageNotFoundError):
            return pkg_version(name)
        return None

    metadata_version = _installed("vesma") or _installed("vesmaro") or _installed("mnemos-memory-server")
    assert metadata_version is not None, (
        "no distribution metadata found for vesma (or legacy "
        "vesmaro/mnemos-memory-server) — install with `pip install -e .`"
    )
    assert __version__ == metadata_version, (
        f"__version__ ({__version__}) != metadata ({metadata_version})"
    )


def test_version_not_unknown() -> None:
    """__version__ must not be the fallback '0.0.0+unknown'."""
    assert __version__ != "0.0.0+unknown"
