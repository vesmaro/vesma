"""Portability hygiene pins (wave61 portability slice, audit F6-F8).

Three machine-assumption guards, each with a behavioral pin:

* ``VESMA_PROD_VENV_BASES`` — the prod-venv scan bases are env-overridable
  (the design machine-map literal stays as the DEFAULT, never a hardcode
  the operator cannot change);
* DR-11 skips with an OK finding on non-Linux unix instead of emitting
  systemd/journald WARN noise that cannot apply there;
* ``self-update timer install`` refuses BEFORE writing unit files on a
  non-Linux host without ``systemctl`` ("not supported on this platform").
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest
import typer
from rich.console import Console

from vesma.cli import update_cmd as updates_cli
from vesma.service import doctor_checks
from vesma.service.doctor_checks import Severity


class TestProdVenvBasesOverride:
    def test_env_override_replaces_bases(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base_a = tmp_path / "a"
        base_b = tmp_path / "b"
        for base in (base_a, base_b):
            (base / "mnemos-prod" / "venv-api").mkdir(parents=True)
        monkeypatch.setenv("VESMA_PROD_VENV_BASES", os.pathsep.join((str(base_a), str(base_b))))
        found = updates_cli._prod_venv_paths()
        assert len(found) == 2, found
        assert all("mnemos-prod" in str(p) for p in found)

    def test_env_entries_expanduser(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".local" / "share" / "mnemos-prod" / "venv-x").mkdir(parents=True)
        monkeypatch.setenv(
            "VESMA_PROD_VENV_BASES", os.pathsep.join(("~/does-not-exist", "~/.local/share"))
        )
        found = updates_cli._prod_venv_paths()
        assert found == [tmp_path / ".local" / "share" / "mnemos-prod" / "venv-x"], found

    def test_unset_env_keeps_default_bases(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VESMA_PROD_VENV_BASES", raising=False)
        bases = updates_cli._prod_venv_bases()
        assert Path.home() / ".local" / "share" in bases
        assert Path("/var/home/abyss/.local/share") in bases


class TestDoctorNonLinuxSkip:
    @pytest.mark.parametrize("platform", ["darwin", "freebsd"])
    def test_dr11_skips_without_systemd_warn(
        self, monkeypatch: pytest.MonkeyPatch, platform: str
    ) -> None:
        monkeypatch.setattr(sys, "platform", platform)
        finding = doctor_checks._dr11()
        assert finding.severity is Severity.OK
        assert "systemd not detected" in finding.detail

    def test_dr11_linux_leg_unchanged_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Linux does NOT take the platform skip (prod behavior preserved)."""
        monkeypatch.setattr(sys, "platform", "linux")
        finding = doctor_checks._dr11()
        assert "systemd not detected on this platform" not in finding.detail


class TestInstallServicePlatformNotice:
    def test_refuses_before_writing_units(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.setenv("HOME", str(tmp_path))
        console = Console(record=True, width=120)
        with pytest.raises(typer.Exit) as excinfo:
            updates_cli._install_timer_on_host(console)
        assert excinfo.value.exit_code == 1
        assert "not supported on" in console.export_text()
        assert not (tmp_path / ".config" / "systemd" / "user").exists(), (
            "unit files must NOT be written before the platform refusal"
        )

    def test_linux_without_systemctl_keeps_write_then_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Linux host without systemctl keeps the historical behavior."""
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.setattr(updates_cli, "_systemctl", lambda *a: None)
        monkeypatch.setenv("HOME", str(tmp_path))
        console = Console(record=True, width=120)
        updates_cli._install_timer_on_host(console)  # warns, does NOT refuse
        output = console.export_text()
        assert "not supported on" not in output
        assert "systemctl --user daemon-reload" in output
        assert (tmp_path / ".config" / "systemd" / "user" / updates_cli.TIMER_UNIT).exists()
