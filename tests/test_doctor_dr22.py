"""DR-22 (card 0f75698d, ArchCom 2026-10-29): «loaded components vs
canonical manifests dir» — the invisible-desync class.

The incident: the system supervisor resolved manifests from the service
account's ``$HOME`` plane and silently ran the BUNDLE fallback while the
installer wrote operator manifests to ``/etc/vesma/components.d`` —
board/metrics looked alive, mesh was NOT loaded. Pins: the n/a leg
(supervisor down), the bundle-fallback FAIL signature, the loaded==disk
OK leg, the mismatch WARN, the parked-workaround WARN, the unreadable
canonical dir FAIL, and the registry entry.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path

import pytest

from vesma.service import doctor_checks, fhs, layout
from vesma.service.doctor_checks import Severity

# ── Seams ─────────────────────────────────────────────────────────────


@pytest.fixture
def user_plane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A plain user machine: isolated HOME, no system stamp, XDG runtime."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    prefix = tmp_path / "empty-prefix"
    prefix.mkdir()
    monkeypatch.setattr(fhs, "default_prefix", lambda: prefix)
    return {"home": home, "tmp": tmp_path}


def _stamp_system(prefix: Path) -> None:
    state = prefix / "var/lib/vesma/install-state.yaml"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        "# vesma system install-state (§3.13)\n"
        'updated: "2026-10-29T00:00:00+00:00"\n'
        "profile: system\n"
        "artifacts: []\n",
        encoding="utf-8",
    )


@pytest.fixture
def system_plane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A system-profile machine: stamp + unit under an FHS test prefix,
    isolated HOME (the service account's $HOME is the data root, not the
    doctor's)."""
    prefix = tmp_path / "fhs"
    prefix.mkdir()
    monkeypatch.setattr(fhs, "default_prefix", lambda: prefix)
    home = tmp_path / "doctor-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    _stamp_system(prefix)
    unit = prefix / "etc/systemd/system/vesma.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Unit]\nDescription=vesma\n", encoding="utf-8")
    return {
        "prefix": prefix,
        "canonical": prefix / "etc/vesma/components.d",
        "data_root": prefix / "var/lib/vesma",
    }


def _manifests(canonical: Path, names: tuple[str, ...]) -> None:
    canonical.mkdir(parents=True, exist_ok=True)
    for name in names:
        (canonical / f"{name}.yaml").write_text(f"name: {name}\nlaunch: {name}\n", encoding="utf-8")


class _FakeSupervisor:
    """A minimal control-socket double: hello + status with canned names,
    strict request→reply discipline (one frame per request)."""

    def __init__(self, sock_path: Path, loaded: tuple[str, ...]) -> None:
        self._loaded = loaded
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(sock_path))
        self._server.listen(1)
        self._server.settimeout(5.0)
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        try:
            conn, _ = self._server.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(5.0)
            buf = b""
            for frame_id in (1, 2):
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    buf += chunk
                line, buf = buf.split(b"\n", 1)
                json.loads(line)  # validate the request frame, then answer
                if frame_id == 1:
                    result: dict[str, object] = {"protocol_version": 1}
                else:
                    result = {"components": {name: {"state": "running"} for name in self._loaded}}
                conn.sendall(
                    (json.dumps({"id": frame_id, "result": result}) + "\n").encode("utf-8")
                )
        self._server.close()

    def __enter__(self) -> _FakeSupervisor:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._thread.join(timeout=5.0)
        if self._server.fileno() != -1:
            self._server.close()


def _start_fake_supervisor(loaded: tuple[str, ...]) -> _FakeSupervisor:
    sock_path = layout.resolve_runtime_dir().path / "control.sock"
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    return _FakeSupervisor(sock_path, loaded)


def _dr22() -> doctor_checks.Finding:
    finding = doctor_checks._dr22()
    assert finding.check_id == "DR-22"
    return finding


# ── The verdict matrix ────────────────────────────────────────────────


def test_supervisor_down_is_status_not_error(user_plane: dict[str, Path]) -> None:
    canonical = user_plane["home"] / ".vesma" / "components.d"
    _manifests(canonical, ("board", "metrics"))
    finding = _dr22()
    assert finding.severity is Severity.OK
    assert "2 manifests" in (finding.detail or "")


def test_bundle_fallback_signature_fails(system_plane: dict[str, Path]) -> None:
    """The exact 0f75698d shape: bundle loaded, operator manifests parked
    in the canonical dir — the loss class, loudly FAIL."""
    _manifests(system_plane["canonical"], ("board", "metrics", "mesh"))
    with _start_fake_supervisor(("board", "metrics")):
        finding = _dr22()
    assert finding.severity is Severity.FAIL
    assert "BUNDLE fallback" in (finding.detail or "")
    assert "mesh" in (finding.detail or "")
    assert finding.fix_command is not None
    assert "restart" in (finding.fix_command or "")


def test_loaded_matches_canonical_is_ok(system_plane: dict[str, Path]) -> None:
    _manifests(system_plane["canonical"], ("board", "metrics", "mesh"))
    with _start_fake_supervisor(("board", "metrics", "mesh")):
        finding = _dr22()
    assert finding.severity is Severity.OK
    assert "matches" in (finding.detail or "")


def test_loaded_mismatch_warns(system_plane: dict[str, Path]) -> None:
    """An operator component loaded that the canonical dir does not hold —
    WARN with both lists (drift the operator must reconcile)."""
    _manifests(system_plane["canonical"], ("board", "metrics"))
    with _start_fake_supervisor(("board", "metrics", "mesh")):
        finding = _dr22()
    assert finding.severity is Severity.WARN
    assert "mesh" in (finding.detail or "")


def test_parked_workaround_dir_warns(system_plane: dict[str, Path]) -> None:
    """The pre-fix workaround shape: copies parked under the service
    account's home plane — flagged for removal once canonical loads."""
    _manifests(system_plane["canonical"], ("board", "metrics"))
    parked = system_plane["data_root"] / ".config" / "vesma" / "components.d"
    _manifests(parked, ("board", "metrics", "mesh"))
    with _start_fake_supervisor(("board", "metrics")):
        finding = _dr22()
    assert finding.severity is Severity.WARN
    assert "non-canonical" in (finding.detail or "")
    assert str(parked) in (finding.detail or "")


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses DAC")
def test_unreadable_canonical_dir_fails(system_plane: dict[str, Path]) -> None:
    """A canonical dir the doctor identity cannot read — the service may
    be blind to its own manifests (fail-closed, security-reviewed)."""
    _manifests(system_plane["canonical"], ("board", "metrics"))
    system_plane["canonical"].chmod(0o000)
    finding = _dr22()
    assert finding.severity is Severity.FAIL
    assert "not readable" in (finding.detail or "")


def test_missing_canonical_dir_warns(system_plane: dict[str, Path]) -> None:
    finding = _dr22()
    assert finding.severity is Severity.WARN
    assert "does not exist" in (finding.detail or "")


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses DAC")
def test_unprivileged_doctor_on_system_machine_is_ok(
    system_plane: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain user on a system machine resolves the user plane — the
    check says so and asks for a privileged re-run instead of lying."""
    state = system_plane["prefix"] / "var/lib/vesma/install-state.yaml"
    state.chmod(0o000)  # EACCES → the stamp degrades to None
    finding = _dr22()
    assert finding.severity is Severity.OK
    assert "privileged" in (finding.detail or "")


def test_check_is_in_the_registry(user_plane: dict[str, Path]) -> None:
    checks = {finding.check_id: finding for finding in doctor_checks.run_service_checks()}
    assert "DR-22" in checks
