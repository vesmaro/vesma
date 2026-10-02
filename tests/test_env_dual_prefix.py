"""Dual env-prefix contract — canonical ``VESMA_`` over deprecated ``VESMARO_``.

Rebrand train 5.3.0 (owner directive: the product end-state is ``vesma``-
branded; removal of ``VESMARO_*``/``MNEMOS_*`` is deferred to 6.0). The
canonical ``VESMA_<SECTION>__<FIELD>`` prefix must resolve and take
precedence over its ``VESMARO_`` twin, which keeps working exactly as
before when the ``VESMA_`` twin is absent (zero behaviour change for
existing deployments — see ``Settings.settings_customise_sources`` for the
full precedence chain).

All env manipulation is in-process (``monkeypatch``) — no test here may
rely on shell env in a subprocess.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import vesmaro
from vesmaro.config import Settings, find_config_file, load_settings

# ── Import-path guard (same rationale as test_env_compat.py) ────────────────

_REPO_SRC = (Path(__file__).resolve().parent.parent / "src").resolve()
_VESMARO_UNDER_REPO_SRC = str(_REPO_SRC) in str(Path(vesmaro.__file__).resolve())

pytestmark = pytest.mark.skipif(
    not _VESMARO_UNDER_REPO_SRC,
    reason="vesmaro resolves to a foreign install; run the suite against the "
    "repo src tree (editable install)",
)


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every dual-prefix name this module touches, both spellings."""
    for name in (
        "VESMA_MNEMOS__DATA_DIR",
        "VESMA_MNEMOS__VAULT_PATH",
        "VESMARO_MNEMOS__DATA_DIR",
        "VESMARO_MNEMOS__VAULT_PATH",
        "VESMA_DATA_DIR",
        "VESMARO_DATA_DIR",
        "VESMA_VAULT__VAULT_PATH",
        "VESMARO_VAULT__VAULT_PATH",
        "VESMA_API__PORT",
        "VESMARO_API__PORT",
        "VESMA_SEARCH__DEFAULT_LIMIT",
        "VESMARO_SEARCH__DEFAULT_LIMIT",
        "VESMA_CONFIG",
        "VESMARO_CONFIG",
    ):
        monkeypatch.delenv(name, raising=False)


def _no_config(tmp_path: Path) -> Path:
    """A config path that does not exist — keeps load_settings off the real
    ``~/.mnemos/config.yaml`` and ``./config.yaml``."""
    return tmp_path / "no-config.yaml"


# ── Canonical VESMA_ prefix resolves ────────────────────────────────────────


class TestVesmaCanonicalResolves:
    def test_nested_mnemos_section(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv("VESMA_MNEMOS__DATA_DIR", "/vesma-canonical-data")
        settings = load_settings(config_path=_no_config(tmp_path))
        assert settings.mnemos.data_dir == Path("/vesma-canonical-data")

    def test_other_nested_sections_and_type_coercion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``VESMA_<SECTION>__<FIELD>`` works for every section, with the same
        pydantic coercion the ``VESMARO_`` prefix always had."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("VESMA_API__PORT", "9999")
        monkeypatch.setenv("VESMA_SEARCH__DEFAULT_LIMIT", "7")
        settings = load_settings(config_path=_no_config(tmp_path))
        assert settings.api.port == 9999
        assert settings.search.default_limit == 7

    def test_direct_settings_construction_honours_vesma(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Isolation helpers build ``Settings()`` directly (no load_settings);
        the canonical source must live on the class, not only in
        load_settings."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("VESMA_MNEMOS__DATA_DIR", "/vesma-direct-data")
        settings = Settings(_env_file=None)
        assert settings.mnemos.data_dir == Path("/vesma-direct-data")


# ── Precedence: VESMA_ wins over VESMARO_ for the same field ────────────────


class TestVesmaWinsOverVesmaro:
    def test_canonical_env_beats_deprecated_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv("VESMARO_MNEMOS__DATA_DIR", "/vesmaro-legacy")
        monkeypatch.setenv("VESMA_MNEMOS__DATA_DIR", "/vesma-canonical")
        settings = load_settings(config_path=_no_config(tmp_path))
        assert settings.mnemos.data_dir == Path("/vesma-canonical")

    def test_legacy_still_resolves_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Zero-change guard: a deployment exporting only VESMARO_* keeps
        working exactly as before the canonical prefix existed."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("VESMARO_MNEMOS__DATA_DIR", "/vesmaro-legacy-only")
        monkeypatch.setenv("VESMARO_API__PORT", "9443")
        settings = load_settings(config_path=_no_config(tmp_path))
        assert settings.mnemos.data_dir == Path("/vesmaro-legacy-only")
        assert settings.api.port == 9443

    def test_canonical_env_beats_legacy_short_alias(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv("VESMARO_DATA_DIR", "/vesmaro-139-alias")
        monkeypatch.setenv("VESMA_MNEMOS__DATA_DIR", "/vesma-canonical")
        settings = load_settings(config_path=_no_config(tmp_path))
        assert settings.mnemos.data_dir == Path("/vesma-canonical")

    def test_config_file_still_beats_canonical_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-existing contract preserved: init kwargs (the YAML config file)
        outrank BOTH env prefixes for the same field."""
        _clear_env(monkeypatch)
        config = tmp_path / "config.yaml"
        config.write_text("mnemos:\n  data_dir: /from-config-file\n", encoding="utf-8")
        monkeypatch.setenv("VESMA_MNEMOS__DATA_DIR", "/vesma-canonical")
        monkeypatch.setenv("VESMARO_MNEMOS__DATA_DIR", "/vesmaro-legacy")
        settings = load_settings(config_path=config)
        assert settings.mnemos.data_dir == Path("/from-config-file")

    def test_fields_coexist_across_prefixes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deep-merge: canonical port + legacy data_dir both apply (per-field
        precedence, whole-source replacement never happens)."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("VESMARO_MNEMOS__DATA_DIR", "/vesmaro-legacy")
        monkeypatch.setenv("VESMA_API__PORT", "9999")
        settings = load_settings(config_path=_no_config(tmp_path))
        assert settings.mnemos.data_dir == Path("/vesmaro-legacy")
        assert settings.api.port == 9999


# ── #139 short aliases: VESMA_ twin over legacy VESMARO_ name ───────────────


class TestAliasTwins:
    def test_vesma_short_alias_resolves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv("VESMA_DATA_DIR", "/vesma-alias-data")
        settings = load_settings(config_path=_no_config(tmp_path))
        assert settings.mnemos.data_dir == Path("/vesma-alias-data")

    def test_vesma_short_alias_wins_over_legacy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv("VESMARO_DATA_DIR", "/vesmaro-139-alias")
        monkeypatch.setenv("VESMA_DATA_DIR", "/vesma-alias")
        settings = load_settings(config_path=_no_config(tmp_path))
        assert settings.mnemos.data_dir == Path("/vesma-alias")

    def test_legacy_short_alias_still_resolves_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv("VESMARO_DATA_DIR", "/vesmaro-139-alias-only")
        settings = load_settings(config_path=_no_config(tmp_path))
        assert settings.mnemos.data_dir == Path("/vesmaro-139-alias-only")


# ── .env file layer ──────────────────────────────────────────────────────────


class TestDotenvLayer:
    def test_vesma_dotenv_entry_respects_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In a .env file: VESMA_ wins over VESMARO_; process env wins over
        both; unknown keys no longer crash Settings (dotenv_filtering)."""
        _clear_env(monkeypatch)
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text(
            "VESMA_MNEMOS__DATA_DIR=/dotenv-vesma\n"
            "VESMARO_MNEMOS__DATA_DIR=/dotenv-vesmaro\n"
            "VESMARO_MNEMOS__VAULT_PATH=/dotenv-legacy-vault\n"
            "UNRELATED_KEY=ignore-me\n",
            encoding="utf-8",
        )
        settings = Settings()
        assert settings.mnemos.data_dir == Path("/dotenv-vesma")
        # legacy twin fills the field the canonical spelling does not set
        assert settings.mnemos.vault_path == Path("/dotenv-legacy-vault")

        monkeypatch.setenv("VESMA_MNEMOS__DATA_DIR", "/process-env-wins")
        assert Settings().mnemos.data_dir == Path("/process-env-wins")


# ── Config-file path: VESMA_CONFIG over VESMARO_CONFIG ──────────────────────


class TestConfigPathEnv:
    @staticmethod
    def _config(tmp_path: Path, name: str, data_dir: str) -> Path:
        config = tmp_path / name
        config.write_text(f"mnemos:\n  data_dir: {data_dir}\n", encoding="utf-8")
        return config

    def test_vesma_config_wins_over_vesmaro_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        canonical = self._config(tmp_path, "canonical.yaml", "/vesma-config")
        legacy = self._config(tmp_path, "legacy.yaml", "/vesmaro-config")
        monkeypatch.setenv("VESMA_CONFIG", str(canonical))
        monkeypatch.setenv("VESMARO_CONFIG", str(legacy))
        assert find_config_file() == canonical
        settings = load_settings()
        assert settings.mnemos.data_dir == Path("/vesma-config")

    def test_vesmaro_config_still_works_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        legacy = self._config(tmp_path, "legacy.yaml", "/vesmaro-config-only")
        monkeypatch.setenv("VESMARO_CONFIG", str(legacy))
        monkeypatch.delenv("VESMA_CONFIG", raising=False)
        assert find_config_file() == legacy
        settings = load_settings()
        assert settings.mnemos.data_dir == Path("/vesmaro-config-only")


# ── Doctor reports the same file the loader would use ───────────────────────


class TestDoctorConfigPath:
    def test_doctor_prefers_vesma_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from vesmaro.cli import doctor

        canonical = self._config(tmp_path, "canonical.yaml", "/vesma-config")
        legacy = self._config(tmp_path, "legacy.yaml", "/vesmaro-config")
        monkeypatch.setenv("VESMA_CONFIG", str(canonical))
        monkeypatch.setenv("VESMARO_CONFIG", str(legacy))
        result = doctor._check_config()
        assert result.status == doctor.CheckStatus.PASS
        assert str(canonical) in result.detail

    @staticmethod
    def _config(tmp_path: Path, name: str, data_dir: str) -> Path:
        config = tmp_path / name
        config.write_text(f"mnemos:\n  data_dir: {data_dir}\n", encoding="utf-8")
        return config


# ── Deprecation notice surfaces ─────────────────────────────────────────────


class TestDeprecationNotice:
    def test_jev_deprecated_key_name_logs_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Canonical key env unset + deprecated twin set → works, but warns
        (no silent swap). Lives here so the whole VESMA_/VESMARO_ story is
        readable from one module; the jev-specific pins stay in
        test_decision_jev.py."""
        from vesmaro.config import VesmaConfig
        from vesmaro.decision_jev import resolve_decision_provider

        monkeypatch.delenv("VESMA_OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv("VESMARO_OPENROUTER_API_KEY", "test-key")
        with caplog.at_level(logging.WARNING, logger="vesmaro.decision_jev"):
            provider = resolve_decision_provider(VesmaConfig(decision_provider="jev"))
        assert provider is not None
        assert any("DEPRECATED-ENV" in rec.message for rec in caplog.records)
