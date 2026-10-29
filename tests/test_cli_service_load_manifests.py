"""Pin (card 0f75698d, ArchCom 2026-10-29): the supervisor entrypoint's
bundle fallback is LOUD — a silent fallback was the loss class (board/
metrics looked alive while operator components, e.g. mesh, were not
loaded). The fallback itself stays legitimate for a machine with no
operator manifests yet; what changed is that journald now carries the
warning."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from vesma.cli import service as service_cli


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    return home


def test_bundle_fallback_warns(isolated_home: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="vesma.cli.service"):
        manifests = service_cli._load_manifests()
    assert set(manifests) == {"board", "metrics"}
    fallback_warnings = [
        record for record in caplog.records if "operator manifests not found" in record.getMessage()
    ]
    assert fallback_warnings, "the bundle fallback must be loud, not silent"
    assert str(isolated_home / ".vesma" / "components.d") in fallback_warnings[0].getMessage()


def test_existing_components_dir_does_not_warn(
    isolated_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    components_dir = isolated_home / ".vesma" / "components.d"
    components_dir.mkdir(parents=True)
    with caplog.at_level(logging.WARNING, logger="vesma.cli.service"):
        manifests = service_cli._load_manifests()
    assert manifests == {}  # dir exists, nothing installed yet
    assert not [
        record for record in caplog.records if "operator manifests not found" in record.getMessage()
    ]
