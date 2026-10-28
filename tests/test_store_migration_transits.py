"""ADR-0049 wave 0: the disk-transit accounting (marker + preflight sum).

Pins: the transit opens its on-disk account (sibling marker, live pid),
closes it on every exit path, a live sibling transit surfaces in the plan
warnings, and the C14 free-space arithmetic counts the sibling's appetite
on the same device. The class the contract closes: clone + backup on one
disk starving each other mid-copy.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

import pytest

from tests.test_store_migration import (
    _DB_SCHEMA,
    _insert_records,
    _output,
    _target_of,
    app,
    runner,
)
from vesma.store_migration import (
    InsufficientSpaceError,
    TransitMarker,
    _cleanup_staging,
    _live_sibling_transits,
    _sibling_transit_bytes,
    _sibling_transit_lines,
    _transit_marker_for,
)

_Usage = namedtuple("_Usage", "total used free")


@pytest.fixture
def store_home(tmp_path: Path) -> Path:
    """A minimal fabricated 5.x home (main db + one sibling sqlite)."""
    home = tmp_path / "old-home"
    data = home / "data"
    data.mkdir(parents=True)
    _insert_records(data / "mnemos.db")
    vectors = sqlite3.connect(str(data / "vectors.db"))
    try:
        vectors.executescript(_DB_SCHEMA)
        vectors.execute("CREATE TABLE vec (id TEXT PRIMARY KEY, blob BLOB)")
        vectors.commit()
    finally:
        vectors.close()
    return home


def _foreign_live_pid() -> tuple[int, subprocess.Popen[str]]:
    """A genuinely foreign LIVE pid (our own never counts as a sibling —
    the marker's self-exclusion is the accounting contract). The caller
    kills the process (context-manager style)."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    return proc.pid, proc


def _marker_for(staging: Path, *, pid: int, planned: int) -> Path:
    marker = _transit_marker_for(staging)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "pid": pid,
                "started": "2026-10-28T00:00:00+00:00",
                "source": "/somewhere/old",
                "target": str(staging.name),
                "planned_bytes": planned,
            }
        ),
        encoding="utf-8",
    )
    return marker


def test_marker_sibling_not_inside_a_dir() -> None:
    staging = Path("/tmp/whatever/data.staging-20261028-000000")
    assert _transit_marker_for(staging) == Path(
        "/tmp/whatever/data.staging-20261028-000000.transit.json"
    )


def test_live_sibling_visible_dead_ignored(tmp_path: Path) -> None:
    parent = tmp_path / "vol"
    live_staging = parent / "h1.staging-20261028-000001"
    dead_staging = parent / "h2.staging-20261028-000002"
    pid, proc = _foreign_live_pid()
    try:
        _marker_for(live_staging, pid=pid, planned=1000)
        _marker_for(dead_staging, pid=2**22, planned=999_999_999)

        live = _live_sibling_transits([parent])
        assert len(live) == 1
        path, marker = live[0]
        assert path == _transit_marker_for(live_staging)
        assert marker is not None and marker.planned_bytes == 1000

        lines = _sibling_transit_lines([parent])
        assert len(lines) == 1 and "pid" in lines[0]
        assert _sibling_transit_bytes([parent], os.stat(parent).st_dev) == 1000
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_other_device_not_counted(tmp_path: Path) -> None:
    parent = tmp_path / "vol"
    pid, proc = _foreign_live_pid()
    try:
        _marker_for(parent / "h1.staging-1", pid=pid, planned=5000)
        assert _sibling_transit_bytes([parent], os.stat(tmp_path).st_dev + 1) == 0
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_unparsable_marker_stays_visible_contributes_zero(tmp_path: Path) -> None:
    parent = tmp_path / "vol"
    marker = _transit_marker_for(parent / "h1.staging-1")
    marker.parent.mkdir(parents=True)
    marker.write_text("{ junk", encoding="utf-8")
    assert _sibling_transit_bytes([parent], os.stat(parent).st_dev) == 0
    lines = _sibling_transit_lines([parent])
    assert len(lines) == 1 and "unparsable" in lines[0]


def test_cleanup_staging_removes_the_marker(tmp_path: Path) -> None:
    staging = tmp_path / "h.staging-1"
    staging.mkdir()
    marker = _marker_for(staging, pid=os.getpid(), planned=1)
    _cleanup_staging(staging)
    assert not staging.exists()
    assert not marker.exists()


def test_transit_lifecycle_through_apply(
    store_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full user-target apply: the marker opens with the transit and
    is gone when it closes (success path)."""
    import vesma.store_migration as sm

    target = _target_of(store_home, tmp_path)
    seen_markers: list[Path] = []
    real_write = sm._write_transit_marker

    def spy(plan: sm.MigrationPlan, progress: object) -> None:
        real_write(plan, progress)
        seen_markers.append(_transit_marker_for(plan.staging_dir))

    monkeypatch.setattr(sm, "_write_transit_marker", spy)
    result = runner.invoke(
        app,
        ["migrate-store", "--from", str(store_home), "--to", str(target), "--apply"],
    )
    assert result.exit_code == 0, _output(result)
    assert len(seen_markers) == 1
    # Success: the staging dir was renamed onto the target; the sibling
    # marker was removed with the cleanup.
    assert not seen_markers[0].exists()
    assert target.is_dir()


def test_free_space_counts_sibling_appetite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C14 arithmetic: a live sibling transit's planned bytes join the
    «needed» side and can push an otherwise-fitting volume over."""
    from vesma.store_migration import _check_free_space

    snapshot_parent = tmp_path / "snaps"
    target_root = tmp_path / "target"
    snapshot_parent.mkdir()
    target_root.mkdir()
    # A live sibling on the SAME device with a heavy appetite.
    pid, proc = _foreign_live_pid()
    try:
        _marker_for(snapshot_parent / "other.staging-1", pid=pid, planned=10**9)

        monkeypatch.setattr(
            "vesma.store_migration.shutil.disk_usage",
            lambda _p: _Usage(10**10, 0, int(1.5 * 10**9)),
        )
        # Our planned 10**9 fits 1.5 GiB alone; with the sibling's 10**9 on
        # the same device the snapshot volume no longer fits.
        with pytest.raises(InsufficientSpaceError) as excinfo:
            _check_free_space(10**9, snapshot_parent / "ours", target_root)
    finally:
        proc.kill()
        proc.wait(timeout=10)
    message = str(excinfo.value)
    assert "concurrent transits counted" in message
    assert "other.staging-1.transit.json" in message


def test_transit_marker_is_alive_semantics() -> None:
    dead = TransitMarker(pid=2**22, started="", source="", target="", planned_bytes=None)
    assert dead.is_alive() is False
    self_marker = TransitMarker(pid=os.getpid(), started="", source="", target="", planned_bytes=1)
    assert self_marker.is_alive() is False  # our own transit never self-counts
