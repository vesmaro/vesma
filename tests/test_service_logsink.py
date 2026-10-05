"""Structural lines, history journal and logsink conformance
(service-lifecycle v1 §3.4 + layout v1 §3.7).

The §3.4 grammar is normative (SL-08): prefix, field order, token charset,
exit code/signal mutual exclusion, severity table. The builders are pinned
to the same shapes the contract's examples/example-log-lines.txt fixes.
"""

from __future__ import annotations

import socket
import stat
from datetime import datetime
from pathlib import Path

import pytest

from vesmaro.service.logsink import (
    DEFAULT_JOURNAL_SOCKET,
    MAX_LOG_BYTES,
    ROTATED_FILES,
    HistoryJournal,
    JournaldSink,
    Logsink,
    RotatingFileSink,
    build_degraded_line,
    build_exit_line,
    build_health_line,
    build_refusal_line,
    build_spawn_line,
    journal_socket_available,
    make_logsink,
)

# ── Builders render the normative shapes (vs examples/example-log-lines) ─


class TestBuilderShapes:
    def test_spawn_line_minimal_and_attempt(self) -> None:
        assert build_spawn_line("server", 4213).render() == (
            "vesma.supervisor component=server event=spawn pid=4213"
        )
        assert build_spawn_line("server", 4213, attempt=3).render() == (
            "vesma.supervisor component=server event=spawn pid=4213 attempt=3"
        )

    def test_exit_by_code_zero_is_info(self) -> None:
        line = build_exit_line("board", 4221, code=0, signal_name=None)
        assert line.render() == (
            "vesma.supervisor component=board event=exit pid=4221 code=0 signal=none"
        )
        assert line.severity == "INFO"

    def test_exit_nonzero_is_warning(self) -> None:
        line = build_exit_line("board", 4221, code=1, signal_name=None)
        assert line.render().endswith("code=1 signal=none")
        assert line.severity == "WARNING"

    def test_exit_by_signal_no_sig_prefix_is_warning(self) -> None:
        line = build_exit_line("server", 4310, code=None, signal_name="KILL")
        assert line.render() == (
            "vesma.supervisor component=server event=exit pid=4310 code=none signal=KILL"
        )
        assert line.severity == "WARNING"

    def test_exit_mutual_exclusion_enforced(self) -> None:
        with pytest.raises(ValueError, match="EXACTLY ONE"):
            build_exit_line("x", 1, code=0, signal_name="KILL")
        with pytest.raises(ValueError, match="EXACTLY ONE"):
            build_exit_line("x", 1, code=None, signal_name=None)

    def test_exit_sig_prefix_refused(self) -> None:
        with pytest.raises(ValueError, match="SIG prefix"):
            build_exit_line("x", 1, code=None, signal_name="SIGKILL")

    def test_health_line_shape(self) -> None:
        line = build_health_line("board", 4252, "healthy", "degraded")
        assert line.render() == (
            "vesma.supervisor component=board event=health pid=4252 from=healthy to=degraded"
        )
        assert line.severity == "INFO"

    def test_degraded_line_optional_exact_fields(self) -> None:
        line = build_degraded_line(
            "board", 4252, reason="restart-budget-exhausted", attempts=5, window_s=300.0
        )
        assert line.render() == (
            "vesma.supervisor component=board event=degraded pid=4252 "
            "state=degraded reason=restart-budget-exhausted attempts=5 window=300s"
        )
        assert line.severity == "ERROR"

    def test_degraded_line_core_crash_loop_window_none(self) -> None:
        line = build_degraded_line("server", 4310, reason="crash-loop", attempts=10, window_s=None)
        assert line.render() == (
            "vesma.supervisor component=server event=degraded pid=4310 "
            "state=degraded reason=crash-loop attempts=10 window=none"
        )

    def test_refusal_line_reports_the_actual_state(self) -> None:
        """P2-G (cascade 2026-10-05): a start refusal never puts the FSM
        into degraded — the line carries the ACTUAL FSM state (§3.4
        extension, draft.3 candidate)."""
        line = build_refusal_line("x", None, state="stopped", reason="config-invalid", attempts=0)
        assert line.render() == (
            "vesma.supervisor component=x event=degraded pid=none "
            "state=stopped reason=config-invalid attempts=0 window=none"
        )
        assert line.severity == "ERROR"

    def test_token_charset_enforced_on_component(self) -> None:
        with pytest.raises(ValueError, match="token charset"):
            build_spawn_line("Bad Component", 1).render()

    def test_token_charset_enforced_on_state_values(self) -> None:
        with pytest.raises(ValueError, match="token charset"):
            build_health_line("x", 1, "starting", "HEALTHY")

    def test_pid_none_rendering(self) -> None:
        assert build_spawn_line("inproc", None).render().endswith("event=spawn pid=none")


# ── History journal (layout §3.7: append-only, 0700 dir, 0600 file) ───


class TestHistoryJournal:
    def test_appends_with_iso8601_prefix_and_modes(self, tmp_path: Path) -> None:
        journal = HistoryJournal(directory=tmp_path / "history")
        journal.append(build_spawn_line("server", 7).render())
        journal.append(build_exit_line("server", 7, code=1, signal_name=None).render())
        records = journal.read_records()
        assert len(records) == 2
        prefix, _, line = records[0].partition(" ")
        stamp = datetime.fromisoformat(prefix)
        assert stamp.tzinfo is not None  # ISO-8601 with an explicit zone
        assert line == "vesma.supervisor component=server event=spawn pid=7"
        assert stat.S_IMODE(journal.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(journal.path.parent.stat().st_mode) == 0o700

    def test_append_only_across_instances(self, tmp_path: Path) -> None:
        directory = tmp_path / "history"
        HistoryJournal(directory=directory).append("first")
        HistoryJournal(directory=directory).append("second")
        records = HistoryJournal(directory=directory).read_records()
        assert [record.split(" ", 1)[1] for record in records] == ["first", "second"]


# ── Mode decision + sinks (layout §3.7: EXACTLY ONE mode per run) ─────


class TestLogsinkModes:
    def test_journald_primary_when_socket_present(self, tmp_path: Path) -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        listener.bind(str(tmp_path / "journal.sock"))
        try:
            assert journal_socket_available(tmp_path / "journal.sock") is True
            sink = make_logsink(
                journal_socket_path=tmp_path / "journal.sock", state_logs_root=tmp_path / "logs"
            )
            assert isinstance(sink, JournaldSink)
        finally:
            listener.close()

    def test_file_mode_when_socket_absent(self, tmp_path: Path) -> None:
        sink = make_logsink(
            journal_socket_path=tmp_path / "absent.sock", state_logs_root=tmp_path / "logs"
        )
        assert isinstance(sink, Logsink)  # structural protocol (runtime-checkable)
        assert not isinstance(sink, JournaldSink)  # EXACTLY ONE mode: files, not journald
        assert not journal_socket_available(tmp_path / "absent.sock")

    def test_file_sink_writes_under_state_dir_0700(self, tmp_path: Path) -> None:
        root = tmp_path / "logs"
        sink = make_logsink(journal_socket_path=tmp_path / "absent.sock", state_logs_root=root)
        sink.emit("comp", "vesma.supervisor component=comp event=spawn pid=9")
        sink.close()
        content = (root / "comp" / "comp.log").read_text(encoding="utf-8")
        assert "event=spawn pid=9" in content
        assert stat.S_IMODE((root / "comp").stat().st_mode) == 0o700

    def test_journald_sink_payload_marks_identifier_and_priority(self, tmp_path: Path) -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        listener.bind(str(tmp_path / "j.sock"))
        listener.settimeout(2.0)
        sink = JournaldSink(tmp_path / "j.sock")
        try:
            sink.emit("vesma-board", "hello board", severity="WARNING")
            payload = listener.recv(4096).decode("utf-8")
            assert payload.startswith("SYSLOG_IDENTIFIER=vesma-board\n")
            assert "MESSAGE=hello board\n" in payload
            assert "PRIORITY=4" in payload  # WARNING -> syslog priority 4
        finally:
            sink.close()
            listener.close()

    def test_journald_sink_counts_drops_never_raises(self, tmp_path: Path) -> None:
        sink = JournaldSink(tmp_path / "nobody-listening.sock")
        try:
            sink.emit("vesma-x", "into the void")
            assert sink.dropped == 1  # counted, not raised: the mode never flips
        finally:
            sink.close()

    def test_default_journal_socket_path_is_canonical(self) -> None:
        assert Path("/run/systemd/journal/socket") == DEFAULT_JOURNAL_SOCKET


# ── Rotation (layout §3.7: 10 MB x 5) ─────────────────────────────────


class TestRotation:
    def test_rotates_with_injected_small_budget(self, tmp_path: Path) -> None:
        sink = RotatingFileSink(tmp_path, "comp", max_bytes=100, backups=3)
        try:
            for index in range(10):
                sink.emit("vesma-comp", f"line-{index:02d} padding-padding-padding")
        finally:
            sink.close()
        names = sorted(p.name for p in (tmp_path / "comp").iterdir())
        assert "comp.log" in names
        for backup in range(1, 4):
            assert f"comp.log.{backup}" in names
        assert "comp.log.4" not in names  # backups=3 respected

    def test_contract_rotation_numbers(self) -> None:
        assert MAX_LOG_BYTES == 10 * 1024 * 1024
        assert ROTATED_FILES == 5


# ── The sink protocol is structural ───────────────────────────────────


class _MySink:
    def __init__(self) -> None:
        self.seen: list[str] = []

    def emit(self, identifier: str, line: str, *, severity: str = "INFO") -> None:
        self.seen.append(f"{identifier}:{severity}:{line}")

    def close(self) -> None:
        self.seen.clear()


class TestProtocol:
    def test_any_emit_close_object_is_a_logsink(self) -> None:
        sink: Logsink = _MySink()
        sink.emit("vesma-x", "line", severity="ERROR")
        assert sink.seen == ["vesma-x:ERROR:line"]
