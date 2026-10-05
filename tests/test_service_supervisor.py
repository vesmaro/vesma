"""Supervisor conformance (service-lifecycle v1 §3.1/3.2/3.5 — SL-01…SL-16).

Every test names the checklist item it executes. Known deferred legs
(honest gaps, reported in the wave summary):

- SL-01 socket-accepts leg — the control socket is W3 scope; supervisor
  aliveness is asserted via snapshot()/FSM instead;
- SL-06 true-container leg — ``unshare`` is refused in this sandbox; the
  test probes at runtime and skips with that reason; the handler/reap
  unit legs run for real;
- SL-08 journald-leg live coverage — file mode is covered end-to-end;
  the journald sink itself is unit-covered against a bound datagram
  socket in tests/test_service_logsink.py.

Tests drive REAL child processes (tests/service_children/*) with REAL
signals where the item demands process semantics, and the fake Clock
where deterministic time is needed (SL-09/SL-11/SL-12). No sleeps where
a seam exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from vesmaro.service.logsink import HistoryJournal
from vesmaro.service.manifest import ComponentManifest, load_manifest
from vesmaro.service.supervisor import (
    CORE_BACKOFF_BASE_S,
    CORE_BACKOFF_MAX_S,
    CORE_BACKOFF_RESET_AFTER_S,
    CORE_CRASH_LOOP_ATTEMPTS,
    FIXED_PATH_TAIL,
    OPTIONAL_LAZY_RETRY_S,
    OPTIONAL_WINDOW_ATTEMPTS,
    OPTIONAL_WINDOW_S,
    Clock,
    RestartPolicy,
    Supervisor,
)

CHILDREN = Path(__file__).parent / "service_children"

# ── grammar regex (SL-08 mirror of §3.4) ──────────────────────────────

_PREFIX_RE = (
    r"^vesma\.supervisor component=[a-z0-9_.-]+ "
    r"event=(spawn|exit|health|degraded) pid=(none|[0-9]+)"
)
_EXIT_FIELDS_RE = r"^code=(none|[0-9]+) signal=(none|[A-Z][A-Z0-9]{0,15})$"
_DEGRADED_FIELDS_RE = r"^state=degraded reason=[a-z0-9_.-]+ attempts=[0-9]+ window=(none|[0-9]+s)$"
_HEALTH_FIELDS_RE = r"^from=[a-z0-9_.-]+ to=[a-z0-9_.-]+$"
_SPAWN_FIELDS_RE = r"^(attempt=[0-9]+)?$"
_EVENT_FIELDS = {
    "spawn": _SPAWN_FIELDS_RE,
    "exit": _EXIT_FIELDS_RE,
    "health": _HEALTH_FIELDS_RE,
    "degraded": _DEGRADED_FIELDS_RE,
}


# ── fixtures + helpers ────────────────────────────────────────────────


@pytest.fixture
def isolated_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate HOME + every XDG root so nothing leaks into the real home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    return home


_PY_ARTIFACT_HASH: str | None = None


def _python_artifact_hash() -> str:
    """CM §3.3: tier core REQUIRES provenance.artifact_sha256; the supervisor
    hashes argv[0] — for conformance children that is sys.executable."""
    global _PY_ARTIFACT_HASH
    if _PY_ARTIFACT_HASH is None:
        digest = hashlib.sha256()
        with open(sys.executable, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        _PY_ARTIFACT_HASH = digest.hexdigest()
    return _PY_ARTIFACT_HASH


def _child_doc(
    name: str,
    script: str,
    args: tuple[str, ...] = (),
    *,
    tier: str = "optional",
    health: dict[str, Any] | None = None,
    depends_on: tuple[str, ...] = (),
    stop: dict[str, Any] | None = None,
    env_vars: dict[str, str] | None = None,
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "apiVersion": "vesma.component/v1",
        "kind": "child-process",
        "metadata": {
            "name": name,
            "version": "0.1.0",
            "tier": tier,
            "description": f"conformance child {name}",
            "provenance": {"repo": "https://example.com/x", "license": "MIT"},
        },
        "launch": {"argv": [sys.executable, str(CHILDREN / script), *args]},
        "depends_on": list(depends_on),
        # CM §3.8: the stop section is REQUIRED for child-process manifests.
        "stop": stop or {"signal": "SIGTERM", "grace_period": "2s"},
    }
    if tier == "core":
        doc["metadata"]["provenance"]["artifact_sha256"] = _python_artifact_hash()
    if env_vars:
        doc["launch"]["env"] = {"vars": env_vars}
    if health is not None:
        doc["health"] = health
    return doc


def _in_process_doc(
    name: str,
    module: str,
    *,
    entrypoint: str = "create_component",
    callback: str | None = None,
    tier: str = "optional",
    config_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "apiVersion": "vesma.component/v1",
        "kind": "in-process",
        "metadata": {
            "name": name,
            "version": "0.1.0",
            "tier": tier,
            "description": f"conformance in-process {name}",
            "provenance": {"repo": "https://example.com/x", "license": "MIT"},
        },
        "in_process": {"module": module, "entrypoint": entrypoint},
    }
    if callback is not None:
        doc["health"] = {"checker": "callback", "callback": callback}
    if config_schema is not None:
        doc["config"] = {"schema_inline": config_schema}
    return doc


def _write_manifests(
    tmp_path: Path, docs: dict[str, dict[str, Any]]
) -> dict[str, ComponentManifest]:
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir(exist_ok=True)
    loaded: dict[str, ComponentManifest] = {}
    for name, doc in docs.items():
        path = manifests_dir / f"{name}.yaml"
        path.write_text(yaml.safe_dump(doc), encoding="utf-8")
        loaded[name] = load_manifest(path)
    return loaded


def _make_supervisor(
    tmp_path: Path,
    manifests: dict[str, ComponentManifest],
    *,
    clock: Clock | None = None,
    jitter: Any = None,
    component_configs: dict[str, dict[str, Any]] | None = None,
    **kw: Any,
) -> Supervisor:
    journal = HistoryJournal(directory=tmp_path / "history")
    return Supervisor(
        manifests,
        clock=clock,
        journal=journal,
        jitter=jitter,
        component_configs=component_configs or {},
        **kw,
    )


def _journal(tmp_path: Path) -> HistoryJournal:
    return HistoryJournal(directory=tmp_path / "history")


def _records(tmp_path: Path) -> list[str]:
    """Journal lines without the ISO-8601 prefix."""
    return [line.partition(" ")[2] for line in _journal(tmp_path).read_records()]


def _override_policy(supervisor: Supervisor, name: str, **overrides: Any) -> None:
    component = supervisor._components[name]
    component.policy = component.policy.with_overrides(**overrides)


def _fast_backoff(supervisor: Supervisor, name: str) -> None:
    _override_policy(supervisor, name, backoff_base_s=0.1, backoff_max_s=0.3)


def _wait_until(predicate: Any, message: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {message}")


def _pid_of(supervisor: Supervisor, name: str) -> int:
    pid = supervisor.snapshot()["components"][name]["pid"]
    assert pid is not None
    return int(pid)


def _pid_exists(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            return fh.read().rpartition(")")[2].split()[0] != "Z"
    except OSError:
        return False


def _zombie(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            return fh.read().rpartition(")")[2].split()[0] == "Z"
    except OSError:
        return False


def _flag_probe(flag: Path) -> dict[str, Any]:
    return {
        "checker": "exec",
        "exec": {
            "argv": ["/usr/bin/test", "-f", str(flag)],
            "interval": "100ms",
            "timeout": "1s",
            "unhealthy_threshold": 1,
        },
        "startup": {"grace": "30s", "interval": "100ms", "timeout": "1s"},
    }


# ── contract constants are the RestartPolicy defaults (SL §3.5) ───────


class TestContractConstants:
    def test_restart_policy_defaults_are_the_contract_numbers(self) -> None:
        policy = RestartPolicy(tier="core")
        assert policy.backoff_base_s == CORE_BACKOFF_BASE_S == 1.0
        assert policy.backoff_max_s == CORE_BACKOFF_MAX_S == 30.0
        assert policy.backoff_reset_after_s == CORE_BACKOFF_RESET_AFTER_S == 300.0
        assert policy.crash_loop_attempts == CORE_CRASH_LOOP_ATTEMPTS == 10
        assert policy.jitter_span == pytest.approx(0.2)
        optional = RestartPolicy(tier="optional")
        assert optional.window_attempts == OPTIONAL_WINDOW_ATTEMPTS == 5
        assert optional.window_s == OPTIONAL_WINDOW_S == 300.0
        assert optional.lazy_retry_s == OPTIONAL_LAZY_RETRY_S == 300.0

    def test_fixed_path_tail_matches_the_contract_string(self) -> None:
        assert FIXED_PATH_TAIL == "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

    def test_no_pkill_anywhere_in_the_service_package(self) -> None:
        # SL-04 hand leg: inventory-by-name is the legacy anti-pattern;
        # stopping is kill(-pgid) by construction.
        service_dir = Path(__file__).parents[1] / "src" / "vesmaro" / "service"
        offenders = [
            str(path)
            for path in service_dir.rglob("*.py")
            if "pkill" in path.read_text(encoding="utf-8")
        ]
        assert offenders == []


# ── SL-01 isolation: kill -9 a subset, supervisor lives ───────────────


class TestSL01Isolation:
    def test_kill9_subset_leaves_supervisor_alive_with_backoffs(
        self, isolated_xdg: Path, tmp_path: Path
    ) -> None:
        manifests = _write_manifests(
            tmp_path,
            {
                "a": _child_doc("a", "sleeper.py"),
                "b": _child_doc("b", "sleeper.py"),
                "c": _child_doc("c", "sleeper.py"),
            },
        )
        sup = _make_supervisor(tmp_path, manifests)
        for name in ("a", "b", "c"):
            _fast_backoff(sup, name)
        sup.start()
        try:
            _wait_until(
                lambda: all(sup.component_state(n) == "healthy" for n in ("a", "b", "c")),
                "all children healthy",
            )
            killed_a, killed_b = _pid_of(sup, "a"), _pid_of(sup, "b")
            os.kill(killed_a, signal.SIGKILL)
            os.kill(killed_b, signal.SIGKILL)
            _wait_until(
                lambda: (
                    sup.component_state("a") == "backoff" and sup.component_state("b") == "backoff"
                ),
                "killed children in backoff",
            )
            assert sup.global_health == "healthy"  # supervisor alive + serving
            assert sup.snapshot()["supervisor"]["pid"] == os.getpid()
            assert _pid_of(sup, "c") not in (killed_a, killed_b)
            _wait_until(
                lambda: (
                    sup.component_state("a") == "healthy" and sup.component_state("b") == "healthy"
                ),
                "killed children restarted back to healthy",
                timeout=15,
            )
            records = _records(tmp_path)
            kill_exits = [ln for ln in records if "event=exit" in ln and "signal=KILL" in ln]
            assert len(kill_exits) >= 2
            respawns = [ln for ln in records if "event=spawn" in ln and "attempt=2" in ln]
            assert len(respawns) >= 2
        finally:
            sup.shutdown()

    def test_single_kill9_stays_within_budget_no_degradation(
        self, isolated_xdg: Path, tmp_path: Path
    ) -> None:
        manifests = _write_manifests(tmp_path, {"one": _child_doc("one", "sleeper.py")})
        sup = _make_supervisor(tmp_path, manifests)
        _fast_backoff(sup, "one")
        sup.start()
        try:
            _wait_until(lambda: sup.component_state("one") == "healthy", "one healthy")
            os.kill(_pid_of(sup, "one"), signal.SIGKILL)
            _wait_until(
                lambda: sup.component_state("one") == "healthy",
                "one restarted after SIGKILL",
                timeout=15,
            )
            assert sup.global_health == "healthy"
            assert not [ln for ln in _records(tmp_path) if "event=degraded" in ln]
        finally:
            sup.shutdown()


# ── SL-02 per-child exception boundary ────────────────────────────────


class TestSL02ExceptionBoundary:
    def test_per_child_exception_never_kills_the_main_loop(
        self, isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manifests = _write_manifests(
            tmp_path,
            {"sick": _child_doc("sick", "sleeper.py"), "well": _child_doc("well", "sleeper.py")},
        )
        sup = _make_supervisor(tmp_path, manifests)
        original_probe = sup._probe

        def poisoned_probe(component: Any) -> Any:
            if component.name == "sick":
                raise RuntimeError("injected per-child failure (SL-02)")
            return original_probe(component)

        monkeypatch.setattr(sup, "_probe", poisoned_probe)
        sup.start()
        try:
            _wait_until(lambda: sup.component_state("well") == "healthy", "well healthy")
            _wait_until(
                lambda: sup.component_state("sick") == "backoff",
                "sick steered to backoff by the SL-02 boundary guard",
            )
            assert sup.snapshot()["supervisor"]["pid"] == os.getpid()
        finally:
            sup.shutdown()


# ── SL-03 own session: pgid == pid ────────────────────────────────────


class TestSL03Sessions:
    def test_child_pgid_equals_pid(self, isolated_xdg: Path, tmp_path: Path) -> None:
        manifests = _write_manifests(tmp_path, {"solo": _child_doc("solo", "sleeper.py")})
        sup = _make_supervisor(tmp_path, manifests)
        sup.start()
        try:
            _wait_until(lambda: sup.component_state("solo") == "healthy", "solo healthy")
            pid = _pid_of(sup, "solo")
            stat_fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            after_comm = stat_fields.rpartition(")")[2].split()
            assert after_comm[0] in ("S", "R")  # alive
            assert int(after_comm[2]) == pid  # field 5 (1-based): pgrp == pid
        finally:
            sup.shutdown()


# ── SL-04 group stop: SIGTERM -> grace -> SIGKILL, never pkill ────────


class TestSL04GroupStop:
    def test_stubborn_grandchild_dies_with_group_sigkill(
        self, isolated_xdg: Path, tmp_path: Path
    ) -> None:
        out = tmp_path / "stubborn.json"
        manifests = _write_manifests(
            tmp_path,
            {
                "stubborn": _child_doc(
                    "stubborn",
                    "stubborn.py",
                    ("--out", str(out)),
                    stop={"signal": "SIGTERM", "grace_period": "1s"},
                )
            },
        )
        sup = _make_supervisor(tmp_path, manifests)
        sup.start()
        try:
            _wait_until(lambda: sup.component_state("stubborn") == "healthy", "stubborn healthy")
            payload = json.loads(out.read_text(encoding="utf-8"))
            pid, grandchild = int(payload["pid"]), int(payload["grandchild"])
            assert _pid_exists(pid) and _pid_exists(grandchild)
            sup.request_stop("stubborn")
            _wait_until(lambda: sup.component_state("stubborn") == "stopped", "stopped")
            _wait_until(
                lambda: not _pid_exists(pid) and not _pid_exists(grandchild),
                "SIGTERM-ignoring group died via SIGKILL",
                timeout=15,
            )
        finally:
            sup.shutdown()


# ── SL-05 subreaper: orphaned grandchild reaped, no zombie ───────────


class TestSL05Subreaper:
    def test_orphaned_grandchild_reaped_by_supervisor(
        self, isolated_xdg: Path, tmp_path: Path
    ) -> None:
        out = tmp_path / "orphan.json"
        manifests = _write_manifests(
            tmp_path,
            {"maker": _child_doc("maker", "orphan_maker.py", ("--out", str(out)))},
        )
        sup = _make_supervisor(tmp_path, manifests)
        sup.start()
        try:
            _wait_until(lambda: out.exists(), "orphan maker wrote its payload")
            grandchild = int(json.loads(out.read_text(encoding="utf-8"))["grandchild"])
            _wait_until(
                lambda: not os.path.exists(f"/proc/{grandchild}"),
                "orphaned grandchild fully reaped (not even a zombie)",
                timeout=15,
            )
            assert not _zombie(grandchild)
            _wait_until(
                lambda: sup.component_state("maker") == "backoff",
                "maker exited and went to backoff",
            )
        finally:
            sup.shutdown()


# ── SL-06 PID1 mode ───────────────────────────────────────────────────


class TestSL06Pid1:
    def test_pid1_unit_legs_sigterm_graceful_stop_exit_zero(
        self, isolated_xdg: Path, tmp_path: Path
    ) -> None:
        manifests = _write_manifests(tmp_path, {"svc": _child_doc("svc", "sleeper.py")})
        sup = _make_supervisor(tmp_path, manifests, pid1=True)
        assert sup.pid1 is True
        # PID1 handlers are installed on start() and REQUIRE the main thread
        # (contract SL §3.1); the blocking run() loop itself is exercised in
        # the unshare driver (skipped here — honest gap note).
        sup.start()
        try:
            _wait_until(lambda: sup.component_state("svc") == "healthy", "svc healthy")
            pid = _pid_of(sup, "svc")
            os.kill(os.getpid(), signal.SIGTERM)  # contract's default stop signal
            _wait_until(
                lambda: sup._shutdown_requested,
                "SIGTERM handler -> request_shutdown (PID1 MUST: handlers installed)",
            )
            code = sup.shutdown()
            assert code == 0
            _wait_until(lambda: not _pid_exists(pid), "child stopped and reaped before exit")
            assert not _pid_exists(pid) and not _zombie(pid)
        finally:
            sup.shutdown()

    def test_pid1_container_leg_via_unshare(self, isolated_xdg: Path, tmp_path: Path) -> None:
        manifests_dir = tmp_path / "manifests"
        manifests_dir.mkdir()
        doc = _child_doc("svc", "sleeper.py", stop={"signal": "SIGTERM", "grace_period": "2s"})
        (manifests_dir / "svc.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
        try:
            probe = subprocess.run(
                ["unshare", "--pid", "--fork", "true"],
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            probe = None
        if probe is None or probe.returncode != 0:
            pytest.skip(
                "unshare refused in this sandbox (SL-06 container leg: honest gap); "
                "handler/reap unit legs covered by test_pid1_unit_legs"
            )
        driver = subprocess.run(
            [
                sys.executable,
                str(CHILDREN / "pid1_driver.py"),
                "--manifest",
                str(manifests_dir / "svc.yaml"),
                "--journal-dir",
                str(tmp_path / "history"),
            ],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert driver.returncode == 0, driver.stderr
        summary = json.loads(driver.stdout.strip().splitlines()[-1])
        assert summary["ok"] is True
        assert summary["pid1"] is True
        assert summary["child_gone"] is True


# ── SL-08 structural-line grammar over a full scripted run ────────────


class TestSL08Grammar:
    def test_every_emitted_line_matches_the_normative_grammar(
        self, isolated_xdg: Path, tmp_path: Path
    ) -> None:
        flag = tmp_path / "flag"
        flag.write_text("on", encoding="utf-8")
        manifests = _write_manifests(
            tmp_path,
            {
                # core tier: restarts FOREVER — an optional component would
                # burn its 5-per-window budget within the fast-backoff cycles
                # and park in terminal degraded (T11), hiding the exit legs.
                "boom": _child_doc("boom", "exiter.py", ("--code", "1"), tier="core"),
                "dummy": _in_process_doc(
                    "dummy",
                    "tests.service_children.inproc_dummy",
                    callback="tests.service_children.inproc_dummy:health",
                ),
                "flagged": _child_doc("flagged", "sleeper.py", health=_flag_probe(flag)),
                "victim": _child_doc("victim", "sleeper.py", tier="core"),
            },
        )
        sup = _make_supervisor(tmp_path, manifests)
        _fast_backoff(sup, "boom")
        sup.start()
        try:
            _wait_until(lambda: sup.component_state("dummy") == "healthy", "dummy healthy")
            _wait_until(lambda: sup.component_state("flagged") == "healthy", "flagged healthy")
            _wait_until(lambda: sup.component_state("victim") == "healthy", "victim healthy")
            # boom: exit by CODE — journal carries `code=1 signal=none`.
            _wait_until(lambda: sup.component_state("boom") == "backoff", "boom exit recorded")
            # victim: exit BY SIGNAL — a LIVE sleeper is SIGKILLed, so there
            # is no race between "state is starting" and the kill strike.
            os.kill(_pid_of(sup, "victim"), signal.SIGKILL)
            _wait_until(
                lambda: sup.component_state("victim") == "backoff", "victim SIGKILL recorded"
            )
            sup.request_stop("boom")  # park the exiter so the run stays small
            # T7 + T9 on flagged: probe failure -> degraded, recovery -> healthy.
            flag.unlink()
            _wait_until(lambda: sup.component_state("flagged") == "degraded", "T7 degraded")
            flag.write_text("on", encoding="utf-8")
            _wait_until(lambda: sup.component_state("flagged") == "healthy", "T9 recovery")
        finally:
            sup.shutdown()

        lines = _records(tmp_path)
        assert lines, "journal must not be empty"
        for line in lines:
            match = re.match(_PREFIX_RE, line)
            assert match is not None, f"grammar violation: {line!r}"
            event = match.group(1)
            rest = line[match.end() :].strip()
            assert re.match(_EVENT_FIELDS[event], rest) is not None, f"{event} fields: {line!r}"

        joined = "\n".join(lines)
        assert "event=spawn pid=none" in joined  # in-process component: pid=none
        assert re.search(r"event=exit pid=[0-9]+ code=1 signal=none", joined)
        assert re.search(r"event=exit pid=[0-9]+ code=none signal=KILL", joined)
        assert "from=stopped to=starting" in joined  # T1 journal record
        assert "from=starting to=healthy" in joined  # T4
        assert "from=healthy to=degraded" in joined  # T7
        assert "from=degraded to=healthy" in joined  # T9

    def test_exit_code_and_signal_are_mutually_exclusive(
        self, isolated_xdg: Path, tmp_path: Path
    ) -> None:
        manifests = _write_manifests(tmp_path, {"x": _child_doc("x", "exiter.py", ("--code", "0"))})
        sup = _make_supervisor(tmp_path, manifests)
        sup.start()
        try:
            _wait_until(lambda: sup.component_state("x") == "backoff", "clean exit recorded")
        finally:
            sup.shutdown()
        exits = [ln for ln in _records(tmp_path) if "event=exit" in ln]
        assert exits, "an exit line must exist"
        for line in exits:
            code_part = line.split("code=")[1].split()[0]
            signal_part = line.split("signal=")[1].split()[0]
            assert (code_part == "none") != (signal_part == "none")


# ── severity-capturing sink (the ERROR-line counter for SL-09/SL-10/SL-11) ─


class _CapturingSink:
    """Test Logsink: records every (identifier, severity, line) emission."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.records: list[tuple[str, str, str]] = []

    def emit(self, identifier: str, line: str, *, severity: str = "INFO") -> None:
        with self._lock:
            self.records.append((identifier, severity, line))

    def close(self) -> None:
        return None

    def error_lines(self) -> list[str]:
        with self._lock:
            return [line for _ident, severity, line in self.records if severity == "ERROR"]


def _error_count(sink: _CapturingSink) -> int:
    return len(sink.error_lines())


# ── SL-09 optional budget exhaustion: exactly ONE exact ERROR line ────


class TestSL09OptionalBudgetExhaustion:
    def test_exactly_one_error_line_with_exact_fields(
        self, isolated_xdg: Path, tmp_path: Path
    ) -> None:
        manifests = _write_manifests(
            tmp_path, {"opt": _child_doc("opt", "exiter.py", ("--code", "1"))}
        )
        sink = _CapturingSink()
        sup = _make_supervisor(tmp_path, manifests, logsink=sink)
        _override_policy(
            sup,
            "opt",
            backoff_base_s=0.05,
            backoff_max_s=0.1,
            window_attempts=3,
            window_s=300.0,
            lazy_retry_s=5.0,  # long dwell: nothing moves while we assert
        )
        sup.start()
        try:
            _wait_until(lambda: _error_count(sink) == 1, "the ONE ERROR line", timeout=15)
            pid = _pid_of(sup, "opt")
            assert sink.error_lines() == [
                f"vesma.supervisor component=opt event=degraded pid={pid} "
                "state=degraded reason=restart-budget-exhausted attempts=3 window=300s"
            ]
            assert sup.component_state("opt") == "degraded"  # T11 terminal state
            degraded_rows = [ln for ln in _records(tmp_path) if "event=degraded" in ln]
            assert len(degraded_rows) == 1  # the journal carries it exactly once too
        finally:
            sup.shutdown()


# ── SL-10 lazy-retry silence + manual start resets the budget ─────────


class TestSL10LazyRetry:
    def test_lazy_retries_stay_silent_and_manual_start_resets(
        self, isolated_xdg: Path, tmp_path: Path
    ) -> None:
        manifests = _write_manifests(
            tmp_path, {"opt": _child_doc("opt", "exiter.py", ("--code", "1"))}
        )
        sink = _CapturingSink()
        sup = _make_supervisor(tmp_path, manifests, logsink=sink)
        _override_policy(
            sup,
            "opt",
            backoff_base_s=0.05,
            backoff_max_s=0.1,
            window_attempts=2,
            window_s=300.0,
            lazy_retry_s=0.4,
        )
        sup.start()
        try:
            _wait_until(lambda: _error_count(sink) == 1, "exhaustion ERROR line", timeout=15)

            def lazy_spawns() -> list[float]:
                stamps = []
                for record in _journal(tmp_path).read_records():
                    stamp, _, line = record.partition(" ")
                    if "component=opt event=spawn" in line:
                        stamps.append(datetime.fromisoformat(stamp).timestamp())
                return stamps

            _wait_until(
                lambda: len(lazy_spawns()) >= 4,  # initial burn (2) + ≥ 2 lazy retries
                "two silent lazy-retry cycles",
                timeout=30,
            )
            assert _error_count(sink) == 1  # SL-10: no alert spam in lazy mode
            gaps = [b - a for a, b in zip(lazy_spawns()[2:], lazy_spawns()[3:], strict=False)]
            assert gaps and all(0.2 < gap < 2.5 for gap in gaps), f"lazy cadence: {gaps}"

            # Manual start resets the budget (§3.5): a fresh burn re-alerts.
            sup.request_start("opt")
            _wait_until(
                lambda: _error_count(sink) == 2,
                "post-reset exhaustion produces a SECOND alert",
                timeout=15,
            )
        finally:
            sup.shutdown()


# ── SL-11 core crash-loop at 10 → global degraded + alert, no stall ───


class TestSL11CoreCrashLoop:
    def test_crash_loop_alerts_once_and_restarts_continue(
        self, isolated_xdg: Path, tmp_path: Path
    ) -> None:
        manifests = _write_manifests(
            tmp_path, {"core": _child_doc("core", "exiter.py", ("--code", "1"), tier="core")}
        )
        sink = _CapturingSink()
        sup = _make_supervisor(tmp_path, manifests, logsink=sink, jitter=lambda value: value)
        _override_policy(sup, "core", backoff_base_s=0.05, backoff_max_s=0.1)
        sup.start()
        try:
            # Wait on the ALERT LINE, not the health flag: the flag also
            # degrades on the first `dead:core` backoff, long before the
            # 10th attempt that makes the alert mandatory.
            _wait_until(lambda: _error_count(sink) == 1, "crash-loop alert", timeout=20)
            assert sup.global_health == "degraded"
            # Reasons: the crash-loop reason PLUS the dead-in-backoff reason
            # (core is in backoff at alert time).
            assert sup.global_reasons == frozenset({"dead:core", "crash-loop:core"})
            # The alert line: exact tail per §3.5 (`window=none`), ERROR level.
            errors = sink.error_lines()
            assert len(errors) == 1
            assert errors[0].endswith("state=degraded reason=crash-loop attempts=10 window=none")
            assert errors[0].startswith("vesma.supervisor component=core event=degraded pid=")

            # Restarts CONTINUE after the alert — the supervisor never stalls.
            def spawn_count() -> int:
                return sum(1 for ln in _records(tmp_path) if "component=core event=spawn" in ln)

            baseline = spawn_count()
            _wait_until(lambda: spawn_count() >= baseline + 3, "restarts continue", timeout=15)
            assert _error_count(sink) == 1  # exactly ONE crash-loop alert, ever
            assert sup.snapshot()["supervisor"]["pid"] == os.getpid()
        finally:
            sup.shutdown()


# ── SL-12 core backoff intervals (injected clock) + uptime reset ──────


class TestSL12CoreBackoffTiming:
    def test_interval_ladder_is_exponential_capped_and_resets(self, tmp_path: Path) -> None:
        """The ladder drives `_schedule_backoff` directly on the fake clock:
        a process-level fake-clock loop would race the dwell-entry (an
        advance landing between the deadline computation and the sleep is
        consumed invisibly), so the timing math is tested as the pure
        policy it is; the dwell → respawn mechanics run end-to-end with
        real clocks in SL-09/10/11."""
        manifests = _write_manifests(
            tmp_path, {"core": _child_doc("core", "sleeper.py", tier="core")}
        )
        clock = Clock(fake=True)
        sup = _make_supervisor(tmp_path, manifests, clock=clock, jitter=lambda value: value)
        component = sup._components["core"]

        # The ladder: base 1s, x2 per attempt, capped at 30s (§3.5 — the
        # dataclass defaults themselves are pinned by TestContractConstants).
        seen: list[float] = []
        for _ in range(7):
            sup._schedule_backoff(component)
            seen.append(float(component.next_backoff_s))
        assert seen == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]

        # The reset rule reads continuous uptime off the injected clock.
        component.last_spawn_at = clock.now() - 301.0
        sup._schedule_backoff(component)
        assert component.next_backoff_s == 1.0
        assert component.backoff_attempts == 1

    def test_uptime_reset_end_to_end_with_injected_threshold(
        self, isolated_xdg: Path, tmp_path: Path
    ) -> None:
        """Real-clock end-to-end: the reset fires on a real death after the
        (injected, shortened) continuous-uptime threshold — without the
        reset the second death would show attempts=2 and a doubled interval."""
        manifests = _write_manifests(
            tmp_path, {"core": _child_doc("core", "sleeper.py", tier="core")}
        )
        sup = _make_supervisor(tmp_path, manifests, jitter=lambda value: value)
        _override_policy(
            sup,
            "core",
            backoff_base_s=0.5,
            backoff_max_s=1.0,
            backoff_reset_after_s=2.0,
        )
        sup.start()
        try:
            _wait_until(
                lambda: sup.component_state("core") == "healthy", "core healthy", timeout=15
            )
            os.kill(_pid_of(sup, "core"), signal.SIGKILL)
            _wait_until(
                lambda: sup.component_state("core") == "backoff",
                "first death → backoff",
            )
            component = sup._components["core"]
            assert component.next_backoff_s == 0.5

            _wait_until(
                lambda: sup.component_state("core") == "healthy",
                "core healthy again",
                timeout=15,
            )
            time.sleep(2.5)  # continuous uptime crosses the injected 2s threshold
            os.kill(_pid_of(sup, "core"), signal.SIGKILL)
            _wait_until(
                lambda: (
                    sup.component_state("core") == "backoff"
                    and sup._components["core"].backoff_attempts == 1
                ),
                "reset after continuous uptime: fresh attempt count",
            )
            assert sup._components["core"].next_backoff_s == 0.5  # no reset → 1.0
        finally:
            sup.shutdown()


# ── SL-13 env semantics: the constructed set, nothing else ────────────


class TestSL13ChildEnv:
    def test_env_is_exactly_the_constructed_set(
        self, isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VESMA_HOST_LEAK_PROBE", "sentinel-401")  # the 401-storm probe
        out = tmp_path / "env.json"
        manifests = _write_manifests(
            tmp_path,
            {
                "envy": _child_doc(
                    "envy",
                    "env_printer.py",
                    ("--out", str(out)),
                    env_vars={"VESMA_TEST_VAR": "w2-value"},
                )
            },
        )
        sup = _make_supervisor(tmp_path, manifests)
        sup.start()
        try:
            _wait_until(lambda: out.exists(), "env payload written", timeout=10)
            payload = json.loads(out.read_text(encoding="utf-8"))
            # No venv exists for this component → PATH is the fixed tail only.
            expected = {
                "PATH": FIXED_PATH_TAIL,
                "VESMA_TEST_VAR": "w2-value",
                "PYTHONNOUSERSITE": "1",  # forced last (layout §3.8)
            }
            child_env = dict(payload["env"])
            # PEP 538: CPython coerces the C locale at startup and sets
            # LC_CTYPE in its OWN environment — a self-inflicted artifact of
            # the child runtime, not a host leak (the constructed env carries
            # no LC_* at all).
            coerced = child_env.pop("LC_CTYPE", None)
            assert coerced in (None, "C.UTF-8")
            assert child_env == expected  # env -i: EXACT set, no host leaks
        finally:
            sup.shutdown()


# ── SL-14 fd/socket isolation across the spawn boundary ───────────────


class TestSL14SocketIsolation:
    def test_children_inherit_no_sockets_and_no_socket_path(
        self, isolated_xdg: Path, tmp_path: Path
    ) -> None:
        out = tmp_path / "fds.json"
        manifests = _write_manifests(
            tmp_path, {"watched": _child_doc("watched", "env_printer.py", ("--out", str(out)))}
        )
        sup = _make_supervisor(tmp_path, manifests)
        sup.start()
        try:
            _wait_until(lambda: out.exists(), "fd payload written", timeout=10)
            payload = json.loads(out.read_text(encoding="utf-8"))
            sockets = [fd for fd, target in payload["fds"].items() if target.startswith("socket:")]
            assert sockets == []  # close_fds: no socket crosses the boundary
            leaked = [
                key
                for key, value in payload["env"].items()
                if ".sock" in value or "socket" in key.lower()
            ]
            assert leaked == []  # the future control socket never reaches a child
        finally:
            sup.shutdown()


# ── SL-15 start ordering: cycles refused, blocked until core is up ────


class TestSL15StartOrdering:
    def test_depends_on_cycle_is_refused(self, isolated_xdg: Path, tmp_path: Path) -> None:
        manifests = _write_manifests(
            tmp_path,
            {
                "alpha": _child_doc("alpha", "sleeper.py", depends_on=("beta",)),
                "beta": _child_doc("beta", "sleeper.py", depends_on=("alpha",)),
            },
        )
        with pytest.raises(ValueError, match="cycle"):
            _make_supervisor(tmp_path, manifests)

    def test_dependent_blocked_until_core_dependency_healthy(
        self, isolated_xdg: Path, tmp_path: Path
    ) -> None:
        flag = tmp_path / "flag"
        manifests = _write_manifests(
            tmp_path,
            {
                "coredep": _child_doc(
                    "coredep", "sleeper.py", tier="core", health=_flag_probe(flag)
                ),
                "dependent": _child_doc("dependent", "sleeper.py", depends_on=("coredep",)),
            },
        )
        sup = _make_supervisor(tmp_path, manifests)
        sup.start()
        try:
            _wait_until(
                lambda: sup.component_state("dependent") == "blocked", "T2 blocked", timeout=15
            )
            # The journal row: T2 with pid=none (never spawned). The row is
            # appended a few statements AFTER the state flips visible, so a
            # plain read can outrun the writer — poll for it.
            _wait_until(
                lambda: (
                    "vesma.supervisor component=dependent event=health pid=none "
                    "from=stopped to=blocked" in _records(tmp_path)
                ),
                "T2 journal row",
            )
            time.sleep(0.5)  # no timeout on core deps (SL-15) — it must STAY blocked
            assert sup.component_state("dependent") == "blocked"
            assert sup.component_state("dependent") is not None  # and no spawn happened
            assert sup.snapshot()["components"]["dependent"]["pid"] is None

            flag.write_text("on", encoding="utf-8")
            _wait_until(lambda: sup.component_state("coredep") == "healthy", "coredep up")
            _wait_until(
                lambda: sup.component_state("dependent") == "healthy",
                "T3 unblocked → dependent up",
                timeout=15,
            )
            assert any(
                "component=dependent" in ln and "from=blocked to=starting" in ln
                for ln in _records(tmp_path)
            )
        finally:
            sup.shutdown()


# ── SL-16 graceful stop: reverse-topological, zero leftovers ──────────


class TestSL16GracefulStop:
    def test_reverse_topological_stop_order_and_no_leftover_children(
        self, isolated_xdg: Path, tmp_path: Path
    ) -> None:
        manifests = _write_manifests(
            tmp_path,
            {
                "base": _child_doc("base", "sleeper.py", tier="core"),
                "mid": _child_doc("mid", "sleeper.py", depends_on=("base",)),
                "top": _child_doc("top", "sleeper.py", depends_on=("mid",)),
            },
        )
        sup = _make_supervisor(tmp_path, manifests)
        sup.start()
        try:
            for name in ("base", "mid", "top"):
                _wait_until(lambda n=name: sup.component_state(n) == "healthy", f"{name} healthy")
            pids = {name: _pid_of(sup, name) for name in ("base", "mid", "top")}
        finally:
            code = sup.shutdown()
        assert code == 0

        # Stop rows appear in the journal in EMISSION order — the reverse
        # topological order: dependents before dependencies.
        stop_rows = [ln for ln in _records(tmp_path) if "to=stopped" in ln and "event=health" in ln]
        stopped_names = [ln.split()[1].removeprefix("component=") for ln in stop_rows]
        assert stopped_names == ["top", "mid", "base"]

        # No child survives and none lingers as a zombie.
        for name, pid in pids.items():
            assert not _pid_exists(pid), f"{name}'s process {pid} survived the stop"
            assert not _zombie(pid), f"{name}'s process {pid} is an unreaped zombie"
        leftover: list[int] = []
        try:
            while True:
                pid, _status = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
                leftover.append(pid)
        except ChildProcessError:
            pass  # ECHILD — nothing of this installation was ever unreaped
        assert leftover == []


# ── the two signal-name conventions (§3.4 vs CM §3.8) are pinned ──────


class TestSignalConventions:
    def test_manifest_names_prefixed_exit_names_unprefixed(self) -> None:
        from vesmaro.service.supervisor import _signal_name, _signal_number

        assert _signal_number("SIGTERM") == int(signal.SIGTERM)
        assert _signal_number("SIGINT") == int(signal.SIGINT)
        with pytest.raises(ValueError, match="unsupported stop signal"):
            _signal_number("TERM")  # unprefixed is NOT a manifest name
        with pytest.raises(ValueError, match="unsupported stop signal"):
            _signal_number("NOTASIGNAL")
        assert _signal_name(int(signal.SIGKILL)) == "KILL"
        assert _signal_name(int(signal.SIGTERM)) == "TERM"

    def test_exit_name_round_trips_through_the_reaper_convention(self) -> None:
        from vesmaro.service.supervisor import _signal_name

        # The reaper re-prefixes the §3.4 name for Popen's -signum form.
        name = _signal_name(int(signal.SIGKILL))
        assert -int(signal.Signals[f"SIG{name}"].value) == -9
