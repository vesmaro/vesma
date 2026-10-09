"""Home-flip tests — the canonical home is ``~/.vesma`` (owner 2026-10-08).

Covers the four flip guards the 6.0.x line shipped without:

* **Defaults** — the zero-config profile resolves every path under
  ``~/.vesma`` with ``vesma.db`` (config search, vault, data, logs).
* **Fork refusal** (ADR-0044 gate 5) — the pure zero-config profile
  REFUSES with a typed error + the one-line ``vesma migrate-store`` hint
  while a substantial legacy ``~/.mnemos`` home exists and the canonical
  home is fresh. Explicit config choices (argument, ``VESMA_CONFIG``,
  ``./config.yaml``) are never refused.
* **Legacy config diagnostics** — a config file carrying the 5.x
  ``mnemos:`` section (or old-era flat keys like a top-level
  ``graph_walk``) raises the typed ``LegacyConfigError`` with the hint —
  never the raw pydantic ``extra_forbidden`` traceback; doctor surfaces
  the same text under a stable diagnostic code.
* **Update hint** — ``vesma update`` check/apply output carries the same
  one-line migration suggestion when the legacy home has content.

Every test runs in an isolated HOME; the real machine home is never read
for store content and never written.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from vesma.cli import _manager as cli_manager_module
from vesma.cli.doctor import _check_config
from vesma.cli.main import app as cli_app
from vesma.cli.update_cmd import _legacy_home_hint
from vesma.config import (
    MIGRATE_HINT,
    LegacyConfigError,
    LegacyStoreForkRefused,
    canonical_home_is_fresh,
    find_config_file,
    legacy_home_is_substantial,
    load_settings,
)
from vesma.scanner_runtime import reset_scanner

runner = CliRunner()


def _streams(result: object) -> str:
    """stdout + stderr of a CliRunner invocation (streams split on click 8.2+),
    whitespace-normalized so rich line-wrapping cannot break substring pins."""
    merged = getattr(result, "output", "") or ""
    with contextlib.suppress(ValueError):
        merged += getattr(result, "stderr", "") or ""
    return " ".join(merged.split())


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HOME: fresh tmp home, no VESMA_* env, empty cwd.

    Both ``Path.home()`` and ``HOME`` are patched (expanduser vs Path.home),
    and every VESMA_* variable is cleared so the zero-config profile is
    actually exercised regardless of the developer shell / gate env.
    """
    for key in list(os.environ):
        if key.startswith("VESMA_"):
            monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)  # no ./config.yaml in cwd
    cli_manager_module._manager = None
    reset_scanner()
    yield home
    cli_manager_module._manager = None
    reset_scanner()


def _seed_legacy_store(home: Path) -> Path:
    """A minimal-but-substantial legacy 5.x home: data/mnemos.db."""
    legacy = home / ".mnemos"
    (legacy / "data").mkdir(parents=True)
    (legacy / "data" / "mnemos.db").write_bytes(b"")  # marker only
    return legacy


# ── Defaults: the zero-config profile lives under ~/.vesma ────────────────────


class TestCanonicalDefaults:
    def test_find_config_file_searches_vesma_home(self, fake_home: Path) -> None:
        cfg = fake_home / ".vesma" / "config.yaml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("api:\n  port: 9999\n", encoding="utf-8")
        assert find_config_file() == cfg
        assert load_settings().api.port == 9999

    def test_zero_config_defaults_resolve_under_vesma(self, fake_home: Path) -> None:
        settings = load_settings()
        assert settings.vesma.vault_path == fake_home / ".vesma" / "vault"
        assert settings.vesma.data_dir == fake_home / ".vesma" / "data"
        assert settings.db_path == fake_home / ".vesma" / "data" / "vesma.db"
        assert settings.logging.log_file == fake_home / ".vesma" / "logs" / "vesma.log"


# ── Substantial-content + freshness detectors ─────────────────────────────────


class TestDetectors:
    def test_legacy_home_with_db_is_substantial(self, fake_home: Path) -> None:
        _seed_legacy_store(fake_home)
        assert legacy_home_is_substantial() is True

    def test_legacy_home_config_only_is_substantial(self, fake_home: Path) -> None:
        legacy = fake_home / ".mnemos"
        legacy.mkdir()
        (legacy / "config.yaml").write_text("mnemos:\n  db_name: mnemos.db\n", encoding="utf-8")
        assert legacy_home_is_substantial() is True

    def test_legacy_home_vault_only_is_substantial(self, fake_home: Path) -> None:
        legacy = fake_home / ".mnemos"
        (legacy / "vault").mkdir(parents=True)
        (legacy / "vault" / "note.md").write_text("x", encoding="utf-8")
        assert legacy_home_is_substantial() is True

    def test_legacy_home_logs_only_is_not_substantial(self, fake_home: Path) -> None:
        legacy = fake_home / ".mnemos"
        (legacy / "logs").mkdir(parents=True)
        (legacy / "logs" / "vesma.log").write_text("log", encoding="utf-8")
        assert legacy_home_is_substantial() is False

    def test_absent_legacy_home_is_not_substantial(self, fake_home: Path) -> None:
        assert legacy_home_is_substantial() is False

    def test_canonical_home_absent_is_fresh(self, fake_home: Path) -> None:
        assert canonical_home_is_fresh() is True

    def test_canonical_home_with_store_is_not_fresh(self, fake_home: Path) -> None:
        target = fake_home / ".vesma"
        (target / "data").mkdir(parents=True)
        (target / "data" / "vesma.db").write_bytes(b"")
        assert canonical_home_is_fresh() is False

    def test_canonical_home_with_config_is_not_fresh(self, fake_home: Path) -> None:
        target = fake_home / ".vesma"
        target.mkdir()
        (target / "config.yaml").write_text("vesma:\n  db_name: vesma.db\n", encoding="utf-8")
        assert canonical_home_is_fresh() is False

    def test_canonical_home_logs_only_is_fresh(self, fake_home: Path) -> None:
        target = fake_home / ".vesma"
        (target / "logs").mkdir(parents=True)
        assert canonical_home_is_fresh() is True


# ── Fork refusal (ADR-0044 gate 5) ────────────────────────────────────────────


class TestForkRefusal:
    def test_zero_config_over_legacy_store_refuses(self, fake_home: Path) -> None:
        """The core gate: fresh ~/.vesma + substantial ~/.mnemos → typed refusal."""
        legacy = _seed_legacy_store(fake_home)
        with pytest.raises(LegacyStoreForkRefused) as excinfo:
            load_settings()
        assert excinfo.value.code == "legacy-home"
        assert str(legacy) in str(excinfo.value)
        assert MIGRATE_HINT in str(excinfo.value)

    def test_refusal_names_the_exact_hint_command(self, fake_home: Path) -> None:
        _seed_legacy_store(fake_home)
        with pytest.raises(LegacyStoreForkRefused) as excinfo:
            load_settings()
        assert "vesma migrate-store --from ~/.mnemos --to ~/.vesma --apply" in str(excinfo.value)

    def test_no_refusal_without_legacy_home(self, fake_home: Path) -> None:
        settings = load_settings()  # must not raise
        assert settings.vesma.data_dir == fake_home / ".vesma" / "data"

    def test_no_refusal_when_legacy_home_is_only_logs(self, fake_home: Path) -> None:
        (fake_home / ".mnemos" / "logs").mkdir(parents=True)
        load_settings()  # must not raise

    def test_no_refusal_when_canonical_home_already_holds_a_store(self, fake_home: Path) -> None:
        """The migrated-production shape: both homes exist, canonical is real."""
        _seed_legacy_store(fake_home)
        target = fake_home / ".vesma"
        (target / "data").mkdir(parents=True)
        (target / "data" / "vesma.db").write_bytes(b"")
        load_settings()  # must not raise

    def test_no_refusal_when_vesma_config_env_set(self, fake_home: Path) -> None:
        """An explicit VESMA_CONFIG is the operator's choice — never refused."""
        _seed_legacy_store(fake_home)
        cfg = fake_home / "operator.yaml"
        cfg.write_text("vesma:\n  db_name: vesma.db\n", encoding="utf-8")
        os.environ["VESMA_CONFIG"] = str(cfg)
        try:
            load_settings()  # must not raise
        finally:
            os.environ.pop("VESMA_CONFIG", None)

    def test_no_refusal_when_explicit_config_path_given(self, fake_home: Path) -> None:
        _seed_legacy_store(fake_home)
        cfg = fake_home / "operator.yaml"
        cfg.write_text("vesma:\n  db_name: vesma.db\n", encoding="utf-8")
        load_settings(cfg)  # must not raise

    def test_no_refusal_when_cwd_config_present(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_legacy_store(fake_home)
        (fake_home / "config.yaml").write_text("vesma:\n  db_name: vesma.db\n", encoding="utf-8")
        monkeypatch.chdir(fake_home)
        load_settings()  # must not raise

    def test_cli_command_surfaces_friendly_refusal(self, fake_home: Path) -> None:
        """The refusal reaches the CLI as a ONE-LINE stderr message with the
        hint and exit code 1 — never a rich traceback panel."""
        _seed_legacy_store(fake_home)
        result = runner.invoke(cli_app, ["stats"])
        assert result.exit_code == 1
        merged = _streams(result)
        assert "legacy-home" in merged
        assert MIGRATE_HINT in merged
        assert "Traceback" not in merged


# ── Legacy config diagnostics ─────────────────────────────────────────────────


class TestLegacyConfigDiagnostics:
    def test_mnemos_section_raises_typed_error(self, fake_home: Path) -> None:
        cfg = fake_home / ".vesma" / "config.yaml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("mnemos:\n  db_name: mnemos.db\n", encoding="utf-8")
        with pytest.raises(LegacyConfigError) as excinfo:
            load_settings()
        assert excinfo.value.code == "legacy-config"
        assert "mnemos:" in str(excinfo.value)
        assert MIGRATE_HINT in str(excinfo.value)
        # Privacy: key names may surface, values never do.
        assert "mnemos.db" not in str(excinfo.value)

    def test_old_era_flat_key_raises_typed_error(self, fake_home: Path) -> None:
        """A top-level `graph_walk:` (today a vesma: section field) is legacy."""
        cfg = fake_home / ".vesma" / "config.yaml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("graph_walk: true\n", encoding="utf-8")
        with pytest.raises(LegacyConfigError) as excinfo:
            load_settings()
        assert "graph_walk" in str(excinfo.value)
        assert MIGRATE_HINT in str(excinfo.value)

    def test_unknown_typo_key_stays_raw_pydantic(self, fake_home: Path) -> None:
        """A genuinely unknown key is NOT legacy — the raw error stays honest."""
        cfg = fake_home / ".vesma" / "config.yaml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("totally_unknown_key: 1\n", encoding="utf-8")
        with pytest.raises(ValidationError):
            load_settings()

    def test_doctor_reports_legacy_config_with_code(self, fake_home: Path) -> None:
        """Doctor shows the same text under the stable diagnostic code."""
        cfg = fake_home / ".vesma" / "config.yaml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("mnemos:\n  db_name: mnemos.db\n", encoding="utf-8")
        result = _check_config()
        assert result.status.value == "fail"
        assert "legacy-config" in result.detail
        assert "legacy config detected" in result.detail
        assert MIGRATE_HINT in result.detail

    def test_doctor_reports_fork_refusal_with_code(self, fake_home: Path) -> None:
        """The fork refusal reaches the doctor as a coded FAIL (not a crash)."""
        _seed_legacy_store(fake_home)
        result = _check_config()
        assert result.status.value == "fail"
        assert "legacy-home" in result.detail
        assert MIGRATE_HINT in result.detail


# ── Update hint ───────────────────────────────────────────────────────────────


class TestUpdateHint:
    def test_hint_present_when_legacy_home_has_content(self, fake_home: Path) -> None:
        _seed_legacy_store(fake_home)
        hint = _legacy_home_hint()
        assert hint is not None
        assert MIGRATE_HINT in hint

    def test_hint_absent_without_legacy_home(self, fake_home: Path) -> None:
        assert _legacy_home_hint() is None

    def test_update_check_prints_the_hint(self, fake_home: Path) -> None:
        """`vesma update check` output carries the one-line suggestion."""
        _seed_legacy_store(fake_home)
        from vesma.cli.update_cmd import update_app

        result = runner.invoke(update_app, ["check"])
        assert result.exit_code == 0, result.output
        assert MIGRATE_HINT in _streams(result)
