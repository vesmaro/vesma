"""ADR-0019 Phase B slice B1 — pipeline_state, quarantine predicate, marker.

Coverage map (one section per B1 deliverable):

* **Schema / backfill** — the five new columns reach legacy DBs via
  ALTER (instant, metadata-only); the one-time backfill classifies the
  three legacy row classes (synthesized/processed → ``refined`` with
  ``processed_at``; lineage-less published → ``pending``;
  raw/processing/archived → NULL) idempotently, and the external-content
  FTS index stays intact (rowids stable, no rebuild).
* **Quarantine predicate (§5)** — ``pipeline_state='quarantined'`` rows
  are excluded from every issuance path, ABSOLUTELY (regardless of
  status — quarantined rows carry status='published' and the external
  payloads carry no pipeline_state, so an explicit ``status=`` leak
  would be undetectable by the caller): the FTS leg and the vector
  resolve leg of ``search`` (default, ``include_raw`` AND the explicit
  ``status=`` drill-down), the listing surface (``list_recent`` —
  REST GET /memories / MCP vesma_list_recent),
  ``issue_context_filter``, ``recall_context``'s recency leg,
  ``agent_recall``'s recency leg, and ``assemble_context`` (which
  recalls through ``search``). Direct get-by-id is the documented
  residual access until the B2 retraction render.
* **Marker contract (§4)** — the bracket string renders
  ``pipeline=<phase> v=<n>`` from the SAME ``Memory`` snapshot the
  projection was cut from (single construction site:
  ``assemble.build_provenance``); NULL/legacy pipeline_state omits the
  segment; the structured block fields are the source of truth.
* **refined_only (§4)** — manager / REST / MCP issue only
  ``pipeline_state='refined'`` projections; legacy NULL rows never match.
* **N1 (review #161 follow-up)** — direct publication flips route
  through the Phase A danger gate: direct-seed ``add``, status flips via
  ``update``, and content OR TITLE edits of already-published rows
  (titles are part of the served projection; the issuance scan does not
  screen titles for injections). Refusals are zero-loss (content stored,
  visibility refused/demoted) and audited with the '``publish gate: …``'
  line convention.
* **B2a — async refinement cycle (§5/§6/§10-B)** — publication enters
  the pipeline (NULL→pending, failed→pending fresh cycle, refined
  untouched); the daemon intake picks pending + retry-budgeted failed
  rows; the §6 swap is one ``update_fields`` transaction on the SAME
  row (id stable, ``raw_content`` byte-identical, ``clean_content``
  reset, ``marker_version`` bumped only on a real content change,
  ``swap_key`` idempotency); the four outcome lanes (refined /
  refined-noop / failed-with-backoff / quarantined-terminal) and their
  audit lines; competitive claim CAS; manual quarantine release
  (manager + REST); the stale-embed sweeper and the quarantine skip in
  ``rebuild_vector_index``.
* **Lease/reclaim (issue #170, ADR-0019 Phase C)** — a ``processing``
  claim stamps the lease clock (``updated_at``); the sweeper's reclaim
  returns lease-expired rows to ``pending`` through a per-row CAS
  (fresh claims untouched, double reclaim single-wins, retry counter
  not consumed) and the reclaimed rows re-enter the intake.
* **N1 on PROCESSED rows (issue #171)** — content/title edits of
  PROCESSED rows (the other admissible status) re-enter the same Phase
  A gate: refusal demotes to RAW without touching ``pipeline_state``
  (B1 invariant), a clean edit requeues the row to ``pending`` (F8
  semantics, legacy NULL rows of admissible status included).
* **Filter projection on edit (issue #193)** — a content replace
  resets ``clean_content`` in the SAME transaction: the served
  projection (``effective_content``) is the new content immediately,
  never the stale pre-edit filter output.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import vesmaro.pipeline.refine as refine_mod
from vesmaro.api import main as api_main
from vesmaro.api.main import app, lifespan
from vesmaro.assemble import build_provenance
from vesmaro.config import Settings
from vesmaro.danger_detectors import DetectionResult
from vesmaro.manager import MemoryManager
from vesmaro.models import (
    AgentRecallQuery,
    Memory,
    MemoryCreate,
    MemorySource,
    MemoryStatus,
    MemoryUpdate,
    PipelineState,
    SearchQuery,
)
from vesmaro.pipeline.refine import refine_single

PROJECT = "b1-proj"
AGENT = "b1-agent"
SESSION = "b1-sess"

# Fake high-confidence secrets (the detector's own regex shapes — never real).
FAKE_AWS_KEY = "AKIAEXAMPLEABCDEFGH1"
# ghp_ + 36 alnum (the github-token pattern's exact shape).
FAKE_GITHUB_TOKEN = "ghp_Ex4mplePl4ceh0lderTok3n42AbCdEfGhIjKl"

MARKER_RE = re.compile(
    r"^\[mnemos:(?P<id>[0-9a-f-]{36}) project=(?P<project>\S+) "
    r"status=(?P<status>\S+) origin=(?P<origin>\S+)(?: pipeline=(?P<pipeline>\S+))? "
    r"v=(?P<version>\d+) retrieved=(?P<iso>\S+)\]$"
)


# ── Fixtures / helpers ─────────────────────────────────────────────────────────


def _settings(tmp: Path) -> Settings:
    settings = Settings(
        mnemos={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
        },
        scanner={"enabled": False},
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
    with TemporaryDirectory() as tmpdir:
        mgr = _manager(_settings(Path(tmpdir)))
        yield mgr
        mgr.close()


@pytest.fixture
def rest_client(manager: MemoryManager) -> Iterator[TestClient]:
    api_main._manager = manager
    test_app = FastAPI(title="Mnemos-B1-Test", version="0.1.0", lifespan=lifespan)
    for route in app.routes:
        test_app.routes.append(route)
    with TestClient(test_app) as tc:
        yield tc
    api_main._manager = None


def _published(
    mgr: MemoryManager,
    content: str,
    *,
    tags: list[str] | None = None,
    title: str | None = None,
    status: MemoryStatus = MemoryStatus.PUBLISHED,
) -> Memory:
    """Direct-seed a memory (clean content passes the N1 gate)."""
    data = MemoryCreate(
        content=content,
        title=title,
        tags=tags or [f"project:{PROJECT}", f"agent:{AGENT}", "mnemos:learning"],
        source=MemorySource.MCP,
        status=status,
    )
    return mgr.add(data, project=PROJECT, agent=AGENT)


def _quarantine(mgr: MemoryManager, memory_id: str, reason: str = "secret") -> None:
    """Store-internal quarantine transition (the B2 daemon's writer path)."""
    assert mgr.sqlite.update_fields(
        memory_id, pipeline_state=PipelineState.QUARANTINED, quarantine_reason=reason
    )


def _pipeline_state(mgr: MemoryManager, memory_id: str, state: PipelineState) -> None:
    assert mgr.sqlite.update_fields(memory_id, pipeline_state=state)


# ── 1. Schema migration + backfill ────────────────────────────────────────────


_LEGACY_MEMORIES_DDL = """
CREATE TABLE memories (
    id               TEXT PRIMARY KEY,
    content          TEXT NOT NULL,
    title            TEXT,
    tags             TEXT NOT NULL DEFAULT '[]',
    source           TEXT NOT NULL DEFAULT 'manual',
    source_url       TEXT,
    memory_type      TEXT NOT NULL DEFAULT 'note',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    metadata         TEXT NOT NULL DEFAULT '{}',
    file_path        TEXT,
    category         TEXT,
    project          TEXT NOT NULL DEFAULT '',
    agent            TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'raw',
    quality_score    REAL,
    confidence       REAL,
    source_coverage  INTEGER,
    cluster_id       TEXT,
    derived_from     TEXT NOT NULL DEFAULT '[]',
    embedding_id     TEXT
)
"""

_LEGACY_FTS = """
CREATE VIRTUAL TABLE memories_fts USING fts5(
    id UNINDEXED, title, content, tags, project UNINDEXED, agent UNINDEXED,
    content=memories, content_rowid=rowid, tokenize='unicode61'
);
CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, id, title, content, tags, project, agent)
    VALUES (new.rowid, new.id, new.title, new.content, new.tags,
            new.project, new.agent);
END;
CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, id, title, content, tags,
                             project, agent)
    VALUES ('delete', old.rowid, old.id, old.title, old.content, old.tags,
            old.project, old.agent);
    INSERT INTO memories_fts(rowid, id, title, content, tags, project, agent)
    VALUES (new.rowid, new.id, new.title, new.content, new.tags,
            new.project, new.agent);
END;
"""

_TS = "2026-08-29T12:00:00+00:00"


def _build_legacy_db(path: Path) -> None:
    """Hand-build a pre-B1 database covering the backfill classes."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(_LEGACY_MEMORIES_DDL)
        conn.executescript(_LEGACY_FTS)
        rows = [
            # (id, content, status, cluster_id, derived_from, q, conf, cov, source)
            # (a) synthesized pipeline output: derived_from present →
            # refined (cluster_id AND source are irrelevant here)
            (
                "syn-pub",
                "synthesized alpha output",
                "published",
                "cl-1",
                '["src1"]',
                0.9,
                0.8,
                3,
                "manual",
            ),
            # (a) synthesized via the source marker alone (no
            # derived_from, no cluster) → refined
            (
                "syn-source",
                "synthesized theta by source marker",
                "published",
                None,
                "[]",
                0.9,
                0.8,
                2,
                "synthesized",
            ),
            # (a) mid-pipeline processed row → refined
            ("proc-row", "processed beta row", "processed", None, "[]", 0.7, 0.6, 1, "manual"),
            # (b') stuck-rescue row (reviewer case): published by the
            # rescue path with cluster_id (the clustering stage writes
            # it on raw members) but NO synthesis lineage — empty
            # derived_from, source != synthesized → pending, NOT refined
            (
                "stuck-rescue",
                "rescued kappa cluster member",
                "published",
                "cl-9",
                "[]",
                0.5,
                0.5,
                1,
                "manual",
            ),
            # (b) Hermes bypass: published, no lineage, gate wrote nothing
            (
                "hermes-row",
                "bypass gamma notes",
                "published",
                None,
                "[]",
                None,
                None,
                None,
                "manual",
            ),
            # (b) single-passthrough placeholder promotion (0.5/0.5/1, no
            # lineage, no LLM refinement) — same class as the bypass
            (
                "passthrough",
                "placeholder delta row",
                "published",
                None,
                "[]",
                0.5,
                0.5,
                1,
                "manual",
            ),
            # (c) legacy statuses — untouched (NULL)
            ("raw-row", "raw epsilon draft", "raw", None, "[]", None, None, None, "manual"),
            ("proc-ing", "processing zeta", "processing", None, "[]", None, None, None, "manual"),
            ("arch-row", "archived eta", "archived", None, "[]", None, None, None, "manual"),
        ]
        for mid, content, status, cl, df, q, conf, cov, src in rows:
            conn.execute(
                "INSERT INTO memories (id, content, status, cluster_id, derived_from,"
                " quality_score, confidence, source_coverage, source, created_at,"
                " updated_at, metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (mid, content, status, cl, df, q, conf, cov, src, _TS, _TS, "{}"),
            )
        conn.commit()
    finally:
        conn.close()


class TestB1Migration:
    def test_legacy_columns_added_and_rows_classified(self, tmp_path: Path) -> None:
        from vesmaro.storage.sqlite_store import SQLiteStore

        db = tmp_path / "legacy.db"
        _build_legacy_db(db)
        store = SQLiteStore(db)
        conn = store._get_conn()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
        assert {
            "pipeline_state",
            "processed_at",
            "swap_key",
            "quarantine_reason",
            "marker_version",
        } <= cols
        rows = {
            r["id"]: (r["pipeline_state"], r["processed_at"], r["marker_version"])
            for r in conn.execute(
                "SELECT id, pipeline_state, processed_at, marker_version FROM memories"
            )
        }
        # (a) refined + processed_at = updated_at + marker_version default 1
        assert rows["syn-pub"] == ("refined", _TS, 1)
        assert rows["syn-source"] == ("refined", _TS, 1)
        assert rows["proc-row"] == ("refined", _TS, 1)
        # (b') stuck-rescue: cluster_id alone is NOT lineage → pending,
        # no fabricated processed_at (reviewer case)
        assert rows["stuck-rescue"] == ("pending", None, 1)
        # (b) bypass / passthrough heritage → pending (re-healed by B2)
        assert rows["hermes-row"] == ("pending", None, 1)
        assert rows["passthrough"] == ("pending", None, 1)
        # (c) legacy statuses stay NULL
        assert rows["raw-row"][0] is None
        assert rows["proc-ing"][0] is None
        assert rows["arch-row"][0] is None
        # Backfill flag set in the same commit.
        assert conn.execute(
            "SELECT 1 FROM meta WHERE key='schema_backfill_pipeline_state_v1'"
        ).fetchone()
        store.close()

    def test_backfill_idempotent_on_reopen(self, tmp_path: Path) -> None:
        from vesmaro.storage.sqlite_store import SQLiteStore

        db = tmp_path / "legacy.db"
        _build_legacy_db(db)
        store = SQLiteStore(db)
        store.close()
        store = SQLiteStore(db)  # reopen: no re-classification drift
        conn = store._get_conn()
        rows = {
            r["id"]: r["pipeline_state"]
            for r in conn.execute("SELECT id, pipeline_state FROM memories")
        }
        assert rows["syn-pub"] == "refined"
        assert rows["syn-source"] == "refined"
        assert rows["stuck-rescue"] == "pending"
        assert rows["hermes-row"] == "pending"
        assert rows["raw-row"] is None
        store.close()

    def test_fts_intact_after_migration_no_rowid_drift(self, tmp_path: Path) -> None:
        """No FTS rebuild: the backfill UPDATEs reindex the SAME rowids."""
        from vesmaro.storage.sqlite_store import SQLiteStore

        db = tmp_path / "legacy.db"
        _build_legacy_db(db)
        before = sqlite3.connect(str(db))
        rowids_before = sorted(r[0] for r in before.execute("SELECT rowid FROM memories"))
        before.close()
        store = SQLiteStore(db)
        conn = store._get_conn()
        rowids_after = sorted(r[0] for r in conn.execute("SELECT rowid FROM memories"))
        assert rowids_after == rowids_before
        fts_n = conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]
        assert fts_n == conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        # Classified rows remain searchable (trigger reindex is balanced).
        hits = store.fts_search("gamma", project=None)
        assert any(m.id == "hermes-row" for m, _ in hits)
        store.close()

    def test_fresh_db_has_columns_from_day_one(self, tmp_path: Path) -> None:
        from vesmaro.storage.sqlite_store import SQLiteStore

        store = SQLiteStore(tmp_path / "fresh.db")
        conn = store._get_conn()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
        assert "marker_version" in cols
        # Default value materialised (NOT NULL DEFAULT 1).
        default = [
            r for r in conn.execute("PRAGMA table_info(memories)") if r[1] == "marker_version"
        ]
        assert default[0][4] == "1"
        store.close()

    def test_save_roundtrips_lifecycle_columns(self, manager: MemoryManager) -> None:
        mem = _published(manager, "roundtrip target about sigma")
        _pipeline_state(manager, mem.id, PipelineState.REFINED)
        reloaded = manager.sqlite.get(mem.id)
        assert reloaded is not None
        assert reloaded.pipeline_state == PipelineState.REFINED
        assert reloaded.marker_version == 1
        # update_fields swap path (ADR §Swap): version + state in one call.
        manager.sqlite.update_fields(
            mem.id,
            pipeline_state=PipelineState.REFINED,
            swap_key="hash-1",
            marker_version=2,
        )
        reloaded = manager.sqlite.get(mem.id)
        assert reloaded is not None
        assert reloaded.swap_key == "hash-1"
        assert reloaded.marker_version == 2


# ── 2. Quarantine visibility predicate (§5) ───────────────────────────────────


class TestQuarantineExclusion:
    def test_search_fts_leg_excludes_quarantined(self, manager: MemoryManager) -> None:
        clean = _published(manager, "visible notes about sigma deployment")
        quarantined = _published(manager, "quarantined notes about tau deployment")
        _quarantine(manager, quarantined.id)

        hits = manager.search("deployment", project=PROJECT)
        ids = {r.memory.id for r in hits}
        assert clean.id in ids
        assert quarantined.id not in ids

    def test_search_vector_leg_excludes_quarantined(self, manager: MemoryManager) -> None:
        """Vector-resolve guard: a stale embed must not resurface the row."""
        clean = _published(manager, "vector leg control row about omega")
        quarantined = _published(manager, "vector leg dirty row about omega")
        _quarantine(manager, quarantined.id)
        # Lexically unmatchable query → only the vector leg can surface rows.
        hits = manager.search("quokka", project=PROJECT)
        ids = {r.memory.id for r in hits}
        assert clean.id in ids
        assert quarantined.id not in ids

    def test_include_raw_still_excludes_quarantined(self, manager: MemoryManager) -> None:
        quarantined = _published(manager, "raw widening probe about iota")
        _quarantine(manager, quarantined.id)
        hits = manager.search("iota", project=PROJECT, include_raw=True)
        assert quarantined.id not in {r.memory.id for r in hits}

    def test_explicit_status_drill_down_excludes_quarantined(self, manager: MemoryManager) -> None:
        """Explicit ``status=`` does NOT resurrect a quarantined row (§5).

        Quarantined rows carry status='published' and the external
        payloads carry no pipeline_state — an explicit-status leak would
        serve terminal danger-lane content undetectably. The exclusion
        is absolute on BOTH search legs. Direct get-by-id stays the
        documented residual access until the B2 retraction render.
        """
        clean = _published(manager, "drill down control row about kappa")
        quarantined = _published(manager, "drill down dirty row about kappa")
        _quarantine(manager, quarantined.id)

        # FTS leg: the explicit status=published drill-down must not
        # serve the quarantined row.
        hits = manager.search("kappa", project=PROJECT, status=MemoryStatus.PUBLISHED)
        ids = {r.memory.id for r in hits}
        assert clean.id in ids
        assert quarantined.id not in ids

        # Vector leg (lexically unmatchable query → only the vector leg
        # can surface rows): the resolve guard holds the same absolute
        # rule under an explicit status.
        vec_hits = manager.search("quokka", project=PROJECT, status=MemoryStatus.PUBLISHED)
        vec_ids = {r.memory.id for r in vec_hits}
        assert clean.id in vec_ids
        assert quarantined.id not in vec_ids

        # Documented residual: direct get-by-id still returns the row.
        assert manager.get(quarantined.id) is not None

    def test_rest_search_explicit_status_excludes_quarantined(
        self, manager: MemoryManager, rest_client: TestClient
    ) -> None:
        """POST /search {"status": "published"} — same absolute rule."""
        clean = _published(manager, "rest drill control row about rho")
        quarantined = _published(manager, "rest drill dirty row about rho")
        _quarantine(manager, quarantined.id)
        resp = rest_client.post(
            "/search",
            json=SearchQuery(
                query="rho", project=PROJECT, status=MemoryStatus.PUBLISHED
            ).model_dump(),
        )
        assert resp.status_code == 200
        ids = {item["id"] for item in resp.json()}
        assert clean.id in ids
        assert quarantined.id not in ids

    def test_list_recent_excludes_quarantined(
        self, manager: MemoryManager, rest_client: TestClient
    ) -> None:
        """GET /memories drops quarantined rows (default AND explicit status)."""
        quarantined = _published(manager, "listing dirty row about sigma")
        _quarantine(manager, quarantined.id)

        default_ids = {m.id for m in manager.list_recent(project=PROJECT, limit=50)}
        assert quarantined.id not in default_ids
        explicit_ids = {
            m.id
            for m in manager.list_recent(project=PROJECT, status=MemoryStatus.PUBLISHED, limit=50)
        }
        assert quarantined.id not in explicit_ids

        resp = rest_client.get(f"/memories?status=published&project={PROJECT}")
        assert resp.status_code == 200
        assert quarantined.id not in {item["id"] for item in resp.json()}

    def test_issue_context_filter_refuses_quarantined(self, manager: MemoryManager) -> None:
        quarantined = _published(manager, "filter gate probe about lambda")
        _quarantine(manager, quarantined.id)
        result = manager.issue_context_filter(quarantined.id)
        assert result["status"] == "error"
        assert result["reason"] == "status_gate"
        assert "quarantined" in result["error"]

    def test_recall_context_recency_leg_excludes_quarantined(self, manager: MemoryManager) -> None:
        checkpoint_tags = [
            f"project:{PROJECT}",
            f"agent:{AGENT}",
            "mnemos:checkpoint",
        ]
        kept = _published(manager, "checkpoint kept about mu", tags=checkpoint_tags)
        dropped = _published(manager, "checkpoint dropped about nu", tags=checkpoint_tags)
        _quarantine(manager, dropped.id)

        recalled = manager.recall_context(project=PROJECT)
        ids = {m.id for m in recalled}
        assert kept.id in ids
        assert dropped.id not in ids

    def test_agent_recall_recency_leg_excludes_quarantined(self, manager: MemoryManager) -> None:
        """The agent's own recency feed (no query) is an issuance path
        too — terminally quarantined rows must not enter agent context."""
        kept = _published(manager, "agent feed kept row about digamma")
        dropped = _published(manager, "agent feed dirty row about digamma")
        _quarantine(manager, dropped.id)

        results = manager.agent_recall(AgentRecallQuery(agent=AGENT, project=PROJECT, limit=20))
        ids = {r.memory.id for r in results}
        assert kept.id in ids
        assert dropped.id not in ids

    def test_assemble_context_excludes_quarantined(self, manager: MemoryManager) -> None:
        kept = _published(manager, "assembly kept row about xi")
        dropped = _published(manager, "assembly dropped row about omicron")
        _quarantine(manager, dropped.id)

        result = manager.assemble_context(session=SESSION, project=PROJECT)
        ids = {b["memory_id"] for b in result["blocks"]}
        assert kept.id in ids
        assert dropped.id not in ids
        assert "omicron" not in result["text"]

    def test_external_update_schema_has_no_pipeline_state(self) -> None:
        """MemoryUpdate must not carry pipeline lifecycle fields.

        The lifecycle columns are store-internal (whitelisted in
        ``_FIELD_UPDATERS`` for the B2 swap path) — a client-supplied
        pipeline_state would forge provenance. Pydantic's default
        ``extra='ignore'`` silently DROPS unknown keys, so the guard is
        "the field does not exist on the model" (and therefore never
        reaches the update loop), not a construction error.
        """
        lifecycle_fields = (
            "pipeline_state",
            "processed_at",
            "swap_key",
            "quarantine_reason",
            "marker_version",
            # B2a review F5: raw_content joined the store-internal
            # whitelist (§6 zero-loss materialisation at swap time) — it
            # must stay unreachable from external payloads exactly like
            # the rest of the lifecycle group. A client-supplied
            # raw_content would forge the immutable-source invariant.
            "raw_content",
        )
        for field in lifecycle_fields:
            assert field not in MemoryUpdate.model_fields
            assert field not in MemoryCreate.model_fields
        # Even a payload carrying the keys cannot influence the row:
        payload = MemoryUpdate(status=MemoryStatus.PUBLISHED, marker_version=99)  # type: ignore[call-arg]
        assert not hasattr(payload, "marker_version")


# ── 3. Marker contract (§4) ───────────────────────────────────────────────────


class TestMarkerContract:
    def test_render_with_pipeline_phase_and_version(self) -> None:
        mem = Memory(
            id="01234567-89ab-cdef-0123-456789abcdef",
            content="x",
            status=MemoryStatus.PUBLISHED,
            project="proj",
            pipeline_state=PipelineState.REFINED,
            marker_version=7,
        )
        line = build_provenance(mem, _TS)
        assert line == (
            "[mnemos:01234567-89ab-cdef-0123-456789abcdef project=proj "
            "status=published origin=manual pipeline=refined v=7 "
            "retrieved=2026-08-29T12:00:00+00:00]"
        )
        match = MARKER_RE.match(line)
        assert match is not None
        assert match.group("pipeline") == "refined"
        assert match.group("version") == "7"
        assert match.group("origin") == "manual"

    def test_render_null_pipeline_omits_segment(self) -> None:
        mem = Memory(
            id="01234567-89ab-cdef-0123-456789abcdef",
            content="x",
            status=MemoryStatus.PUBLISHED,
            project="proj",
            pipeline_state=None,
            marker_version=1,
        )
        line = build_provenance(mem, _TS)
        assert line == (
            "[mnemos:01234567-89ab-cdef-0123-456789abcdef project=proj "
            "status=published origin=manual v=1 retrieved=2026-08-29T12:00:00+00:00]"
        )
        assert "pipeline=" not in line

    def test_origin_renders_server_column_spoof_ignored(self) -> None:
        """origin= comes from the source COLUMN — never client-mintable input.

        ADR-0025 P0: tags and metadata are caller-controlled; a spoofed
        'origin:federated' tag (or an 'origin' metadata key) must never
        reach the provenance marker — only the server-side source column
        renders.
        """
        mem = Memory(
            id="01234567-89ab-cdef-0123-456789abcdef",
            content="x",
            status=MemoryStatus.PUBLISHED,
            project="proj",
            source=MemorySource.WEB,
            tags=["origin:federated"],
            metadata={"origin": "federated"},
            pipeline_state=None,
            marker_version=1,
        )
        line = build_provenance(mem, _TS)
        match = MARKER_RE.match(line)
        assert match is not None
        assert match.group("origin") == "web"
        assert "federated" not in line

    def test_snapshot_consistency_anti_toctou(self, manager: MemoryManager) -> None:
        """The marker is cut from the SAME snapshot as the projection.

        Mutating the stored row after recall cannot change what the
        already-built marker says: both the bracket string and the
        structured fields derive from one ``Memory`` object.
        """
        mem = _published(manager, "snapshot consistency probe about rho")
        _pipeline_state(manager, mem.id, PipelineState.REFINED)
        manager.sqlite.update_fields(mem.id, marker_version=4)

        result = manager.assemble_context(session=SESSION, project=PROJECT)
        block = next(b for b in result["blocks"] if b["memory_id"] == mem.id)
        match = MARKER_RE.match(block["provenance"])
        assert match is not None
        # Structured fields are the source of truth; the string is their
        # projection — and both agree with the snapshot they were cut from.
        assert block["pipeline_phase"] == "refined"
        assert block["marker_version"] == 4
        assert block["origin"] == mem.source.value == "mcp"
        assert match.group("pipeline") == block["pipeline_phase"]
        assert match.group("version") == str(block["marker_version"])
        assert match.group("origin") == block["origin"]

    def test_legacy_row_block_fields(self, manager: MemoryManager) -> None:
        """A NULL pipeline_state row carries pipeline_phase=None in the
        structured block and omits the segment in the string."""
        mem = _published(manager, "legacy marker probe about upsilon")
        result = manager.assemble_context(session=SESSION, project=PROJECT)
        block = next(b for b in result["blocks"] if b["memory_id"] == mem.id)
        assert block["pipeline_phase"] is None
        assert block["marker_version"] == 1
        assert "pipeline=" not in block["provenance"]

    def test_single_construction_site(self) -> None:
        """No other module formats the bracket marker (grep-pinned).

        If a new issuance path starts hand-formatting ``[mnemos:`` the
        format would drift — the guard keeps assemble.build_provenance
        the single source.
        """
        import subprocess

        repo = Path(__file__).resolve().parent.parent
        out = subprocess.run(
            ["grep", "-rn", "--include=*.py", "-l", r"\[mnemos:{", str(repo / "src")],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        files = {line for line in out.splitlines() if line}
        assert files == {str(repo / "src" / "vesmaro" / "assemble.py")}, files


# ── 4. refined_only (§4) ──────────────────────────────────────────────────────


class TestRefinedOnly:
    def _seed(self, manager: MemoryManager) -> dict[str, str]:
        rows = {
            "refined": _published(manager, "refined phi entry about the widget"),
            "pending": _published(manager, "pending chi entry about the widget"),
            "legacy": _published(manager, "legacy psi entry about the widget"),
        }
        _pipeline_state(manager, rows["refined"].id, PipelineState.REFINED)
        _pipeline_state(manager, rows["pending"].id, PipelineState.PENDING)
        return {k: m.id for k, m in rows.items()}

    def test_manager_refined_only(self, manager: MemoryManager) -> None:
        ids = self._seed(manager)
        hits = manager.search("widget", project=PROJECT, refined_only=True)
        assert {r.memory.id for r in hits} == {ids["refined"]}
        # Default (flag off): everything visible as before.
        hits = manager.search("widget", project=PROJECT)
        assert set(ids.values()) <= {r.memory.id for r in hits}

    def test_refined_only_with_include_raw(self, manager: MemoryManager) -> None:
        ids = self._seed(manager)
        hits = manager.search("widget", project=PROJECT, include_raw=True, refined_only=True)
        assert {r.memory.id for r in hits} == {ids["refined"]}

    def test_rest_search_refined_only(
        self, manager: MemoryManager, rest_client: TestClient
    ) -> None:
        ids = self._seed(manager)
        resp = rest_client.post(
            "/search",
            json=SearchQuery(query="widget", project=PROJECT, refined_only=True).model_dump(),
        )
        assert resp.status_code == 200
        assert [item["id"] for item in resp.json()] == [ids["refined"]]

    def test_mcp_search_refined_only(self, manager: MemoryManager) -> None:
        import asyncio

        import vesmaro.mcp_server as mcp_mod
        from vesmaro.mcp_server import _dispatch

        ids = self._seed(manager)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(mcp_mod, "_manager", manager)
            results = asyncio.new_event_loop().run_until_complete(
                _dispatch(
                    "vesma_search",
                    {"query": "widget", "project": PROJECT, "refined_only": True},
                )
            )
        assert isinstance(results, list)
        assert [item["id"] for item in results] == [ids["refined"]]


# ── 5. N1 — direct flips through the gate ─────────────────────────────────────


class TestN1DirectSeedGate:
    def test_secret_seed_stored_raw_with_audit(self, manager: MemoryManager, caplog) -> None:
        with caplog.at_level("WARNING", logger="vesmaro.manager"):
            mem = _published(manager, f"deploy note with api key {FAKE_AWS_KEY} inline")
        assert mem.status == MemoryStatus.RAW  # demoted, zero-loss
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert stored.status == MemoryStatus.RAW
        assert FAKE_AWS_KEY in stored.content  # content kept
        audit = [r for r in caplog.records if "publish gate" in r.message]
        assert audit and "verdict=refused" in audit[-1].message
        assert "path=direct-seed" in audit[-1].message
        assert "reason=danger-detector" in audit[-1].message
        assert FAKE_AWS_KEY not in audit[-1].message  # raw values never logged

    def test_injection_seed_stored_raw(self, manager: MemoryManager) -> None:
        mem = _published(manager, "notes mentioning <|im_start|> system payload")
        assert mem.status == MemoryStatus.RAW

    def test_scanner_error_fail_closed(self, manager: MemoryManager, monkeypatch) -> None:
        monkeypatch.setattr(
            "vesmaro.manager.detect",
            lambda content, title=None: DetectionResult(error="boom"),
        )
        mem = _published(manager, "clean content but scanner is down")
        assert mem.status == MemoryStatus.RAW

    def test_clean_seed_publishes_with_pass_audit(self, manager: MemoryManager, caplog) -> None:
        with caplog.at_level("INFO", logger="vesmaro.manager"):
            mem = _published(manager, "clean direct seed about dorian")
        assert mem.status == MemoryStatus.PUBLISHED
        audit = [r for r in caplog.records if "publish gate" in r.message]
        assert audit and "verdict=pass" in audit[-1].message
        assert "path=direct-seed" in audit[-1].message


class TestN1UpdateGate:
    def _raw(self, manager: MemoryManager, content: str) -> Memory:
        return _published(manager, content, status=MemoryStatus.RAW)

    def test_clean_flip_publishes(self, manager: MemoryManager) -> None:
        mem = self._raw(manager, "flip target clean content")
        updated = manager.update(mem.id, MemoryUpdate(status=MemoryStatus.PUBLISHED))
        assert updated is not None
        assert updated.status == MemoryStatus.PUBLISHED

    def test_secret_flip_refused_status_unchanged_content_saved(
        self, manager: MemoryManager, caplog
    ) -> None:
        mem = self._raw(manager, f"token {FAKE_GITHUB_TOKEN} inline")
        with caplog.at_level("WARNING", logger="vesmaro.manager"):
            updated = manager.update(mem.id, MemoryUpdate(status=MemoryStatus.PUBLISHED))
        assert updated is not None
        assert updated.status == MemoryStatus.RAW  # stayed previous
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert FAKE_GITHUB_TOKEN in stored.content  # zero-loss
        audit = [r for r in caplog.records if "publish gate" in r.message]
        assert audit and "path=status-flip" in audit[-1].message
        assert "verdict=refused" in audit[-1].message

    def test_flip_with_new_dirty_content_from_raw_stays_raw(self, manager: MemoryManager) -> None:
        mem = self._raw(manager, "original clean body")
        updated = manager.update(
            mem.id,
            MemoryUpdate(
                content=f"replaced with {FAKE_AWS_KEY}",
                status=MemoryStatus.PUBLISHED,
            ),
        )
        assert updated is not None
        # (b) dominates from a non-published row: status stays previous.
        assert updated.status == MemoryStatus.RAW
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert FAKE_AWS_KEY in stored.content

    def test_clean_content_edit_of_published_stays_published(self, manager: MemoryManager) -> None:
        mem = _published(manager, "published body before edit")
        updated = manager.update(mem.id, MemoryUpdate(content="edited clean body"))
        assert updated is not None
        assert updated.status == MemoryStatus.PUBLISHED

    def test_dirty_content_edit_of_published_demotes_to_raw(
        self, manager: MemoryManager, caplog
    ) -> None:
        mem = _published(manager, "published body awaiting a dirty edit")
        with caplog.at_level("WARNING", logger="vesmaro.manager"):
            updated = manager.update(mem.id, MemoryUpdate(content=f"edited in {FAKE_AWS_KEY}"))
        assert updated is not None
        assert updated.status == MemoryStatus.RAW  # demoted
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert FAKE_AWS_KEY in stored.content  # zero-loss: content kept
        audit = [r for r in caplog.records if "publish gate" in r.message]
        assert audit and "path=published-content-edit" in audit[-1].message

    def test_dirty_edit_demotion_drops_stale_vector(
        self, manager: MemoryManager, monkeypatch
    ) -> None:
        mem = _published(manager, "vector hygiene probe published body")
        deleted: list[str] = []
        monkeypatch.setattr(manager.vectors, "delete", lambda mid: deleted.append(mid))
        manager.update(mem.id, MemoryUpdate(content=f"now carries {FAKE_AWS_KEY}"))
        assert deleted == [mem.id]

    def test_scanner_error_on_flip_fail_closed(self, manager: MemoryManager, monkeypatch) -> None:
        mem = self._raw(manager, "flip under scanner outage")
        monkeypatch.setattr(
            "vesmaro.manager.detect",
            lambda content, title=None: DetectionResult(error="down"),
        )
        updated = manager.update(mem.id, MemoryUpdate(status=MemoryStatus.PUBLISHED))
        assert updated is not None
        assert updated.status == MemoryStatus.RAW

    def test_noop_flip_does_not_regate_legacy_dirty_row(self, manager: MemoryManager) -> None:
        """status=PUBLISHED on an already-published row with NO content
        change is a no-op — nothing about the served projection changes,
        so the gate does not fire (pinned decision; the row itself is a
        pre-gate legacy state in this fixture)."""
        mem = _published(manager, "legacy published body")
        manager.sqlite.update_fields(mem.id, content=f"legacy {FAKE_AWS_KEY} body")
        updated = manager.update(mem.id, MemoryUpdate(status=MemoryStatus.PUBLISHED))
        assert updated is not None
        assert updated.status == MemoryStatus.PUBLISHED

    def test_dirty_title_only_edit_of_published_demotes_to_raw(
        self, manager: MemoryManager, caplog
    ) -> None:
        """Title-only edit IS gated (review F3).

        The title is part of the served projection (markers/echo paths
        render it) and the issuance scan does NOT screen titles for
        injections — the N1 gate is the only guard. Refusal follows the
        (c) convention: the new title is stored zero-loss, the row is
        demoted to RAW, audit path=published-content-edit.
        """
        mem = _published(manager, "published body with a dirty title edit coming")
        dirty_title = "notes <|im_start|> system override"
        with caplog.at_level("WARNING", logger="vesmaro.manager"):
            updated = manager.update(mem.id, MemoryUpdate(title=dirty_title))
        assert updated is not None
        assert updated.status == MemoryStatus.RAW  # demoted
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert stored.title == dirty_title  # zero-loss: title kept
        assert dirty_title not in stored.content  # body untouched
        audit = [r for r in caplog.records if "publish gate" in r.message]
        assert audit and "path=published-content-edit" in audit[-1].message
        assert "verdict=refused" in audit[-1].message

    def test_clean_title_only_edit_of_published_stays_published(
        self, manager: MemoryManager
    ) -> None:
        """A clean title-only edit re-gates and passes — no behavior
        regression on the benign path."""
        mem = _published(manager, "published body with a clean title edit coming")
        updated = manager.update(mem.id, MemoryUpdate(title="Clean heading"))
        assert updated is not None
        assert updated.status == MemoryStatus.PUBLISHED
        assert updated.title == "Clean heading"


# ── 6. B2a — pipeline entry at publication (§10-B) ────────────────────────────


class TestPublishPipelineEntry:
    def test_first_publication_enqueues_pending(self, manager: MemoryManager, caplog) -> None:
        mem = _published(manager, "pipeline entry body about aleph", status=MemoryStatus.RAW)
        assert manager.sqlite.get(mem.id).pipeline_state is None  # pre-condition
        with caplog.at_level("INFO", logger="vesmaro.pipeline.publish"):
            result = manager.publish(mem.id, skip_quality_check=True)
        assert result.published is True
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert stored.pipeline_state == PipelineState.PENDING
        audit = [r for r in caplog.records if "outcome=enqueued" in r.message]
        assert audit and "from=none" in audit[-1].message

    def test_republish_keeps_refined(self, manager: MemoryManager) -> None:
        mem = _published(manager, "converged row body about beth")
        manager.sqlite.update_fields(
            mem.id, pipeline_state=PipelineState.REFINED, swap_key="key-1", marker_version=4
        )
        result = manager.publish(mem.id, skip_quality_check=True)
        assert result.published is True
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert stored.pipeline_state == PipelineState.REFINED
        assert stored.swap_key == "key-1"
        assert stored.marker_version == 4

    def test_republish_of_failed_starts_fresh_cycle(self, manager: MemoryManager, caplog) -> None:
        mem = _published(manager, "failed row body about gimel", status=MemoryStatus.RAW)
        manager.publish(mem.id, skip_quality_check=True)
        # Exhaust the retry budget, then manually re-publish.
        manager.sqlite.record_refine_failure(mem.id, attempt=3, next_retry_at=None)
        with caplog.at_level("INFO", logger="vesmaro.pipeline.publish"):
            result = manager.publish(mem.id, skip_quality_check=True)
        assert result.published is True
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert stored.pipeline_state == PipelineState.PENDING
        assert stored.metadata["pipeline_retry_count"] == 0  # fresh cycle
        audit = [r for r in caplog.records if "outcome=enqueued" in r.message]
        assert audit and "from=failed" in audit[-1].message


# ── 7. B2a — the §6 swap (success with artifact) ──────────────────────────────


class TestRefineSwap:
    def test_full_cycle_pending_to_refined_swap(self, manager: MemoryManager, caplog) -> None:
        """The ADR-0019 §6 contract: same id, stable rowid, byte-identical
        raw_content, clean_content reset, marker_version bumped, FTS finds
        the NEW tokens, vector re-embedded after the commit."""
        target = _published(manager, "swapped member one zebra unique")
        mate = _published(manager, "cluster mate two quokka unique")
        raw_source = "original source payload of member one"
        manager.sqlite.update_fields(
            target.id,
            cluster_id="cl-b2a",
            raw_content=raw_source,
            clean_content="stale filtered projection of one",
        )
        manager.sqlite.update_fields(mate.id, cluster_id="cl-b2a")
        _pipeline_state(manager, target.id, PipelineState.PENDING)

        def _rowid(mid: str) -> int:
            row = (
                manager.sqlite._get_conn()
                .execute("SELECT rowid FROM memories WHERE id=?", (mid,))
                .fetchone()
            )
            return int(row[0])

        rowid_before = _rowid(target.id)
        manager.vectors.delete(target.id)  # embed must come back after the swap
        fts_before = [m.id for m, _ in manager.sqlite.fts_search("quokka", project=PROJECT)]
        assert target.id not in fts_before  # new token not indexed yet
        assert mate.id in fts_before

        with caplog.at_level("INFO", logger="vesmaro.pipeline.refine"):
            summary = manager.refine_pending()

        assert summary["refined"] == 1
        swapped = manager.sqlite.get(target.id)
        assert swapped is not None
        # Identity: same id, same rowid (FTS external-content rowid stable).
        assert swapped.id == target.id
        assert _rowid(target.id) == rowid_before
        # §6 swap semantics.
        assert swapped.pipeline_state == PipelineState.REFINED
        assert swapped.processed_at is not None
        assert swapped.swap_key is not None
        assert swapped.clean_content is None  # else effective_content() masks the swap
        assert swapped.marker_version == target.marker_version + 1
        assert swapped.effective_content() == swapped.content  # the swap is served
        assert "quokka" in swapped.content  # the artifact carries the mate's text
        # Zero-loss: raw_content byte-identical, never rewritten.
        assert swapped.raw_content == raw_source
        # Status untouched — visibility is owned by MemoryStatus.
        assert swapped.status == MemoryStatus.PUBLISHED
        # FTS reindexed the swapped projection: NEW tokens find the row.
        fts_after = [m.id for m, _ in manager.sqlite.fts_search("quokka", project=PROJECT)]
        assert target.id in fts_after
        hits = manager.search("quokka", project=PROJECT)
        assert target.id in {r.memory.id for r in hits}
        # Vector re-embedded AFTER the commit, stamped with freshness hash.
        assert manager.vectors.has(target.id)
        meta = manager.vectors.get_metadata([target.id])[target.id]
        assert meta["content_hash"] == manager._embed_content_hash(manager._embedding_text(swapped))
        audit = [r for r in caplog.records if "outcome=refined" in r.message]
        assert audit and "attempt=1" in audit[-1].message

    def test_double_swap_noop_by_swap_key(self, manager: MemoryManager, monkeypatch) -> None:
        target = _published(manager, "idempotency member one sigma unique")
        mate = _published(manager, "idempotency mate two tau unique")
        manager.sqlite.update_fields(target.id, cluster_id="cl-idem")
        manager.sqlite.update_fields(mate.id, cluster_id="cl-idem")
        _pipeline_state(manager, target.id, PipelineState.PENDING)

        # Spy on the swap writer FROM THE FIRST RUN. §6 atomicity: the
        # real swap is ONE update_fields call = ONE transaction carrying
        # content + clean_content reset + lifecycle columns together —
        # a split write (e.g. clean_content moved to a second call)
        # would open a window where the swapped content is masked by a
        # stale clean_content in effective_content().
        calls: list[dict] = []
        original = manager.sqlite.update_fields

        def _spy(memory_id: str, **kwargs: object) -> bool:
            calls.append({"memory_id": memory_id, **kwargs})
            return original(memory_id, **kwargs)  # type: ignore[arg-type]

        swap_columns = (
            "content",
            "clean_content",
            "pipeline_state",
            "processed_at",
            "swap_key",
            "marker_version",
        )

        monkeypatch.setattr(manager.sqlite, "update_fields", _spy)
        try:
            assert manager.refine_pending()["refined"] == 1
        finally:
            monkeypatch.setattr(manager.sqlite, "update_fields", original)
        first = manager.sqlite.get(target.id)
        assert first is not None

        # FIRST run — the full swap branch: exactly ONE update_fields
        # call touching any swap column, and it carries ALL of them.
        first_swap_calls = [
            c for c in calls if c["memory_id"] == target.id and any(k in c for k in swap_columns)
        ]
        assert len(first_swap_calls) == 1, first_swap_calls
        for column in swap_columns:
            assert column in first_swap_calls[0], (
                f"§6 atomicity broken: {column} missing from the swap transaction"
            )
        assert first_swap_calls[0]["content"] != target.content  # a real swap happened

        # Re-enqueue (through the original writer — the spy stays clean)
        # and re-run with the SAME artifact: the second run must collapse
        # by swap_key — ONLY the lifecycle columns, never a content
        # rewrite (which would re-fire the FTS trigger for nothing and
        # could race a concurrent projection edit).
        original(target.id, pipeline_state=PipelineState.PENDING)
        calls.clear()
        monkeypatch.setattr(manager.sqlite, "update_fields", _spy)
        try:
            summary = manager.refine_pending()
        finally:
            monkeypatch.setattr(manager.sqlite, "update_fields", original)
        second = manager.sqlite.get(target.id)
        assert second is not None
        assert summary["refined"] == 1
        swap_writes = [c for c in calls if "swap_key" in c]
        assert len(swap_writes) == 1
        assert swap_writes[0]["memory_id"] == target.id
        assert "content" not in swap_writes[0]  # no rewrite on a swap_key hit
        assert "clean_content" not in swap_writes[0]
        assert "marker_version" not in swap_writes[0]
        assert "raw_content" not in swap_writes[0]
        # Row-level convergence.
        assert second.content == first.content
        assert second.marker_version == first.marker_version  # NOT incremented
        assert second.swap_key == first.swap_key
        assert second.raw_content == first.raw_content
        assert second.pipeline_state == PipelineState.REFINED

    def test_marker_version_not_bumped_when_artifact_equals_projection(
        self, manager: MemoryManager, monkeypatch
    ) -> None:
        """The version guard: marker_version grows ONLY on a REAL content
        change of the served projection. A re-processing whose artifact
        equals the currently-served text goes through the swap branch
        (lifecycle columns + swap_key written) but must NOT bump the
        version — consumers desync-detect through it, so an idle bump
        would cry wolf."""
        mem = _published(manager, "identical artifact body about allegheny")
        _pipeline_state(manager, mem.id, PipelineState.PENDING)
        monkeypatch.setattr(
            refine_mod,
            "_produce_refined_projection",
            lambda mgr, memory: memory.effective_content(),  # same text, no improvement
        )
        summary = manager.refine_pending()
        assert summary["refined"] == 1  # the SWAP branch ran (artifact exists)
        assert summary["refined_noop"] == 0
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert stored.marker_version == mem.marker_version  # NOT bumped
        assert stored.content == mem.content  # text unchanged
        assert stored.pipeline_state == PipelineState.REFINED
        assert stored.swap_key is not None  # swap bookkeeping still written
        assert stored.clean_content is None

    def test_swap_commit_and_embed_upsert_audit_events(
        self, manager: MemoryManager, caplog
    ) -> None:
        """ADR-0019 §Swap audit contract (review F4): the swap commit point
        emits ``swap_committed`` with the OLD and NEW content revision
        hashes, and the single embedding write point emits
        ``embed_upserted`` binding the stamped content_hash to the actual
        upsert. Hashes only — never raw content."""
        target = _published(manager, "audit event member one zebra unique")
        mate = _published(manager, "audit event mate two quokka unique")
        manager.sqlite.update_fields(target.id, cluster_id="cl-audit")
        manager.sqlite.update_fields(mate.id, cluster_id="cl-audit")
        _pipeline_state(manager, target.id, PipelineState.PENDING)
        seeded = manager.sqlite.get(target.id)
        assert seeded is not None
        pre_swap_projection = seeded.effective_content()

        with caplog.at_level("INFO"):
            summary = manager.refine_pending()
        assert summary["refined"] == 1
        swapped = manager.sqlite.get(target.id)
        assert swapped is not None

        # swap_committed: one event, at the commit, with both revisions.
        swap_events = [r.getMessage() for r in caplog.records if "swap_committed" in r.message]
        assert len(swap_events) == 1
        match = re.search(
            r"swap_committed: id=(\S+) old_revision=([0-9a-f]{16}) new_revision=([0-9a-f]{16})",
            swap_events[0],
        )
        assert match is not None, swap_events[0]
        assert match.group(1) == target.id[:8]
        old_rev, new_rev = match.group(2), match.group(3)
        assert old_rev != new_rev
        assert old_rev == hashlib.sha256(pre_swap_projection.encode()).hexdigest()[:16]
        assert new_rev == hashlib.sha256(swapped.content.encode()).hexdigest()[:16]
        # Audit carries hashes only — never the raw content.
        assert pre_swap_projection not in swap_events[0]
        assert swapped.content[:40] not in swap_events[0]

        # embed_upserted: the content_hash bound to the actual upsert.
        embed_events = [r.getMessage() for r in caplog.records if "embed_upserted" in r.message]
        target_events = [m for m in embed_events if f"id={target.id[:8]}" in m]
        assert len(target_events) == 1, embed_events
        embed_match = re.search(
            r"embed_upserted: id=\S+ content_hash=([0-9a-f]{16})", target_events[0]
        )
        assert embed_match is not None, target_events[0]
        assert (
            embed_match.group(1)
            == manager.vectors.get_metadata([target.id])[target.id]["content_hash"]
        )
        assert embed_match.group(1) == manager._embed_content_hash(manager._embedding_text(swapped))


# ── 8. B2a — refined-noop (success without artifact) ──────────────────────────


class TestRefineNoop:
    def test_lone_record_transitions_without_content_mutation(
        self, manager: MemoryManager, caplog
    ) -> None:
        mem = _published(manager, "lone record body about psi — nothing to improve")
        _pipeline_state(manager, mem.id, PipelineState.PENDING)
        with caplog.at_level("INFO", logger="vesmaro.pipeline.refine"):
            summary = manager.refine_pending()
        assert summary["refined_noop"] == 1
        assert summary["refined"] == 0
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert stored.pipeline_state == PipelineState.REFINED
        assert stored.processed_at is not None
        assert stored.content == mem.content  # untouched — honest "nothing to improve"
        assert stored.swap_key is None  # no swap was performed
        assert stored.marker_version == mem.marker_version  # version not grown
        audit = [r for r in caplog.records if "outcome=refined-noop" in r.message]
        assert audit and "reason=no-artifact" in audit[-1].message

    def test_cluster_of_invisible_mates_is_noop(self, manager: MemoryManager) -> None:
        """Cluster members still in the (untouched) legacy flow are not an
        admissible refinement context — the stub refuses to merge raw
        members into a visible projection."""
        mem = _published(manager, "visible member about omega")
        raw_mate = _published(manager, "raw mate about omega", status=MemoryStatus.RAW)
        manager.sqlite.update_fields(mem.id, cluster_id="cl-mixed")
        manager.sqlite.update_fields(raw_mate.id, cluster_id="cl-mixed")
        _pipeline_state(manager, mem.id, PipelineState.PENDING)
        summary = manager.refine_pending()
        assert summary["refined_noop"] == 1
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert stored.content == mem.content
        assert raw_mate.status == MemoryStatus.RAW  # legacy flow untouched


# ── 9. B2a — lane (a): quality/infra failure with bounded retry ───────────────


class TestRefineFailedLane:
    def _failing_producer(self, manager: MemoryManager) -> Memory:
        mem = _published(manager, "failing lane body about dorian")
        _pipeline_state(manager, mem.id, PipelineState.PENDING)
        return mem

    def test_failure_stays_visible_raw_with_backoff_and_audit(
        self, manager: MemoryManager, monkeypatch, caplog
    ) -> None:
        mem = self._failing_producer(manager)

        def _boom(mgr: MemoryManager, memory: Memory) -> str | None:
            raise RuntimeError("stub outage")

        monkeypatch.setattr(refine_mod, "_produce_refined_projection", _boom)
        with caplog.at_level("WARNING", logger="vesmaro.pipeline.refine"):
            summary = manager.refine_pending()

        assert summary["refine_failed"] == 1
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert stored.status == MemoryStatus.PUBLISHED  # visible raw
        assert stored.content == mem.content  # projection untouched
        assert stored.pipeline_state == PipelineState.FAILED
        assert stored.metadata["pipeline_retry_count"] == 1
        # Backoff scheduled in the future → the daemon does not spin.
        retry_at = datetime.fromisoformat(stored.metadata["pipeline_retry_at"])
        assert retry_at > datetime(2026, 1, 1, tzinfo=UTC)
        audit = [r for r in caplog.records if "outcome=failed" in r.message]
        assert audit and "attempt=1" in audit[-1].message
        assert manager.sqlite.list_refine_intake() == []  # backoff blocks re-pick
        assert manager.sqlite.claim_for_refinement(mem.id) is False

    def test_retry_budget_exhausts_to_stable_failure(
        self, manager: MemoryManager, monkeypatch
    ) -> None:
        mem = self._failing_producer(manager)

        def _boom(mgr: MemoryManager, memory: Memory) -> str | None:
            raise RuntimeError("stub outage")

        monkeypatch.setattr(refine_mod, "_produce_refined_projection", _boom)
        past = "2000-01-01T00:00:00+00:00"
        assert manager.refine_pending()["refine_failed"] == 1  # attempt 1
        for expected_attempt in (2, 3):
            # Simulate elapsed backoff (tests never sleep).
            manager.sqlite.record_refine_failure(
                mem.id, attempt=expected_attempt - 1, next_retry_at=past
            )
            summary = manager.refine_pending()
            assert summary["refine_failed"] == 1
            stored = manager.sqlite.get(mem.id)
            assert stored is not None
            assert stored.metadata["pipeline_retry_count"] == expected_attempt

        stable = manager.sqlite.get(mem.id)
        assert stable is not None
        assert stable.metadata["pipeline_retry_at"] == ""  # no retry scheduled
        # Stable failure never re-enters the queue — even with a past
        # retry_at the exhausted counter gates the intake.
        manager.sqlite.record_refine_failure(mem.id, attempt=3, next_retry_at=past)
        assert manager.sqlite.list_refine_intake() == []
        assert manager.sqlite.claim_for_refinement(mem.id) is False
        assert manager.stats()["processor"]["refine_queue_depth"] == 0


# ── 10. B2a — lane (b): danger in the PROCESSED projection, terminal ─────────


class TestRefineQuarantineLane:
    def _pending(self, manager: MemoryManager, content: str) -> Memory:
        mem = _published(manager, content)
        _pipeline_state(manager, mem.id, PipelineState.PENDING)
        return mem

    def test_secret_introduced_by_processing_quarantines(
        self, manager: MemoryManager, monkeypatch, caplog
    ) -> None:
        mem = self._pending(manager, "danger lane body about eta")
        manager.vectors.upsert(mem.id, [0.1] * 384, {"project": PROJECT, "agent": AGENT})

        def _dirty(mgr: MemoryManager, memory: Memory) -> str | None:
            return f"processed projection carries {FAKE_AWS_KEY} inline"

        monkeypatch.setattr(refine_mod, "_produce_refined_projection", _dirty)
        with caplog.at_level("WARNING", logger="vesmaro.pipeline.refine"):
            summary = manager.refine_pending()

        assert summary["quarantined"] == 1
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert stored.pipeline_state == PipelineState.QUARANTINED
        assert stored.quarantine_reason == "secret"  # detector class code
        assert stored.status == MemoryStatus.PUBLISHED  # statuses untouched
        assert stored.content == mem.content  # the raw projection stays stored
        assert manager.vectors.has(mem.id) is False  # embed dropped
        # Excluded from issuance (B1 predicate) on the FTS leg.
        assert mem.id not in {r.memory.id for r in manager.search("eta", project=PROJECT)}
        audit = [r for r in caplog.records if "outcome=quarantined" in r.message]
        assert audit and "reason=secret" in audit[-1].message
        assert FAKE_AWS_KEY not in audit[-1].message  # raw values never logged
        # Terminal: the daemon never re-picks the row.
        assert manager.sqlite.list_refine_intake() == []
        assert manager.refine_pending()["considered"] == 0

    def test_detector_error_quarantines_fail_closed_not_retry(
        self, manager: MemoryManager, monkeypatch
    ) -> None:
        """§5 ambiguity: a scanner/detector error during processing is a
        quarantine, NOT a lane-(a) retry — an unreliable scanner must not
        keep re-serving the row as visible-raw."""
        mem = self._pending(manager, "ambiguity lane body about theta")
        monkeypatch.setattr(
            refine_mod,
            "_produce_refined_projection",
            lambda mgr, memory: "clean-looking artifact",
        )
        monkeypatch.setattr(
            refine_mod,
            "detect",
            lambda content, title=None: DetectionResult(error="scanner down"),
        )
        summary = manager.refine_pending()
        assert summary["quarantined"] == 1
        assert summary["refine_failed"] == 0  # not retried
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert stored.pipeline_state == PipelineState.QUARANTINED
        assert stored.quarantine_reason == refine_mod.QUARANTINE_REASON_DETECTOR_ERROR
        assert stored.metadata.get("pipeline_retry_count") is None  # no retry cycle

    def test_quarantine_entry_drops_embed_and_never_touches_status(
        self, manager: MemoryManager
    ) -> None:
        mem = _published(manager, "manual quarantine body about iota")
        manager.vectors.upsert(mem.id, [0.1] * 384, {"project": PROJECT, "agent": AGENT})
        assert manager.quarantine_entry(mem.id, reason="prompt-injection", source="test")
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert stored.pipeline_state == PipelineState.QUARANTINED
        assert stored.quarantine_reason == "prompt-injection"
        assert stored.status == MemoryStatus.PUBLISHED  # statuses untouched
        assert manager.vectors.has(mem.id) is False
        assert manager.quarantine_entry("no-such-id", reason="secret") is False


# ── 11. B2a — competitive claim + quarantine release ──────────────────────────


class TestClaimAndRelease:
    def test_second_worker_claim_is_noop(self, manager: MemoryManager) -> None:
        mem = _published(manager, "claim race body about kappa")
        _pipeline_state(manager, mem.id, PipelineState.PENDING)
        assert manager.sqlite.claim_for_refinement(mem.id) is True  # first worker
        assert manager.sqlite.claim_for_refinement(mem.id) is False  # second: no-op
        assert refine_single(manager, mem.id) == "lost-race"
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert stored.pipeline_state == PipelineState.PROCESSING  # untouched by the loser

    def test_release_quarantine_returns_to_failed_for_new_cycle(
        self, manager: MemoryManager, caplog
    ) -> None:
        mem = _published(manager, "release flow body about lambda")
        _quarantine(manager, mem.id, "secret")
        # Terminality guard: release is the only exit.
        with caplog.at_level("WARNING", logger="vesmaro.manager"):
            assert manager.release_quarantine(mem.id) is True
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert stored.pipeline_state == PipelineState.FAILED  # not refined/pending
        assert stored.quarantine_reason is None
        assert stored.metadata["pipeline_retry_count"] == 0  # fresh cycle
        audit = [r for r in caplog.records if "outcome=quarantine-released" in r.message]
        assert audit and "to=failed" in audit[-1].message
        # The failed row re-enters the daemon queue and completes a cycle.
        assert manager.sqlite.list_refine_intake()
        summary = manager.refine_pending()
        assert summary["considered"] == 1
        after = manager.sqlite.get(mem.id)
        assert after is not None
        assert after.pipeline_state == PipelineState.REFINED

    def test_release_refuses_non_quarantined(self, manager: MemoryManager) -> None:
        mem = _published(manager, "release refusal body about mu")
        assert manager.release_quarantine(mem.id) is False
        assert manager.release_quarantine("no-such-id") is False

    def test_rest_release_endpoint(self, manager: MemoryManager, rest_client: TestClient) -> None:
        mem = _published(manager, "rest release body about nu")
        _quarantine(manager, mem.id, "secret")
        resp = rest_client.post(f"/memories/{mem.id}/quarantine/release")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "released"
        assert body["pipeline_state"] == "failed"
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert stored.pipeline_state == PipelineState.FAILED
        # Not quarantined anymore → 404 on a second release.
        assert rest_client.post(f"/memories/{mem.id}/quarantine/release").status_code == 404
        assert rest_client.post("/memories/no-such-id/quarantine/release").status_code == 404


# ── 12. B2a — daemon intake, queue statistics, sweeper, rebuild ───────────────


class TestDaemonIntakeAndStats:
    def test_run_pipeline_drains_both_queues(self, manager: MemoryManager) -> None:
        raw = _published(manager, "unique standalone passthrough body", status=MemoryStatus.RAW)
        pending = _published(manager, "pending row awaiting refinement about xi")
        _pipeline_state(manager, pending.id, PipelineState.PENDING)

        summary = manager.run_pipeline()

        # Legacy flow: the raw row was promoted (and its publication
        # entered the pipeline in the same cycle).
        assert summary["single_promoted"] == 1
        assert manager.sqlite.get(raw.id).status == MemoryStatus.PUBLISHED
        # Refine queue: the manually-pending row AND the just-published
        # passthrough row both completed a (noop) cycle.
        assert summary["refined_noop"] == 2
        assert summary["refined"] == 0
        states = manager.sqlite.count_by_pipeline_state()
        assert states.get("pending", 0) == 0
        assert manager.stats()["processor"]["queue_depth"] == 0  # both queues empty

    def test_stats_counts_both_queues(self, manager: MemoryManager) -> None:
        for i in range(2):
            mem = _published(manager, f"stats probe row {i} about omicron")
            _pipeline_state(manager, mem.id, PipelineState.PENDING)
        legacy = _published(manager, "legacy raw probe", status=MemoryStatus.RAW)
        assert legacy.status == MemoryStatus.RAW
        stats = manager.stats()["processor"]
        assert stats["legacy_queue_depth"] == 1
        assert stats["refine_queue_depth"] == 2
        assert stats["queue_depth"] == 3  # both queues
        assert stats["pipeline_states"]["pending"] == 2

    def test_legacy_null_rows_never_enter_refine_intake(self, manager: MemoryManager) -> None:
        _published(manager, "legacy raw null row", status=MemoryStatus.RAW)
        _published(manager, "legacy published null row about pi")
        assert manager.sqlite.list_refine_intake() == []
        assert manager.stats()["processor"]["refine_queue_depth"] == 0

    def test_stable_failed_row_not_queued(self, manager: MemoryManager) -> None:
        mem = _published(manager, "stable failed stats probe about rho")
        manager.sqlite.record_refine_failure(mem.id, attempt=3, next_retry_at=None)
        stats = manager.stats()["processor"]
        assert stats["refine_queue_depth"] == 0
        assert stats["pipeline_states"]["failed"] == 1


class TestSweeperAndRebuild:
    def test_heal_fixes_stale_refined_embed(self, manager: MemoryManager) -> None:
        mem = _published(manager, "sweeper target body about sigma")
        _pipeline_state(manager, mem.id, PipelineState.REFINED)
        manager.vectors.upsert(
            mem.id,
            [0.3] * 384,
            {"project": PROJECT, "agent": AGENT, "content_hash": "stale"},
        )
        result = manager.heal_stale_embeddings()
        assert result["healed"] == 1
        meta = manager.vectors.get_metadata([mem.id])[mem.id]
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert meta["content_hash"] == manager._embed_content_hash(manager._embedding_text(stored))

    def test_heal_restores_missing_embed_and_skips_fresh(self, manager: MemoryManager) -> None:
        fresh = _published(manager, "fresh embed row about tau")
        _pipeline_state(manager, fresh.id, PipelineState.REFINED)
        # Seed a FRESH embed (correct content_hash) for the fresh row.
        fresh_row = manager.sqlite.get(fresh.id)
        assert fresh_row is not None
        manager.vectors.upsert(fresh.id, [0.1] * 384, manager._vector_metadata(fresh_row))
        missing = _published(manager, "missing embed row about upsilon")
        _pipeline_state(manager, missing.id, PipelineState.REFINED)
        manager.vectors.delete(missing.id)
        result = manager.heal_stale_embeddings()
        assert result["healed"] == 1  # only the missing one
        assert manager.vectors.has(missing.id)

    def test_heal_never_touches_quarantined(self, manager: MemoryManager) -> None:
        mem = _published(manager, "quarantined sweeper probe about phi")
        _pipeline_state(manager, mem.id, PipelineState.REFINED)
        _quarantine(manager, mem.id, "secret")
        # Simulate drift: a stale embed re-appeared for the quarantined id.
        manager.vectors.upsert(
            mem.id, [0.5] * 384, {"project": PROJECT, "agent": AGENT, "content_hash": "stale"}
        )
        result = manager.heal_stale_embeddings()
        assert result["healed"] == 0
        assert manager.vectors.get_metadata([mem.id])[mem.id]["content_hash"] == "stale"

    def test_rebuild_vector_index_skips_quarantined(self, manager: MemoryManager) -> None:
        clean = _published(manager, "rebuild clean row about chi")
        dirty = _published(manager, "rebuild dirty row about psi")
        # The daemon-path quarantine (embed dropped with the row excluded).
        assert manager.quarantine_entry(dirty.id, reason="secret", source="test")
        assert manager.vectors.has(dirty.id) is False
        result = manager.rebuild_vector_index()
        assert result["skipped_quarantined"] == 1
        assert result["indexed"] == 1
        assert manager.vectors.has(clean.id)  # re-embedded with freshness stamp
        meta = manager.vectors.get_metadata([clean.id])[clean.id]
        stored = manager.sqlite.get(clean.id)
        assert stored is not None
        assert meta["content_hash"] == manager._embed_content_hash(manager._embedding_text(stored))
        # The quarantined row gained no embed from the rebuild.
        assert manager.vectors.has(dirty.id) is False


class TestSweeperVintageFingerprint:
    """ADR-0021 round-3 swap: ``model_fingerprint`` freshness semantics.

    After a weights swap the content is unchanged, so the content-hash
    leg alone would call old-geometry vectors fresh forever — the
    vintage key is what forces the re-embed. Quarantined rows stay
    absolutely excluded from the migration.
    """

    def test_metadata_stamps_model_fingerprint(self, manager: MemoryManager) -> None:
        mem = _published(manager, "vintage stamp probe about omega")
        manager._embedder.fingerprint = "nano:sha256:testweights"
        meta = manager._vector_metadata(mem)
        assert meta["model_fingerprint"] == "nano:sha256:testweights"
        assert "content_hash" in meta  # both freshness keys present

    def test_heal_reembeds_on_fingerprint_mismatch(self, manager: MemoryManager) -> None:
        """Same content, OTHER embedder geometry ⇒ stale ⇒ re-embed."""
        mem = _published(manager, "vintage mismatch probe about alpha")
        _pipeline_state(manager, mem.id, PipelineState.REFINED)
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        # Pre-swap vector: correct content_hash, OLD fingerprint.
        manager._embedder.fingerprint = "nano:sha256:oldweights"
        manager.vectors.upsert(mem.id, [0.2] * 384, manager._vector_metadata(stored))
        manager._embedder.fingerprint = "nano:sha256:newweights"  # the swap
        result = manager.heal_stale_embeddings()
        assert result["healed"] == 1
        assert result["stale_by_fingerprint"] == 1
        meta = manager.vectors.get_metadata([mem.id])[mem.id]
        assert meta["model_fingerprint"] == "nano:sha256:newweights"

    def test_heal_reembeds_on_missing_fingerprint(self, manager: MemoryManager) -> None:
        """Pre-round-3 rows carry no vintage key at all ⇒ stale ⇒ re-embed.

        This is the actual upgrade path: every vector written before the
        fingerprint stamp must migrate, not just mismatched ones.
        """
        mem = _published(manager, "missing fingerprint probe about beta")
        _pipeline_state(manager, mem.id, PipelineState.REFINED)
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        manager._embedder.fingerprint = "nano:sha256:newweights"
        expected_hash = manager._embed_content_hash(manager._embedding_text(stored))
        manager.vectors.upsert(
            mem.id,
            [0.2] * 384,
            {"project": PROJECT, "agent": AGENT, "content_hash": expected_hash},
        )
        result = manager.heal_stale_embeddings()
        assert result["healed"] == 1
        assert result["stale_by_fingerprint"] == 1
        meta = manager.vectors.get_metadata([mem.id])[mem.id]
        assert meta["model_fingerprint"] == "nano:sha256:newweights"

    def test_heal_noops_when_vintage_current(self, manager: MemoryManager) -> None:
        """Both freshness keys match ⇒ nothing to do (steady state)."""
        mem = _published(manager, "vintage current probe about gamma")
        _pipeline_state(manager, mem.id, PipelineState.REFINED)
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        manager._embedder.fingerprint = "nano:sha256:newweights"
        seeded = manager._vector_metadata(stored)
        manager.vectors.upsert(mem.id, [0.2] * 384, seeded)
        result = manager.heal_stale_embeddings()
        assert result["healed"] == 0
        assert result["stale_by_fingerprint"] == 0
        # The untouched row keeps its seeded vector (no re-embed churn).
        assert manager.vectors.get_metadata([mem.id])[mem.id] == seeded

    def test_heal_vintage_migration_respects_limit(self, manager: MemoryManager) -> None:
        """Batch semantics: a vintage migration drains ``limit`` rows per call."""
        manager._embedder.fingerprint = "nano:sha256:newweights"
        ids = []
        for i in range(3):
            mem = _published(manager, f"vintage limit probe {i} about delta")
            _pipeline_state(manager, mem.id, PipelineState.REFINED)
            ids.append(mem.id)
        # All three rows carry pre-swap vectors (old fingerprint).
        for mid in ids:
            stored = manager.sqlite.get(mid)
            assert stored is not None
            manager._embedder.fingerprint = "nano:sha256:oldweights"
            manager.vectors.upsert(mid, [0.2] * 384, manager._vector_metadata(stored))
        manager._embedder.fingerprint = "nano:sha256:newweights"
        first = manager.heal_stale_embeddings(limit=2)
        assert first["checked"] == 2
        assert first["healed"] == 2
        second = manager.heal_stale_embeddings(limit=2)
        assert second["healed"] == 1
        third = manager.heal_stale_embeddings(limit=2)
        assert third["healed"] == 0  # fully migrated, idempotent

    def test_heal_never_touches_quarantined_on_vintage_mismatch(
        self, manager: MemoryManager
    ) -> None:
        """Quarantine is absolute: even the vintage migration skips it."""
        mem = _published(manager, "quarantined vintage probe about epsilon")
        _pipeline_state(manager, mem.id, PipelineState.REFINED)
        _quarantine(manager, mem.id, "secret")
        manager._embedder.fingerprint = "nano:sha256:oldweights"
        manager.vectors.upsert(mem.id, [0.5] * 384, {"project": PROJECT, "agent": AGENT})
        manager._embedder.fingerprint = "nano:sha256:newweights"
        result = manager.heal_stale_embeddings()
        assert result["healed"] == 0
        # The quarantined row keeps its stale vector — untouched.
        assert "model_fingerprint" not in manager.vectors.get_metadata([mem.id])[mem.id]

    # ── review P2: failures must be bounded within ONE heal call ──────────

    @staticmethod
    def _seed_vintage_stale(
        manager: MemoryManager, content: str, *, old_fp: str = "nano:sha256:oldweights"
    ) -> str:
        """Seed a REFINED row carrying a pre-swap (old-fingerprint) vector."""
        mem = _published(manager, content)
        _pipeline_state(manager, mem.id, PipelineState.REFINED)
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        manager._embedder.fingerprint = old_fp
        manager.vectors.upsert(mem.id, [0.2] * 384, manager._vector_metadata(stored))
        return mem.id

    def test_heal_vintage_migration_poison_row_bounded(
        self, manager: MemoryManager, caplog
    ) -> None:
        """Review P2: every attempt consumes budget; poison cannot unbound it.

        Pre-fix, only successes consumed budget, so one poison row (or a
        dead embedder — ollama/sentence-transformers mid-migration) let a
        single background tick attempt EVERY remaining stale row inline.
        Now each TRIED re-embed (success or failure) consumes one unit,
        and a run of consecutive failures stops the pass early with one
        summary warning instead of a per-row flood.
        """
        manager._embedder.fingerprint = "nano:sha256:newweights"

        def poisoned_embed(text: str) -> list[float]:
            if "poison-marker" in text:
                raise RuntimeError("poison row explodes the embedder")
            return [0.1] * 384

        manager._embedder.embed.side_effect = poisoned_embed
        healthy = [
            self._seed_vintage_stale(manager, f"poison bound probe {i} about eta") for i in range(4)
        ]
        poison = self._seed_vintage_stale(manager, "poison-marker row about theta")
        # The sweep pages created_at DESC — backdate the healthy rows so
        # the poison row is guaranteed inside the first budget window.
        past = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
        conn = manager.sqlite._get_conn()
        conn.executemany(
            "UPDATE memories SET created_at=? WHERE id=?", [(past, mid) for mid in healthy]
        )
        conn.commit()

        manager._embedder.fingerprint = "nano:sha256:newweights"  # the swap
        first = manager.heal_stale_embeddings(limit=3)
        # The FAILED poison attempt consumes budget too: 3 attempts total.
        assert first["checked"] == 3
        assert first["healed"] == 2
        assert first["failed"] == 1
        assert first["healed"] + first["failed"] == 3  # attempts ≤ limit
        # Poison (attempted, failed) + the two never-attempted rows stay
        # stale — bounded work, leftovers for later ticks.
        probe_ids = [*healthy, poison]
        metas = manager.vectors.get_metadata(probe_ids)
        still_stale = [
            mid
            for mid in probe_ids
            if metas[mid].get("model_fingerprint") != "nano:sha256:newweights"
        ]
        assert len(still_stale) == 3
        assert poison in still_stale

        # Embedder healthy again: the drain resumes and finishes the set.
        manager._embedder.embed.side_effect = None
        manager._embedder.fingerprint = "nano:sha256:newweights"
        second = manager.heal_stale_embeddings(limit=3)
        assert second["healed"] == 3
        assert second["failed"] == 0

        # Dead embedder: the consecutive-failure cutoff stops the pass
        # early with ONE summary warning (per-row detail is debug-level).
        manager._embedder.embed.side_effect = RuntimeError("ollama unreachable")
        batch = [
            self._seed_vintage_stale(manager, f"dead embedder probe {i} about iota")
            for i in range(12)
        ]
        manager._embedder.fingerprint = "nano:sha256:newweights"
        embed_calls_before = manager._embedder.embed.call_count
        with caplog.at_level("WARNING", logger="vesmaro.manager"):
            dead = manager.heal_stale_embeddings()  # default limit=200
        assert dead["healed"] == 0
        assert dead["failed"] == 10  # HEAL_CONSECUTIVE_FAILURE_CUTOFF stops mid-set
        # Exactly 10 embed ATTEMPTS: the 12-row stale set is not walked to
        # the end — the 2 rows past the cutoff stay for the next tick.
        assert manager._embedder.embed.call_count - embed_calls_before == 10
        heal_warnings = [
            r
            for r in caplog.records
            if r.levelname == "WARNING" and "heal_stale_embeddings" in r.getMessage()
        ]
        assert len(heal_warnings) == 1
        assert "10 consecutive" in heal_warnings[0].getMessage()
        # Nothing healed: all 12 rows keep the old fingerprint (10 failed
        # attempts + 2 never attempted) — the drain resumes next tick.
        metas = manager.vectors.get_metadata(batch)
        assert all(metas[mid].get("model_fingerprint") != "nano:sha256:newweights" for mid in batch)

    def test_heal_stale_by_fingerprint_count_when_budget_exhausts(
        self, manager: MemoryManager
    ) -> None:
        """Review P2 pin: vintage counting under budget pressure.

        More stale rows than budget: ``stale_by_fingerprint`` names
        exactly the stale rows the call EXAMINED, healed == limit, and
        the un-attempted leftovers stay stale so successive ticks keep
        draining the set (idempotent full-corpus migration).
        """
        manager._embedder.fingerprint = "nano:sha256:newweights"
        ids = [
            self._seed_vintage_stale(manager, f"budget exhaust probe {i} about kappa")
            for i in range(7)
        ]
        manager._embedder.fingerprint = "nano:sha256:newweights"  # the swap
        first = manager.heal_stale_embeddings(limit=3)
        assert first["checked"] == 3
        assert first["healed"] == 3  # == limit: the budget exhausted
        assert first["failed"] == 0
        assert first["stale_by_fingerprint"] == 3  # every examined row was stale
        # Leftovers: 4 rows still carry the old fingerprint for later ticks.
        metas = manager.vectors.get_metadata(ids)
        still_stale = [
            mid for mid in ids if metas[mid].get("model_fingerprint") != "nano:sha256:newweights"
        ]
        assert len(still_stale) == 4

        second = manager.heal_stale_embeddings(limit=3)
        assert second["healed"] == 3  # fresh head skipped, budget spent deeper
        third = manager.heal_stale_embeddings(limit=3)
        assert third["healed"] == 1
        fourth = manager.heal_stale_embeddings(limit=3)
        assert fourth["healed"] == 0  # drained — idempotent steady state
        assert fourth["stale_by_fingerprint"] == 0


# ── 13. Lease/reclaim — issue #170 (ADR-0019 Phase C) ─────────────────────────


class TestLeaseReclaim:
    """A worker crash between the claim CAS and the outcome write strands
    the row in ``processing``; the sweeper's lease-reclaim is the path
    back into the intake."""

    def _claimed(self, manager: MemoryManager, content: str) -> Memory:
        """Seed a row and drive it to a live ``processing`` claim."""
        mem = _published(manager, content)
        _pipeline_state(manager, mem.id, PipelineState.PENDING)
        assert manager.sqlite.claim_for_refinement(mem.id)
        return mem

    @staticmethod
    def _backdate(mgr: MemoryManager, memory_id: str, seconds: int) -> None:
        """Rewind the lease clock (``updated_at``) into the past."""
        past = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
        conn = mgr.sqlite._get_conn()
        conn.execute("UPDATE memories SET updated_at=? WHERE id=?", (past, memory_id))
        conn.commit()

    def test_stale_processing_row_is_reclaimed_to_pending(
        self, manager: MemoryManager, caplog
    ) -> None:
        mem = self._claimed(manager, "stranded lease body about omega")
        self._backdate(manager, mem.id, seconds=700)  # > REFINE_LEASE_TIMEOUT_SEC
        with caplog.at_level("WARNING", logger="vesmaro.manager"):
            result = manager.reclaim_stale_refinements()
        assert result["reclaimed"] == 1
        assert result["reclaimed_ids"] == [mem.id]
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert stored.pipeline_state == PipelineState.PENDING  # back in the intake
        audit = [r for r in caplog.records if "outcome=lease-reclaimed" in r.message]
        assert audit and f"id={mem.id[:8]}" in audit[-1].message
        assert "age=" in audit[-1].message

    def test_fresh_processing_claim_is_not_reclaimed(self, manager: MemoryManager) -> None:
        mem = self._claimed(manager, "live worker body about alpha")
        result = manager.reclaim_stale_refinements()
        assert result["reclaimed"] == 0
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert stored.pipeline_state == PipelineState.PROCESSING  # lease still held

    def test_claim_stamps_the_lease_clock(self, manager: MemoryManager) -> None:
        """A row that queued for a long time must not look expired the
        moment a worker claims it — the claim restarts the lease."""
        mem = _published(manager, "long-queued body about beta")
        _pipeline_state(manager, mem.id, PipelineState.PENDING)
        self._backdate(manager, mem.id, seconds=700)  # old enqueue timestamp
        assert manager.sqlite.claim_for_refinement(mem.id)
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert stored.pipeline_state == PipelineState.PROCESSING
        # Freshly claimed despite the old pre-claim timestamp: the claim
        # stamped updated_at, so the reclaim must leave it alone.
        assert manager.reclaim_stale_refinements()["reclaimed"] == 0

    def test_double_reclaim_concurrently_single_wins(self, manager: MemoryManager) -> None:
        mem = self._claimed(manager, "contested lease body about gamma")
        self._backdate(manager, mem.id, seconds=700)
        results: list[dict] = []
        barrier = threading.Barrier(2)

        def _sweep() -> None:
            barrier.wait()  # both sweepers observe the stale row first
            results.append(manager.reclaim_stale_refinements())

        threads = [threading.Thread(target=_sweep) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert len(results) == 2
        assert sum(r["reclaimed"] for r in results) == 1  # exactly one winner
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert stored.pipeline_state == PipelineState.PENDING

    def test_sequential_reclaim_is_idempotent(self, manager: MemoryManager) -> None:
        mem = self._claimed(manager, "idempotent lease body about delta")
        self._backdate(manager, mem.id, seconds=700)
        assert manager.reclaim_stale_refinements()["reclaimed"] == 1
        second = manager.reclaim_stale_refinements()
        assert second["reclaimed"] == 0  # already pending — CAS misses
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert stored.pipeline_state == PipelineState.PENDING

    def test_reclaim_does_not_consume_retry_budget(self, manager: MemoryManager) -> None:
        mem = self._claimed(manager, "retry-budget lease body about epsilon")
        # One honest lane-(a) failure already spent attempt 1.
        manager.sqlite.record_refine_failure(
            mem.id, attempt=1, next_retry_at=datetime.now(UTC).isoformat()
        )
        _pipeline_state(manager, mem.id, PipelineState.PENDING)  # re-enqueue
        assert manager.sqlite.claim_for_refinement(mem.id)
        self._backdate(manager, mem.id, seconds=700)
        manager.reclaim_stale_refinements()
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        # Lease expiry is infrastructure, not a failure: counter unmoved.
        assert stored.metadata.get("pipeline_retry_count") == 1

    def test_reclaimed_row_reenters_refine_intake(self, manager: MemoryManager) -> None:
        mem = self._claimed(manager, "requeue target body about zeta")
        self._backdate(manager, mem.id, seconds=700)
        manager.reclaim_stale_refinements()
        intake_ids = {m.id for m in manager.sqlite.list_refine_intake()}
        assert mem.id in intake_ids
        outcome = refine_single(manager, mem.id)  # a live worker picks it up again
        assert outcome == "refined-noop"  # solo row: honest no-artifact completion
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert stored.pipeline_state == PipelineState.REFINED


# ── 14. N1 on PROCESSED rows — issue #171 ─────────────────────────────────────


class TestN1ProcessedEditGate:
    """PROCESSED is the other context-admissible status: its content
    edits re-enter the SAME Phase A gate (the published-path twin was
    already gated; the processed half was the seam)."""

    def _processed(self, manager: MemoryManager, content: str) -> Memory:
        return _published(manager, content, status=MemoryStatus.PROCESSED)

    def test_dirty_content_edit_of_processed_demotes_to_raw(
        self, manager: MemoryManager, caplog
    ) -> None:
        mem = self._processed(manager, "processed body awaiting a dirty edit")
        with caplog.at_level("WARNING", logger="vesmaro.manager"):
            updated = manager.update(mem.id, MemoryUpdate(content=f"edited in {FAKE_AWS_KEY}"))
        assert updated is not None
        assert updated.status == MemoryStatus.RAW  # demoted out of admissible
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert FAKE_AWS_KEY in stored.content  # zero-loss: content kept
        audit = [r for r in caplog.records if "publish gate" in r.message]
        assert audit and "path=published-content-edit" in audit[-1].message
        assert "verdict=refused" in audit[-1].message

    def test_dirty_edit_demotion_preserves_pipeline_state_b1(self, manager: MemoryManager) -> None:
        mem = self._processed(manager, "refined processed body about eta")
        _pipeline_state(manager, mem.id, PipelineState.REFINED)
        updated = manager.update(mem.id, MemoryUpdate(content=f"now carries {FAKE_GITHUB_TOKEN}"))
        assert updated is not None
        assert updated.status == MemoryStatus.RAW
        # B1 invariant: N1 demotions write RAW-status side effects ONLY.
        assert updated.pipeline_state == PipelineState.REFINED

    def test_clean_content_edit_of_processed_stays_processed_and_requeues(
        self, manager: MemoryManager, caplog
    ) -> None:
        mem = self._processed(manager, "processed body before a clean edit")
        assert manager.sqlite.get(mem.id).pipeline_state is None  # pre-condition: legacy
        with caplog.at_level("INFO", logger="vesmaro.manager"):
            updated = manager.update(mem.id, MemoryUpdate(content="edited clean body about theta"))
        assert updated is not None
        assert updated.status == MemoryStatus.PROCESSED  # status unchanged
        # F8 semantics: the projection is stale after an edit — re-enter
        # the refine intake (legacy NULL rows of admissible status too).
        assert updated.pipeline_state == PipelineState.PENDING
        audit = [r for r in caplog.records if "outcome=enqueued" in r.message]
        assert audit and "from=none" in audit[-1].message
        assert "reason=content-edit" in audit[-1].message

    def test_clean_edit_of_processed_refined_requeues_from_refined(
        self, manager: MemoryManager, caplog
    ) -> None:
        mem = self._processed(manager, "refined processed body about iota")
        _pipeline_state(manager, mem.id, PipelineState.REFINED)
        with caplog.at_level("INFO", logger="vesmaro.manager"):
            updated = manager.update(mem.id, MemoryUpdate(content="edited clean body about iota"))
        assert updated is not None
        assert updated.status == MemoryStatus.PROCESSED
        assert updated.pipeline_state == PipelineState.PENDING
        audit = [r for r in caplog.records if "outcome=enqueued" in r.message]
        assert audit and "from=refined" in audit[-1].message

    def test_dirty_flip_from_processed_to_published_refused(
        self, manager: MemoryManager, caplog
    ) -> None:
        mem = self._processed(manager, "flip target processed body about kappa")
        # Store-level bypass plants danger the gate must catch on the flip
        # (clean_content dropped too — the planted text IS the projection
        # that would be served).
        manager.sqlite.update_fields(
            mem.id, content=f"planted {FAKE_AWS_KEY} body", clean_content=None
        )
        with caplog.at_level("WARNING", logger="vesmaro.manager"):
            updated = manager.update(mem.id, MemoryUpdate(status=MemoryStatus.PUBLISHED))
        assert updated is not None
        assert updated.status == MemoryStatus.PROCESSED  # stayed previous
        audit = [r for r in caplog.records if "publish gate" in r.message]
        assert audit and "path=status-flip" in audit[-1].message

    def test_flip_to_processed_is_not_the_gate_seam(self, manager: MemoryManager) -> None:
        """Deliberate scoping of the #171 extension: the EDIT branch
        covers all admissible statuses, the FLIP branch stays
        PUBLISHED-only. A RAW→PROCESSED advance is the knowledge
        pipeline's own transition (context-rewrite originals carry
        redact-at-issuance secrets by contract) — the issuance
        scan/redaction owns them there, not this gate."""
        mem = _published(
            manager, f"rewrite-original body with {FAKE_AWS_KEY} inside", status=MemoryStatus.RAW
        )
        updated = manager.update(mem.id, MemoryUpdate(status=MemoryStatus.PROCESSED))
        assert updated is not None
        assert updated.status == MemoryStatus.PROCESSED  # advanced, not gated

    def test_swap_key_survives_the_requeue(self, manager: MemoryManager) -> None:
        mem = self._processed(manager, "swap-key probe body about lambda")
        _pipeline_state(manager, mem.id, PipelineState.REFINED)
        manager.sqlite.update_fields(mem.id, swap_key="old-artifact-key")
        updated = manager.update(mem.id, MemoryUpdate(content="edited clean body about lambda"))
        assert updated is not None
        # Deliberate F8 decision: the new cycle recomputes swap_key; the
        # stale value is what makes a no-op re-run of the OLD artifact
        # detectable.
        assert updated.swap_key == "old-artifact-key"


# ── 15. Filter projection on content edit — issue #193 ────────────────────────


class TestUpdateResetsCleanContent:
    """A content replace resets the filter projection in the SAME
    transaction (the B2a swap discipline applied to update): the served
    projection is the new content immediately, never the stale
    pre-edit filter output."""

    def test_content_edit_resets_clean_content_immediately(self, manager: MemoryManager) -> None:
        mem = _published(manager, "filtered body before the edit about mu")
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert stored.clean_content is not None  # auto_filter ran at ingest
        updated = manager.update(mem.id, MemoryUpdate(content="replacement body about nu"))
        assert updated is not None
        after = manager.sqlite.get(mem.id)
        assert after is not None
        assert after.clean_content is None  # stale projection dropped
        assert after.effective_content() == "replacement body about nu"  # serves the new text

    def test_dirty_edit_also_drops_the_stale_projection(self, manager: MemoryManager) -> None:
        mem = _published(manager, "filtered body before the dirty edit about xi")
        updated = manager.update(mem.id, MemoryUpdate(content=f"replacement {FAKE_AWS_KEY} body"))
        assert updated is not None
        after = manager.sqlite.get(mem.id)
        assert after is not None
        assert after.status == MemoryStatus.RAW  # demoted by the gate…
        assert after.clean_content is None  # …AND the projection is not stale
        assert FAKE_AWS_KEY in after.effective_content()  # zero-loss on the served text

    def test_title_only_edit_keeps_clean_content(self, manager: MemoryManager) -> None:
        mem = _published(manager, "filtered body with a title edit coming about omicron")
        before = manager.sqlite.get(mem.id)
        assert before is not None and before.clean_content is not None
        updated = manager.update(mem.id, MemoryUpdate(title="Clean heading about pi"))
        assert updated is not None
        after = manager.sqlite.get(mem.id)
        assert after is not None
        assert after.clean_content == before.clean_content  # content unchanged
        assert after.title == "Clean heading about pi"
