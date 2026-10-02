"""Phase A integration tests — the vitals plane inside vesmaro (ADR-0026).

The deep sink contract (allowlist projection, non-fatal semantics,
keyed-HMAC fingerprints, retention cascade) is pinned in the
mnemos-vitals repo — the master copy this package is vendored from.
This suite verifies the INTEGRATION: default-on wiring, the C3
config-lint, the collection boundary (async envelopes excluded), the
retention cadence, and the C1 isolation canary in its structural form
(only ``vesma.metrics`` may reference the sidecar — bug-report /
backup / export / federation code paths can therefore never include it).
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.metrics.boundary import create_vitals_store
from vesma.metrics.schema import SIDECAR_FILENAME

SRC = Path(__file__).resolve().parents[1] / "src" / "vesma"


def _settings(tmp: Path, **overrides: object) -> Settings:
    payload: dict[str, object] = {
        "vesma": {
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
        },
        "scanner": {"enabled": False},
    }
    payload.update(overrides)
    settings = Settings(**payload)
    settings.resolve_paths()
    return settings


def _manager(settings: Settings) -> MemoryManager:
    mgr = MemoryManager(settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    return mgr


class TestStoreWiring:
    def test_default_on_local_first(self, tmp_path: Path):
        """ArchCom 7ec9dda3 + owner directive: collection ships enabled."""
        store = create_vitals_store(_settings(tmp_path))
        assert store is not None
        store.close()

    def test_disabled_by_config(self, tmp_path: Path):
        settings = _settings(tmp_path, vitals={"enabled": False})
        assert create_vitals_store(settings) is None

    def test_c3_refuses_under_auth_without_ack(self, tmp_path: Path):
        """Per-request rows carry a session column: auth_enabled=true
        without the explicit acknowledgement refuses to start."""
        settings = _settings(tmp_path, api={"auth_enabled": True})
        assert create_vitals_store(settings) is None

    def test_c3_ack_admits_collection(self, tmp_path: Path):
        settings = _settings(
            tmp_path,
            api={"auth_enabled": True},
            vitals={"ack_session_collection": True},
        )
        store = create_vitals_store(settings)
        assert store is not None
        store.close()


class TestCollectionBoundary:
    def test_assemble_records_one_row(self, tmp_path: Path):
        settings = _settings(tmp_path)
        mgr = _manager(settings)
        try:
            result = mgr.assemble_context(session="sess-1", project="demo")
            assert "stats" in result  # a real assembly, not a handle envelope
            # the boundary lives on the SURFACES (MCP handler / hook), not
            # on the manager method — call it exactly as they do (S2 calls
            # the manager directly and must stay outside this path)
            mgr.record_assemble_vitals(result)
            db = settings.vesma.data_dir / SIDECAR_FILENAME
            assert db.exists()
            conn = sqlite3.connect(db)
            rows = conn.execute("SELECT * FROM assemble_metrics").fetchall()
            conn.close()
            assert len(rows) == 1
        finally:
            mgr.close()

    def test_async_envelope_is_not_recorded(self, tmp_path: Path):
        """mode="async" returns a handle envelope — the real assembly is
        recorded when the handle is redeemed, never the envelope."""
        settings = _settings(tmp_path)
        mgr = _manager(settings)
        try:
            envelope = {"mode": "async", "handle": "abc", "status": "ready"}
            mgr.record_assemble_vitals(envelope)  # must be a no-op
            db = settings.vesma.data_dir / SIDECAR_FILENAME
            assert not db.exists()  # nothing was written
        finally:
            mgr.close()

    def test_boundary_survives_garbage(self, tmp_path: Path):
        settings = _settings(tmp_path)
        mgr = _manager(settings)
        try:
            # carries "stats" so the garbage passes the envelope guard and
            # actually hits the sink's catch-all (not just the filter)
            garbage = {
                "stats": "not-a-dict",
                "tokens": {"estimated": "x"},
                "blocks": [{"score": "NOT-A-FLOAT"}],
            }
            mgr.record_assemble_vitals(garbage)  # no raise
        finally:
            mgr.close()


class TestRetentionJob:
    def test_retention_deletes_and_holds_cadence(self, tmp_path: Path):
        import time

        settings = _settings(tmp_path)
        mgr = _manager(settings)
        try:
            result = mgr.assemble_context(session="sess-1", project="demo")
            mgr.record_assemble_vitals(result)
            store = mgr._vitals_store
            assert store is not None
            db = settings.vesma.data_dir / SIDECAR_FILENAME
            conn = sqlite3.connect(db)
            old = time.time() - 400 * 86400
            conn.execute("UPDATE assemble_metrics SET ts=?", (old,))
            conn.commit()
            conn.close()

            mgr._vitals_retention_last_ts = 0.0
            mgr._maybe_run_vitals_retention()
            conn = sqlite3.connect(db)
            n = conn.execute("SELECT COUNT(*) FROM assemble_metrics").fetchone()[0]
            conn.close()
            assert n == 0  # TTL cascade applied

            stamp = mgr._vitals_retention_last_ts
            assert stamp > 0
            mgr._maybe_run_vitals_retention()  # within interval → skipped
            assert mgr._vitals_retention_last_ts == stamp
        finally:
            mgr.close()


class TestC1IsolationCanary:
    def test_import_guard_no_mnemos_vitals_dependency(self):
        """Guest contract: the server never imports the vitals repo."""
        pattern = re.compile(r"^\s*(?:from|import)\s+mnemos_vitals\b", re.MULTILINE)
        offenders = [
            str(p.relative_to(SRC))
            for p in SRC.rglob("*.py")
            if pattern.search(p.read_text(encoding="utf-8"))
        ]
        assert offenders == [], f"server imports the vitals repo: {offenders}"

    def test_sidecar_referenced_only_by_metrics_package(self):
        """C1 tripwire (not a boundary): nothing outside the vendored
        ``vesma/metrics/`` package may even NAME the sidecar — so
        bug-report / backup / export / federation code paths stay blind
        to it. The real C1 enforcement is that the sink is the only
        write path; this tripwire catches naming drift early."""
        tokens = ("metrics.sqlite", ".hkey", "SIDECAR_FILENAME")
        offenders: list[str] = []
        for p in SRC.rglob("*.py"):
            if p.relative_to(SRC).parts[0] == "metrics":
                continue  # the vendored package itself
            text = p.read_text(encoding="utf-8")
            if any(t in text for t in tokens):
                offenders.append(str(p.relative_to(SRC)))
        assert offenders == [], f"sidecar referenced outside vesma.metrics: {offenders}"

    def test_sidecar_lives_next_to_main_db_not_inside_it(self, tmp_path: Path):
        settings = _settings(tmp_path)
        mgr = _manager(settings)
        try:
            result = mgr.assemble_context(session="sess-1", project="demo")
            mgr.record_assemble_vitals(result)
            sidecar = settings.vesma.data_dir / SIDECAR_FILENAME
            main_db = settings.db_path
            assert sidecar.exists() and sidecar != main_db
            main_bytes = main_db.read_bytes()
            assert b"assemble_metrics" not in main_bytes  # born isolated
        finally:
            mgr.close()
