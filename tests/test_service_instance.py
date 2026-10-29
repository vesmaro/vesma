"""Instance identity + endpoint parameterization tests (ADR-0049 wave 1).

Covers: the id charset contract, mint-once stability, env-vs-file
resolution with the typed mismatch refusal, the endpoint namespace
derivation (default byte-identical / instances/<id>/), the effective
mesh socket resolution (card ba729a40), the typed port-conflict refusal
(card ff6a9385), the hello-frame identity, and the read-only response
resolver behind the X-Vesma-Instance-Id header.
"""

from __future__ import annotations

import http.server
import socket
import threading
from pathlib import Path

import pytest

from tests.control_fakes import DEFAULT_COMPONENTS, FakeBackend
from vesma.service import control
from vesma.service.control import ControlServer
from vesma.service.instance import (
    DEFAULT_INSTANCE_ID,
    INSTANCE_ENV_VAR,
    EndpointBusyError,
    InstanceHomeMismatchError,
    InstanceIdError,
    current_instance_id,
    effective_mesh_socket_path,
    ensure_instance_id,
    id_file_path,
    preflight_api_port,
    read_persisted_instance_id,
    resolve_requested_instance_id,
    runtime_instance_dir,
    validate_instance_id,
)


@pytest.fixture
def runtime_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A per-test XDG_RUNTIME_DIR (short paths — AF_UNIX budget)."""
    runtime = tmp_path / "run"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    return runtime


@pytest.fixture
def no_instance_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(INSTANCE_ENV_VAR, raising=False)


# ── id charset contract ───────────────────────────────────────────────


class TestInstanceIdContract:
    def test_valid_ids_pass_through(self) -> None:
        assert validate_instance_id("work") == "work"
        assert validate_instance_id("2nd-brain") == "2nd-brain"
        assert validate_instance_id("a" * 63) == "a" * 63

    def test_default_normalizes(self) -> None:
        assert validate_instance_id("default") == DEFAULT_INSTANCE_ID
        assert validate_instance_id(" default ") == DEFAULT_INSTANCE_ID

    @pytest.mark.parametrize(
        "bad", ["", "Work", "has_underscore", "has space", "-lead", ".dot", "a" * 64, "кode"]
    )
    def test_invalid_ids_refused(self, bad: str) -> None:
        with pytest.raises(InstanceIdError):
            validate_instance_id(bad)

    def test_env_resolution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(INSTANCE_ENV_VAR, raising=False)
        assert resolve_requested_instance_id() is None
        assert resolve_requested_instance_id({}) is None
        assert resolve_requested_instance_id({INSTANCE_ENV_VAR: "  "}) is None
        assert resolve_requested_instance_id({INSTANCE_ENV_VAR: "work"}) == "work"
        assert resolve_requested_instance_id({INSTANCE_ENV_VAR: "default"}) == (DEFAULT_INSTANCE_ID)
        monkeypatch.setenv(INSTANCE_ENV_VAR, "work")
        assert resolve_requested_instance_id() == "work"

    def test_env_bad_charset_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(INSTANCE_ENV_VAR, "Not_Valid")
        with pytest.raises(InstanceIdError):
            resolve_requested_instance_id()


# ── mint-once stability + resolution chain ────────────────────────────


class TestEnsureInstanceId:
    def test_mint_is_stable_and_private(self, tmp_path: Path) -> None:
        home = tmp_path / "data"
        first = ensure_instance_id(home)
        second = ensure_instance_id(home)
        assert first == second
        assert len(first) == 12
        mode = id_file_path(home).stat().st_mode & 0o777
        assert mode == 0o600

    def test_env_wins_and_agrees_with_file(self, tmp_path: Path) -> None:
        home = tmp_path / "data"
        assert ensure_instance_id(home, env_value="work") == "work"
        assert read_persisted_instance_id(home) == "work"
        assert ensure_instance_id(home, env_value="work") == "work"

    def test_env_mismatch_refused_fail_loud(self, tmp_path: Path) -> None:
        home = tmp_path / "data"
        ensure_instance_id(home, env_value="alpha")
        with pytest.raises(InstanceHomeMismatchError) as excinfo:
            ensure_instance_id(home, env_value="beta")
        message = str(excinfo.value)
        assert "alpha" in message and "beta" in message
        # the persisted identity is untouched by the refusal
        assert read_persisted_instance_id(home) == "alpha"

    def test_damaged_id_file_refused_not_silently_reminted(self, tmp_path: Path) -> None:
        home = tmp_path / "data"
        home.mkdir()
        id_file_path(home).write_text("Bad_Id!\n", encoding="utf-8")
        with pytest.raises(InstanceIdError):
            read_persisted_instance_id(home)
        with pytest.raises(InstanceIdError):
            ensure_instance_id(home)

    def test_absent_file_reads_none(self, tmp_path: Path) -> None:
        assert read_persisted_instance_id(tmp_path) is None


# ── endpoint namespace derivation ─────────────────────────────────────


class TestEndpointNamespace:
    def test_default_keeps_canonical_root(self, tmp_path: Path) -> None:
        root = tmp_path / "vesma"
        assert runtime_instance_dir(root, None) == root
        assert runtime_instance_dir(root, DEFAULT_INSTANCE_ID) == root

    def test_named_instance_gets_instances_dir(self, tmp_path: Path) -> None:
        root = tmp_path / "vesma"
        assert runtime_instance_dir(root, "work") == root / "instances" / "work"

    def test_socket_path_default_byte_identical(self, runtime_dir: Path) -> None:
        path, used_fallback = control.socket_path_for(None)
        assert used_fallback is False
        assert path == runtime_dir / "vesma" / "control.sock"
        assert control.default_socket_path() == (path, used_fallback)

    def test_socket_path_named_instance(self, runtime_dir: Path) -> None:
        path, _ = control.socket_path_for("work")
        assert path == runtime_dir / "vesma" / "instances" / "work" / "control.sock"

    def test_socket_path_explicit_default_is_canonical(self, runtime_dir: Path) -> None:
        path, _ = control.socket_path_for(DEFAULT_INSTANCE_ID)
        assert path == runtime_dir / "vesma" / "control.sock"


# ── hello-frame identity (Lock 3) ─────────────────────────────────────


class TestHelloIdentity:
    def test_hello_reports_named_instance(self) -> None:
        server = ControlServer(FakeBackend(DEFAULT_COMPONENTS), instance_id="work")
        payload = control.hello_result(server._instance_id)
        assert payload["instance_id"] == "work"

    def test_hello_defaults_to_default(self) -> None:
        payload = control.hello_result()
        assert payload["instance_id"] == DEFAULT_INSTANCE_ID
        assert payload["pid"] == __import__("os").getpid()

    def test_named_instance_binds_instances_dir(self, runtime_dir: Path) -> None:
        server = ControlServer(FakeBackend(DEFAULT_COMPONENTS), socket_instance_id="work")
        result = server.bind()
        assert result.status == "bound"
        assert result.path == runtime_dir / "vesma" / "instances" / "work" / "control.sock"
        assert server.path == result.path
        server.stop()

    def test_default_instance_binds_canonical_socket(self, runtime_dir: Path) -> None:
        server = ControlServer(FakeBackend(DEFAULT_COMPONENTS))
        result = server.bind()
        assert result.status == "bound"
        assert result.path == runtime_dir / "vesma" / "control.sock"
        server.stop()


# ── effective mesh socket (card ba729a40) ─────────────────────────────


class TestEffectiveMeshSocket:
    def _settings(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **mesh_kwargs):  # type: ignore[no-untyped-def]
        from vesma.config import MeshConfig, Settings

        monkeypatch.chdir(tmp_path)  # no .env interference
        settings = Settings(mesh=MeshConfig(**mesh_kwargs))
        settings.resolve_paths()
        return settings

    def test_explicit_socket_path_always_wins(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        settings = self._settings(monkeypatch, tmp_path, socket_path="/opt/mesh/core.sock")
        assert effective_mesh_socket_path(settings) == "/opt/mesh/core.sock"

    def test_default_user_instance_keeps_today_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_instance_env: None
    ) -> None:
        from vesma.config import MeshConfig

        settings = self._settings(monkeypatch, tmp_path)
        assert settings.mesh.socket_path == MeshConfig().socket_path
        assert effective_mesh_socket_path(settings) == settings.mesh.socket_path

    def test_named_user_instance_gets_runtime_dir_socket(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, runtime_dir: Path
    ) -> None:
        monkeypatch.setenv(INSTANCE_ENV_VAR, "work")
        settings = self._settings(monkeypatch, tmp_path)
        assert effective_mesh_socket_path(settings) == str(
            runtime_dir / "vesma" / "instances" / "work" / "core.sock"
        )

    def test_system_profile_gets_fhs_ipc_socket(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_instance_env: None
    ) -> None:
        from vesma.service import fhs, layout

        monkeypatch.setattr(layout, "system_profile_active", lambda: True)
        settings = self._settings(monkeypatch, tmp_path)
        assert effective_mesh_socket_path(settings) == str(fhs.system_ipc_dir() / "core.sock")


# ── typed port-conflict refusal (card ff6a9385) ───────────────────────


class _IdentityHandler(http.server.BaseHTTPRequestHandler):
    """Minimal HTTP holder: answers /health with the instance header."""

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("X-Vesma-Instance-Id", "holder-1")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args: object) -> None:
        del args  # silence the test stderr


class TestPreflightApiPort:
    def test_free_port_passes(self) -> None:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
        probe.close()
        preflight_api_port("127.0.0.1", free_port)

    def test_ephemeral_port_never_conflicts(self) -> None:
        preflight_api_port("127.0.0.1", 0)

    def test_busy_port_refused_unattributed(self) -> None:
        holder = socket.socket()
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        with pytest.raises(EndpointBusyError) as excinfo:
            preflight_api_port("127.0.0.1", port)
        assert "another process" in str(excinfo.value)
        holder.close()

    def test_busy_port_names_vesma_holder_instance(self) -> None:
        server = http.server.HTTPServer(("127.0.0.1", 0), _IdentityHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            with pytest.raises(EndpointBusyError) as excinfo:
                preflight_api_port("127.0.0.1", port)
            message = str(excinfo.value)
            assert "holder-1" in message
            assert "api.port" in message
        finally:
            server.shutdown()
            server.server_close()


# ── read-only resolver (API header source) ────────────────────────────


class TestCurrentInstanceId:
    def test_env_wins(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv(INSTANCE_ENV_VAR, "work")
        assert current_instance_id(tmp_path) == "work"

    def test_persisted_file_next(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv(INSTANCE_ENV_VAR, raising=False)
        home = tmp_path / "data"
        identity = ensure_instance_id(home)
        assert current_instance_id(home) == identity

    def test_nothing_yet_degrades_to_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_instance_env: None
    ) -> None:
        assert current_instance_id(tmp_path / "absent") == DEFAULT_INSTANCE_ID

    def test_damaged_file_degrades_not_crashes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_instance_env: None
    ) -> None:
        home = tmp_path / "data"
        home.mkdir()
        id_file_path(home).write_text("!!!\n", encoding="utf-8")
        assert current_instance_id(home) == DEFAULT_INSTANCE_ID
