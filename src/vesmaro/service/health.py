"""Health checkers (component-manifest v1 §3.7; FSM feeds — SL §3.3 T4/T7/T9).

One checker per manifest ``health`` section; the supervisor runs them and
maps results onto FSM events (never the other way around):

- ``http`` — GET with a socket timeout; 2xx/3xx = pass;
- ``tcp``  — connect probe;
- ``exec`` — argv probe (placeholders expanded by the supervisor), rc == 0;
- ``liveness`` — no probe block: "process alive" for children, "module
  imported and factory ran" for in-process components (CM §3.7);
- ``callback`` — in-process only (``module:attr``, signature
  ``() -> {"state": healthy|degraded|failed, "detail"?: str}``). CM §3.7/D
  isolation boundary: the call runs in a worker thread with a timeout, and
  ANY exception/timeout/bad shape is a ``failed`` result with
  ``CALLBACK_FAILED`` logged — a broken callback can never propagate into
  the supervisor.

Probe timing (CM §3.7): the ``startup`` section defines the start window —
``grace`` is the TOTAL budget for the first successful pass, ``interval``
the probe period, ``timeout`` the per-probe timeout inside that window;
without a startup section the defaults below apply. Steady-state numbers
(``interval``/``timeout``/``unhealthy_threshold``) come from the probe
block; ``liveness``/``callback`` (which carry no block of their own) use
the module defaults.
"""

from __future__ import annotations

import dataclasses
import importlib
import logging
import socket
import subprocess
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from vesmaro.service.errors import CALLBACK_FAILED
from vesmaro.service.manifest import ComponentManifest, duration_to_ms
from vesmaro.service.placeholders import expand

logger = logging.getLogger("vesmaro.service.health")

__all__ = [
    "CALLBACK_TIMEOUT_S",
    "LIVENESS_INTERVAL_S",
    "LIVENESS_THRESHOLD",
    "LIVENESS_TIMEOUT_S",
    "STARTUP_DEFAULT_GRACE_S",
    "STARTUP_DEFAULT_INTERVAL_S",
    "CallbackChecker",
    "Checker",
    "ExecChecker",
    "HttpChecker",
    "LivenessChecker",
    "ProbeParams",
    "ProbeResult",
    "TcpChecker",
    "build_checker",
    "parse_module_attr",
    "probe_params",
]

# Module defaults for checkers whose manifest section carries no timing
# (liveness, callback). Probe blocks (http/tcp/exec) always carry their own.
STARTUP_DEFAULT_GRACE_S = 30.0
STARTUP_DEFAULT_INTERVAL_S = 5.0
LIVENESS_INTERVAL_S = 5.0
LIVENESS_TIMEOUT_S = 2.0
LIVENESS_THRESHOLD = 1
CALLBACK_TIMEOUT_S = 2.0

_HEALTHY_STATES = frozenset({"healthy", "degraded", "failed"})


@dataclasses.dataclass(frozen=True)
class ProbeResult:
    """One probe outcome (``ok`` maps to FSM health events, never raises)."""

    ok: bool
    detail: str


class Checker(Protocol):
    """A health probe: pure check, never raises, never blocks past its timeout."""

    name: str

    def check(self) -> ProbeResult: ...


@dataclasses.dataclass(frozen=True)
class ProbeParams:
    """Timing knobs the supervisor's probe loops read (injectable in tests)."""

    startup_grace_s: float
    startup_interval_s: float
    startup_timeout_s: float
    steady_interval_s: float
    steady_timeout_s: float
    unhealthy_threshold: int


def parse_module_attr(spec: str) -> tuple[str, str]:
    """Split a CM §3.7 ``module:attr`` reference into its two parts."""
    module, sep, attr = spec.partition(":")
    if not sep or not module or not attr:
        raise ValueError(f"health callback {spec!r} must be of the form 'module:attr' (CM §3.7)")
    return module, attr


# ── Checkers ──────────────────────────────────────────────────────────


class HttpChecker:
    """GET probe: 2xx/3xx passes, anything else (incl. network errors) fails."""

    def __init__(self, url: str, timeout_s: float) -> None:
        self.name = "http"
        self._url = url
        self._timeout_s = timeout_s

    def check(self) -> ProbeResult:
        try:
            with urllib.request.urlopen(self._url, timeout=self._timeout_s) as response:
                status = response.status
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return ProbeResult(False, f"http probe error: {exc.__class__.__name__}")
        ok = 200 <= status < 400
        return ProbeResult(ok, f"http status {status}")


class TcpChecker:
    """Connect probe: a successful TCP connect passes."""

    def __init__(self, host: str, port: int, timeout_s: float) -> None:
        self.name = "tcp"
        self._host = host
        self._port = port
        self._timeout_s = timeout_s

    def check(self) -> ProbeResult:
        try:
            with socket.create_connection((self._host, self._port), timeout=self._timeout_s):
                return ProbeResult(True, f"tcp connect {self._host}:{self._port}")
        except OSError as exc:
            return ProbeResult(False, f"tcp connect failed: {exc.__class__.__name__}")


class ExecChecker:
    """argv probe (no shell — the manifest loader already rejects metacharacters)."""

    def __init__(self, argv: list[str], timeout_s: float) -> None:
        self.name = "exec"
        self._argv = argv
        self._timeout_s = timeout_s

    def check(self) -> ProbeResult:
        try:
            completed = subprocess.run(  # nosec B603 - argv from validated manifest, no shell
                self._argv,
                capture_output=True,
                timeout=self._timeout_s,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return ProbeResult(False, f"exec probe error: {exc.__class__.__name__}")
        return ProbeResult(completed.returncode == 0, f"exec rc={completed.returncode}")


class LivenessChecker:
    """No probe block: the supervisor injects the liveness predicate.

    child-process: "process alive"; in-process: "module imported and the
    factory ran" (the supervisor only builds the checker after both, so the
    injected predicate is the honest one for the kind).
    """

    def __init__(self, alive: Callable[[], bool], description: str) -> None:
        self.name = "liveness"
        self._alive = alive
        self._description = description

    def check(self) -> ProbeResult:
        ok = self._alive()
        return ProbeResult(ok, self._description if ok else f"{self._description}: not alive")


class CallbackChecker:
    """In-process callback (``module:attr``) behind the CM §3.7/D boundary.

    The callback runs on a worker thread; resolution errors, exceptions,
    timeouts and malformed results ALL collapse to a ``failed`` probe with
    ``CALLBACK_FAILED`` logged — nothing ever propagates.
    """

    def __init__(self, spec: str, timeout_s: float) -> None:
        self.name = "callback"
        self._spec = spec
        self._timeout_s = timeout_s
        self._resolved: Callable[[], Any] | None = None

    def _callable(self) -> Callable[[], Any]:
        if self._resolved is not None:
            return self._resolved
        module_name, attr = parse_module_attr(self._spec)
        module = importlib.import_module(module_name)
        target: Callable[[], Any] = getattr(module, attr)
        if not callable(target):
            raise TypeError(f"health callback {self._spec!r} is not callable")
        self._resolved = target
        return target

    def check(self) -> ProbeResult:
        try:
            callback = self._callable()
        except Exception as exc:  # isolation boundary — resolution failures are probe failures
            logger.warning(
                "%s: callback resolution failed: %s: %s",
                CALLBACK_FAILED,
                exc.__class__.__name__,
                exc,
            )
            return ProbeResult(False, f"{CALLBACK_FAILED}: callback not resolvable")

        outcome: dict[str, Any] = {}

        def _run() -> None:
            try:
                outcome["result"] = callback()
            except Exception as exc:  # isolation boundary (CM §3.7/D)
                outcome["error"] = exc

        worker = threading.Thread(target=_run, name=f"vesma-callback-{self._spec}", daemon=True)
        worker.start()
        worker.join(self._timeout_s)
        if worker.is_alive():
            logger.warning(
                "%s: callback timed out after %.1fs (worker thread left running; "
                "a hung callback leaks one thread per probe)",
                CALLBACK_FAILED,
                self._timeout_s,
            )
            return ProbeResult(False, f"{CALLBACK_FAILED}: timeout after {self._timeout_s}s")
        error = outcome.get("error")
        if error is not None:
            logger.warning(
                "%s: callback raised: %s: %s",
                CALLBACK_FAILED,
                error.__class__.__name__,
                error,
            )
            return ProbeResult(False, f"{CALLBACK_FAILED}: {error.__class__.__name__}")
        result = outcome.get("result")
        if not isinstance(result, dict):
            logger.warning(
                "%s: callback returned %s, expected a {'state': ...} mapping",
                CALLBACK_FAILED,
                type(result).__name__,
            )
            return ProbeResult(False, f"{CALLBACK_FAILED}: malformed result")
        state = result.get("state")
        if state not in _HEALTHY_STATES:
            logger.warning(
                "%s: callback state %r outside {healthy, degraded, failed}",
                CALLBACK_FAILED,
                state,
            )
            return ProbeResult(False, f"{CALLBACK_FAILED}: bad state {state!r}")
        detail = str(result.get("detail", ""))
        return ProbeResult(state == "healthy", f"callback state={state} {detail}".strip())


# ── Builders (manifest → checker + timing) ────────────────────────────


def _duration_s(value: str) -> float:
    return duration_to_ms(value) / 1000.0


def probe_params(manifest: ComponentManifest) -> ProbeParams:
    """Timing knobs for one component's probes (CM §3.7 + module defaults)."""
    health = manifest.health
    if health is None:
        return ProbeParams(
            startup_grace_s=STARTUP_DEFAULT_GRACE_S,
            startup_interval_s=STARTUP_DEFAULT_INTERVAL_S,
            startup_timeout_s=LIVENESS_TIMEOUT_S,
            steady_interval_s=LIVENESS_INTERVAL_S,
            steady_timeout_s=LIVENESS_TIMEOUT_S,
            unhealthy_threshold=LIVENESS_THRESHOLD,
        )
    startup = health.startup
    if health.http is not None:
        steady_interval = _duration_s(health.http.interval)
        steady_timeout = _duration_s(health.http.timeout)
        threshold = health.http.unhealthy_threshold
    elif health.tcp is not None:
        steady_interval = _duration_s(health.tcp.interval)
        steady_timeout = _duration_s(health.tcp.timeout)
        threshold = health.tcp.unhealthy_threshold
    elif health.exec is not None:
        steady_interval = _duration_s(health.exec.interval)
        steady_timeout = _duration_s(health.exec.timeout)
        threshold = health.exec.unhealthy_threshold
    else:  # liveness / callback — no probe block of their own
        steady_interval = LIVENESS_INTERVAL_S
        steady_timeout = CALLBACK_TIMEOUT_S if health.callback else LIVENESS_TIMEOUT_S
        threshold = LIVENESS_THRESHOLD
    return ProbeParams(
        startup_grace_s=_duration_s(startup.grace) if startup else STARTUP_DEFAULT_GRACE_S,
        startup_interval_s=(
            _duration_s(startup.interval) if startup else STARTUP_DEFAULT_INTERVAL_S
        ),
        startup_timeout_s=(
            _duration_s(startup.timeout)
            if startup
            else (steady_timeout if health.callback is None else CALLBACK_TIMEOUT_S)
        ),
        steady_interval_s=steady_interval,
        steady_timeout_s=steady_timeout,
        unhealthy_threshold=threshold,
    )


def build_checker(
    manifest: ComponentManifest,
    *,
    expansion: Mapping[str, str],
    alive: Callable[[], bool],
) -> Checker:
    """Build the checker the manifest declares (CM §3.7 if/then consistency
    is enforced by the manifest loader; this builder mirrors it defensively)."""
    health = manifest.health
    if health is None:
        kind = "child process" if manifest.kind == "child-process" else "in-process module"
        return LivenessChecker(alive, f"{kind} alive")
    if health.http is not None:
        return HttpChecker(health.http.url, _duration_s(health.http.timeout))
    if health.tcp is not None:
        return TcpChecker(health.tcp.host, health.tcp.port, _duration_s(health.tcp.timeout))
    if health.exec is not None:
        argv = expand(health.exec.argv, expansion)
        return ExecChecker(argv, _duration_s(health.exec.timeout))
    if health.callback is not None:
        startup = health.startup
        timeout = _duration_s(startup.timeout) if startup else CALLBACK_TIMEOUT_S
        return CallbackChecker(health.callback, timeout)
    # Unreachable for loader-validated manifests (checker↔block consistency);
    # defensive fallthrough keeps the supervisor alive even on hostile input.
    return LivenessChecker(alive, "manifest declared no checker block")
