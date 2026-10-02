"""Tests for federation config load-time warnings (B4).

Covers the mTLS pinning-OFF warning emitted by ``load_settings`` when
federation is active (peers configured) but a peer has
``mtls_cert_fingerprint=None``. The warning is non-breaking — it does
not refuse to load, only makes the operator opt-out visible.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from vesmaro.config import PeerConfig, load_settings


def _write_config(
    tmp_path: Path,
    *,
    peers: dict[str, PeerConfig] | None = None,
) -> Path:
    """Write a minimal config.yaml with the given federation peers."""
    cfg = tmp_path / "config.yaml"
    if peers:
        peer_lines: list[str] = []
        for pid, peer in peers.items():
            peer_lines.append(f"    {pid}:")
            peer_lines.append(f"      bearer_token_env: {peer.bearer_token_env}")
            fp = peer.mtls_cert_fingerprint
            if fp is None:
                peer_lines.append("      mtls_cert_fingerprint: null")
            else:
                peer_lines.append(f'      mtls_cert_fingerprint: "{fp}"')
        peers_block = "\n".join(peer_lines)
    else:
        peers_block = "    {}"
    cfg.write_text(
        "mnemos:\n"
        f"  vault_path: {tmp_path / 'vault'}\n"
        f"  data_dir: {tmp_path / 'data'}\n"
        "embedding:\n"
        "  provider: onnx\n"
        "scanner:\n"
        "  enabled: false\n"
        "federation:\n"
        "  shared_projects: [project-mnemos]\n"
        "  peers:\n"
        f"{peers_block}\n",
        encoding="utf-8",
    )
    return cfg


def test_warns_when_peer_mtls_pinning_off(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """load_settings warns when a peer has mtls_cert_fingerprint=None."""
    cfg = _write_config(
        tmp_path,
        peers={
            "mnemos-A": PeerConfig(
                bearer_token_env="VESMA_FED_PEER_TOKEN",
                mtls_cert_fingerprint=None,
            ),
        },
    )
    with caplog.at_level(logging.WARNING, logger="vesmaro.config"):
        load_settings(cfg)
    warnings = [r for r in caplog.records if "pinning OFF" in r.message]
    assert len(warnings) == 1
    assert "mnemos-A" in warnings[0].message


def test_no_warning_when_peer_mtls_pinning_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """load_settings does NOT warn when the peer has a fingerprint set."""
    cfg = _write_config(
        tmp_path,
        peers={
            "mnemos-A": PeerConfig(
                bearer_token_env="VESMA_FED_PEER_TOKEN",
                mtls_cert_fingerprint="aa:bb:cc:dd",
            ),
        },
    )
    with caplog.at_level(logging.WARNING, logger="vesmaro.config"):
        load_settings(cfg)
    warnings = [r for r in caplog.records if "pinning OFF" in r.message]
    assert warnings == []


def test_no_warning_when_federation_inactive(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """load_settings does NOT warn when no peers are configured (federation off)."""
    cfg = _write_config(tmp_path, peers=None)
    with caplog.at_level(logging.WARNING, logger="vesmaro.config"):
        load_settings(cfg)
    warnings = [r for r in caplog.records if "pinning OFF" in r.message]
    assert warnings == []


def test_warns_per_peer_with_off_peer(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """One warning per peer that has pinning off; pinned peer is silent."""
    cfg = _write_config(
        tmp_path,
        peers={
            "mnemos-A": PeerConfig(
                bearer_token_env="VESMA_FED_PEER_TOKEN_A",
                mtls_cert_fingerprint=None,
            ),
            "mnemos-B": PeerConfig(
                bearer_token_env="VESMA_FED_PEER_TOKEN_B",
                mtls_cert_fingerprint=None,
            ),
            "mnemos-C": PeerConfig(
                bearer_token_env="VESMA_FED_PEER_TOKEN_C",
                mtls_cert_fingerprint="11:22:33:44",
            ),
        },
    )
    with caplog.at_level(logging.WARNING, logger="vesmaro.config"):
        load_settings(cfg)
    msgs = [r.message for r in caplog.records if "pinning OFF" in r.message]
    assert len(msgs) == 2
    assert any("mnemos-A" in m for m in msgs)
    assert any("mnemos-B" in m for m in msgs)
    assert not any("mnemos-C" in m for m in msgs)


def test_warning_is_non_blocking(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The warning does NOT prevent settings from loading (non-breaking)."""
    cfg = _write_config(
        tmp_path,
        peers={
            "mnemos-A": PeerConfig(
                bearer_token_env="VESMA_FED_PEER_TOKEN",
                mtls_cert_fingerprint=None,
            ),
        },
    )
    settings = load_settings(cfg)
    # Settings still loaded with the peer intact.
    assert "mnemos-A" in settings.federation.peers
    assert settings.federation.peers["mnemos-A"].mtls_cert_fingerprint is None
