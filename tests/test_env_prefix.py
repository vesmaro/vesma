"""Canonical env prefix contract — ``VESMA_`` only.

6.0.0 ends the #471 dual-read period: ``VESMA_<SECTION>__<FIELD>`` is the
only honoured spelling. The deprecated ``VESMARO_*``/``MNEMOS_*`` names are
IGNORED — an export of only a deprecated name falls through to the field
default (or errors for required fields), never to a silent legacy read
(see ``Settings.settings_customise_sources`` for the full precedence chain).

All env manipulation is in-process (``monkeypatch``) — no test here may
rely on shell env in a subprocess.
"""

from __future__ import annotations

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
    """Strip every prefix name this module touches, canonical + retired."""
    for name in (
        "VESMA_MNEMOS__DATA_DIR",
        "VESMA_MNEMOS__VAULT_PATH",
        "VESMARO_MNEMOS__DATA_DIR",
        "VESMARO_MNEMOS__VAULT_PATH",
        "VESMA_DATA_DIR",
        "VESMA_VAULT__VAULT_PATH",
        "VESMARO_DATA_DIR",
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


class TestCanonicalPrefixResolves:
    def test_nested_mnemos_section(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv("VESMA_MNEMOS__DATA_DIR", "/vesma-canonical-data")
        settings = load_settings(config_path=_no_config(tmp_path))
        assert settings.mnemos.data_dir == Path("/vesma-canonical-data")

    def test_other_nested_sections_and_type_coercion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``VESMA_<SECTION>__<FIELD>`` works for every section, with the same
        pydantic coercion the settings machinery always had."""
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
        the prefix must live on the class, not only in load_settings."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("VESMA_MNEMOS__DATA_DIR", "/vesma-direct-data")
        settings = Settings(_env_file=None)
        assert settings.mnemos.data_dir == Path("/vesma-direct-data")


# ── Deprecated spellings are IGNORED (6.0.0 retirement) ─────────────────────


class TestDeprecatedSpellingsIgnored:
    def test_deprecated_prefixed_env_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only ``VESMARO_*`` exported → fields fall through to defaults, NOT
        to the legacy values (the #471 dual read is over)."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("VESMARO_MNEMOS__DATA_DIR", "/vesmaro-retired")
        monkeypatch.setenv("VESMARO_API__PORT", "9443")
        settings = load_settings(config_path=_no_config(tmp_path))
        assert settings.mnemos.data_dir == (Path.home() / ".mnemos" / "data").resolve()
        assert settings.api.port != 9443

    def test_deprecated_short_alias_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv("VESMARO_DATA_DIR", "/vesmaro-139-retired")
        settings = load_settings(config_path=_no_config(tmp_path))
        assert settings.mnemos.data_dir == (Path.home() / ".mnemos" / "data").resolve()

    def test_canonical_wins_when_deprecated_also_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv("VESMARO_MNEMOS__DATA_DIR", "/vesmaro-retired")
        monkeypatch.setenv("VESMA_MNEMOS__DATA_DIR", "/vesma-canonical")
        settings = load_settings(config_path=_no_config(tmp_path))
        assert settings.mnemos.data_dir == Path("/vesma-canonical")

    def test_deprecated_config_path_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``VESMARO_CONFIG`` no longer selects the config file (the search
        falls through to the default candidates instead)."""
        _clear_env(monkeypatch)
        legacy = tmp_path / "legacy.yaml"
        legacy.write_text("mnemos:\n  data_dir: /vesmaro-config-only\n", encoding="utf-8")
        monkeypatch.setenv("VESMARO_CONFIG", str(legacy))
        assert find_config_file() != legacy


# ── #139 short aliases (canonical ``VESMA_`` spellings) ─────────────────────


class TestShortAliases:
    def test_short_alias_resolves(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv("VESMA_DATA_DIR", "/vesma-alias-data")
        settings = load_settings(config_path=_no_config(tmp_path))
        assert settings.mnemos.data_dir == Path("/vesma-alias-data")

    def test_short_alias_never_beats_canonical_nested_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv("VESMA_DATA_DIR", "/vesma-alias")
        monkeypatch.setenv("VESMA_MNEMOS__DATA_DIR", "/vesma-canonical")
        settings = load_settings(config_path=_no_config(tmp_path))
        assert settings.mnemos.data_dir == Path("/vesma-canonical")

    def test_config_file_still_beats_canonical_env_and_alias(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-existing contract preserved: init kwargs (the YAML config file)
        outrank every env source for the same field."""
        _clear_env(monkeypatch)
        config = tmp_path / "config.yaml"
        config.write_text("mnemos:\n  data_dir: /from-config-file\n", encoding="utf-8")
        monkeypatch.setenv("VESMA_MNEMOS__DATA_DIR", "/vesma-canonical")
        monkeypatch.setenv("VESMA_DATA_DIR", "/vesma-alias")
        settings = load_settings(config_path=config)
        assert settings.mnemos.data_dir == Path("/from-config-file")

    def test_fields_coexist_across_sources(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deep-merge: aliased data_dir + canonical port both apply (per-field
        precedence, whole-source replacement never happens)."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("VESMA_DATA_DIR", "/vesma-alias")
        monkeypatch.setenv("VESMA_API__PORT", "9999")
        settings = load_settings(config_path=_no_config(tmp_path))
        assert settings.mnemos.data_dir == Path("/vesma-alias")
        assert settings.api.port == 9999


# ── .env file layer ──────────────────────────────────────────────────────────


class TestDotenvLayer:
    def test_vesma_dotenv_entry_respects_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In a .env file: canonical entries apply; deprecated entries are
        ignored; process env beats the file; unknown keys no longer crash
        Settings (dotenv_filtering)."""
        _clear_env(monkeypatch)
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text(
            "VESMA_MNEMOS__DATA_DIR=/dotenv-vesma\n"
            "VESMARO_MNEMOS__DATA_DIR=/dotenv-retired\n"
            "UNRELATED_KEY=ignore-me\n",
            encoding="utf-8",
        )
        settings = Settings()
        assert settings.mnemos.data_dir == Path("/dotenv-vesma")

        monkeypatch.setenv("VESMA_MNEMOS__DATA_DIR", "/process-env-wins")
        assert Settings().mnemos.data_dir == Path("/process-env-wins")


# ── Config-file path: VESMA_CONFIG ──────────────────────────────────────────


class TestConfigPathEnv:
    @staticmethod
    def _config(tmp_path: Path, name: str, data_dir: str) -> Path:
        config = tmp_path / name
        config.write_text(f"mnemos:\n  data_dir: {data_dir}\n", encoding="utf-8")
        return config

    def test_vesma_config_resolves(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        canonical = self._config(tmp_path, "canonical.yaml", "/vesma-config")
        monkeypatch.setenv("VESMA_CONFIG", str(canonical))
        assert find_config_file() == canonical
        settings = load_settings()
        assert settings.mnemos.data_dir == Path("/vesma-config")


# ── Doctor reports the same file the loader would use ───────────────────────


class TestDoctorConfigPath:
    def test_doctor_prefers_vesma_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from vesmaro.cli import doctor

        canonical = TestConfigPathEnv._config(tmp_path, "canonical.yaml", "/vesma-config")
        monkeypatch.setenv("VESMA_CONFIG", str(canonical))
        result = doctor._check_config()
        assert result.status == doctor.CheckStatus.PASS
        assert str(canonical) in result.detail
