"""ADR-0027 Phase 0, slice 1 (epic #308, checklist items 1-2) — contract tests.

Covers the two zero-migration Phase-0 components:

1. **``task:`` tag-contract extension** — ``^task:[a-z0-9_-]{1,64}$``,
   optional (zero or one per record). The scope-hierarchy doctrine
   (ADR-0027): inheritance is INTERSECTION (project x agent x session x
   task) — a task tag NARROWS, never widens. Pinned here:
     * validator: format / cardinality / case rules, strict and lax;
     * search: a task-scoped query (``tags=["task:x"]`` + project +
       agent) stays inside ALL THREE scopes — never leaves the task,
       never leaves project+agent, including on the graph edge leg;
     * the project soft-fallback (#313) is NOT applied to task-scoped
       queries: the retry would keep the task tag while dropping the
       project scope — widening a task view across projects. The
       non-task control case pins that the default fallback still fires
       (the exemption is task-specific, the default path is untouched).
2. **Doc-grouping metadata convention** — ``{doc_id, chunk_idx,
   heading_path}`` rides the existing ``Memory.metadata`` JSON column
   (zero migration: no column, no index, no backfill). Pinned here:
   builder/accessor shape validation (all-or-nothing) and the full
   store round-trip through ``MemoryManager.add`` → SQLite → read-back.

3. **Default-path byte-identity** — with no task tags in play, the
   issuance path is unchanged: ``assemble_context`` output is
   byte-stable across calls and across store rebuilds (the per-call
   ``retrieved=`` provenance timestamp and row uuids are masked — they
   are by-design per-row/per-call data, not issuance determinism), and
   task-tagged rows stay fully visible to the DEFAULT search path (no
   accidental narrowing of the unscoped view).

Out of scope (slice 2): the code lens-preset and the ``task`` parameter
on ``assemble_context``.

Test embedder: ``_HashEmbedder`` (deterministic hashed bag-of-tokens) —
same rationale as the graph suites: a MagicMock embedder cannot
discriminate and would fake the ranking/gate semantics under test.

MUTATION PROTOCOL — the load-bearing pins are mutation-verified (mutant
→ failing test, documented in the PR body; unmutated code GREEN):

* ``test_task_scoped_query_never_soft_falls_back`` — M1 drops the
  ``and not task_scoped`` condition from the soft-fallback gate in
  ``MemoryManager.search``.
* ``test_graph_edge_never_leaves_task_scope`` — M2 disables the tags
  gate on the graph edge leg in ``_search_core``.
* ``test_multiple_task_tags_{raise,always_fatal}`` — M3 turns the
  multiple-``task:`` fatal check into a no-op in
  ``validate_tag_contract``.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.models import (
    TASK_SLUG_RE,
    Memory,
    MemoryCreate,
    MemorySource,
    MemoryStatus,
    TagContract,
    TagContractError,
    build_doc_grouping_metadata,
    doc_grouping_from_metadata,
    validate_tag_contract,
)

PROJECT = "f0-proj"
PROJECT_B = "f0-other"
AGENT = "f0-agent"
AGENT_B = "f0-other-agent"

VALID_BASE = ["project:x", "agent:y", "mnemos:learning"]


# ---------------------------------------------------------------------------
# Test embedder (deterministic — mirrors the graph suites)
# ---------------------------------------------------------------------------


class _HashEmbedder:
    """Deterministic test embedder: hashed bag-of-tokens vectors.

    ``embed`` is a pure function of the text; cosine tracks token
    overlap, so lexical overlap is the only similarity signal and every
    scope gate under test is the DECIDER.
    """

    DIM = 256

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.DIM
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            vec[h % self.DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def _settings(tmp: Path) -> Settings:
    settings = Settings(
        vesma={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
        },
        scanner={"enabled": False},  # type: ignore[arg-type]
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def mgr(tmp_path: Path) -> Iterator[MemoryManager]:
    manager = MemoryManager(_settings(tmp_path))
    manager._embedder = _HashEmbedder()
    yield manager
    manager.close()


def _row(
    manager: MemoryManager,
    content: str,
    *,
    project: str = PROJECT,
    agent: str = AGENT,
    task: str | None = None,
) -> Memory:
    """Add one published row with an explicit scope triple."""
    tags = [f"project:{project}", f"agent:{agent}", "mnemos:learning"]
    if task is not None:
        tags.append(f"task:{task}")
    return manager.add(
        MemoryCreate(
            content=content, tags=tags, source=MemorySource.MCP, status=MemoryStatus.PUBLISHED
        ),
        project=project,
        agent=agent,
    )


# ---------------------------------------------------------------------------
# 1a. Validator — task: tag format and cardinality (strict mode)
# ---------------------------------------------------------------------------


class TestTaskTagValidationStrict:
    def test_valid_task_tag_passes_through(self) -> None:
        tags = [*VALID_BASE, "task:refactor-auth"]
        assert validate_tag_contract(tags) == tags

    def test_no_task_tag_passes(self) -> None:
        """task: is optional — the pre-Phase-0 tag set stays valid."""
        assert validate_tag_contract(VALID_BASE) == VALID_BASE

    def test_slug_boundaries(self) -> None:
        """1..64 chars of [a-z0-9_-]: 1 and 64 pass, 0 and 65 raise."""
        validate_tag_contract([*VALID_BASE, "task:a"])
        validate_tag_contract([*VALID_BASE, "task:" + "a" * 64])
        with pytest.raises(TagContractError, match="task:"):
            validate_tag_contract([*VALID_BASE, "task:"])
        with pytest.raises(TagContractError, match="task:"):
            validate_tag_contract([*VALID_BASE, "task:" + "a" * 65])

    @pytest.mark.parametrize(
        "bad", ["task:Refactor", "task:refactor auth", "task:a$b", "task:рефакторинг"]
    )
    def test_invalid_slug_raises(self, bad: str) -> None:
        with pytest.raises(TagContractError, match="invalid task: tag format"):
            validate_tag_contract([*VALID_BASE, bad])

    def test_multiple_task_tags_raise(self) -> None:
        with pytest.raises(TagContractError, match="at most one task:"):
            validate_tag_contract([*VALID_BASE, "task:a", "task:b"])


class TestTaskTagValidationLax:
    def test_multiple_task_tags_always_fatal(self) -> None:
        """Cardinality ambiguity is fatal even in lax mode (union is
        forbidden by the intersection doctrine)."""
        with pytest.raises(TagContractError, match="at most one task:"):
            validate_tag_contract([*VALID_BASE, "task:a", "task:b"], strict=False)

    def test_uppercase_slug_normalized(self) -> None:
        result = validate_tag_contract([*VALID_BASE, "task:Refactor Auth"], strict=False)
        assert "task:refactor-auth" in result

    def test_unsalvageable_slug_dropped_not_unknown(self) -> None:
        """Lax mode drops an unsalvageable task slug — it never mints a
        fake ``task:unknown`` scope (for an OPTIONAL tag, absence is the
        honest fallback; ``project:unknown`` exists only because the
        project family is required)."""
        result = validate_tag_contract([*VALID_BASE, "task:!!!"], strict=False)
        assert not any(t.startswith("task:") for t in result)
        assert "task:unknown" not in result


class TestTaskSlugNewlineAnchor:
    """Ф1-PREP item 2 (#360 review) — the ``$``-anchor flaw.

    ``$`` also matches just before a trailing newline, so ``task:t1\\n``
    passed validation and could persist as a DEAD tag: the search tags
    filter is exact membership (``"task:t1" in row.tags``), so the
    newline-carrying tag is unreachable forever. The ``\\Z`` anchor
    (TASK_SLUG_RE and _TASK_RE alike) closes it; lax mode still SALVAGES
    a newline-carrying slug (``_normalize_slug`` strips whitespace).
    """

    def test_slug_re_rejects_trailing_newline(self) -> None:
        assert TASK_SLUG_RE.match("t1\n") is None
        assert TASK_SLUG_RE.match("t1") is not None  # control

    def test_strict_rejects_trailing_newline_tag(self) -> None:
        with pytest.raises(TagContractError, match="invalid task: tag format"):
            validate_tag_contract([*VALID_BASE, "task:t1\n"])

    def test_lax_salvages_trailing_newline_tag(self) -> None:
        """Whitespace is salvageable by design: lax mode normalizes the
        slug instead of dropping the (optional) task scope."""
        result = validate_tag_contract([*VALID_BASE, "task:t1\n"], strict=False)
        assert "task:t1" in result


class TestAnchorCompletionNewlineSweep:
    """#368 item 3 — the ``\\Z`` anchor sweep completed across the four
    scope regexes.

    ``_PROJECT_RE``/``_AGENT_RE``/``_VESMARO_RE`` were still on ``$``:
    'project:p1\\n' (confirmed live) and 'agent:a1\\n' passed STRICT
    validation with a trailing newline. No behavioral coupling with the
    task regexes (project/agent are matched columnally at query time,
    ``m.project = ?``), but a newline-carrying slug is unreachable at
    query time exactly like the task dead-tag. ``\\Z`` (absolute end)
    rejects it at the boundary; lax mode still salvages via
    ``_normalize_slug`` strip.
    """

    @pytest.mark.parametrize("tag", ["project:p1\n", "agent:a1\n"])
    def test_strict_rejects_trailing_newline_tags(self, tag: str) -> None:
        """The duplicate-prefix fatal rule needs an INVALID, not a
        second VALID prefix — so the newline tag rides on a base whose
        own prefix family is patched around it (VALID_BASE with the
        matching member removed)."""
        base = [t for t in VALID_BASE if not tag.startswith(t.split(":")[0] + ":")]
        with pytest.raises(TagContractError):
            validate_tag_contract([*base, tag])

    @pytest.mark.parametrize("tag", ["project:p1\n", "agent:a1\n"])
    def test_lax_salvages_trailing_newline_tags(self, tag: str) -> None:
        """Same salvage contract as the task anchor: whitespace is
        strip-normalized, the scope survives."""
        base = [t for t in VALID_BASE if not tag.startswith(t.split(":")[0] + ":")]
        result = validate_tag_contract([*base, tag], strict=False)
        assert tag.strip() in result
        assert tag not in result

    @pytest.mark.parametrize("tag", ["project:p1", "agent:a1"])
    def test_clean_tags_still_validate(self, tag: str) -> None:
        """Control: the sweep only NARROWS admissibility — clean tags
        behave identically before and after the anchor change."""
        base = [t for t in VALID_BASE if not tag.startswith(t.split(":")[0] + ":")]
        tags = [*base, tag]
        assert validate_tag_contract(tags) == tags

    def test_mnemos_trailing_newline_rejected_strict(self) -> None:
        """The ``mnemos:`` family rides the same anchor: a trailing
        newline fails the format check in strict mode ('learning\\n' can
        never be a whitelisted subtype)."""
        with pytest.raises(TagContractError, match="invalid mnemos: tag format"):
            validate_tag_contract([*VALID_BASE, "mnemos:learning\n"])


class TestTagContractModel:
    def test_extracts_task_slug(self) -> None:
        tc = TagContract(tags=[*VALID_BASE, "task:refactor-auth"])
        assert tc.task == "refactor-auth"

    def test_task_defaults_to_empty(self) -> None:
        tc = TagContract(tags=VALID_BASE)
        assert tc.task == ""

    def test_memory_strict_tags_accepts_task(self) -> None:
        m = Memory(
            content="x",
            tags=["project:p", "agent:a", "mnemos:decision", "task:t"],
            strict_tags=True,
        )
        assert "task:t" in m.tags


# ---------------------------------------------------------------------------
# 1b. Search — scope intersection (project x agent x task)
# ---------------------------------------------------------------------------


class TestScopeIntersection:
    def _seed(self, manager: MemoryManager) -> dict[str, Memory]:
        rows = {
            "in_scope": _row(manager, "kubernetes sidecar retry budget notes", task="t1"),
            "other_task": _row(manager, "kubernetes sidecar retry budget notes", task="t2"),
            "no_task": _row(manager, "kubernetes sidecar retry budget notes"),
            "other_project": _row(
                manager, "kubernetes sidecar retry budget notes", project=PROJECT_B, task="t1"
            ),
            "other_agent": _row(
                manager, "kubernetes sidecar retry budget notes", agent=AGENT_B, task="t1"
            ),
        }
        manager.vectors.wipe()  # FTS-only: the scope gates are the decider
        return rows

    def test_task_scoped_search_intersects_all_three_scopes(self, mgr: MemoryManager) -> None:
        rows = self._seed(mgr)
        results = mgr.search(
            "kubernetes sidecar",
            tags=["task:t1"],
            project=PROJECT,
            agent=AGENT,
            limit=10,
        )
        ids = {r.memory.id for r in results}
        assert ids == {rows["in_scope"].id}
        # Never leaves the task:
        assert rows["other_task"].id not in ids
        assert rows["no_task"].id not in ids
        # Stays inside project+agent:
        assert rows["other_project"].id not in ids
        assert rows["other_agent"].id not in ids

    def test_default_search_unnarrowed_by_task_tags(self, mgr: MemoryManager) -> None:
        """Task tags are inert on the DEFAULT path: every in-scope row
        (task-tagged or not) still surfaces without a tags filter."""
        rows = self._seed(mgr)
        results = mgr.search("kubernetes sidecar", project=PROJECT, agent=AGENT, limit=10)
        ids = {r.memory.id for r in results}
        assert rows["in_scope"].id in ids
        assert rows["other_task"].id in ids
        assert rows["no_task"].id in ids
        assert rows["other_project"].id not in ids
        assert rows["other_agent"].id not in ids

    def test_graph_edge_never_leaves_task_scope(self, mgr: MemoryManager) -> None:
        """An edge neighbour without the query's task tag must not
        surface in a task-scoped search (the tags gate binds to the edge
        leg exactly like the project guard — intersection, not union)."""
        anchor = _row(mgr, "anchor tide schedule notes", task="t1")
        neighbour = _row(mgr, "dormant ledger reconciliation figures")
        mgr.add_memory_edge(anchor.id, neighbour.id, kind="supersedes")
        mgr.vectors.wipe()

        results = mgr.search("anchor tide", tags=["task:t1"], project=PROJECT, agent=AGENT, limit=5)
        ids = {r.memory.id for r in results}
        assert anchor.id in ids
        assert neighbour.id not in ids
        # Control: without the task condition the edge neighbour DOES
        # surface — the pin above is the task gate, not a broken edge.
        control = mgr.search("anchor tide", project=PROJECT, agent=AGENT, limit=5)
        assert neighbour.id in {r.memory.id for r in control}

    def test_task_scoped_query_never_soft_falls_back(self, mgr: MemoryManager) -> None:
        """ADR-0027 intersection doctrine: a scoped search that finds
        nothing in the task must NOT retry without the project scope —
        the retry would widen a task view across projects. The foreign
        row carrying the SAME task tag and matching the query text must
        stay invisible; the fallback counter must not move."""
        foreign = _row(
            mgr,
            "quokka habitat survey notes",
            project=PROJECT_B,
            task="t9",
        )
        mgr.vectors.wipe()

        results = mgr.search(
            "quokka habitat",
            tags=["task:t9"],
            project=PROJECT,
            agent=AGENT,
            limit=10,
        )
        assert results == []
        assert mgr.search_stats()["project_scope_fallback_total"] == 0
        # Control (default path untouched): the same shape WITHOUT a
        # task tag still soft-falls-back — the exemption is task-specific.
        control = mgr.search("quokka habitat", project=PROJECT, agent=AGENT, limit=10)
        assert foreign.id in {r.memory.id for r in control}
        assert all(r.project_scope_fallback for r in control)
        assert mgr.search_stats()["project_scope_fallback_total"] == 1


class TestBareSlugTaskFallback:
    """Issue #455 (eyes-session feedback #2): the tags filter is
    exact-membership, so ``tags=["<bare-slug>"]`` never matched rows
    tagged ``task:<slug>`` — a silent empty page. The fallback retries
    ONCE with the exact minted tag when the store's tag census proves
    ``task:<slug>`` rows exist; surfaced rows carry
    ``task_tag_fallback=True`` and the event counts in
    ``search_stats()["task_tag_fallback_total"]``."""

    def test_bare_slug_finds_task_rows_marked_fallback(self, mgr: MemoryManager) -> None:
        rows = {
            "t1": _row(mgr, "harbor tide schedule notes", task="qa-vesma"),
            "t2": _row(mgr, "harbor tide schedule notes", task="other-task"),
        }
        mgr.vectors.wipe()  # FTS-only: the tag gate is the decider

        results = mgr.search("harbor tide", tags=["qa-vesma"], project=PROJECT, limit=10)
        ids = {r.memory.id for r in results}
        assert ids == {rows["t1"].id}, "the bare slug must find the task-scoped row"
        assert all(r.task_tag_fallback for r in results)
        assert mgr.search_stats()["task_tag_fallback_total"] == 1

    def test_exact_tag_query_unchanged_no_marker(self, mgr: MemoryManager) -> None:
        row = _row(mgr, "harbor tide schedule notes", task="qa-vesma")
        mgr.vectors.wipe()

        results = mgr.search("harbor tide", tags=["task:qa-vesma"], project=PROJECT, limit=10)
        assert {r.memory.id for r in results} == {row.id}
        assert all(not r.task_tag_fallback for r in results)
        assert mgr.search_stats()["task_tag_fallback_total"] == 0

    def test_no_task_rows_stays_empty_without_fallback(self, mgr: MemoryManager) -> None:
        """A bare slug with NO ``task:<slug>`` rows in the store is an
        honest empty page — no retry, no marker, no counter movement."""
        _row(mgr, "harbor tide schedule notes", task="qa-vesma")
        mgr.vectors.wipe()

        results = mgr.search("harbor tide", tags=["no-such-task"], project=PROJECT, limit=10)
        assert results == []
        assert mgr.search_stats()["task_tag_fallback_total"] == 0
        # The project soft-fallback alone does not rescue it either (the
        # bare tag matches nothing in ANY project).

    def test_bare_slug_keeps_project_scope(self, mgr: MemoryManager) -> None:
        """The retry narrows nothing and widens nothing: the project
        scope (and agent) stay bound — only the tag FORM changes."""
        mine = _row(mgr, "harbor tide schedule notes", task="qa-vesma")
        foreign = _row(mgr, "harbor tide schedule notes", project=PROJECT_B, task="qa-vesma")
        mgr.vectors.wipe()

        results = mgr.search("harbor tide", tags=["qa-vesma"], project=PROJECT, limit=10)
        ids = {r.memory.id for r in results}
        assert ids == {mine.id}
        assert foreign.id not in ids
        assert all(r.task_tag_fallback for r in results)
        # The task fallback fired and surfaced rows → the project
        # soft-fallback must NOT also fire (results were found in scope).
        assert mgr.search_stats()["project_scope_fallback_total"] == 0

    def test_task_param_mints_exact_tag_no_fallback(self, mgr: MemoryManager) -> None:
        """The ``task=`` switcher mints the tag at the boundary — the
        A==C equivalence doctrine: it needs no fallback and never takes
        it (its filter carries the ``task:`` prefix by construction)."""
        row = _row(mgr, "harbor tide schedule notes", task="qa-vesma")
        mgr.vectors.wipe()

        results = mgr.search("harbor tide", task="qa-vesma", project=PROJECT, limit=10)
        assert {r.memory.id for r in results} == {row.id}

    def test_mixed_exact_and_bare_tags_never_retry(
        self, mgr: MemoryManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Guard branch (manager._bare_slug_task_retry_tags): a tags
        filter that ALREADY carries a ``task:`` entry never takes the
        rewrite — exact-tag queries keep byte-identical behavior even
        when a bare sibling entry could have been rewritten (the
        ``task:<bare>`` rows exist in the census, so the rewrite WOULD
        qualify). Pinned on the retry log line: the rewrite never fires,
        so the honest empty page is the FIRST answer, not a fallback."""
        _row(mgr, "harbor tide schedule notes", task="qa-vesma")
        _row(mgr, "harbor tide schedule notes", task="keep-me")
        mgr.vectors.wipe()

        import logging

        with caplog.at_level(logging.INFO, logger="vesma.manager"):
            # The mixed filter matches nothing: no row carries BOTH
            # ``task:keep-me`` AND the bare ``qa-vesma`` (rows carry
            # ``task:qa-vesma`` — the AND-exact-membership filter).
            results = mgr.search(
                "harbor tide", tags=["task:keep-me", "qa-vesma"], project=PROJECT, limit=10
            )
        assert results == []
        assert mgr.search_stats()["task_tag_fallback_total"] == 0
        assert not any("task-tag fallback" in r.message for r in caplog.records), (
            "a mixed exact+bare tags filter must never fire the bare-slug retry"
        )

    def test_retry_with_zero_rows_no_marker_no_counter(self, mgr: MemoryManager) -> None:
        """Guard branch (MemoryManager.search retry leg): the census says
        ``task:<slug>`` rows exist, the retry fires — but finds ZERO
        rows (the task row lives in another project; the retry narrows
        nothing and widens nothing). Zero surfaced rows means no
        ``task_tag_fallback`` marker anywhere and NO counter bump: the
        counter counts surfaced-fallback pages, not fired retries."""
        _row(mgr, "harbor tide schedule notes", project=PROJECT_B, task="qa-vesma")
        mgr.vectors.wipe()

        results = mgr.search("harbor tide", tags=["qa-vesma"], project=PROJECT, limit=10)
        assert results == []
        assert mgr.search_stats()["task_tag_fallback_total"] == 0
        # The follow-on project soft-fallback also surfaces nothing
        # (the bare tag matches no row in ANY project) — its counter
        # stays quiet too.
        assert mgr.search_stats()["project_scope_fallback_total"] == 0
        assert all(not r.task_tag_fallback for r in results)
        assert mgr.search_stats()["task_tag_fallback_total"] == 0


# ---------------------------------------------------------------------------
# 2. Doc-grouping metadata convention (zero migration)
# ---------------------------------------------------------------------------


class TestDocGroupingMetadata:
    def test_builder_round_shape(self) -> None:
        meta = build_doc_grouping_metadata("adr-0027", 3, ["Context", "Decision"])
        assert meta == {
            "doc_id": "adr-0027",
            "chunk_idx": 3,
            "heading_path": ["Context", "Decision"],
        }

    def test_builder_strips_doc_id_and_allows_empty_headings(self) -> None:
        meta = build_doc_grouping_metadata("  doc-1  ", 0, [])
        assert meta == {"doc_id": "doc-1", "chunk_idx": 0, "heading_path": []}

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"doc_id": "", "chunk_idx": 0, "heading_path": []},
            {"doc_id": "x" * 257, "chunk_idx": 0, "heading_path": []},
            {"doc_id": "d", "chunk_idx": -1, "heading_path": []},
            {"doc_id": "d", "chunk_idx": True, "heading_path": []},
            {"doc_id": "d", "chunk_idx": "3", "heading_path": []},
            {"doc_id": "d", "chunk_idx": 0, "heading_path": "Context"},
            {"doc_id": "d", "chunk_idx": 0, "heading_path": [1, 2]},
        ],
    )
    def test_builder_rejects_bad_shapes(self, kwargs: dict[str, object]) -> None:
        with pytest.raises(ValueError):
            build_doc_grouping_metadata(**kwargs)

    def test_accessor_none_when_absent(self) -> None:
        assert doc_grouping_from_metadata({"source": "chat"}) is None
        assert doc_grouping_from_metadata({}) is None

    def test_accessor_round_trip(self) -> None:
        meta = build_doc_grouping_metadata("doc-9", 12, ["Install", "Linux"])
        assert doc_grouping_from_metadata(meta) == meta

    @pytest.mark.parametrize(
        "partial",
        [
            {"doc_id": "d"},
            {"doc_id": "d", "chunk_idx": 0},
            {"chunk_idx": 0, "heading_path": []},
            {"doc_id": "d", "heading_path": []},
        ],
    )
    def test_accessor_rejects_partial_triples(self, partial: dict[str, object]) -> None:
        """All-or-nothing: a half-written grouping is corruption, not a
        non-document row (Phase-3 assembly groups by doc_id — a silent
        None would split a document without a trace)."""
        with pytest.raises(ValueError, match="all-or-nothing"):
            doc_grouping_from_metadata(partial)

    def test_accessor_rejects_malformed_types(self) -> None:
        with pytest.raises(ValueError):
            doc_grouping_from_metadata({"doc_id": "d", "chunk_idx": "3", "heading_path": []})


class TestDocGroupingStoreRoundTrip:
    def test_metadata_survives_add_sqlite_readback(self, mgr: MemoryManager) -> None:
        """The convention rides the existing ``metadata`` JSON column:
        MemoryManager.add → SQLiteStore.save → get returns the triple
        unchanged, alongside the existing file_path/source_url columns
        (zero migration)."""
        grouping = build_doc_grouping_metadata("runbook-42", 7, ["Deploy", "Rollback"])
        memory = mgr.add(
            MemoryCreate(
                content="Rollback: flip the canonical tag, then drain.",
                tags=["project:f0-proj", "agent:f0-agent", "mnemos:rule"],
                source=MemorySource.MCP,
                status=MemoryStatus.PUBLISHED,
                source_url="https://example.test/runbook-42",
                metadata=dict(grouping),
            ),
            project=PROJECT,
            agent=AGENT,
        )
        stored = mgr.sqlite.get(memory.id)
        assert stored is not None
        assert doc_grouping_from_metadata(stored.metadata) == grouping
        # add() vault-writes and stamps its own file_path (by design) —
        # the grouping convention coexists with both existing columns.
        assert stored.file_path  # vault path populated
        assert stored.source_url == "https://example.test/runbook-42"
        # Non-doc rows are unaffected: no grouping keys anywhere.
        plain = _row(mgr, "plain row without doc grouping")
        stored_plain = mgr.sqlite.get(plain.id)
        assert stored_plain is not None
        assert doc_grouping_from_metadata(stored_plain.metadata) is None


# ---------------------------------------------------------------------------
# 3. Default-path byte-identity (no task tags → unchanged)
# ---------------------------------------------------------------------------

_RETRIEVED_RE = re.compile(r"retrieved=[0-9T:.\+\-]+Z?")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

BYTE_ROWS: list[tuple[str, str]] = [
    ("alpha", "Deploy pipeline uses FTS5 hybrid search with reciprocal rank fusion"),
    ("beta", "Vector leg applies a pre-RRF project predicate before fusion"),
    ("gamma", "Checkpoints ride the on_session_start bootstrap channel"),
    ("delta", "Quarantine exclusion is absolute on every search leg"),
]


def _normalized(text: str) -> str:
    t = _RETRIEVED_RE.sub("retrieved=X", text)
    return _UUID_RE.sub("<uuid>", t)


class TestDefaultPathByteIdentity:
    def test_assemble_byte_stable_in_instance(self, mgr: MemoryManager) -> None:
        for _title, content in BYTE_ROWS:
            _row(mgr, content)
        t1 = mgr.assemble_context(
            session="f0-s", project=PROJECT, agent=AGENT, query="hybrid search deploy"
        )["text"]
        t2 = mgr.assemble_context(
            session="f0-s", project=PROJECT, agent=AGENT, query="hybrid search deploy"
        )["text"]
        assert t1 == t2

    def test_assemble_byte_stable_across_store_rebuilds(self, tmp_path: Path) -> None:
        """Two fresh stores built from the same no-task inputs produce
        byte-identical assemblies (uuids and the per-call ``retrieved=``
        provenance masked — everything else must match)."""
        texts: list[str] = []
        for i in range(2):
            root = tmp_path / f"store{i}"
            manager = MemoryManager(_settings(root))
            manager._embedder = _HashEmbedder()
            for _title, content in BYTE_ROWS:
                _row(manager, content)
            texts.append(
                _normalized(
                    manager.assemble_context(
                        session="f0-s", project=PROJECT, agent=AGENT, query="hybrid search deploy"
                    )["text"]
                )
            )
            manager.close()
        assert texts[0] == texts[1]

    def test_task_tagged_rows_visible_on_default_path(self, mgr: MemoryManager) -> None:
        """The default assembly recalls task-tagged rows like any other
        row — Phase 0 adds no narrowing to the unscoped path (a task
        condition exists only as an explicit query filter)."""
        _row(mgr, "baseline content about zebra migrations")
        tagged = _row(mgr, "tagged content about zebra migrations", task="t1")
        out = mgr.assemble_context(
            session="f0-s", project=PROJECT, agent=AGENT, query="zebra migrations"
        )
        assert tagged.id in {b["memory_id"] for b in out["blocks"]}
