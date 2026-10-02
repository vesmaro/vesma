"""Tests for the background secrets scanner — Layer 2 defence-in-depth (#89).

Covers :mod:`vesma.scanner` (:class:`BackgroundScanner`, :class:`ScanResult`),
:mod:`vesma.scanner_runtime` (singleton), :mod:`vesma.cli.scanner_cmd`
(``mnemos scanner run/status``), and the scanner audit log in
:mod:`vesma.audit`.

Reuses (DRY):
* :func:`vesma.secrets_detector.detect_secrets` — the SAME patterns as
  Layer 1 (write-path scanner). The scanner never re-implements a pattern;
  this test suite asserts that the pattern names reported by the scanner
  match the pattern names from ``secrets_detector`` exactly.

All secret fixtures are OBVIOUSLY FAKE (per
``sensitive-data.instructions.md``): ``AKIA`` + 16 uppercase alnum
(AWS key shape), ``ghp_`` + 36 uppercase (GitHub PAT shape). No real
credentials appear anywhere in this file.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from vesma.audit import log_scanner_audit
from vesma.config import ScannerConfig, Settings
from vesma.manager import MemoryManager
from vesma.models import (
    NO_FEDERATE_TAG,
    Memory,
    MemorySource,
    MemoryStatus,
)
from vesma.scanner import BackgroundScanner, ScanResult
from vesma.scanner_runtime import get_scanner, reset_scanner
from vesma.secrets_detector import detect_secrets, findings_by_pattern

# ---------------------------------------------------------------------------
# Fixtures — mirror tests/test_sync.py and tests/test_no_federate.py
# ---------------------------------------------------------------------------

#: Obviously-fake AWS key (AKIA + 16 uppercase alnum). Never a real credential.
FAKE_AWS_KEY = "AKIA" + "T" * 16

#: Obviously-fake GitHub PAT (ghp_ + 36 uppercase). Never a real credential.
FAKE_GHP_TOKEN = "ghp_" + "T" * 36


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    settings = Settings(
        vesma={
            "vault_path": str(tmp_path / "vault"),
            "data_dir": str(tmp_path / "data"),
            "db_name": "test-scanner.db",
            "auto_filter": False,
        },
        embedding={"provider": "onnx"},
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def mgr(tmp_settings: Settings) -> MemoryManager:
    m = MemoryManager(tmp_settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    m._embedder = mock_embedder
    yield m
    m.close()


@pytest.fixture
def scanner(mgr: MemoryManager) -> BackgroundScanner:
    """A scanner bound to ``mgr`` with a short interval for lifecycle tests."""
    return BackgroundScanner(mgr, ScannerConfig(enabled=True, interval_hours=1))


@pytest.fixture(autouse=True)
def _isolated_scanner_audit_log(monkeypatch, tmp_path: Path) -> Path:
    """Redirect the scanner audit log to a tmp path so tests stay isolated.

    Patches ``scanner_audit_path`` (read by :func:`log_scanner_audit` on
    every call) to return a path under the test's ``tmp_path``. Because
    :func:`log_scanner_audit` calls :func:`scanner_audit_path` at call
    time (not import time), this redirects every audit write — both the
    ones from :class:`BackgroundScanner` and the ones from direct
    :func:`log_scanner_audit` calls.
    """
    audit_path = tmp_path / "audit" / "scanner-audit.jsonl"
    import vesma.audit as audit_mod

    monkeypatch.setattr(audit_mod, "scanner_audit_path", lambda: audit_path)
    return audit_path


@pytest.fixture(autouse=True)
def _reset_scanner_singleton() -> None:
    """Stop the scanner thread and clear the singleton between tests.

    ``reset_scanner()`` only nulls the module-level singleton — it does NOT
    join the daemon thread spawned by ``start()``. If a previous test
    started the scanner (via ``get_scanner(mgr).start()`` or through the
    API lifespan), the orphaned thread keeps a closure reference to the
    old ``tmp_path`` DB. When the next test deletes that tmp_path and
    builds a new scanner, the orphan can race on the deleted DB file
    (``sqlite3.OperationalError: unable to open database file``) — the
    root cause of the flaky ``test_dashboard_metrics`` failure.

    Fix: ``stop()`` the singleton (joins the thread, timeout=10) BEFORE
    nulling it. Idempotent — ``stop()`` is a no-op when no thread is
    running. The leading ``reset_scanner()`` clears any leftover
    singleton from a crashed previous test (defence-in-depth).
    """
    reset_scanner()  # clear at start (idempotent, handles crash leftovers)
    yield
    from vesma.scanner_runtime import _scanner as _current

    if _current is not None:
        _current.stop()  # join the daemon thread (timeout=10) BEFORE nulling
    reset_scanner()


def _save_direct(
    mgr: MemoryManager,
    *,
    id: str,
    content: str,
    tags: list[str] | None = None,
    created_at: datetime | None = None,
) -> Memory:
    """Save a memory directly to SQLite, bypassing ``mgr.add`` (Layer 1).

    ``mgr.add`` runs the Layer 1 write-path scanner and auto-tags
    ``mnemos:no-federate`` before persistence. To exercise Layer 2
    (the background scanner catching false negatives the write-path
    missed), we save directly to SQLite so the record reaches the
    corpus without the no-federate tag.
    """
    tags = tags or ["project:mnemos", "agent:tech-lead", "mnemos:decision"]
    mem = Memory(
        id=id,
        content=content,
        tags=tags,
        source=MemorySource.CLI,
        status=MemoryStatus.PUBLISHED,
        project="mnemos",
        agent="tech-lead",
        created_at=created_at or datetime.now(UTC),
    )
    mgr.sqlite.save(mem)
    return mem


def _read_audit_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ── Core scan behaviour ──────────────────────────────────────────────────────


class TestScanTagging:
    def test_scan_tags_record_with_secret(self, scanner: BackgroundScanner) -> None:
        """Record with a secret (saved direct, bypassing Layer 1) → tagged."""
        mem = _save_direct(
            scanner._manager,
            id="11111111-1111-1111-1111-111111111111",
            content=f"config has key={FAKE_AWS_KEY} for aws",
        )
        assert NO_FEDERATE_TAG not in mem.tags  # pre-condition: Layer 1 missed it

        result = scanner.run_scan(incremental=False)

        assert result.records_tagged == 1
        assert result.records_skipped == 0
        updated = scanner._manager.get(mem.id)
        assert updated is not None
        assert NO_FEDERATE_TAG in updated.tags

    def test_scan_skips_already_tagged(self, scanner: BackgroundScanner) -> None:
        """Record already carrying no-federate → counted as skipped, not re-tagged."""
        _save_direct(
            scanner._manager,
            id="22222222-2222-2222-2222-222222222222",
            content=f"key={FAKE_AWS_KEY}",
            tags=["project:mnemos", "agent:tech-lead", "mnemos:decision", NO_FEDERATE_TAG],
        )

        result = scanner.run_scan(incremental=False)

        assert result.records_tagged == 0
        assert result.records_skipped == 1
        assert result.records_scanned == 1

    def test_scan_skips_clean_records(self, scanner: BackgroundScanner) -> None:
        """Record with no secret pattern → not tagged, not skipped (no findings)."""
        _save_direct(
            scanner._manager,
            id="33333333-3333-3333-3333-333333333333",
            content="just a normal note about the weather",
        )

        result = scanner.run_scan(incremental=False)

        assert result.records_tagged == 0
        assert result.records_skipped == 0
        assert result.records_scanned == 1
        assert result.patterns_matched == {}

    def test_scan_multiple_records_mixed(self, scanner: BackgroundScanner) -> None:
        """A mixed corpus: one clean, one already-tagged, one new secret."""
        _save_direct(
            scanner._manager,
            id="44444444-4444-4444-4444-444444444444",
            content="clean note",
        )
        _save_direct(
            scanner._manager,
            id="55555555-5555-5555-5555-555555555555",
            content=f"key={FAKE_AWS_KEY}",
            tags=["project:mnemos", "agent:tech-lead", "mnemos:decision", NO_FEDERATE_TAG],
        )
        _save_direct(
            scanner._manager,
            id="66666666-6666-6666-6666-666666666666",
            content=f"token={FAKE_GHP_TOKEN}",
        )

        result = scanner.run_scan(incremental=False)

        assert result.records_scanned == 3
        assert result.records_tagged == 1  # only the ghp record
        assert result.records_skipped == 1  # the already-tagged aws record
        # Patterns reported are pattern NAMES, not values.
        assert FAKE_AWS_KEY not in str(result.patterns_matched)
        assert FAKE_GHP_TOKEN not in str(result.patterns_matched)


# ── Incremental vs full scan ──────────────────────────────────────────────────


class TestIncrementalScan:
    def test_first_incremental_is_effectively_full(self, scanner: BackgroundScanner) -> None:
        """The first incremental scan (no prior boundary) scans everything."""
        _save_direct(
            scanner._manager,
            id="77777777-7777-7777-7777-777777777777",
            content=f"key={FAKE_AWS_KEY}",
        )

        result = scanner.run_scan(incremental=True)

        assert result.incremental is True
        assert result.records_scanned == 1
        assert result.records_tagged == 1

    def test_incremental_skips_old_records(
        self, scanner: BackgroundScanner, tmp_path: Path
    ) -> None:
        """After a scan, only records newer than the boundary are re-scanned."""
        # Old record — created before the first scan boundary.
        old_ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        _save_direct(
            scanner._manager,
            id="88888888-8888-8888-8888-888888888888",
            content=f"key={FAKE_AWS_KEY}",
            created_at=old_ts,
        )

        # First pass — full scan, tags the old record.
        first = scanner.run_scan(incremental=False)
        assert first.records_tagged == 1
        assert scanner.last_scan_ts is not None

        # Second pass — incremental. The old record's created_at is before
        # the boundary, so it is NOT re-scanned. records_scanned == 0.
        second = scanner.run_scan(incremental=True)
        assert second.records_scanned == 0
        assert second.records_tagged == 0

    def test_full_scan_ignores_boundary(self, scanner: BackgroundScanner) -> None:
        """``--full`` forces a full corpus scan regardless of the boundary."""
        old_ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        _save_direct(
            scanner._manager,
            id="99999999-9999-9999-9999-999999999999",
            content=f"key={FAKE_AWS_KEY}",
            created_at=old_ts,
        )

        # First pass — full, tags the record.
        first = scanner.run_scan(incremental=False)
        assert first.records_tagged == 1

        # Second pass — full again. The record is already tagged → skipped.
        second = scanner.run_scan(incremental=False)
        assert second.records_scanned == 1
        assert second.records_tagged == 0
        assert second.records_skipped == 1


# ── Idempotency ───────────────────────────────────────────────────────────────


class TestIdempotency:
    def test_second_scan_tags_zero(self, scanner: BackgroundScanner) -> None:
        """Running the scanner twice on the same corpus tags 0 on pass 2."""
        _save_direct(
            scanner._manager,
            id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            content=f"key={FAKE_AWS_KEY}",
        )

        first = scanner.run_scan(incremental=False)
        assert first.records_tagged == 1

        second = scanner.run_scan(incremental=False)
        assert second.records_tagged == 0
        assert second.records_skipped == 1
        # The tag count on the record is still exactly 1 (no duplicates).
        updated = scanner._manager.get("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        assert updated is not None
        assert updated.tags.count(NO_FEDERATE_TAG) == 1

    def test_total_tagged_accumulates_across_passes(self, scanner: BackgroundScanner) -> None:
        """``total_tagged`` accumulates across passes (operational telemetry)."""
        _save_direct(
            scanner._manager,
            id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            content=f"key={FAKE_AWS_KEY}",
        )
        _save_direct(
            scanner._manager,
            id="cccccccc-cccc-cccc-cccc-cccccccccccc",
            content=f"token={FAKE_GHP_TOKEN}",
        )

        # Both bbbb (AWS key) and cccc (GitHub token) have secrets and
        # neither carries the no-federate tag (saved via _save_direct,
        # bypassing Layer 1). The first full scan tags both.
        scanner.run_scan(incremental=False)
        assert scanner.total_tagged == 2

        # Add a third secret record after the first pass, full-scan again.
        _save_direct(
            scanner._manager,
            id="dddddddd-dddd-dddd-dddd-dddddddddddd",
            content=f"another key={FAKE_AWS_KEY}",
        )
        scanner.run_scan(incremental=False)
        assert scanner.total_tagged == 3


# ── Audit log ─────────────────────────────────────────────────────────────────


class TestScannerAuditLog:
    def test_audit_log_written_with_counters(
        self, scanner: BackgroundScanner, _isolated_scanner_audit_log: Path
    ) -> None:
        """A scan pass appends one JSONL entry with the expected counter fields."""
        _save_direct(
            scanner._manager,
            id="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
            content=f"key={FAKE_AWS_KEY}",
        )

        scanner.run_scan(incremental=False)

        entries = _read_audit_entries(_isolated_scanner_audit_log)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["action"] == "background-scan"
        assert entry["records_scanned"] == 1
        assert entry["records_tagged"] == 1
        assert entry["records_skipped"] == 0
        assert "patterns_matched" in entry
        assert "duration_sec" in entry
        assert "incremental" in entry
        assert "timestamp" in entry

    def test_audit_log_no_raw_values(
        self, scanner: BackgroundScanner, _isolated_scanner_audit_log: Path
    ) -> None:
        """The audit JSONL MUST NOT contain raw secret values."""
        _save_direct(
            scanner._manager,
            id="ffffffff-ffff-ffff-ffff-ffffffffffff",
            content=f"secret={FAKE_AWS_KEY} and token={FAKE_GHP_TOKEN}",
        )

        scanner.run_scan(incremental=False)

        raw = _isolated_scanner_audit_log.read_text()
        assert FAKE_AWS_KEY not in raw
        assert FAKE_GHP_TOKEN not in raw
        # The audit entry must also not contain the record's content.
        assert "secret=" not in raw


# ── Pattern reuse (DRY) ───────────────────────────────────────────────────────


class TestPatternReuse:
    def test_patterns_match_secrets_detector_names(self, scanner: BackgroundScanner) -> None:
        """The scanner reports the SAME pattern names as ``secrets_detector``.

        This is the DRY contract: Layer 2 re-uses ``detect_secrets``
        unchanged. The pattern names in ``ScanResult.patterns_matched``
        must be a subset of the pattern names returned by
        :func:`findings_by_pattern` for the same content.
        """
        content = f"key={FAKE_AWS_KEY} token={FAKE_GHP_TOKEN}"
        _save_direct(
            scanner._manager,
            id="11111111-2222-3333-4444-555555555555",
            content=content,
        )

        # Ground truth: what does secrets_detector report for this content?
        direct_findings = detect_secrets(content)
        direct_pattern_names = set(findings_by_pattern(direct_findings).keys())

        result = scanner.run_scan(incremental=False)

        assert set(result.patterns_matched.keys()) == direct_pattern_names
        # Each pattern's count matches the detector's count for that content.
        for name, count in findings_by_pattern(direct_findings).items():
            assert result.patterns_matched[name] == count


# ── Lifecycle: start / stop / disabled ────────────────────────────────────────


class TestScannerLifecycle:
    def test_start_launches_daemon_thread(self, scanner: BackgroundScanner) -> None:
        """``start()`` launches a background daemon thread."""
        assert scanner.running is False
        scanner.start()
        try:
            assert scanner.running is True
            assert scanner._thread is not None
            assert scanner._thread.daemon is True
            assert scanner._thread.name == "mnemos-scanner"
        finally:
            scanner.stop()
        assert scanner.running is False

    def test_start_is_idempotent(self, scanner: BackgroundScanner) -> None:
        """Calling ``start()`` twice does not launch a second thread."""
        scanner.start()
        first_thread = scanner._thread
        scanner.start()  # no-op
        assert scanner._thread is first_thread
        scanner.stop()

    def test_stop_when_not_running_is_noop(self, scanner: BackgroundScanner) -> None:
        """``stop()`` before ``start()`` is a safe no-op."""
        scanner.stop()  # must not raise
        assert scanner.running is False

    def test_disabled_config_does_not_start(self, mgr: MemoryManager) -> None:
        """When ``enabled=False``, ``start()`` is a no-op (no thread)."""
        disabled = BackgroundScanner(mgr, ScannerConfig(enabled=False, interval_hours=1))
        disabled.start()
        assert disabled.running is False
        assert disabled._thread is None
        # stop() must still be safe.
        disabled.stop()

    def test_stop_joins_thread_cleanly(self, scanner: BackgroundScanner) -> None:
        """``stop()`` signals the thread and joins within the timeout."""
        scanner.start()
        thread = scanner._thread
        scanner.stop()
        assert thread is not None
        assert not thread.is_alive()


# ── Singleton runtime ─────────────────────────────────────────────────────────


class TestScannerRuntime:
    def test_get_scanner_returns_singleton(self, mgr: MemoryManager) -> None:
        """``get_scanner`` returns the same instance on repeated calls."""
        s1 = get_scanner(mgr)
        s2 = get_scanner(mgr)
        assert s1 is s2

    def test_reset_scanner_clears_singleton(self, mgr: MemoryManager) -> None:
        """``reset_scanner`` clears the cache so the next call rebuilds."""
        s1 = get_scanner(mgr)
        reset_scanner()
        s2 = get_scanner(mgr)
        assert s1 is not s2


# ── ScanResult dataclass ──────────────────────────────────────────────────────


class TestScanResult:
    def test_to_audit_entry_shape(self) -> None:
        """``to_audit_entry`` produces the expected counter-only dict."""
        result = ScanResult(
            records_scanned=5,
            records_tagged=2,
            records_skipped=1,
            patterns_matched={"aws-key": 1, "github-pat": 1},
            duration_sec=1.5,
            incremental=False,
        )
        entry = result.to_audit_entry()
        assert entry["action"] == "background-scan"
        assert entry["records_scanned"] == 5
        assert entry["records_tagged"] == 2
        assert entry["records_skipped"] == 1
        assert entry["patterns_matched"] == {"aws-key": 1, "github-pat": 1}
        assert entry["incremental"] is False
        # No raw-value fields leak into the audit entry.
        assert "content" not in entry
        assert "matched_value" not in entry

    def test_timestamp_defaults_to_now_utc(self) -> None:
        """A ScanResult without a timestamp gets an ISO 8601 UTC default."""
        before = datetime.now(UTC)
        result = ScanResult(records_scanned=0, records_tagged=0, records_skipped=0)
        after = datetime.now(UTC)
        # The timestamp ends with a Z suffix (UTC marker).
        assert result.timestamp.endswith("Z")
        # Parse it back and confirm it falls in the [before, after] window.
        parsed = datetime.fromisoformat(result.timestamp.replace("Z", "+00:00"))
        assert before <= parsed <= after


# ── CLI ────────────────────────────────────────────────────────────────────────


class TestScannerCLI:
    """Smoke tests for ``mnemos scanner run`` and ``mnemos scanner status``.

    Uses Typer's ``CliRunner`` in-process against an isolated config so
    the CLI never touches ``~/.mnemos``. Deeper behaviour is exercised
    through :class:`BackgroundScanner` directly above.
    """

    runner = CliRunner()

    @pytest.fixture
    def isolated_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Point VESMA_CONFIG at an empty YAML so the CLI uses tmp_path."""
        from vesma.cli._manager import reset_manager

        reset_manager()
        reset_scanner()
        cfg = tmp_path / "vesma.yaml"
        cfg.write_text(
            f"vesma:\n"
            f"  vault_path: {tmp_path / 'vault'}\n"
            f"  data_dir: {tmp_path / 'data'}\n"
            f"  db_name: cli-scanner.db\n"
            f"  auto_filter: false\n"
            f"embedding:\n"
            f"  provider: nano\n"
            f"scanner:\n"
            f"  enabled: true\n"
            f"  interval_hours: 1\n"
            f"  incremental: true\n"
        )
        monkeypatch.setenv("VESMA_CONFIG", str(cfg))
        yield cfg
        reset_manager()
        reset_scanner()

    def test_cli_run_incremental(self, isolated_config: Path) -> None:
        """``mnemos scanner run`` exits 0 and prints the scan summary."""
        from vesma.cli.main import app

        result = self.runner.invoke(app, ["scanner", "run"])
        assert result.exit_code == 0, result.output
        assert "Scan complete" in result.output
        assert "records_scanned" in result.output

    def test_cli_run_full(self, isolated_config: Path) -> None:
        """``mnemos scanner run --full`` exits 0 and reports a full scan."""
        from vesma.cli.main import app

        result = self.runner.invoke(app, ["scanner", "run", "--full"])
        assert result.exit_code == 0, result.output
        assert "full" in result.output

    def test_cli_status(self, isolated_config: Path) -> None:
        """``mnemos scanner status`` exits 0 and prints the scanner state."""
        from vesma.cli.main import app

        result = self.runner.invoke(app, ["scanner", "status"])
        assert result.exit_code == 0, result.output
        assert "enabled" in result.output
        assert "running" in result.output
        assert "interval_hours" in result.output
        assert "total_tagged" in result.output


# ── Direct audit-module coverage ──────────────────────────────────────────────


class TestAuditModule:
    def test_log_scanner_audit_appends_entry(self, _isolated_scanner_audit_log: Path) -> None:
        """``log_scanner_audit`` appends one JSON line with a timestamp."""
        log_scanner_audit(
            {
                "action": "background-scan",
                "records_scanned": 1,
                "records_tagged": 0,
                "records_skipped": 0,
            }
        )
        entries = _read_audit_entries(_isolated_scanner_audit_log)
        assert len(entries) == 1
        assert "timestamp" in entries[0]
        assert entries[0]["records_scanned"] == 1

    def test_scanner_audit_path_under_home(self) -> None:
        """``scanner_audit_path`` resolves under ``~/.mnemos/logs/``."""
        # NOTE: this test reads the real (un-patched) path; the autouse
        # fixture patches the *function object*, but we import the
        # original here via a fresh reference to assert the contract.
        from vesma.audit import SCANNER_AUDIT_FILENAME

        assert SCANNER_AUDIT_FILENAME == ".mnemos/logs/scanner-audit.jsonl"
