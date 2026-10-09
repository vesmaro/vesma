"""Background secrets scanner — Layer 2 of the federation defence-in-depth.

ArchCom 2026-07-17 federation contract §2.2.1 — the background scanner
periodically re-scans the whole Vesma corpus for secrets missed by the
write-path scanner (Layer 1) and auto-tags ``mnemos:no-federate`` so the
record is excluded from all external exchange (batch sync + mediated
pull). It catches false negatives:

* the LLM did not tag the record at write time,
* a pattern was added to :mod:`vesma.secrets_detector` after the record
  was written,
* the record was updated via ``manager.update`` without re-running the
  scanner (mitigated by the #86 S-2 fix, but the scanner is still the
  safety net).

Design constraints
------------------
* **DRY** — re-uses :func:`vesma.secrets_detector.detect_secrets`
  unchanged. The scanner NEVER re-implements a pattern. Layer 1 and
  Layer 2 share one source of truth.
* **No raw values in logs/audit** — only pattern names and counts.
  ``SecretFinding.matched_value`` is never logged, never written to the
  audit JSONL.
* **Thread-safe** — the scanner runs in a background daemon thread. The
  scan itself is read-only (it calls ``detect_secrets`` on each
  record's content); the tag update uses ``manager.update`` which is
  atomic at the SQLite level (single ``UPDATE`` statement).
* **Non-blocking** — the background thread never blocks the MCP server
  or HTTP API. A scan pass runs to completion or fails non-fatally; the
  interval timer waits for the previous pass to finish before starting
  the next one.
* **Idempotent** — a record that already carries ``mnemos:no-federate``
  is counted as ``skipped`` and never re-tagged. Running the scanner
  twice on the same corpus tags 0 records on the second pass.
* **Configurable** — interval, enabled/disabled, incremental/full.

Public API
----------
* :class:`BackgroundScanner` — start/stop the background loop, run a
  one-shot scan.
* :class:`ScanResult` — frozen dataclass returned by ``run_scan``.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from vesma.audit import log_scanner_audit
from vesma.config import ScannerConfig
from vesma.models import NO_FEDERATE_TAG, MemoryUpdate
from vesma.secrets_detector import detect_secrets, findings_by_pattern

if TYPE_CHECKING:
    from vesma.manager import MemoryManager
    from vesma.models import Memory

__all__ = [
    "BackgroundScanner",
    "ScanResult",
]

logger = logging.getLogger(__name__)

#: Action label written to the scanner audit log.
_AUDIT_ACTION: str = "background-scan"


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Summary of one background scan pass.

    All fields are counters or metadata — **no raw content, no secrets,
    no PII values**. ``patterns_matched`` is a ``{pattern_name: count}``
    mapping (pattern names only, never matched values). Safe to log,
    serialise to the audit JSONL, or return from the CLI.
    """

    records_scanned: int
    records_tagged: int
    records_skipped: int
    patterns_matched: dict[str, int] = field(default_factory=dict)
    duration_sec: float = 0.0
    incremental: bool = True
    timestamp: str = ""

    def __post_init__(self) -> None:
        # Frozen dataclass — use object.__setattr__ to set the default
        # timestamp when the caller omitted it.
        if not self.timestamp:
            object.__setattr__(
                self, "timestamp", datetime.now(UTC).isoformat().replace("+00:00", "Z")
            )

    def to_audit_entry(self) -> dict[str, object]:
        """Build the audit-log entry for this scan pass.

        Counters only — never includes raw content or matched values.
        """
        return {
            "action": _AUDIT_ACTION,
            "records_scanned": self.records_scanned,
            "records_tagged": self.records_tagged,
            "records_skipped": self.records_skipped,
            "patterns_matched": dict(self.patterns_matched),
            "duration_sec": round(self.duration_sec, 3),
            "incremental": self.incremental,
        }


class BackgroundScanner:
    """Layer 2 defence-in-depth: periodic corpus re-scan for secrets.

    Re-uses :func:`vesma.secrets_detector.detect_secrets` — the SAME
    patterns as the write-path scanner (Layer 1). No pattern is
    duplicated; the scanner is the safety net, not a second source of
    rules.

    Lifecycle
    ---------
    * :meth:`start` — launch the background daemon thread (idempotent;
      calling it twice is a no-op). No-op when ``config.enabled`` is
      ``False``.
    * :meth:`stop` — signal the thread to stop and join it (10s
      timeout). Safe to call when not running.
    * :meth:`run_scan` — run one pass synchronously (used by the CLI and
      by the background loop). Returns a :class:`ScanResult`.

    Thread safety
    -------------
    The scan is read-only over the corpus; the tag update goes through
    ``manager.update`` which is atomic at the SQLite level. The
    background thread holds no lock across the scan — a concurrent
    write simply means the scanner may see a slightly newer snapshot on
    the next pass, which is the desired behaviour.
    """

    def __init__(self, manager: MemoryManager, config: ScannerConfig) -> None:
        self._manager = manager
        self._config = config
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        # Last successful scan timestamp (UTC ISO 8601). Used as the
        # ``since`` boundary for incremental scans. ``None`` means "no
        # scan has run yet" → the first incremental scan scans nothing
        # newer than the epoch (i.e. effectively a full scan, because
        # every record is newer than no boundary). We model this as a
        # very old timestamp so the SQL ``>=`` comparison is uniform.
        self._last_scan_ts: datetime | None = None
        # Cumulative counter — total records tagged across all passes.
        # Used by ``vesma scanner status`` (CLI name). Reset only on process
        # restart (intentional — the counter is operational telemetry,
        # not a persisted metric).
        self._total_tagged: int = 0

    # ── Public properties ────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        """Whether the background scanner thread is currently active."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def enabled(self) -> bool:
        """Whether the scanner is enabled in config."""
        return self._config.enabled

    @property
    def last_scan_ts(self) -> datetime | None:
        """UTC timestamp of the last successful scan (``None`` if never)."""
        return self._last_scan_ts

    @property
    def total_tagged(self) -> int:
        """Cumulative count of records tagged across all passes."""
        return self._total_tagged

    @property
    def interval_sec(self) -> int:
        """Configured scan interval in seconds."""
        return self._config.interval_hours * 3600

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background scanner loop (idempotent, no-op if disabled).

        When ``config.enabled`` is ``False`` the call returns
        immediately without launching a thread — defence-in-depth is
        opt-out, but the opt-out is honoured.
        """
        if not self._config.enabled:
            logger.info("Background scanner disabled by config — not starting")
            return
        if self._thread is not None:
            return
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="vesma-scanner",
        )
        self._thread.start()
        logger.info(
            "Background scanner started (interval=%dh, incremental=%s)",
            self._config.interval_hours,
            self._config.incremental,
        )

    def stop(self) -> None:
        """Stop the background scanner loop and join the thread."""
        if self._thread is None or self._stop_event is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=10)
        self._thread = None
        self._stop_event = None
        logger.info("Background scanner stopped")

    # ── Scan loop ─────────────────────────────────────────────────────────

    def _loop(self) -> None:
        """Background loop: run a scan, wait for the interval, repeat.

        The first pass runs immediately on start (so a freshly-started
        server catches false negatives without waiting 6h). Subsequent
        passes wait ``interval_sec`` between runs. The wait uses the
        stop event so ``stop()`` wakes the loop promptly.
        """
        if self._stop_event is None:
            return
        while not self._stop_event.is_set():
            try:
                _scan_t0 = time.monotonic()
                self.run_scan(incremental=self._config.incremental)
                # Vitals boundary #6 (A2) — scanner.scan verb.
                self._manager.record_verb_vitals(
                    surface="background",
                    verb="scanner.scan",
                    status="ok",
                    latency_ms=(time.monotonic() - _scan_t0) * 1000,
                )
            except Exception as exc:
                # Vitals boundary #6 (A2) — error leg.
                with contextlib.suppress(Exception):
                    self._manager.record_verb_vitals(
                        surface="background",
                        verb="scanner.scan",
                        status="error",
                        latency_ms=(time.monotonic() - _scan_t0) * 1000,
                        meta={"error_type": type(exc).__name__},
                    )
                # Non-fatal — a scan failure must never crash the
                # scanner thread. The next pass will retry.
                logger.exception("Background scanner pass failed (non-fatal)")
            self._stop_event.wait(timeout=self.interval_sec)

    # ── One-shot scan ─────────────────────────────────────────────────────

    def run_scan(self, *, incremental: bool = True) -> ScanResult:
        """Run one scan pass and return a :class:`ScanResult`.

        Args:
            incremental: When ``True``, only records whose
                ``created_at`` OR ``updated_at`` is newer than the last
                successful scan are scanned. When ``False``, every
                record in the corpus is scanned regardless of
                modification time.

        Returns:
            Summary of the pass — counters only, no raw values.

        Side effects:
            * Records with detected secrets that do not already carry
              ``mnemos:no-federate`` are updated via ``manager.update``
              (atomic SQLite UPDATE) to add the tag.
            * ``last_scan_ts`` is advanced to the END of this pass
              (so the next incremental scan excludes records modified
              during this pass — they were already scanned).
            * One entry is appended to the scanner audit log
              (``~/.vesma/logs/scanner-audit.jsonl``).
        """
        t0 = time.monotonic()

        # Determine the ``since`` boundary for incremental scans.
        # ``None`` → full scan (no boundary). On the first incremental
        # scan (``_last_scan_ts is None``) we fall back to a full scan
        # because there is no prior boundary to compare against.
        since: datetime | None = None
        if incremental and self._last_scan_ts is not None:
            since = self._last_scan_ts

        records = self._fetch_records(since=since)

        records_scanned = 0
        records_tagged = 0
        records_skipped = 0
        patterns_matched: dict[str, int] = {}

        for memory in records:
            records_scanned += 1
            content = memory.content or ""
            try:
                findings = detect_secrets(content)
            except Exception:
                # Non-fatal — a detection failure on one record must not
                # abort the pass. Log and move on; the record is
                # counted as scanned but not tagged.
                logger.warning(
                    "detect_secrets failed for record %s (non-fatal)",
                    memory.id[:8],
                )
                continue

            if not findings:
                continue

            # Aggregate pattern counts (names only — never values).
            for name, count in findings_by_pattern(findings).items():
                patterns_matched[name] = patterns_matched.get(name, 0) + count

            if NO_FEDERATE_TAG in (memory.tags or []):
                records_skipped += 1
                continue

            # Tag the record. Reuse the manager's update path so the
            # tag-adding logic is consistent with Layer 1 (the write-
            # path scanner also goes through the same tag list).
            updated = self._manager.update(
                memory.id,
                MemoryUpdate(tags=[*list(memory.tags or []), NO_FEDERATE_TAG]),
            )
            if updated is not None:
                records_tagged += 1
                logger.info(
                    "background scan tagged record %s (patterns: %s) — raw values not logged",
                    memory.id[:8],
                    findings_by_pattern(findings),
                )
            else:
                # Record vanished between fetch and update — treat as
                # skipped, not a failure.
                records_skipped += 1

        duration = time.monotonic() - t0
        # Advance the boundary to the END of this pass. Records tagged
        # during the pass have updated_at between scan_started and now;
        # setting the boundary to scan_completed (after the loop) means
        # the next incremental scan excludes them (updated_at < boundary).
        scan_completed = datetime.now(UTC)
        self._last_scan_ts = scan_completed
        self._total_tagged += records_tagged

        result = ScanResult(
            records_scanned=records_scanned,
            records_tagged=records_tagged,
            records_skipped=records_skipped,
            patterns_matched=patterns_matched,
            duration_sec=duration,
            incremental=incremental,
        )

        logger.info(
            "background scan: scanned=%d, tagged=%d, skipped=%d, "
            "patterns=%s, duration=%.1fs, incremental=%s",
            result.records_scanned,
            result.records_tagged,
            result.records_skipped,
            result.patterns_matched,
            result.duration_sec,
            result.incremental,
        )

        # Audit log — counters only. The entry is built from the
        # ScanResult, which by construction contains no raw values.
        try:
            log_scanner_audit(result.to_audit_entry())
        except Exception:
            # Non-fatal — an audit-log write failure must not affect
            # the scan result.
            logger.warning("scanner audit log write failed (non-fatal)")

        return result

    # ── Helpers ──────────────────────────────────────────────────────────

    def _fetch_records(self, *, since: datetime | None) -> list[Memory]:
        """Fetch the records to scan from the SQLite store.

        Uses ``list_for_export`` because it already supports a ``since``
        boundary that applies to both ``created_at`` and ``updated_at``
        (so an updated record is re-scanned even if it was created
        before the boundary). When ``since`` is ``None`` every record
        is returned (full scan). No status filter — the scanner covers
        all statuses including ``raw`` and ``archived`` so a secret in
        an unpublished record is still caught before it can be
        federated by a future status change.
        """
        # ``list_for_export`` has no upper bound by default — we want
        # the whole corpus (or the whole delta), not a page.
        return self._manager.sqlite.list_for_export(since=since, limit=None)
