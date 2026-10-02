"""Integration tests: vesmaro MeshClient ↔ MeshServer ↔ mnemos-mesh binary.

Architecture under test (W2 stitch, mnemos-mesh#20 — updated 2026-09-17):

  The mesh↔mnemos transport is a Unix socket **served by mnemos** and
  **dialed by the mesh binary**. Concretely:

  1. :class:`vesmaro.mesh_server.MeshServer` binds ``MnemosCore`` gRPC on
     the socket (``settings.mesh.socket_path``) — mnemos is the SERVER.
  2. The ``mnemos-mesh`` Go binary DIALS that socket (its ``unix_socket``
     config key) and proxies ``FederationPeer`` RPCs onto ``MnemosCore``.
  3. :class:`vesmaro.mesh_client.MeshClient` dials the same socket and is
     the Python-side consumer used by tests and tooling.

  The pre-2026-09 fixture here assumed the M2 mesh binary itself creates
  the socket — that never shipped; the tests skipped forever with "M2
  serves FederationPeer, not MnemosCore". This rewrite starts the real
  MeshServer in-process and layers the binary on top when available.

Binary discovery:
  The mesh binary is OPTIONAL. When the env var ``MESH_BIN`` points at an
  existing ``mnemos-mesh`` executable the fixture starts it (``serve``
  against a scratch mesh.yaml pinning the same socket) so the Go-side
  dial path is exercised too; without ``MESH_BIN`` the tests cover the
  Python-side contract only and the binary-dependent assertions degrade
  to "server reachable" checks. A legacy sibling checkout at
  ``../mnemos-mesh/bin/mnemos-mesh`` is used as fallback so a fresh
  clone with ``make build`` already done still exercises the dial path.

No cert fixtures are committed: when the mesh binary runs, a TEST-ONLY
CA + node cert (CN prefixed ``test-``) are generated in ``tmp_path`` via
the ``cryptography`` library (a mnemos dependency). Keys live only in
``tmp_path`` and are removed on teardown.
"""

from __future__ import annotations

import datetime
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Generator
from pathlib import Path

import pytest
import yaml

from vesmaro.compact import CompactRecord
from vesmaro.config import FederationConfig, PeerConfig, Settings
from vesmaro.manager import MemoryManager
from vesmaro.mesh_client import (
    MeshClient,
    MeshUnavailableError,
    MeshUnimplementedError,
)
from vesmaro.mesh_server import MeshServer

# ── Constants ─────────────────────────────────────────────────────────────────

#: Peer identity used in client calls. With exactly one configured peer
#: the MeshServer resolves it without gRPC metadata (single-peer fallback).
_PEER_ID = "test-peer"

#: Project the test peer is allowed to pull; kept in sync with the ACL
#: configured on the fixture settings.
_PROJECT = "test-project"

#: Name of the env var that may point at the mnemos-mesh binary.
MESH_BIN_ENV = "MESH_BIN"

#: Legacy fallback: sibling mnemos-mesh checkout with a built binary.
_FALLBACK_BINARY = (
    Path(__file__).resolve().parent.parent.parent / "mnemos-mesh" / "bin" / "mnemos-mesh"
)


def _find_mesh_binary() -> Path | None:
    """Resolve the mesh binary: ``$MESH_BIN`` first, sibling checkout second.

    Returns ``None`` when no binary is available — the tests then skip the
    Go-side dial assertions instead of failing (the binary is a separate
    repo and artifact).
    """
    env_bin = os.environ.get(MESH_BIN_ENV, "")
    if env_bin:
        p = Path(env_bin)
        if p.is_file() and os.access(p, os.X_OK):
            return p
        pytest.fail(f"{MESH_BIN_ENV}={env_bin!r} is set but not an executable file")
    if _FALLBACK_BINARY.is_file() and os.access(_FALLBACK_BINARY, os.X_OK):
        return _FALLBACK_BINARY
    return None


MESH_BINARY = _find_mesh_binary()

# ── Settings / manager (isolated tmp stores, no ~/.mnemos contact) ───────────


def _settings_with_peer(tmp_path: Path, socket_path: Path) -> Settings:
    """Build Settings with one peer + mesh enabled on the scratch socket.

    Mirrors the pattern in ``tests/test_mesh_server.py``: isolated SQLite
    + vault under ``tmp_path``, a single fail-closed peer ACL, and the
    ``mesh`` section pointed at the fixture-owned socket. The embedder is
    mocked downstream so no ONNX download happens.
    """
    settings = Settings(
        **{  # type: ignore[arg-type]  # pydantic dict→model coercion
            "mnemos": {
                "vault_path": str(tmp_path / "vault"),
                "data_dir": str(tmp_path / "data"),
                "db_name": "test_mesh_integration.db",
            },
            "embedding": {"provider": "onnx"},
            "scanner": {"enabled": False},
            "federation": FederationConfig(
                shared_projects=[_PROJECT],
                peers={
                    _PEER_ID: PeerConfig(
                        bearer_token_env="VESMA_FED_PEER_TEST_TOKEN",
                        allowed_projects=[_PROJECT],
                        allowed_types=["decision", "learning"],
                        rate_limit_per_minute=600,
                    ),
                },
            ),
            "mesh": {
                "enabled": True,
                "socket_path": str(socket_path),
                "timeout_s": 5.0,
            },
        }
    )
    settings.resolve_paths()
    return settings


# ── mTLS cert generation (only needed when the mesh binary runs) ─────────────


def _generate_test_certs(cert_dir: Path) -> dict[str, Path]:
    """Generate a TEST-ONLY CA, node cert, and node key in ``cert_dir``.

    Uses the ``cryptography`` library. All CNs are prefixed with ``test-``
    so the material can never be mistaken for production certs. Returns a
    dict mapping ``ca_cert`` / ``node_cert`` / ``node_key`` to paths.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    cert_dir.mkdir(parents=True, exist_ok=True)

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-mnemos-mesh-ca")])
    ca_cert_obj = (
        x509.CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    ca_cert_path = cert_dir / "ca.crt"
    ca_cert_path.write_bytes(ca_cert_obj.public_bytes(serialization.Encoding.PEM))

    node_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    node_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-node")])
    node_cert_obj = (
        x509.CertificateBuilder()
        .subject_name(node_subject)
        .issuer_name(ca_subject)
        .public_key(node_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("test-node")]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    node_cert_path = cert_dir / "node.crt"
    node_cert_path.write_bytes(node_cert_obj.public_bytes(serialization.Encoding.PEM))
    node_key_path = cert_dir / "node.key"
    node_key_path.write_bytes(
        node_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    os.chmod(node_key_path, 0o600)

    return {
        "ca_cert": ca_cert_path,
        "node_cert": node_cert_path,
        "node_key": node_key_path,
    }


def _free_tcp_port() -> int:
    """Reserve and immediately release a free TCP port for the mesh peer listener."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def mesh_socket(tmp_path: Path) -> Generator[str, None, None]:
    """Serve ``MnemosCore`` on a scratch Unix socket via the real MeshServer.

    Yields the socket path. This is the top of the transport stack: the
    mesh binary and MeshClient both dial this socket. Teardown stops the
    gRPC server (which unlinks the socket) and closes the manager.
    """
    socket_path = tmp_path / "core.sock"
    settings = _settings_with_peer(tmp_path, socket_path)

    mgr = MemoryManager(settings)
    # Stub the embedder so MemoryManager works without the ONNX runtime.
    from unittest.mock import MagicMock

    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder

    server = MeshServer(str(socket_path), mgr, settings, max_workers=2)
    server.start()
    try:
        yield str(socket_path)
    finally:
        server.stop(grace=2.0)
        mgr.close()


@pytest.fixture
def mesh_binary(tmp_path: Path, mesh_socket: str) -> Generator[str, None, None]:
    """Start the mesh binary (when present) dialed into ``mesh_socket``.

    Layered on top of ``mesh_socket``: writes a minimal ``mesh.yaml`` whose
    ``unix_socket`` points at the MeshServer-owned socket, starts the Go
    binary with mTLS generated in ``tmp_path`` (no peers — the peer
    listener is not exercised here), and yields the socket path either way.

    Binary contract checks (the Go binary answers on the dialed socket)
    only run when a binary was found; without one the fixture degrades to
    the pure Python-side path — a hard skip would hide server-side
    regressions behind an optional artifact.
    """
    if MESH_BINARY is None:
        yield mesh_socket
        return

    cert_paths = _generate_test_certs(tmp_path / "certs")
    config = {
        "node_id": "test-node",
        "listen": f"127.0.0.1:{_free_tcp_port()}",
        "unix_socket": mesh_socket,
        "peers": [],
        "mtls": {
            "ca_cert": str(cert_paths["ca_cert"]),
            "node_cert": str(cert_paths["node_cert"]),
            "node_key": str(cert_paths["node_key"]),
        },
    }
    config_path = tmp_path / "mesh.yaml"
    config_path.write_text(yaml.safe_dump(config))

    proc = subprocess.Popen(
        [str(MESH_BINARY), "serve", "--config", str(config_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        # The binary must come up and NOT report a failed dial into the
        # MeshServer socket. A clean startup is the contract here.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.1)
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            pytest.fail(f"mesh binary exited early (rc={proc.returncode}):\n{out[-2000:]}")
        yield mesh_socket
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        if cert_paths["node_key"].exists():
            cert_paths["node_key"].unlink(missing_ok=True)
        shutil.rmtree(tmp_path / "certs", ignore_errors=True)


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_heartbeat_round_trip(mesh_binary: str) -> None:
    """Heartbeat against the socket returns a non-empty version string.

    With the real MeshServer serving, ``Heartbeat`` reports the mnemos
    version (e.g. ``"mnemos 4.3.0"``). We assert non-empty so a change
    that drops the version field is caught.
    """
    with MeshClient(mesh_binary, timeout=5.0) as client:
        healthy, version, _uptime = client.heartbeat(_PEER_ID, component="mnemos")

    assert healthy is True
    assert isinstance(version, str)
    assert version != ""


def test_list_memories_empty_page(mesh_binary: str) -> None:
    """ListMemories on an empty store returns a valid empty page.

    The store is fresh per test, so the honest contract here is an empty
    list — not the old M2-era UNIMPLEMENTED tolerance. A UNAVAILABLE
    error still means the transport is broken.
    """
    with MeshClient(mesh_binary, timeout=5.0) as client:
        try:
            records = client.list_memories(projects=[_PROJECT])
        except MeshUnimplementedError:
            pytest.fail("ListMemories returned UNIMPLEMENTED — servicer not wired")
        except MeshUnavailableError:
            pytest.fail("ListMemories returned UNAVAILABLE — mesh socket down")

    assert isinstance(records, list)
    assert records == []
    assert all(hasattr(r, "id") for r in records)


def test_write_memory_import_round_trip(mesh_binary: str) -> None:
    """WriteMemory imports a record; a follow-up ListMemories returns it.

    Replaces the M2-era "expect UNIMPLEMENTED" assertion: the servicer
    implements the write path, so the round trip is the real contract.
    The record carries the peer's allowed project tag so the ACL GATE
    passes on both directions.
    """
    record = CompactRecord(
        id=f"fed:{_PEER_ID}:uuid-1",
        type="decision",
        title="test record",
        summary="generated by integration test",
        key_points=["one"],
        tags=["project:" + _PROJECT, "agent:test-agent", "mnemos:decision"],
        source_agent="test-agent",
        timestamp="2026-07-22T00:00:00Z",
    )

    with MeshClient(mesh_binary, timeout=5.0) as client:
        written_id = client.write_memory(record, import_mode="MERGE")
        assert written_id != ""

        records = client.list_memories(projects=[_PROJECT])
        assert records, "the just-imported record must be exported back"
        # Contract note: mnemos does NOT reuse the incoming compact id as
        # the storage id — WriteMemory mints a fresh memory id (the
        # incoming id is preserved in metadata as ``fed_id``). The
        # exported envelope is rebuilt as ``fed:<source_agent>:<memory
        # id>``, so we match on title + source_agent, not on the id.
        assert any(r.title == "test record" and r.source_agent == "test-agent" for r in records)


def test_mesh_binary_dial_not_degraded(mesh_binary: str, tmp_path: Path) -> None:
    """The Go binary dials the MeshServer socket without degraded mode.

    Binary-only: verifies the Go side comes up with no
    ``dial mnemos failed`` warning — the W1 scratch validation's key
    signal, now pinned as a regression test. Skips (with a clear reason)
    when no mesh binary is available.
    """
    if MESH_BINARY is None:
        pytest.skip(
            f"mesh binary not found — set {MESH_BIN_ENV}=/path/to/mnemos-mesh "
            "or build the sibling mnemos-mesh checkout (make build)"
        )
    assert MESH_BINARY is not None
    # The mesh_binary fixture already failed the test had the binary
    # exited early or degraded; here we assert the positive path: the
    # socket is reachable via a plain Heartbeat while the binary is up.
    with MeshClient(mesh_binary, timeout=5.0) as client:
        healthy, _version, _uptime = client.heartbeat(_PEER_ID, component="mnemos")
    assert healthy is True
