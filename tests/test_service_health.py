"""Health checker conformance (component-manifest v1 §3.7 + SL §3.3 T4/T7/T9).

The callback isolation boundary is the core of the file: ANY exception,
timeout, resolution failure or malformed result collapses to a ``failed``
probe with CALLBACK_FAILED logged — nothing ever propagates into the
supervisor (CM §3.7/D).
"""

from __future__ import annotations

import http.server
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from vesma.service.health import (
    CALLBACK_TIMEOUT_S,
    CallbackChecker,
    ExecChecker,
    HttpChecker,
    LivenessChecker,
    TcpChecker,
    build_checker,
    parse_module_attr,
    probe_params,
)

# ── parse_module_attr ─────────────────────────────────────────────────


class TestParseModuleAttr:
    def test_valid(self) -> None:
        assert parse_module_attr("vesma.service.board:health") == (
            "vesma.service.board",
            "health",
        )

    @pytest.mark.parametrize("spec", ["", "module", "module:", ":attr"])
    def test_invalid(self, spec: str) -> None:
        with pytest.raises(ValueError, match="module:attr"):
            parse_module_attr(spec)


# ── Callback isolation boundary (CM §3.7/D) ───────────────────────────

_CB_SPEC = "tests.test_service_health:cb_target"


def _install_cb(monkeypatch: pytest.MonkeyPatch, target: object) -> None:
    monkeypatch.setattr("tests.test_service_health.cb_target", target, raising=False)


class TestCallbackIsolationBoundary:
    def test_resolution_failure_never_propagates(self) -> None:
        result = CallbackChecker("no.such.module:cb", 0.5).check()
        assert result.ok is False
        assert "CALLBACK_FAILED" in result.detail

    def test_raising_callback_is_a_failed_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom() -> dict[str, str]:
            raise RuntimeError("callback exploded")

        _install_cb(monkeypatch, boom)
        result = CallbackChecker(_CB_SPEC, 1.0).check()
        assert result.ok is False
        assert result.detail.startswith("CALLBACK_FAILED")
        assert "RuntimeError" in result.detail

    def test_timeout_is_a_failed_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def hang() -> dict[str, str]:
            time.sleep(2.0)

        _install_cb(monkeypatch, hang)
        result = CallbackChecker(_CB_SPEC, 0.1).check()
        assert result.ok is False
        assert "timeout" in result.detail

    def test_malformed_result_is_a_failed_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_cb(monkeypatch, lambda: "not a dict")
        assert CallbackChecker(_CB_SPEC, 1.0).check().ok is False

    def test_bad_state_value_is_a_failed_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_cb(monkeypatch, lambda: {"state": "excellent"})
        result = CallbackChecker(_CB_SPEC, 1.0).check()
        assert result.ok is False
        assert "CALLBACK_FAILED" in result.detail

    def test_degraded_state_does_not_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_cb(monkeypatch, lambda: {"state": "degraded"})
        assert CallbackChecker(_CB_SPEC, 1.0).check().ok is False

    def test_healthy_state_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_cb(monkeypatch, lambda: {"state": "healthy", "detail": "fine"})
        result = CallbackChecker(_CB_SPEC, 1.0).check()
        assert result.ok is True
        assert "fine" in result.detail

    def test_default_timeout_constant(self) -> None:
        assert CALLBACK_TIMEOUT_S == 2.0


# ── Exec / liveness ───────────────────────────────────────────────────


class TestExecAndLiveness:
    def test_exec_rc0_passes(self, tmp_path: Path) -> None:
        probe = tmp_path / "flag"
        probe.write_text("x", encoding="utf-8")
        assert ExecChecker(["/usr/bin/test", "-f", str(probe)], 2.0).check().ok is True

    def test_exec_rc1_fails(self, tmp_path: Path) -> None:
        checker = ExecChecker(["/usr/bin/test", "-f", str(tmp_path / "absent")], 2.0)
        assert checker.check().ok is False

    def test_exec_missing_binary_fails(self) -> None:
        assert ExecChecker(["/nonexistent/binary/xyz"], 2.0).check().ok is False

    def test_probe_child_env_is_empty_and_fds_closed(self, tmp_path: Path) -> None:
        """P2-E (cascade 2026-10-05): probe children inherit NOTHING of the
        supervisor's environment (env={}, explicit close_fds=True); the argv
        carries absolute paths, so no PATH lookup is needed."""
        import json
        import sys

        out = tmp_path / "probe_env.json"
        printer = Path(__file__).parent / "service_children" / "env_printer.py"
        checker = ExecChecker([sys.executable, str(printer), "--out", str(out)], 10.0)
        assert checker.check().ok is True
        payload = json.loads(out.read_text(encoding="utf-8"))
        child_env = dict(payload["env"])
        # PEP 538: CPython coerces the C locale at startup and sets LC_CTYPE
        # in its OWN environment — a self-inflicted artifact of the child
        # runtime (see the SL-13 supervisor test), not an inherited variable.
        child_env.pop("LC_CTYPE", None)
        assert child_env == {}  # env={}: the probe sees nothing
        sockets = [fd for fd, target in payload["fds"].items() if target.startswith("socket:")]
        assert sockets == []  # close_fds=True: no fd crosses the probe boundary

    def test_reaper_owned_exec_reports_the_honest_code(self) -> None:
        """The reaper owns waitpid (SL §3.1) — the checker must NOT wait the
        probe child itself. A subprocess.run wait would race the reaper's
        waitpid(-1) drain, Popen would answer the stolen child (ECHILD) with
        a fabricated sts=0, and a FAILING probe would be reported healthy
        (a spurious T4 «up»). With the reap lookup injected, the checker
        reads the reaper's record instead."""
        from types import SimpleNamespace

        def reap(pid: int) -> SimpleNamespace | None:
            try:
                done_pid, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                done_pid, status = pid, 0  # already collected — nothing left to say
            if done_pid == 0:
                return None  # still running — keep polling
            code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else None
            return SimpleNamespace(code=code, signal_name=None)

        checker = ExecChecker(["/usr/bin/test", "-f", "/nonexistent/probe-path"], 2.0, reap=reap)
        result = checker.check()
        assert result.ok is False
        assert result.detail == "exec rc=1"

    def test_reaper_owned_exec_timeout_kills_and_fails(self) -> None:
        from types import SimpleNamespace

        def reap(pid: int) -> SimpleNamespace | None:
            try:
                done_pid, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                done_pid, status = pid, 0
            if done_pid == 0:
                return None
            code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else None
            return SimpleNamespace(code=code, signal_name=None)

        checker = ExecChecker(["/bin/sleep", "5"], 0.2, reap=reap)
        result = checker.check()
        assert result.ok is False
        assert result.detail == "exec probe timeout"
        # Collect the killed probe child — the production reaper would have.
        try:
            while os.waitpid(-1, os.WNOHANG)[0] != 0:
                pass
        except ChildProcessError:
            pass

    def test_liveness_predicate(self) -> None:
        assert LivenessChecker(lambda: True, "alive").check().ok is True
        result = LivenessChecker(lambda: False, "alive").check()
        assert result.ok is False
        assert "not alive" in result.detail


# ── Tcp / http against real local sockets ─────────────────────────────


class _OkHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args: object) -> None:
        return None


def _free_dead_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class TestTcpAndHttp:
    @pytest.fixture
    def http_port(self) -> int:
        server = http.server.HTTPServer(("127.0.0.1", 0), _OkHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield int(server.server_address[1])
        server.shutdown()
        server.server_close()
        thread.join()

    def test_tcp_connect_ok(self, http_port: int) -> None:
        assert TcpChecker("127.0.0.1", http_port, 2.0).check().ok is True

    def test_tcp_refused(self) -> None:
        assert TcpChecker("127.0.0.1", _free_dead_port(), 0.5).check().ok is False

    def test_http_2xx_passes(self, http_port: int) -> None:
        checker = HttpChecker(f"http://127.0.0.1:{http_port}/anything", 2.0)
        result = checker.check()
        assert result.ok is True
        assert "200" in result.detail

    def test_http_refused(self) -> None:
        checker = HttpChecker(f"http://127.0.0.1:{_free_dead_port()}/", 0.5)
        assert checker.check().ok is False


# ── Builders (manifest -> checker + timing) ───────────────────────────


class _FakeManifest:
    def __init__(self, health: object, kind: str) -> None:
        self.health = health
        self.kind = kind


class TestBuilders:
    def test_probe_params_defaults_without_health_section(self) -> None:
        params = probe_params(_FakeManifest(health=None, kind="child-process"))  # type: ignore[arg-type]
        assert params.startup_grace_s == 30.0
        assert params.startup_interval_s == 5.0
        assert params.unhealthy_threshold == 1

    def test_build_checker_liveness_for_missing_health(self) -> None:
        checker = build_checker(
            _FakeManifest(health=None, kind="child-process"),  # type: ignore[arg-type]
            expansion={},
            alive=lambda: True,
        )
        assert isinstance(checker, LivenessChecker)
        assert checker.check().ok is True
