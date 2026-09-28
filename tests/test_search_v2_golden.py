"""Search v2 golden-query regression suite (issue #313).

Every case below mirrors a LIVE PROBE against the production DB — the
proven defects, re-derived as semantic assertions against small
fixtures (shapes only; no production data copied). If any case fails,
a v2 defect has regressed:

  1. [CRITICAL] multi-token AND — the M15.2 whole-input phrase made
     'GWS конвейер' need ADJACENT words in order (0 hits live; the
     per-token AND found 4).
  2. [HIGH] morphology/prefix — inflected RU/EN forms were invisible
     ('конвейер' 49 vs 'конвейер*' 77 live).
  3. [MEDIUM] embedding_id backfill/write path (covered in
     test_embedding_id_backfill.py — referenced here for the index).
  4. [MEDIUM] project drift — scoped searches silently zeroed when the
     slug drifted; the soft fallback surfaces them TAGGED.
  5. [MEDIUM] no graph leg — memory_edges (supersedes) unused by
     search; the leg now surfaces the superseded sibling.
  6. Injection safety — the M15.2 hardening holds under the v2 builder
     (every MATCH expression valid; no operator syntax from user
     text).
  7. No single-token regression — single-token searches behave exactly
     as before (a prefix term is a superset of the old exact phrase).
  8. Degenerate short tokens (issue #314) — 1-char tokens and RU/EN
     stopwords are dropped before the AND join (they match nearly every
     row and collapse bm25 idf to 0); acronyms (isupper) and digit
     identifiers survive; an all-degenerate query keeps its tokens.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vesmaro.config import Settings
from vesmaro.manager import MemoryManager
from vesmaro.models import MemoryCreate, MemorySource, MemoryStatus
from vesmaro.storage.sqlite_store import (
    SQLiteStore,
    fts_join_or,
    fts_query_terms,
    fts_query_v2,
)

PROJECT_A = "release-pipeline"
PROJECT_B = "releases-pipeline"
AGENT = "golden-agent"

# Fixture corpus — mirrors the SHAPES of the live probes (multi-token
# far-apart words, hyphenated identifiers, inflected RU forms, a
# superseded pair, a drifted-scope row). No production data.
CORPUS_A = "GWS gateway: настройка конвейера поставки для релизного контура выполнена по регламенту"
CORPUS_B = "конвейер сборки релиза крутится стабильно каждую неделю"
CORPUS_HYPHEN = "release-trigger configuration notes for the deploy pipeline"
CORPUS_HYPHEN_PROSE = "the release trigger fires before the deploy window"


def _settings(tmp: Path) -> Settings:
    # Leg-isolation goldens: the vector leg is wiped per-test, and the
    # graph legs (auto-mint + walk, default-on since the owner decision
    # of 2026-09-28) are pinned OFF — each golden pins ONE leg's
    # semantics; the graph surface has its own suites.
    settings = Settings(
        mnemos={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
            "graph_auto_mint": False,
            "graph_walk": False,
        },
        scanner={"enabled": False},
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def manager() -> MemoryManager:
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = MemoryManager(_settings(Path(tmpdir)))
        mock_embedder = MagicMock()
        mock_embedder.embed.return_value = [0.1] * 384
        mgr._embedder = mock_embedder
        yield mgr
        mgr.close()


def _add(
    mgr: MemoryManager,
    content: str,
    *,
    project: str = PROJECT_A,
    status: MemoryStatus = MemoryStatus.PUBLISHED,
) -> object:
    data = MemoryCreate(
        content=content,
        tags=[f"project:{project}", f"agent:{AGENT}", "mnemos:test"],
        source=MemorySource.MCP,
        status=status,
    )
    return mgr.add(data, project=project, agent=AGENT)


# ── 1. Multi-token AND: words far apart, no adjacency ───────────────────────


class TestGoldenMultiTokenAnd:
    def test_far_apart_words_match(self, manager) -> None:
        _add(manager, CORPUS_A)  # 'GWS' and 'конвейер' far apart
        _add(manager, "unrelated prose about windmills and dikes")
        results = manager.search("GWS конвейер", limit=5)
        ids = {r.memory.id for r in results}
        assert ids, "multi-token AND must match far-apart words (live defect: 0 hits)"

    def test_and_beats_the_old_phrase_semantics(self) -> None:
        """The OLD builder's output must NOT match; the v2 output must."""
        con = sqlite3.connect(":memory:")
        con.execute('CREATE VIRTUAL TABLE t USING fts5(c, tokenize="unicode61")')
        con.execute("INSERT INTO t VALUES (?)", (CORPUS_A,))
        old_style = '"GWS конвейер"'  # M15.2 whole-input phrase
        assert con.execute("SELECT * FROM t WHERE t MATCH ?", (old_style,)).fetchall() == []
        v2 = fts_query_v2("GWS конвейер")
        assert con.execute("SELECT * FROM t WHERE t MATCH ?", (v2,)).fetchall()

    def test_term_cap_truncates_runaway_conjunctions(self) -> None:
        assert len(fts_query_terms("a b c d e f g h i j k")) == 8

    def test_or_fallback_ranked_wider_recall(self, manager) -> None:
        """AND zero + OR nonzero → the OR join surfaces partial matches."""
        _add(manager, "alpha runbook: the gamma protocol")
        _add(manager, "beta runbook: nothing relevant here")
        # 'alpha beta' never co-occur → AND is 0; OR matches both.
        results = manager.search("alpha beta", limit=5)
        assert results, "OR fallback must rescue the AND dead-end"


# ── 2. Morphology: prefix terms find inflected forms ───────────────────────


class TestGoldenMorphology:
    def test_inflected_ru_form_found_via_prefix(self, manager) -> None:
        """'конвейер' (the live probe token) finds 'конвейера' — the
        inflected form was invisible pre-v2 (49 vs 77 rows live)."""
        _add(manager, CORPUS_A)  # contains 'конвейера' (inflected)
        results = manager.search("конвейер", limit=5)
        assert results, "prefix term must match the inflected form"

    def test_inflected_en_form_found_via_prefix(self, manager) -> None:
        _add(manager, "the deployment pipeline runs every pipeline stage")
        assert manager.search("pipelin", limit=5)

    def test_builder_emits_prefix_star(self) -> None:
        assert fts_query_v2("конвейер") == '"конвейер"*'


# ── Hyphenated identifiers ───────────────────────────────────────────────────


class TestGoldenHyphenatedIdentifiers:
    def test_both_spellings_match(self, manager) -> None:
        """The identifier form AND the de-hyphenated prose form both hit:
        unicode61 splits on hyphens, so the bare quoted identifier can
        never match the split index — the OR-alternative covers it."""
        hyphen = _add(manager, CORPUS_HYPHEN)  # contains 'release-trigger'
        prose = _add(manager, CORPUS_HYPHEN_PROSE)  # contains 'release trigger'
        for row in (hyphen, prose):
            results = manager.search("release-trigger", limit=5)
            ids = {r.memory.id for r in results}
            assert row.id in ids, f"hyphenated query must find id {row.id[:8]}"

    def test_builder_emits_or_alternative(self) -> None:
        assert fts_query_v2("release-trigger") == '("release-trigger"* OR "release"*)'

    def test_long_hyphenated_identifier(self, manager) -> None:
        """The live-probe shape: 'gcw-git-workflow-specialist' never matched
        one token; the head-segment alternative fixes it."""
        row = _add(manager, "guide for the gcw git workflow specialist role")
        results = manager.search("gcw-git-workflow-specialist", limit=5)
        assert row.id in {r.memory.id for r in results}


# ── 4. Project drift → soft fallback ────────────────────────────────────────


class TestGoldenProjectDrift:
    def test_drifted_slug_surfaces_tagged(self, manager) -> None:
        _add(manager, "deploy pipeline notes live under the sibling slug", project=PROJECT_B)
        results = manager.search("deploy pipeline notes", project=PROJECT_A, limit=5)
        assert results
        assert all(r.project_scope_fallback for r in results)
        assert all(r.memory.project == PROJECT_B for r in results)

    def test_stats_expose_the_drift_counter(self, manager) -> None:
        _add(manager, "drift counter probe row", project=PROJECT_B)
        manager.search("drift counter probe", project=PROJECT_A, limit=5)
        assert manager.search_stats()["project_scope_fallback_total"] == 1


# ── 5. Graph leg ────────────────────────────────────────────────────────────


class TestGoldenGraphLeg:
    def test_superseded_sibling_surfaces_with_provenance(self, manager) -> None:
        new = _add(manager, "v2 of the onboarding doc: the canonical checklist")
        old = _add(manager, "the prior checklist revision, replaced")
        manager.add_memory_edge(new.id, old.id, kind="supersedes")
        manager.vectors.wipe()  # pin the leg: sibling reachable ONLY via the edge
        results = manager.search("onboarding checklist", limit=5)
        by_id = {r.memory.id: r for r in results}
        assert old.id in by_id
        assert by_id[old.id].via_graph is True
        assert by_id[new.id].via_graph is False


# ── 6. Injection safety (M15.2 hardening preserved under v2) ────────────────


class TestGoldenInjectionSafety:
    @pytest.mark.parametrize(
        "hostile",
        [
            '" OR col:"content',
            '"; DROP TABLE memories; --',
            "anything* NEAR whatever",
            "(hack)",
            "tag:admin",
            '***"""((()))***',
            "content:x AND title:y",
        ],
    )
    def test_builder_output_is_always_safe(self, hostile: str) -> None:
        expr = fts_query_v2(hostile)
        con = sqlite3.connect(":memory:")
        con.execute('CREATE VIRTUAL TABLE t USING fts5(c, tokenize="unicode61")')
        con.execute("INSERT INTO t VALUES ('hello world')")
        con.execute("SELECT * FROM t WHERE t MATCH ?", (expr,)).fetchall()  # never raises
        # No un-quoted user text: every emitted AND/OR term carries the
        # builder's own quoting.
        for term in fts_query_terms(hostile):
            core = term.strip("()")
            for part in core.split(" OR "):
                assert part.startswith('"') and part.endswith('"*'), part

    def test_or_join_inherits_safety(self) -> None:
        expr = fts_join_or(fts_query_terms('" OR col:"content'))
        con = sqlite3.connect(":memory:")
        con.execute('CREATE VIRTUAL TABLE t USING fts5(c, tokenize="unicode61")')
        con.execute("SELECT * FROM t WHERE t MATCH ?", (expr,)).fetchall()

    def test_empty_sanitisation_placeholder(self) -> None:
        assert fts_query_v2('***"""((()))***') == '"__mnemos_fts5_no_match_placeholder__"'

    def test_legacy_chokepoint_is_v2(self) -> None:
        """_build_fts_query (the M15.2-pinned symbol) IS the v2 builder."""
        assert SQLiteStore._build_fts_query("GWS конвейер") == fts_query_v2("GWS конвейер")


# ── 7. Single-token searches: no regression ─────────────────────────────────


class TestGoldenSingleTokenNoRegression:
    def test_single_token_finds_exact_row(self, manager) -> None:
        row = _add(manager, "windmill maintenance schedule for the polder")
        results = manager.search("windmill", limit=5)
        assert row.id in {r.memory.id for r in results}

    def test_single_token_prefix_is_superset_of_old_phrase(self) -> None:
        """A single-token v2 prefix term matches everything the old exact
        phrase matched (prefix ⊇ exact), so single-token recall can only
        GROW — the no-regression argument for the v2 semantics."""
        con = sqlite3.connect(":memory:")
        con.execute('CREATE VIRTUAL TABLE t USING fts5(c, tokenize="unicode61")')
        con.execute("INSERT INTO t VALUES ('windmill maintenance schedule')")
        old_style = '"windmill"'
        new_style = fts_query_v2("windmill")
        old_hits = con.execute("SELECT * FROM t WHERE t MATCH ?", (old_style,)).fetchall()
        new_hits = con.execute("SELECT * FROM t WHERE t MATCH ?", (new_style,)).fetchall()
        assert old_hits and new_hits
        assert {h[0] for h in new_hits} >= {h[0] for h in old_hits}

    def test_single_token_no_or_retry_needed(self) -> None:
        """Single-token queries never OR-retry (OR degenerates to the same
        single term) — the term list is exactly one."""
        assert len(fts_query_terms("windmill")) == 1
        assert fts_join_or(fts_query_terms("windmill")) == fts_query_v2("windmill")

    def test_russian_single_token_no_regression(self, manager) -> None:
        row = _add(manager, "регламент конвейера обновлён на этой неделе")
        assert row.id in {r.memory.id for r in manager.search("конвейер", limit=5)}


# ── 8. Degenerate short tokens (issue #314) ─────────────────────────────────


class TestGoldenDegenerateShortTokens:
    """Issue #314 — degenerate tokens must not constrain the AND join.

    A 1-char token (`a`) or a function word (`the`, «как») matches
    nearly every row, so its bm25 idf collapses to 0 and one such token
    inside an AND query degraded the result to LIMIT rows ordered by
    id-tiebreak noise. The guard drops them BEFORE the cap; acronyms
    (fully uppercase) and digit-bearing identifiers survive on merit.
    """

    def test_one_char_token_dropped(self) -> None:
        assert fts_query_terms("a конвейер") == ['"конвейер"*']

    def test_digit_identifiers_survive(self) -> None:
        """v2/p0/m15-class tokens are >=2 chars and never alpha
        stopwords — they survive without a digit-scanning carve-out."""
        assert fts_query_terms("v2 p0 конвейер") == ['"v2"*', '"p0"*', '"конвейер"*']

    def test_short_non_stopword_latin_survives(self) -> None:
        # 'qa' is 2 chars and not in the stopword list — survives.
        assert fts_query_terms("qa gates") == ['"qa"*', '"gates"*']

    def test_acronym_exemption_isupper(self) -> None:
        """Fully-uppercase tokens are acronyms in this technical corpus
        (IT, QA, DB, CI, ML, GWS) — never function words."""
        assert fts_query_terms("IT infrastructure") == ['"IT"*', '"infrastructure"*']

    def test_cyrillic_isupper_acronym_exemption(self) -> None:
        """Cyrillic isupper() acronyms are exempt too: «ИТ» (RU for IT)
        survives. The «ИХ» case is the discriminating one — lowercase
        «их» IS a stopword, so an .isascii() "fix" to the exemption
        would drop it from a multi-token query (review follow-up to
        issue #314)."""
        assert fts_query_terms("ИТ инфраструктура") == ['"ИТ"*', '"инфраструктура"*']
        assert fts_query_terms("ИХ инфраструктура") == ['"ИХ"*', '"инфраструктура"*']

    def test_en_stopword_dropped(self) -> None:
        assert fts_query_terms("the release runbook") == ['"release"*', '"runbook"*']

    def test_ru_sentence_initial_stopword_dropped(self) -> None:
        # «Как» is title-case, not isupper → lowercased check drops it.
        assert fts_query_terms("Как настроить конвейер") == ['"настроить"*', '"конвейер"*']

    def test_never_empty_single_char_query(self) -> None:
        """All-degenerate input keeps its tokens — the user's explicit
        query wins over a silent no-match placeholder."""
        assert fts_query_terms("a") == ['"a"*']

    def test_never_empty_stopword_query(self) -> None:
        assert fts_query_terms("the") == ['"the"*']

    def test_never_empty_is_whole_list(self) -> None:
        # All-degenerate MULTI-token input keeps ALL its tokens — the
        # never-empty fallback resurrects the whole original list, not
        # just the first token (review follow-up to issue #314).
        assert fts_query_terms("a the") == ['"a"*', '"the"*']

    def test_guard_runs_before_cap(self) -> None:
        """Dropped tokens consume no cap budget: 7 droppable tokens + 1
        real one leaves exactly 1 term (cap 8 never bites)."""
        assert len(fts_query_terms("a the and for with that this конвейер")) == 1

    def test_degenerate_term_no_longer_constrains_the_and(self, manager) -> None:
        """Recall preserved WITHOUT the OR retry: 'a' is dropped, so the
        AND is a single real term and a row with no Latin-'a'-prefixed
        token (CORPUS_B is all-Cyrillic) is still found by the AND."""
        row = _add(manager, CORPUS_B)
        manager.vectors.wipe()  # pin the leg: the FTS AND must find it alone
        results = manager.search("a конвейер", limit=5)
        assert row.id in {r.memory.id for r in results}

    def test_digit_identifier_stays_discriminative(self, manager) -> None:
        """The guard must not over-drop: 'v2' survives and still
        constrains the AND — a row with «конвейер» but no v2-token is
        NOT returned by the 'v2 конвейер' AND query."""
        both = _add(manager, "v2 конвейер поставки обновлён по регламенту")
        no_v2 = _add(manager, CORPUS_B)
        manager.vectors.wipe()  # pin the leg: only the FTS AND decides
        ids = {r.memory.id for r in manager.search("v2 конвейер", limit=5)}
        assert both.id in ids
        assert no_v2.id not in ids
