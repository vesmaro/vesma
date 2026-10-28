"""ADR-0049 Lock 1 (wave 0): the per-home exclusive writer lock.

Pins: dual-writer refusal with holder attribution, kernel release on
holder death, holder-file evidence, probe verdicts, in-process
reentrancy/conflict, file hygiene (0600, control-plane state home — the
STORE home tree stays untouched, the C6 property), hermetic
XDG_STATE_HOME.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from vesma.service.home_lock import (
    LOCK_DIR_NAME,
    WriterHolder,
    WriterLockBusyError,
    lock_path_for,
    probe_writer_lock,
    read_lock_holder,
    writer_lock,
)

_SRC_ROOT = str(Path(__file__).parent.parent / "src")

_HOLD_SCRIPT = (
    "import sys, time\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, %r)\n"
    "from vesma.service.home_lock import writer_lock\n"
    "with writer_lock(Path(%r), endpoint=%r):\n"
    "    print('held', flush=True)\n"
    "    time.sleep(60)\n"
)


@pytest.fixture(autouse=True)
def _hermetic_state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The lock files live in the XDG state plane — point it at the test
    tmp (subprocesses inherit the env, so holders collide hermetically)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _spawn_holder(home: Path, endpoint: str) -> subprocess.Popen[str]:
    """A foreign PROCESS holding the home's writer lock (true contention)."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLD_SCRIPT % (_SRC_ROOT, str(home), endpoint)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "held"
    return proc


def _holder_of(home: Path) -> WriterHolder:
    holder = read_lock_holder(home)
    assert holder is not None, "holder payload must be recorded in the lock file"
    return holder


def test_lock_file_lives_in_the_state_home_and_is_0600(tmp_path: Path) -> None:
    """One lock file per home, OUTSIDE the home tree: the state home keyed
    by the resolved home, mode 0600 — and the home itself is untouched."""
    home = tmp_path / "data"
    home.mkdir(parents=True)
    with writer_lock(home, endpoint="test") as path:
        assert path == lock_path_for(home)
        assert LOCK_DIR_NAME in path.parts and path.name.endswith(".lock")
        assert not path.is_relative_to(home), "the store home must stay clean (C6)"
        assert path.is_file()
        assert (path.stat().st_mode & 0o777) == 0o600
    # The file STAYS after release — attribution evidence of the last holder.
    assert path.is_file()
    # The home tree carries nothing: taking/releasing the lock created no
    # entry under it (the mover's source-tree pin).
    assert list(home.iterdir()) == []


def test_second_exclusive_writer_refused_with_holder_name(tmp_path: Path) -> None:
    """Two writers on one home: the second refuses, naming the holder."""
    home = tmp_path / "data"
    proc = _spawn_holder(home, "supervisor-a")
    try:
        holder = _holder_of(home)
        assert holder.pid == proc.pid
        assert holder.endpoint == "supervisor-a"
        assert holder.mode == "exclusive"
        assert holder.is_alive()
        with (
            pytest.raises(WriterLockBusyError) as excinfo,
            writer_lock(home, endpoint="supervisor-b"),
        ):
            pass  # pragma: no cover — never reached
    finally:
        proc.kill()
        proc.wait(timeout=10)
    message = str(excinfo.value)
    assert f"pid {proc.pid}" in message
    assert "supervisor-a" in message
    assert "ADR-0049" in message


def test_in_process_second_logical_writer_refused(tmp_path: Path) -> None:
    """Same process, same mode, DIFFERENT endpoint: refused, not shared."""
    home = tmp_path / "data"
    with (
        writer_lock(home, endpoint="writer-a"),
        pytest.raises(WriterLockBusyError) as excinfo,
        writer_lock(home, endpoint="writer-b"),
    ):
        pass
    assert "writer-a" in str(excinfo.value)


def test_refusal_message_marks_stale_dead_holder(tmp_path: Path) -> None:
    """A held lock whose holder payload names a dead pid (the flock→write
    attribution jitter, or a damaged payload) reads DEAD in the refusal —
    the flock stays the authority, the file is evidence."""
    home = tmp_path / "data"
    path = lock_path_for(home)
    proc = _spawn_holder(home, "live-holder")
    try:
        dead = json.dumps(
            {
                "pid": 2**22,
                "home": str(home),
                "endpoint": "ghost",
                "mode": "exclusive",
                "started": "2000-01-01T00:00:00",
            }
        )
        path.write_text(dead, encoding="utf-8")  # simulate stale/corrupt evidence
        assert _holder_of(home).is_alive() is False
        with (
            pytest.raises(WriterLockBusyError) as excinfo,
            writer_lock(home, endpoint="new-writer"),
        ):
            pass  # pragma: no cover — never reached
    finally:
        proc.kill()
        proc.wait(timeout=10)
    message = str(excinfo.value)
    assert "DEAD" in message
    assert "ghost" in message


def test_release_allows_reacquire(tmp_path: Path) -> None:
    home = tmp_path / "data"
    with writer_lock(home, endpoint="first"):
        pass
    with writer_lock(home, endpoint="second"):
        assert _holder_of(home).endpoint == "second"


def test_kernel_releases_on_holder_death(tmp_path: Path) -> None:
    """A holder killed without cleanup leaves a usable lock (flock is the
    authority) + attribution evidence (the stale holder file)."""
    home = tmp_path / "data"
    proc = _spawn_holder(home, "crasher")
    proc.kill()
    proc.wait(timeout=10)
    # The stale holder names the dead crasher...
    stale = _holder_of(home)
    assert stale.endpoint == "crasher"
    assert stale.is_alive() is False
    # ...but the flock is free: we take it and rewrite the payload.
    with writer_lock(home, endpoint="successor"):
        assert _holder_of(home).endpoint == "successor"
        assert _holder_of(home).is_alive()


def test_probe_reports_held_and_free(tmp_path: Path) -> None:
    home = tmp_path / "data"
    held, holder = probe_writer_lock(home)
    assert held is False and holder is None  # no file at all
    with writer_lock(home, endpoint="probed"):
        held, holder = probe_writer_lock(home)
        assert held is True
        assert holder is not None and holder.endpoint == "probed"
    held, holder = probe_writer_lock(home)
    assert held is False
    assert holder is not None and holder.endpoint == "probed"  # evidence remains


def test_in_process_conflicting_mode_refused_not_deadlock(tmp_path: Path) -> None:
    """Same process, conflicting mode: loud refusal, no flock self-block."""
    home = tmp_path / "data"
    with (
        writer_lock(home, endpoint="base", mode="exclusive"),
        pytest.raises(WriterLockBusyError),
        writer_lock(home, endpoint="upgrade", mode="shared"),
    ):
        pass


def test_in_process_reentrancy_refcounts(tmp_path: Path) -> None:
    """Two acquisitions of the same mode in one process share ONE flock."""
    home = tmp_path / "data"
    with writer_lock(home, endpoint="outer"):
        with writer_lock(home, endpoint="outer") as nested:  # no deadlock
            assert nested == lock_path_for(home)
        # Inner released; the outer refcount still holds the flock — a
        # probe's fresh fd (a different open-file-description) still
        # sees the contention.
        held, holder = probe_writer_lock(home)
        assert held is True
        assert holder is not None and holder.pid == os.getpid()
    held, _ = probe_writer_lock(home)
    assert held is False


def test_corrupt_holder_file_reads_none(tmp_path: Path) -> None:
    path = lock_path_for(tmp_path / "data")
    path.parent.mkdir(parents=True)
    path.write_text("not json at all {", encoding="utf-8")
    assert read_lock_holder(tmp_path / "data") is None
    held, _holder = probe_writer_lock(tmp_path / "data")
    assert held is False


def test_same_home_under_different_spellings_collide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock is keyed by the RESOLVED home: a symlinked path spelling
    arbitrates against the real one — not a second lock file."""
    real = tmp_path / "data"
    real.mkdir(parents=True)
    link = tmp_path / "link-to-data"
    link.symlink_to(real)
    with writer_lock(real, endpoint="real-spelling"):
        assert lock_path_for(link) == lock_path_for(real)
        with (
            pytest.raises(WriterLockBusyError),
            writer_lock(link, endpoint="link-spelling"),
        ):
            pass  # pragma: no cover — never reached
