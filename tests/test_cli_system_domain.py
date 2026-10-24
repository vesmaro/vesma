"""CLI-topology wave (variant B, owner-ratified 2026-10-23) — `vesma system`.

Pins the system-profile domain wiring after the 7.0.0 alias removal
(owner doctrine 2026-10-24 — ONE spelling per operation):

* ``vesma system install|uninstall|update`` — each verb calls its impl
  seam (seam-level pin: monkeypatch the impl function in the CLI module,
  no filesystem, no root);
* the domain exposes EXACTLY the lifecycle verbs + the ``config``
  sub-domain (wave C) — ``doctor`` is NOT registered here anymore (the
  ONE doctor is the L1 ``vesma doctor``);
* the former ``service <verb> --system`` hidden flags are GONE (usage
  errors); bare ``vesma service update`` keeps its refusal naming
  ``vesma system update`` (guidance, not an alias).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from vesma.cli import service as service_cli
from vesma.cli import system_cmd as system_cli
from vesma.cli.main import app
from vesma.service import system_install as si
from vesma.service import system_update as su

runner = CliRunner()


def _fake_install_system(called: list[bool]) -> Any:
    def fake() -> si.SystemInstallResult:
        called.append(True)
        return si.SystemInstallResult(
            lines=("line",),
            state_path=Path("/tmp/state.yaml"),
            unit_path=Path("/tmp/vesma.service"),
            engine_venv=Path("/tmp/venv"),
            revisions=(),
        )

    return fake


def _fake_uninstall_system(called: list[bool]) -> Any:
    def fake(*, dry_run: bool = False) -> si.SystemUninstallResult:
        called.append(dry_run)
        return si.SystemUninstallResult(lines=("removed: x",))

    return fake


def _fake_update_system(called: list[bool]) -> Any:
    def fake() -> su.SystemUpdateResult:
        called.append(True)
        return su.SystemUpdateResult(
            lines=("health: ok",),
            engine_venv=Path("/tmp/venv"),
            revisions=(),
            rolled_back=False,
        )

    return fake


# ── registration + topology ──────────────────────────────────────────────────


class TestRegistration:
    def test_system_group_is_registered(self) -> None:
        registered = {getattr(g, "name", None) for g in app.registered_groups}
        assert "system" in registered

    def test_system_exposes_exactly_the_lifecycle_verbs_plus_config(self) -> None:
        """Topology FINAL (7.0.0): the three lifecycle verbs + the
        `config` sub-domain (wave C, the ratified contract table); the
        `doctor` twin registration is REMOVED (the L1 `vesma doctor` is
        the one doctor). Removal-safety: any new verb must be a conscious
        contract change, not drift."""
        click_root = get_command(app)
        system = click_root.commands["system"]
        assert set(system.commands) == {"install", "uninstall", "update", "config"}
        for verb_cmd in system.commands.values():
            assert not verb_cmd.hidden, "system verbs must be visible (tab-completable)"

    def test_service_system_flags_are_gone(self) -> None:
        """The former hidden ``--system`` alias flags left the surface —
        the system profile is its own domain (usage errors, not hints)."""
        click_root = get_command(app)
        service = click_root.commands["service"]
        for verb in ("install", "uninstall", "update"):
            options = {p.name: p for p in service.commands[verb].params}
            assert "system" not in options, f"service {verb} lost the --system alias"
            result = runner.invoke(service_cli.service_app, [verb, "--system"])
            assert result.exit_code == 2, (verb, result.output)


# ── the domain: seam-level dispatch pins ─────────────────────────────────────


class TestSystemVerbsDispatch:
    def test_install_dispatches_to_impl_seam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called: list[bool] = []
        monkeypatch.setattr(system_cli, "install_system", _fake_install_system(called))
        result = runner.invoke(system_cli.system_app, ["install"])
        assert result.exit_code == 0, result.output
        assert called == [True]
        assert "Service installed" in result.output

    def test_uninstall_dispatches_to_impl_seam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Wave B (G4): the typed confirmation — `--yes` bypasses the prompt
        and dispatches; the impl seam sees a plain (non-dry-run) run."""
        called: list[bool] = []
        monkeypatch.setattr(system_cli, "uninstall_system", _fake_uninstall_system(called))
        result = runner.invoke(system_cli.system_app, ["uninstall", "--yes"])
        assert result.exit_code == 0, result.output
        assert called == [False]
        assert "Uninstall complete" in result.output

    def test_update_dispatches_to_impl_seam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called: list[bool] = []
        monkeypatch.setattr(system_cli, "update_system", _fake_update_system(called))
        result = runner.invoke(system_cli.system_app, ["update"])
        assert result.exit_code == 0, result.output
        assert called == [True]
        assert "System profile updated" in result.output

    def test_install_failure_exits_1_with_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Failure mode: the impl's typed error renders and exits 1."""

        def broken() -> si.SystemInstallResult:
            raise si.SystemInstallError("no system install-state — run as root")

        monkeypatch.setattr(system_cli, "install_system", broken)
        result = runner.invoke(system_cli.system_app, ["install"])
        assert result.exit_code == 1
        assert "no system install-state" in result.output

    def test_system_group_help_mentions_both_neighbors(self) -> None:
        """Topology help: the system domain names `vesma service` + `self-update`."""
        result = runner.invoke(system_cli.system_app, ["--help"])
        assert result.exit_code == 0
        assert "vesma service" in result.output
        assert "vesma self-update" in result.output


# ── the guidance surface: bare `service update` ──────────────────────────────


class TestServiceUpdateGuidance:
    def test_plain_service_update_refuses_naming_system_domain(self) -> None:
        """The refusal is unconditional (guidance, not an alias — there is
        no dispatch behind it to guard anymore)."""
        result = runner.invoke(service_cli.service_app, ["update"])
        assert result.exit_code == 1
        # Rich wraps the console line — match on whitespace-normalized text.
        flat = " ".join(result.output.split())
        assert "vesma system update" in flat, "hint names the canonical spelling"
        assert "self-update" in flat
        assert "add --system" not in flat, "old wording must be gone"
