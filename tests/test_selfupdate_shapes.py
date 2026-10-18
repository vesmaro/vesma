"""Update-apply install-shape matrix (wave 61 selfupdate slice, 2026-10-18).

The apply-shape contract, covered end to end:

* DETECTION: owned-venv (ownership marker OR the canonical engine venv
  path), foreign-venv, tool-manager (pipx / ``uv tool``), user-site;
* APPLY: an owned venv upgrades IN PLACE (no ``--user`` — the 6.2.0
  field incident: ``pip --user`` inside any venv fails with "User
  site-packages are not visible in this virtualenv") and refreshes the
  marker; a foreign venv and a tool-manager environment are refused
  with zero writes and zero history; user-site keeps ``pip --user``;
* LEGACY-DIST CLEANUP: after a successful apply the pre-rebrand dist
  names are uninstalled from the SAME environment — install-new BEFORE
  uninstall-old (a legacy uninstall removes the SHARED console script
  per its own RECORD), failures WARN + record without failing the
  apply, and the shared script is self-checked and repaired;
* VERSION DETECTION prefers the canonical ``vesma`` name over legacy
  aliases (updates.py level — pinned in test_updates.py).

No test touches the real ``~/.local/share/vesma``: HOME/XDG_* are
monkeypatched to ``tmp_path``, the interpreter identity and the
subprocess seam are faked except in the marked real-pip tests, which
run a genuine pip inside a throwaway ``tmp_path`` venv.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

import vesma.cli.update_cmd as uc
import vesma.updates as updates

runner = CliRunner()
INSTALLED = "6.2.0"
LATEST = "6.3.0"


# ── helpers ───────────────────────────────────────────────────────────────────


def _info(update_available: bool = True) -> updates.UpdateInfo:
    return updates.UpdateInfo(
        installed=INSTALLED,
        latest=LATEST,
        dist="vesma",
        update_available=update_available,
        checked_at="2026-10-18T00:00:00+00:00",
    )


def _invoke(args: list[str]) -> CliRunner.Result:
    from vesma.cli.main import app

    return runner.invoke(app, ["self-update", *args])


def _flat(result: CliRunner.Result) -> str:
    """Whitespace-normalized output — rich word-wraps the long lines."""
    return " ".join(result.output.split())


def _fake_interpreter(monkeypatch: pytest.MonkeyPatch, prefix: Path) -> None:
    """Pretend the running interpreter lives in ``prefix`` (a venv)."""
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(sys, "executable", str(prefix / "bin" / "python"))


def _stub_pip(
    monkeypatch: pytest.MonkeyPatch,
    record: list[list[str]] | None = None,
    rc: int = 0,
) -> None:
    def fake_run(cmd: list[str], timeout: int = 900) -> subprocess.CompletedProcess[str]:
        if record is not None:
            record.append(list(cmd))
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")

    monkeypatch.setattr(uc, "_run_cmd", fake_run)


def _owned_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """An owned tmp venv (marker branch) wired into the shape seam."""
    prefix = tmp_path / "share" / "vesma" / "venv"
    prefix.mkdir(parents=True)
    marker = prefix / uc._OWNED_VENV_MARKER
    marker.write_text("installer=install.sh version=6.1.0\n", encoding="utf-8")
    monkeypatch.setattr(
        uc, "_install_shape", lambda: uc.InstallShape(uc.OWNED_VENV, prefix, marker)
    )
    return prefix, marker


@pytest.fixture
def shape_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HOME; the apply path default here is USER-SITE."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(uc, "_prod_venv_bases", lambda: (tmp_path / "nonexistent",))
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(uc, "_install_shape", lambda: uc.InstallShape(uc.USER_SITE))
    return tmp_path


# ── detection matrix ──────────────────────────────────────────────────────────


def test_shape_owned_by_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The installer's ``.vesma-venv`` marker is the primary ownership proof."""
    prefix = tmp_path / "venv"
    prefix.mkdir()
    (prefix / uc._OWNED_VENV_MARKER).write_text("installer=install.sh version=6.2.0\n")
    _fake_interpreter(monkeypatch, prefix)
    shape = uc._install_shape()
    assert shape.kind == uc.OWNED_VENV
    assert shape.venv_root == prefix
    assert shape.marker_path == prefix / uc._OWNED_VENV_MARKER


def test_shape_owned_by_canonical_layout_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No marker — the canonical engine venv path still reads as owned.

    The 6.2.0 field shape: a service-installed engine venv predates the
    marker; the layout-contract path is the ownership proof there.
    """
    share = tmp_path / "share"
    venv_root = share / "vesma" / "venv"
    venv_root.mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(share))
    _fake_interpreter(monkeypatch, venv_root)
    shape = uc._install_shape()
    assert shape.kind == uc.OWNED_VENV
    assert shape.marker_path == venv_root / uc._OWNED_VENV_MARKER


def test_shape_foreign_venv_without_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A venv with no marker and no canonical claim is FOREIGN (apply refuses)."""
    prefix = tmp_path / "some-foreign-venv"
    prefix.mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "elsewhere"))
    _fake_interpreter(monkeypatch, prefix)
    shape = uc._install_shape()
    assert shape.kind == uc.FOREIGN_VENV
    assert shape.marker_path == prefix / uc._OWNED_VENV_MARKER


def test_shape_tool_manager_pipx_priority_over_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a stray marker must not authorize self-writes into pipx's venv."""
    prefix = tmp_path / "pipx" / "venvs" / "vesma"
    prefix.mkdir(parents=True)
    (prefix / uc._OWNED_VENV_MARKER).write_text("installer=stray\n")
    _fake_interpreter(monkeypatch, prefix)
    assert uc._install_shape().kind == uc.TOOL_MANAGER


def test_shape_tool_manager_uv_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``uv tool`` venvs (``/uv/tools/`` segment) are manager-owned too."""
    prefix = tmp_path / "uv" / "tools" / "vesma"
    prefix.mkdir(parents=True)
    _fake_interpreter(monkeypatch, prefix)
    assert uc._install_shape().kind == uc.TOOL_MANAGER


def test_shape_tool_manager_detected_via_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrapper on PATH inside a manager tree is signal enough."""
    prefix = tmp_path / "plain-venv"
    prefix.mkdir(parents=True)
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "pipx" / "bin" / "vesma"))
    assert uc._install_shape().kind == uc.TOOL_MANAGER


def test_shape_user_site_when_not_in_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    """No venv (``sys.prefix == base_prefix``) — the classic user-site case."""
    monkeypatch.setattr(sys, "prefix", sys.base_prefix)
    shape = uc._install_shape()
    assert shape.kind == uc.USER_SITE
    assert shape.venv_root is None


def test_legacy_installer_venv_hint_matches_mnemos_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-6.2 install.sh default (~/.mnemos/venv) earns a migration hint."""
    monkeypatch.setenv("HOME", str(tmp_path))
    legacy = tmp_path / ".mnemos" / "venv"
    legacy.mkdir(parents=True)
    assert uc._is_legacy_installer_venv(legacy)
    assert not uc._is_legacy_installer_venv(tmp_path / "other-venv")


# ── apply strategy ────────────────────────────────────────────────────────────


def test_apply_owned_venv_upgrades_in_place(
    shape_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owned venv: upgrade in THIS venv, no --user, marker refreshed."""
    _prefix, marker = _owned_shape(shape_home, monkeypatch)
    monkeypatch.setattr(uc, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    monkeypatch.setattr(uc, "fetch_latest", lambda dist: LATEST)
    monkeypatch.setattr(uc, "_pep668_externally_managed", lambda: False)
    record: list[list[str]] = []
    _stub_pip(monkeypatch, record=record)

    result = _invoke(["apply"])

    assert result.exit_code == 0, result.output
    assert record, "pip must have been invoked"
    cmd = record[0]
    assert cmd[0] == sys.executable
    assert "--user" not in cmd, "the 6.2.0 field bug: --user inside a venv fails"
    assert "--upgrade" in cmd
    assert "vesma" in cmd
    assert marker.read_text(encoding="utf-8").startswith(f"installer=self-update version={LATEST}")


def test_apply_user_site_keeps_user_flag(shape_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The classic bare-python install keeps the pip --user path."""
    monkeypatch.setattr(uc, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    monkeypatch.setattr(uc, "fetch_latest", lambda dist: LATEST)
    monkeypatch.setattr(uc, "_pep668_externally_managed", lambda: False)
    monkeypatch.setattr(uc, "_installed_legacy_dists", lambda: [])
    record: list[list[str]] = []
    _stub_pip(monkeypatch, record=record)

    result = _invoke(["apply"])

    assert result.exit_code == 0, result.output
    assert record[0][1:5] == ["-m", "pip", "install", "--user"]


def test_apply_foreign_venv_refuses_without_writes(
    shape_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Foreign venv: loud refusal, no pip run, no marker, no history."""
    prefix = shape_home / "bathys-venv"
    prefix.mkdir(parents=True)
    marker = prefix / uc._OWNED_VENV_MARKER
    monkeypatch.setattr(
        uc, "_install_shape", lambda: uc.InstallShape(uc.FOREIGN_VENV, prefix, marker)
    )
    monkeypatch.setattr(uc, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    monkeypatch.setattr(uc, "fetch_latest", lambda dist: LATEST)  # must never fire
    record: list[list[str]] = []
    _stub_pip(monkeypatch, record=record)

    result = _invoke(["apply"])

    assert result.exit_code == 1
    flat = _flat(result)
    assert "was not installed by vesma" in flat
    assert "no ownership marker at" in flat
    assert "canonical installer" in flat
    assert record == [], "a refused apply must not run pip"
    assert not marker.exists(), "a refusal must not write the marker"
    history = shape_home / ".local" / "share" / "vesma" / "update-history.json"
    assert not history.exists(), "a refusal must not append history"


def test_apply_foreign_legacy_mnemos_venv_names_migration(
    shape_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-6.2 default venv gets the one-time migration line."""
    legacy = shape_home / ".mnemos" / "venv"
    legacy.mkdir(parents=True)
    monkeypatch.setattr(
        uc,
        "_install_shape",
        lambda: uc.InstallShape(uc.FOREIGN_VENV, legacy, legacy / uc._OWNED_VENV_MARKER),
    )
    _stub_pip(monkeypatch)

    result = _invoke(["apply"])

    assert result.exit_code == 1
    assert "re-run scripts/install.sh" in _flat(result)


def test_apply_tool_manager_refuses_with_manager_hint(
    shape_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tool managers are never written by self-update — the manager updates."""
    monkeypatch.setattr(
        uc,
        "_install_shape",
        lambda: uc.InstallShape(uc.TOOL_MANAGER, Path("/x/pipx/venvs/vesma"), None),
    )
    record: list[list[str]] = []
    _stub_pip(monkeypatch, record=record)

    result = _invoke(["apply"])

    assert result.exit_code == 1
    flat = _flat(result)
    assert "pipx upgrade vesma" in flat
    assert "uv tool upgrade vesma" in flat
    assert record == []
    history = shape_home / ".local" / "share" / "vesma" / "update-history.json"
    assert not history.exists()


# ── check report, shape-aware ─────────────────────────────────────────────────


def test_check_tool_manager_note_points_at_manager(
    shape_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check note names the manager path, not a command that would exit 1."""
    monkeypatch.setattr(uc, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    monkeypatch.setattr(uc, "check_for_update", lambda settings=None, **kw: _info())
    monkeypatch.setattr(uc, "family_latest", lambda: LATEST)
    monkeypatch.setattr(
        uc,
        "_install_shape",
        lambda: uc.InstallShape(uc.TOOL_MANAGER, Path("/x/pipx/venvs/vesma"), None),
    )

    result = _invoke(["check"])

    assert result.exit_code == 0
    flat = _flat(result)
    assert "pipx upgrade vesma" in flat
    assert "UPDATE AVAILABLE" not in flat, "the apply advice must not appear here"


def test_check_foreign_venv_note_names_the_installer(
    shape_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(uc, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    monkeypatch.setattr(uc, "check_for_update", lambda settings=None, **kw: _info())
    monkeypatch.setattr(uc, "family_latest", lambda: LATEST)
    monkeypatch.setattr(
        uc,
        "_install_shape",
        lambda: uc.InstallShape(uc.FOREIGN_VENV, Path("/x/foreign-venv"), None),
    )

    result = _invoke(["check"])

    assert result.exit_code == 0
    flat = _flat(result)
    assert "foreign environment" in flat
    assert "UPDATE AVAILABLE" not in flat


# ── legacy-dist cleanup ───────────────────────────────────────────────────────


def test_apply_cleans_legacy_after_install_in_order(
    shape_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Install new BEFORE uninstall old — the live-verified order discipline."""
    _owned_shape(shape_home, monkeypatch)
    monkeypatch.setattr(uc, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    monkeypatch.setattr(uc, "fetch_latest", lambda dist: LATEST)
    monkeypatch.setattr(uc, "_pep668_externally_managed", lambda: False)
    monkeypatch.setattr(uc, "_installed_legacy_dists", lambda: [("vesma-memory-server", "5.6.4")])
    monkeypatch.setattr(uc, "_owned_script_paths", lambda shape: [])
    record: list[list[str]] = []
    _stub_pip(monkeypatch, record=record)

    result = _invoke(["apply"])

    assert result.exit_code == 0, result.output
    installs = [c for c in record if "install" in c]
    uninstalls = [c for c in record if "uninstall" in c]
    assert len(installs) == 1 and len(uninstalls) == 1
    assert record.index(installs[0]) < record.index(uninstalls[0]), (
        "uninstall-old first could leave the environment without the entrypoint"
    )
    uninstall_cmd = uninstalls[0]
    assert uninstall_cmd[1:5] == ["-m", "pip", "uninstall", "-y"]
    assert "--user" not in uninstall_cmd
    assert "vesma-memory-server" in uninstall_cmd
    assert "legacy dist removed: vesma-memory-server 5.6.4" in _flat(result)


def test_apply_cleanup_failure_warns_and_records(
    shape_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed legacy uninstall never fails the apply: WARN + history row."""
    _owned_shape(shape_home, monkeypatch)
    monkeypatch.setattr(uc, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    monkeypatch.setattr(uc, "fetch_latest", lambda dist: LATEST)
    monkeypatch.setattr(uc, "_pep668_externally_managed", lambda: False)
    monkeypatch.setattr(uc, "_installed_legacy_dists", lambda: [("mnemos-memory-server", "5.4.0")])
    monkeypatch.setattr(uc, "_owned_script_paths", lambda shape: [])

    def fake_run(cmd: list[str], timeout: int = 900) -> subprocess.CompletedProcess[str]:
        rc = 1 if "uninstall" in cmd else 0
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="boom")

    monkeypatch.setattr(uc, "_run_cmd", fake_run)

    result = _invoke(["apply"])

    assert result.exit_code == 0, "the new vesma is installed — cleanup failure is a WARN"
    assert "could not remove legacy dist mnemos-memory-server" in _flat(result)
    history = json.loads(
        (shape_home / ".local" / "share" / "vesma" / "update-history.json").read_text()
    )
    assert history[0]["dist"] == "vesma" and history[0]["rc"] == 0
    assert history[1]["dist"] == "mnemos-memory-server" and history[1]["rc"] == 1


def test_apply_without_legacy_dists_runs_no_uninstall(
    shape_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owned_shape(shape_home, monkeypatch)
    monkeypatch.setattr(uc, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    monkeypatch.setattr(uc, "fetch_latest", lambda dist: LATEST)
    monkeypatch.setattr(uc, "_pep668_externally_managed", lambda: False)
    monkeypatch.setattr(uc, "_installed_legacy_dists", lambda: [])
    record: list[list[str]] = []
    _stub_pip(monkeypatch, record=record)

    result = _invoke(["apply"])

    assert result.exit_code == 0, result.output
    assert not any("uninstall" in cmd for cmd in record)


def test_cleanup_spares_the_dist_apply_upgraded(
    shape_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alias-only install: the upgraded legacy channel must survive cleanup."""
    monkeypatch.setattr(uc, "detect_installed_dist", lambda: ("vesma-memory-server", "6.1.0"))
    monkeypatch.setattr(uc, "fetch_latest", lambda dist: "6.2.0")
    monkeypatch.setattr(uc, "_pep668_externally_managed", lambda: False)
    monkeypatch.setattr(
        uc,
        "_installed_legacy_dists",
        lambda: [("vesma-memory-server", "6.2.0"), ("mnemos-memory-server", "5.0.0")],
    )
    monkeypatch.setattr(uc, "_owned_script_paths", lambda shape: [])
    record: list[list[str]] = []
    _stub_pip(monkeypatch, record=record)

    result = _invoke(["apply"])

    assert result.exit_code == 0, result.output
    uninstalls = [cmd for cmd in record if "uninstall" in cmd]
    assert len(uninstalls) == 1
    assert "mnemos-memory-server" in uninstalls[0]
    assert not any("vesma-memory-server" in " ".join(cmd) for cmd in uninstalls), (
        "the dist apply just upgraded must never be removed by the cleanup"
    )


def test_apply_repairs_shared_script_after_legacy_removal(
    shape_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared console script the legacy uninstall took is repaired."""
    prefix, _marker = _owned_shape(shape_home, monkeypatch)
    script = prefix / "bin" / "vesma"
    train_script = prefix / "bin" / "vesma-train"
    monkeypatch.setattr(uc, "_owned_script_paths", lambda shape: [script, train_script])
    monkeypatch.setattr(uc, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    monkeypatch.setattr(uc, "fetch_latest", lambda dist: LATEST)
    monkeypatch.setattr(uc, "_pep668_externally_managed", lambda: False)
    monkeypatch.setattr(uc, "_installed_legacy_dists", lambda: [("vesma-memory-server", "5.6.4")])
    (prefix / "bin").mkdir(parents=True)
    train_script.write_text("#!/bin/sh\n")  # vesma itself was taken by the uninstall
    record: list[list[str]] = []

    def fake_run(cmd: list[str], timeout: int = 900) -> subprocess.CompletedProcess[str]:
        record.append(list(cmd))
        if "--force-reinstall" in cmd:
            script.write_text("#!/bin/sh\n")  # the repair restored the launcher
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(uc, "_run_cmd", fake_run)

    result = _invoke(["apply"])

    assert result.exit_code == 0, result.output
    repair = next(cmd for cmd in record if "--force-reinstall" in cmd)
    assert "--no-deps" in repair
    assert "--user" not in repair, "owned-venv repair writes into the venv only"
    assert f"vesma=={LATEST}" in repair
    assert "shared console script(s) restored: vesma" in _flat(result)


def test_apply_repair_failure_is_loud_but_does_not_refuse(
    shape_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed script repair prints the exact manual fix, apply still stands."""
    prefix, _marker = _owned_shape(shape_home, monkeypatch)
    script = prefix / "bin" / "vesma"
    monkeypatch.setattr(uc, "_owned_script_paths", lambda shape: [script])
    monkeypatch.setattr(uc, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    monkeypatch.setattr(uc, "fetch_latest", lambda dist: LATEST)
    monkeypatch.setattr(uc, "_pep668_externally_managed", lambda: False)
    monkeypatch.setattr(uc, "_installed_legacy_dists", lambda: [("vesma-memory-server", "5.6.4")])

    def fake_run(cmd: list[str], timeout: int = 900) -> subprocess.CompletedProcess[str]:
        rc = 1 if "--force-reinstall" in cmd else 0
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")

    monkeypatch.setattr(uc, "_run_cmd", fake_run)

    result = _invoke(["apply"])

    assert result.exit_code == 0, "the dist is installed — the repair leg cannot unapply it"
    flat = _flat(result)
    assert "automatic repair failed" in flat
    assert "restore them manually" in flat
    assert f"--force-reinstall --no-deps vesma=={LATEST}" in flat


def test_repair_uses_user_flag_on_user_site_shape(
    shape_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User-site repair reinstalls into the user site, not the system."""
    script = shape_home / ".local" / "bin" / "vesma"
    monkeypatch.setattr(uc, "_owned_script_paths", lambda shape: [script])
    monkeypatch.setattr(uc, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    monkeypatch.setattr(uc, "fetch_latest", lambda dist: LATEST)
    monkeypatch.setattr(uc, "_pep668_externally_managed", lambda: False)
    monkeypatch.setattr(uc, "_installed_legacy_dists", lambda: [("vesma-memory-server", "5.6.4")])
    record: list[list[str]] = []
    _stub_pip(monkeypatch, record=record)

    result = _invoke(["apply"])

    assert result.exit_code == 0, result.output
    repair = next(cmd for cmd in record if "--force-reinstall" in cmd)
    assert "--user" in repair


# ── cascade fix-slice (2026-10-18): F2 / F3 / P2-1 ────────────────────────────


def test_pin_spec_validator_matrix() -> None:
    """Cascade F2: the ``--to`` whitelist accepts real specs, blocks injection."""
    assert uc._valid_pin_spec("6.2.0")
    assert uc._valid_pin_spec("5.1.1rc1")
    assert uc._valid_pin_spec("2020.01.01")
    assert uc._valid_pin_spec("6.2.0+local")
    assert uc._valid_pin_spec("6.*")
    assert not uc._valid_pin_spec("6.2.0\ninstaller=evil")
    assert not uc._valid_pin_spec("6.2.0; rm -rf /")
    assert not uc._valid_pin_spec("6.2.0 drop")
    assert not uc._valid_pin_spec("a" * 65)


def test_apply_invalid_to_refuses_without_writes(
    shape_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hostile ``--to`` never reaches pip, the marker or the history."""
    _prefix, marker = _owned_shape(shape_home, monkeypatch)
    monkeypatch.setattr(uc, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    monkeypatch.setattr(uc, "fetch_latest", lambda dist: LATEST)  # must never fire
    record: list[list[str]] = []
    _stub_pip(monkeypatch, record=record)

    for bad in ("6.2.0\ninstaller=evil", "6.2.0; drop"):
        result = _invoke(["apply", "--to", bad])
        assert result.exit_code == 1
        assert "invalid --to" in _flat(result)

    assert record == [], "the validator must fire before any pip call"
    history = shape_home / ".local" / "share" / "vesma" / "update-history.json"
    assert not history.exists(), "the validator must fire before any history row"
    assert marker.read_text(encoding="utf-8") == "installer=install.sh version=6.1.0\n", (
        "the validator must fire before any marker write"
    )


def test_apply_history_is_private(shape_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cascade F3: history lands 0700-dir / 0600-file under a permissive umask."""
    _owned_shape(shape_home, monkeypatch)
    monkeypatch.setattr(uc, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    monkeypatch.setattr(uc, "fetch_latest", lambda dist: LATEST)
    monkeypatch.setattr(uc, "_pep668_externally_managed", lambda: False)
    monkeypatch.setattr(uc, "_installed_legacy_dists", lambda: [])
    _stub_pip(monkeypatch)

    old_umask = os.umask(0o000)  # widest possible — exactly the F3 failure mode
    try:
        result = _invoke(["apply"])
    finally:
        os.umask(old_umask)

    assert result.exit_code == 0, result.output
    vesma_root = shape_home / ".local" / "share" / "vesma"
    history = vesma_root / "update-history.json"
    assert (vesma_root.stat().st_mode & 0o777) == 0o700
    assert (history.stat().st_mode & 0o777) == 0o600


def test_pep668_cleanup_carries_break_system_packages(
    shape_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cascade P2-1: the uninstall AND the manual hint carry the PEP 668 flag."""
    _owned_shape(shape_home, monkeypatch)
    monkeypatch.setattr(uc, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    monkeypatch.setattr(uc, "fetch_latest", lambda dist: LATEST)
    monkeypatch.setattr(uc, "_pep668_externally_managed", lambda: True)
    monkeypatch.setattr(uc, "_installed_legacy_dists", lambda: [("mnemos-memory-server", "5.4.0")])
    monkeypatch.setattr(uc, "_owned_script_paths", lambda shape: [])
    record: list[list[str]] = []

    def fake_run(cmd: list[str], timeout: int = 900) -> subprocess.CompletedProcess[str]:
        record.append(list(cmd))
        rc = 1 if "uninstall" in cmd else 0  # failing cleanup → the hint path
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")

    monkeypatch.setattr(uc, "_run_cmd", fake_run)

    result = _invoke(["apply"])

    assert result.exit_code == 0, "the failed cleanup must not fail the apply"
    uninstall = next(cmd for cmd in record if "uninstall" in cmd)
    assert "--break-system-packages" in uninstall
    flat = _flat(result)
    assert "could not remove legacy dist mnemos-memory-server" in flat
    assert "pip uninstall mnemos-memory-server --break-system-packages" in flat, (
        "the printed manual command must not repeat the refusal"
    )


# ── real pip, real venv (honest end-to-end) ───────────────────────────────────


@pytest.mark.skipif(os.name != "posix", reason="venv bin/ layout is posix")
class TestRealPipEndToEnd:
    """A genuine pip inside a throwaway venv — the 6.2.0 field incident.

    All wheels are hand-crafted micro packages resolved OFFLINE through
    PIP_FIND_LINKS/PIP_NO_INDEX; the legacy dist and the real dist claim
    the SAME console script, so the cleanup genuinely walks the shared
    entrypoint collision (the prod 6.2.0 incident, fact (в)).
    """

    MODULE_BODY = "def main() -> None:\n    print('e2e-ok')\n"

    @pytest.fixture
    def real_venv(self, tmp_path: Path) -> Path:
        if importlib.util.find_spec("ensurepip") is None:
            pytest.skip("ensurepip unavailable — cannot build a real tmp venv")
        venv_dir = tmp_path / "target-venv"
        proc = subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert proc.returncode == 0, proc.stderr
        return venv_dir

    @staticmethod
    def _write_micro_wheel(
        wheelhouse: Path,
        name: str,
        version: str,
        *,
        entry: str | None = None,
        module: str | None = None,
    ) -> None:
        """Hand-craft a minimal pure wheel pip can install off find-links."""
        dist_info = f"{name.replace('-', '_')}-{version}.dist-info"
        payload: dict[str, str] = {
            f"{dist_info}/METADATA": (f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"),
            f"{dist_info}/WHEEL": (
                "Wheel-Version: 1.0\nGenerator: test-fixture\n"
                "Root-Is-Purelib: true\nTag: py3-none-any\n"
            ),
        }
        if entry is not None and module is not None:
            payload[f"{dist_info}/entry_points.txt"] = (
                f"[console_scripts]\n{entry} = {module}:main\n"
            )
            payload[f"{module}/__init__.py"] = TestRealPipEndToEnd.MODULE_BODY
        record_entries = [f"{arcname},," for arcname in payload]
        record_entries.append(f"{dist_info}/RECORD,,")
        payload[f"{dist_info}/RECORD"] = "\n".join(record_entries) + "\n"
        # PEP 427 filename: the distribution part is underscore-normalized —
        # a hyphenated filename does not parse and pip sees zero candidates.
        wheel = wheelhouse / f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            for arcname, data in payload.items():
                archive.writestr(arcname, data)

    def test_pip_user_flag_fails_inside_any_venv(self) -> None:
        """Negative control — the exact 6.2.0 field bug, reproduced live."""
        if not uc._in_venv():
            pytest.skip("the suite interpreter is not a venv — nothing to reproduce")
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--user",
                "--no-index",
                "vesma-e2e-negative-control",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode != 0
        assert "not visible in this virtualenv" in proc.stdout + proc.stderr

    def test_owned_venv_apply_upgrades_cleans_legacy_keeps_entrypoint(
        self, tmp_path: Path, real_venv: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """venv with legacy dist → apply → new stands, legacy gone, script alive."""
        venv_dir = real_venv
        wheelhouse = tmp_path / "wheelhouse"
        wheelhouse.mkdir()
        for version in ("0.1.0", "0.2.0"):
            self._write_micro_wheel(
                wheelhouse,
                "vesma-e2e-dummy",
                version,
                entry="vesma-e2e-dummy",
                module="vesma_e2e_dummy",
            )
        self._write_micro_wheel(
            wheelhouse,
            "vesma-e2e-legacy",
            "0.5.0",
            entry="vesma-e2e-dummy",  # the SHARED entrypoint — the incident
            module="vesma_e2e_legacy",
        )
        monkeypatch.setenv("PIP_NO_INDEX", "1")
        monkeypatch.setenv("PIP_FIND_LINKS", str(wheelhouse))
        monkeypatch.setenv("PIP_DISABLE_PIP_VERSION_CHECK", "1")
        monkeypatch.setenv("PIP_NO_INPUT", "1")
        monkeypatch.setenv("HOME", str(tmp_path))

        venv_py = venv_dir / "bin" / "python"

        def pip(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [str(venv_py), "-m", "pip", *args],
                capture_output=True,
                text=True,
                timeout=300,
            )

        def dist_version(name: str) -> str | None:
            query = subprocess.run(
                [
                    str(venv_py),
                    "-c",
                    f"import importlib.metadata as md; print(md.version('{name}'))",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            return query.stdout.strip() if query.returncode == 0 else None

        seed = pip("install", "vesma-e2e-dummy==0.1.0", "vesma-e2e-legacy==0.5.0")
        assert seed.returncode == 0, seed.stdout + seed.stderr
        assert dist_version("vesma-e2e-dummy") == "0.1.0"
        script = venv_dir / "bin" / "vesma-e2e-dummy"
        assert script.exists()

        marker = venv_dir / uc._OWNED_VENV_MARKER
        marker.write_text("installer=install.sh version=0.1.0\n", encoding="utf-8")
        monkeypatch.setattr(sys, "prefix", str(venv_dir))
        monkeypatch.setattr(sys, "executable", str(venv_py))
        monkeypatch.setattr(
            uc, "_install_shape", lambda: uc.InstallShape(uc.OWNED_VENV, venv_dir, marker)
        )
        monkeypatch.setattr(uc, "detect_installed_dist", lambda: ("vesma-e2e-dummy", "0.1.0"))
        monkeypatch.setattr(uc, "fetch_latest", lambda dist: "0.2.0")
        monkeypatch.setattr(uc, "_pep668_externally_managed", lambda: False)
        monkeypatch.setattr(uc, "_LEGACY_DISTS", ("vesma-e2e-legacy",))
        monkeypatch.setattr(uc, "_OWNED_SCRIPTS", ("vesma-e2e-dummy",))
        # The pre-filter reads THIS interpreter's metadata (the pytest
        # venv), which cannot see the tmp venv's truth — the seam states
        # what the real venv carries; the uninstall subprocess below is
        # genuinely real and genuinely removes it.
        monkeypatch.setattr(uc, "_installed_legacy_dists", lambda: [("vesma-e2e-legacy", "0.5.0")])
        monkeypatch.setattr("shutil.which", lambda name: None)  # no npm leg

        result = _invoke(["apply", "--to", "0.2.0"])

        assert result.exit_code == 0, result.output
        assert dist_version("vesma-e2e-dummy") == "0.2.0", "the new dist must stand"
        assert dist_version("vesma-e2e-legacy") is None, "the legacy dist must be removed"
        assert script.exists(), "the shared console script must survive the cleanup"
        run_script = subprocess.run([str(script)], capture_output=True, text=True, timeout=60)
        assert run_script.returncode == 0
        assert "e2e-ok" in run_script.stdout
        assert marker.read_text(encoding="utf-8").startswith("installer=self-update version=0.2.0")


# ── install.sh contract ───────────────────────────────────────────────────────


def test_install_sh_default_venv_is_canonical_and_writes_marker() -> None:
    """Default = the canonical isolated venv; ownership markers are written."""
    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "install.sh"
    text = script.read_text(encoding="utf-8")
    assert "${XDG_DATA_HOME:-${HOME}/.local/share}/vesma" in text
    assert 'VENV_PATH="${_VESMA_DATA_ROOT}/venv"' in text
    assert ".vesma-venv" in text, "venv installs must write the ownership marker"
    assert ".user-site-install" in text, "--no-venv installs must write the user-site flag"
    assert "~/.mnemos/venv" not in text, "the legacy default must stay retired"
    syntax = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr


def test_install_sh_help_covers_the_whole_flag_block() -> None:
    """Cascade P2-2: --help anchors on the header block, never a line number."""
    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "install.sh"
    proc = subprocess.run(
        ["bash", str(script), "--help"], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    help_text = proc.stdout
    assert "Show this help" in help_text, "the help must reach the LAST flag lines"
    assert "--container" in help_text
    assert "--wire-agents" in help_text
