"""Tests for T3 — CCR cleanup wired into the background processor.

Verifies that ``ccr_cleanup()`` (TTL expiry + LRU eviction) runs
automatically from ``_processor_loop`` on its own interval, that disabled
CCR skips cleanup, that an exception in cleanup does not crash the
processor, and that the interval config is respected.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from vesma.config import Settings
from vesma.manager import MemoryManager


@pytest.fixture(autouse=True)
def _restore_compat_env_aliases() -> Iterator[None]:
    """Restore the #139 compat env aliases after each test.

    ``_make_settings`` writes ``VESMA_DATA_DIR`` / ``VESMA_VAULT__VAULT_PATH``
    directly into ``os.environ``. Since the #139 compat shim made these names
    live, a leak would bleed into later modules (e.g. ``test_config_layout``
    asserts default paths under a fake home). Snapshot and restore around
    every test in this module.
    """
    names = ("VESMA_DATA_DIR", "VESMA_VAULT__VAULT_PATH")
    saved = {name: os.environ.get(name) for name in names}
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_settings(tmp_path: Path, **ccr_overrides: object) -> Settings:
    """Create Settings with isolated tmp paths and CCR config overrides.

    Uses ``ccr_cleanup_interval_sec=60`` (the field minimum) so tests stay
    within the config bounds. Timing-sensitive tests manipulate
    ``_ccr_cleanup_last_ts`` directly instead of waiting real seconds.
    """
    os.environ["VESMA_DATA_DIR"] = str(tmp_path / "data")
    os.environ["VESMA_VAULT__VAULT_PATH"] = str(tmp_path / "vault")
    Path(tmp_path / "data").mkdir(parents=True, exist_ok=True)
    Path(tmp_path / "vault").mkdir(parents=True, exist_ok=True)
    ccr = {
        "min_size_chars": 100,
        "max_entries": 100,
        "ttl_days": 1,
        "ccr_cleanup_interval_sec": 60,
    }
    ccr.update(ccr_overrides)
    s = Settings(ccr=ccr)
    s.resolve_paths()
    return s


# ── Cleanup runs on interval ────────────────────────────────────────────────


class TestCleanupRunsOnInterval:
    def test_maybe_run_ccr_cleanup_calls_ccr_cleanup(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        mgr = MemoryManager(settings)
        try:
            with (
                patch.object(
                    mgr,
                    "ccr_cleanup",
                    return_value={"ttl_deleted": 0, "lru_evicted": 0},
                ) as mock_cleanup,
            ):
                # First call: _ccr_cleanup_last_ts is 0 → runs immediately.
                mgr._maybe_run_ccr_cleanup()
                assert mock_cleanup.called
        finally:
            mgr.close()

    def test_maybe_run_ccr_cleanup_skips_within_interval(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path, ccr_cleanup_interval_sec=3600)
        mgr = MemoryManager(settings)
        try:
            with (
                patch.object(
                    mgr,
                    "ccr_cleanup",
                    return_value={"ttl_deleted": 0, "lru_evicted": 0},
                ) as mock_cleanup,
            ):
                # First call runs (last_ts was 0).
                mgr._maybe_run_ccr_cleanup()
                assert mock_cleanup.call_count == 1
                # Second call within the 3600s interval → skipped.
                mgr._maybe_run_ccr_cleanup()
                assert mock_cleanup.call_count == 1
        finally:
            mgr.close()

    def test_maybe_run_ccr_cleanup_runs_after_interval_elapses(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path, ccr_cleanup_interval_sec=60)
        mgr = MemoryManager(settings)
        try:
            with (
                patch.object(
                    mgr,
                    "ccr_cleanup",
                    return_value={"ttl_deleted": 0, "lru_evicted": 0},
                ) as mock_cleanup,
            ):
                # First call runs.
                mgr._maybe_run_ccr_cleanup()
                assert mock_cleanup.call_count == 1
                # Simulate interval elapse by backdating _ccr_cleanup_last_ts
                # to 61 seconds ago (just past the 60s interval).
                mgr._ccr_cleanup_last_ts = time.monotonic() - 61
                mgr._maybe_run_ccr_cleanup()
                assert mock_cleanup.call_count == 2
        finally:
            mgr.close()


# ── Disabled CCR skips cleanup ──────────────────────────────────────────────


class TestDisabledCCRSkipsCleanup:
    def test_disabled_ccr_skips_cleanup(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        settings.ccr.enabled = False
        mgr = MemoryManager(settings)
        try:
            with (
                patch.object(
                    mgr,
                    "ccr_cleanup",
                    return_value={"ttl_deleted": 0, "lru_evicted": 0},
                ) as mock_cleanup,
            ):
                mgr._maybe_run_ccr_cleanup()
                assert not mock_cleanup.called
        finally:
            mgr.close()


# ── Exception in cleanup does not crash processor ───────────────────────────


class TestExceptionDoesNotCrashProcessor:
    def test_cleanup_exception_is_caught(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        settings = _make_settings(tmp_path)
        mgr = MemoryManager(settings)
        try:
            with patch.object(mgr, "ccr_cleanup", side_effect=RuntimeError("boom")):
                # Must not raise.
                mgr._maybe_run_ccr_cleanup()
            # The exception is logged (non-fatal).
            assert any("CCR cleanup failed" in r.message for r in caplog.records)
        finally:
            mgr.close()

    def test_processor_loop_survives_cleanup_exception(self, tmp_path: Path) -> None:
        """The full _processor_loop must not crash when cleanup raises."""
        settings = _make_settings(tmp_path, ccr_cleanup_interval_sec=60)
        mgr = MemoryManager(settings)
        try:
            call_count = {"n": 0}

            def _flaky_cleanup() -> dict[str, int]:
                call_count["n"] += 1
                raise RuntimeError("cleanup boom")

            with patch.object(mgr, "ccr_cleanup", side_effect=_flaky_cleanup):
                # Start the processor with a short interval so it cycles fast.
                mgr.start_background_processor(interval_sec=1)
                # Let it run a couple of cycles.
                time.sleep(2.5)
                mgr.stop_background_processor()
            # The processor did not crash — it ran at least one cleanup attempt.
            assert call_count["n"] >= 1
        finally:
            mgr.stop_background_processor()
            mgr.close()


# ── Interval config respected ──────────────────────────────────────────────


class TestIntervalConfigRespected:
    def test_ccr_cleanup_interval_sec_default(self) -> None:
        from vesma.config import CCRConfig

        cfg = CCRConfig()
        assert cfg.ccr_cleanup_interval_sec == 1200

    def test_ccr_cleanup_interval_sec_min_60(self) -> None:
        from pydantic import ValidationError

        from vesma.config import CCRConfig

        with pytest.raises(ValidationError):
            CCRConfig(ccr_cleanup_interval_sec=30)

    def test_ccr_cleanup_interval_sec_max_86400(self) -> None:
        from pydantic import ValidationError

        from vesma.config import CCRConfig

        with pytest.raises(ValidationError):
            CCRConfig(ccr_cleanup_interval_sec=100000)

    def test_ccr_cleanup_interval_sec_custom(self) -> None:
        from vesma.config import CCRConfig

        cfg = CCRConfig(ccr_cleanup_interval_sec=600)
        assert cfg.ccr_cleanup_interval_sec == 600


# ── QA fix #7 — CCR cleanup logs when ttl/lru nonzero ──────────────────────


class TestCleanupLogsWhenNonzero:
    def test_ccr_cleanup_logs_when_nonzero(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When ccr_cleanup returns ttl_deleted>0 or lru_evicted>0, the
        info-level log line at manager.py:1391-1395 must fire."""
        settings = _make_settings(tmp_path)
        mgr = MemoryManager(settings)
        try:
            with (
                patch.object(
                    mgr,
                    "ccr_cleanup",
                    return_value={"ttl_deleted": 5, "lru_evicted": 2},
                ),
                caplog.at_level("INFO", logger="vesma.manager"),
            ):
                mgr._maybe_run_ccr_cleanup()
            # The log message must mention both counts.
            assert any(
                "ttl_deleted=5" in r.message and "lru_evicted=2" in r.message
                for r in caplog.records
            ), [r.message for r in caplog.records]
        finally:
            mgr.close()

    def test_ccr_cleanup_no_log_when_zero(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Conversely, when both counts are 0, the info log must NOT fire
        (no spam on idle cycles)."""
        settings = _make_settings(tmp_path)
        mgr = MemoryManager(settings)
        try:
            with (
                patch.object(
                    mgr,
                    "ccr_cleanup",
                    return_value={"ttl_deleted": 0, "lru_evicted": 0},
                ),
                caplog.at_level("INFO", logger="vesma.manager"),
            ):
                mgr._maybe_run_ccr_cleanup()
            assert not any("ttl_deleted=" in r.message for r in caplog.records), [
                r.message for r in caplog.records
            ]
        finally:
            mgr.close()

    def test_ccr_cleanup_logs_ttl_only(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Only ttl_deleted nonzero (lru 0) still triggers the log."""
        settings = _make_settings(tmp_path)
        mgr = MemoryManager(settings)
        try:
            with (
                patch.object(
                    mgr,
                    "ccr_cleanup",
                    return_value={"ttl_deleted": 3, "lru_evicted": 0},
                ),
                caplog.at_level("INFO", logger="vesma.manager"),
            ):
                mgr._maybe_run_ccr_cleanup()
            assert any(
                "ttl_deleted=3" in r.message and "lru_evicted=0" in r.message
                for r in caplog.records
            )
        finally:
            mgr.close()


# ── QA fix #8 — runtime mutation of ccr_cleanup_interval_sec ───────────────


class TestIntervalRuntimeMutation:
    def test_ccr_cleanup_interval_change_takes_effect_at_runtime(self, tmp_path: Path) -> None:
        """Mutating settings.ccr.ccr_cleanup_interval_sec between calls must
        take effect without a restart — the interval is read fresh on each
        _maybe_run_ccr_cleanup call."""
        settings = _make_settings(tmp_path, ccr_cleanup_interval_sec=60)
        mgr = MemoryManager(settings)
        try:
            with (
                patch.object(
                    mgr,
                    "ccr_cleanup",
                    return_value={"ttl_deleted": 0, "lru_evicted": 0},
                ) as mock_cleanup,
            ):
                # 1) First call runs (last_ts was 0).
                mgr._maybe_run_ccr_cleanup()
                assert mock_cleanup.call_count == 1

                # 2) Increase interval way beyond any plausible elapsed
                #    time → second call within the new interval must skip.
                mgr.settings.ccr.ccr_cleanup_interval_sec = 86400
                mgr._maybe_run_ccr_cleanup()
                assert mock_cleanup.call_count == 1, (
                    "after increasing interval, the second call must skip"
                )

                # 3) Decrease interval back to the minimum (60s) and
                #    backdate last_ts so the elapsed time exceeds it →
                #    the third call must run again.
                mgr.settings.ccr.ccr_cleanup_interval_sec = 60
                mgr._ccr_cleanup_last_ts = time.monotonic() - 61
                mgr._maybe_run_ccr_cleanup()
                assert mock_cleanup.call_count == 2, (
                    "after decreasing interval + backdating last_ts, the call must run"
                )
        finally:
            mgr.close()


# ── QA fix #9 — ccr.enabled flip mid-run ───────────────────────────────────


class TestCcrEnabledFlipMidRun:
    def test_ccr_enabled_flip_takes_effect_without_restart(self, tmp_path: Path) -> None:
        """Flipping settings.ccr.enabled at runtime (without restarting
        the manager) must take effect on the next _maybe_run_ccr_cleanup."""
        settings = _make_settings(tmp_path)
        mgr = MemoryManager(settings)
        try:
            with (
                patch.object(
                    mgr,
                    "ccr_cleanup",
                    return_value={"ttl_deleted": 0, "lru_evicted": 0},
                ) as mock_cleanup,
            ):
                # 1) Start disabled → skips.
                mgr.settings.ccr.enabled = False
                mgr._maybe_run_ccr_cleanup()
                assert mock_cleanup.call_count == 0

                # 2) Flip to enabled → runs (last_ts was 0).
                mgr.settings.ccr.enabled = True
                mgr._maybe_run_ccr_cleanup()
                assert mock_cleanup.call_count == 1

                # 3) Flip back to disabled → skips even though last_ts
                #    is now set (the enabled guard short-circuits first).
                mgr.settings.ccr.enabled = False
                mgr._maybe_run_ccr_cleanup()
                assert mock_cleanup.call_count == 1
        finally:
            mgr.close()
