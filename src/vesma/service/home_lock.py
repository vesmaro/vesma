"""Per-home exclusive writer lock (ADR-0049 Lock 1, wave 0).

ONE ``flock(2)`` lock file per STORE HOME (the folder that holds
``vesma.db`` and its sibling databases — the default user layout resolves
it to ``~/.vesma/data``, the system profile to ``/var/lib/vesma``), taken
by every writer-of-record before it touches the home:

- the supervisor (``vesma service run``) and the API server (``vesma
  serve``) hold it **EXCLUSIVE** for the whole process lifetime — a second
  supervisor over the SAME home (any endpoint) is refused at startup with
  the holder's name (kernel-level fork refusal; the control-socket guard
  stays endpoint-scoped and cannot see a different-endpoint sibling);
- the store mover (``vesma migrate-store --apply``) holds it EXCLUSIVE on
  the source home (and on an existing target root) for the WHOLE transit —
  a transit can no longer overlap a live supervisor or a second transit
  over the same home.

The lock FILE lives in the CONTROL-PLANE state home
(``$XDG_STATE_HOME/vesma/writer-locks/<sha256(home)>.lock``), never inside
a store home: the mover's C6 pin (the system-target source tree stays
byte+stat untouched) forbids creating a file under the source tree, and
the v2 layout home root (``~/.vesma``) IS a store home. The location is
generation-independent on purpose — the lock arbitrates the HOME, so it
must not move when the layout generation does. Same owner ⇒ same state
home ⇒ every contender for one home collides on the same file.

The authority is the kernel ``flock`` — it is released by the OS when the
holder process dies, whatever the cleanup discipline. The lock FILE
additionally records its holder (pid + home + endpoint + mode + started-at)
for INCIDENT ATTRIBUTION: after a crash the file answers "who held it", and
a refused contender prints that answer fail-loud
(:class:`WriterLockBusyError`). Stale holder files (holder dead, lock
already released by the kernel) are attributed as dead — the flock is the
authority, the file is the evidence. (The state home is not a promise of
forensic permanence: an OS reboot clears it — and with it the stale pids
it would misattribute.)

Deliberately NOT locked here: CLI commands that open the stores directly
(``vesma logs``, ``vesma agent-token …``, ``vesma auth …``) keep their
today's WAL-coexistence behavior. Locking them SHARED would refuse every
one of them while the supervisor holds EXCLUSIVE — all of them are
designed live-coexistence flows (traces ``--follow``, token minting
against a running mesh). The wave-0 lock guards the supervisor-vs-
supervisor and transit-vs-supervisor hazards; widening it to CLI writers
is a wave-1 contract question (routing those flows through the control
plane).

Reentrancy: the lock is refcounted PER PROCESS (a registry keyed by the
resolved lock path). One process acquiring the same home twice shares one
``flock`` (flock is per open-file-description — a naive second ``flock``
from the same process on another fd would deadlock against itself); a
CONFLICTING mode in the same process refuses loudly instead of blocking.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import fcntl
import hashlib
import json
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from vesma.fs_hardening import ensure_private_dir

LOCK_DIR_NAME = "writer-locks"
LOCK_FILE_SUFFIX = ".lock"

_MODE_EXCLUSIVE = "exclusive"
_MODE_SHARED = "shared"

_HOLDER_FILE_MODE = 0o600


class WriterLockError(RuntimeError):
    """Base class of the per-home writer lock failures (fail-loud)."""


class WriterLockBusyError(WriterLockError):
    """The home writer lock is contended — another writer holds it.

    The message names the recorded holder (pid + endpoint + mode + started
    at) when the lock file carries one, and marks a dead pid as DEAD. A
    filesystem that cannot flock at all (NFS without lock support) raises
    the raw ``OSError`` instead — an environment failure, not a contention
    verdict.
    """


@dataclasses.dataclass(frozen=True)
class WriterHolder:
    """The recorded holder of a home writer lock (lock-file payload)."""

    pid: int
    home: str
    endpoint: str
    mode: str
    started: str

    def is_alive(self) -> bool:
        """True when the holder pid is a LIVE process (signal-0 probe)."""
        if self.pid <= 0:
            return False
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # The process exists but belongs to another account — alive
            # for attribution purposes (we can see it, we may not signal it).
            return True
        return True

    def describe(self) -> str:
        """One-line attribution: pid, liveness, home, endpoint, mode."""
        state = "alive" if self.is_alive() else "DEAD"
        return (
            f"pid {self.pid} ({state}) home={self.home!r} endpoint={self.endpoint!r} "
            f"mode={self.mode} started={self.started}"
        )


def lock_path_for(data_dir: Path) -> Path:
    """The ONE lock file of a store home: ``<writer-locks>/<sha256>.lock``.

    Keyed by the RESOLVED home path so every contender for the same home —
    whatever path spelling it arrived under — collides on the same file.
    """
    resolved = data_dir.resolve()
    digest = hashlib.sha256(os.fsencode(str(resolved))).hexdigest()
    return _writer_lock_root() / f"{digest}{LOCK_FILE_SUFFIX}"


def _writer_lock_root() -> Path:
    """Control-plane state home of the lock files: generation-independent
    ``$XDG_STATE_HOME/vesma/writer-locks`` (see the module docstring for
    why no store home can host them)."""
    xdg = os.environ.get("XDG_STATE_HOME", "")
    root = Path(xdg) if xdg.strip() else Path.home() / ".local" / "state"
    return root / "vesma" / LOCK_DIR_NAME


def _holder_from_file(path: Path) -> WriterHolder | None:
    """Parse the holder payload; ``None`` when absent/corrupt (evidence,
    never authority — a corrupt file must not block the refusal message)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    pid = raw.get("pid")
    if not isinstance(pid, int):
        return None
    endpoint = raw.get("endpoint")
    mode = raw.get("mode")
    started = raw.get("started")
    home = raw.get("home")
    return WriterHolder(
        pid=pid,
        home=home if isinstance(home, str) else "",
        endpoint=endpoint if isinstance(endpoint, str) else "",
        mode=mode if isinstance(mode, str) else "",
        started=started if isinstance(started, str) else "",
    )


def read_lock_holder(data_dir: Path) -> WriterHolder | None:
    """The recorded holder of ``data_dir``'s lock file, if any.

    Pure file reading — NO liveness/flock verdict. The holder may be long
    dead (the kernel releases the flock at death; the file stays as
    attribution evidence). Use :func:`probe_writer_lock` for the live
    verdict.
    """
    return _holder_from_file(lock_path_for(data_dir))


def probe_writer_lock(data_dir: Path) -> tuple[bool, WriterHolder | None]:
    """``(held, holder)`` verdict on the home's writer lock — non-blocking.

    ``held=True`` means a LIVE process (possibly this one) holds the flock
    right now. ``holder`` is the recorded payload when the lock file
    parses (it may lag the true holder by the flock→write window, and may
    name a previous holder of the SAME still-held lock if the current
    holder has not written yet — attribution jitter, documented).

    A filesystem where ``flock`` itself fails raises the raw ``OSError``:
    the probe never guesses a verdict it cannot establish.
    """
    path = lock_path_for(data_dir)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
    except FileNotFoundError:
        return (False, None)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return (True, _holder_from_file(path))
            raise
        # We won the race — nobody holds it. Release immediately.
        fcntl.flock(fd, fcntl.LOCK_UN)
        return (False, _holder_from_file(path))
    finally:
        os.close(fd)


@dataclasses.dataclass
class _LockEntry:
    fd: int
    mode: str
    endpoint: str
    refcount: int


_registry_lock = threading.Lock()
_active_locks: dict[Path, _LockEntry] = {}


def _flock_flags(mode: str) -> int:
    return fcntl.LOCK_EX if mode == _MODE_EXCLUSIVE else fcntl.LOCK_SH


def _acquire(data_dir: Path, mode: str, endpoint: str) -> _LockEntry:
    """Registry-checked acquire (the caller holds ``_registry_lock``)."""
    key = data_dir.resolve()
    entry = _active_locks.get(key)
    if entry is not None:
        if entry.mode != mode:
            raise WriterLockBusyError(
                f"this process already holds the {entry.mode} writer lock on "
                f"{key} — acquiring {mode} in the same process is refused "
                "(flock self-conflict)"
            )
        if entry.endpoint != endpoint:
            raise WriterLockBusyError(
                f"this process already holds the {entry.mode} writer lock on "
                f"{key} as {entry.endpoint!r} — a different in-process "
                f"writer ({endpoint!r}) is refused (ADR-0049 Lock 1)"
            )
        entry.refcount += 1
        return entry
    path = lock_path_for(key)
    ensure_private_dir(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, _HOLDER_FILE_MODE)
    try:
        try:
            fcntl.flock(fd, _flock_flags(mode) | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                holder = _holder_from_file(path)
                detail = (
                    holder.describe()
                    if holder is not None
                    else (
                        "holder payload unreadable — the holder did not finish "
                        "writing it, or the file was damaged"
                    )
                )
                raise WriterLockBusyError(
                    f"store home {key} is locked by another writer ({detail}) — "
                    "a second writer over one store home is refused (ADR-0049 "
                    "Lock 1); stop the holder first or point this process at "
                    "a different home"
                ) from exc
            raise
        # Hold the registry lock while writing the payload: two threads of
        # this process cannot interleave acquire/release around the write.
        _write_holder(path, key, mode, endpoint)
        entry = _LockEntry(fd=fd, mode=mode, endpoint=endpoint, refcount=1)
        _active_locks[key] = entry
        return entry
    except OSError:
        os.close(fd)
        raise


def _write_holder(path: Path, home: Path, mode: str, endpoint: str) -> None:
    payload = json.dumps(
        {
            "pid": os.getpid(),
            "home": str(home),
            "endpoint": endpoint,
            "mode": mode,
            "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        }
    )
    # "w" truncates: a previous holder's payload never survives ours.
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(payload + "\n")
    os.chmod(path, _HOLDER_FILE_MODE)


def _release(entry: _LockEntry, data_dir: Path) -> None:
    key = data_dir.resolve()
    with _registry_lock:
        entry.refcount -= 1
        if entry.refcount > 0:
            return
        _active_locks.pop(key, None)
    fcntl.flock(entry.fd, fcntl.LOCK_UN)
    os.close(entry.fd)
    # The holder file STAYS: it is the attribution evidence of the last
    # holder (the kernel released the flock; the file answers "who held
    # it" for an incident that happened before the release).


@contextlib.contextmanager
def writer_lock(
    data_dir: Path, *, endpoint: str = "", mode: str = _MODE_EXCLUSIVE
) -> Iterator[Path]:
    """Hold the home's writer lock for the scope (fail-loud on contention).

    ``mode`` is ``exclusive`` (supervisors, the mover's transit) or
    ``shared`` (reserved for future read-writers; SHARED coexists with
    SHARED, refuses against EXCLUSIVE). The returned path is the lock
    file. Filesystem-level failures (no flock support) raise the raw
    ``OSError`` — never a silent fallback.
    """
    if mode not in (_MODE_EXCLUSIVE, _MODE_SHARED):
        raise ValueError(f"unknown writer lock mode: {mode!r}")
    with _registry_lock:
        entry = _acquire(data_dir, mode, endpoint)
    try:
        yield lock_path_for(data_dir.resolve())
    finally:
        _release(entry, data_dir)
