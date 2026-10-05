"""The component supervisor (contract specs/service-lifecycle/v1, 1.0.0-draft.2).

Owns the lifecycle of every component of an installation: spawn, health,
restart, stop, observability. The FSM (``.fsm``) and the structural lines
(``.logsink``) are separate modules; this module is the orchestration.

Process model (§3.1):

- every child starts with ``start_new_session=True`` (setsid) — pgid == pid
  (SL-03), so a component is addressable as one POSIX session/group;
- **group-signal boundary (MUST)**: ``kill(-pgid, …)`` is sent ONLY while
  the direct child is unreaped. During a stop's grace window the reaper
  thread is PAUSED (a reference-counted pause), so the direct child stays
  an unreaped zombie that pins its pid/pgid against reuse while the group
  SIGKILL is decided and sent; the reaper resumes strictly after the kill
  phase. After a child is reaped its pgid is never signalled again — the
  pid may already have been handed to a new process;
- reaping: one reaper thread drains ``os.waitpid(-1, WNOHANG)`` until
  ECHILD (SL-05), woken by SIGCHLD via a self-pipe (``signal.set_wakeup_fd``
  — async-signal-safe) plus a conservative poll cadence. No zombie survives;
- subreaper: ``prctl(PR_SET_CHILD_SUBREAPER, 1)`` at start (Linux-only,
  loud refusal elsewhere) — orphaned grandchildren reparent into the
  supervisor and are reaped by the same drain (SL-05);
- PID1 mode (SL-06): same code paths; SIGTERM/SIGINT handlers are
  mandatory and installed in ``start()``; exit happens only after every
  child is stopped and reaped.

Isolation (§3.2, SL-13/14): the child environment is CONSTRUCTED —
``PATH`` (component venv bin dir + the fixed system string) +
``launch.env.vars`` + the env-file contents, with ``PYTHONNOUSERSITE=1``
forced unconditionally last (layout §3.8). No host variable leaks;
``close_fds=True`` and an empty ``pass_fds`` mean children inherit nothing
beyond stdio, and the future control socket's path never appears in a
child env by construction (W3 adds the socket; it is not part of env).

Restarts (§3.5): tier discipline from :class:`RestartPolicy` — core
restarts forever (exponential backoff base 1s x2 cap 30s jitter ±20%,
counter reset after 300s continuous uptime, crash-loop alert at 10
attempts without a healthy pass); optional restarts under a rolling
window budget (5 per 300s) — exhausted = ONE ERROR ``event=degraded``
line, then silent lazy-retries every 300s; a manual start resets the
budget. All policy numbers are injectable for tests (contract defaults
are the dataclass defaults).

Ordering (§3.5, SL-15/16): start is topological over ``depends_on``
(independent components proceed in parallel — one supervision thread
each, gated on healthy CORE dependencies); stop is reverse-topological
(dependents first), each child SIGTERM(group) → manifest grace_period →
SIGKILL(group), all inside a supervisor stop budget strictly below the
unit's ``TimeoutStopSec=90`` (≥ 25% margin).

History journal (§3.4, layout §3.7): every FSM transition of every
component AND every global-health change is appended to the append-only
journal as a §3.4 structural line with an ISO-8601 prefix. The published
logsink stream carries spawn/exit/degraded always and ``event=health``
only for the T4/T7/T9 transitions (the §3.4 semantics of that event);
the journal additionally carries one health-form record per FSM
transition so the journal alone is a complete transition record.
"""

from __future__ import annotations

import contextlib
import ctypes
import dataclasses
import hashlib
import importlib
import json
import logging
import os
import random
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema

from vesmaro.service.envfile import load_env_file
from vesmaro.service.errors import (
    ARTIFACT_HASH_MISMATCH,
    CONFIG_INVALID,
    ManifestError,
)
from vesmaro.service.fsm import (
    ChildFsm,
    ChildState,
    FsmEvent,
    TransitionError,
    TransitionResult,
)
from vesmaro.service.health import (
    Checker,
    ProbeParams,
    ProbeResult,
    build_checker,
    probe_params,
)
from vesmaro.service.layout import ensure_dir, resolve_component_paths
from vesmaro.service.logsink import (
    HistoryJournal,
    Logsink,
    SupervisorLine,
    build_degraded_line,
    build_exit_line,
    build_health_line,
    build_spawn_line,
    emit_child_forward,
)
from vesmaro.service.manifest import ComponentManifest, duration_to_ms
from vesmaro.service.placeholders import expand

logger = logging.getLogger("vesmaro.service.supervisor")

__all__ = [
    "CORE_BACKOFF_BASE_S",
    "CORE_BACKOFF_MAX_S",
    "CORE_BACKOFF_RESET_AFTER_S",
    "CORE_CRASH_LOOP_ATTEMPTS",
    "DEFAULT_STOP_GRACE_S",
    "FIXED_PATH_TAIL",
    "OPTIONAL_LAZY_RETRY_S",
    "OPTIONAL_WINDOW_ATTEMPTS",
    "OPTIONAL_WINDOW_S",
    "STOP_BUDGET_S",
    "ChildFsm",
    "ChildState",
    "Clock",
    "ExitRecord",
    "RestartPolicy",
    "Supervisor",
    "set_child_subreaper",
]

# ── Contract restart numbers (SL §3.5) ────────────────────────────────

CORE_BACKOFF_BASE_S = 1.0
CORE_BACKOFF_MAX_S = 30.0
CORE_BACKOFF_RESET_AFTER_S = 300.0
CORE_CRASH_LOOP_ATTEMPTS = 10
OPTIONAL_WINDOW_ATTEMPTS = 5
OPTIONAL_WINDOW_S = 300.0
OPTIONAL_LAZY_RETRY_S = 300.0
JITTER_SPAN = 0.2  # ±20%

# ── Stop numbers (SL §3.5: internal budget strictly < TimeoutStopSec=90,
#    margin ≥ 25%) ─────────────────────────────────────────────────────

STOP_BUDGET_S = 60.0
DEFAULT_STOP_GRACE_S = 10.0

# ── Child env canon (SL §3.2 + layout §3.8) ───────────────────────────

FIXED_PATH_TAIL = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
PYTHONNO_USERSITE = "1"

_SUPERVISOR_COMPONENT_TOKEN = "supervisor"
_SIGNAL_PREFIX = "SIG"


def _signal_number(name: str) -> int:
    """``SIGTERM`` → signal number (manifest stop.signal, CM §3.8)."""
    try:
        return int(signal.Signals[name.removeprefix(_SIGNAL_PREFIX)])
    except KeyError as exc:
        raise ValueError(f"unsupported stop signal: {name!r}") from exc


def _signal_name(signum: int) -> str:
    """Signal number → name WITHOUT the SIG prefix (exit line §3.4)."""
    return signal.Signals(signum).name


def set_child_subreaper(enable: bool = True) -> None:
    """prctl(PR_SET_CHILD_SUBREAPER) — Linux only, loud refusal elsewhere."""
    if sys.platform != "linux":
        raise RuntimeError(
            "vesma service supervisor is Linux-only (contract "
            "specs/service-lifecycle/v1 §3.1 mandates prctl subreaper); "
            f"refusing to run on {sys.platform!r}"
        )
    pr_set_child_subreaper = 36
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    libc.prctl.restype = ctypes.c_int
    result = libc.prctl(pr_set_child_subreaper, 1 if enable else 0, 0, 0, 0)
    if result != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"prctl(PR_SET_CHILD_SUBREAPER, {int(enable)}) failed")


# ── Restart policy ────────────────────────────────────────────────────


@dataclass(frozen=True)
class RestartPolicy:
    """Tier restart discipline (SL §3.5). Numbers are injectable for tests;
    the dataclass defaults ARE the contract numbers."""

    tier: str  # "core" | "optional"
    backoff_base_s: float = CORE_BACKOFF_BASE_S
    backoff_max_s: float = CORE_BACKOFF_MAX_S
    backoff_reset_after_s: float = CORE_BACKOFF_RESET_AFTER_S
    jitter_span: float = JITTER_SPAN
    # core tier — infinite restart under this crash-loop alert threshold:
    crash_loop_attempts: int = CORE_CRASH_LOOP_ATTEMPTS
    # optional tier — rolling window budget; None fields are ignored:
    window_attempts: int | None = OPTIONAL_WINDOW_ATTEMPTS
    window_s: float | None = OPTIONAL_WINDOW_S
    lazy_retry_s: float | None = OPTIONAL_LAZY_RETRY_S

    @classmethod
    def from_manifest(cls, manifest: ComponentManifest) -> RestartPolicy:
        """Tier discipline + ``restart.*`` overrides within the W1 clamps."""
        tier = manifest.metadata.tier
        if tier not in ("core", "optional"):
            raise ValueError(f"unknown component tier: {tier!r}")
        base = cls.backoff_base_s
        max_backoff = cls.backoff_max_s
        reset_after = cls.backoff_reset_after_s
        window_attempts: int | None = cls.window_attempts if tier == "optional" else None
        window_s: float | None = cls.window_s if tier == "optional" else None
        lazy_retry_s: float | None = cls.lazy_retry_s if tier == "optional" else None
        restart = manifest.restart
        if restart is not None:
            if restart.backoff is not None:
                # Clamps (CM §3.10) were enforced at load; re-assert defensively.
                base = max(0.5, duration_to_ms(restart.backoff.base) / 1000.0)
                max_backoff = min(300.0, duration_to_ms(restart.backoff.max) / 1000.0)
                reset_after = duration_to_ms(restart.backoff.reset_after) / 1000.0
            if restart.window is not None and tier == "optional":
                window_attempts = max(3, restart.window.attempts)
                window_s = duration_to_ms(restart.window.per) / 1000.0
        return cls(
            tier=tier,
            backoff_base_s=base,
            backoff_max_s=max_backoff,
            backoff_reset_after_s=reset_after,
            window_attempts=window_attempts,
            window_s=window_s,
            lazy_retry_s=lazy_retry_s,
        )

    def with_overrides(self, **overrides: Any) -> RestartPolicy:
        """Test seam: short windows/backoffs without touching manifests."""
        values = {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}
        values.update(overrides)
        return RestartPolicy(**values)


# ── Clock seam ────────────────────────────────────────────────────────


class Clock:
    """Monotonic time seam.

    Real mode waits on a condition in bounded chunks (state changes call
    :meth:`wake` — no missed-notification stalls). Fake mode holds a manual
    ``now``; :meth:`advance` moves it and wakes every waiter, so backoff,
    probe intervals and grace periods run deterministically in tests.
    """

    def __init__(self, *, fake: bool = False, start: float = 0.0) -> None:
        self._fake = fake
        self._t = start
        self._cond = threading.Condition()

    def now(self) -> float:
        if self._fake:
            with self._cond:
                return self._t
        return time.monotonic()

    def wake(self) -> None:
        """Interrupt every sleep — call after ANY state change a waiter checks."""
        with self._cond:
            self._cond.notify_all()

    def advance(self, seconds: float) -> None:
        """Fake mode only: move time and wake deadline waiters."""
        if not self._fake:
            raise RuntimeError("advance() is the fake-clock test seam")
        with self._cond:
            self._t += seconds
            self._cond.notify_all()

    def sleep_until(
        self,
        deadline: float | None,
        give_up: Callable[[], bool],
    ) -> bool:
        """Sleep until ``deadline`` (None = indefinitely).

        Returns True when the deadline was reached, False when ``give_up()``
        turned true first. ``give_up`` closures must read plain flags set
        BEFORE the setter calls :meth:`wake`.
        """
        with self._cond:
            while True:
                if give_up():
                    return False
                if deadline is not None:
                    remaining = deadline - (self._t if self._fake else time.monotonic())
                    if remaining <= 0:
                        return True
                if self._fake:
                    # advance()/wake() notify under this lock; the short cap
                    # also bounds give_up() flag changes made without wake().
                    self._cond.wait(0.05)
                else:
                    self._cond.wait(0.2)


# ── Exit records + reaper ─────────────────────────────────────────────


@dataclass(frozen=True)
class ExitRecord:
    """One reaped process (exit code or termination signal, §3.4 exit line)."""

    pid: int
    code: int | None  # None when terminated by a signal
    signal_name: str | None  # uppercase, no SIG prefix (KILL, TERM, ...)


def _decode_status(pid: int, status: int) -> ExitRecord | None:
    """waitpid status → ExitRecord; None for stop/continue notifications."""
    if os.WIFEXITED(status):
        return ExitRecord(pid, os.WEXITSTATUS(status), None)
    if os.WIFSIGNALED(status):
        return ExitRecord(pid, None, _signal_name(os.WTERMSIG(status)))
    return None  # WIFSTOPPED/WIFCONTINUED are not exits


class _Reaper:
    """waitpid(-1, WNOHANG) drain loop with a stop-grace pause (§3.1 MUST)."""

    def __init__(self, supervisor: Supervisor) -> None:
        self._supervisor = supervisor
        self._cond = threading.Condition()
        self._pause_depth = 0
        self.records: dict[int, ExitRecord] = {}
        self._thread: threading.Thread | None = None
        self._stopped = False

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="vesma-supervisor-reaper", daemon=True
        )
        self._thread.start()

    def notify(self) -> None:
        """SIGCHLD arrived (or anything reapable may exist)."""
        with self._cond:
            self._cond.notify_all()

    @contextlib.contextmanager
    def pause(self) -> Iterator[None]:
        """Defer reaping so a direct child pins its pgid during a group-kill
        sequence (the group-signal boundary, §3.1)."""
        with self._cond:
            self._pause_depth += 1
        try:
            yield
        finally:
            with self._cond:
                self._pause_depth -= 1
                self._cond.notify_all()

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify_all()

    def join(self, timeout: float) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _run(self) -> None:
        while True:
            with self._cond:
                if self._stopped:
                    return
                if self._pause_depth > 0:
                    self._cond.wait(0.2)
                    continue
            if not self.drain():
                # Idle cadence: SIGCHLD wakes us via notify(), the poll is the
                # conservative backstop (lost-wakeup races are bounded, not
                # assumed away).
                with self._cond:
                    if self._stopped:
                        return
                    self._cond.wait(0.2)

    def drain(self) -> bool:
        """waitpid(-1, WNOHANG) until ECHILD — no zombie ever survives (SL-05).

        Returns True when at least one process was reaped.
        """
        drained = False
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break  # ECHILD — nothing left to reap
            except InterruptedError:
                continue
            if pid == 0:
                break  # children exist, none has exited yet
            record = _decode_status(pid, status)
            if record is not None:
                with self._cond:
                    self.records[pid] = record
                    while len(self.records) > 1024:  # memory hygiene, not contract
                        self.records.pop(next(iter(self.records)))
                drained = True
                self._supervisor._on_process_reaped(record)
        if drained:
            self._supervisor._clock.wake()
        return drained

    def record_of(self, pid: int) -> ExitRecord | None:
        with self._cond:
            return self.records.get(pid)


# ── Per-component runtime state ───────────────────────────────────────


@dataclass
class _Component:
    manifest: ComponentManifest
    fsm: ChildFsm
    policy: RestartPolicy
    params: ProbeParams
    core_dependencies: tuple[str, ...]

    thread: threading.Thread | None = None
    process: subprocess.Popen[bytes] | None = None
    in_process: Any | None = None
    last_pid: int | None = None
    exit_record: ExitRecord | None = None
    last_probe_detail: str = ""
    checker: Checker | None = None

    # restart bookkeeping (SL §3.5)
    backoff_attempts: int = 0
    attempts_without_healthy: int = 0
    crash_loop_alerted: bool = False
    window: deque[float] = field(default_factory=deque)
    budget_exhausted: bool = False
    lazy_retry_at: float | None = None
    next_spawn_at: float | None = None
    last_spawn_at: float | None = None
    next_backoff_s: float | None = None

    # control-plane flags (plain flags + clock.wake() — no separate events)
    stop_requested: bool = False
    manual_start_pending: bool = False
    spawn_refused: bool = False

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def tier(self) -> str:
        return self.manifest.metadata.tier


# ── The supervisor ────────────────────────────────────────────────────


class Supervisor:
    """Lifecycle owner of one installation (service-lifecycle v1 §3)."""

    def __init__(
        self,
        manifests: Mapping[str, ComponentManifest],
        *,
        clock: Clock | None = None,
        logsink: Logsink | None = None,
        journal: HistoryJournal | None = None,
        jitter: Callable[[float], float] | None = None,
        component_configs: Mapping[str, Mapping[str, Any]] | None = None,
        stop_budget_s: float = STOP_BUDGET_S,
        default_stop_grace_s: float = DEFAULT_STOP_GRACE_S,
        pid1: bool | None = None,
    ) -> None:
        if not manifests:
            raise ValueError("an installation needs at least one component")
        self._clock = clock or Clock()
        self._sink = logsink
        self._journal = journal
        self._jitter = jitter or _default_jitter
        self._component_configs: Mapping[str, Mapping[str, Any]] = component_configs or {}
        self._stop_budget_s = stop_budget_s
        self._default_stop_grace_s = default_stop_grace_s
        self._pid1 = os.getpid() == 1 if pid1 is None else pid1

        self._components: dict[str, _Component] = {}
        for manifest in manifests.values():
            deps = tuple(
                dep
                for dep in manifest.depends_on
                if dep in manifests and manifests[dep].metadata.tier == "core"
            )
            for dep in manifest.depends_on:
                if dep not in manifests:
                    raise ValueError(f"component {manifest.name!r} depends on unknown {dep!r}")
            self._components[manifest.name] = _Component(
                manifest=manifest,
                fsm=ChildFsm(manifest.name),
                policy=RestartPolicy.from_manifest(manifest),
                params=probe_params(manifest),
                core_dependencies=deps,
            )
        missing = {d for c in self._components.values() for d in c.core_dependencies} - set(
            self._components
        )
        if missing:
            raise ValueError(f"core dependencies not in the installation: {sorted(missing)}")
        self._start_order = _topological_order(manifests)
        self._stop_order = list(reversed(self._start_order))

        self._reaper = _Reaper(self)
        self._state_lock = threading.Lock()
        self._global_health = "healthy"
        self._global_reasons: set[str] = set()
        self._shutdown_requested = False
        self._shutdown_done = False
        self._running = False
        self._signal_pipe: tuple[int, int] | None = None
        self._previous_handlers: dict[int, Any] = {}
        self._previous_wakeup_fd: int = -1
        self._dispatcher: threading.Thread | None = None
        self._exit_code = 0

    # ── introspection (board /status, W3 control plane) ──

    @property
    def global_health(self) -> str:
        with self._state_lock:
            return self._global_health

    @property
    def global_reasons(self) -> frozenset[str]:
        with self._state_lock:
            return frozenset(self._global_reasons)

    @property
    def pid1(self) -> bool:
        return self._pid1

    def component_state(self, name: str) -> ChildState | None:
        component = self._components.get(name)
        return component.fsm.state if component else None

    def snapshot(self) -> dict[str, Any]:
        """Read-only status snapshot (board /status; W3 status verb)."""
        with self._state_lock:
            return {
                "supervisor": {
                    "pid": os.getpid(),
                    "health": self._global_health,
                    "reasons": sorted(self._global_reasons),
                    "pid1": self._pid1,
                },
                "components": {
                    name: {
                        "state": component.fsm.state.value,
                        "pid": component.last_pid,
                        "restarts": component.backoff_attempts,
                        "detail": component.last_probe_detail,
                    }
                    for name, component in sorted(self._components.items())
                },
            }

    # ── lifecycle ──

    def start(self) -> None:
        """Set the subreaper, install signals, start the reaper and every
        component thread (topological launch order; gating per SL-15)."""
        if self._running:
            raise RuntimeError("supervisor already started")
        set_child_subreaper(True)
        self._install_signals()
        self._running = True
        self._reaper.start()
        for name in self._start_order:
            component = self._components[name]
            thread = threading.Thread(
                target=self._supervise,
                args=(component,),
                name=f"vesma-supervise-{name}",
                daemon=True,
            )
            component.thread = thread
            thread.start()
        if self._pid1:
            logger.info(
                "supervisor in PID1 mode: SIGTERM/SIGINT handlers mandatory "
                "and installed; orphans reaped; graceful stop before exit (SL-06)"
            )

    def request_shutdown(self) -> None:
        """Signal-safe shutdown request (signal handlers land here)."""
        self._shutdown_requested = True
        self._clock.wake()

    def run(self) -> int:
        """Blocking production loop: start, wait for a shutdown request,
        perform the graceful stop, return the exit code (PID1 §3.1)."""
        self.start()
        self._clock.sleep_until(None, give_up=lambda: self._shutdown_requested)
        return self.shutdown()

    def shutdown(self) -> int:
        """Graceful stop (SL-16): reverse-topological, per-child
        SIGTERM(group) → grace → SIGKILL(group), then reap everything and
        return only when no child/zombie of this installation remains."""
        if self._shutdown_done:
            return self._exit_code
        self._shutdown_requested = True
        self._clock.wake()
        budget_end = self._clock.now() + self._stop_budget_s
        for name in self._stop_order:
            self._stop_component(self._components[name], budget_end)
        # Final sweep: no pauses left — drain to ECHILD (PID1: §3.1 MUST).
        self._reaper.drain()
        for component in self._components.values():
            self._join_component(component)
        self._reaper.stop()
        if not self._reaper.join(5.0):
            logger.error("reaper thread did not stop cleanly")
            self._exit_code = 1
        self._restore_signals()
        if self._sink is not None:
            with contextlib.suppress(Exception):
                self._sink.close()
        self._shutdown_done = True
        self._running = False
        return self._exit_code

    # ── control-plane seams (W3 socket will wrap these) ──

    def request_start(self, name: str) -> None:
        """Manual start (CM/SL §3.5): resets the optional restart budget and
        revives refused/parked/terminal components."""
        component = self._components[name]
        component.manual_start_pending = True
        component.spawn_refused = False
        self._clock.wake()
        thread = component.thread
        if thread is None or not thread.is_alive():
            thread = threading.Thread(
                target=self._supervise,
                args=(component,),
                name=f"vesma-supervise-{name}",
                daemon=True,
            )
            component.thread = thread
            thread.start()

    def request_stop(self, name: str) -> None:
        """Manual stop of one component (T6/T13/T14 family)."""
        self._stop_component(self._components[name], None)

    # ── signals (SL-06: PID1 mandatory handlers) ──

    def _install_signals(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            if self._pid1:
                raise RuntimeError("PID1 mode requires signal handlers (main thread)")
            logger.info("supervisor started off the main thread: signal handlers skipped")
            return
        reader, writer = os.pipe()
        os.set_blocking(writer, False)
        self._signal_pipe = (reader, writer)
        self._previous_wakeup_fd = signal.set_wakeup_fd(writer)
        for signum in (signal.SIGCHLD, signal.SIGTERM, signal.SIGINT):
            self._previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signal.SIGCHLD, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        self._dispatcher = threading.Thread(
            target=self._dispatch_signals, args=(reader,), name="vesma-signal-dispatch", daemon=True
        )
        self._dispatcher.start()

    def _on_signal(self, signum: Any, frame: Any) -> None:
        # Handler signature requires both params; the wakeup fd already
        # delivered the signal number to the pipe. This handler exists to
        # override SIG_IGN/SIG_DFL inheritance and to keep Python's default
        # SIGINT KeyboardInterrupt out of the way.
        del signum, frame
        return None

    def _dispatch_signals(self, reader: int) -> None:
        while not self._shutdown_done:
            try:
                data = os.read(reader, 64)
            except (BlockingIOError, InterruptedError):
                self._clock_sleep_real(0.2)
                continue
            except OSError:
                return  # pipe closed at shutdown
            if not data:
                return
            for byte in data:
                if byte == int(signal.SIGCHLD):
                    self._reaper.notify()
                elif byte in (int(signal.SIGTERM), int(signal.SIGINT)):
                    self.request_shutdown()
                # any other signal number needs no supervision action

    @staticmethod
    def _clock_sleep_real(seconds: float) -> None:
        time.sleep(seconds)

    def _restore_signals(self) -> None:
        if self._signal_pipe is None:
            return
        reader, writer = self._signal_pipe
        signal.set_wakeup_fd(self._previous_wakeup_fd)
        for signum, handler in self._previous_handlers.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signum, handler)
        self._signal_pipe = None
        for fd in (reader, writer):
            with contextlib.suppress(OSError):
                os.close(fd)

    # ── reaper callbacks ──

    def _on_process_reaped(self, record: ExitRecord) -> None:
        """Reaper context: keep the owning Popen's returncode honest."""
        for component in self._components.values():
            if component.last_pid == record.pid and component.process is not None:
                component.process.returncode = (
                    -signal.Signals[record.signal_name].value
                    if record.signal_name is not None
                    else record.code
                )

    # ── component supervision thread ──

    def _supervise(self, component: _Component) -> None:
        """One thread per component (§3.2: no per-child path may kill the
        supervisor — the whole body runs under the SL-02 boundary guard)."""
        while not self._shutdown_requested:
            try:
                if not self._supervise_once(component):
                    return  # parked (refused / manual-stop) or stopping
            except Exception:  # SL-02 boundary, never propagates
                logger.exception(
                    "component %s: per-child boundary exception (SL-02)",
                    component.name,
                )
                self._recover_to_backoff(component)

    def _supervise_once(self, component: _Component) -> bool:
        # 1. dependency gate (SL-15: core dependency must be healthy first)
        if not self._gate_on_dependencies(component):
            return False  # parked in blocked or stopping
        if self._shutdown_requested or component.stop_requested:
            return False

        # 2. manual start bookkeeping
        if component.manual_start_pending:
            component.manual_start_pending = False
            self._reset_budget(component)

        # 3. spawn preparation — refusals park the component (fail-closed)
        prepared = self._prepare_spawn(component)
        if prepared is None:
            return False  # parked on refusal (artifact/env) until manual start

        # 4. transition into starting (T1 stopped / T12 backoff / already starting via T3)
        if component.fsm.state is ChildState.STOPPED:
            self._transition(component, FsmEvent.SPAWN)
        elif component.fsm.state is ChildState.BACKOFF:
            self._transition(component, FsmEvent.RESPAWN_DUE)

        # 5. spawn
        if not self._spawn(component, prepared):
            self._handle_exit(component, None)  # Popen failure == death before healthy
            return self._after_exit(component)

        # 6. run until the process exits / stop requested / window ends
        outcome = self._run_until_exit(component)
        if outcome == "stopped":
            return False  # shutdown/manual stop owns the STOP transition
        # 7. exit bookkeeping: budget/backoff/crash-loop decisions (§3.5)
        self._handle_exit(component, component.exit_record)
        return self._after_exit(component)

    def _gate_on_dependencies(self, component: _Component) -> bool:
        """True when every CORE dependency is healthy (SL-15, T2/T3)."""
        if not component.core_dependencies:
            return True

        def deps_healthy() -> bool:
            return all(
                self._components[dep].fsm.state is ChildState.HEALTHY
                for dep in component.core_dependencies
            )

        if deps_healthy():
            return True
        if component.fsm.state is ChildState.STOPPED:
            self._transition(component, FsmEvent.DEP_BLOCK)
        elif component.fsm.state is ChildState.BACKOFF:
            pass  # wait below while the core dep is down (no BACKOFF→BLOCKED row)
        elif component.fsm.state is not ChildState.BLOCKED:
            return True  # live states (starting/healthy/degraded) are past the gate
        # T3 unblocks; no timeout for core dependencies (SL-15)
        self._clock.sleep_until(
            None,
            give_up=lambda: (
                self._shutdown_requested
                or component.stop_requested
                or component.manual_start_pending
                or deps_healthy()
            ),
        )
        if self._shutdown_requested or component.stop_requested:
            return False
        if component.fsm.state is ChildState.BLOCKED and deps_healthy():
            self._transition(component, FsmEvent.DEP_UP)
        return not (self._shutdown_requested or component.stop_requested)

    def _prepare_spawn(
        self, component: _Component
    ) -> tuple[list[str], dict[str, str], Path] | None:
        """Build argv/env/cwd and run the spawn-time verifications.

        Returns None (and emits the ERROR refusal line) when the start must
        be refused: config/schema invalid (CM §4 CONFIG_INVALID), exec-argv
        placeholder unresolvable, artifact hash mismatch
        (ARTIFACT_HASH_MISMATCH) or env construction failure (fail-closed
        env file, SL-13)."""
        manifest = component.manifest
        try:
            paths = resolve_component_paths(component.name)
            expansion: dict[str, str] = {
                "config_path": str(paths.config_path),
                "data_dir": str(paths.data_dir),
                "runtime_dir": str(paths.runtime_dir),
            }
            if paths.venv_bin.is_dir():
                expansion["venv_bin"] = str(paths.venv_bin)
            self._validate_component_config(component)
            component.checker = build_checker(
                manifest,
                expansion=expansion,
                alive=lambda: self._component_alive(component),
            )
            if manifest.in_process is not None:
                return ([], {}, Path("."))
            launch = manifest.launch
            if launch is None:
                raise ValueError(f"child-process manifest {component.name!r} has no launch section")
            argv = expand(launch.argv, expansion)
            env_file = launch.env.env_file if launch.env is not None else None
            env = self._construct_env(component, env_file)
            cwd = (
                Path(launch.cwd).expanduser()
                if launch.cwd is not None
                else ensure_dir(paths.data_dir, 0o700)
            )
        except ManifestError as exc:
            self._refuse_start(component, exc)
            return None
        except (OSError, ValueError) as exc:
            self._refuse_start(component, exc)
            return None
        expected = manifest.metadata.provenance.artifact_sha256
        if expected is not None and not self._artifact_hash_ok(argv[0], expected):
            self._refuse_start(
                component,
                ManifestError(
                    ARTIFACT_HASH_MISMATCH,
                    "$.metadata.provenance.artifact_sha256",
                    f"launch artifact {argv[0]!r} hash does not match "
                    "artifact_sha256 — tamper or drift; reinstall the component",
                ),
            )
            return None
        return argv, env, cwd

    def _validate_component_config(self, component: _Component) -> None:
        """CM §3.9: invalid component config = start refusal (CONFIG_INVALID).

        The schema comes from ``config.schema_inline`` or ``config.schema_file``
        (a relative file resolves against the component's data dir). A config
        override supplied for a component that declares no schema is refused —
        fail-closed, no undocumented pass-through.
        """
        manifest = component.manifest
        config = self._component_configs.get(component.name)
        if manifest.config is None:
            if config:
                raise ManifestError(
                    CONFIG_INVALID,
                    "$.config",
                    f"component {component.name!r} declares no config schema — "
                    "refusing the non-empty config override (fail-closed)",
                )
            return
        if config is None:
            config = {}
        schema = manifest.config.schema_inline
        if schema is None:
            schema_file = manifest.config.schema_file
            if schema_file is None:
                return
            path = Path(schema_file).expanduser()
            if not path.is_absolute():
                path = resolve_component_paths(component.name).data_dir / path
            schema = json.loads(path.read_text(encoding="utf-8"))
        try:
            jsonschema.validate(config, schema)
        except jsonschema.ValidationError as exc:
            raise ManifestError(
                CONFIG_INVALID,
                "$.config",
                f"component {component.name!r} config does not match its schema "
                f"at /{'/'.join(str(p) for p in exc.absolute_path) or '<root>'}: "
                f"{exc.message}",
            ) from exc

    def _component_alive(self, component: _Component) -> bool:
        """The liveness predicate (CM §3.7): process alive / factory ran.

        Deliberately avoids ``Popen.poll`` — reaping belongs to the reaper
        thread alone; a poll could steal the waitpid and lose the exit.
        """
        if component.manifest.in_process is not None:
            return component.in_process is not None
        if component.last_pid is None:
            return False
        return self._reaper.record_of(component.last_pid) is None

    def _construct_env(self, component: _Component, env_file: str | None) -> dict[str, str]:
        """SL-13 env construction: constructed PATH + env.vars + env_file,
        then PYTHONNOUSERSITE=1 forced unconditionally (layout §3.8)."""
        paths = resolve_component_paths(component.name)
        path_entries = [str(paths.venv_bin)] if paths.venv_bin.is_dir() else []
        env: dict[str, str] = {"PATH": ":".join([*path_entries, FIXED_PATH_TAIL])}
        launch = component.manifest.launch
        if launch is not None and launch.env is not None:
            env.update(launch.env.vars)
            if env_file is not None:
                # Fail-closed W1 loader: ANY violation refuses the start.
                env.update(load_env_file(env_file, manifests_dir=component.manifest.path.parent))
        env["PYTHONNOUSERSITE"] = PYTHONNO_USERSITE
        return env

    @staticmethod
    def _artifact_hash_ok(artifact: str, expected_hex: str) -> bool:
        try:
            digest = hashlib.sha256()
            with open(artifact, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
        except OSError:
            return False
        return digest.hexdigest() == expected_hex

    def _refuse_start(self, component: _Component, exc: Exception) -> None:
        """Fail-closed start refusal: ERROR structural line, component parked."""
        component.spawn_refused = True
        code = exc.code if isinstance(exc, ManifestError) else "SPAWN_REFUSED"
        reason = str(code).lower().replace("_", "-")
        logger.error("component %s: start refused: %s", component.name, exc)
        line = build_degraded_line(
            component.name,
            component.last_pid,
            reason=reason,
            attempts=component.backoff_attempts,
            window_s=None,
        )
        self._publish(line)
        self._clock.wake()

    def _spawn(
        self, component: _Component, prepared: tuple[list[str], dict[str, str], Path]
    ) -> bool:
        """Spawn the component instance; False when the spawn itself failed.

        ``attempts_without_healthy`` is NOT reset here — it counts spawns
        without a healthy pass and is cleared on T4 (HEALTH_OK) and on a
        budget reset, which is what makes the core crash-loop counter work.
        """
        now = self._clock.now()
        component.exit_record = None
        manifest = component.manifest
        attempt = component.backoff_attempts + 1
        pid: int | None = None
        try:
            if manifest.in_process is not None:
                self._spawn_in_process(component)
            else:
                argv, env, cwd = prepared
                process = subprocess.Popen(  # nosec B603 - argv from the validated manifest, no shell
                    argv,
                    cwd=str(cwd),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,  # setsid: pgid == pid (SL-03)
                    close_fds=True,  # SL-14: children inherit nothing beyond stdio
                )
                component.process = process
                component.last_pid = process.pid
                pid = process.pid
                actual_pgid = os.getpgid(process.pid)
                if actual_pgid != process.pid:
                    raise RuntimeError(f"SL-03 violated: pgid {actual_pgid} != pid {process.pid}")
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        threading.Thread(
                            target=self._forward_child_output,
                            args=(component.name, stream),
                            name=f"vesma-forward-{component.name}",
                            daemon=True,
                        ).start()
        except Exception as exc:  # per-child failure is a child failure (SL-02)
            logger.warning(
                "component %s: spawn failed: %s: %s",
                component.name,
                exc.__class__.__name__,
                exc,
            )
            component.exit_record = None
            return False
        line = build_spawn_line(component.name, pid, attempt=attempt if attempt > 1 else None)
        self._publish(line)
        component.last_spawn_at = now
        self._clock.wake()
        return True

    def _spawn_in_process(self, component: _Component) -> None:
        """§3.1/§3.2: in-process modules are trust-equivalent first-party code.

        CM §3.6: the entrypoint factory is strictly ``() -> Component``. The
        validated component config travels through the module-level
        ``register_config`` hook (called BEFORE the factory); the snapshot
        provider travels through ``register_state_provider`` (after it).
        """
        declaration = component.manifest.in_process
        if declaration is None:
            raise ValueError(f"component {component.name!r} has no in_process section")
        module = importlib.import_module(declaration.module)
        config = dict(self._component_configs.get(component.name) or {})
        register_config = getattr(module, "register_config", None)
        if callable(register_config):
            register_config(config)
        factory = getattr(module, declaration.entrypoint)
        instance = factory()
        component.in_process = instance
        provider = getattr(module, "register_state_provider", None)
        if callable(provider):
            provider(self.snapshot)

    def _forward_child_output(self, component: str, stream: Any) -> None:
        """Forward one child output stream under vesma-<component> (§3.4).

        The stream is ALWAYS drained — even when no sink is configured, a
        full stdout pipe would block the child mid-run (isolation §3.2).
        """
        sink = self._sink
        try:
            for raw in iter(stream.readline, b""):
                if sink is not None:
                    emit_child_forward(sink, component, raw.decode("utf-8", errors="replace"))
        finally:
            with contextlib.suppress(OSError):
                stream.close()

    # ── probes + exit detection (the run loop) ──

    def _run_until_exit(self, component: _Component) -> str:
        """Probe loop from spawn to death (or stop). Returns "stopped" when a
        stop/shutdown took ownership, else "exited"."""
        params = component.params
        spawn_at = self._clock.now()
        startup_deadline = spawn_at + params.startup_grace_s
        next_probe_at = spawn_at + params.startup_interval_s
        streak = 0
        while True:
            record = self._reaper.record_of(component.last_pid) if component.last_pid else None
            if record is not None:
                component.exit_record = record
                return "exited"
            if self._shutdown_requested or component.stop_requested:
                return "stopped"
            state = component.fsm.state
            if state is ChildState.STARTING:
                if self._clock.now() >= next_probe_at:
                    result = self._probe(component)
                    next_probe_at = self._clock.now() + params.startup_interval_s
                    if result.ok:
                        self._transition(component, FsmEvent.HEALTH_OK)  # T4 — «up»
                        component.attempts_without_healthy = 0
                        if component.budget_exhausted:
                            # Recovery re-arms the rolling budget: a crash
                            # AFTER a recovered episode gets the full window
                            # again (a stale terminal flag would otherwise
                            # silently suppress every future alert).
                            component.budget_exhausted = False
                            component.window.clear()
                            component.lazy_retry_at = None
                        if component.crash_loop_alerted:
                            component.crash_loop_alerted = False
                            self._global_health_recompute()
                        streak = 0
                        continue
                if self._clock.now() >= startup_deadline:
                    # Startup window exhausted → kill + exit-equivalent (T5 family).
                    logger.warning(
                        "component %s: startup window (%.1fs) exhausted without a "
                        "healthy pass; stopping the instance",
                        component.name,
                        params.startup_grace_s,
                    )
                    self._stop_child(component, None)
                    return "exited"
                self._clock.sleep_until(
                    min(next_probe_at, startup_deadline), self._exit_give_up(component)
                )
                continue
            # steady state: HEALTHY or DEGRADED
            if self._clock.now() < next_probe_at:
                self._clock.sleep_until(next_probe_at, self._exit_give_up(component))
                continue
            result = self._probe(component)
            next_probe_at = self._clock.now() + params.steady_interval_s
            if result.ok:
                streak = 0
                if state is ChildState.DEGRADED:
                    self._transition(component, FsmEvent.HEALTH_RECOVER)  # T9
                continue
            component.last_probe_detail = result.detail
            streak += 1
            if state is ChildState.HEALTHY and streak >= params.unhealthy_threshold:
                self._transition(component, FsmEvent.HEALTH_FAIL)  # T7

    def _probe(self, component: _Component) -> ProbeResult:
        checker = component.checker
        if checker is None:
            return ProbeResult(True, "no checker")
        try:
            result = checker.check()
        except Exception as exc:  # probes never kill the supervisor (SL-02)
            logger.exception("component %s: checker crashed (SL-02)", component.name)
            return ProbeResult(False, f"checker crashed: {exc.__class__.__name__}")
        component.last_probe_detail = result.detail
        return result

    def _exit_give_up(self, component: _Component) -> Callable[[], bool]:
        def give_up() -> bool:
            if self._shutdown_requested or component.stop_requested:
                return True
            return (
                component.last_pid is not None
                and self._reaper.record_of(component.last_pid) is not None
            )

        return give_up

    # ── exit → backoff / budget / crash-loop (§3.5) ──

    def _handle_exit(self, component: _Component, record: ExitRecord | None) -> None:
        """Apply the exit event with the correct budget_exhausted flag."""
        budget_exhausted = self._budget_exhausted(component)
        self._transition(component, FsmEvent.EXIT, budget_exhausted=budget_exhausted)
        if record is not None:
            self._publish(
                build_exit_line(
                    component.name,
                    record.pid,
                    code=record.code,
                    signal_name=record.signal_name,
                )
            )
        # else: spawn-level failure (no process ever existed) — the transition
        # record in the journal is the evidence.

    def _budget_exhausted(self, component: _Component) -> bool:
        policy = component.policy
        if policy.window_attempts is None or policy.window_s is None:
            return False  # core tier: infinite restart discipline
        if component.budget_exhausted:
            return True  # lazy-retry deaths keep the terminal state (T11 self-loop)
        now = self._clock.now()
        while component.window and now - component.window[0] > policy.window_s:
            component.window.popleft()
        if len(component.window) >= policy.window_attempts:
            component.budget_exhausted = True
            component.lazy_retry_at = now + (policy.lazy_retry_s or OPTIONAL_LAZY_RETRY_S)
            self._publish(
                build_degraded_line(
                    component.name,
                    component.last_pid,
                    reason="restart-budget-exhausted",
                    attempts=policy.window_attempts,
                    window_s=policy.window_s,
                )
            )
            return True
        component.window.append(now)
        return False

    def _after_exit(self, component: _Component) -> bool:
        """Post-exit: terminal degraded (lazy-retry) or backoff scheduling."""
        if self._shutdown_requested or component.stop_requested:
            return False
        if component.budget_exhausted:
            # optional tier, terminal degraded: silent lazy-retry (SL-10).
            # The budget stays EXHAUSTED across natural lazy-retry wakes —
            # the next death re-enters this branch through the T11
            # self-loop WITHOUT a new ERROR line (alert-silence is the
            # point); only a MANUAL start resets the budget (SL-10).
            deadline = component.lazy_retry_at or (
                self._clock.now() + (component.policy.lazy_retry_s or OPTIONAL_LAZY_RETRY_S)
            )
            component.lazy_retry_at = deadline
            self._clock.sleep_until(
                deadline,
                lambda: (
                    self._shutdown_requested
                    or component.stop_requested
                    or component.manual_start_pending
                ),
            )
            if self._shutdown_requested or component.stop_requested:
                return False
            if component.manual_start_pending:
                return True  # loop head resets the budget and respawns
            self._transition(component, FsmEvent.STOP)  # degraded → stopped (T14 family)
            return True  # fresh T1 spawn on the next pass
        # backoff scheduling (core + optional with live budget)
        self._schedule_backoff(component)
        deadline = self._clock.now() + (component.next_backoff_s or 0.0)
        component.next_spawn_at = deadline
        self._clock.sleep_until(
            deadline,
            lambda: (
                self._shutdown_requested
                or component.stop_requested
                or component.manual_start_pending
            ),
        )
        if self._shutdown_requested or component.stop_requested:
            return False
        if component.manual_start_pending:
            self._reset_budget(component)
            self._transition(component, FsmEvent.STOP)  # backoff → stopped (T13)
            return True
        self._transition(component, FsmEvent.RESPAWN_DUE)  # T12
        return True

    def _schedule_backoff(self, component: _Component) -> None:
        policy = component.policy
        now = self._clock.now()
        spawn_at = component.last_spawn_at
        uptime = now - spawn_at if spawn_at is not None else 0.0
        if uptime >= policy.backoff_reset_after_s:
            component.backoff_attempts = 0  # reset after continuous uptime (SL-12)
        component.backoff_attempts += 1
        exponent = min(component.backoff_attempts - 1, 16)
        raw = min(policy.backoff_base_s * (2**exponent), policy.backoff_max_s)
        component.next_backoff_s = self._jitter(raw)
        if component.tier == "core":
            component.attempts_without_healthy += 1
            if (
                not component.crash_loop_alerted
                and component.attempts_without_healthy >= policy.crash_loop_attempts
            ):
                component.crash_loop_alerted = True
                self._publish(
                    build_degraded_line(
                        component.name,
                        component.last_pid,
                        reason="crash-loop",
                        attempts=policy.crash_loop_attempts,
                        window_s=None,
                    )
                )
                self._global_health_recompute()

    def _recover_to_backoff(self, component: _Component) -> None:
        """SL-02 guard: after a per-child exception steer the FSM to backoff."""
        try:
            state = component.fsm.state
            if state in (ChildState.STARTING, ChildState.HEALTHY, ChildState.DEGRADED):
                self._transition(component, FsmEvent.EXIT)
                self._schedule_backoff(component)
                component.next_spawn_at = self._clock.now() + (component.next_backoff_s or 0.0)
        except TransitionError:
            logger.exception("component %s: recovery transition refused", component.name)

    def _reset_budget(self, component: _Component) -> None:
        """Manual start (SL-10): fresh budget, terminal state cleared."""
        component.window.clear()
        component.budget_exhausted = False
        component.lazy_retry_at = None
        component.attempts_without_healthy = 0
        component.crash_loop_alerted = False
        component.spawn_refused = False
        self._global_health_recompute()

    # ── stopping ──

    def _stop_component(self, component: _Component, budget_end: float | None) -> None:
        """Stop one component: FSM → stopped, then the group-signal sequence."""
        component.stop_requested = True
        self._clock.wake()
        if component.fsm.state is not ChildState.STOPPED:
            try:
                self._transition(component, FsmEvent.STOP)
            except TransitionError:
                logger.exception("component %s: stop transition refused", component.name)
        self._stop_child(component, budget_end)
        instance = component.in_process
        if instance is not None:
            shutdown = getattr(instance, "shutdown", None)
            if callable(shutdown):
                with contextlib.suppress(Exception):
                    shutdown()
        thread = component.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def _stop_child(self, component: _Component, budget_end: float | None) -> None:
        """SIGTERM(group) → grace → SIGKILL(group) with the pgid-reuse
        boundary (§3.1): group signals ONLY while the direct child is
        unreaped — the reaper is paused for the whole sequence."""
        pid = component.last_pid
        if pid is None or component.exit_record is not None:
            return  # nothing alive; a reaped pgid is NEVER signalled again
        reaped = self._reaper.record_of(pid)
        if reaped is not None:
            component.exit_record = reaped
            return
        stop_signal = "SIGTERM"
        stop_section = component.manifest.stop
        if stop_section is not None:
            stop_signal = stop_section.signal
        signum = _signal_number(stop_signal)
        grace_s = (
            duration_to_ms(stop_section.grace_period) / 1000.0
            if stop_section is not None
            else self._default_stop_grace_s
        )
        if budget_end is not None:
            grace_s = max(0.0, min(grace_s, budget_end - self._clock.now()))
        with self._reaper.pause():
            self._signal_group(pgid=pid, signum=signum)
            deadline = self._clock.now() + grace_s
            while not self._proc_is_dead(pid):
                if not self._clock.sleep_until(
                    deadline,
                    lambda: self._shutdown_requested or self._proc_is_dead(pid),
                ):
                    break
                if self._clock.now() >= deadline:
                    break
            if not self._proc_is_dead(pid):
                self._signal_group(pid, signal.SIGKILL)
        # reaper resumed: the exit will be recorded; wait for it (bounded)
        waited_until = time.monotonic() + 10.0
        while self._reaper.record_of(pid) is None and time.monotonic() < waited_until:
            if self._reaper.record_of(pid) is not None:
                break
            time.sleep(0.01)
        record = self._reaper.record_of(pid)
        if record is None:
            logger.error("component %s: child %d was not reaped after SIGKILL", component.name, pid)
            self._exit_code = 1
        else:
            component.exit_record = record

    @staticmethod
    def _signal_group(pgid: int, signum: int) -> None:
        """kill(-pgid, …) — legal ONLY while the direct child is unreaped."""
        try:
            os.killpg(pgid, signum)
        except ProcessLookupError:
            pass  # group already gone
        except PermissionError:
            logger.warning("killpg(%d, %d): permission denied", pgid, signum)

    @staticmethod
    def _proc_is_dead(pid: int) -> bool:
        """True once the direct child is a zombie (or gone) — readable via
        /proc WITHOUT reaping it, which is what keeps the pgid pinned."""
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except OSError:
            return True  # gone
        state = stat.rpartition(")")[2].split()[0]
        return state == "Z"

    def _join_component(self, component: _Component) -> None:
        thread = component.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
            if thread.is_alive():
                logger.error("component %s: supervision thread did not stop", component.name)
                self._exit_code = 1

    # ── FSM + publishing ──

    def _transition(
        self,
        component: _Component,
        event: FsmEvent,
        *,
        budget_exhausted: bool = False,
    ) -> TransitionResult:
        result = component.fsm.transition(event, budget_exhausted=budget_exhausted)
        pid = component.last_pid
        line = build_health_line(
            component.name, pid, result.from_state.value, result.to_state.value
        )
        rendered = line.render()
        journal = self._journal
        if journal is not None:
            journal.append(rendered)
        if result.event in (FsmEvent.HEALTH_OK, FsmEvent.HEALTH_FAIL, FsmEvent.HEALTH_RECOVER):
            self._emit(line)
        self._clock.wake()
        self._global_health_recompute()
        return result

    def _publish(self, line: SupervisorLine) -> None:
        """One structural line → history journal (always) + logsink."""
        rendered = line.render()
        if self._journal is not None:
            self._journal.append(rendered)
        self._emit(line)

    def _emit(self, line: SupervisorLine) -> None:
        if self._sink is None:
            return
        self._sink.emit("vesma-supervisor", line.render(), severity=line.severity)

    # ── global health flag (SL §3.3 note — NOT an FSM state) ──

    def _global_health_recompute(self) -> None:
        reasons: set[str] = set()
        for component in self._components.values():
            if component.tier != "core":
                continue
            if component.fsm.state is ChildState.BACKOFF:
                reasons.add(f"dead:{component.name}")
            if component.crash_loop_alerted:
                reasons.add(f"crash-loop:{component.name}")
        new_health = "degraded" if reasons else "healthy"
        with self._state_lock:
            changed = new_health != self._global_health
            self._global_reasons = reasons
            previous = self._global_health
            self._global_health = new_health
        if changed:
            line = build_health_line(_SUPERVISOR_COMPONENT_TOKEN, None, previous, new_health)
            rendered = line.render()
            if self._journal is not None:
                self._journal.append(rendered)
            self._emit(line)


def _default_jitter(value: float) -> float:
    return value * (1.0 + random.uniform(-JITTER_SPAN, JITTER_SPAN))


def _topological_order(manifests: Mapping[str, ComponentManifest]) -> list[str]:
    """Kahn topological order (dependencies first), name tie-break."""
    remaining = {name: set(manifest.depends_on) for name, manifest in manifests.items()}
    order: list[str] = []
    while remaining:
        ready = sorted(name for name, deps in remaining.items() if not deps)
        if not ready:
            raise ValueError(
                "depends_on cycle in the installation: " + ", ".join(sorted(remaining))
            )
        for name in ready:
            order.append(name)
            del remaining[name]
        for deps in remaining.values():
            deps.difference_update(ready)
    return order
