"""Logging configuration for Vesma.

Configures the root logger with a console (stderr) handler and an optional
rotating file handler based on ``Settings.logging``. Also integrates
Uvicorn's loggers so access logs respect the configured level.

Usage::

    from vesma.config import load_settings
    from vesma.logging_setup import setup_logging

    settings = load_settings()
    setup_logging(settings)
"""

from __future__ import annotations

import logging
from io import TextIOWrapper
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

from vesma.fs_hardening import ensure_private_dir, harden_file

if TYPE_CHECKING:
    from vesma.config import Settings

# Valid log levels — anything outside this set falls back to INFO.
_VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that keeps the log file at 0600.

    Cascade fix 2026-10-09: the log records memory titles/tags, so the
    file must not inherit the writer's umask. ``_open`` runs on every
    open — creation here and the fresh file after each rollover — so a
    best-effort narrow-if-wider after the super call covers both.
    """

    def _open(self) -> TextIOWrapper:
        stream = super()._open()
        harden_file(Path(self.baseFilename))
        return stream


def _resolve_level(level_str: str) -> int:
    """Resolve a level string to a logging constant, defaulting to INFO."""
    upper = level_str.upper().strip()
    if upper in _VALID_LEVELS:
        level: int = getattr(logging, upper)
        return level
    return logging.INFO


def setup_logging(settings: Settings, *, verbose: bool = False) -> None:
    """Configure root logging from ``settings.logging``.

    Parameters
    ----------
    settings:
        Loaded Vesma settings (``Settings`` instance).
    verbose:
        When True, overrides the configured level to DEBUG (CLI ``--verbose``).
    """
    cfg = settings.logging
    level = logging.DEBUG if verbose else _resolve_level(cfg.level)
    formatter = logging.Formatter(fmt=cfg.format, datefmt=cfg.date_format)

    root = logging.getLogger()
    # Clear existing handlers so repeated calls (e.g. in tests) don't stack.
    root.handlers.clear()
    root.setLevel(level)

    # ── Console handler (stderr) ────────────────────────────────────────
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(formatter)
    root.addHandler(console)

    # ── Rotating file handler (optional) ──────────────────────────────────
    if cfg.log_file and str(cfg.log_file).strip():
        log_path: Path = cfg.log_file
        try:
            # Cascade fix 2026-10-09: 0700 on every created level.
            ensure_private_dir(log_path.parent)
            max_bytes = cfg.max_file_size_mb * 1024 * 1024
            file_handler = _PrivateRotatingFileHandler(
                filename=str(log_path),
                maxBytes=max_bytes,
                backupCount=cfg.backup_count,
                encoding="utf-8",
            )
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError:
            # If the log directory isn't writable, fall back to stderr-only.
            # Don't crash the server over logging.
            logging.getLogger(__name__).warning(
                "Cannot write log file %s — falling back to stderr only",
                log_path,
            )

    # ── Uvicorn integration ───────────────────────────────────────────────
    # Uvicorn configures its own loggers; bring them in line with our level.
    for uv_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uv_logger = logging.getLogger(uv_name)
        uv_logger.handlers.clear()
        uv_logger.setLevel(level)
        uv_logger.propagate = True  # let root handlers emit the records
