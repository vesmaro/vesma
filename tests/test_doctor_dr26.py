"""DR-26 (ADR-0052, ArchCom 2026-11-06): the MCP token injector's doctor
check — detection-gated (speaks only when a unit or process is seen),
availability-first (unit inactive WARN, healthz-down-while-active FAIL),
fail-closed credential legs (unreadable/weakened 0600 = FAIL) and the
rotation-invariant leg (token mtime older than 25h = WARN — the rotation
automation stopped; the period<TTL invariant from ADR-0052).

All probes run through the check's seams (probe/healthz/now — the
_dr25_default_settings precedent): the host's real state must never decide
a test outcome.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from vesma.service.doctor_checks import Severity, _dr26


def _exec_start(token: Path, secret: Path) -> str:
    """The systemd structured ExecStart property shape _dr26 must parse."""
    return (
        "{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 /opt/injector "
        f"--port 8789 --token-file {token} --secret-file {secret} ; "
        "ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; "
        "code=(null) ; status=0/USER }"
    )


class _FakeSystemd:
    """The probe seam: canned answers for the fixed argv set _dr26 uses."""

    def __init__(
        self,
        load_state: str = "loaded",
        active: str = "active",
        pgrep_hits: bool = False,
        exec_start: str | None = None,
    ) -> None:
        self.load_state = load_state
        self.active = active
        self.pgrep_hits = pgrep_hits
        self.exec_start = exec_start

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[bytes] | None:
        assert argv[0] in ("systemctl", "pgrep")
        if argv[0] == "pgrep":
            out = b"12345\n" if self.pgrep_hits else b""
            return subprocess.CompletedProcess(argv, 0 if self.pgrep_hits else 1, out, b"")
        if argv[2] == "is-active":
            return subprocess.CompletedProcess(argv, 0, self.active.encode(), b"")
        if argv[2] == "show":
            _property = argv[4]  # systemctl --user show -p <property> --value <unit>
            if _property == "LoadState":
                return subprocess.CompletedProcess(argv, 0, self.load_state.encode(), b"")
            if _property == "ExecStart":
                return subprocess.CompletedProcess(
                    argv, 0, (self.exec_start or "").encode(), b""
                )
        raise AssertionError(f"unexpected probe: {argv}")


def _ok_healthz(url: str, timeout: float) -> dict[str, Any]:
    return {"ok": True, "token_file_readable": True, "upstream_tcp_ok": True}


def _down_healthz(url: str, timeout: float) -> dict[str, Any]:
    return {"ok": True, "token_file_readable": True, "upstream_tcp_ok": False}


def _dead_healthz(url: str, timeout: float) -> dict[str, Any]:
    raise OSError("connection refused")


def _cred_files(tmp_path: Path) -> tuple[Path, Path, str]:
    token, secret = tmp_path / "token", tmp_path / "secret"
    token.write_bytes(b"core-token")
    secret.write_bytes(b"listener-secret")
    token.chmod(0o600)
    secret.chmod(0o600)
    return token, secret, _exec_start(token, secret)


def _finding(**kwargs: Any) -> Any:
    finding = _dr26(**kwargs)
    assert finding.check_id == "DR-26"
    return finding


def test_idle_when_no_unit_and_no_process() -> None:
    finding = _finding(probe=_FakeSystemd(load_state="not-found", pgrep_hits=False))
    assert finding.severity == Severity.OK
    assert "check idle" in finding.detail


def test_systemd_unavailable_degrades_to_status_line() -> None:
    finding = _finding(probe=lambda argv: None)
    assert finding.severity == Severity.OK
    assert "unavailable" in finding.detail


def test_healthy_deployment_is_ok(tmp_path: Path) -> None:
    _token, _secret, exec_start = _cred_files(tmp_path)
    finding = _finding(
        probe=_FakeSystemd(exec_start=exec_start), healthz=_ok_healthz, now=int(time.time())
    )
    assert finding.severity == Severity.OK
    assert "rotation invariant intact" in finding.detail


def test_inactive_unit_is_warn(tmp_path: Path) -> None:
    _token, _secret, exec_start = _cred_files(tmp_path)
    finding = _finding(
        probe=_FakeSystemd(active="failed", exec_start=exec_start),
        healthz=_dead_healthz,
        now=int(time.time()),
    )
    assert finding.severity == Severity.WARN
    assert "is failed" in finding.detail
    assert finding.fix_command is not None and "systemctl --user start" in finding.fix_command


def test_healthz_down_while_active_is_fail(tmp_path: Path) -> None:
    _token, _secret, exec_start = _cred_files(tmp_path)
    finding = _finding(
        probe=_FakeSystemd(exec_start=exec_start), healthz=_dead_healthz, now=int(time.time())
    )
    assert finding.severity == Severity.FAIL
    assert "availability SPOF leg" in finding.detail
    assert finding.fix_command is not None and "journalctl" in finding.fix_command


def test_healthz_core_down_is_warn(tmp_path: Path) -> None:
    _token, _secret, exec_start = _cred_files(tmp_path)
    finding = _finding(
        probe=_FakeSystemd(exec_start=exec_start), healthz=_down_healthz, now=int(time.time())
    )
    assert finding.severity == Severity.WARN
    assert "upstream_tcp_ok=false" in finding.detail


def test_weakened_token_mode_is_fail(tmp_path: Path) -> None:
    token, _secret, exec_start = _cred_files(tmp_path)
    token.chmod(0o644)
    finding = _finding(
        probe=_FakeSystemd(exec_start=exec_start), healthz=_ok_healthz, now=int(time.time())
    )
    assert finding.severity == Severity.FAIL
    assert "weakened mode" in finding.detail
    assert finding.fix_command is not None and "chmod 600" in finding.fix_command


def test_unreadable_secret_is_fail(tmp_path: Path) -> None:
    _token, secret, exec_start = _cred_files(tmp_path)
    secret.unlink()
    finding = _finding(
        probe=_FakeSystemd(exec_start=exec_start), healthz=_ok_healthz, now=int(time.time())
    )
    assert finding.severity == Severity.FAIL
    assert "secret file unreadable" in finding.detail


def test_stale_token_mtime_warns_rotation_stopped(tmp_path: Path) -> None:
    token, _secret, exec_start = _cred_files(tmp_path)
    stale = int(time.time()) - 26 * 3600  # one missed 2x/day cycle + slack
    import os

    os.utime(token, (stale, stale))
    finding = _finding(
        probe=_FakeSystemd(exec_start=exec_start), healthz=_ok_healthz, now=int(time.time())
    )
    assert finding.severity == Severity.WARN
    assert "rotation automation stopped" in finding.detail


def test_fresh_token_mtime_stays_ok(tmp_path: Path) -> None:
    _token, _secret, exec_start = _cred_files(tmp_path)
    finding = _finding(
        probe=_FakeSystemd(exec_start=exec_start), healthz=_ok_healthz, now=int(time.time())
    )
    assert "rotation automation stopped" not in finding.detail


def test_process_without_unit_is_warn() -> None:
    finding = _finding(probe=_FakeSystemd(load_state="not-found", pgrep_hits=True))
    assert finding.severity == Severity.WARN
    assert "lifecycle unmanaged" in finding.detail
