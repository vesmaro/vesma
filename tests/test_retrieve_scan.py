"""ADR-0018 P0 — issuance secret scan on vesma_retrieve + status gate.

P0 fix-track tests (docs/project/adr/0018-context-rewrite-ltm-bridge.md,
Phases row 1) for the CCR rehydrate channel:

* secret echo — a compressed original containing a fake secret is
  redacted in the ``retrieve_content`` response, while the stored
  original stays byte-identical (zero-loss storage);
* redaction note — the response reports the redaction count and
  log-safe per-pattern counts;
* snippet path — FTS5 snippets are masked too;
* refuse mode — ``ccr.retrieve_refuse_on_secret`` returns
  ``refused=True`` with no content;
* raw issuance — the ``CONTEXT_ADMISSIBLE_STATUSES`` gate keeps raw /
  archived records out of default search (published/processed defaults);
* regression — clean content round-trips unchanged (``redactions == 0``).

All secrets below are obviously fake EXAMPLE-style values built from the
detector's own pattern catalogue (src/mnemos/secrets_detector.py); real
credentials never appear in this file.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vesmaro.config import Settings
from vesmaro.manager import MemoryManager
from vesmaro.models import CONTEXT_ADMISSIBLE_STATUSES, MemoryStatus
from vesmaro.secrets_detector import detect_secrets

# ── Fake (EXAMPLE-style) secrets from the detector's own regexes ──────────────

# aws-key pattern: AKIA + 16 chars of [0-9A-Z].
FAKE_AWS_KEY = "AKIAEXAMPLEABCDEFGH1"
# github-token pattern: ghp_ + 36 chars of [A-Za-z0-9] (all-same-char →
# zero entropy, so the high-entropy leg never fires on it).
FAKE_GITHUB_TOKEN = "ghp_" + "f" * 36


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
def manager() -> MemoryManager:
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = MemoryManager(_settings(Path(tmpdir)))
        yield mgr
        mgr.close()


@pytest.fixture
def refuse_manager() -> MemoryManager:
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = MemoryManager(_settings(Path(tmpdir), retrieve_refuse_on_secret=True))
        yield mgr
        mgr.close()


@pytest.fixture
def search_manager() -> MemoryManager:
    """Manager with a deterministic embedder (no ONNX dependency)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = MemoryManager(_settings(Path(tmpdir)))
        mock_embedder = MagicMock()
        mock_embedder.embed.return_value = [0.1] * 384
        mgr._embedder = mock_embedder
        yield mgr
        mgr.close()


# ── Content builders ──────────────────────────────────────────────────────────


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
    snippet mode outright, so the m2/m5 snippet-path semantics these tests
    pin (mask / scanner-error) are only reachable on rows the store did
    not verdict — legacy NULL rows. This helper simulates exactly that
    population; the hit-row refusal itself is covered in
    ``tests/test_b5_verdict_snippet_refusal.py``.
    """
    conn = mgr.sqlite._get_conn()
    conn.execute("UPDATE ccr_cache SET secret_scan_verdict=NULL WHERE hash=?", (h,))
    conn.commit()


# ── Fixture sanity ────────────────────────────────────────────────────────────


class TestFakeFixtures:
    def test_fake_secrets_are_detected(self):
        """The fake values must match the detector's own patterns."""
        aws = detect_secrets(FAKE_AWS_KEY)
        assert [f.pattern_name for f in aws] == ["aws-key"]
        gh = detect_secrets(FAKE_GITHUB_TOKEN)
        assert [f.pattern_name for f in gh] == ["github-token"]

    def test_clean_log_has_no_findings(self):
        assert detect_secrets(_clean_log()) == []


# ── Secret echo (full-original path) ──────────────────────────────────────────


class TestIssuanceSecretScan:
    def test_secret_masked_in_response_original_intact_in_store(self, manager):
        text = _secret_log(FAKE_AWS_KEY)
        compressed = manager.compress_content(text, profile="log", project="p0")
        assert compressed["cached"] is True
        h = compressed["hash"]

        result = manager.retrieve_content(h)

        assert result["found"] is True
        assert FAKE_AWS_KEY not in result["original"]
        assert "<REDACTED:aws-key>" in result["original"]
        # Zero-loss storage: the stored original is byte-identical.
        stored = manager.sqlite.ccr_get(h)
        assert stored is not None
        assert stored["original"] == text
        assert FAKE_AWS_KEY in stored["original"]

    def test_redaction_note_counts_and_patterns(self, manager):
        text = _secret_log(FAKE_AWS_KEY) + "\n" + _secret_log(FAKE_GITHUB_TOKEN)
        h = manager.compress_content(text, profile="log")["hash"]

        result = manager.retrieve_content(h)

        assert result["redactions"] == 2
        assert result["redacted_patterns"] == {"aws-key": 1, "github-token": 1}
        assert "<REDACTED:aws-key>" in result["original"]
        assert "<REDACTED:github-token>" in result["original"]

    def test_clean_content_roundtrip_unchanged(self, manager):
        text = _clean_log()
        h = manager.compress_content(text, profile="log")["hash"]

        result = manager.retrieve_content(h)

        assert result["found"] is True
        assert result["original"] == text
        assert result["redactions"] == 0
        assert "redacted_patterns" not in result

    def test_missing_hash_response_unchanged(self, manager):
        result = manager.retrieve_content("0" * 64)
        assert result["found"] is False
        assert "redactions" not in result


# ── Snippet path ──────────────────────────────────────────────────────────────


class TestSnippetScan:
    def test_snippets_masked_when_secret_in_window(self, manager):
        text = _secret_log(FAKE_AWS_KEY)
        h = manager.compress_content(text, profile="log")["hash"]
        # B5 tier-1: hit rows refuse snippet mode; the masking semantics
        # live on the legacy unscanned population.
        _as_legacy_unscanned(manager, h)

        result = manager.retrieve_content(h, query="unobtanium")

        assert result["found"] is True
        assert result["snippets"], "FTS5 must match 'unobtanium'"
        for snippet in result["snippets"]:
            assert FAKE_AWS_KEY not in snippet["snippet"]
        assert any("<REDACTED:" in s["snippet"] for s in result["snippets"])
        assert result["redactions"] >= 1

    def test_clean_snippets_carry_zero_redactions(self, manager):
        text = _clean_log()
        h = manager.compress_content(text, profile="log")["hash"]

        result = manager.retrieve_content(h, query="worker")

        assert result["found"] is True
        assert result["redactions"] == 0
        assert "redacted_patterns" not in result


# ── Refuse mode (opt-in) ──────────────────────────────────────────────────────


class TestRefuseMode:
    def test_full_original_refused(self, refuse_manager):
        text = _secret_log(FAKE_AWS_KEY)
        h = refuse_manager.compress_content(text, profile="log")["hash"]

        result = refuse_manager.retrieve_content(h)

        assert result["found"] is True
        assert result["refused"] is True
        assert "original" not in result
        assert result["redactions"] >= 1
        # Refusal redacts nothing and stores nothing new — original intact.
        stored = refuse_manager.sqlite.ccr_get(h)
        assert stored is not None
        assert stored["original"] == text

    def test_snippets_refused(self, refuse_manager):
        text = _secret_log(FAKE_AWS_KEY)
        h = refuse_manager.compress_content(text, profile="log")["hash"]
        # B5 tier-1: a hit row refuses snippets even knob-off; the KNOB's
        # own snippet branch needs the legacy unscanned population.
        _as_legacy_unscanned(refuse_manager, h)

        result = refuse_manager.retrieve_content(h, query="unobtanium")

        assert result["found"] is True
        assert result["refused"] is True
        assert "snippets" not in result

    def test_clean_content_still_issued_in_refuse_mode(self, refuse_manager):
        text = _clean_log()
        h = refuse_manager.compress_content(text, profile="log")["hash"]

        result = refuse_manager.retrieve_content(h)

        assert result["found"] is True
        assert "refused" not in result
        assert result["original"] == text


# ── Status gate (raw issuance) ────────────────────────────────────────────────


class TestStatusGate:
    def _add(self, mgr: MemoryManager, content: str, status: MemoryStatus):
        from vesmaro.models import MemoryCreate

        data = MemoryCreate(
            content=content,
            tags=["project:p0-gate", "agent:gate-agent", "mnemos:learning"],
            status=status,
        )
        return mgr.add(data, project="p0-gate", agent="gate-agent")

    def test_gate_constant_is_published_and_processed(self):
        assert set(CONTEXT_ADMISSIBLE_STATUSES) == {MemoryStatus.PUBLISHED, MemoryStatus.PROCESSED}
        assert MemoryStatus.RAW not in CONTEXT_ADMISSIBLE_STATUSES
        assert MemoryStatus.ARCHIVED not in CONTEXT_ADMISSIBLE_STATUSES

    def test_raw_not_surfaced_by_default(self, search_manager):
        self._add(search_manager, "unique xylophone notes", MemoryStatus.RAW)
        assert search_manager.search("xylophone") == []
        # Documented widening: explicit include_raw is a caller decision.
        raw_hits = search_manager.search("xylophone", include_raw=True)
        assert len(raw_hits) == 1

    def test_published_and_processed_surface_by_default(self, search_manager):
        published = self._add(search_manager, "unique quokka facts", MemoryStatus.PUBLISHED)
        processed = self._add(search_manager, "unique yacketty tang facts", MemoryStatus.PROCESSED)
        # Membership (not exact count): the deterministic mock embedder can
        # let the vector leg surface the sibling record with a tiny RRF
        # score — the gate cares about admissibility, not ranking.
        quokka_hits = search_manager.search("quokka")
        assert any(r.memory.id == published.id for r in quokka_hits)
        yack_hits = search_manager.search("yacketty")
        assert any(r.memory.id == processed.id for r in yack_hits)
        for hits in (quokka_hits, yack_hits):
            assert all(r.memory.status in CONTEXT_ADMISSIBLE_STATUSES for r in hits)
