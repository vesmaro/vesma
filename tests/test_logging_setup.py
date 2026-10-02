"""Tests for vesmaro.logging_setup — logging configuration from settings.

Covers:
- setup_logging() configures root logger with correct level
- File handler is created when log_file is set
- Rotation works (max_file_size_mb, backup_count)
- Verbose mode overrides level to DEBUG
- Uvicorn loggers are integrated
- Empty log_file → stderr only (no file handler)
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from vesmaro.config import Settings
from vesmaro.logging_setup import setup_logging


@pytest.fixture
def clean_logging() -> None:
    """Reset root logger before and after each test."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    root.handlers.clear()
    yield
    root.handlers.clear()
    root.handlers.extend(saved_handlers)
    root.setLevel(saved_level)


@pytest.fixture
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings with paths pointing to tmp_path."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    settings = Settings()
    settings.vesma.data_dir = tmp_path / "data"
    settings.vesma.vault_path = tmp_path / "vault"
    settings.logging.log_file = tmp_path / "logs" / "vesmaro.log"
    settings.resolve_paths()
    return settings


# ── Basic configuration ────────────────────────────────────────────────────────


def test_setup_logging_sets_root_level(clean_logging: None, isolated_settings: Settings) -> None:
    """setup_logging() sets the root logger level from config."""
    isolated_settings.logging.level = "WARNING"
    setup_logging(isolated_settings)
    assert logging.getLogger().level == logging.WARNING


def test_setup_logging_invalid_level_defaults_to_info(
    clean_logging: None, isolated_settings: Settings
) -> None:
    """An invalid level string falls back to INFO."""
    isolated_settings.logging.level = "BOGUS"
    setup_logging(isolated_settings)
    assert logging.getLogger().level == logging.INFO


def test_setup_logging_verbose_overrides_to_debug(
    clean_logging: None, isolated_settings: Settings
) -> None:
    """verbose=True overrides config level to DEBUG."""
    isolated_settings.logging.level = "WARNING"
    setup_logging(isolated_settings, verbose=True)
    assert logging.getLogger().level == logging.DEBUG


# ── Console handler ───────────────────────────────────────────────────────────


def test_setup_logging_adds_console_handler(
    clean_logging: None, isolated_settings: Settings
) -> None:
    """A StreamHandler (console) is always added."""
    setup_logging(isolated_settings)
    root = logging.getLogger()
    stream_handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler)]
    assert len(stream_handlers) >= 1


# ── File handler ──────────────────────────────────────────────────────────────


def test_setup_logging_creates_file_handler(
    clean_logging: None, isolated_settings: Settings, tmp_path: Path
) -> None:
    """A RotatingFileHandler is created when log_file is set."""
    setup_logging(isolated_settings)
    root = logging.getLogger()
    file_handlers = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
    assert len(file_handlers) == 1
    assert file_handlers[0].baseFilename == str(tmp_path / "logs" / "vesmaro.log")


def test_setup_logging_creates_log_directory(
    clean_logging: None, isolated_settings: Settings, tmp_path: Path
) -> None:
    """The log directory is created if it doesn't exist."""
    log_dir = tmp_path / "logs"
    assert not log_dir.exists()
    setup_logging(isolated_settings)
    assert log_dir.is_dir()


def test_setup_logging_writes_to_file(
    clean_logging: None, isolated_settings: Settings, tmp_path: Path
) -> None:
    """Log messages are actually written to the file."""
    setup_logging(isolated_settings)
    test_logger = logging.getLogger("test_module")
    test_logger.info("test log message")
    # Flush all handlers
    for h in logging.getLogger().handlers:
        h.flush()
    log_content = (tmp_path / "logs" / "vesmaro.log").read_text()
    assert "test log message" in log_content


def test_setup_logging_rotation_config(clean_logging: None, isolated_settings: Settings) -> None:
    """RotatingFileHandler uses max_file_size_mb and backup_count from config."""
    isolated_settings.logging.max_file_size_mb = 5
    isolated_settings.logging.backup_count = 7
    setup_logging(isolated_settings)
    root = logging.getLogger()
    file_handler = next(h for h in root.handlers if isinstance(h, RotatingFileHandler))
    assert file_handler.maxBytes == 5 * 1024 * 1024
    assert file_handler.backupCount == 7


def test_setup_logging_empty_log_file_no_file_handler(
    clean_logging: None, isolated_settings: Settings
) -> None:
    """When log_file is empty, no file handler is created (stderr only)."""
    isolated_settings.logging.log_file = Path("")
    isolated_settings.resolve_paths()
    setup_logging(isolated_settings)
    root = logging.getLogger()
    file_handlers = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
    assert file_handlers == []


# ── Uvicorn integration ───────────────────────────────────────────────────────


def test_setup_logging_integrates_uvicorn(clean_logging: None, isolated_settings: Settings) -> None:
    """Uvicorn loggers are configured to propagate to root."""
    setup_logging(isolated_settings)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uv_logger = logging.getLogger(name)
        assert uv_logger.level == logging.INFO
        assert uv_logger.propagate is True


def test_setup_logging_verbose_uvicorn(clean_logging: None, isolated_settings: Settings) -> None:
    """Verbose mode sets uvicorn loggers to DEBUG."""
    setup_logging(isolated_settings, verbose=True)
    uv_logger = logging.getLogger("uvicorn")
    assert uv_logger.level == logging.DEBUG


# ── Idempotency ───────────────────────────────────────────────────────────────


def test_setup_logging_idempotent(clean_logging: None, isolated_settings: Settings) -> None:
    """Calling setup_logging twice doesn't stack handlers."""
    setup_logging(isolated_settings)
    count1 = len(logging.getLogger().handlers)
    setup_logging(isolated_settings)
    count2 = len(logging.getLogger().handlers)
    assert count1 == count2
