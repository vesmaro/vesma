"""Tests for consolidated directory layout, migration, and logging config.

Covers:
- New default paths (~/.mnemos/data, ~/.mnemos/vault, ~/.mnemos/logs)
- LoggingConfig defaults and overrides
- migrate_layout() — old → new path migration (idempotent, non-destructive)
- resolve_paths() — log_file resolution
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from vesma.config import (
    FederationConfig,
    LoggingConfig,
    PeerConfig,
    Settings,
    VesmaConfig,
    load_settings,
)

# ── New default paths ─────────────────────────────────────────────────────────


def test_default_vault_path_is_consolidated() -> None:
    """Default vault_path should be ~/.mnemos/vault, not ~/mnemos-vault."""
    cfg = VesmaConfig()
    assert str(cfg.vault_path) == "~/.mnemos/vault"


def test_default_data_dir_is_consolidated() -> None:
    """Default data_dir should be ~/.mnemos/data, not ~/.vesmaro."""
    cfg = VesmaConfig()
    assert str(cfg.data_dir) == "~/.mnemos/data"


def test_default_db_name_unchanged() -> None:
    """db_name stays 'mnemos.db' — it's now under data_dir."""
    cfg = VesmaConfig()
    assert cfg.db_name == "mnemos.db"


def test_db_path_resolves_under_data_dir(tmp_path: Path) -> None:
    """db_path property = data_dir / db_name."""
    settings = Settings()
    settings.vesma.data_dir = tmp_path / "data"
    settings.vesma.db_name = "test.db"
    assert settings.db_path == tmp_path / "data" / "test.db"


# ── LoggingConfig ─────────────────────────────────────────────────────────────


def test_logging_config_defaults() -> None:
    """LoggingConfig has sensible defaults."""
    cfg = LoggingConfig()
    assert cfg.level == "INFO"
    assert str(cfg.log_file) == "~/.mnemos/logs/mnemos.log"
    assert cfg.max_file_size_mb == 10
    assert cfg.backup_count == 3
    assert "%(asctime)s" in cfg.format
    assert "%Y-%m-%d" in cfg.date_format


def test_settings_has_logging_section() -> None:
    """Settings must include a logging config."""
    settings = Settings()
    assert isinstance(settings.logging, LoggingConfig)
    assert settings.logging.level == "INFO"


def test_resolve_paths_resolves_log_file(tmp_path: Path) -> None:
    """resolve_paths() should expanduser + resolve log_file."""
    settings = Settings()
    settings.logging.log_file = Path("~/some_test_log.log")
    settings.resolve_paths()
    assert settings.logging.log_file.is_absolute()
    assert "~" not in str(settings.logging.log_file)


def test_resolve_paths_empty_log_file(tmp_path: Path) -> None:
    """An empty log_file path means stderr-only — resolved to empty Path()."""
    settings = Settings()
    settings.logging.log_file = Path("")
    settings.resolve_paths()
    assert settings.logging.log_file == Path()


# ── Migration ─────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Patch Path.home() and HOME env to a tmp directory.

    Both must be patched because ``Path.expanduser()`` reads the ``HOME``
    environment variable, while ``migrate_layout()`` uses ``Path.home()``.
    """
    fake = tmp_path / "fake_home"
    fake.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake)
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.setenv("USERPROFILE", str(fake))  # Windows compat
    return fake


def test_migrate_layout_no_old_paths(fake_home: Path) -> None:
    """When no old paths exist, migration does nothing."""
    settings = Settings()
    settings.resolve_paths()
    actions = settings.migrate_layout()
    assert actions == []


def test_migrate_layout_moves_vault(fake_home: Path) -> None:
    """Old ~/mnemos-vault/ is moved to ~/.mnemos/vault/."""
    old_vault = fake_home / "mnemos-vault"
    old_vault.mkdir()
    (old_vault / "note.md").write_text("test note")

    settings = Settings()
    settings.resolve_paths()
    actions = settings.migrate_layout()

    assert len(actions) == 1
    assert "vault" in actions[0]
    assert not old_vault.exists()  # old moved
    new_vault = fake_home / ".mnemos" / "vault"
    assert new_vault.is_dir()
    assert (new_vault / "note.md").read_text() == "test note"


def test_migrate_layout_moves_data_files(fake_home: Path) -> None:
    """Old ~/.mnemos/mnemos.db is moved to ~/.mnemos/data/mnemos.db."""
    old_root = fake_home / ".mnemos"
    old_root.mkdir()
    (old_root / "mnemos.db").write_text("fake db")
    (old_root / "vectors.db").write_text("fake vectors")

    settings = Settings()
    settings.resolve_paths()
    actions = settings.migrate_layout()

    # Should have moved mnemos.db and vectors.db
    data_moved = [a for a in actions if a.startswith("data:")]
    assert len(data_moved) == 2
    new_data = fake_home / ".mnemos" / "data"
    assert (new_data / "mnemos.db").exists()
    assert (new_data / "vectors.db").exists()
    # Old files should be gone (moved, not copied)
    assert not (old_root / "mnemos.db").exists()


def test_migrate_layout_preserves_config_yaml(fake_home: Path) -> None:
    """config.yaml at ~/.mnemos/config.yaml stays in place during migration."""
    old_root = fake_home / ".mnemos"
    old_root.mkdir()
    (old_root / "config.yaml").write_text("vesma:\n  data_dir: ~/.mnemos/data\n")
    (old_root / "mnemos.db").write_text("fake db")

    settings = Settings()
    settings.resolve_paths()
    settings.migrate_layout()

    # config.yaml must remain at root, not moved into data/
    assert (old_root / "config.yaml").exists()
    assert not (fake_home / ".mnemos" / "data" / "config.yaml").exists()


def test_migrate_layout_idempotent(fake_home: Path) -> None:
    """Running migration twice does nothing the second time."""
    old_vault = fake_home / "mnemos-vault"
    old_vault.mkdir()
    (old_vault / "note.md").write_text("test")

    settings = Settings()
    settings.resolve_paths()
    actions1 = settings.migrate_layout()
    assert len(actions1) == 1

    # Second run — old path is gone, new path exists → no action
    actions2 = settings.migrate_layout()
    assert actions2 == []


def test_migrate_layout_does_not_overwrite_new(fake_home: Path) -> None:
    """If new path already exists, old path is NOT moved (no overwrite)."""
    old_vault = fake_home / "mnemos-vault"
    old_vault.mkdir()
    (old_vault / "old.md").write_text("old")

    new_vault = fake_home / ".mnemos" / "vault"
    new_vault.mkdir(parents=True)
    (new_vault / "new.md").write_text("new")

    settings = Settings()
    settings.resolve_paths()
    actions = settings.migrate_layout()

    # Vault should NOT be moved because new already exists
    vault_actions = [a for a in actions if a.startswith("vault:")]
    assert vault_actions == []
    assert (new_vault / "new.md").read_text() == "new"
    assert old_vault.exists()  # old still there, untouched


def test_migrate_layout_skips_custom_data_dir(fake_home: Path) -> None:
    """If data_dir was overridden to a custom path, migration is skipped."""
    old_root = fake_home / ".mnemos"
    old_root.mkdir()
    (old_root / "mnemos.db").write_text("fake db")

    custom_data = fake_home / "custom_data"
    settings = Settings()
    settings.vesma.data_dir = custom_data
    settings.resolve_paths()
    actions = settings.migrate_layout()

    # No data migration because data_dir is not the default ~/.mnemos/data
    data_actions = [a for a in actions if a.startswith("data:")]
    assert data_actions == []
    assert (old_root / "mnemos.db").exists()  # old untouched


# ── load_settings integration ─────────────────────────────────────────────────


def test_load_settings_calls_migrate_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_settings() should call migrate_layout() automatically."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setenv("HOME", str(fake_home))

    # Create a config file so load_settings finds it
    cfg = fake_home / ".mnemos" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("vesma:\n  data_dir: ~/.mnemos/data\n")

    # Patch migrate_layout to track the call
    with patch.object(Settings, "migrate_layout", return_value=[]) as mock_migrate:
        load_settings()
        mock_migrate.assert_called_once()


# ── Federation Phase 1 prerequisites — per-peer ACL ──────────────────────────
# Contract §3.2, §6, ADR-0016. All fixtures use RFC-reserved dummy values
# per sensitive-data.instructions.md — never real tokens.

# Dummy env var NAME (not value) per sensitive-data.instructions.md.
_DUMMY_TOKEN_ENV = "VESMA_FED_PEER_A_TOKEN"
# Dummy SHA-256 fingerprint (SHA-256 of empty string).
_DUMMY_FINGERPRINT = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_federation_config_peers_defaults_to_empty_dict() -> None:
    """FederationConfig.peers defaults to an empty dict — fail-closed."""
    cfg = FederationConfig()
    assert cfg.peers == {}
    assert isinstance(cfg.peers, dict)


def test_federation_config_peers_accepts_peer_config() -> None:
    """A PeerConfig can be added under a peer A2A id key."""
    cfg = FederationConfig(
        peers={"mnemos-A": PeerConfig(bearer_token_env=_DUMMY_TOKEN_ENV)},
    )
    assert "mnemos-A" in cfg.peers
    assert cfg.peers["mnemos-A"].bearer_token_env == _DUMMY_TOKEN_ENV


def test_peer_config_bearer_token_env_required() -> None:
    """PeerConfig.bearer_token_env is required (no default)."""
    with pytest.raises(ValidationError):
        PeerConfig()  # type: ignore[call-arg]


def test_peer_config_bearer_token_env_is_a_name_not_value() -> None:
    """bearer_token_env holds an env var NAME, never a token value."""
    cfg = PeerConfig(bearer_token_env=_DUMMY_TOKEN_ENV)
    # It is a string naming the env var, not a bearer token itself.
    assert cfg.bearer_token_env == _DUMMY_TOKEN_ENV
    assert not cfg.bearer_token_env.startswith("mnk_fed_")


def test_peer_config_bearer_token_env_rejects_empty() -> None:
    """Empty bearer_token_env is rejected — a peer needs a real env name."""
    with pytest.raises(ValidationError):
        PeerConfig(bearer_token_env="")


def test_peer_config_allowed_projects_defaults_empty() -> None:
    """allowed_projects defaults to empty — fail-closed (peer gets nothing)."""
    cfg = PeerConfig(bearer_token_env=_DUMMY_TOKEN_ENV)
    assert cfg.allowed_projects == []


def test_peer_config_allowed_projects_wildcard_accepted() -> None:
    """allowed_projects=['*'] is accepted as the explicit wildcard."""
    cfg = PeerConfig(bearer_token_env=_DUMMY_TOKEN_ENV, allowed_projects=["*"])
    assert cfg.allowed_projects == ["*"]


def test_peer_config_allowed_projects_explicit_list() -> None:
    """An explicit list of project slugs is accepted."""
    cfg = PeerConfig(
        bearer_token_env=_DUMMY_TOKEN_ENV,
        allowed_projects=["mnemos", "project-umbra"],
    )
    assert cfg.allowed_projects == ["mnemos", "project-umbra"]


def test_peer_config_allowed_types_defaults_empty() -> None:
    """allowed_types defaults to empty — fail-closed."""
    cfg = PeerConfig(bearer_token_env=_DUMMY_TOKEN_ENV)
    assert cfg.allowed_types == []


def test_peer_config_allowed_types_wildcard_accepted() -> None:
    """allowed_types=['*'] is accepted as the explicit wildcard."""
    cfg = PeerConfig(bearer_token_env=_DUMMY_TOKEN_ENV, allowed_types=["*"])
    assert cfg.allowed_types == ["*"]


def test_peer_config_rate_limit_default() -> None:
    """rate_limit_per_minute defaults to 30 (contract §8)."""
    cfg = PeerConfig(bearer_token_env=_DUMMY_TOKEN_ENV)
    assert cfg.rate_limit_per_minute == 30


def test_peer_config_rate_limit_clamped_low() -> None:
    """rate_limit_per_minute below 1 is rejected."""
    with pytest.raises(ValidationError):
        PeerConfig(bearer_token_env=_DUMMY_TOKEN_ENV, rate_limit_per_minute=0)


def test_peer_config_rate_limit_clamped_high() -> None:
    """rate_limit_per_minute above 600 is rejected."""
    with pytest.raises(ValidationError):
        PeerConfig(bearer_token_env=_DUMMY_TOKEN_ENV, rate_limit_per_minute=601)


def test_peer_config_mtls_fingerprint_optional() -> None:
    """mtls_cert_fingerprint is optional — defaults to None (no pinning)."""
    cfg = PeerConfig(bearer_token_env=_DUMMY_TOKEN_ENV)
    assert cfg.mtls_cert_fingerprint is None


def test_peer_config_mtls_fingerprint_accepted() -> None:
    """A SHA-256 fingerprint hex string is accepted when provided."""
    cfg = PeerConfig(
        bearer_token_env=_DUMMY_TOKEN_ENV,
        mtls_cert_fingerprint=_DUMMY_FINGERPRINT,
    )
    assert cfg.mtls_cert_fingerprint == _DUMMY_FINGERPRINT


def test_settings_federation_peers_default_empty() -> None:
    """Top-level Settings.federation.peers defaults to empty — fail-closed."""
    settings = Settings()
    assert settings.federation.peers == {}
