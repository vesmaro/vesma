"""ADR-0018 P1-b — issuance-layer tests (M1 / m2 / m5, findings 1+4).

P1-b fix-track tests (issue #146, items 3-4 part-2 + Security-review
additions) for the content-echo channels:

* M1 — scan-at-issuance on the search/recall paths: MCP
  ``vesma_search`` / ``vesma_agent_recall`` / ``vesma_recall_context``
  and REST ``/search`` (incl. the ``raw_content`` swap) / ``/recall/agent``
  scan the CONTENT field of each result at the boundary — redacted copy
  issued, per-item ``redactions`` note, refuse mode drops the item;
* m2 — FTS5 snippet marker-split hardening: highlight markers
  (``>>>``/``<<<``) and the ``' ... '`` separator split multi-token
  secrets so ``detect_secrets`` misses them; snippets are scanned on a
  marker-stripped copy and a hit withholds the WHOLE snippet (offsets in
  the stripped copy do not map back to the marked text);
* m5 — a scanner exception maps to the ``refused`` shape
  (``reason="scanner error"``) instead of a raw 500 / MCP error —
  fail-closed, observable;
* finding 1+4 (CWE-668 ergonomics) — unscoped retrieval of a
  project-scoped CCR entry logs a WARNING; ``ccr.require_project_match``
  (default False) upgrades it to a denial;
* m3 follow-up — chained 3-overlap detector resolution (aws-key →
  high-entropy → jwt extends one span to max(end));
* scoped-retrieve pass-through proven at the MCP and REST level
  (P1-a regression: the ``project`` argument reaches the store lookup).

All secrets below are obviously fake EXAMPLE-style values built from the
detector's own pattern catalogue (src/vesma/secrets_detector.py); real
credentials never appear in this file.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import vesma.mcp_server as mcp_mod
from vesma.api import main as api_main
from vesma.api.main import app, lifespan
from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.mcp_server import _dispatch
from vesma.models import MemoryCreate, MemorySource, MemoryStatus
from vesma.secrets_detector import detect_secrets, redact_content
from vesma.storage.sqlite_store import (
    FTS_SNIPPET_ELLIPSIS,
    FTS_SNIPPET_END_MARK,
    FTS_SNIPPET_START_MARK,
)


def _snippet_scan_text(snippet: str) -> str:
    """Test-local copy of the m2 strip helper (removed from production by
    the B5 tier-2 offset-mapped scan, which localizes fragments instead
    of scanning a flattened copy). Keeps the fixture assertions below
    readable: highlight marks AND the ellipsis removed, like the tier-1
    detection copy was."""
    for mark in (FTS_SNIPPET_START_MARK, FTS_SNIPPET_END_MARK, FTS_SNIPPET_ELLIPSIS):
        snippet = snippet.replace(mark, "")
    return snippet


# ── Fake (EXAMPLE-style) secrets from the detector's own regexes ──────────────

# aws-key pattern: AKIA + 16 chars of [0-9A-Z] (all-caps body, entropy leg quiet).
FAKE_AWS_KEY = "AKIAEXAMPLEABCDEFGH1"
# github-token pattern: ghp_ + 36 chars of [A-Za-z0-9] (same-char body → zero
# entropy, so the high-entropy leg never fires on this value).
FAKE_GITHUB_TOKEN = "ghp_" + "f" * 36
# jwt pattern: three base64url segments, first two starting with eyJ.
FAKE_JWT = "eyJfakeHeaderContent22.eyJfakePayloadXYZ.fakesigABCDEF1234567890"

# m3 follow-up: a chained 3-overlap fixture. The contiguous 39-char alnum
# run ("AKIA"+16 caps+"eyJ"+mixed) clears the high-entropy leg (~4.88
# bits/char >= 4.8, len 39 >= 32) and starts with a valid aws-key; the
# JWT regex starts inside that run (at the eyJ) and runs past its end.
# Resolution must chain BOTH partial overlaps into ONE span covering the
# whole construct, labelled by the first-match (aws-key).
CHAIN_OVERLAP_CONTENT = (
    "AKIA" + "QZ7WJ4XEPLMRT82H" + "eyJbR9uHk2NqX5sVd7W.eyJpayloadFake123.fakesigABCDEF1234567890"
)

PROJECT = "p1b-proj"
AGENT = "p1b-agent"


# ── Fixtures ──────────────────────────────────────────────────────────────────


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
        scanner={"enabled": False},
        ccr=ccr,  # type: ignore[arg-type]
    )
    settings.resolve_paths()
    return settings


def _manager(settings: Settings) -> MemoryManager:
    mgr = MemoryManager(settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    return mgr


@pytest.fixture
def manager() -> Iterator[MemoryManager]:
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = _manager(_settings(Path(tmpdir)))
        yield mgr
        mgr.close()


@pytest.fixture
def refuse_manager() -> Iterator[MemoryManager]:
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = _manager(_settings(Path(tmpdir), retrieve_refuse_on_secret=True))
        yield mgr
        mgr.close()


@pytest.fixture
def scoped_manager() -> Iterator[MemoryManager]:
    """Manager with ccr.require_project_match=True (finding 1 deny knob)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = _manager(_settings(Path(tmpdir), require_project_match=True))
        yield mgr
        mgr.close()


@pytest.fixture
def rest_client(manager: MemoryManager) -> Iterator[TestClient]:
    """TestClient wired to the shared ``manager`` fixture (test_api pattern)."""
    api_main._manager = manager
    test_app = FastAPI(title="Mnemos-P1b-Test", version="0.1.0", lifespan=lifespan)
    for route in app.routes:
        test_app.routes.append(route)
    with TestClient(test_app) as tc:
        yield tc
    api_main._manager = None


def _add(
    mgr: MemoryManager,
    content: str,
    *,
    tags: list[str] | None = None,
    title: str | None = None,
) -> object:
    """Seed a PUBLISHED row; gate-demoted seeds are re-flipped at the store.

    ADR-0019 N1: a direct-seed publication whose content trips the
    danger detectors is demoted to RAW by ``manager.add``. The
    issuance-scan fixtures in this module pin scan behavior on exactly
    that residual population (published rows carrying a secret — legacy
    pre-gate rows / secrets introduced after publication), so a demoted
    seed is restored with the store-level status flip. Clean content
    publishes through the gate as before.
    """
    data = MemoryCreate(
        content=content,
        title=title,
        tags=tags or [f"project:{PROJECT}", f"agent:{AGENT}", "mnemos:learning"],
        source=MemorySource.MCP,
        status=MemoryStatus.PUBLISHED,
    )
    memory = mgr.add(data, project=PROJECT, agent=AGENT)
    if memory.status != MemoryStatus.PUBLISHED:
        mgr.sqlite.update_status(memory.id, MemoryStatus.PUBLISHED)
        memory.status = MemoryStatus.PUBLISHED
    return memory


def _secret_note(secret: str, marker: str = "unobtanium") -> str:
    return (
        f"Service {marker} deployment notes.\n"
        f"The {marker} service authenticates with api key {secret}\n"
        "Rotate quarterly per policy."
    )


def _first_line_secret_note(secret: str, marker: str = "unobtanium") -> str:
    """Content whose FIRST line carries the secret — auto_title() derives
    from the first line, so the title leaks unless it is scanned too."""
    return (
        f"credentials for {marker}: {secret}\n"
        f"{marker} backup rotation schedule and ownership notes.\n"
        "Rotate quarterly per policy."
    )


def _cacheable_secret_log(secret: str) -> str:
    lines = [f"2026-08-26T10:00:{i % 60:02d}Z INFO worker processing item {i}" for i in range(20)]
    lines.append(f"2026-08-26T10:01:00Z CONFIG the unobtanium service uses key {secret}")
    lines.append("2026-08-26T10:01:01Z INFO shutdown complete")
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


# ── M1: MCP vesma_search ─────────────────────────────────────────────────────


class TestMcpSearchScan:
    def test_secret_redacted_with_per_item_note(self, manager, monkeypatch):
        _add(manager, _secret_note(FAKE_AWS_KEY))
        monkeypatch.setattr(mcp_mod, "_manager", manager)

        results = asyncio.new_event_loop().run_until_complete(
            _dispatch("vesma_search", {"query": "unobtanium", "project": PROJECT})
        )

        assert isinstance(results, list) and results, "FTS must match 'unobtanium'"
        hit = results[0]
        assert FAKE_AWS_KEY not in hit["content"]
        assert "<REDACTED:aws-key>" in hit["content"]
        assert hit["redactions"] == 1
        assert hit["redacted_patterns"] == {"aws-key": 1}
        # The stored memory is never mutated (zero-loss storage).
        stored = manager.sqlite.get(hit["id"])
        assert stored is not None
        assert FAKE_AWS_KEY in stored.content

    def test_clean_result_carries_zero_redactions(self, manager, monkeypatch):
        _add(manager, _secret_note("plain-text-key-no-pattern"))
        monkeypatch.setattr(mcp_mod, "_manager", manager)

        results = asyncio.new_event_loop().run_until_complete(
            _dispatch("vesma_search", {"query": "unobtanium", "project": PROJECT})
        )

        assert results
        assert results[0]["redactions"] == 0
        assert "redacted_patterns" not in results[0]

    def test_refuse_mode_drops_the_item(self, refuse_manager, monkeypatch):
        secret_mem = _add(refuse_manager, _secret_note(FAKE_AWS_KEY))
        _add(refuse_manager, "quokka facts entirely clean note")
        monkeypatch.setattr(mcp_mod, "_manager", refuse_manager)

        results = asyncio.new_event_loop().run_until_complete(
            _dispatch("vesma_search", {"query": "unobtanium", "project": PROJECT})
        )

        # The secret-bearing item is dropped; the mock embedder makes the
        # vector leg surface the clean sibling (equidistant vectors), which
        # must survive — only the dirty item is refused.
        assert all(r["id"] != secret_mem.id for r in results)
        assert all(FAKE_AWS_KEY not in r["content"] for r in results)

    def test_refuse_mode_keeps_clean_items(self, refuse_manager, monkeypatch):
        _add(refuse_manager, "quokka facts entirely clean note")
        monkeypatch.setattr(mcp_mod, "_manager", refuse_manager)

        results = asyncio.new_event_loop().run_until_complete(
            _dispatch("vesma_search", {"query": "quokka", "project": PROJECT})
        )

        assert len(results) == 1
        assert "quokka" in results[0]["content"]


# ── M1: MCP vesma_agent_recall ───────────────────────────────────────────────


class TestMcpAgentRecallScan:
    def test_agent_recall_content_redacted(self, manager, monkeypatch):
        _add(manager, _secret_note(FAKE_GITHUB_TOKEN))
        monkeypatch.setattr(mcp_mod, "_manager", manager)

        results = asyncio.new_event_loop().run_until_complete(
            _dispatch(
                "vesma_agent_recall",
                {"agent": AGENT, "project": PROJECT, "query": "unobtanium"},
            )
        )

        assert results
        assert FAKE_GITHUB_TOKEN not in results[0]["content"]
        assert "<REDACTED:github-token>" in results[0]["content"]
        assert results[0]["redactions"] == 1
        assert results[0]["redacted_patterns"] == {"github-token": 1}


# ── M1: MCP vesma_recall_context ─────────────────────────────────────────────


class TestMcpRecallContextScan:
    def test_checkpoint_content_redacted(self, manager, monkeypatch):
        checkpoint_tags = [
            f"project:{PROJECT}",
            f"agent:{AGENT}",
            "mnemos:checkpoint",
        ]
        _add(manager, _secret_note(FAKE_AWS_KEY), tags=checkpoint_tags)
        monkeypatch.setattr(mcp_mod, "_manager", manager)

        rendered = asyncio.new_event_loop().run_until_complete(
            _dispatch("vesma_recall_context", {"project": PROJECT})
        )

        assert isinstance(rendered, str)
        assert FAKE_AWS_KEY not in rendered
        assert "<REDACTED:aws-key>" in rendered


# ── M1: REST /search (incl. raw_content swap) and /recall/agent ───────────────


class TestRestSearchScan:
    def test_search_content_redacted(self, rest_client, manager):
        _add(manager, _secret_note(FAKE_AWS_KEY))

        resp = rest_client.post("/search", json={"query": "unobtanium", "project": PROJECT})

        assert resp.status_code == 200
        results = resp.json()
        assert results
        assert FAKE_AWS_KEY not in results[0]["content"]
        assert "<REDACTED:aws-key>" in results[0]["content"]
        assert results[0]["redactions"] == 1
        assert results[0]["redacted_patterns"] == {"aws-key": 1}

    def test_raw_content_swap_is_scanned(self, rest_client, manager):
        """include_raw drills into raw_content — the swapped string is the
        one echoed, so IT must be scanned (not the clean effective content)."""
        mem = _add(manager, _secret_note("harmless-visible-key"))
        # Public store path: persist a secret-bearing raw_content while the
        # effective content stays clean (raw is the audit drill-down field).
        mem.raw_content = _secret_note(FAKE_AWS_KEY, marker="unobtanium")
        manager.sqlite.save(mem)

        resp = rest_client.post(
            "/search",
            json={"query": "unobtanium", "project": PROJECT, "include_raw": True},
        )

        assert resp.status_code == 200
        results = resp.json()
        assert results
        assert FAKE_AWS_KEY not in results[0]["content"]
        assert "<REDACTED:aws-key>" in results[0]["content"]
        assert results[0]["redactions"] == 1

    def test_recall_agent_content_redacted(self, rest_client, manager):
        _add(manager, _secret_note(FAKE_AWS_KEY))

        resp = rest_client.get(
            f"/recall/agent/{AGENT}", params={"project": PROJECT, "q": "unobtanium"}
        )

        assert resp.status_code == 200
        results = resp.json()
        assert results
        assert FAKE_AWS_KEY not in results[0]["content"]
        assert "<REDACTED:aws-key>" in results[0]["content"]
        assert results[0]["redactions"] == 1


# ── m2: snippet marker-split hardening ────────────────────────────────────────


class TestSnippetMarkerSplit:
    def test_snippet_scan_text_reconstitutes_split_secrets(self):
        # Query matched the payload token → FTS5 wraps it in highlight
        # markers, breaking the jwt regex on the raw snippet text.
        full_marked = (
            f"log {FAKE_JWT.replace('.eyJfakePayloadXYZ.', '.>>>eyJfakePayloadXYZ<<<.')} end"
        )
        assert detect_secrets(full_marked) == [], "fixture: markers must break the pattern"
        stripped = _snippet_scan_text(full_marked)
        assert FAKE_JWT in stripped
        findings = detect_secrets(stripped)
        assert [f.pattern_name for f in findings] == ["jwt"]

    def test_marker_split_jwt_snippet_redacted_via_original(self, manager):
        """Query matches the JWT payload token; FTS5 markers split the JWT
        so the raw marked snippet evades detect_secrets. B5 tier-2 (W3):
        the snippet's fragments localize in the cached original, the
        original-window scan finds the whole JWT, and the INTERSECTING
        span is redacted in the emitted snippet — span-wise redaction
        replaces the tier-1 whole-snippet withholding for localizable
        snippets (the withholding survives as the non-localizable
        fallback, covered in test_b5_tier2_snippet_scan.py)."""
        text = _cacheable_secret_log(FAKE_JWT)
        h = manager.compress_content(text, profile="log", project="m2")["hash"]
        # B5 tier-1: hit rows refuse snippet mode; the marker-split
        # semantics live on the legacy unscanned population.
        _as_legacy_unscanned(manager, h)

        result = manager.retrieve_content(h, query="eyJfakePayloadXYZ")

        assert result["found"] is True
        snippets = result["snippets"]
        assert snippets, "FTS5 must match the payload token"
        for snippet in snippets:
            body = str(snippet["snippet"])
            stripped = _snippet_scan_text(body)
            # No fragment of the token may survive in any snippet.
            for segment in FAKE_JWT.split("."):
                assert segment not in body
                assert segment not in stripped
        assert any("<REDACTED:jwt>" in s["snippet"] for s in snippets)
        assert result["redactions"] >= 1
        assert result["redacted_patterns"] == {"jwt": 1}


# ── m3 follow-up: chained 3-overlap detector resolution ───────────────────────


class TestChainedOverlap:
    _AWS_RE = re.compile(r"AKIA[0-9A-Z]{16}")
    _JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
    _SPAN_RE = re.compile(r"[A-Za-z0-9+/=_-]{32,}")

    def test_fixture_legs_fire(self):
        """The chain fixture really is three overlapping findings before
        resolution (aws-key, high-entropy, jwt)."""
        raw: list[tuple[str, int, int]] = [
            ("aws-key", *self._AWS_RE.search(CHAIN_OVERLAP_CONTENT).span())
        ]
        entropy = self._SPAN_RE.search(CHAIN_OVERLAP_CONTENT)
        assert entropy is not None
        raw.append(("high-entropy", *entropy.span()))
        raw.append(("jwt", *self._JWT_RE.search(CHAIN_OVERLAP_CONTENT).span()))
        assert len(raw) == 3
        # aws-key and high-entropy share start 0 (tie → declaration order);
        # the jwt starts inside the entropy span and ends past it — two
        # chained partial overlaps.
        starts = [f[1] for f in raw]
        assert starts.count(0) == 2

    def test_chain_collapses_into_one_max_end_span(self):
        findings = detect_secrets(CHAIN_OVERLAP_CONTENT)
        assert len(findings) == 1
        only = findings[0]
        assert only.pattern_name == "aws-key"  # first-match precedence label
        assert only.start == 0
        # The span must cover the ENTIRE construct including the jwt tail.
        assert only.end == len(CHAIN_OVERLAP_CONTENT)
        assert only.matched_value == CHAIN_OVERLAP_CONTENT

    def test_redaction_leaves_no_fragment(self):
        findings = detect_secrets(CHAIN_OVERLAP_CONTENT)
        redacted = redact_content(CHAIN_OVERLAP_CONTENT, findings)
        assert redacted == "<REDACTED:aws-key>"
        for segment in CHAIN_OVERLAP_CONTENT.split("."):
            assert segment not in redacted


# ── m5: scanner exception → refused shape ─────────────────────────────────────


class TestScannerExceptionRefusedShape:
    def _break_detector(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(content: str) -> list[object]:
            raise RuntimeError("simulated detector crash")

        monkeypatch.setattr("vesma.secrets_detector.detect_secrets", _raise)

    def test_full_original_returns_refused_scanner_error(self, manager, monkeypatch):
        text = _cacheable_secret_log(FAKE_AWS_KEY)
        h = manager.compress_content(text, profile="log")["hash"]
        self._break_detector(monkeypatch)

        result = manager.retrieve_content(h)

        assert result["found"] is True
        assert result["refused"] is True
        assert result["reason"] == "scanner error"
        assert "original" not in result
        assert result["redactions"] == 0

    def test_snippet_path_returns_refused_scanner_error(self, manager, monkeypatch):
        text = _cacheable_secret_log(FAKE_AWS_KEY)
        h = manager.compress_content(text, profile="log")["hash"]
        # B5 tier-1: a hit row would refuse at the verdict gate BEFORE the
        # scanner runs; the m5 scanner-error shape is only reachable on the
        # legacy unscanned population.
        _as_legacy_unscanned(manager, h)
        self._break_detector(monkeypatch)

        result = manager.retrieve_content(h, query="unobtanium")

        assert result["found"] is True
        assert result["refused"] is True
        assert result["reason"] == "scanner error"
        assert "snippets" not in result

    def test_scan_issuance_helper_refuses_on_scanner_error(self, manager, monkeypatch):
        self._break_detector(monkeypatch)

        scan = manager.scan_issuance("anything", context="test")

        assert scan.refused is True
        assert scan.reason == "scanner error"
        assert scan.text == ""
        assert scan.redactions == 0

    def test_mcp_search_survives_scanner_error_fail_closed(self, manager, monkeypatch):
        """List-returning channels degrade to dropping the item (no 500,
        no MCP error string) — the unscanned string never leaves."""
        _add(manager, _secret_note(FAKE_AWS_KEY))
        monkeypatch.setattr(mcp_mod, "_manager", manager)
        self._break_detector(monkeypatch)

        results = asyncio.new_event_loop().run_until_complete(
            _dispatch("vesma_search", {"query": "unobtanium", "project": PROJECT})
        )

        assert results == []

    def test_scan_issuance_clean_passthrough(self, manager):
        scan = manager.scan_issuance("clean text", context="test")
        assert scan.text == "clean text"
        assert scan.refused is False
        assert scan.reason is None
        assert scan.redactions == 0


# ── Findings 1+4 (CWE-668): unscoped retrieval of scoped rows ────────────────


class TestProjectScopeErgonomics:
    def test_unscoped_retrieve_of_scoped_entry_warns_but_issues(self, manager, caplog):
        text = _cacheable_secret_log("plain-value-no-pattern")
        h = manager.compress_content(text, profile="log", project="alpha")["hash"]

        with caplog.at_level("WARNING", logger="vesma.manager"):
            result = manager.retrieve_content(h)

        assert result["found"] is True
        assert "original" in result  # legacy behavior preserved
        assert any(
            "Unscoped CCR retrieval of project-scoped entry" in r.message for r in caplog.records
        )

    def test_require_project_match_denies_unscoped_retrieval(self, scoped_manager):
        text = _cacheable_secret_log("plain-value-no-pattern")
        h = scoped_manager.compress_content(text, profile="log", project="alpha")["hash"]

        result = scoped_manager.retrieve_content(h)  # no project passed

        assert result["found"] is True
        assert result["refused"] is True
        assert "project" in result["reason"]
        assert "original" not in result

    def test_require_project_match_allows_matching_scope(self, scoped_manager):
        text = _cacheable_secret_log("plain-value-no-pattern")
        h = scoped_manager.compress_content(text, profile="log", project="alpha")["hash"]

        result = scoped_manager.retrieve_content(h, project="alpha")

        assert result["found"] is True
        assert "refused" not in result
        assert result["original"] == text

    def test_unscoped_entry_never_warns(self, manager, caplog):
        text = _cacheable_secret_log("plain-value-no-pattern")
        h = manager.compress_content(text, profile="log")["hash"]

        with caplog.at_level("WARNING", logger="vesma.manager"):
            result = manager.retrieve_content(h)

        assert result["found"] is True
        assert not any("Unscoped CCR retrieval" in r.message for r in caplog.records)


# ── P1-a regression: project pass-through at MCP/REST level ──────────────────


class TestScopedRetrievePassThrough:
    def test_mcp_retrieve_project_scoping(self, manager, monkeypatch):
        text = _cacheable_secret_log("plain-value-no-pattern")
        h = manager.compress_content(text, profile="log", project="alpha")["hash"]
        monkeypatch.setattr(mcp_mod, "_manager", manager)

        wrong = asyncio.new_event_loop().run_until_complete(
            _dispatch("vesma_retrieve", {"hash": h, "project": "beta"})
        )
        right = asyncio.new_event_loop().run_until_complete(
            _dispatch("vesma_retrieve", {"hash": h, "project": "alpha"})
        )

        assert wrong["found"] is False
        assert right["found"] is True
        assert right["original"] == text

    def test_rest_retrieve_project_scoping(self, rest_client, manager):
        text = _cacheable_secret_log("plain-value-no-pattern")
        h = manager.compress_content(text, profile="log", project="alpha")["hash"]

        wrong = rest_client.post("/retrieve", json={"hash": h, "project": "beta"})
        right = rest_client.post("/retrieve", json={"hash": h, "project": "alpha"})

        assert wrong.status_code == 200 and wrong.json()["found"] is False
        assert right.status_code == 200 and right.json()["found"] is True


# ── Shape sanity for the helper contract ──────────────────────────────────────


class TestIssuanceScanShape:
    def test_refuse_mode_outcome_never_carries_content(self, refuse_manager):
        scan = refuse_manager.scan_issuance(_secret_note(FAKE_AWS_KEY), context="test")

        assert scan.refused is True
        assert scan.text == ""
        assert scan.reason == "secret detected"
        assert scan.redactions == 1
        assert scan.redacted_patterns == {"aws-key": 1}


# ── Review round F1: title channel (auto_title bypass) ───────────────────────


class TestReviewF1TitleScan:
    def test_first_line_secret_title_redacted_mcp_search(self, manager, monkeypatch):
        mem = _add(manager, _first_line_secret_note(FAKE_AWS_KEY))
        monkeypatch.setattr(mcp_mod, "_manager", manager)

        results = asyncio.new_event_loop().run_until_complete(
            _dispatch("vesma_search", {"query": "unobtanium", "project": PROJECT})
        )

        assert results, "FTS must match 'unobtanium'"
        hit = results[0]
        # Title derives from the secret-bearing first line — must be redacted.
        assert FAKE_AWS_KEY not in hit["title"]
        assert "<REDACTED:aws-key>" in hit["title"]
        # Content redacted in the same response.
        assert FAKE_AWS_KEY not in hit["content"]
        assert "<REDACTED:aws-key>" in hit["content"]
        # Merged per-item note: one finding in the title + one in content.
        assert hit["redactions"] == 2
        assert hit["redacted_patterns"] == {"aws-key": 2}
        # Zero-loss storage.
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert FAKE_AWS_KEY in stored.content

    def test_explicit_title_secret_redacted_rest_search(self, rest_client, manager):
        _add(
            manager,
            "unobtanium deployment runbook body without secrets",
            title=f"backup key {FAKE_GITHUB_TOKEN}",
        )

        resp = rest_client.post("/search", json={"query": "unobtanium", "project": PROJECT})

        assert resp.status_code == 200
        results = resp.json()
        assert results
        hit = results[0]
        # auto_title() echoes an explicitly-set title — scanned too.
        assert FAKE_GITHUB_TOKEN not in hit["title"]
        assert "<REDACTED:github-token>" in hit["title"]
        assert hit["redactions"] == 1  # title only; content is clean
        assert hit["redacted_patterns"] == {"github-token": 1}

    def test_list_recent_title_redacted(self, manager, monkeypatch):
        _add(manager, _first_line_secret_note(FAKE_AWS_KEY))
        monkeypatch.setattr(mcp_mod, "_manager", manager)

        listed = asyncio.new_event_loop().run_until_complete(
            _dispatch("vesma_list_recent", {"project": PROJECT})
        )

        assert listed
        for item in listed:
            assert FAKE_AWS_KEY not in item["title"]
        titled = next(i for i in listed if "<REDACTED:" in i["title"])
        assert titled["redactions"] >= 1
        assert "redacted_patterns" in titled

    def test_refuse_mode_drops_title_only_secret(self, refuse_manager, monkeypatch):
        # Content clean, explicit title dirty — the ITEM must be dropped.
        _add(
            refuse_manager,
            "unobtanium clean body text",
            title=f"leaked {FAKE_GITHUB_TOKEN}",
        )
        _add(refuse_manager, "quokka facts entirely clean note")
        monkeypatch.setattr(mcp_mod, "_manager", refuse_manager)

        results = asyncio.new_event_loop().run_until_complete(
            _dispatch("vesma_search", {"query": "unobtanium", "project": PROJECT})
        )

        assert all(FAKE_GITHUB_TOKEN not in r["title"] for r in results)
        assert all(FAKE_GITHUB_TOKEN not in r["content"] for r in results)

    def test_scan_issuance_item_merges_and_refuses(self, manager, refuse_manager):
        clean = manager.scan_issuance_item(
            _secret_note(FAKE_AWS_KEY),
            title=f"key {FAKE_AWS_KEY}",
            context="test",
        )
        assert clean.refused is False
        assert clean.redactions == 2
        assert clean.redacted_patterns == {"aws-key": 2}

        dirty = refuse_manager.scan_issuance_item(
            "clean body", title=f"key {FAKE_AWS_KEY}", context="test"
        )
        assert dirty.refused is True
        assert dirty.title == ""
        assert dirty.content == ""  # refused items carry NOTHING
        assert dirty.redactions == 1


# ── Review round F2a: /context/recall channel symmetry ───────────────────────


class TestReviewF2aContextRecall:
    def _checkpoint(self, mgr: MemoryManager, content: str, title: str | None = None):
        return _add(
            mgr,
            content,
            tags=[f"project:{PROJECT}", f"agent:{AGENT}", "mnemos:checkpoint"],
            title=title,
        )

    def test_context_recall_scans_content_and_title(self, rest_client, manager):
        self._checkpoint(
            manager,
            _first_line_secret_note(FAKE_AWS_KEY),
        )

        resp = rest_client.post("/context/recall", json={"project": PROJECT})

        assert resp.status_code == 200
        checkpoints = resp.json()["checkpoints"]
        assert checkpoints
        first = checkpoints[0]
        assert FAKE_AWS_KEY not in first["content"]
        assert FAKE_AWS_KEY not in first["title"]
        assert "<REDACTED:aws-key>" in first["content"]
        assert "<REDACTED:aws-key>" in first["title"]
        assert first["redactions"] == 2

    def test_context_recall_clean_checkpoints_unchanged(self, rest_client, manager):
        self._checkpoint(manager, "## Goals\nship the release checklist")

        resp = rest_client.post("/context/recall", json={"project": PROJECT})

        assert resp.status_code == 200
        first = resp.json()["checkpoints"][0]
        assert "release checklist" in first["content"]
        assert first["redactions"] == 0
        assert "redacted_patterns" not in first


# ── Review round F3: drop-log forensics (item id in the context label) ───────


class TestReviewF3DropForensics:
    def test_refuse_warning_carries_memory_id(self, refuse_manager, monkeypatch, caplog):
        mem = _add(refuse_manager, _secret_note(FAKE_AWS_KEY))
        monkeypatch.setattr(mcp_mod, "_manager", refuse_manager)

        with caplog.at_level("WARNING", logger="vesma.manager"):
            asyncio.new_event_loop().run_until_complete(
                _dispatch("vesma_search", {"query": "unobtanium", "project": PROJECT})
            )

        refusal_logs = [r for r in caplog.records if "Issuance refused" in r.message]
        assert refusal_logs, "the drop must be WARNING-logged"
        assert any(f"mcp:vesma_search:{mem.id}" in r.message for r in refusal_logs)


# ── Review round F4: retrieval counter bump ordering ─────────────────────────


class TestReviewF4BumpOrdering:
    def _count(self, mgr: MemoryManager, h: str) -> int:
        entry = mgr.sqlite.ccr_get(h, bump=False)
        assert entry is not None
        return int(entry["retrieval_count"])

    def test_refused_issuance_does_not_bump(self, refuse_manager):
        text = _cacheable_secret_log(FAKE_AWS_KEY)
        h = refuse_manager.compress_content(text, profile="log")["hash"]
        assert self._count(refuse_manager, h) == 0

        result = refuse_manager.retrieve_content(h)

        assert result["refused"] is True
        assert self._count(refuse_manager, h) == 0, "refusal must not LRU-pin"

    def test_denied_unscoped_retrieval_does_not_bump(self, scoped_manager):
        text = _cacheable_secret_log("plain-value-no-pattern")
        h = scoped_manager.compress_content(text, profile="log", project="alpha")["hash"]

        result = scoped_manager.retrieve_content(h)  # require_project_match deny

        assert result["refused"] is True
        assert self._count(scoped_manager, h) == 0

    def test_scanner_error_issuance_does_not_bump(self, manager, monkeypatch):
        text = _cacheable_secret_log(FAKE_AWS_KEY)
        h = manager.compress_content(text, profile="log")["hash"]

        def _raise(content: str) -> list[object]:
            raise RuntimeError("simulated detector crash")

        monkeypatch.setattr("vesma.secrets_detector.detect_secrets", _raise)
        result = manager.retrieve_content(h)

        assert result["refused"] is True
        assert result["reason"] == "scanner error"
        assert self._count(manager, h) == 0

    def test_successful_issuance_bumps_exactly_once(self, manager):
        text = _cacheable_secret_log("plain-value-no-pattern")
        h = manager.compress_content(text, profile="log")["hash"]

        first = manager.retrieve_content(h)

        assert first["found"] is True
        assert first["retrieval_count"] == 1, "response reflects the post-bump count"
        assert self._count(manager, h) == 1
        manager.retrieve_content(h)
        assert self._count(manager, h) == 2
