"""DR-20 (ADR-0049 wave 0): the «second writer on the store home» doctor
check.

Pins: the verdict matrix (held+alive=OK designed state, held+no-
supervisor=FAIL hazard with holder attribution, free+alive=WARN broken
invariant, free+no-supervisor=OK), the advisory registry context, and the
check's presence in the doctor registry.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from vesma.service import doctor_checks
from vesma.service.doctor_checks import Severity, run_service_checks
from vesma.service.instance_registry import InstanceEntry, record_instance

_SRC_ROOT = str(Path(__file__).parent.parent / "src")

_HOLD_SCRIPT = (
    "import sys, time\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, %r)\n"
    "from vesma.service.home_lock import writer_lock\n"
    "with writer_lock(Path(%r), endpoint=%r):\n"
    "    print('held', flush=True)\n"
    "    time.sleep(60)\n"
)


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A controlled store home + hermetic registry state."""
    home = tmp_path / "store-home"
    home.mkdir(parents=True)
    monkeypatch.setattr(doctor_checks, "_user_store_home", lambda: home)
    monkeypatch.setattr(doctor_checks, "_supervisor_alive", lambda: False)
    return home


def _hold(home: Path, endpoint: str) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLD_SCRIPT % (_SRC_ROOT, str(home), endpoint)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "held"
    return proc


def _dr20() -> doctor_checks.Finding:
    finding = doctor_checks._dr20()
    assert finding.check_id == "DR-20"
    return finding


def test_free_and_no_supervisor_is_ok() -> None:
    finding = _dr20()
    assert finding.severity is Severity.OK


def test_free_but_supervisor_alive_warns() -> None:
    doctor_checks._supervisor_alive = lambda: True  # type: ignore[method-assign]
    finding = _dr20()
    assert finding.severity is Severity.WARN
    assert "WITHOUT the home writer lock" in finding.detail


def test_held_without_supervisor_fails_with_attribution() -> None:
    home = doctor_checks._user_store_home()
    proc = _hold(home, "foreign-supervisor")
    try:
        finding = _dr20()
    finally:
        proc.kill()
        proc.wait(timeout=10)
    assert finding.severity is Severity.FAIL
    assert f"pid {proc.pid}" in finding.detail
    assert "foreign-supervisor" in finding.detail
    assert finding.fix_command is not None


def test_held_with_alive_supervisor_is_the_designed_state() -> None:
    doctor_checks._supervisor_alive = lambda: True  # type: ignore[method-assign]
    home = doctor_checks._user_store_home()
    proc = _hold(home, "service control=... core=http://127.0.0.1:8787")
    try:
        finding = _dr20()
    finally:
        proc.kill()
        proc.wait(timeout=10)
    assert finding.severity is Severity.OK
    assert f"pid {proc.pid}" in finding.detail


def test_registry_row_joins_the_detail() -> None:
    home = doctor_checks._user_store_home()
    assert (
        record_instance(
            InstanceEntry(
                profile="user",
                home=str(home),
                endpoint="http://127.0.0.1:8787",
                unit="vesma.service",
                instance_id=None,
                updated_at="2026-10-28T00:00:00+00:00",
            )
        )
        is None
    )
    finding = _dr20()
    assert "registry: profile=user" in finding.detail
    assert "http://127.0.0.1:8787" in finding.detail


def test_check_is_in_the_registry() -> None:
    checks = {finding.check_id: finding for finding in run_service_checks()}
    assert "DR-20" in checks
    assert checks["DR-20"].title == "second writer on the store home"


def test_hold_releases_cleanly() -> None:
    """Sanity: the holder subprocess's lock is kernel-released at death —
    a follow-up DR-20 run sees the home free."""
    home = doctor_checks._user_store_home()
    proc = _hold(home, "transient")
    proc.kill()
    proc.wait(timeout=10)
    assert _dr20().severity is Severity.OK
