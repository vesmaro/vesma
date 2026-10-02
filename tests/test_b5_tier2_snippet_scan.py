"""B5 tier-2 (ArchCom 2026-08-27, W3) — offset-mapped snippet scan.

Tier-1 (landed, tests/test_b5_verdict_snippet_refusal.py) refuses
snippet mode outright for rows whose scan-at-store verdict is ``'hit'``.
Tier-2 covers the remaining population (NULL / 'unknown' / 'clean'):
the snippet's ellipsis-split fragments are localized in the cached
original, the original is scanned over the localized window ±
``SNIPPET_SCAN_MARGIN_CHARS``, and findings INTERSECTING the window are
redacted span-wise in the emitted snippet.

Acceptance (mnemos #125 Wave 3):

* a secret split by the 32-token FTS5 window boundary (a JWT whose
  segment tokens straddle the window edge — the visible fragment evades
  ``detect_secrets`` on the snippet text alone) is redacted via the
  original-scan mapping;
* a snippet that cannot be UNIQUELY localized in the original falls
  back to the tier-1 behavior — the whole snippet is withheld
  (``<REDACTED:snippet>``), fail-closed;
* the clean path is unchanged — localizable, secret-free snippets are
  emitted verbatim (highlight marks intact) with zero redactions.

All secrets below are obviously fake EXAMPLE-style values built from
the detector's own pattern catalogue (src/mnemos/secrets_detector.py);
real credentials never appear in this file.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.secrets_detector import detect_secrets
from vesma.storage.sqlite_store import (
    FTS_SNIPPET_END_MARK,
    FTS_SNIPPET_START_MARK,
)

# jwt pattern: three base64url segments joined by dots, starting eyJ.
FAKE_JWT = "eyJfakeHeaderContent22.eyJfakePayloadXYZ.fakesigABCDEF1234567890"

PROJECT = "b5t2-proj"


def _settings(tmp: Path, **ccr: object) -> Settings:
    settings = Settings(
        vesma={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
        },
        ccr={
            "min_size_chars": 100,
            "max_entries": 100,
            "ttl_days": 1,
            **ccr,
        },  # type: ignore[arg-type]
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def manager() -> Iterator[MemoryManager]:
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = MemoryManager(_settings(Path(tmpdir)))
        yield mgr
        mgr.close()


@pytest.fixture
def refuse_manager() -> Iterator[MemoryManager]:
    """Refuse-mode deployment (ccr.retrieve_refuse_on_secret=True)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = MemoryManager(_settings(Path(tmpdir), retrieve_refuse_on_secret=True))
        yield mgr
        mgr.close()


def _strip_marks(snippet: str) -> str:
    for mark in (FTS_SNIPPET_START_MARK, FTS_SNIPPET_END_MARK):
        snippet = snippet.replace(mark, "")
    return snippet


def _as_legacy_unscanned(mgr: MemoryManager, h: str) -> None:
    """Rewrite a row's scan verdict to NULL (pre-P1-a legacy cache row).

    Tier-2 only runs on NULL / 'unknown' / 'clean' rows — a 'hit' row is
    refused at the tier-1 verdict gate before the offset-mapped scan.
    """
    conn = mgr.sqlite._get_conn()
    conn.execute("UPDATE ccr_cache SET secret_scan_verdict=NULL WHERE hash=?", (h,))
    conn.commit()


class TestWindowTruncationFragmentRedacted:
    def test_jwt_cut_by_window_edge_redacted_via_original(self, manager):
        """The query match sits ~30 tokens before the JWT; the 32-token
        FTS5 snippet window ends INSIDE the JWT (its segments are
        separate unicode61 tokens), so the snippet shows only leading
        segments — a fragment that evades the jwt pattern on the snippet
        text. The original-window scan finds the full JWT; the visible
        intersection must be redacted."""
        # 21 filler tokens: the 32-token FTS5 window opened at the query
        # match ends INSIDE the JWT — the first two segments are visible,
        # the third is cut off by the window edge (probed empirically:
        # 20 fillers swallow the whole JWT, 22+ leave only one segment).
        filler = " ".join(f"filler{i:03d}" for i in range(21))
        text = (
            "deploy run quokka-window boundary probe start\n"
            f"{filler}\n"
            f"service token {FAKE_JWT} rotate monthly\n" + "trailing payload " * 40
        )
        h = manager.compress_content(text, profile="log", project=PROJECT)["hash"]
        _as_legacy_unscanned(manager, h)

        result = manager.retrieve_content(h, query="quokka-window", project=PROJECT)

        assert result["found"] is True
        snippets = result["snippets"]
        assert snippets, "FTS5 must match the query token"
        # Precondition: at least one raw snippet shows a TRUNCATED jwt —
        # some (not all) segments present, the full value absent, and
        # detect_secrets on the stripped snippet text finds NOTHING.
        raw = manager.sqlite.ccr_search(h, "quokka-window", limit=5, project=PROJECT)
        truncated = [
            s
            for s in raw
            if any(seg in _strip_marks(str(s["snippet"])) for seg in FAKE_JWT.split("."))
            and FAKE_JWT not in _strip_marks(str(s["snippet"]))
        ]
        assert truncated, "fixture must produce a window-truncated jwt fragment"
        assert all(detect_secrets(_strip_marks(str(s["snippet"]))) == [] for s in truncated), (
            "the truncated fragment must evade snippet-text detection"
        )

        # Postcondition: the visible fragment is redacted via the
        # original-window mapping; no jwt segment survives anywhere.
        for snippet in snippets:
            body = str(snippet["snippet"])
            for segment in FAKE_JWT.split("."):
                assert segment not in body
        assert any("<REDACTED:jwt>" in str(s["snippet"]) for s in snippets)
        assert result["redactions"] >= 1
        assert result["redacted_patterns"].get("jwt") == 1


class TestNonLocalizableFallback:
    def test_repeated_fragment_snippet_withheld(self, manager):
        """The original is a wall of IDENTICAL lines carrying the query
        token; every snippet window is a contiguous run of that line,
        and the run's text occurs at EVERY line boundary — the fragment
        cannot be uniquely localized — tier-1 fallback: the whole
        snippet is withheld (``<REDACTED:snippet>``, counted under the
        placeholder's own pattern name), fail-closed."""
        line = "worker quokka-repeat processing batch item completed status ok"
        text = "\n".join([line] * 80)
        h = manager.compress_content(text, profile="log", project=PROJECT)["hash"]

        result = manager.retrieve_content(h, query="quokka-repeat", project=PROJECT)

        assert result["found"] is True
        snippets = result["snippets"]
        assert snippets, "FTS5 must match the query token"
        for snippet in snippets:
            assert snippet["snippet"] == "<REDACTED:snippet>"
        assert result["redactions"] >= 1
        assert result["redacted_patterns"] == {"snippet": len(snippets)}


class TestRefuseModeReasons:
    """W3 review F5: refuse-mode forensics name the TRUE cause — a
    localization failure (nothing detected) is not a detection."""

    def test_localization_failure_reason_is_distinct(self, refuse_manager: MemoryManager) -> None:
        """Non-localizable snippet (repeated text) under refuse mode: the
        refusal must NOT claim a detection — the fixed distinct reason
        names the ambiguity; still fail-closed (no snippets, no bump)."""
        line = "worker quokka-repeat processing batch item completed status ok"
        text = "\n".join([line] * 80)
        h = refuse_manager.compress_content(text, profile="log", project=PROJECT)["hash"]
        before = refuse_manager.sqlite.ccr_get(h, project=PROJECT, bump=False)["retrieval_count"]

        result = refuse_manager.retrieve_content(h, query="quokka-repeat", project=PROJECT)

        assert result["found"] is True
        assert result["refused"] is True
        assert result["reason"] == (
            "snippet localization failed (ambiguous) — refusing under refuse-mode"
        )
        assert result["redacted_patterns"] == {"snippet": result["redactions"]}
        assert "snippets" not in result
        after = refuse_manager.sqlite.ccr_get(h, project=PROJECT, bump=False)["retrieval_count"]
        assert after == before, "refusal must not bump the retrieval counter"

    def test_real_detection_keeps_detection_reason(self, refuse_manager: MemoryManager) -> None:
        """A genuine intersecting finding under refuse mode keeps the
        detection reason — the F5 split never downgrades a real hit."""
        filler = " ".join(f"filler{i:03d}" for i in range(21))
        text = (
            "deploy run quokka-window boundary probe start\n"
            f"{filler}\n"
            f"service token {FAKE_JWT} rotate monthly\n" + "trailing payload " * 40
        )
        h = refuse_manager.compress_content(text, profile="log", project=PROJECT)["hash"]
        _as_legacy_unscanned(refuse_manager, h)

        result = refuse_manager.retrieve_content(h, query="quokka-window", project=PROJECT)

        assert result["found"] is True
        assert result["refused"] is True
        assert result["reason"] == "secret detected in retrieved snippets"
        assert result["redacted_patterns"].get("jwt") == 1


class TestCleanPathUnchanged:
    def test_clean_localizable_snippet_issued_verbatim(self, manager):
        """Unique, secret-free content: the snippet is emitted EXACTLY as
        FTS5 produced it (highlight marks intact), zero redactions, and
        the response carries no redacted_patterns key."""
        text = "incident quokka-clean postmortem start\n" + "\n".join(
            f"2026-08-27T11:{i:02d}:00Z INFO unique step {i} of rollout finished" for i in range(40)
        )
        h = manager.compress_content(text, profile="log", project=PROJECT)["hash"]

        result = manager.retrieve_content(h, query="quokka-clean", project=PROJECT)

        assert result["found"] is True
        snippets = result["snippets"]
        assert snippets, "FTS5 must match the query token"
        raw = manager.sqlite.ccr_search(h, "quokka-clean", limit=5, project=PROJECT)
        raw_bodies = [str(s["snippet"]) for s in raw]
        for snippet in snippets:
            assert snippet["snippet"] in raw_bodies, "clean snippets must be verbatim"
        assert result["redactions"] == 0
        assert "redacted_patterns" not in result

    def test_internal_original_never_echoed(self, manager):
        """The tier-2 scan datum (the cached original threaded through
        ccr.retrieve's snippet mode) is popped by the issuance layer —
        it never crosses the boundary into the response."""
        text = "baseline quokka-echo probe start\n" + "\n".join(
            f"row {i} unique content {i}" for i in range(40)
        )
        h = manager.compress_content(text, profile="log", project=PROJECT)["hash"]

        result = manager.retrieve_content(h, query="quokka-echo", project=PROJECT)

        assert result["found"] is True
        assert "original" not in result
