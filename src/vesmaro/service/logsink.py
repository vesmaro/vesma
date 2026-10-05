"""Observability channel (service-lifecycle §3.4 + layout §3.7).

Three pieces, kept deliberately boring:

- **Structural lines** — the normative §3.4 grammar, rendered and VALIDATED
  here (token charset ``[a-z0-9_.-]``, mandatory field order, exit
  code/signal mutual exclusion). Builders refuse to produce a line that the
  grammar would reject, so SL-08 holds by construction, not by hope.
- **History journal** — ``~/.local/state/vesma/history/journal.log``,
  append-only, single writer (the supervisor), every record a structural
  line with an ISO-8601 UTC timestamp prefix; directory 0700, file 0600
  (layout §3.7).
- **Logsink** — EXACTLY ONE mode per run (layout §3.7): journald primary
  (datagram to ``/run/systemd/journal/socket``, ``SYSLOG_IDENTIFIER`` set BY
  the supervisor — a child never writes the journal itself, threat model §7);
  when the journal socket is unavailable → files-under-state
  ``~/.local/state/vesma/logs/<identifier>/`` with 10 MB x 5 rotation.
  Never both.
"""

from __future__ import annotations

import dataclasses
import os
import re
import socket
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from vesmaro.service.layout import ensure_dir

__all__ = [
    "DEFAULT_JOURNAL_SOCKET",
    "MAX_LOG_BYTES",
    "ROTATED_FILES",
    "HistoryJournal",
    "JournaldSink",
    "Logsink",
    "RotatingFileSink",
    "SupervisorLine",
    "build_degraded_line",
    "build_exit_line",
    "build_health_line",
    "build_spawn_line",
    "journal_socket_available",
    "make_logsink",
]

#: Default systemd journal socket (layout §3.7 journald-primary detection).
DEFAULT_JOURNAL_SOCKET = Path("/run/systemd/journal/socket")

#: File-mode rotation (layout §3.7): 10 MB per file, 5 rotated files.
MAX_LOG_BYTES = 10 * 1024 * 1024
ROTATED_FILES = 5

# ── Structural lines (SL §3.4 grammar — normative) ────────────────────

_TOKEN_RE = re.compile(r"^[a-z0-9_.-]+$")
# The signal field is the one §3.4 value that is NOT a lowercase token: the
# normative example line is ``signal=KILL`` (exit §3.4 table — "signal
# <NAME|none>", name WITHOUT the SIG prefix). Uppercase names only.
_SIGNAL_RE = re.compile(r"^[A-Z][A-Z0-9]{0,15}$")
_PREFIX = "vesma.supervisor"

# syslog priorities used by the journald sink (SL §3.4 severities).
_PRIORITY = {"INFO": 6, "WARNING": 4, "ERROR": 3}


def _token(value: str, what: str) -> str:
    if not _TOKEN_RE.match(value):
        raise ValueError(
            f"structural-line {what} {value!r} is outside the §3.4 token charset [a-z0-9_.-]"
        )
    return value


def _field_value(key: str, value: str) -> str:
    """Validate one ``key=value`` payload per §3.4.

    Lowercase token charset everywhere EXCEPT the ``signal`` field of the
    exit event, whose normative value is an uppercase signal name
    (``signal=KILL`` — §3.4 table + example lines).
    """
    if key == "signal" and value != "none":
        if not _SIGNAL_RE.match(value):
            raise ValueError(
                f"structural-line signal value {value!r} must be an uppercase "
                "signal name without the SIG prefix (e.g. KILL, TERM) or none"
            )
        return value
    return _token(value, f"field value of {key}")


@dataclasses.dataclass(frozen=True)
class SupervisorLine:
    """One structural line, already validated against the §3.4 grammar."""

    component: str
    event: str
    pid: int | None
    fields: tuple[tuple[str, str], ...]
    severity: str  # INFO | WARNING | ERROR

    def render(self) -> str:
        _token(self.component, "component")
        _token(self.event, "event")
        parts = [
            _PREFIX,
            f"component={_token(self.component, 'component')}",
            f"event={_token(self.event, 'event')}",
            f"pid={self.pid if self.pid is not None else 'none'}",
        ]
        for key, value in self.fields:
            _token(key, "field key")
            _field_value(key, value)
            parts.append(f"{key}={value}")
        return " ".join(parts)


def build_spawn_line(
    component: str,
    pid: int | None,
    attempt: int | None = None,
) -> SupervisorLine:
    """``event=spawn`` — ребёнок заспавнен (INFO). MAY carry ``attempt=<n>``.

    ``pid`` is ``None`` for in-process components (§3.4 grammar: ``pid=none``).
    """
    fields: list[tuple[str, str]] = []
    if attempt is not None:
        fields.append(("attempt", str(attempt)))
    return SupervisorLine(component, "spawn", pid, tuple(fields), "INFO")


def build_exit_line(
    component: str,
    pid: int | None,
    *,
    code: int | None,
    signal_name: str | None,
) -> SupervisorLine:
    """``event=exit`` — ровно одно из code/signal несёт значение (SL §3.4).

    ``code=<n> signal=none`` или ``code=none signal=<NAME>`` — сигнал БЕЗ
    префикса ``SIG``. Серьёзность: код 0 — INFO, ненулевой код или сигнал —
    WARNING.
    """
    if (code is None) == (signal_name is None):
        raise ValueError(
            "exit line needs EXACTLY ONE of code/signal "
            f"(got code={code!r}, signal={signal_name!r})"
        )
    if code is not None:
        fields = (("code", str(code)), ("signal", "none"))
        severity = "INFO" if code == 0 else "WARNING"
    else:
        name = str(signal_name)
        if name.startswith("SIG"):
            raise ValueError(f"signal name must not carry the SIG prefix: {name!r}")
        fields = (("code", "none"), ("signal", _field_value("signal", name)))
        severity = "WARNING"
    return SupervisorLine(component, "exit", pid, fields, severity)


def build_health_line(
    component: str,
    pid: int | None,
    from_state: str,
    to_state: str,
) -> SupervisorLine:
    """``event=health`` — переход FSM по T4/T7/T9 (INFO)."""
    return SupervisorLine(
        component,
        "health",
        pid,
        (("from", _token(from_state, "from state")), ("to", _token(to_state, "to state"))),
        "INFO",
    )


def build_degraded_line(
    component: str,
    pid: int | None,
    *,
    reason: str,
    attempts: int,
    window_s: float | None,
) -> SupervisorLine:
    """``event=degraded`` — supervisor-level деградация (ERROR).

    ``window=<Ns|none>``: целые секунды (``300s``) или ``none`` (core
    crash-loop). EXACTLY эта форма зафиксирована в SL-09/SL-11.
    """
    window = "none" if window_s is None else f"{_format_seconds(window_s)}s"
    return SupervisorLine(
        component,
        "degraded",
        pid,
        (
            ("state", "degraded"),
            ("reason", _token(reason, "reason")),
            ("attempts", str(attempts)),
            ("window", window),
        ),
        "ERROR",
    )


def _format_seconds(value: float) -> str:
    rendered = int(value)
    return str(rendered) if rendered == value else f"{value:g}"


# ── Sinks (layout §3.7 — EXACTLY ONE mode per run) ────────────────────


class Logsink(Protocol):
    """One mode: journald OR files-under-state, chosen once per run."""

    def emit(self, identifier: str, line: str, *, severity: str = "INFO") -> None: ...

    def close(self) -> None: ...


def journal_socket_available(path: Path = DEFAULT_JOURNAL_SOCKET) -> bool:
    """True iff ``path`` exists and IS a socket (journald primary mode)."""
    try:
        return stat.S_ISSOCK(path.stat().st_mode)
    except OSError:
        return False


def _journal_escape(value: str) -> str:
    """Journal socket protocol: literal newline inside a value is ``\\n``."""
    return value.replace("\n", "\\n")


class JournaldSink:
    """Datagram writer to the systemd journal socket.

    Marking is done BY the supervisor: ``SYSLOG_IDENTIFIER=vesma-<component>``
    travels with every forwarded line (SL §3.4, threat model — a child never
    attributes journal lines itself). A failed send is counted and dropped —
    the mode never silently flips to file writing (EXACTLY ONE mode).
    """

    def __init__(self, socket_path: Path = DEFAULT_JOURNAL_SOCKET) -> None:
        self._path = socket_path
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.dropped = 0

    def emit(self, identifier: str, line: str, *, severity: str = "INFO") -> None:
        payload = (
            f"SYSLOG_IDENTIFIER={_journal_escape(identifier)}\n"
            f"MESSAGE={_journal_escape(line)}\n"
            f"PRIORITY={_PRIORITY.get(severity, 6)}\n"
        ).encode("utf-8", errors="replace")
        try:
            self._sock.sendto(payload, str(self._path))
        except OSError:
            self.dropped += 1

    def close(self) -> None:
        self._sock.close()


class RotatingFileSink:
    """files-under-state: ``~/.local/state/vesma/logs/<identifier>/``.

    Rotation 10 MB x 5 (layout §3.7): ``<identifier>.log`` plus
    ``.log.1`` … ``.log.5``. One identifier per directory, per-run mode
    decision — never writes when the journald mode is active.
    """

    def __init__(
        self,
        state_logs_root: Path,
        identifier: str,
        *,
        max_bytes: int = MAX_LOG_BYTES,
        backups: int = ROTATED_FILES,
    ) -> None:
        self._dir = ensure_dir(state_logs_root / identifier, 0o700)
        self._path = self._dir / f"{identifier}.log"
        self._max_bytes = max_bytes
        self._backups = backups
        self._lock = threading.Lock()

    def emit(self, identifier: str, line: str, *, severity: str = "INFO") -> None:
        del identifier, severity  # file mode keeps only the raw line
        with self._lock:
            self._rotate_if_needed()
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(f"{_timestamp()} {line}\n")

    def _rotate_if_needed(self) -> None:
        try:
            size = self._path.stat().st_size
        except OSError:
            return
        if size < self._max_bytes:
            return
        for index in range(self._backups, 0, -1):
            source = (
                self._path if index == 1 else self._path.with_name(f"{self._path.name}.{index - 1}")
            )
            target = self._path.with_name(f"{self._path.name}.{index}")
            if source.exists():
                os.replace(source, target)

    def close(self) -> None:
        return None


def make_logsink(
    *,
    journal_socket_path: Path = DEFAULT_JOURNAL_SOCKET,
    state_logs_root: Path | None = None,
) -> Logsink:
    """The §3.7 mode decision, made ONCE: journald if reachable else files.

    ``state_logs_root`` injects the file-mode root (tests point it into a
    tmp dir); default is the canonical ``~/.local/state/vesma/logs/``.
    """
    if journal_socket_available(journal_socket_path):
        return JournaldSink(journal_socket_path)
    if state_logs_root is None:
        from vesmaro.service.layout import state_root

        state_logs_root = state_root() / "logs"
    return _FileModeRouter(state_logs_root)


class _FileModeRouter:
    """File-mode sink: one RotatingFileSink per identifier, created lazily."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._sinks: dict[str, RotatingFileSink] = {}
        self._lock = threading.Lock()

    def _sink_for(self, identifier: str) -> RotatingFileSink:
        with self._lock:
            sink = self._sinks.get(identifier)
            if sink is None:
                sink = RotatingFileSink(self._root, identifier)
                self._sinks[identifier] = sink
            return sink

    def emit(self, identifier: str, line: str, *, severity: str = "INFO") -> None:
        self._sink_for(identifier).emit(identifier, line, severity=severity)

    def close(self) -> None:
        return None


# ── History journal (layout §3.7 — append-only, single writer) ────────


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


class HistoryJournal:
    """Append-only supervisor journal of FSM transitions + global health.

    Layout §3.7: ``~/.local/state/vesma/history/`` (0700), records are the
    §3.4 structural lines with an ISO-8601 timestamp prefix. The supervisor
    is the ONLY writer (append-only semantics of v1 — same-uid writes are
    the known trust-domain boundary, SL §3.2). Injectable directory for
    tests; the default resolves at first write (XDG-aware).
    """

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory
        self._lock = threading.Lock()
        self._path: Path | None = None

    @property
    def path(self) -> Path:
        if self._path is None:
            directory = self._directory
            if directory is None:
                from vesmaro.service.layout import history_dir

                directory = history_dir()
            ensure_dir(directory, 0o700)
            self._path = directory / "journal.log"
        return self._path

    def append(self, line: str) -> None:
        """Append one record; timestamp prefix added here (§3.4/§3.7)."""
        path = self.path
        with self._lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(f"{_timestamp()} {line}\n")
            os.chmod(path, 0o600)

    def read_records(self) -> list[str]:
        """Test/inspection helper: the raw journal lines (with prefixes)."""
        try:
            return self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []


def emit_record(
    journal: HistoryJournal,
    sink: Logsink,
    line: SupervisorLine,
    *,
    supervisor_identifier: str = "vesma-supervisor",
) -> None:
    """One structural line → history journal (always) + logsink (own id)."""
    rendered = line.render()
    journal.append(rendered)
    sink.emit(supervisor_identifier, rendered, severity=line.severity)


def emit_child_forward(
    sink: Logsink,
    component: str,
    raw_line: str,
) -> None:
    """Forward one raw child stdout/stderr line under its own identifier."""
    sink.emit(f"vesma-{component}", raw_line.rstrip("\n"), severity="INFO")
