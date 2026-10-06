"""Doctor service-installation checks (layout v1 §3.10, DR-01…DR-13).

Every DR check is exercised with an INJECTED defect and asserts BOTH the
severity and the ready fix command (LY-14: a finding without a fix command
is a contract violation). The doctor itself is read-only — the
snapshot-mutation test walks the whole isolated installation before and
after a full run and requires byte-identity. DR-03 runs the REAL
clean-env subprocess (no mock): the actual interpreter must keep the user
site out of ``sys.path`` under ``PYTHONNOUSERSITE=1``.

No test talks to PyPI or the host systemd bus (same fake legs as
tests/test_service_install.py).
"""

from __future__ import annotations

import json
import os
import socket
import stat
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from vesmaro.service import doctor_checks, layout, unitgen
from vesmaro.service import install as install_mod
from vesmaro.service.doctor_checks import Finding, Severity, exit_code, run_service_checks

# journald probes are pointed at fixtures in every test that can reach them
# (deterministic: the real /etc/systemd/journald.conf must not decide tests).


@pytest.fixture
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate every XDG root + HOME into a tmp dir (empty vars = defaults).

    Container detection is pinned to False (the bare-host profile): the
    test host may itself be a distrobox/container, and the downgrade path
    is covered by dedicated tests that flip the detect explicitly.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(install_mod.unitgen, "container_detect", lambda *a, **k: False)
    for var in (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture(autouse=True)
def deterministic_host_probes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Pin host-context probes: no host systemd bus, no host journald conf."""
    monkeypatch.setattr(install_mod, "_daemon_reload", lambda: "ok (stubbed)")
    monkeypatch.setattr(
        install_mod, "_systemctl_user", lambda args: f"ok (stubbed: {' '.join(args)})"
    )
    absent = tmp_path / "absent-systemd"  # does not exist -> DR-11 n/a by default
    monkeypatch.setattr(doctor_checks, "_SYSTEMD_RUN_DIR", absent)
    monkeypatch.setattr(doctor_checks, "_JOURNALD_CONF", absent / "journald.conf")


@pytest.fixture(autouse=True)
def fake_venv_installer(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the venv/pip leg: fake venv tree + a full-freeze lock file."""
    created: list[str] = []
    from vesmaro import __version__

    def fake_install_component_venv(manifest: Any, report: list[str]) -> Path:
        name = manifest.name
        created.append(name)
        venv_dir = layout.component_venv_dir(name)
        venv_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(venv_dir, 0o700)
        bin_dir = venv_dir / "bin"
        bin_dir.mkdir(exist_ok=True)
        (bin_dir / "python").write_text("#!/bin/true\n", encoding="utf-8")
        site = venv_dir / "lib" / "python9.9" / "site-packages"
        site.mkdir(parents=True, exist_ok=True)
        pkg = site / "fakepkg"
        pkg.mkdir(exist_ok=True)  # reinstall-safe
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        lock_path = layout.data_dir(name) / "requirements-lock.txt"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(f"pip==99.0\nvesma=={__version__}\n", encoding="utf-8")
        report.append(f"venv: {name} -> {venv_dir} (lock: {lock_path})")
        return venv_dir

    monkeypatch.setattr(install_mod, "_install_component_venv", fake_install_component_venv)
    return created


@pytest.fixture
def installed(isolated_home: Path) -> Path:
    install_mod.install()
    return isolated_home


def _finding(findings: list[Finding], check_id: str) -> Finding:
    matches = [f for f in findings if f.check_id == check_id]
    assert len(matches) == 1, f"expected exactly one {check_id} finding, got {matches}"
    return matches[0]


def _write_manifest(components: Path, name: str, body: str) -> None:
    (components / f"{name}.yaml").write_text(body, encoding="utf-8")


def _child_manifest(name: str, extra_argv: str = "", health: str = "") -> str:
    return (
        "apiVersion: vesma.component/v1\n"
        f"kind: child-process\n"
        "metadata:\n"
        f"  name: {name}\n"
        "  version: 1.0.0\n"
        "  tier: optional\n"
        f"  description: {name}\n"
        "  provenance:\n"
        f"    repo: https://example.com/{name}\n"
        "    license: MIT\n"
        "launch:\n"
        "  python:\n"
        "    requirements:\n"
        '      - "vesma=={engine_version}"\n'
        "  argv:\n"
        '    - "{venv_bin}/python"\n'
        "    - -m\n"
        f"    - {name}\n"
        f"{extra_argv}"
        "stop:\n"
        "  signal: SIGTERM\n"
        "  grace_period: 5s\n"
        f"{health}"
    )


# ── DR-01: rights/ownership of config/state/env roots ────────────────


class TestDR01:
    def test_all_absent_is_ok(self, isolated_home: Path) -> None:
        finding = _finding(run_service_checks(), "DR-01")
        assert finding.severity is Severity.OK

    def test_canonical_after_install(self, installed: Path) -> None:
        finding = _finding(run_service_checks(), "DR-01")
        assert finding.severity is Severity.OK

    def test_env_file_not_0600_fails_with_fix(self, installed: Path) -> None:
        env_file = layout.env_file_path("metrics")
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text("METRICS_TOKEN=x\n", encoding="utf-8")
        os.chmod(env_file, 0o644)
        finding = _finding(run_service_checks(), "DR-01")
        assert finding.severity is Severity.FAIL  # fail-closed loader class
        assert finding.fix_command is not None
        assert f"chmod 600 {env_file}" in finding.fix_command

    def test_weakened_dir_fails_with_fix(self, installed: Path) -> None:
        os.chmod(layout.components_dir(), 0o755)
        finding = _finding(run_service_checks(), "DR-01")
        assert finding.severity is Severity.FAIL
        assert finding.fix_command is not None
        assert "chmod 0700" in finding.fix_command

    def test_stricter_dir_warns_with_fix(self, installed: Path) -> None:
        os.chmod(layout.data_root(), 0o500)
        finding = _finding(run_service_checks(), "DR-01")
        assert finding.severity is Severity.WARN  # odd, not a security weakening
        assert finding.fix_command is not None
        assert "chmod 0700" in finding.fix_command
        os.chmod(layout.data_root(), 0o700)  # restore: teardown-independent


# ── DR-02: venv integrity ────────────────────────────────────────────


class TestDR02:
    def test_freeze_drift_fails_with_reinstall_fix(self, installed: Path) -> None:
        lock = layout.data_dir("metrics") / "requirements-lock.txt"
        lock.write_text("vesma==0.0.0\n", encoding="utf-8")  # vs freeze {}
        finding = _finding(run_service_checks(), "DR-02")
        assert finding.severity is Severity.FAIL
        assert finding.fix_command is not None
        assert "vesma service install" in finding.fix_command

    def test_world_writable_site_packages_fails_with_chmod_fix(self, installed: Path) -> None:
        site = layout.component_venv_dir("metrics") / "lib" / "python9.9" / "site-packages"
        os.chmod(site, 0o777)
        finding = _finding(run_service_checks(), "DR-02")
        assert finding.severity is Severity.FAIL
        assert finding.fix_command is not None
        assert "go-w" in finding.fix_command

    def test_matching_lock_is_ok(self, installed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        lock_path = layout.data_dir("metrics") / "requirements-lock.txt"
        monkeypatch.setattr(
            doctor_checks, "_pip_freeze", lambda v: doctor_checks.read_lock(lock_path)
        )
        finding = _finding(run_service_checks(), "DR-02")
        assert finding.severity is Severity.OK
        assert finding.fix_command is None


# ── Real pip freeze shapes (shared parser + freeze-vs-lock compare) ───


class TestFreezeFormats:
    """REAL freeze shapes through the SHARED pin parser and the
    freeze-vs-lock compare. Pinned per LY-08: only exact ``==`` pins are
    lock material — direct-url (``pkg @ file://…``) and editable lines are
    SKIPPED by the parser (they can never enter a lock, and a freeze that
    carries them still compares cleanly against the pin subset)."""

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("vesma==5.4.0", ("vesma", "5.4.0")),
            ("name==1.0+local", ("name", "1.0+local")),  # PEP 440 local version
            ("Weird_Name==2.0", ("weird-name", "2.0")),  # PEP 503 name normalization
            ("Weird__Name--x==2.0", ("weird-name-x", "2.0")),
            ("  spaced.name==3.0  ", ("spaced-name", "3.0")),
        ],
    )
    def test_real_pin_shapes_parse(self, line: str, expected: tuple[str, str]) -> None:
        assert install_mod.parse_pin_line(line) == expected
        # The doctor uses the SAME function object — no duplicated parser.
        assert doctor_checks.parse_pin_line is install_mod.parse_pin_line

    @pytest.mark.parametrize(
        "line",
        [
            "pkg @ file:///tmp/x.whl",  # direct-url install — not an exact pin
            "-e git+https://example.com/repo#egg=x",  # editable — not lock material
            "pip",  # bare name, no version
            "",
            "   ",
        ],
    )
    def test_non_pin_freeze_lines_are_skipped(self, line: str) -> None:
        assert install_mod.parse_pin_line(line) is None

    def test_freeze_vs_lock_compare_with_real_shapes(self, isolated_home: Path) -> None:
        """A freeze carrying direct-url/editable noise compares cleanly
        against a lock built from the pin subset — and a genuinely
        hand-installed package still diverges (drift detected)."""
        from vesmaro import __version__

        pins = [f"vesma=={__version__}", "pip==99.0", "weird==1.0+local"]
        lock_path = layout.data_dir("metrics") / "requirements-lock.txt"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("\n".join(pins) + "\n", encoding="utf-8")
        freeze_lines = [
            *pins,
            "local-tool @ file:///tmp/tool.whl",
            "-e git+https://example.com/repo#egg=ed",
        ]
        frozen: dict[str, str] = {}
        for line in freeze_lines:
            parsed = doctor_checks.parse_pin_line(line)
            if parsed is not None:
                frozen[parsed[0]] = parsed[1]
        assert frozen == doctor_checks.read_lock(lock_path)
        frozen["rogue"] = "9.9"  # a hand `pip install` is not canon
        assert frozen != doctor_checks.read_lock(lock_path)


# ── DR-03: user-site leak — REAL clean-env subprocess (no mock) ───────


class TestDR03:
    def test_real_clean_env_subprocess_user_site_excluded(self, isolated_home: Path) -> None:
        """The REAL interpreter under a clean env + PYTHONNOUSERSITE=1 must
        keep the user site dir out of sys.path — this is the DR-03 probe.
        Runs on an ISOLATED installation (the bundled pack, install flow):
        the probe itself is host-agnostic, but run_service_checks() loads
        components.d fail-closed first, and the HOST components.d may carry
        a manifest of another engine version (the load verdict must not
        depend on machine state outside the check's subject)."""

        install_mod.install()
        finding = _finding(run_service_checks(), "DR-03")
        assert finding.severity is Severity.OK
        assert "clean-env import test passed" in finding.detail
        # The subprocess really ran: sys.path from the live interpreter.
        assert finding.detail  # detail carries the probed user-site path

    def test_probe_uses_pythonnousersite_mechanism(self) -> None:
        """Sanity on the probe construction: the exact env the supervisor
        injects (PYTHONNOUSERSITE=1) is what the check relies on."""
        import subprocess
        import sys

        probe = (
            "import json,site,sys;"
            "print(json.dumps({'user': site.getusersitepackages(), 'path': sys.path}))"
        )
        env = {"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~"), "PYTHONNOUSERSITE": "1"}
        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, env=env, check=True
        )
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        assert payload["user"] not in payload["path"]


# ── DR-04: two manifests on one venv ─────────────────────────────────


class TestDR04:
    def test_cross_referenced_venv_fails(self, installed: Path) -> None:
        components = layout.components_dir()
        _write_manifest(
            components,
            "sneaky",
            _child_manifest(
                "sneaky", extra_argv="    - --borrow\n    - venvs/metrics/bin/python\n"
            ),
        )
        finding = _finding(run_service_checks(), "DR-04")
        assert finding.severity is Severity.FAIL
        assert "venvs/metrics/" in finding.detail
        assert finding.fix_command is not None

    def test_own_venv_reference_is_ok(self, installed: Path) -> None:
        finding = _finding(run_service_checks(), "DR-04")
        assert finding.severity is Severity.OK  # metrics references venvs/metrics/ — its own


# ── DR-05: python version constraints ────────────────────────────────


class TestDR05:
    def test_unsatisfiable_constraint_fails(self, installed: Path) -> None:
        components = layout.components_dir()
        _write_manifest(
            components,
            "ancient",
            "apiVersion: vesma.component/v1\n"
            "kind: in-process\n"
            "metadata:\n"
            "  name: ancient\n"
            "  version: 1.0.0\n"
            "  tier: optional\n"
            "  description: needs python 99\n"
            "  provenance:\n"
            "    repo: https://example.com/ancient\n"
            "    license: MIT\n"
            "in_process:\n"
            "  module: ancient.mod\n"
            "  entrypoint: run\n"
            "  python:\n"
            '    version: ">=99.0"\n',
        )
        finding = _finding(run_service_checks(), "DR-05")
        assert finding.severity is Severity.FAIL
        assert ">=99.0" in finding.detail
        assert finding.fix_command is not None

    def test_bundled_constraints_satisfied(self, installed: Path) -> None:
        finding = _finding(run_service_checks(), "DR-05")
        assert finding.severity is Severity.OK


# ── DR-06: venv != engine venv; reserved names ────────────────────────


class TestDR06:
    def test_symlinked_component_venv_fails(self, installed: Path) -> None:
        engine_venv = layout.engine_venv_dir()
        link = layout.data_root() / "venvs" / "evil"
        link.symlink_to(engine_venv)
        finding = _finding(run_service_checks(), "DR-06")
        assert finding.severity is Severity.FAIL
        assert "symlink" in finding.detail
        assert finding.fix_command is not None

    def test_reserved_component_name_fails(self, installed: Path) -> None:
        """Belt-and-braces: the DR-06 scan catches a reserved name even when
        reached directly (the fail-closed LOADER rejects it first — that
        path is asserted in TestFailClosedLoad via DR-00)."""
        components = layout.components_dir()
        _write_manifest(
            components,
            "venv",
            "apiVersion: vesma.component/v1\n"
            "kind: in-process\n"
            "metadata:\n"
            "  name: venv\n"
            "  version: 1.0.0\n"
            "  tier: optional\n"
            "  description: reserved-name squatter\n"
            "  provenance:\n"
            "    repo: https://example.com/venv\n"
            "    license: MIT\n"
            "in_process:\n"
            "  module: venv.mod\n"
            "  entrypoint: run\n",
        )
        finding = doctor_checks._dr06(None)
        assert finding.severity is Severity.FAIL
        assert "reserved" in finding.detail
        assert finding.fix_command is not None

    def test_clean_install_is_ok(self, installed: Path) -> None:
        finding = _finding(run_service_checks(), "DR-06")
        assert finding.severity is Severity.OK

    def test_same_base_cpython_without_engine_venv_is_ok(self, isolated_home: Path) -> None:
        """#501 live repro: the doctor and the component venv run from
        DIFFERENT venvs of the SAME base CPython — ``bin/python`` resolves
        to the same base binary in both — and the layout engine venv is
        absent. Identity must be n/a-OK: sharing the base interpreter
        binary is how every venv works (the old bin/python-vs-sys.executable
        comparison false-FAILed exactly this)."""
        venv_dir = layout.component_venv_dir("repro")
        bin_dir = venv_dir / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "python").symlink_to(Path(os.path.realpath(sys.executable)))
        finding = doctor_checks._dr06(None)
        assert finding.severity is Severity.OK
        assert "engine venv not present" in finding.detail

    def test_distinct_engine_venv_present_is_ok(self, isolated_home: Path) -> None:
        """Engine venv on disk and distinct — own root, own site-packages —
        while the component venv still resolves bin/python to the same base
        CPython binary: identity is judged by roots/site-packages, NOT by
        interpreter binary resolution (#501)."""
        engine_site = layout.engine_venv_dir() / "lib" / "python9.9" / "site-packages"
        engine_site.mkdir(parents=True)
        venv_dir = layout.component_venv_dir("app")
        bin_dir = venv_dir / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "python").symlink_to(Path(os.path.realpath(sys.executable)))
        (venv_dir / "lib" / "python9.9" / "site-packages").mkdir(parents=True)
        finding = doctor_checks._dr06(None)
        assert finding.severity is Severity.OK
        assert "disjoint from the engine venv" in finding.detail

    def test_shared_engine_site_packages_fails(self, isolated_home: Path) -> None:
        """A component venv whose site-packages resolves into the ENGINE
        venv's site-packages is the supply-chain collision DR-06 exists
        for (#501: judged by resolved site-packages, not by bin/python)."""
        engine_site = layout.engine_venv_dir() / "lib" / "python9.9" / "site-packages"
        engine_site.mkdir(parents=True)
        venv_dir = layout.component_venv_dir("leech")
        (venv_dir / "lib" / "python9.9").mkdir(parents=True)
        (venv_dir / "lib" / "python9.9" / "site-packages").symlink_to(engine_site)
        finding = doctor_checks._dr06(None)
        assert finding.severity is Severity.FAIL
        assert "site-packages" in finding.detail
        assert finding.fix_command is not None

    def test_two_manifests_same_venv_root_still_fail_via_dr04(self, installed: Path) -> None:
        """Two manifests pointing at one venv root stay FAIL — DR-04 is the
        enforcer; the DR-06 re-scope (#501: identity vs the ENGINE venv)
        must not absorb or weaken it (DR-06 stays OK on distinct roots)."""
        components = layout.components_dir()
        _write_manifest(
            components,
            "parasite",
            _child_manifest(
                "parasite", extra_argv="    - --borrow\n    - venvs/metrics/bin/python\n"
            ),
        )
        dr04 = _finding(run_service_checks(), "DR-04")
        assert dr04.severity is Severity.FAIL
        assert "venvs/metrics/" in dr04.detail
        dr06 = _finding(run_service_checks(), "DR-06")
        assert dr06.severity is Severity.OK


# ── DR-07: installed unit vs regeneration ────────────────────────────


class TestDR07:
    def test_fresh_install_matches(self, installed: Path) -> None:
        finding = _finding(run_service_checks(), "DR-07")
        assert finding.severity is Severity.OK
        assert "byte-for-byte" in finding.detail

    def test_no_unit_is_ok_na(self, isolated_home: Path) -> None:
        finding = _finding(run_service_checks(), "DR-07")
        assert finding.severity is Severity.OK
        assert "no unit installed" in finding.detail

    def test_hand_edit_fails_with_install_fix(self, installed: Path) -> None:
        unit = installed / ".config/systemd/user/vesma.service"
        unit.write_text(
            unit.read_text(encoding="utf-8").replace("KillMode=mixed", "KillMode=none"),
            encoding="utf-8",
        )
        finding = _finding(run_service_checks(), "DR-07")
        assert finding.severity is Severity.FAIL
        assert finding.fix_command == "vesma service install"


# ── DR-08: control-socket liveness (probe is NEVER a FAIL) ───────────


class _HelloServer:
    """One-shot UNIX socket answering the contract hello (CS §4.5)."""

    def __init__(self, path: Path, reply: str) -> None:
        self._path = path
        self._reply = reply
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(path))
        self._sock.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(self._reply.encode("utf-8"))
        except OSError:
            pass

    def __enter__(self) -> _HelloServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._sock.close()
        self._thread.join(timeout=5)
        if self._path.exists():
            self._path.unlink()


class TestDR08:
    def test_absent_socket_is_ok_status_not_error(self, installed: Path) -> None:
        finding = _finding(run_service_checks(), "DR-08")
        assert finding.severity is Severity.OK
        assert "no control socket" in finding.detail

    @pytest.fixture
    def runtime_socket_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        runtime = tmp_path / "xdg-run"
        (runtime / "vesma").mkdir(parents=True)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
        return runtime / "vesma"

    def test_live_supervisor_hello_is_ok(self, installed: Path, runtime_socket_dir: Path) -> None:
        reply = json.dumps(
            {
                "id": 1,
                "result": {
                    "protocol_version": 1,
                    "min_protocol": 1,
                    "server_version": "test",
                    "pid": 1,
                },
            }
        )
        with _HelloServer(runtime_socket_dir / "control.sock", reply + "\n"):
            finding = _finding(run_service_checks(), "DR-08")
        assert finding.severity is Severity.OK
        assert "live supervisor answered hello" in finding.detail

    def test_stale_socket_is_status_not_error(
        self, installed: Path, runtime_socket_dir: Path
    ) -> None:
        (runtime_socket_dir / "control.sock").write_text("not a socket", encoding="utf-8")
        finding = _finding(run_service_checks(), "DR-08")
        assert finding.severity is Severity.OK  # contract: stale is a status, not a failure
        assert "stale" in finding.detail or "probe failed" in finding.detail

    def test_foreign_json_answerer_warns_with_fix(
        self, installed: Path, runtime_socket_dir: Path
    ) -> None:
        with _HelloServer(runtime_socket_dir / "control.sock", "DEFINITELY NOT JSON\n"):
            finding = _finding(run_service_checks(), "DR-08")
        assert finding.severity is Severity.WARN  # a foreign listener is a live warning
        assert "not a vesma supervisor" in finding.detail
        assert finding.fix_command is not None  # LY-14

    def test_protocol_mismatch_answerer_warns_with_fix(
        self, installed: Path, runtime_socket_dir: Path
    ) -> None:
        reply = json.dumps({"id": 1, "error": {"code": 404, "message": "no such method"}})
        with _HelloServer(runtime_socket_dir / "control.sock", reply + "\n"):
            finding = _finding(run_service_checks(), "DR-08")
        # stale/absent stays OK per contract; a talkative mismatch warns.
        assert finding.severity is Severity.WARN
        assert "protocol mismatch" in finding.detail
        assert finding.fix_command is not None


# ── DR-09: health port collisions ────────────────────────────────────


class TestDR09:
    def test_port_collision_fails(self, installed: Path) -> None:
        components = layout.components_dir()
        _write_manifest(
            components,
            "thief",
            _child_manifest(
                "thief",
                health=(
                    "health:\n"
                    "  checker: http\n"
                    "  http:\n"
                    '    url: "http://127.0.0.1:9110/metrics"\n'
                    "    interval: 5s\n"
                    "    timeout: 2s\n"
                    "    unhealthy_threshold: 3\n"
                ),
            ),
        )
        finding = _finding(run_service_checks(), "DR-09")
        assert finding.severity is Severity.FAIL
        assert "9110" in finding.detail
        assert finding.fix_command is not None

    def test_bundled_pair_no_collision(self, installed: Path) -> None:
        finding = _finding(run_service_checks(), "DR-09")
        assert finding.severity is Severity.OK  # board: callback health; metrics: 9110 alone


# ── DR-10: free space on canonical roots ─────────────────────────────


class TestDR10:
    def test_tiny_volume_fails_with_cache_fix(
        self, installed: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_statvfs = os.statvfs

        def tiny_statvfs(path: Any) -> os.statvfs_result:
            result = real_statvfs(path)
            return os.statvfs_result(
                (
                    result.f_bsize,
                    result.f_frsize,
                    result.f_blocks,
                    result.f_bfree,
                    1,  # f_bavail: one 4K block free -> below the FAIL threshold
                    result.f_files,
                    result.f_ffree,
                    result.f_favail,
                    result.f_flag,
                    result.f_namemax,
                )
            )

        monkeypatch.setattr(doctor_checks.os, "statvfs", tiny_statvfs)
        finding = _finding(run_service_checks(), "DR-10")
        assert finding.severity is Severity.FAIL
        assert finding.fix_command is not None
        assert str(layout.cache_base()) in finding.fix_command

    def test_healthy_volume_is_ok(self, installed: Path) -> None:
        finding = _finding(run_service_checks(), "DR-10")
        assert finding.severity is Severity.OK


# ── DR-11: journald Storage=persistent ───────────────────────────────


class TestDR11:
    def test_no_systemd_is_ok_na(self, installed: Path) -> None:
        finding = _finding(run_service_checks(), "DR-11")
        assert finding.severity is Severity.OK  # fixture pins the run-dir as absent

    def test_volatile_journald_warns_with_fix(
        self, installed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_dir = tmp_path / "run-systemd"
        run_dir.mkdir()
        conf = tmp_path / "journald.conf"
        conf.write_text("[Journal]\nStorage=volatile\n", encoding="utf-8")
        monkeypatch.setattr(doctor_checks, "_SYSTEMD_RUN_DIR", run_dir)
        monkeypatch.setattr(doctor_checks, "_JOURNALD_CONF", conf)
        finding = _finding(run_service_checks(), "DR-11")
        assert finding.severity is Severity.WARN
        assert finding.fix_command is not None
        assert "Storage=persistent" in finding.fix_command

    def test_persistent_journald_is_ok(
        self, installed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_dir = tmp_path / "run-systemd"
        run_dir.mkdir()
        conf = tmp_path / "journald.conf"
        conf.write_text("[Journal]\nStorage=persistent\n", encoding="utf-8")
        monkeypatch.setattr(doctor_checks, "_SYSTEMD_RUN_DIR", run_dir)
        monkeypatch.setattr(doctor_checks, "_JOURNALD_CONF", conf)
        finding = _finding(run_service_checks(), "DR-11")
        assert finding.severity is Severity.OK


# ── DR-12: venv/venvs read-only at runtime ───────────────────────────


class TestDR12:
    def test_fresh_install_is_ok(self, installed: Path) -> None:
        finding = _finding(run_service_checks(), "DR-12")
        assert finding.severity is Severity.OK

    def test_missing_coverage_fails_with_install_fix(self, installed: Path) -> None:
        unit = installed / ".config/systemd/user/vesma.service"
        unit.write_text(
            unit.read_text(encoding="utf-8").replace(
                "ReadOnlyPaths=%h/.local/share/vesma/venv %h/.local/share/vesma/venvs",
                "ReadOnlyPaths=%h/.local/share/vesma/venv",
            ),
            encoding="utf-8",
        )
        finding = _finding(run_service_checks(), "DR-12")
        assert finding.severity is Severity.FAIL
        assert finding.fix_command is not None
        assert "vesma service install" in finding.fix_command

    def test_commented_readonlypaths_without_marker_fails(self, installed: Path) -> None:
        unit = installed / ".config/systemd/user/vesma.service"
        unit.write_text(
            unit.read_text(encoding="utf-8").replace(
                "ReadOnlyPaths=%h/.local/share/vesma/venv %h/.local/share/vesma/venvs",
                "# ReadOnlyPaths=%h/.local/share/vesma/venv %h/.local/share/vesma/venvs",
            ),
            encoding="utf-8",
        )
        finding = _finding(run_service_checks(), "DR-12")
        assert finding.severity is Severity.FAIL  # tampering, not a documented downgrade
        assert "tampering" in finding.detail

    def test_documented_container_downgrade_warns_not_fails(
        self, installed: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The loud documented downgrade (allowlisted, marked) is a WARN —
        a legitimate container install must not be permanently red; any
        UNdocumented loss of coverage stays FAIL (see above)."""
        unit = installed / ".config/systemd/user/vesma.service"
        text = unit.read_text(encoding="utf-8")
        for directive in sorted(unitgen.DOWNGRADE_ALLOWED):
            text = text.replace(f"{directive}=", f"# {directive}=")
        marker = f"{unitgen.DOWNGRADE_MARKER}{','.join(sorted(unitgen.DOWNGRADE_ALLOWED))}"
        text = text.replace("# vesma:generator=", f"{marker}\n# vesma:generator=")
        unit.write_text(text, encoding="utf-8")
        finding = _finding(run_service_checks(), "DR-12")
        assert finding.severity is Severity.WARN
        assert finding.fix_command is not None  # LY-14: a ready command, honestly scoped

    def test_world_writable_venv_on_disk_fails(self, installed: Path) -> None:
        os.chmod(layout.data_root() / "venvs", 0o777)
        finding = _finding(run_service_checks(), "DR-12")
        assert finding.severity is Severity.FAIL
        assert finding.fix_command is not None
        assert "chmod 700" in finding.fix_command

    def test_quoted_readonlypaths_with_spaces_is_still_covered(self, installed: Path) -> None:
        """The generator double-quotes paths containing spaces; the coverage
        comparison must undo exactly that quoting (shlex, not str.split)."""
        unit = installed / ".config/systemd/user/vesma.service"
        unit.write_text(
            unit.read_text(encoding="utf-8").replace(
                "ReadOnlyPaths=%h/.local/share/vesma/venv %h/.local/share/vesma/venvs",
                'ReadOnlyPaths="%h/.local/share/vesma/venv" "%h/.local/share/vesma/venvs"',
            ),
            encoding="utf-8",
        )
        finding = _finding(run_service_checks(), "DR-12")
        assert finding.severity is Severity.OK  # same coverage, spaces-ready form


# ── DR-13: container downgrade allowlist ─────────────────────────────


class TestDR13:
    def test_no_downgrades_ok(self, installed: Path) -> None:
        finding = _finding(run_service_checks(), "DR-13")
        assert finding.severity is Severity.OK
        assert "full hardening block active" in finding.detail

    def test_allowlisted_downgrades_ok(
        self, installed: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(install_mod.unitgen, "container_detect", lambda *a, **k: True)
        install_mod.install()
        finding = _finding(run_service_checks(), "DR-13")
        assert finding.severity is Severity.OK
        assert "inside the allowlist" in finding.detail

    def test_illegal_downgrade_marker_fails(self, installed: Path) -> None:
        unit = installed / ".config/systemd/user/vesma.service"
        unit.write_text(
            unit.read_text(encoding="utf-8").replace(
                "# vesma:generator=", f"{unitgen.DOWNGRADE_MARKER}Restart\n# vesma:generator="
            ),
            encoding="utf-8",
        )
        finding = _finding(run_service_checks(), "DR-13")
        assert finding.severity is Severity.FAIL
        assert "Restart" in finding.detail
        assert finding.fix_command is not None

    def test_silent_hardening_comment_out_fails(self, installed: Path) -> None:
        unit = installed / ".config/systemd/user/vesma.service"
        unit.write_text(
            unit.read_text(encoding="utf-8").replace(
                "NoNewPrivileges=true", "# NoNewPrivileges=true"
            ),
            encoding="utf-8",
        )
        finding = _finding(run_service_checks(), "DR-13")
        assert finding.severity is Severity.FAIL  # commented WITHOUT the marker = tampering
        assert "NoNewPrivileges" in finding.detail
        assert "vesma service install" in (finding.fix_command or "")

    def test_tier_b_systemcallfilter_comment_is_not_a_downgrade(self, installed: Path) -> None:
        finding = _finding(run_service_checks(), "DR-13")
        assert finding.severity is Severity.OK  # the designed Tier-B comment is exempt


# ── LY-14: every finding carries severity + a ready fix command ──────


class TestEveryFindingHasFixCommand:
    def test_warn_and_fail_findings_carry_fix_commands(self, installed: Path) -> None:
        # Inject a spread of defects so the run produces WARN and FAIL rows.
        env_file = layout.env_file_path("metrics")
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text("X=1\n", encoding="utf-8")
        os.chmod(env_file, 0o644)  # DR-01 FAIL
        lock = layout.data_dir("metrics") / "requirements-lock.txt"
        lock.write_text("vesma==0.0.0\n", encoding="utf-8")  # DR-02 FAIL
        os.chmod(layout.components_dir(), 0o755)  # DR-01 FAIL
        findings = run_service_checks()
        assert findings
        for finding in findings:
            assert isinstance(finding.severity, Severity)
            if finding.severity in (Severity.WARN, Severity.FAIL):
                assert finding.fix_command, f"{finding.check_id} has no ready fix command"
            else:
                assert finding.severity is Severity.OK

    def test_exit_codes(self, installed: Path) -> None:
        assert exit_code([_finding(run_service_checks(), "DR-07")]) == 0
        warns = [Finding("X-1", "t", Severity.WARN, "d", "fix")]
        assert exit_code(warns) == 2
        fails = [Finding("X-1", "t", Severity.FAIL, "d", "fix")]
        assert exit_code(fails) == 1


# ── Doctor NEVER mutates (snapshot before/after a full run) ──────────


class TestDoctorDoesNotMutate:
    def test_full_run_leaves_installation_byte_identical(self, installed: Path) -> None:
        def snapshot() -> dict[str, tuple[int, int, int, bytes]]:
            state: dict[str, tuple[int, int, int, bytes]] = {}
            for path in sorted(installed.rglob("*")):
                st = path.stat()
                if stat.S_ISDIR(st.st_mode):
                    state[str(path)] = (st.st_mode, st.st_mtime_ns, st.st_ino, b"")
                else:
                    state[str(path)] = (
                        stat.S_IMODE(st.st_mode),
                        st.st_mtime_ns,
                        st.st_ino,
                        path.read_bytes(),
                    )
            return state

        before = snapshot()
        findings = run_service_checks()
        after = snapshot()
        assert before == after, "doctor mutated the installation — contract violation"
        # And it really exercised the checks (not a vacuous pass).
        assert {f.check_id for f in findings} >= {f"DR-{i:02d}" for i in range(1, 14)}


# ── Fail-closed installation load surfaces as DR-00 ──────────────────


class TestFailClosedLoad:
    def test_invalid_manifest_file_reports_dr00_fail(self, installed: Path) -> None:
        _write_manifest(layout.components_dir(), "broken", "not: a: manifest:\n")
        findings = run_service_checks()
        assert len(findings) == 1
        assert findings[0].check_id == "DR-00"
        assert findings[0].severity is Severity.FAIL
        assert findings[0].fix_command is not None
        assert exit_code(findings) == 1


# ── CLI surface: `vesma doctor service` ───────────────────────────────


class TestDoctorServiceCli:
    def test_json_output_carries_findings_and_exit_code(
        self, installed: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from typer.testing import CliRunner

        from vesmaro.cli.doctor import doctor_app

        env_file = layout.env_file_path("metrics")
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text("X=1\n", encoding="utf-8")
        os.chmod(env_file, 0o644)  # one FAIL -> exit 1

        result = CliRunner().invoke(doctor_app, ["service", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["exit_code"] == 1
        ids = {c["check_id"] for c in payload["checks"]}
        assert "DR-01" in ids
        dr01 = next(c for c in payload["checks"] if c["check_id"] == "DR-01")
        assert dr01["severity"] == "FAIL"
        assert dr01["fix_command"] and "chmod 600" in dr01["fix_command"]

    def test_doctor_never_executes_fixes(self, installed: Path) -> None:
        """DR-01 injects a 0644 env file; the doctor run must leave it 0644."""
        env_file = layout.env_file_path("metrics")
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text("X=1\n", encoding="utf-8")
        os.chmod(env_file, 0o644)
        run_service_checks()
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o644
