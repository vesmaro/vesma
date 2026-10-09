"""Harden-on-first-touch of the vesma home (cascade fix 2026-10-09, P3).

A fresh first run used to create ``~/.vesma/{data,vault,logs}`` and the
db/WAL/log files under the process umask — directories landed 0755 and
the memory database 0644, readable by every other local account. The
contract now: every created directory is 0700, every store-owned file
(db, WAL/SHM sidecars, logs, vault markdown, caches, audit logs) is
0600 — best-effort narrow-if-wider, never a widening and never a crash.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from vesma.cli.main import app
from vesma.fs_hardening import ensure_private_dir, harden_file

runner = CliRunner()


def _assert_private_tree(root: Path) -> None:
    """No group/other bit anywhere under ``root`` (dirs and files)."""
    assert root.is_dir()
    for dirpath, dirnames, filenames in os.walk(root):
        for name in (*dirnames, *filenames):
            mode = (Path(dirpath) / name).stat().st_mode
            assert mode & 0o077 == 0, (
                f"group/other bits on {Path(dirpath) / name}: {stat.filemode(mode)}"
            )


@pytest.fixture
def fresh_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A HOME that does not exist yet + an isolated VESMA_CONFIG inside it.

    The home is NOT pre-created: the first vesma call must build the whole
    ``~/.vesma`` tree itself — that is the first-touch case.
    """
    from vesma.cli._manager import reset_manager

    reset_manager()
    home = tmp_path / "home"
    cfg = home / "vesma.yaml"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VESMA_CONFIG", str(cfg))
    yield home, cfg
    reset_manager()


def _write_config(cfg: Path, db_name: str = "hardening.db") -> None:
    """Config whose data/vault live at the canonical ~/.vesma spots."""
    home = cfg.parent
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        f"vesma:\n"
        f"  vault_path: {home / '.vesma' / 'vault'}\n"
        f"  data_dir: {home / '.vesma' / 'data'}\n"
        f"  db_name: {db_name}\n"
        f"embedding:\n"
        f"  provider: nano\n",
        encoding="utf-8",
    )


def test_stats_first_touch_builds_a_private_tree(fresh_home: tuple[Path, Path]) -> None:
    """`vesma stats` on a fresh HOME: the whole ~/.vesma tree is 0700/0600."""
    home, cfg = fresh_home
    _write_config(cfg)

    result = runner.invoke(app, ["stats"])

    assert result.exit_code == 0, result.output
    _assert_private_tree(home / ".vesma")


def test_add_first_touch_writes_private_vault_and_db(
    fresh_home: tuple[Path, Path],
) -> None:
    """add on a fresh home: the db, WAL sidecars, and the vault markdown are
    all 0600 — same content, same bar, regardless of umask."""
    home, cfg = fresh_home
    _write_config(cfg)

    result = runner.invoke(
        app,
        [
            "add",
            "hardening probe: private home contract",
            "--title",
            "hardening probe",
            "--tags",
            "project:proj,agent:cli,vesma:learning",
        ],
    )

    assert result.exit_code == 0, result.output
    _assert_private_tree(home / ".vesma")
    # The vault note really exists (the walk covered it) — pin it.
    notes = list((home / ".vesma" / "vault").rglob("*.md"))
    assert notes, "the add must have written a vault markdown file"


def test_serve_first_touch_builds_a_private_log_tree(
    fresh_home: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """serve on a fresh HOME: setup_logging creates ~/.vesma/logs private.

    uvicorn.run is stubbed (no server); the update-check fetch is opted
    out so the test is hermetic. The walk covers everything serve's
    startup path touched under the home.
    """
    import logging
    from unittest.mock import patch

    home, cfg = fresh_home
    _write_config(cfg)
    monkeypatch.setenv("VESMA_UPDATES_CHECK", "off")
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        with patch("uvicorn.run"):
            result = runner.invoke(app, ["serve", "--host", "127.0.0.1", "--port", "18799"])
        assert result.exit_code == 0, result.output
        assert (home / ".vesma" / "logs").is_dir(), "serve must create the logs dir"
        assert (home / ".vesma" / "logs" / "vesma.log").is_file()
        _assert_private_tree(home / ".vesma")
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def test_existing_wider_paths_are_narrowed_never_widened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ensure_private_dir/harden_file narrow-if-wider: a pre-existing 0755
    dir becomes 0700; an already-compliant tree keeps its mtime/mode."""
    target = tmp_path / "a" / "b"
    target.mkdir(parents=True)  # 0755-ish under the test umask
    wide_file = target / "wide.db"
    wide_file.write_text("x", encoding="utf-8")
    os.chmod(wide_file, 0o644)

    ensure_private_dir(target)
    harden_file(wide_file)

    assert target.stat().st_mode & 0o777 == 0o700
    assert wide_file.stat().st_mode & 0o777 == 0o600

    # Idempotent: a compliant path is left untouched (mtime unchanged).
    before = (target.stat().st_mtime_ns, wide_file.stat().st_mtime_ns)
    ensure_private_dir(target)
    harden_file(wide_file)
    after = (target.stat().st_mtime_ns, wide_file.stat().st_mtime_ns)
    assert before == after


def test_narrowing_is_best_effort_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chmod failure (foreign owner) is swallowed — the helpers never
    turn a working write into a crash."""
    from vesma import fs_hardening

    target = tmp_path / "dir"
    target.mkdir()
    file_path = target / "f.db"
    file_path.write_text("x", encoding="utf-8")

    def _boom(*_a: Any, **_k: Any) -> None:
        raise OSError("not owner")

    monkeypatch.setattr(Path, "chmod", _boom)
    monkeypatch.setattr(fs_hardening, "_log", _FakeLogger())

    ensure_private_dir(target)  # must not raise
    harden_file(file_path)  # must not raise


class _FakeLogger:
    """Minimal logger double for the best-effort failure path."""

    def debug(self, *_a: Any, **_k: Any) -> None:
        return None


def test_federation_access_log_is_private(tmp_path: Path) -> None:
    """The federation audit JSONL (contract §10 leak surface) lands 0600 in
    a 0700 logs dir, whatever the umask says."""
    from datetime import UTC, datetime

    from vesma.federation_access_log import AccessLogEntry, FederationAccessLog

    log = FederationAccessLog(tmp_path / "logs" / "federation-access.jsonl")
    entry = AccessLogEntry(
        peer_id="peer-a",
        topic_hash="a" * 64,
        timestamp=datetime.now(UTC),
        project_scope="proj",
        trigger_code="EXHAUSTIVE",
        record_ids_accessed=[],
    )

    log.append(entry)

    _assert_private_tree(tmp_path / "logs")
