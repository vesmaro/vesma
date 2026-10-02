"""ADR-0018 P1-a — storage/detector layer tests.

Covers the four P1-a build items (manager-issuance items M1/m2/m5 are
P1-b, a separate wave):

* scan-at-store verdict flag — ``ccr_store`` persists a
  ``secret_scan_verdict`` ('clean' | 'hit' | 'unknown'; NULL on legacy
  rows) while the stored original stays verbatim, and issuance keeps
  scanning unconditionally (no fast-path on a stored verdict);
* project scoping — ``ccr_get`` / ``ccr_search`` / ``retrieve_content``
  scope lookups to a project when one is supplied; a hash cached under
  another project is not returned (cross-session marker redemption
  denied, fail-closed); unscoped callers keep the legacy behavior;
* m3 detector overlap-tail fix — a discrete pattern matching the prefix
  of a longer high-entropy run extends the accepted span to max(end) so
  the tail is redacted;
* minimal ``memory_edges`` — supersedes-only edges with idempotent add,
  direct-edge reads, self-edge/kind/FK constraints, and cascade on
  memory delete.

All secrets below are obviously fake EXAMPLE-style values built from the
detector's own pattern catalogue (src/vesma/secrets_detector.py); real
credentials never appear in this file.
"""

from __future__ import annotations

import logging
import sqlite3
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.models import Memory, MemorySource, MemoryStatus, MemoryType
from vesma.secrets_detector import detect_secrets, redact_content
from vesma.storage.sqlite_store import SQLiteStore

# ── Fake (EXAMPLE-style) secrets from the detector's own regexes ──────────────

# aws-key pattern: AKIA + 16 chars of [0-9A-Z] (zero-entropy body so the
# high-entropy leg never fires on this value alone).
FAKE_AWS_KEY = "AKIAEXAMPLEABCDEFGH1"

# m3 fixture: the first 20 chars are a valid aws-key; the full 38-char
# contiguous alnum run clears the high-entropy leg (Shannon entropy
# ~4.99 bits/char > 4.8 threshold, span length 38 >= 32). Under the OLD
# overlap rule the entropy finding was dropped entirely, leaving the
# 18-char tail unredacted; the m3 fix must extend the aws-key span over
# the whole run.
FAKE_OVERLAP_RUN = "AKIA" + "QZ7WJ4XEPLMRT82H" + "bY9uHk5NqR3sVd7Wxj"
FAKE_OVERLAP_TAIL = FAKE_OVERLAP_RUN[20:]


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _settings(tmp: Path, **ccr_overrides: object) -> Settings:
    """Settings against ``tmp`` with CCR tuned for tests."""
    ccr: dict[str, object] = {
        "min_size_chars": 100,
        "max_entries": 100,
        "ttl_days": 1,
    }
    ccr.update(ccr_overrides)
    settings = Settings(
        vesma={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
        },
        ccr=ccr,  # type: ignore[arg-type]
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def manager() -> Iterator[MemoryManager]:
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = MemoryManager(_settings(Path(tmpdir)))
        # Deterministic embedder — manager.add() embeds synchronously and
        # must not pull a real ONNX model in tests.
        mock_embedder = MagicMock()
        mock_embedder.embed.return_value = [0.1] * 384
        mgr._embedder = mock_embedder
        yield mgr
        mgr.close()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    s = SQLiteStore(tmp_path / "test.db")
    yield s
    s.close()


def _secret_log(secret: str) -> str:
    """Log-like content with one secret line (>100 chars, cacheable)."""
    lines = [f"2026-08-26T10:00:{i % 60:02d}Z INFO worker processing item {i}" for i in range(20)]
    lines.append(
        f"2026-08-26T10:01:00Z CONFIG the unobtanium service authenticates with api key {secret}"
    )
    lines.append("2026-08-26T10:01:01Z INFO shutdown complete")
    return "\n".join(lines)


def _clean_log() -> str:
    """Cacheable content with no secret patterns."""
    lines = [f"2026-08-26T11:00:{i % 60:02d}Z INFO worker finished item {i}" for i in range(30)]
    return "\n".join(lines)


def _as_legacy_unscanned(mgr: MemoryManager, h: str) -> None:
    """Rewrite a row's scan verdict to NULL (pre-P1-a legacy cache row).

    B5 tier-1 (ArchCom 2026-08-27): rows whose verdict is ``'hit'`` REFUSE
    snippet mode outright, so the snippet-path semantics pinned here are
    only reachable on rows the store did not verdict — legacy NULL rows.
    This helper simulates exactly that population; the hit-row refusal
    itself is covered in ``tests/test_b5_verdict_snippet_refusal.py``.
    """
    conn = mgr.sqlite._get_conn()
    conn.execute("UPDATE ccr_cache SET secret_scan_verdict=NULL WHERE hash=?", (h,))
    conn.commit()


def _make_memory(mid: str, content: str = "hello world") -> Memory:
    now = datetime.now(UTC)
    return Memory(
        id=mid,
        content=content,
        title="test",
        tags=["project:p1a", "agent:p1a-agent", "mnemos:learning"],
        source=MemorySource.MANUAL,
        source_url=None,
        memory_type=MemoryType.NOTE,
        created_at=now,
        updated_at=now,
        metadata={},
        file_path=None,
        category=None,
        project="p1a",
        agent="p1a-agent",
        status=MemoryStatus.RAW,
        quality_score=None,
        confidence=None,
        source_coverage=None,
        cluster_id=None,
        derived_from=[],
        embedding_id=None,
        raw_content=None,
        clean_content=None,
        filter_profile=None,
        filter_stats=None,
        filter_version=None,
    )


# ── B1: scan-at-store verdict flag ────────────────────────────────────────────


class TestScanAtStoreVerdict:
    def test_hit_verdict_persisted_original_verbatim(self, store):
        text = _secret_log(FAKE_AWS_KEY)
        store.ccr_store(hash="a" * 64, original=text, project="p1a")

        entry = store.ccr_get("a" * 64)

        assert entry is not None
        assert entry["secret_scan_verdict"] == "hit"
        assert entry["secret_scan_at"] is not None
        # Zero-loss: the stored original is byte-identical, flag only.
        assert entry["original"] == text
        assert FAKE_AWS_KEY in entry["original"]

    def test_clean_verdict_persisted(self, store):
        text = _clean_log()
        store.ccr_store(hash="b" * 64, original=text, project="p1a")

        entry = store.ccr_get("b" * 64)

        assert entry is not None
        assert entry["secret_scan_verdict"] == "clean"

    def test_hit_round_trip_issuance_still_redacts(self, manager):
        text = _secret_log(FAKE_AWS_KEY)
        h = manager.compress_content(text, profile="log", project="p1a")["hash"]

        # Verdict is 'hit' on the row...
        stored = manager.sqlite.ccr_get(h)
        assert stored is not None
        assert stored["secret_scan_verdict"] == "hit"

        # ...and issuance STILL scans and redacts (no fast-path).
        result = manager.retrieve_content(h)
        assert result["found"] is True
        assert FAKE_AWS_KEY not in result["original"]
        assert "<REDACTED:aws-key>" in result["original"]
        assert result["redactions"] >= 1

    def test_clean_round_trip_no_fast_path_scan_still_runs(self, manager):
        text = _clean_log()
        h = manager.compress_content(text, profile="log")["hash"]

        stored = manager.sqlite.ccr_get(h)
        assert stored is not None
        assert stored["secret_scan_verdict"] == "clean"

        result = manager.retrieve_content(h)
        assert result["found"] is True
        assert result["original"] == text
        assert result["redactions"] == 0

    def test_restore_refreshes_verdict_on_legacy_null_row(self, manager):
        """Re-compressing identical content upgrades a legacy NULL verdict."""
        text = _secret_log(FAKE_AWS_KEY)
        h = manager.compress_content(text, profile="log")["hash"]

        conn = manager.sqlite._get_conn()
        conn.execute(
            "UPDATE ccr_cache SET secret_scan_verdict=NULL, secret_scan_at=NULL WHERE hash=?",
            (h,),
        )
        conn.commit()
        legacy = manager.sqlite.ccr_get(h)
        assert legacy is not None
        assert legacy["secret_scan_verdict"] is None

        manager.compress_content(text, profile="log")

        refreshed = manager.sqlite.ccr_get(h)
        assert refreshed is not None
        assert refreshed["secret_scan_verdict"] == "hit"

    def test_hit_warning_logs_hash_not_value(self, manager, caplog):
        text = _secret_log(FAKE_AWS_KEY)
        with caplog.at_level(logging.WARNING, logger="vesma.storage.sqlite_store"):
            manager.compress_content(text, profile="log")

        warnings = [r for r in caplog.records if "CCR store scan hit" in r.message]
        assert warnings, "store-time hit must log a WARNING"
        rendered = " ".join(r.getMessage() for r in warnings)
        # Log-safe: pattern counts only, never the matched value.
        assert FAKE_AWS_KEY not in rendered
        assert "aws-key" in rendered


class TestLegacyUnscannedRows:
    @pytest.mark.parametrize("verdict", [None, "unknown"])
    def test_unscanned_row_still_scanned_at_issuance(self, manager, verdict):
        """NULL (pre-migration) and 'unknown' verdicts are unscanned.

        Issuance must keep scanning them — the verdict is observability
        only and never gates the issuance scan.
        """
        text = _secret_log(FAKE_AWS_KEY)
        h = manager.compress_content(text, profile="log")["hash"]

        conn = manager.sqlite._get_conn()
        conn.execute(
            "UPDATE ccr_cache SET secret_scan_verdict=? WHERE hash=?",
            (verdict, h),
        )
        conn.commit()

        result = manager.retrieve_content(h)

        assert result["found"] is True
        assert FAKE_AWS_KEY not in result["original"]
        assert "<REDACTED:aws-key>" in result["original"]
        assert result["redactions"] >= 1


# ── B2: project scoping ───────────────────────────────────────────────────────


class TestProjectScopingStore:
    def test_ccr_get_scoped_positive(self, store):
        store.ccr_store(hash="c" * 64, original=_clean_log(), project="alpha")

        entry = store.ccr_get("c" * 64, project="alpha")

        assert entry is not None
        assert entry["project"] == "alpha"

    def test_ccr_get_scoped_negative_other_project(self, store):
        store.ccr_store(hash="d" * 64, original=_clean_log(), project="alpha")

        assert store.ccr_get("d" * 64, project="beta") is None

    def test_ccr_get_unscoped_legacy_behavior(self, store):
        store.ccr_store(hash="e" * 64, original=_clean_log(), project="alpha")

        assert store.ccr_get("e" * 64) is not None

    def test_mismatch_does_not_bump_retrieval_counter(self, store):
        store.ccr_store(hash="f" * 64, original=_clean_log(), project="alpha")

        assert store.ccr_get("f" * 64, project="beta") is None
        conn = store._get_conn()
        row = conn.execute(
            "SELECT retrieval_count FROM ccr_cache WHERE hash=?", ("f" * 64,)
        ).fetchone()
        assert int(row["retrieval_count"]) == 0

    def test_ccr_search_scoped_snippet_channel(self, store):
        text = _secret_log(FAKE_AWS_KEY)
        store.ccr_store(hash="1" * 64, original=text, project="alpha")

        hits = store.ccr_search("1" * 64, "unobtanium", project="alpha")
        assert hits, "owning project must get snippet hits"

        assert store.ccr_search("1" * 64, "unobtanium", project="beta") == []


class TestProjectScopingManager:
    def test_retrieve_scoped_positive_and_negative(self, manager):
        text = _secret_log(FAKE_AWS_KEY)
        h = manager.compress_content(text, profile="log", project="alpha")["hash"]

        ok = manager.retrieve_content(h, project="alpha")
        assert ok["found"] is True
        assert "<REDACTED:aws-key>" in ok["original"]

        denied = manager.retrieve_content(h, project="beta")
        assert denied["found"] is False
        assert "original" not in denied

    def test_retrieve_unscoped_keeps_legacy_behavior(self, manager):
        text = _clean_log()
        h = manager.compress_content(text, profile="log", project="alpha")["hash"]

        result = manager.retrieve_content(h)
        assert result["found"] is True
        assert result["original"] == text

    def test_snippet_retrieval_scoped(self, manager):
        text = _secret_log(FAKE_AWS_KEY)
        h = manager.compress_content(text, profile="log", project="alpha")["hash"]
        # B5 tier-1: hit rows refuse snippet mode; the scoping semantics
        # pinned here live on the legacy unscanned population.
        _as_legacy_unscanned(manager, h)

        ok = manager.retrieve_content(h, query="unobtanium", project="alpha")
        assert ok["found"] is True
        assert ok["snippets"]

        denied = manager.retrieve_content(h, query="unobtanium", project="beta")
        assert denied["found"] is False
        assert "snippets" not in denied


# ── B3: m3 detector overlap-tail fix ──────────────────────────────────────────


class TestOverlapTailRedaction:
    def test_fixture_legs_fire(self):
        """Both detector legs must fire on the fixture: the first 20 chars
        are a discrete aws-key, and the whole run clears the high-entropy
        gate (own thresholds, so retuning the detector trips this first)."""
        from vesma.secrets_detector import (
            _BASE64_SPAN_RE,
            _HIGH_ENTROPY_THRESHOLD,
            _shannon_entropy,
        )

        discrete = detect_secrets(FAKE_AWS_KEY)
        assert [f.pattern_name for f in discrete] == ["aws-key"]

        assert _BASE64_SPAN_RE.fullmatch(FAKE_OVERLAP_RUN) is not None
        assert _shannon_entropy(FAKE_OVERLAP_RUN) > _HIGH_ENTROPY_THRESHOLD

    def test_prefix_discrete_pattern_extends_over_entropy_tail(self):
        findings = detect_secrets(FAKE_OVERLAP_RUN)

        # Exactly one accepted finding: aws-key wins the same-start tie
        # and its span is EXTENDED to the entropy run's end.
        assert len(findings) == 1
        assert findings[0].pattern_name == "aws-key"
        assert findings[0].end == len(FAKE_OVERLAP_RUN)
        assert findings[0].matched_value == FAKE_OVERLAP_RUN

    def test_tail_redacted_in_output(self):
        findings = detect_secrets(FAKE_OVERLAP_RUN)
        redacted = redact_content(FAKE_OVERLAP_RUN, findings)

        # The whole run — including the 18-char tail that the old rule
        # left in the clear — is replaced by a single redaction marker.
        assert FAKE_OVERLAP_TAIL not in redacted
        assert FAKE_OVERLAP_RUN not in redacted
        assert redacted == "<REDACTED:aws-key>"

    def test_tail_redacted_in_context(self):
        content = f"config api_key={FAKE_OVERLAP_RUN} for staging"
        findings = detect_secrets(content)
        redacted = redact_content(content, findings)

        # "api_key=" is itself inside the base64-like charset, so the
        # entropy span starts earlier and owns the label — what matters
        # for m3 is that the tail is gone and the span is redacted.
        assert FAKE_OVERLAP_TAIL not in redacted
        assert FAKE_OVERLAP_RUN not in redacted
        assert "<REDACTED:" in redacted

    def test_discrete_prefix_owns_label_when_delimited(self):
        content = f"config value {FAKE_OVERLAP_RUN} for staging"
        findings = detect_secrets(content)
        redacted = redact_content(content, findings)

        # Whitespace-delimited: the span starts at AKIA, the aws-key wins
        # the same-start tie, and the extension covers the whole run.
        assert [f.pattern_name for f in findings] == ["aws-key"]
        assert findings[0].end - findings[0].start == len(FAKE_OVERLAP_RUN)
        assert FAKE_OVERLAP_TAIL not in redacted
        assert "<REDACTED:aws-key>" in redacted

    def test_fully_contained_finding_still_dropped(self):
        """Containment semantics are unchanged (JWT-wins regression)."""
        jwt = "eyJTESTTESTTEST.eyJAKIATESTTESTTESTTESTTT.TEST"
        findings = detect_secrets(f"tok={jwt}")
        names = [f.pattern_name for f in findings]
        assert "jwt" in names
        assert "aws-key" not in names


# ── B4: memory_edges ──────────────────────────────────────────────────────────


class TestMemoryEdgesStore:
    def test_add_and_get_direct_edges(self, store):
        store.save(_make_memory("m-new"))
        store.save(_make_memory("m-old"))

        assert store.add_memory_edge("m-new", "m-old") is True

        edges = store.get_direct_edges("m-new")
        assert len(edges) == 1
        assert edges[0]["from_memory_id"] == "m-new"
        assert edges[0]["to_memory_id"] == "m-old"
        assert edges[0]["kind"] == "supersedes"
        assert edges[0]["created_at"]

    def test_add_is_idempotent(self, store):
        store.save(_make_memory("m-new"))
        store.save(_make_memory("m-old"))

        assert store.add_memory_edge("m-new", "m-old") is True
        assert store.add_memory_edge("m-new", "m-old") is False
        assert len(store.get_direct_edges("m-new")) == 1

    def test_self_edge_rejected(self, store):
        store.save(_make_memory("m-solo"))
        with pytest.raises(ValueError, match="self-edge"):
            store.add_memory_edge("m-solo", "m-solo")

    def test_unknown_kind_rejected(self, store):
        store.save(_make_memory("m-a"))
        store.save(_make_memory("m-b"))
        with pytest.raises(ValueError, match="unknown edge kind"):
            store.add_memory_edge("m-a", "m-b", kind="derives-from")

    def test_foreign_key_enforced(self, store):
        store.save(_make_memory("m-real"))
        with pytest.raises(sqlite3.IntegrityError):
            store.add_memory_edge("m-real", "m-ghost")

    def test_cascade_delete_removes_edges(self, store):
        store.save(_make_memory("m-new"))
        store.save(_make_memory("m-old"))
        store.add_memory_edge("m-new", "m-old")

        store.delete("m-old")

        assert store.get_direct_edges("m-new") == []

    def test_edges_do_not_leak_across_from_ids(self, store):
        store.save(_make_memory("m-a1"))
        store.save(_make_memory("m-a2"))
        store.save(_make_memory("m-b1"))
        store.save(_make_memory("m-b2"))
        store.add_memory_edge("m-a1", "m-a2")
        store.add_memory_edge("m-b1", "m-b2")

        assert [e["to_memory_id"] for e in store.get_direct_edges("m-a1")] == ["m-a2"]
        assert [e["to_memory_id"] for e in store.get_direct_edges("m-b1")] == ["m-b2"]


class TestMemoryEdgesManager:
    def test_manager_wrappers(self, manager):
        from vesma.models import MemoryCreate

        new = manager.add(
            MemoryCreate(
                content="replacement memory content",
                tags=["project:p1a", "agent:p1a-agent", "mnemos:learning"],
                status=MemoryStatus.PUBLISHED,
            ),
            project="p1a",
            agent="p1a-agent",
        )
        old = manager.add(
            MemoryCreate(
                content="superseded memory content",
                tags=["project:p1a", "agent:p1a-agent", "mnemos:learning"],
                status=MemoryStatus.PUBLISHED,
            ),
            project="p1a",
            agent="p1a-agent",
        )

        assert manager.add_memory_edge(new.id, old.id) is True
        assert manager.add_memory_edge(new.id, old.id) is False

        edges = manager.get_memory_edges(new.id)
        assert len(edges) == 1
        assert edges[0]["to_memory_id"] == old.id
        assert edges[0]["kind"] == "supersedes"

    def test_manager_self_edge_rejected(self, manager):
        from vesma.models import MemoryCreate

        mem = manager.add(
            MemoryCreate(
                content="solo memory content",
                tags=["project:p1a", "agent:p1a-agent", "mnemos:learning"],
                status=MemoryStatus.PUBLISHED,
            ),
            project="p1a",
            agent="p1a-agent",
        )
        with pytest.raises(ValueError, match="self-edge"):
            manager.add_memory_edge(mem.id, mem.id)
