"""Issue #510 — mesh legs in ``service run`` (ServiceApp parity with serve()).

``vesma service run`` embeds the SAME app serve() runs (SL §3.1); its
native mesh wiring (unix socket +, with ``mesh.tcp.enabled``, the mTLS
TCP leg) must come up in the service-run process too. Covers:

* :func:`vesma.service.backend.start_mesh_legs` / :func:`stop_mesh_legs`
  — the shared helper (serve() and ServiceApp call the SAME code);
* ServiceApp e2e: with ``mesh.enabled: true`` the unix leg binds on the
  configured socket file while the supervisor lives, and the socket is
  removed on graceful stop; with ``mesh.tcp.enabled`` + a throwaway PKI
  the TCP leg binds (ephemeral ``port: 0`` form, read back through
  ``tcp_bound_port``);
* fail-fast: a mesh leg that cannot start fails the service start loudly.

The real :class:`MeshServer` runs (real gRPC, tmp paths); the manager
singleton under it is the REAL MemoryManager against a tmp store (the
``vesma.api.main._manager`` singleton is reset around every test).
mTLS handshake/client-auth depth lives in ``tests/test_mesh_server_tcp_leg.py``
and is deliberately not re-asserted here.
"""

from __future__ import annotations

import logging
import socket
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

# The throwaway PKI comes from the W2.5 suite (imported, not duplicated —
# one PKI generator in the codebase).
from tests.test_mesh_server_tcp_leg import _generate_pki
from vesma.api import main as api_main
from vesma.config import load_settings
from vesma.service.backend import (
    CoreStartupError,
    ServiceApp,
    start_mesh_legs,
    stop_mesh_legs,
)
from vesma.service.manifest import load_manifest

WAIT_TIMEOUT_S = 20.0


def _one_manifest(tmp_path: Path) -> dict[str, Any]:
    """One in-process dummy component (the supervisor needs >= 1)."""
    doc: dict[str, Any] = {
        "apiVersion": "vesma.component/v1",
        "kind": "in-process",
        "metadata": {
            "name": "dummy",
            "version": "0.1.0",
            "tier": "optional",
            "description": "service mesh-leg e2e dummy",
            "provenance": {"repo": "https://example.com/x", "license": "MIT"},
        },
        "in_process": {
            "module": "tests.service_children.inproc_dummy",
            "entrypoint": "create_component",
        },
    }
    path = tmp_path / "dummy.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return {"dummy": load_manifest(path)}


def _make_app(tmp_path: Path, config: Path, *, with_core: bool) -> ServiceApp:
    """ServiceApp over the dummy manifest + tmp config (control socket
    also in tmp; the real §3.7 sink is replaced by the test seam)."""
    return ServiceApp(
        _one_manifest(tmp_path),
        socket_path=tmp_path / "control.sock",
        core_bind=("127.0.0.1", 0),
        with_core=with_core,
        config=str(config),
    )


def wait_until(
    predicate: Callable[[], bool], message: str, timeout: float = WAIT_TIMEOUT_S
) -> None:
    """Poll-until with a deadline (the SL-05 flake lesson)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {message}")


def _tcp_accepts(port: int) -> bool:
    """Transport-level probe: the bound port accepts a TCP handshake (no
    client cert — mTLS depth stays with the W2.5 suite)."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def _write_config(tmp_path: Path, mesh_section: dict[str, Any]) -> Path:
    """Minimal config with isolated storage + a mesh section."""
    cfg: dict[str, Any] = {
        "vesma": {
            "data_dir": str(tmp_path / "data"),
            "vault_path": str(tmp_path / "vault"),
        },
    }
    cfg["mesh"] = mesh_section
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


@pytest.fixture
def manager_singleton_reset() -> Iterator[None]:
    """Reset the api.main manager singleton around every test (the helper
    seeds it from the test's own config; the next test must not inherit a
    manager pointing at the previous tmp store)."""
    api_main._manager = None
    try:
        yield
    finally:
        manager = api_main._manager
        api_main._manager = None
        if manager is not None:
            manager.close()


class TestStartMeshLegsHelper:
    def test_disabled_returns_none_no_manager_seeding(self) -> None:
        """mesh.enabled=false (the default): the helper is a no-op —
        byte-parity with the pre-#510 disabled shape (no manager
        construction, nothing listened)."""
        assert start_mesh_legs(load_settings()) is None
        assert api_main._manager is None

    def test_stop_mesh_legs_none_is_noop(self) -> None:
        stop_mesh_legs(None)  # must not raise


class TestServiceAppUnixLeg:
    def test_unix_leg_listens_and_stops_clean(
        self,
        tmp_path: Path,
        manager_singleton_reset: None,
    ) -> None:
        """service run with mesh.enabled=true: the unix leg binds on the
        configured socket path — issue #510's core parity claim (under the
        service the mnemos-mesh Go node's LOCAL transport stays green
        instead of degrading)."""
        socket_path = tmp_path / "run" / "core.sock"
        config = _write_config(tmp_path, {"enabled": True, "socket_path": str(socket_path)})
        app = _make_app(tmp_path, config, with_core=False)
        try:
            app.bind()
            app.start()
            wait_until(
                lambda: socket_path.is_socket(), "mesh unix leg bound on the configured path"
            )
        finally:
            app.request_stop()
            code = app.wait()
        assert code == 0
        wait_until(lambda: not socket_path.exists(), "mesh unix socket removed on stop")


class TestServiceAppTcpLeg:
    def test_tcp_leg_binds_with_mtls_pki(
        self,
        tmp_path: Path,
        manager_singleton_reset: None,
    ) -> None:
        """service run with mesh.tcp.enabled (throwaway PKI, ephemeral
        port 0): the TCP leg listens — the migrated-installation health
        gate (mnemos-mesh dials 127.0.0.1:8790) is satisfiable under the
        service. The bound port is read back via ``tcp_bound_port``."""
        pki = _generate_pki(tmp_path / "pki")
        config = _write_config(
            tmp_path,
            {
                "enabled": True,
                "socket_path": str(tmp_path / "run" / "core.sock"),
                "tcp": {
                    "enabled": True,
                    "port": 0,  # ephemeral: parallel runs never collide
                    "bind": "127.0.0.1",
                    "tls": {
                        "existing_secret": "mnemos-core-grpc-tls",
                        "cert_file": str(pki.core.cert_path),
                        "key_file": str(pki.core.key_path),
                        "ca_file": str(pki.ca_path),
                    },
                },
            },
        )
        app = _make_app(tmp_path, config, with_core=False)
        try:
            app.bind()
            app.start()
            mesh_server = app._mesh_server
            assert mesh_server is not None, "mesh leg must be wired under the service"
            bound = mesh_server.tcp_bound_port
            assert bound is not None and bound > 0
            wait_until(lambda: _tcp_accepts(bound), "tcp leg accepts connections")
        finally:
            app.request_stop()
            code = app.wait()
        assert code == 0


class TestMeshLegFailFast:
    def test_unstartable_leg_fails_the_service_start(
        self,
        tmp_path: Path,
        manager_singleton_reset: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A leg that cannot bind is a LOUD failure (SL §3.1 fail-fast):
        the start raises, the supervisor is ordered down, no half-alive
        service (mirrors mesh_server's own amendment-3c semantics at the
        service boundary)."""
        dead_cert = tmp_path / "no-such-cert.pem"  # tcp on, TLS material missing
        config = _write_config(
            tmp_path,
            {
                "enabled": True,
                "socket_path": str(tmp_path / "run" / "core.sock"),
                "tcp": {
                    "enabled": True,
                    "port": 0,
                    "bind": "127.0.0.1",
                    "tls": {
                        "existing_secret": "x",
                        "cert_file": str(dead_cert),
                        "key_file": str(dead_cert),
                        "ca_file": str(dead_cert),
                    },
                },
            },
        )
        app = _make_app(tmp_path, config, with_core=False)
        app.bind()
        with (
            caplog.at_level(logging.ERROR),
            pytest.raises(CoreStartupError, match="mesh server failed to start"),
        ):
            app.start()
        assert app.supervisor.shutdown_requested
        assert any("fail-fast" in rec.message for rec in caplog.records)
        # no leg socket was created mid-failure
        assert not (tmp_path / "run" / "core.sock").is_socket()


class TestHelperConfigSeeding:
    def test_manager_seeded_from_given_config(
        self, tmp_path: Path, manager_singleton_reset: None
    ) -> None:
        """The singleton is seeded from the GIVEN config path (the serve()
        contract, now shared with ServiceApp) — one manager for the mesh
        and the HTTP/in-process core."""
        socket_path = tmp_path / "run" / "core.sock"
        config = _write_config(tmp_path, {"enabled": True, "socket_path": str(socket_path)})
        try:
            server = start_mesh_legs(load_settings(str(config)), str(config))
        finally:
            stop_mesh_legs(server)
        assert server is not None
        assert api_main._manager is not None
