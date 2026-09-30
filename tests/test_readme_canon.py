"""README canon: the front-page badges and registry links tell the truth.

The 5.1.0 release shipped with an npm badge pointing at the legacy
`pi-mnemos` package (honest 4.3.0 — wrong project) and the pre-rebrand
repo slug. This pin forbids the whole class: the badge block must
reference ONLY the canonical registry names; static version text in
badges is banned (dynamic endpoints cannot lie)."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _readme(name: str) -> str:
    return (REPO / name).read_text(encoding="utf-8")


def test_npm_badge_points_at_canonical_scoped_package() -> None:
    for name in ("README.md", "README.ru.md"):
        s = _readme(name)
        assert "shields.io/npm/v/pi-mnemos" not in s, (
            f"{name}: npm badge targets the legacy pi-mnemos package"
        )
        assert "shields.io/npm/v/@vesmaro%2Fvesma" in s, (
            f"{name}: canonical scoped npm badge missing"
        )
        assert "npmjs.com/package/@vesmaro/vesma" in s, f"{name}: npm link missing"


def test_version_badge_uses_canonical_repo_slug() -> None:
    for name in ("README.md", "README.ru.md"):
        s = _readme(name)
        assert "shields.io/github/v/release/vesmaro/vesmaro" not in s, (
            f"{name}: version badge uses the pre-rebrand slug"
        )
        assert "shields.io/github/v/release/vesmaro/vesma" in s, (
            f"{name}: canonical version badge missing"
        )


def test_pypi_badge_is_dynamic() -> None:
    """A static `badge/pypi-vN` would freeze one release forever (the
    v0.0.1-forever class); the dynamic /pypi/v/ endpoint cannot."""
    for name in ("README.md", "README.ru.md"):
        s = _readme(name)
        assert "shields.io/pypi/v/vesma" in s, f"{name}: dynamic PyPI badge missing"
        assert "badge/pypi-" not in s, f"{name}: static PyPI version badge is banned"


def test_project_graph_is_announced() -> None:
    for name, needle in (
        ("README.md", "The codebase becomes memory"),
        ("README.ru.md", "Кодовая база становится памятью"),
    ):
        assert needle in _readme(name), f"{name}: the project-graph feature bullet is missing"
