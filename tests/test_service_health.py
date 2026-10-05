"""Health checker conformance (component-manifest v1 §3.7 + SL §3.3 T4/T7/T9).

The callback isolation boundary is the core of the file: ANY exception,
timeout, resolution failure or malformed result collapses to a ``failed``
probe with CALLBACK_FAILED logged — nothing ever propagates into the
supervisor (CM §3.7/D).
"""

from __future__ import annotations

import http.server
import socket
import threading
import time
from pathlib import Path

import pytest

from vesmaro.service.health import (
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
        assert parse_module_attr("vesmaro.service.board:health") == (
            "vesmaro.service.board",
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

    def test_raising_callback_is_a_failed_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    def test_malformed_result_is_a_failed_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_cb(monkeypatch, lambda: "not a dict")
        assert CallbackChecker(_CB_SPEC, 1.0).check().ok is False

    def test_bad_state_value_is_a_failed_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
