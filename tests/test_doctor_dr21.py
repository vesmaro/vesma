"""DR-21 (card 4c8a0cbe): the «system daemon serves the FHS data root»
doctor check — the 2026-10-28 false-green class.

The incident: `system install` produced a unit WITHOUT the config
wiring, the daemon resolved ZERO-CONFIG and silently auto-created an
EMPTY store under the service account's ``$HOME/.vesma`` while
``health`` read ok. Pins: the n/a leg (no system install), the OK leg
(wired unit + conffile naming the data root), the false-green FAIL (a
pre-fix unit resolves the daemon to the account-home auto-store), the
live-store corroboration, the no-conffile zero-config FAIL, and the
registry entry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from vesma.service import doctor_checks, fhs
from vesma.service import system_install as si
from vesma.service.doctor_checks import Severity, run_service_checks
from vesma.service.system_install import install_system

# ── Seams (dest-dir sim; the host's real state must not decide) ───────


def _fake_materialize(
    venv_dir: Path, requirements: tuple[str, ...], mode: int, **_kwargs: object
) -> dict[str, str]:
    version = next(line.split("==", 1)[1] for line in requirements if line.startswith("vesma=="))
    (venv_dir / "bin").mkdir(parents=True)
    vesma_bin = venv_dir / "bin" / "vesma"
    vesma_bin.write_text(f"#!/bin/sh\necho vesma {version}\n", encoding="utf-8")
    vesma_bin.chmod(0o755)
    python_bin = venv_dir / "bin" / "python"
    python_bin.write_text("#!/bin/sh\necho 3.13.1\n", encoding="utf-8")
    python_bin.chmod(0o755)
    return {"vesma": version}


@pytest.fixture(autouse=True)
def _stub_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(si, "_apply_sysusers", lambda fragment: "ok (stub)")
    monkeypatch.setattr(si, "_systemctl_system", lambda args: "ok (stub)")
    monkeypatch.setattr(si, "_materialize_venv", _fake_materialize)


class _FakeAccount:
    """pwd entry stand-in: the vesma account with the sysusers home."""

    def __init__(self, pw_dir: str) -> None:
        self.pw_dir = pw_dir


def _fake_getpwnam(data_root: Path) -> Any:
    def lookup(name: str) -> _FakeAccount:
        if name == "vesma":
            return _FakeAccount(str(data_root))
        raise KeyError(name)

    return lookup


@pytest.fixture
def sim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Fresh system install in a temp prefix; the fake vesma account's
    home is the data root (the sysusers.d fragment design)."""
    prefix = tmp_path / "deploy"
    for segment in (
        "etc",
        "var/lib",
        "var/cache",
        "var/log",
        "usr/local/lib",
        "usr/local/share",
        "run",
    ):
        (prefix / segment).mkdir(parents=True)
    monkeypatch.setattr(fhs, "default_prefix", lambda: prefix)
    home = tmp_path / "operator-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    install_system()
    data_root = prefix / "var/lib/vesma"
    monkeypatch.setattr(doctor_checks.pwd, "getpwnam", _fake_getpwnam(data_root))
    return {
        "prefix": prefix,
        "data_root": data_root,
        "config": prefix / "etc/vesma/config.yaml",
        "unit": prefix / "etc/systemd/system/vesma.service",
    }


def _conffile(config: Path, data_root: Path) -> None:
    """The operator-provisioned conffile naming the FHS data root."""
    config.write_text(f"vesma:\n  data_dir: {data_root}\n  db_name: vesma.db\n", encoding="utf-8")


def _strip_wiring(unit_path: Path) -> None:
    """Rewrite the unit into the 6.6.0-era text: no VESMA_CONFIG, no
    WorkingDirectory (the pre-card shape that caused the incident)."""
    stripped = "\n".join(
        line
        for line in unit_path.read_text(encoding="utf-8").splitlines()
        if not line.startswith(('Environment="VESMA_CONFIG=', "WorkingDirectory="))
    )
    unit_path.write_text(stripped + "\n", encoding="utf-8")


def _dr21() -> doctor_checks.Finding:
    finding = doctor_checks._dr21()
    assert finding.check_id == "DR-21"
    return finding


# ── The verdict matrix ────────────────────────────────────────────────


def test_no_system_install_is_na(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prefix = tmp_path / "empty"
    prefix.mkdir()
    monkeypatch.setattr(fhs, "default_prefix", lambda: prefix)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    assert _dr21().severity is Severity.OK


def test_wired_unit_and_conffile_is_ok(sim: dict[str, Path]) -> None:
    _conffile(sim["config"], sim["data_root"])
    finding = _dr21()
    assert finding.severity is Severity.OK
    assert "VESMA_CONFIG" in (finding.detail or "")
    assert str(sim["data_root"]) in (finding.detail or "")


def test_prefix_unit_is_the_false_green_fail(sim: dict[str, Path]) -> None:
    """The 28.10 shape: conffile provisioned, but the installed unit is
    the pre-fix text WITHOUT the wiring — the daemon resolves zero-config
    to the account-home auto-store while health would read ok."""
    _conffile(sim["config"], sim["data_root"])
    _strip_wiring(sim["unit"])
    finding = _dr21()
    assert finding.severity is Severity.FAIL
    assert "NOT the FHS data root" in (finding.detail or "")
    # The resolution names the $HOME/.vesma fork inside the data root.
    assert str(sim["data_root"] / ".vesma" / "data") in (finding.detail or "")
    assert finding.fix_command is not None
    assert "system install" in (finding.fix_command or "")


def test_false_green_evidence_names_the_materialized_db(sim: dict[str, Path]) -> None:
    """A live daemon's wrong store is corroborated by the auto-created
    vesma.db actually present under the wrongly-resolved home."""
    _conffile(sim["config"], sim["data_root"])
    _strip_wiring(sim["unit"])
    auto_store = sim["data_root"] / ".vesma" / "data"
    auto_store.mkdir(parents=True)
    (auto_store / "vesma.db").write_bytes(b"SQLite format 3")
    finding = _dr21()
    assert finding.severity is Severity.FAIL
    assert "EMPTY-auto-store false-green is live" in (finding.detail or "")


def test_missing_conffile_fails_with_zero_config_detail(sim: dict[str, Path]) -> None:
    """A wired unit WITHOUT any conffile still resolves zero-config —
    the daemon would fork the auto-store at next start. Loud, not n/a."""
    finding = _dr21()
    assert finding.severity is Severity.FAIL
    assert "zero-config default" in (finding.detail or "")


def test_foreign_data_dir_in_conffile_fails(sim: dict[str, Path]) -> None:
    """A conffile naming a foreign home (the carried-user-path class)
    resolves the daemon OUTSIDE the data root — FAIL with both paths."""
    foreign = sim["prefix"] / "home" / "someone"
    sim["config"].write_text(
        f"vesma:\n  data_dir: {foreign}\n  db_name: vesma.db\n", encoding="utf-8"
    )
    finding = _dr21()
    assert finding.severity is Severity.FAIL
    assert str(foreign) in (finding.detail or "")


def test_unresolvable_account_warns(sim: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the service account the mirror stops honestly (the daemon
    could not run at all; DR-15 owns the ownership drift)."""

    def lookup(name: str) -> _FakeAccount:
        raise KeyError(name)

    monkeypatch.setattr(doctor_checks.pwd, "getpwnam", lookup)
    finding = _dr21()
    assert finding.severity is Severity.WARN
    assert "not resolvable" in (finding.detail or "")


def test_check_is_in_the_registry(sim: dict[str, Path]) -> None:
    checks = {finding.check_id: finding for finding in run_service_checks()}
    assert "DR-21" in checks
    assert checks["DR-21"].title == "system daemon serves the FHS data root"
