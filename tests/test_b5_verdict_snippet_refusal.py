"""B5 tier-1 (ArchCom 2026-08-27) — verdict-gated snippet refusal.

Committee decision: FTS5 snippet windows are cut around query matches
with no offset mapping back to the original (that mapping is tier-2,
W3), so an entry whose scan-at-store verdict is ``'hit'`` cannot have
its snippets proven secret-free short of withholding them. Tier-1
therefore REFUSES snippet mode for 'hit' rows:

* no snippet is emitted (``snippets`` key absent from the refusal);
* the refusal reason is the fixed string
  ``"snippet mode unavailable for entries with detected secrets"``;
* the retrieval counter is NOT bumped (the refusal sits before
  ``_mark_issued`` — P1-b review F4 semantics);
* NULL / 'unknown' / 'clean' verdicts are unaffected;
* the caller's fallback — a full-original retrieve of the same hit row —
  stays available and is redacted span-wise by the unconditional P0
  issuance scan (zero-loss storage).

All secrets below are obviously fake EXAMPLE-style values built from
the detector's own pattern catalogue (src/mnemos/secrets_detector.py);
real credentials never appear in this file.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from vesma.config import Settings
from vesma.manager import MemoryManager

# aws-key pattern: AKIA + 16 chars of [0-9A-Z].
FAKE_AWS_KEY = "AKIAEXAMPLEABCDEFGH1"

PROJECT = "b5-proj"

REFUSAL_REASON = "snippet mode unavailable for entries with detected secrets"


def _settings(tmp: Path, **ccr_overrides: object) -> Settings:
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
        yield mgr
        mgr.close()


def _log_lines(n: int, *, secret: str = "") -> str:
    """FTS-queryable log text; ``secret`` is planted mid-stream when given."""
    lines = []
    for i in range(n):
        if secret and i == n // 2:
            lines.append(f"2026-08-27T10:00:{i:02d} ERROR token {secret} rejected")
            continue
        lines.append(f"2026-08-27T10:00:{i:02d} INFO processing batch {i} completed")
    return "\n".join(lines)


def _compress(mgr: MemoryManager, text: str) -> str:
    result = mgr.compress_content(text, profile="log", project=PROJECT)
    assert result["cached"], f"expected the original to be cached, got {result}"
    return str(result["hash"])


def _row(mgr: MemoryManager, h: str) -> dict[str, Any]:
    row = mgr.sqlite.ccr_get(h, project=PROJECT, bump=False)
    assert row is not None
    return row


# ── Tier-1 gate ───────────────────────────────────────────────────────────────


class TestVerdictGatedSnippetRefusal:
    def test_hit_row_snippet_mode_refused(self, manager: MemoryManager) -> None:
        h = _compress(manager, _log_lines(120, secret=FAKE_AWS_KEY))
        assert _row(manager, h)["secret_scan_verdict"] == "hit"

        result = manager.retrieve_content(h, query="ERROR", project=PROJECT)

        assert result["found"] is True
        assert result["refused"] is True
        assert result["reason"] == REFUSAL_REASON
        assert "snippets" not in result
        assert result["redactions"] == 0

    def test_hit_row_refusal_does_not_bump_retrieval_counter(self, manager: MemoryManager) -> None:
        h = _compress(manager, _log_lines(120, secret=FAKE_AWS_KEY))
        manager.retrieve_content(h, query="ERROR", project=PROJECT)
        assert _row(manager, h)["retrieval_count"] == 0

    def test_clean_row_snippets_still_work(self, manager: MemoryManager) -> None:
        h = _compress(manager, _log_lines(120))
        assert _row(manager, h)["secret_scan_verdict"] == "clean"

        result = manager.retrieve_content(h, query="processing", project=PROJECT)

        assert result["found"] is True
        assert not result.get("refused")
        assert isinstance(result.get("snippets"), list)
        # The gate's internal verdict datum is consumed, never echoed.
        assert "secret_scan_verdict" not in result

    def test_null_verdict_row_snippets_unaffected(self, manager: MemoryManager) -> None:
        """Legacy NULL-verdict rows (pre-P1-a caches) snippet as before."""
        h = _compress(manager, _log_lines(120))
        conn = manager.sqlite._get_conn()
        conn.execute("UPDATE ccr_cache SET secret_scan_verdict=NULL WHERE hash=?", (h,))
        conn.commit()
        assert _row(manager, h)["secret_scan_verdict"] is None

        result = manager.retrieve_content(h, query="processing", project=PROJECT)

        assert result["found"] is True
        assert not result.get("refused")
        assert isinstance(result.get("snippets"), list)

    def test_hit_row_full_original_still_redeemable_and_redacted(
        self, manager: MemoryManager
    ) -> None:
        """The caller's fallback: full-original retrieve of a hit row.

        The P0 issuance scan runs unconditionally on the full original —
        the returned copy is redacted span-wise while the stored original
        stays byte-identical (zero-loss storage).
        """
        h = _compress(manager, _log_lines(120, secret=FAKE_AWS_KEY))
        stored = _row(manager, h)["original"]
        assert FAKE_AWS_KEY in stored  # zero-loss storage precondition

        result = manager.retrieve_content(h, project=PROJECT)

        assert result["found"] is True
        assert not result.get("refused")
        assert result["redactions"] >= 1
        assert FAKE_AWS_KEY not in result["original"]
        assert "<REDACTED" in result["original"]
        # Stored original untouched.
        assert _row(manager, h)["original"] == stored
