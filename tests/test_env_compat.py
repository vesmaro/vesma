"""Short env-name compatibility (issue #139).

``VESMA_DATA_DIR`` / ``VESMA_VAULT__VAULT_PATH`` (the names documented
across the repo and written into user configs by ``scripts/mcp-setup.sh``)
must map to the nested ``Settings.vesma`` fields ``data_dir`` /
``vault_path``. Without the shim, pydantic-settings silently ignores them
(canonical nested form is ``VESMA_VESMA__DATA_DIR`` /
``VESMA_VESMA__VAULT_PATH``). The pre-5.3 ``VESMARO_`` spellings are
retired in 6.0.0 and covered by tests/test_env_prefix.py (ignored-case).

Precedence contract under test (high → low, per field — see
``Settings.settings_customise_sources``):

1. explicit config-file value (``load_settings`` passes YAML as init kwargs,
   and pydantic-settings gives init kwargs priority over env sources),
2. canonical ``VESMA_VESMA__*`` env var,
3. short alias (``VESMA_DATA_DIR`` / ``VESMA_VAULT__VAULT_PATH``),
4. ``.env`` file, 5. field defaults.

All env manipulation is in-process (``monkeypatch``) — the dev sandbox strips
``VESMA_*`` assignments from spawned subprocess commands, so no test here may
rely on shell env in a subprocess.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import vesma
from vesma.config import Settings, load_settings

# ── Import-path guard ────────────────────────────────────────────────────────
#
# Plain ``pytest`` on some dev machines resolves ``vesma`` from a user-site
# install that predates the #139 shim; these tests would then false-fail.
# Skip loudly instead — the suite must run against the repo src tree
# (editable install or explicit ``sys.path`` bootstrap).

_REPO_SRC = (Path(__file__).resolve().parent.parent / "src").resolve()
_VESMARO_UNDER_REPO_SRC = str(_REPO_SRC) in str(Path(vesma.__file__).resolve())

pytestmark = pytest.mark.skipif(
    not _VESMARO_UNDER_REPO_SRC,
    reason="vesma resolves to a foreign install predating the #139 shim; "
    "run the suite against the repo src tree (editable install)",
)

#: Every env name the shim accepts, tied to the nested field it feeds.
EXPECTED_ALIASES = {
    "VESMA_DATA_DIR": "data_dir",
    "VESMA_VAULT__VAULT_PATH": "vault_path",
}

_CANONICAL = {
    "data_dir": "VESMA_VESMA__DATA_DIR",
    "vault_path": "VESMA_VESMA__VAULT_PATH",
}


def _clear_compat_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all four names (aliases + canonical) for hermetic defaults."""
    for alias in EXPECTED_ALIASES:
        monkeypatch.delenv(alias, raising=False)
    for canonical in _CANONICAL.values():
        monkeypatch.delenv(canonical, raising=False)


def _no_config(tmp_path: Path) -> Path:
    """A config path that does not exist — keeps load_settings off the real
    ``~/.mnemos/config.yaml`` and ``./config.yaml``."""
    return tmp_path / "no-config.yaml"


# ── Short alias is honoured ─────────────────────────────────────────────────


class TestShortAliasApplied:
    def test_data_dir_alias_via_load_settings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VESMA_DATA_DIR", "/mnemos-139-alias-data")
        settings = load_settings(config_path=_no_config(tmp_path))
        assert settings.vesma.data_dir == Path("/mnemos-139-alias-data")

    def test_vault_alias_via_load_settings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VESMA_VAULT__VAULT_PATH", "/mnemos-139-alias-vault")
        settings = load_settings(config_path=_no_config(tmp_path))
        assert settings.vesma.vault_path == Path("/mnemos-139-alias-vault")

    def test_direct_settings_construction_honours_alias(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Isolation helpers build ``Settings()`` directly (no load_settings);
        the shim must live on the class, not only in load_settings."""
        monkeypatch.setenv("VESMA_DATA_DIR", "/mnemos-139-direct-data")
        monkeypatch.setenv("VESMA_VAULT__VAULT_PATH", "/mnemos-139-direct-vault")
        settings = Settings(_env_file=None)
        assert settings.vesma.data_dir == Path("/mnemos-139-direct-data")
        assert settings.vesma.vault_path == Path("/mnemos-139-direct-vault")


# ── Canonical name stays authoritative ──────────────────────────────────────


class TestCanonicalWins:
    def test_canonical_beats_alias_same_field(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VESMA_DATA_DIR", "/mnemos-139-alias-data")
        monkeypatch.setenv("VESMA_VESMA__DATA_DIR", "/mnemos-139-canon-data")
        settings = load_settings(config_path=_no_config(tmp_path))
        assert settings.vesma.data_dir == Path("/mnemos-139-canon-data")

    def test_canonical_and_alias_coexist_on_different_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sources deep-merge: canonical vault + aliased data_dir both apply."""
        monkeypatch.setenv("VESMA_DATA_DIR", "/mnemos-139-alias-data")
        monkeypatch.setenv("VESMA_VESMA__VAULT_PATH", "/mnemos-139-canon-vault")
        settings = load_settings(config_path=_no_config(tmp_path))
        assert settings.vesma.data_dir == Path("/mnemos-139-alias-data")
        assert settings.vesma.vault_path == Path("/mnemos-139-canon-vault")


# ── Config-file interaction (parity with canonical env semantics) ───────────


class TestConfigFileInteraction:
    @staticmethod
    def _write_config(tmp_path: Path, data_dir: str) -> Path:
        config = tmp_path / "config.yaml"
        config.write_text(f"vesma:\n  data_dir: {data_dir}\n", encoding="utf-8")
        return config

    def test_file_value_beats_alias(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A short alias never overrides an explicit config-file value —
        matching how the canonical name already loses to init kwargs."""
        config = self._write_config(tmp_path, "/mnemos-139-file-data")
        monkeypatch.setenv("VESMA_DATA_DIR", "/mnemos-139-alias-data")
        settings = load_settings(config_path=config)
        assert settings.vesma.data_dir == Path("/mnemos-139-file-data")

    def test_file_value_beats_canonical_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Documents the pre-existing semantics the alias mirrors: config
        file (init kwargs) outranks env sources for the same field."""
        config = self._write_config(tmp_path, "/mnemos-139-file-data")
        monkeypatch.setenv("VESMA_VESMA__DATA_DIR", "/mnemos-139-canon-data")
        settings = load_settings(config_path=config)
        assert settings.vesma.data_dir == Path("/mnemos-139-file-data")

    def test_alias_fills_field_the_file_leaves_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """File sets data_dir but not vault_path → the alias supplies vault."""
        config = self._write_config(tmp_path, "/mnemos-139-file-data")
        monkeypatch.setenv("VESMA_VAULT__VAULT_PATH", "/mnemos-139-alias-vault")
        settings = load_settings(config_path=config)
        assert settings.vesma.data_dir == Path("/mnemos-139-file-data")
        assert settings.vesma.vault_path == Path("/mnemos-139-alias-vault")


# ── Defaults and edge cases ─────────────────────────────────────────────────


class TestDefaultsAndEdges:
    def test_neither_set_falls_back_to_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_compat_env(monkeypatch)
        settings = load_settings(config_path=_no_config(tmp_path))
        home = Path.home()
        assert settings.vesma.data_dir == (home / ".mnemos" / "data").resolve()
        assert settings.vesma.vault_path == (home / ".mnemos" / "vault").resolve()

    def test_empty_alias_is_treated_as_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_compat_env(monkeypatch)
        monkeypatch.setenv("VESMA_DATA_DIR", "")
        settings = load_settings(config_path=_no_config(tmp_path))
        assert settings.vesma.data_dir == (Path.home() / ".mnemos" / "data").resolve()


# ── Drift guard: mcp-setup.sh writes exactly the shimmed names ─────────────


class TestMcpSetupDrift:
    def test_setup_script_names_are_shimmed(self) -> None:
        """scripts/mcp-setup.sh writes ``VESMA_DATA_DIR`` and
        ``VESMA_VAULT__VAULT_PATH`` into user mcp.json — every name it
        writes must be accepted by the shim (issue #139 scenario)."""
        from vesma.config import _ENV_COMPAT_ALIASES

        script = (Path(__file__).resolve().parent.parent / "scripts" / "mcp-setup.sh").read_text(
            encoding="utf-8"
        )
        assert set(_ENV_COMPAT_ALIASES) == set(EXPECTED_ALIASES)
        for alias in EXPECTED_ALIASES:
            assert f'"{alias}"' in script, f"mcp-setup.sh no longer writes {alias}"
