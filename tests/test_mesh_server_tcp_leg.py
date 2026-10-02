"""W2.5 TCP leg tests (ADR-0019 option 1, ratified archcom 2026-09-20).

Exercises the optional networked transport of
:class:`vesma.mesh_server.MeshServer` — ``add_secure_port`` with
mesh-CA mTLS on the SAME grpcio server — over a REAL gRPC channel on
loopback with a throwaway PKI (no mocks on the gRPC/TLS layer). Covers:

* Config contract (amendment 3d): default OFF with the exact defaults
  (``port 8790`` / ``bind 127.0.0.1`` / empty ``tls.existing_secret``);
  old configs without a ``tcp:`` section parse unchanged; ``enabled``
  requires all three TLS file paths; ``tcp.enabled`` without
  ``mesh.enabled`` is rejected at the config boundary.
* Default-off opens NO TCP port: with ``tcp.enabled: false`` the server
  starts fine even when the TLS paths point at nonexistent files (no
  TCP material is ever read).
* Enabled leg: a real grpcio listener comes up on loopback; the bound
  port is exposed (ephemeral ``port: 0`` form); startup logs the
  ``mesh tcp leg listening on`` line.
* Fail-fast (amendment 3c): a busy port raises
  :class:`vesma.mesh_server.MeshTCPLegError` from ``start()``.
* Client-auth matrix: no client cert → rejected at the handshake;
  foreign-CA client cert → rejected at the handshake; valid mesh-CA
  client cert → admitted (``Heartbeat`` succeeds).
* Fingerprint pin (symmetric to the mesh peer leg): a pinned
  ``sha256:<hex>`` fingerprint admits the pinned node on a data RPC; a
  DIFFERENT valid mesh-CA node (chain verifies, handshake passes) is
  refused with ``PERMISSION_DENIED`` — fail-closed, not fail-open.

The tests use the real :class:`MemoryManager` against a tmp SQLite
store (same fixture pattern as ``test_mesh_server.py``).
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import grpc
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from pydantic import ValidationError

from vesma import _mesh_gen
from vesma.config import FederationConfig, PeerConfig, Settings
from vesma.manager import MemoryManager
from vesma.mesh_server import MeshServer, MeshTCPLegError

# ── Constants ────────────────────────────────────────────────────────────────

_PROJECT = "test-project"
_PEER_ID = "mnemos-A"
_AGENT = "gcw-test-agent"
_TOKEN_ENV = "VESMA_FED_PEER_TEST_TOKEN"


# ── Throwaway PKI ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Leaf:
    """One generated certificate + key, materialised as PEM files."""

    cert_path: Path
    key_path: Path
    fingerprint: str  # "sha256:<hex-of-DER>" — the mesh peer-leg convention


@dataclass(frozen=True)
class _PKI:
    """Full throwaway PKI for one test: mesh CA + core leaf + clients."""

    ca_path: Path
    core: _Leaf  # mnemos-core server identity (the TCP leg's cert)
    node: _Leaf  # the pinned mesh node (valid mesh-CA client)
    other_node: _Leaf  # a DIFFERENT mesh-CA node (pin-mismatch case)
    foreign_ca_path: Path
    foreign_node: _Leaf  # valid chain, wrong CA


def _generate_pki(root: Path) -> _PKI:
    """Generate the mesh CA + core identity + client leaves (EC P-256).

    Mirrors the deployment shape from ADR-0019 amendment 3e: one common
    mesh CA, a server-identity leaf for mnemos-core, and mesh node
    client certs. The foreign CA models an unrelated PKI.
    """
    root.mkdir(parents=True, exist_ok=True)

    def ca(cn: str) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=2))
            .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
            .sign(key, hashes.SHA256())
        )
        return cert, key

    def leaf(
        cn: str,
        issuer: x509.Certificate,
        issuer_key: ec.EllipticCurvePrivateKey,
        *,
        server: bool = False,
    ) -> _Leaf:
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(UTC)
        usages = [ExtendedKeyUsageOID.CLIENT_AUTH]
        if server:
            usages.append(ExtendedKeyUsageOID.SERVER_AUTH)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
            .issuer_name(issuer.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=2))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.SubjectAlternativeName(
                    [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
                ),
                critical=False,
            )
            .add_extension(x509.ExtendedKeyUsage(usages), critical=False)
            .sign(issuer_key, hashes.SHA256())
        )
        cert_path = root / f"{cn}.crt.pem"
        key_path = root / f"{cn}.key.pem"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
        der = cert.public_bytes(serialization.Encoding.DER)
        return _Leaf(
            cert_path=cert_path,
            key_path=key_path,
            fingerprint="sha256:" + hashlib.sha256(der).hexdigest(),
        )

    mesh_ca, mesh_ca_key = ca("mesh-ca")
    foreign, foreign_key = ca("foreign-ca")
    ca_path = root / "mesh-ca.crt.pem"
    ca_path.write_bytes(mesh_ca.public_bytes(serialization.Encoding.PEM))
    foreign_ca_path = root / "foreign-ca.crt.pem"
    foreign_ca_path.write_bytes(foreign.public_bytes(serialization.Encoding.PEM))
    return _PKI(
        ca_path=ca_path,
        core=leaf("mnemos-core", mesh_ca, mesh_ca_key, server=True),
        node=leaf("mesh-node", mesh_ca, mesh_ca_key),
        other_node=leaf("mesh-node-2", mesh_ca, mesh_ca_key),
        foreign_ca_path=foreign_ca_path,
        foreign_node=leaf("foreign-node", foreign, foreign_key),
    )


# ── Settings / fixtures ──────────────────────────────────────────────────────


def _tcp_settings(
    tmp_path: Path,
    pki: _PKI,
    *,
    tcp_enabled: bool = True,
    port: int = 0,
    tls_cert: str | None = None,
    tls_key: str | None = None,
    tls_ca: str | None = None,
    pinned_fingerprint: str | None = None,
) -> Settings:
    """Settings with one peer + the TCP leg configured against ``pki``.

    ``port=0`` (the default here) binds an ephemeral port so parallel
    test runs never collide; the real port is read back from the running
    server (``MeshServer.tcp_bound_port``).
    """
    peer = PeerConfig(
        bearer_token_env=_TOKEN_ENV,
        allowed_projects=[_PROJECT],
        allowed_types=["decision", "learning"],
        rate_limit_per_minute=600,
        mtls_cert_fingerprint=pinned_fingerprint,
    )
    settings = Settings(
        **{  # type: ignore[arg-type]  # pydantic dict→model coercion
            "vesma": {
                "vault_path": str(tmp_path / "vault"),
                "data_dir": str(tmp_path / "data"),
                "db_name": "test_mesh_tcp_leg.db",
            },
            "embedding": {"provider": "onnx"},
            "scanner": {"enabled": False},
            "federation": FederationConfig(
                shared_projects=[_PROJECT],
                peers={_PEER_ID: peer},
            ),
            "mesh": {
                "enabled": True,
                "tcp": {
                    "enabled": tcp_enabled,
                    "port": port,
                    "bind": "127.0.0.1",
                    "tls": {
                        "existing_secret": "mnemos-core-grpc-tls",
                        "cert_file": tls_cert or str(pki.core.cert_path),
                        "key_file": tls_key or str(pki.core.key_path),
                        "ca_file": tls_ca or str(pki.ca_path),
                    },
                },
            },
        }
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def pki(tmp_path: Path) -> _PKI:
    return _generate_pki(tmp_path / "pki")


@pytest.fixture
def manager(tmp_path: Path, pki: _PKI) -> Generator[MemoryManager, None, None]:
    """Real MemoryManager against a tmp store; embedder mocked (no ONNX)."""
    settings = _tcp_settings(tmp_path, pki)
    mgr = MemoryManager(settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    yield mgr
    mgr.close()


def _tcp_server(
    tmp_path: Path,
    pki: _PKI,
    manager: MemoryManager,
    **overrides: Any,
) -> MeshServer:
    """A MeshServer with the TCP leg configured (caller starts it)."""
    settings = _tcp_settings(tmp_path, pki, **overrides)
    return MeshServer(str(tmp_path / "core.sock"), manager, settings, max_workers=2)


def _secure_stub(
    port: int,
    pki: _PKI,
    *,
    client: _Leaf | None = None,
    ca_path: Path | None = None,
) -> Any:
    """A MnemosCoreStub over a TLS channel.

    ``client=None`` → no client certificate (anonymous TLS attempt).
    ``ca_path`` overrides the trust root (foreign-CA case).
    """
    creds = grpc.ssl_channel_credentials(
        root_certificates=(ca_path or pki.ca_path).read_bytes(),
        private_key=client.key_path.read_bytes() if client else None,
        certificate_chain=client.cert_path.read_bytes() if client else None,
    )
    channel = grpc.secure_channel(f"127.0.0.1:{port}", creds)
    return _mesh_gen.core_pb2_grpc.MnemosCoreStub(channel)


# ── Config contract (amendment 3d) ───────────────────────────────────────────


def test_config_defaults_are_off() -> None:
    """Default Settings carry the amendment-3d defaults — TCP fully off."""
    mesh = Settings().mesh
    assert mesh.tcp.enabled is False
    assert mesh.tcp.port == 8790
    assert mesh.tcp.bind == "127.0.0.1"
    assert mesh.tcp.tls.existing_secret == ""
    assert mesh.tcp.tls.cert_file == ""
    assert mesh.tcp.tls.key_file == ""
    assert mesh.tcp.tls.ca_file == ""


def test_old_config_without_tcp_section_parses_unchanged() -> None:
    """A pre-W2.5 config (no ``tcp:`` key) parses byte-identically."""
    from vesma.config import MeshConfig

    cfg = MeshConfig(enabled=True, socket_path="/run/mnemos/core.sock")
    assert cfg.tcp.enabled is False
    assert cfg.tcp.port == 8790


def test_tcp_enabled_requires_tls_material() -> None:
    """``tcp.enabled: true`` without the TLS file paths is a config error."""
    from vesma.config import MeshConfig

    with pytest.raises(ValidationError, match="cert_file"):
        MeshConfig(enabled=True, tcp={"enabled": True, "port": 8790})


def test_tcp_requires_mesh_master_switch() -> None:
    """``tcp.enabled`` without ``mesh.enabled`` is rejected, not silent."""
    from vesma.config import MeshConfig

    with pytest.raises(ValidationError, match=r"mesh\.enabled"):
        MeshConfig(
            enabled=False,
            tcp={
                "enabled": True,
                "tls": {"cert_file": "/a", "key_file": "/b", "ca_file": "/c"},
            },
        )


def test_full_tcp_config_parses() -> None:
    """The full chart-shaped section parses with the chart-facing key."""
    from vesma.config import MeshConfig

    cfg = MeshConfig(
        enabled=True,
        tcp={
            "enabled": True,
            "port": 8790,
            "bind": "127.0.0.1",
            "tls": {
                "existing_secret": "mnemos-core-grpc-tls",
                "cert_file": "/tls/tls.crt",
                "key_file": "/tls/tls.key",
                "ca_file": "/tls/ca.crt",
            },
        },
    )
    assert cfg.tcp.tls.existing_secret == "mnemos-core-grpc-tls"
    assert cfg.tcp.port == 8790


# ── Default-off opens no TCP port ────────────────────────────────────────────


def test_default_off_opens_no_tcp_port(tmp_path: Path, pki: _PKI, manager: MemoryManager) -> None:
    """With ``tcp.enabled: false`` no TCP material is read, no port opens.

    Poison test: the TLS paths point at NONEXISTENT files — if the
    server ignored the switch it would crash reading them. It must
    start cleanly (UDS only) and expose ``tcp_bound_port=None``.
    """
    settings = _tcp_settings(
        tmp_path,
        pki,
        tcp_enabled=False,
        tls_cert=str(tmp_path / "nonexistent.crt"),
        tls_key=str(tmp_path / "nonexistent.key"),
        tls_ca=str(tmp_path / "nonexistent.ca"),
    )
    srv = MeshServer(str(tmp_path / "core.sock"), manager, settings, max_workers=2)
    srv.start()
    try:
        assert srv.is_running
        assert srv.tcp_bound_port is None
    finally:
        srv.stop(grace=0.5)


# ── Enabled leg: real listener ───────────────────────────────────────────────


def test_enabled_leg_listens_and_admits_valid_ca_client(
    tmp_path: Path, pki: _PKI, manager: MemoryManager
) -> None:
    """Enabled leg binds a real port; a mesh-CA client is admitted."""
    srv = _tcp_server(tmp_path, pki, manager)
    srv.start()
    try:
        port = srv.tcp_bound_port
        assert port is not None and port > 0
        stub = _secure_stub(port, pki, client=pki.node)
        resp = stub.Heartbeat(_mesh_gen.core_pb2.HeartbeatRequest(), timeout=5)
        assert resp.healthy is True
    finally:
        srv.stop(grace=0.5)


def test_enabled_leg_logs_listening_line(
    tmp_path: Path,
    pki: _PKI,
    manager: MemoryManager,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Startup logs exactly one 'mesh tcp leg listening on' line (task §4)."""
    import logging as _logging

    srv = _tcp_server(tmp_path, pki, manager)
    with caplog.at_level(_logging.INFO, logger="vesma.mesh_server"):
        srv.start()
    try:
        lines = [r for r in caplog.records if "mesh tcp leg listening on" in r.message]
        assert len(lines) == 1
        assert f"127.0.0.1:{srv.tcp_bound_port}" in lines[0].message
    finally:
        srv.stop(grace=0.5)


# ── Fail-fast (amendment 3c) ─────────────────────────────────────────────────


def test_fail_fast_on_busy_port(tmp_path: Path, pki: _PKI, manager: MemoryManager) -> None:
    """A port already in use → typed startup exception, no silent skip."""
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = blocker.getsockname()[1]
    try:
        srv = _tcp_server(tmp_path, pki, manager, port=busy_port)
        with pytest.raises(MeshTCPLegError, match=str(busy_port)):
            srv.start()
        assert not srv.is_running
    finally:
        blocker.close()


def test_fail_fast_on_unreadable_tls_material(
    tmp_path: Path,
    pki: _PKI,
    manager: MemoryManager,
) -> None:
    """Broken PEM paths with the leg enabled → typed error + FULL rollback
    (review N1/N3): no stale is_running, no leftover UDS socket file, and a
    second start() is not refused as "already started"."""
    garbage = tmp_path / "garbage.pem"
    garbage.write_text("not a pem")
    settings = _tcp_settings(
        tmp_path,
        pki,
        tls_cert=str(garbage),
        tls_key=str(tmp_path / "missing-key.pem"),
    )
    sock = tmp_path / "core.sock"
    srv = MeshServer(str(sock), manager, settings, max_workers=2)
    with pytest.raises(MeshTCPLegError, match="invalid TLS material"):
        srv.start()
    assert not srv.is_running
    assert not sock.exists()
    # The failed instance must not poison a retry (N1: "already started").
    with pytest.raises(MeshTCPLegError, match="invalid TLS material"):
        srv.start()


# ── Client-auth matrix (anonymous/foreign-CA/valid) ─────────────────────────


def test_client_without_cert_rejected(tmp_path: Path, pki: _PKI, manager: MemoryManager) -> None:
    """Anonymous TLS (trusts the CA, presents no cert) → handshake reject."""
    srv = _tcp_server(tmp_path, pki, manager)
    srv.start()
    try:
        stub = _secure_stub(srv.tcp_bound_port, pki, client=None)
        with pytest.raises(grpc.RpcError) as excinfo:
            stub.Heartbeat(_mesh_gen.core_pb2.HeartbeatRequest(), timeout=5)
        assert excinfo.value.code() == grpc.StatusCode.UNAVAILABLE
    finally:
        srv.stop(grace=0.5)


def test_client_with_foreign_ca_rejected(tmp_path: Path, pki: _PKI, manager: MemoryManager) -> None:
    """A client from an unrelated CA → handshake reject (chain invalid)."""
    srv = _tcp_server(tmp_path, pki, manager)
    srv.start()
    try:
        stub = _secure_stub(
            srv.tcp_bound_port,
            pki,
            client=pki.foreign_node,
            ca_path=pki.foreign_ca_path,
        )
        with pytest.raises(grpc.RpcError) as excinfo:
            stub.Heartbeat(_mesh_gen.core_pb2.HeartbeatRequest(), timeout=5)
        assert excinfo.value.code() == grpc.StatusCode.UNAVAILABLE
    finally:
        srv.stop(grace=0.5)


def test_client_with_valid_ca_admitted(tmp_path: Path, pki: _PKI, manager: MemoryManager) -> None:
    """A mesh-CA client cert → the TLS handshake passes and RPCs serve."""
    srv = _tcp_server(tmp_path, pki, manager)
    srv.start()
    try:
        stub = _secure_stub(srv.tcp_bound_port, pki, client=pki.node)
        resp = stub.Heartbeat(_mesh_gen.core_pb2.HeartbeatRequest(), timeout=5)
        assert resp.healthy is True
        assert resp.version
    finally:
        srv.stop(grace=0.5)


# ── Fingerprint pin (symmetric to the mesh peer leg) ────────────────────────


def test_pin_match_admits_data_rpc(tmp_path: Path, pki: _PKI, manager: MemoryManager) -> None:
    """Pinned fingerprint == presented node cert → data RPC serves."""
    srv = _tcp_server(tmp_path, pki, manager, pinned_fingerprint=pki.node.fingerprint)
    srv.start()
    try:
        stub = _secure_stub(srv.tcp_bound_port, pki, client=pki.node)
        resp = stub.ListMemories(
            _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT]), timeout=5
        )
        assert resp.total == 0  # empty store, but SERVED — not PERMISSION_DENIED
    finally:
        srv.stop(grace=0.5)


def test_pin_mismatch_rejected_even_with_valid_ca_chain(
    tmp_path: Path, pki: _PKI, manager: MemoryManager
) -> None:
    """A DIFFERENT mesh-CA node (chain OK, handshake OK) → pin refuses.

    This is the case the pin exists for: the caller holds a perfectly
    valid mesh-CA certificate but is not THE pinned node.
    """
    srv = _tcp_server(tmp_path, pki, manager, pinned_fingerprint=pki.node.fingerprint)
    srv.start()
    try:
        stub = _secure_stub(srv.tcp_bound_port, pki, client=pki.other_node)
        with pytest.raises(grpc.RpcError) as excinfo:
            stub.ListMemories(
                _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT]), timeout=5
            )
        assert excinfo.value.code() == grpc.StatusCode.PERMISSION_DENIED
    finally:
        srv.stop(grace=0.5)


def test_no_pin_valid_ca_still_admitted_on_data_rpc(
    tmp_path: Path, pki: _PKI, manager: MemoryManager
) -> None:
    """Pin unset (operator opt-out) → mesh-CA chain auth still serves data."""
    srv = _tcp_server(tmp_path, pki, manager, pinned_fingerprint=None)
    srv.start()
    try:
        stub = _secure_stub(srv.tcp_bound_port, pki, client=pki.node)
        resp = stub.ListMemories(
            _mesh_gen.core_pb2.ListMemoriesRequest(projects=[_PROJECT]), timeout=5
        )
        assert resp.total == 0
    finally:
        srv.stop(grace=0.5)
