"""SQLite metadata storage with FTS5 full-text search.

Extended schema: project, agent (denormalised from tags), pipeline fields
(quality_score, confidence, source_coverage, cluster_id, derived_from,
embedding_id), Context Filter fields (raw_content, clean_content,
filter_profile, filter_stats, filter_version), and a trace table (M6 —
explainability). FTS indexes project + agent for fast per-agent recall (M3).
Per-agent / per-project query helpers (M3).
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from sys import getsizeof
from typing import Any, Final, Literal, cast

from vesmaro.compact import FederationIndexEntry, title_matches_blocklist
from vesmaro.models import (
    NO_FEDERATE_TAG,
    Memory,
    MemorySource,
    MemoryStatus,
    MemoryType,
    PipelineState,
    Project,
    Trace,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IndexUpsertStats:
    """Per-batch outcome of :meth:`SQLiteStore.upsert_index_entries`.

    The RPC layer maps these onto the ``UpsertIndexEntriesResponse``
    counters: ``written`` → ``accepted``, ``refused`` → adds to
    ``rejected_by_gate``, ``stale`` → LWW-superseded (neither accepted
    nor rejected).
    """

    written: int
    refused: int
    stale: int


@dataclass(frozen=True, slots=True)
class PollStateRow:
    """One ``federation_poll_state`` row (S2 phase 2 poller watermark).

    ``since_rev`` is the peer's own rowid-space watermark (its
    SyncMetadata ``latest_rev`` echoes); ``last_ok_at`` /
    ``last_error`` are the loop-health telemetry the operator reads.
    """

    peer_id: str
    since_rev: int
    last_ok_at: str
    last_error: str


# ADR-0018 P1-b (m2) — FTS5 snippet highlight markers used by
# ``ccr_search``. Module-level so the issuance-side scanner
# (``MemoryManager.retrieve_content``) strips EXACTLY these markers
# before scanning a snippet: the markers wrap query-matched tokens and
# split multi-token secrets (e.g. a JWT whose payload segment matched
# the query), which makes the raw marked snippet text evade
# ``detect_secrets``.
FTS_SNIPPET_START_MARK: Final[str] = ">>>"
FTS_SNIPPET_END_MARK: Final[str] = "<<<"
FTS_SNIPPET_ELLIPSIS: Final[str] = " ... "

# M15.2 — Whitelisted dispatch for dynamic UPDATE setters.
# Maps public field name -> literal "column=?" SQL fragment. Bandit B608
# requires no user-controlled identifier be interpolated into SQL; this
# constant dict is the ONLY source of column names that `update_fields`
# will accept. Future column additions must be added here AND in the
# memories schema, not by widening the runtime allowlist.
_FIELD_UPDATERS: dict[str, str] = {
    "status": "status=?",
    "quality_score": "quality_score=?",
    "confidence": "confidence=?",
    "source_coverage": "source_coverage=?",
    "cluster_id": "cluster_id=?",
    "derived_from": "derived_from=?",
    "embedding_id": "embedding_id=?",
    "clean_content": "clean_content=?",
    "filter_profile": "filter_profile=?",
    "filter_stats": "filter_stats=?",
    "filter_version": "filter_version=?",
    "title": "title=?",
    "content": "content=?",
    "tags": "tags=?",
    "category": "category=?",
    "file_path": "file_path=?",
    # Denormalised columns derived from tags (project:/agent: slugs).
    # Whitelisted so `tags normalize` can update them via `update_fields`
    # alongside the tags JSON without falling back to `save()` (which
    # uses INSERT OR REPLACE and can desync the FTS5 external content
    # table — see fix in cli/main.py `tags normalize`).
    "project": "project=?",
    "agent": "agent=?",
    # ADR-0019 Phase B (B1) — pipeline lifecycle columns. The ADR's swap
    # mechanics run "via the targeted update_fields path", so the B2
    # daemon/swap/quarantine transitions need them here. This is a
    # STORE-INTERNAL surface only: MemoryCreate/MemoryUpdate carry none
    # of these fields, so no REST/MCP/CLI caller can reach them — the
    # callers are manager-internal code and the B2 daemon.
    "pipeline_state": "pipeline_state=?",
    "processed_at": "processed_at=?",
    "swap_key": "swap_key=?",
    "quarantine_reason": "quarantine_reason=?",
    "marker_version": "marker_version=?",
    # ADR-0019 B2a swap zero-loss: the refine daemon materialises
    # ``raw_content`` with the pre-swap projection when (and only when)
    # the column is still NULL — the invariant in models.py is "never
    # mutated after first write", and the swap's first write is exactly
    # here. No other caller writes it through this path.
    "raw_content": "raw_content=?",
    # ADR-0027 Ф3 (epic #308): the doc-sweep release stamps the sweep
    # timestamp into the chunk row's metadata JSON alongside the
    # pipeline_state/quarantine_reason flip (the §5.1 explicit-release
    # audit trail). Same store-internal discipline as the lifecycle
    # columns above: MemoryCreate/MemoryUpdate carry metadata but the
    # MANAGER validates it (the add()/update() doc-grouping gates), and
    # this targeted path is manager-internal only. JSON-serialised by
    # update_fields before the bind (the dict is never interpolated).
    "metadata": "metadata=?",
}

# FTS5 query-syntax special chars. Stripping them and wrapping the rest in
# double quotes disables FTS5 prefix/NEAR/column-syntax and turns the input
# into a literal phrase. This is the recommended hardening pattern from
# https://www.sqlite.org/fts5.html#fts5_strings — see `_build_fts_query`.
_FTS5_SPECIAL_CHARS = re.compile(r'["\'\*\(\):]')

# ── Search v2 FTS query builder (issue #313) ─────────────────────────────────
#
# The M15.2 baseline wrapped the WHOLE user input in one double-quoted
# phrase. Live probes against the production DB showed two defects:
#   1. multi-token queries became adjacency phrases ("GWS конвейер" → 0
#      hits although both words exist in one document far apart);
#   2. prefix matching was disabled, so inflected RU/EN forms were
#      invisible (конвейер → 49 vs конвейер* → 77).
# The v2 builder keeps the SAME injection-safety invariant (M15.2: no
# un-escaped user text reaches MATCH) while fixing both: every token is
# individually quoted with a trailing prefix star — a one-token quoted
# phrase with a prefix operator is valid FTS5 and injection-safe (the
# quotes guarantee no operator parsing INSIDE the token, the star is OUR
# suffix, never user input).

# Placeholder emitted when sanitisation empties the input. FTS5's `""` is
# a syntax error, so a nonsense unique phrase is the zero-rows-without-
# raising representation (unchanged from M15.2).
_FTS_NO_MATCH_PLACEHOLDER: Final[str] = "__mnemos_fts5_no_match_placeholder__"

# Term cap for the AND join. Long user queries are truncated to the
# first N terms — a bounded MATCH expression keeps the query plan cheap
# and blocks runaway conjunctive dead-ends (every added AND term can only
# shrink the result set).
_FTS_TERM_CAP: Final[int] = 8

# RU/EN stopword list for the short-token guard (issue #314). Exact
# lowercase entries only — the guard lowercases a candidate before the
# membership check UNLESS the token is fully uppercase (`isupper()`),
# because all-caps tokens are acronyms in this technical corpus (IT, QA,
# DB, CI, ML, GWS — and an AND-as-literal must survive as a literal).
# Identifier-shaped tokens (v2, x1, p0, m15, e3) never collide with this
# alpha-only list, so they need no carve-out of their own.
_FTS_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        # EN function words
        "and",
        "the",
        "for",
        "with",
        "that",
        "this",
        "from",
        "into",
        "not",
        "but",
        "are",
        "was",
        "is",
        "it",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "or",
        "an",
        "if",
        "as",
        "be",
        "no",
        "so",
        "we",
        "all",
        "any",
        "its",
        "has",
        "had",
        "will",
        # RU function words
        "не",
        "на",
        "по",
        "из",
        "от",
        "до",
        "за",
        "же",
        "ли",
        "бы",
        "для",
        "как",
        "что",
        "это",
        "или",
        "при",
        "без",
        "где",
        "кто",
        "его",
        "её",
        "их",
        "есть",
        "быть",
        "был",
        "была",
        "было",
        "были",
        "оно",
        "она",
        "они",
        "этот",
        "эта",
        "эти",
        "чем",
        "тот",
    }
)


def _fts_quote_prefix(token: str) -> str:
    """Emit one sanitised token as a quoted FTS5 prefix term `"tok"*`.

    The FTS5 query-syntax chars (`" ' * ( ) :`) are stripped per token;
    an emptied token sanitises to ``None`` upstream. The trailing ``*``
    is appended BY THE BUILDER (never user input), so the term can never
    pivot into operator syntax.
    """
    return '"' + token + '"*'


def _fts_tokenize(user_query: str) -> list[str]:
    """Whitespace-tokenise ``user_query`` and sanitise each token.

    Returns the de-duplicated (first occurrence order) list of non-empty
    sanitised tokens. FTS5 special chars are stripped per token (the
    ``_FTS5_SPECIAL_CHARS`` class — single source of truth with the M15.2
    baseline); a token that sanitises to empty is dropped.
    """
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in (user_query or "").split():
        tok = _FTS5_SPECIAL_CHARS.sub(" ", raw)
        tok = re.sub(r"\s+", " ", tok).strip()
        if tok and tok not in seen:
            seen.add(tok)
            tokens.append(tok)
    return tokens


def _fts_guard_tokens(tokens: list[str]) -> list[str]:
    """Drop degenerate tokens from a SANITISED token list (issue #314).

    Two drop rules, both relevance-only (no correctness gate depends on
    them — quarantined/admissibility/scoping still apply to whatever
    rows the wider AND matches):

      * length — 1-char tokens (`a`, `b`, `x`, `и`, `в`, `с`) are
        unambiguous noise in RU and EN: they match nearly every row, so
        their bm25 idf collapses toward 0 and one such token inside an
        AND query degraded the whole result to LIMIT rows ordered by
        id-tiebreak noise;
      * stopwords — the `_FTS_STOPWORDS` RU/EN list, checked as
        ``token.lower()`` UNLESS ``token.isupper()``: fully-uppercase
        tokens are acronyms in this technical corpus (IT, QA, DB, CI,
        ML, GWS), never function words. This drops sentence-initial
        «Как»/«The» while preserving every acronym.

    Identifier-shaped tokens (v2, x1, p0, m15, e3) survive structurally:
    they are >= 2 chars and can never equal an alpha-only stopword — no
    digit-scanning carve-out exists by design.

    NEVER-EMPTY fallback: if the rules would drop EVERY token, the
    ORIGINAL list is returned — the user's explicit degenerate query
    wins over the builder silently turning it into the no-match
    placeholder. An empty input stays empty (the placeholder path is
    unchanged).
    """
    guarded = [
        tok
        for tok in tokens
        if len(tok) >= 2 and (tok.isupper() or tok.lower() not in _FTS_STOPWORDS)
    ]
    return guarded or tokens


def _fts_expand_hyphen(token: str) -> str:
    """One AND/OR term for a (possibly hyphenated) SANITISED RAW token.

    ``unicode61`` (the memories_fts tokenizer) splits on hyphens, so a
    document containing ``release-trigger`` is indexed as the two tokens
    ``release`` and ``trigger`` — a bare quoted ``"release-trigger"*``
    can NEVER match it, while a plain ``release trigger`` query matches
    both spellings. For tokens containing a hyphen the builder therefore
    emits an OR-alternative:

        ("release-trigger"* OR "release"*)

    — the full spelling first (exact identifier hit when the whole
    string occurs contiguously), the head segment as the permissive
    alternative (covers the split-index case and the de-hyphenated
    prose spelling). Non-hyphenated tokens pass through as a plain
    quoted prefix term. ``token`` must be SANITISED raw text (no quotes
    of its own — both variants are quoted HERE, exactly once).
    """
    if "-" not in token:
        return _fts_quote_prefix(token)
    head = token.split("-", 1)[0]
    if not head:  # leading hyphen — nothing sane to OR; plain prefix term
        return _fts_quote_prefix(token)
    return f"({_fts_quote_prefix(token)} OR {_fts_quote_prefix(head)})"


def fts_query_terms(user_query: str) -> list[str]:
    """Sanitised AND-term list for ``user_query`` (the v2 builder's terms).

    Each returned term is a quoted prefix term (``"tok"*``) or a
    hyphen-expanded OR-alternative (``("tok-a"* OR "tok"*)``). Truncated
    to ``_FTS_TERM_CAP``. The empty list means "no match" (sanitisation
    emptied the input).

    Short-token guard (issue #314): degenerate tokens — 1-char tokens
    and RU/EN stopwords — are dropped by ``_fts_guard_tokens`` BEFORE
    the cap, so they neither constrain the AND nor consume cap budget.
    Fully-uppercase tokens are exempt (acronym literals); if the guard
    would drop every token, the original list is kept (never-empty
    fallback — the user's explicit degenerate query wins).
    """
    return [
        _fts_expand_hyphen(tok)
        for tok in _fts_guard_tokens(_fts_tokenize(user_query))[:_FTS_TERM_CAP]
    ]


def fts_query_v2(user_query: str) -> str:
    """Build the safe FTS5 MATCH expression — per-token prefix AND join.

    Search v2 semantics (issue #313), preserving the M15.2 hardening
    (every token quoted — no NEAR, no column filters, no operator
    injection possible):

      * multi-token queries AND their per-token prefix terms — words no
        longer need adjacency or order (defect 1);
      * the trailing ``*`` per token re-enables prefix matching, which
        is the zero-migration morphology fix for inflected RU/EN forms
        (defect 2 — конвейер now matches конвейер/конвейера/конвейеры);
      * hyphenated identifiers get an OR-alternative per term (the
        unicode61 tokenizer splits on hyphens — see
        ``_fts_expand_hyphen``);
      * degenerate short tokens are dropped before the join (issue
        #314): 1-char tokens and RU/EN stopwords match nearly every row
        and collapse bm25 idf to 0; fully-uppercase tokens are exempt
        (acronyms) and an all-degenerate query keeps its tokens
        (never-empty fallback — see ``_fts_guard_tokens``);
      * the term count is capped (``_FTS_TERM_CAP``) — long queries
        truncate instead of building a runaway conjunctive expression.

    Empty sanitisation returns the no-match placeholder phrase (the
    M15.2 ``""``-is-a-syntax-error workaround, unchanged).
    """
    terms = fts_query_terms(user_query)
    if not terms:
        return f'"{_FTS_NO_MATCH_PLACEHOLDER}"'
    return " AND ".join(terms)


def fts_join_or(terms: list[str]) -> str:
    """OR join of the SAME v2 prefix terms — the zero-AND fallback leg.

    Built from the builder's OWN sanitised term list (never raw user
    input), so the expression inherits the injection safety of
    ``fts_query_v2``. bm25 still ranks the wider recall set, so the
    fallback is ranked, not a flood.
    """
    if not terms:
        return f'"{_FTS_NO_MATCH_PLACEHOLDER}"'
    return " OR ".join(terms)


# ADR-0018 Phase 1 shipped exactly one edge kind; ADR-0030 A0 (issue
# #321) extends the set to the final shape while the table is empty (live
# probe 2026-09-15: 0 edges on 1662 memories — the one-shot window).
# 'supersedes' is the semantic replacement claim (context_rewrite);
# 'relates_to' is the honest weaker claim auto-minted by near-dup
# detection (A0 minting slice, provenance 'auto-dedupe'). Expanding this
# set requires updating the SQL CHECK constraint on memory_edges (schema
# migration), this whitelist, and the manager wrappers — in the same
# change, by design.
_EDGE_KINDS: Final[set[str]] = {"supersedes", "relates_to"}

# ADR-0030 A0 (issue #323) — used/rejected feedback event kinds for the
# edge_stats capture table. Kept in lockstep with the SQL CHECK on
# edge_stats (the same rule _EDGE_KINDS applies to memory_edges):
# 'used' = a search citation was consumed by the caller, 'rejected' =
# the caller dismissed it. Expanding this set requires the SQL CHECK
# migration and this whitelist in the same change.
#
# PUBLIC (review #338 N4): the manager wrapper and the tests consume
# this constant — it sits alongside the cap constants below and is
# imported cross-module under this name, never as a private symbol.
EDGE_STATS_KINDS: Final[frozenset[str]] = frozenset({"used", "rejected"})

#: I5 volume cap (ADR-0030, issue #323) — maximum captured events per
#: principal ``(project, agent)`` bucket. A storage-DoS and APPLY
#: pre-poisoning guard: a hostile or runaway reporter can wedge at most
#: this many rows into the table per identity. Enforced in
#: ``record_edge_stat_event`` — the over-cap event is DROPPED (a normal
#: outcome in the report dict), never an error. A bounded overshoot
#: under a concurrent-writer race is accepted: the guard bounds storage,
#: it is not an exact quota (single-process SQLite, one connection).
EDGE_STATS_EVENTS_PER_PRINCIPAL_CAP: Final[int] = 10_000

#: I5 GLOBAL volume cap (ADR-0030, review #338 N2) — hard ceiling on
#: TOTAL edge_stats rows across all principals. The per-bucket cap
#: above bounds one identity; this one bounds the table itself, which
#: the per-bucket cap alone cannot (unbounded across minted principals).
#: Enforced in ``record_edge_stat_event`` alongside the per-bucket cap
#: (same ``cap_dropped`` outcome, never an error). NOT automatic
#: eviction: once the ceiling is reached, capture stays dropped until
#: an operator reclaims rows via ``purge_edge_stats_oldest`` (the
#: ``vesmaro edge-stats purge`` CLI path, dry-run by default). Sized so
#: a legitimate deployment never touches it (10k x principals << 1M);
#: revisit together with APPLY (#325) when real volume telemetry exists.
EDGE_STATS_TOTAL_ROWS_CAP: Final[int] = 1_000_000

#: ``meta`` key holding the audit stamp (JSON: at/purged/keep_last) of
#: the last applied edge_stats purge — the compensating audit trail for
#: the one sanctioned shrink of the append-only table (review #338 N2;
#: written by ``purge_edge_stats_oldest``, read back by the CLI).
EDGE_STATS_LAST_PURGE_META_KEY: Final[str] = "edge_stats_last_purge"

#: The append-only DELETE guard for edge_stats — ONE literal shared by
#: ``_DB_SCHEMA`` (fresh installs) and ``purge_edge_stats_oldest``
#: (recreation inside the purge transaction). Single source of truth
#: (review #338 round 2, minor): the purge must reinstall exactly the
#: trigger the schema installs, not a drifting second copy.
_EDGE_STATS_NO_DELETE_TRIGGER_DDL: Final[str] = (
    "CREATE TRIGGER IF NOT EXISTS edge_stats_no_delete BEFORE DELETE ON edge_stats "
    "BEGIN "
    "SELECT RAISE(ABORT, 'edge_stats is append-only (ADR-0030 I5)'); "
    "END"
)

#: I5 bounded counter clamp (ADR-0030, issue #323) — the maximum value
#: any per-memory counter derived from edge_stats can reach
#: (``get_edge_stats_counters``). Capture must not drift before APPLY
#: (#325) exists: whatever the table accumulates, the readable counters
#: stay bounded, so APPLY inherits a bounded signal — never an
#: unbounded one — even if a volume-cap race or a future minting bug
#: lets extra rows in.
EDGE_STATS_COUNTER_CLAMP: Final[int] = 1_000

# ── TTL in-memory cache ───────────────────────────────────────────────────────


class _TTLCache:
    """Thread-safe dict with per-key TTL expiry."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _ns(key: str) -> str:
        if ":" in key:
            return key.split(":", 1)[0]
        if key.startswith("graph_"):
            return "graph"
        if key in {"tags", "projects_counts", "data_health", "stats"}:
            return "aggregates"
        return "default"

    @staticmethod
    def _size(value: Any) -> int:
        try:
            return len(json.dumps(value, ensure_ascii=False, default=str).encode())
        except (TypeError, ValueError, OverflowError):
            return getsizeof(value)

    def get(self, key: str, ttl: float) -> tuple[bool, Any]:
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return False, None
            if time.monotonic() - entry["ts"] < ttl:
                entry["hits"] += 1
                return True, entry["value"]
            self._data.pop(key, None)
            return False, None

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = {
                "ts": time.monotonic(),
                "hits": 0,
                "value": value,
                "size": self._size(value),
            }

    def invalidate(self, *keys: str) -> int:
        with self._lock:
            return sum(1 for k in keys if self._data.pop(k, None) is not None)

    def invalidate_prefix(self, prefix: str) -> int:
        with self._lock:
            keys = [k for k in self._data if k.startswith(prefix)]
            for k in keys:
                self._data.pop(k, None)
            return len(keys)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "entries": len(self._data),
                "hits": sum(e["hits"] for e in self._data.values()),
                "size_bytes": sum(e["size"] for e in self._data.values()),
            }


# ── Schema ────────────────────────────────────────────────────────────────────

_DB_SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS memories (
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
    -- Mnemos tag contract denormalisations (M2)
    project          TEXT NOT NULL DEFAULT '',
    agent            TEXT NOT NULL DEFAULT '',
    -- Knowledge pipeline (M4)
    status           TEXT NOT NULL DEFAULT 'raw',
    quality_score    REAL,
    confidence       REAL,
    source_coverage  INTEGER,
    cluster_id       TEXT,
    derived_from     TEXT NOT NULL DEFAULT '[]',
    embedding_id     TEXT,
    -- Context Filter (M10 — fields present from day 1)
    raw_content      TEXT,
    clean_content    TEXT,
    filter_profile   TEXT,
    filter_stats     TEXT,
    filter_version   TEXT,
    -- Workflow lifecycle (mnemos #96). Managed EXCLUSIVELY by the
    -- set_workflow_status method — never by save()/update_fields() — so the
    -- state machine in MemoryManager.workflow_set cannot be bypassed.
    workflow_status  TEXT,
    locked_by        TEXT,
    locked_at        TEXT,
    -- C10 (ArchCom 2026-08-27): denormalised rewrite-event provenance from
    -- metadata JSON. rewrite_source = metadata["source"] (the ingestion
    -- channel discriminator, e.g. 'context-rewrite' — NOT the MemorySource
    -- enum in the `source` column, which stays 'mcp' for rewrite events);
    -- rewrite_session = metadata["rewrite_session"]. Derived in save() and
    -- backfilled by _run_migrations; they exist so the rewrite quota counts
    -- are index-backed instead of json_extract full-scans.
    rewrite_source   TEXT,
    rewrite_session  TEXT,
    -- m3 (final review, same C10 pattern): rewrite_event_key =
    -- metadata["rewrite_event_key"] (the content-addressed idempotency key
    -- minted by the trusted context_rewrite path). Derived in save() under
    -- trusted_rewrite_provenance ONLY and backfilled once by
    -- _run_migrations; backs the dedupe lookup index instead of a
    -- json_extract full-scan per delivery.
    rewrite_event_key TEXT,
    -- ADR-0019 Phase B (B1) — orthogonal pipeline lifecycle. NULL on
    -- legacy rows (the B1 backfill classifies PROCESSED/lineage rows
    -- 'refined', lineage-less PUBLISHED rows 'pending'; RAW/PROCESSING/
    -- ARCHIVED stay NULL). Written by store-internal paths only (the B2
    -- daemon / swap / quarantine transitions) — never by generic create
    -- surfaces.
    pipeline_state    TEXT,
    processed_at      TEXT,
    swap_key          TEXT,
    quarantine_reason TEXT,
    -- Provenance marker version (ADR-0019 §4): incremented on every
    -- served-projection swap so consumers detect projection desync via
    -- the marker alone. 1 from day one (including backfilled rows).
    marker_version    INTEGER NOT NULL DEFAULT 1
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    id UNINDEXED,
    title,
    content,
    tags,
    project UNINDEXED,
    agent UNINDEXED,
    content=memories,
    content_rowid=rowid,
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, id, title, content, tags, project, agent)
    VALUES (new.rowid, new.id, new.title, new.content, new.tags, new.project, new.agent);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, id, title, content, tags, project, agent)
    VALUES ('delete', old.rowid, old.id, old.title, old.content, old.tags, old.project, old.agent);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, id, title, content, tags, project, agent)
    VALUES ('delete', old.rowid, old.id, old.title, old.content, old.tags, old.project, old.agent);
    INSERT INTO memories_fts(rowid, id, title, content, tags, project, agent)
    VALUES (new.rowid, new.id, new.title, new.content, new.tags, new.project, new.agent);
END;

CREATE INDEX IF NOT EXISTS idx_memories_source   ON memories(source);
CREATE INDEX IF NOT EXISTS idx_memories_type     ON memories(memory_type);
CREATE INDEX IF NOT EXISTS idx_memories_created  ON memories(created_at);
CREATE INDEX IF NOT EXISTS idx_memories_status   ON memories(status);
CREATE INDEX IF NOT EXISTS idx_memories_project  ON memories(project);
CREATE INDEX IF NOT EXISTS idx_memories_agent    ON memories(agent);
CREATE INDEX IF NOT EXISTS idx_memories_cluster  ON memories(cluster_id);
CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);
-- NOTE (C10): idx_memories_project_rewrite_source_created is created in
-- _run_migrations, NOT here — the rewrite_source/rewrite_session columns
-- reach legacy DBs via ALTER TABLE in that same routine, and this script
-- runs BEFORE it (an index over a missing column would abort the connect).

-- mnemos #96: workflow lifecycle audit log. Every state transition is
-- recorded here (actor, from->to, reason, force_used). The workflow_status /
-- locked_by / locked_at columns on `memories` are the *current* projection;
-- this table is the immutable history that makes the audit + rate-limit
-- guardrails work.
CREATE TABLE IF NOT EXISTS memory_workflow_history (
    id          TEXT PRIMARY KEY,
    memory_id   TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    actor       TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    force_used  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wf_history_memory ON memory_workflow_history(memory_id);
CREATE INDEX IF NOT EXISTS idx_wf_history_created ON memory_workflow_history(created_at);

CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    paths       TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS traces (
    id                TEXT PRIMARY KEY,
    task_label        TEXT NOT NULL,
    project           TEXT NOT NULL DEFAULT '',
    step              TEXT NOT NULL,
    item_id           TEXT,
    llm_called        INTEGER NOT NULL DEFAULT 0,
    llm_done          INTEGER NOT NULL DEFAULT 0,
    cache_hit         INTEGER NOT NULL DEFAULT 0,
    fallback_used     INTEGER NOT NULL DEFAULT 0,
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    tokens_in         INTEGER NOT NULL DEFAULT 0,
    tokens_out        INTEGER NOT NULL DEFAULT 0,
    tokens_per_sec    REAL NOT NULL DEFAULT 0.0,
    rationale_summary TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_traces_project ON traces(project);
CREATE INDEX IF NOT EXISTS idx_traces_created ON traces(created_at);

-- M5: Dead-Letter Queue for failed synthesis / publish
CREATE TABLE IF NOT EXISTS dlq (
    id              TEXT PRIMARY KEY,
    memory_id       TEXT NOT NULL,
    cluster_id      TEXT,
    task_label      TEXT NOT NULL DEFAULT 'synthesize',
    error_message   TEXT NOT NULL DEFAULT '',
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 3,
    next_retry_at   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dlq_memory    ON dlq(memory_id);
CREATE INDEX IF NOT EXISTS idx_dlq_cluster  ON dlq(cluster_id);
CREATE INDEX IF NOT EXISTS idx_dlq_retry      ON dlq(next_retry_at);

-- M16: A2A Sessions (persistent backend for A2A routing)
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    metadata        TEXT NOT NULL DEFAULT '{}',
    ttl_expires_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_created  ON sessions(created_at);

CREATE TABLE IF NOT EXISTS turns (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    turn_id         TEXT NOT NULL,
    step_number     INTEGER NOT NULL,
    role            TEXT NOT NULL,
    from_agent      TEXT,
    to_agent        TEXT,
    message_id      TEXT,
    content         TEXT NOT NULL,
    summary         TEXT,
    key_decisions   TEXT NOT NULL DEFAULT '[]',
    outcome         TEXT,
    tags            TEXT NOT NULL DEFAULT '[]',
    context_pointer TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE(session_id, turn_id),
    UNIQUE(message_id)
);

CREATE INDEX IF NOT EXISTS idx_turns_session_step ON turns(session_id, step_number);
CREATE INDEX IF NOT EXISTS idx_turns_message_id   ON turns(message_id);
CREATE INDEX IF NOT EXISTS idx_turns_created       ON turns(created_at);

-- C8 (ArchCom 2026-08-27): turns_fts + the turns_ai/ad/au triggers were
-- REMOVED — dead index (zero readers in src) and a second plaintext copy
-- of every turn at rest plus write amplification on the hot turn path.
-- Legacy DBs have the table+triggers dropped idempotently in
-- _run_migrations. Turn-level search is not a feature; the DDL lives in
-- VCS history if a /v1/search consumer ever materialises.

-- T-AUTH: bearer tokens, session tokens, TOTP challenges (ADR-0014)
CREATE TABLE IF NOT EXISTS auth_tokens (
    token_id              TEXT PRIMARY KEY,
    token_sha256          TEXT NOT NULL UNIQUE,
    name                  TEXT,
    totp_secret_encrypted BLOB,
    created_at            TEXT NOT NULL,
    expires_at            TEXT,
    disabled_at           TEXT,
    failure_count         INTEGER NOT NULL DEFAULT 0,
    totp_failure_count    INTEGER NOT NULL DEFAULT 0,
    totp_last_step        INTEGER,
    revoked               INTEGER NOT NULL DEFAULT 0,
    totp_required         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    session_sha256 TEXT PRIMARY KEY,
    token_id       TEXT NOT NULL REFERENCES auth_tokens(token_id) ON DELETE CASCADE,
    created_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    client_ip      TEXT
);

CREATE TABLE IF NOT EXISTS auth_challenges (
    challenge_id TEXT PRIMARY KEY,
    token_id     TEXT NOT NULL REFERENCES auth_tokens(token_id) ON DELETE CASCADE,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_auth_tokens_sha256      ON auth_tokens(token_sha256);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires   ON auth_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_auth_challenges_expires ON auth_challenges(expires_at);

-- Generic key-value metadata store for cross-run state (e.g. pipeline
-- last-run timestamp). Uses UPSERT so concurrent writers don't clobber
-- each other's rows.
CREATE TABLE IF NOT EXISTS meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- P1-4: CCR (Compress-Cache-Retrieve) reversible compression cache.
-- Stores the ORIGINAL uncompressed content keyed by its SHA-256 hash.
-- The compressed representation embeds a marker referencing this hash;
-- mnemos_retrieve fetches the original back with zero data loss.
-- Inspired by headroom's CCR (https://github.com/headroomlabs-ai/headroom),
-- Apache 2.0 — we integrate into the existing mnemos store (one DB).
-- A1 (ArchCom 2026-08-27): composite PK (project, hash) — the same content
-- hash cached by two projects is TWO rows (the first-writer-squatting
-- cross-project DoS edge of the hash-only PK dissolves). Legacy DBs are
-- rebuilt onto this shape by _run_migrations (first-writer-wins dedup).
CREATE TABLE IF NOT EXISTS ccr_cache (
    hash             TEXT NOT NULL,
    original         TEXT NOT NULL,
    project          TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    size_bytes       INTEGER NOT NULL DEFAULT 0,
    retrieval_count  INTEGER NOT NULL DEFAULT 0,
    last_retrieved_at TEXT,
    -- ADR-0018 P1-a: verdict of the detect_secrets scan run at store
    -- time. 'clean' | 'hit' | 'unknown' (scanner error). NULL on legacy
    -- rows written before the migration — treated as unscanned. The
    -- stored original is ALWAYS verbatim (zero-loss, committee
    -- decision); this flag is observability only — issuance keeps
    -- scanning unconditionally (patterns evolve; a store-time verdict
    -- alone would go stale).
    secret_scan_verdict TEXT,
    secret_scan_at      TEXT,
    -- A2 (ArchCom 2026-08-27) — issuer ledger: the agent/session that
    -- FIRST stored this (project, hash) row (the UPSERT does not rewrite
    -- them — first-writer owns, the same rule A1 applied to the PK).
    -- NULL on rows stored without caller identity (legacy migrations,
    -- identity-less compress callers): marker provenance for those rows
    -- is unverifiable and strict-mode validation refuses them.
    issuer_agent    TEXT,
    issuer_session  TEXT,
    PRIMARY KEY (project, hash)
);

CREATE INDEX IF NOT EXISTS idx_ccr_cache_project   ON ccr_cache(project);
CREATE INDEX IF NOT EXISTS idx_ccr_cache_created   ON ccr_cache(created_at);
CREATE INDEX IF NOT EXISTS idx_ccr_cache_retrieval ON ccr_cache(retrieval_count);

-- P1-4: FTS5 over cached originals so retrieve(query=...) can rank
-- snippets without a separate DB. External-content table over ccr_cache.
CREATE VIRTUAL TABLE IF NOT EXISTS ccr_cache_fts USING fts5(
    hash UNINDEXED,
    original,
    content=ccr_cache,
    content_rowid=rowid,
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS ccr_cache_ai AFTER INSERT ON ccr_cache BEGIN
    INSERT INTO ccr_cache_fts(rowid, hash, original)
    VALUES (new.rowid, new.hash, new.original);
END;

CREATE TRIGGER IF NOT EXISTS ccr_cache_ad AFTER DELETE ON ccr_cache BEGIN
    INSERT INTO ccr_cache_fts(ccr_cache_fts, rowid, hash, original)
    VALUES ('delete', old.rowid, old.hash, old.original);
END;

CREATE TRIGGER IF NOT EXISTS ccr_cache_au AFTER UPDATE ON ccr_cache BEGIN
    INSERT INTO ccr_cache_fts(ccr_cache_fts, rowid, hash, original)
    VALUES ('delete', old.rowid, old.hash, old.original);
    INSERT INTO ccr_cache_fts(rowid, hash, original)
    VALUES (new.rowid, new.hash, new.original);
END;

-- ADR-0018 Phase 1 groundwork, extended by ADR-0030 A0 (issue #321)
-- into the FINAL shape while the table was empty (0 edges on 1662
-- memories, live probe 2026-09-15 — after minting starts a rebuild
-- becomes a real migration). Kinds: 'supersedes' (semantic replacement
-- claim, context_rewrite) and 'relates_to' (near-dup affinity, auto-
-- minted). weight DEFAULT 1.0 — minting may raise it, feedback factors
-- (A1) multiply at read time, never at store time. provenance:
-- 'declared' (explicit caller) or the auto-dedupe rule id. Scope
-- columns are NULL for global edges and carry the I5 feedback scope
-- later. PK (from, to, kind) makes add_edge idempotent (INSERT OR
-- IGNORE). Self-edges are rejected both here (CHECK) and in
-- add_memory_edge (friendly ValueError). ON DELETE CASCADE keeps edges
-- consistent when memories are deleted.
CREATE TABLE IF NOT EXISTS memory_edges (
    from_memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    to_memory_id   TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL CHECK (kind IN ('supersedes', 'relates_to')),
    created_at     TEXT NOT NULL,
    weight         REAL NOT NULL DEFAULT 1.0,
    provenance     TEXT NOT NULL DEFAULT 'declared',
    scope_project  TEXT,
    scope_agent    TEXT,
    PRIMARY KEY (from_memory_id, to_memory_id, kind),
    CHECK (from_memory_id <> to_memory_id)
);

CREATE INDEX IF NOT EXISTS idx_memory_edges_from ON memory_edges(from_memory_id, kind);
CREATE INDEX IF NOT EXISTS idx_memory_edges_to   ON memory_edges(to_memory_id, kind);

-- ADR-0030 A0 (issue #323) — used/rejected feedback CAPTURE (I5).
-- FINAL schema from day one (the #321 one-shot-window lesson): scope
-- fields (project/agent) ride the FIRST event — a retrofit loses the
-- project/agent boundary. One row per (report, memory): ``event_id``
-- is the PK, so an agent retrying the same report inserts nothing
-- (INSERT OR IGNORE in record_edge_stat_event — no double weight).
-- ``memory_id`` is the citation id from the search response
-- (SearchResult.memory.id) and deliberately carries NO foreign key:
-- this is an audit trail and audit rows outlive their subject —
-- deleting a memory must neither fail nor rewrite history. Dangling
-- rows are inert (counters read joins them away). The two triggers
-- below make append-only a DATABASE guarantee, not a convention:
-- UPDATE and DELETE abort. ``kind``: 'used' (citation consumed) |
-- 'rejected' (citation dismissed). Counters derived from this table
-- are clamped at EDGE_STATS_COUNTER_CLAMP; per-principal volume is
-- capped at EDGE_STATS_EVENTS_PER_PRINCIPAL_CAP and TOTAL table volume
-- at EDGE_STATS_TOTAL_ROWS_CAP in record_edge_stat_event (I5:
-- storage-DoS + APPLY pre-poisoning). Reclaiming rows past the global
-- cap is an explicit operator path (purge_edge_stats_oldest / the
-- `vesmaro edge-stats purge` CLI), never automatic eviction.
-- CAPTURE ONLY — zero ranking influence in A0 (APPLY is #325).
CREATE TABLE IF NOT EXISTS edge_stats (
    event_id    TEXT PRIMARY KEY,
    memory_id   TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('used', 'rejected')),
    project     TEXT NOT NULL DEFAULT '',
    agent       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_edge_stats_memory ON edge_stats(memory_id, kind);
CREATE INDEX IF NOT EXISTS idx_edge_stats_scope  ON edge_stats(project, agent);

CREATE TRIGGER IF NOT EXISTS edge_stats_no_update BEFORE UPDATE ON edge_stats
BEGIN
    SELECT RAISE(ABORT, 'edge_stats is append-only (ADR-0030 I5)');
END;

-- S2 meta-mirror substrate (ADR-0021 Q10.3, archcom 2026-09-20) — the
-- federation metadata index. One row per record known to this store:
-- locally-originated rows (origin_peer='self', synthesized from the
-- local corpus via build_metadata_entry) and rows mirrored from peers
-- (origin_peer=<peer A2A id>). INDEX-ONLY by design: no summary, no
-- key points, no content — a record's existence is itself an inference
-- surface (position paper §4), and the table is OUTSIDE mnemos_search
-- (never served by the memory search path). ``id`` is the wire
-- MetadataRecord id (fed:<source_agent>:<uuid>) — UNIQUE is the
-- cross-peer dedup key. ``tags`` is a JSON array (same convention as
-- memories.tags). received_at is stamped by the store at upsert (when
-- THIS core received the row) — NOT the record's own timestamp. The
-- rowid (implicit) is the ADR-0020-mechanic resume space for the
-- rowid-ASC cursor walk in list_index.
CREATE TABLE IF NOT EXISTS federation_index (
    id             TEXT PRIMARY KEY,
    type           TEXT NOT NULL DEFAULT '',
    title          TEXT NOT NULL DEFAULT '',
    tags           TEXT NOT NULL DEFAULT '[]',
    project        TEXT NOT NULL DEFAULT '',
    source_agent   TEXT NOT NULL DEFAULT '',
    source_peer    TEXT NOT NULL DEFAULT '',
    origin_peer    TEXT NOT NULL DEFAULT 'self',
    content_state  TEXT NOT NULL DEFAULT 'available'
                   CHECK (content_state IN ('available', 'tombstoned')),
    timestamp      TEXT NOT NULL DEFAULT '',
    schema_version TEXT NOT NULL DEFAULT 'mnemos.federation.metadata.v1',
    received_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Named per ADR-0021 substrate spec: (origin_peer, timestamp) backs
-- "what does peer X hold" walks (drain/purge audits, multi-hop dedup)
-- without a table scan.
CREATE INDEX IF NOT EXISTS idx_federation_index_origin_timestamp
    ON federation_index(origin_peer, timestamp);

-- S2 phase 2 meta-poller watermark state (ADR-0021 Q10.2 poll-first).
-- One row per polled peer: ``since_rev`` is the peer's OWN rowid-space
-- watermark echoed by its SyncMetadata pages (latest_rev) — the next
-- poll sends it as ``--since`` so a restart resumes exactly after the
-- last consumed row. ``last_ok_at`` / ``last_error`` are operator
-- telemetry for "is the poll loop healthy". Additive CREATE IF NOT
-- EXISTS: pre-phase-2 DBs gain the table on first connect, no data
-- migration (the poller starts from rev 0 when no row exists).
CREATE TABLE IF NOT EXISTS federation_poll_state (
    peer_id    TEXT PRIMARY KEY,
    since_rev  INTEGER NOT NULL DEFAULT 0,
    last_ok_at TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT ''
);
"""
    + _EDGE_STATS_NO_DELETE_TRIGGER_DDL
    + ";"
)

_MIGRATIONS: list[tuple[str, str]] = [
    ("project", "ALTER TABLE memories ADD COLUMN project TEXT NOT NULL DEFAULT ''"),
    ("agent", "ALTER TABLE memories ADD COLUMN agent TEXT NOT NULL DEFAULT ''"),
    ("quality_score", "ALTER TABLE memories ADD COLUMN quality_score REAL"),
    ("confidence", "ALTER TABLE memories ADD COLUMN confidence REAL"),
    ("source_coverage", "ALTER TABLE memories ADD COLUMN source_coverage INTEGER"),
    ("cluster_id", "ALTER TABLE memories ADD COLUMN cluster_id TEXT"),
    ("derived_from", "ALTER TABLE memories ADD COLUMN derived_from TEXT NOT NULL DEFAULT '[]'"),
    ("embedding_id", "ALTER TABLE memories ADD COLUMN embedding_id TEXT"),
    ("raw_content", "ALTER TABLE memories ADD COLUMN raw_content TEXT"),
    ("clean_content", "ALTER TABLE memories ADD COLUMN clean_content TEXT"),
    ("filter_profile", "ALTER TABLE memories ADD COLUMN filter_profile TEXT"),
    ("filter_stats", "ALTER TABLE memories ADD COLUMN filter_stats TEXT"),
    ("filter_version", "ALTER TABLE memories ADD COLUMN filter_version TEXT"),
    ("category", "ALTER TABLE memories ADD COLUMN category TEXT"),
    # mnemos #96 — workflow lifecycle columns. Added via ALTER so existing
    # DBs gain the columns on next connect; fresh DBs get them from
    # _DB_SCHEMA. The history table is CREATE TABLE IF NOT EXISTS in
    # _DB_SCHEMA so it does not need an entry here.
    ("workflow_status", "ALTER TABLE memories ADD COLUMN workflow_status TEXT"),
    ("locked_by", "ALTER TABLE memories ADD COLUMN locked_by TEXT"),
    ("locked_at", "ALTER TABLE memories ADD COLUMN locked_at TEXT"),
    # C10 (ArchCom 2026-08-27) — denormalised rewrite-event provenance
    # columns (metadata.source / metadata.rewrite_session). Nullable:
    # non-rewrite memories carry NULL. Existing rows are BACKFILLED once
    # from the metadata JSON by _run_migrations (meta-table flag
    # ``schema_backfill_rewrite_cols_v1``); new rows derive the columns in
    # save(). NOT named `source` — that column is the MemorySource enum.
    ("rewrite_source", "ALTER TABLE memories ADD COLUMN rewrite_source TEXT"),
    ("rewrite_session", "ALTER TABLE memories ADD COLUMN rewrite_session TEXT"),
    # m3 (final review) — denormalised rewrite-event idempotency key
    # (metadata.rewrite_event_key). Nullable: only trusted-path rewrite
    # events carry it. Existing rows are BACKFILLED once from the metadata
    # JSON by _migrate_m3_backfill_rewrite_event_key (meta-table flag
    # ``schema_backfill_rewrite_event_key_v1``), gated on the trusted-path
    # provenance (metadata.source = 'context-rewrite') so planted client
    # metadata is never promoted into the dedupe index.
    ("rewrite_event_key", "ALTER TABLE memories ADD COLUMN rewrite_event_key TEXT"),
    # ADR-0019 Phase B (B1) — orthogonal pipeline lifecycle columns +
    # marker version. ALTER only (instant, metadata-only; rowids and the
    # external-content FTS table are untouched — no rebuild). Legacy
    # rows are classified once by _migrate_b1_backfill_pipeline_state;
    # marker_version backfills to 1 via the column DEFAULT.
    ("pipeline_state", "ALTER TABLE memories ADD COLUMN pipeline_state TEXT"),
    ("processed_at", "ALTER TABLE memories ADD COLUMN processed_at TEXT"),
    ("swap_key", "ALTER TABLE memories ADD COLUMN swap_key TEXT"),
    ("quarantine_reason", "ALTER TABLE memories ADD COLUMN quarantine_reason TEXT"),
    ("marker_version", "ALTER TABLE memories ADD COLUMN marker_version INTEGER NOT NULL DEFAULT 1"),
]

# ADR-0018 P1-a — scan-at-store verdict columns on ccr_cache. Existing
# rows keep NULL (treated as unscanned; issuance keeps scanning them as
# today). Fresh DBs get the columns from _DB_SCHEMA; the ALTER only runs
# on legacy databases. Two columns (verdict + scan timestamp) so the
# flag's freshness is auditable.
_CCR_MIGRATIONS: list[tuple[str, str]] = [
    (
        "secret_scan_verdict",
        "ALTER TABLE ccr_cache ADD COLUMN secret_scan_verdict TEXT",
    ),
    (
        "secret_scan_at",
        "ALTER TABLE ccr_cache ADD COLUMN secret_scan_at TEXT",
    ),
    # A2 (ArchCom 2026-08-27) — issuer-ledger columns. Existing rows keep
    # NULL (provenance unverifiable → strict marker validation refuses
    # them with the distinct "unverifiable legacy marker" reason). Fresh
    # DBs get the columns from _DB_SCHEMA.
    (
        "issuer_agent",
        "ALTER TABLE ccr_cache ADD COLUMN issuer_agent TEXT",
    ),
    (
        "issuer_session",
        "ALTER TABLE ccr_cache ADD COLUMN issuer_session TEXT",
    ),
]

# meta-table flag gating the one-time C10 backfill of the denormalised
# rewrite columns (set inside the same transaction as the backfill).
_BACKFILL_REWRITE_COLS_FLAG: Final[str] = "schema_backfill_rewrite_cols_v1"

# m3 (final review) — meta-table flag gating the one-time backfill of the
# denormalised rewrite_event_key column (same transaction as the backfill).
_BACKFILL_REWRITE_EVENT_KEY_FLAG: Final[str] = "schema_backfill_rewrite_event_key_v1"

# ADR-0019 Phase B (B1) — meta-table flag gating the one-time backfill of
# pipeline_state / processed_at (same commit as the backfill).
_BACKFILL_PIPELINE_STATE_FLAG: Final[str] = "schema_backfill_pipeline_state_v1"

# mnemos #251 D0 — meta-table key namespace for the first-writer-wins
# session→agent binding of the checkpoint channel. Lives in the existing
# ``meta`` table (additive, migration-free): a dedicated column on
# ``sessions`` would require callers to present SessionStore-issued ids,
# while save_context accepts any caller-held session id.
_SESSION_AGENT_META_PREFIX: Final[str] = "session_agent_binding:"

# C8 (ArchCom 2026-08-27) — legacy turn FTS objects dropped idempotently on
# every connect (IF EXISTS no-ops after the first run).
_C8_DROP_SQL: Final[tuple[str, ...]] = (
    "DROP TRIGGER IF EXISTS turns_ai",
    "DROP TRIGGER IF EXISTS turns_ad",
    "DROP TRIGGER IF EXISTS turns_au",
    "DROP TABLE IF EXISTS turns_fts",
)

# A1 (ArchCom 2026-08-27) — rebuild target for the ccr_cache composite-PK
# migration. Shape mirrors the ccr_cache DDL in _DB_SCHEMA exactly.
_CCR_REBUILD_DDL: Final[str] = """
CREATE TABLE ccr_cache_a1_rebuild (
    hash               TEXT NOT NULL,
    original           TEXT NOT NULL,
    project            TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL,
    size_bytes         INTEGER NOT NULL DEFAULT 0,
    retrieval_count    INTEGER NOT NULL DEFAULT 0,
    last_retrieved_at  TEXT,
    secret_scan_verdict TEXT,
    secret_scan_at      TEXT,
    issuer_agent        TEXT,
    issuer_session      TEXT,
    PRIMARY KEY (project, hash)
)
"""

# ADR-0030 A0 (issue #321) — rebuild target for the memory_edges
# edge-kinds migration (SQLite CHECK constraints cannot be ALTERed).
# Shape mirrors the memory_edges DDL in _DB_SCHEMA exactly.
_EDGES_REBUILD_DDL: Final[str] = """
CREATE TABLE memory_edges_a0_rebuild (
    from_memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    to_memory_id   TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL CHECK (kind IN ('supersedes', 'relates_to')),
    created_at     TEXT NOT NULL,
    weight         REAL NOT NULL DEFAULT 1.0,
    provenance     TEXT NOT NULL DEFAULT 'declared',
    scope_project  TEXT,
    scope_agent    TEXT,
    PRIMARY KEY (from_memory_id, to_memory_id, kind),
    CHECK (from_memory_id <> to_memory_id)
)
"""


# ── SQLiteStore ───────────────────────────────────────────────────────────────


class SQLiteStore:
    """Thread-safe SQLite store with FTS5, pipeline status, and per-agent recall."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._cache = _TTLCache()
        # Serialises CONNECTION BOOTSTRAP (schema script + migrations)
        # across this store's threads. The per-thread connections
        # themselves run unlocked as before — only the first connect of
        # each thread takes the lock. Without it, a manager's background
        # threads (scanner loop, CCR cleanup) racing the main thread's
        # first connect interleave multi-statement DDL: the A1 rebuild's
        # DROP/RENAME can collide with a concurrent fresh-CREATE from
        # the schema script ("database disk image is malformed" /
        # "database is locked"). Cross-PROCESS first-connects are
        # serialized at the SQLite level instead (BEGIN IMMEDIATE +
        # busy_timeout + IF EXISTS convergence).
        self._bootstrap_lock = threading.Lock()

    def _get_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            with self._bootstrap_lock:
                conn = getattr(self._local, "conn", None)
                if conn is None:
                    conn = sqlite3.connect(str(self.db_path), check_same_thread=True)
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA foreign_keys=ON")
                    conn.execute("PRAGMA busy_timeout=5000")
                    conn.executescript(_DB_SCHEMA)
                    self._run_migrations(conn)
                    self._local.conn = conn
        return conn

    @staticmethod
    def _run_migrations(conn: sqlite3.Connection) -> None:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
        for col, sql in _MIGRATIONS:
            if col not in existing:
                conn.execute(sql)
        # C10 index — created here (not in _DB_SCHEMA) because legacy DBs
        # gain the rewrite columns from the ALTERs above, while _DB_SCHEMA
        # runs before this routine on every connect. IF NOT EXISTS no-ops
        # after the first connect. Backs the per-(project, session) and
        # per-project rewrite quota counts without json_extract scans.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_project_rewrite_source_created "
            "ON memories(project, rewrite_source, created_at)"
        )
        # m3 index — same placement reasoning as the C10 index above: legacy
        # DBs gain the column from the ALTER in this routine, which runs
        # AFTER _DB_SCHEMA. Backs the rewrite-event dedupe lookup
        # (get_memory_id_by_rewrite_event_key) without json_extract scans;
        # (rewrite_event_key, created_at) lets the ORDER BY walk the index.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_rewrite_event_key_created "
            "ON memories(rewrite_event_key, created_at)"
        )
        ccr_cols = {row[1] for row in conn.execute("PRAGMA table_info(ccr_cache)").fetchall()}
        for col, sql in _CCR_MIGRATIONS:
            if col not in ccr_cols:
                conn.execute(sql)
        conn.commit()
        # ── Schema-поезд (ArchCom 2026-08-27): A1 + C8 + C10 backfill ──
        SQLiteStore._migrate_c8_drop_turns_fts(conn)
        SQLiteStore._migrate_c10_backfill_rewrite_cols(conn)
        SQLiteStore._migrate_a1_ccr_composite_pk(conn)
        # ── Final review m3: rewrite_event_key backfill ──
        SQLiteStore._migrate_m3_backfill_rewrite_event_key(conn)
        # ── ADR-0019 Phase B (B1): pipeline_state backfill ──
        SQLiteStore._migrate_b1_backfill_pipeline_state(conn)
        # ── ADR-0030 A0 (issue #321): memory_edges edge-kinds rebuild ──
        SQLiteStore._migrate_a0_edges_kinds(conn)

    @staticmethod
    def _migrate_c8_drop_turns_fts(conn: sqlite3.Connection) -> None:
        """C8 — drop the dead ``turns_fts`` index and its three triggers.

        Idempotent (IF EXISTS on every connect): after the first run the
        statements are no-ops. Turn writes never touched the FTS table
        directly (only via the triggers), so nothing else changes.
        """
        for sql in _C8_DROP_SQL:
            conn.execute(sql)
        conn.commit()

    @staticmethod
    def _migrate_c10_backfill_rewrite_cols(conn: sqlite3.Connection) -> None:
        """C10 — one-time backfill of ``rewrite_source``/``rewrite_session``.

        Existing rows get the values extracted from the metadata JSON (the
        pre-C10 write path stored them there only). The meta-table flag is
        set in the same commit so an interrupted backfill re-runs on the
        next connect. Rows whose metadata is not valid JSON are skipped
        defensively (json_extract would raise); ``save()`` re-derives both
        columns on the next write of such a row anyway.

        CONCURRENT-CONNECT SAFE (review round): ``_run_migrations`` runs on
        every THREAD-LOCAL connection, and a manager's background threads
        (scanner loop, CCR cleanup) can open their first connection while
        the main thread is still inside this routine. Both connections then
        observe "flag absent" and both run the backfill. The UPDATE is
        idempotent (converges to the same values) and the flag INSERT is
        ``INSERT OR IGNORE`` — the losing racer no-ops instead of raising
        ``UNIQUE constraint failed: meta.key`` (the plain INSERT made
        random concurrent first-connects crash the store and flaked the
        REST suite).
        """
        flag = conn.execute(
            "SELECT 1 FROM meta WHERE key = ?", (_BACKFILL_REWRITE_COLS_FLAG,)
        ).fetchone()
        if flag is not None:
            return
        cur = conn.execute(
            "UPDATE memories SET "
            "rewrite_source = json_extract(metadata, '$.source'), "
            "rewrite_session = json_extract(metadata, '$.rewrite_session') "
            "WHERE json_valid(metadata) "
            "AND (json_extract(metadata, '$.source') IS NOT NULL "
            "     OR json_extract(metadata, '$.rewrite_session') IS NOT NULL)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value, updated_at) VALUES (?,?,?)",
            (
                _BACKFILL_REWRITE_COLS_FLAG,
                "1",
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()
        backfilled = int(cur.rowcount or 0)
        if backfilled:
            logger.info("C10 backfill: denormalised %d memory rows", backfilled)

    @staticmethod
    def _migrate_m3_backfill_rewrite_event_key(conn: sqlite3.Connection) -> None:
        """m3 (final review) — one-time backfill of ``rewrite_event_key``.

        Existing rows written by the trusted ``context_rewrite`` path
        (``metadata.source = 'context-rewrite'`` — the only writer of the
        key pre-m3) get the value promoted from the metadata JSON into the
        indexed column. The provenance gate is load-bearing: generic create
        surfaces accept client-controlled metadata, and a planted
        ``rewrite_event_key`` there must NOT enter the dedupe index (it
        would shadow future legitimate events as "already delivered").
        Rows with invalid JSON metadata are skipped defensively. The
        meta-table flag is set in the same commit so an interrupted
        backfill re-runs on the next connect; concurrent first-connects
        converge (idempotent UPDATE + ``INSERT OR IGNORE`` flag — same
        reasoning as the C10 backfill above).
        """
        flag = conn.execute(
            "SELECT 1 FROM meta WHERE key = ?", (_BACKFILL_REWRITE_EVENT_KEY_FLAG,)
        ).fetchone()
        if flag is not None:
            return
        cur = conn.execute(
            "UPDATE memories SET "
            "rewrite_event_key = json_extract(metadata, '$.rewrite_event_key') "
            "WHERE rewrite_event_key IS NULL "
            "AND json_valid(metadata) "
            "AND json_extract(metadata, '$.source') = 'context-rewrite' "
            "AND json_extract(metadata, '$.rewrite_event_key') IS NOT NULL"
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value, updated_at) VALUES (?,?,?)",
            (
                _BACKFILL_REWRITE_EVENT_KEY_FLAG,
                "1",
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()
        backfilled = int(cur.rowcount or 0)
        if backfilled:
            logger.info("m3 backfill: indexed rewrite_event_key on %d memory rows", backfilled)

    @staticmethod
    def _migrate_b1_backfill_pipeline_state(conn: sqlite3.Connection) -> None:
        """ADR-0019 Phase B (B1) — one-time classification of legacy rows.

        Heals the Hermes legacy into the orthogonal ``pipeline_state``
        column (``MemoryStatus`` itself is untouched):

        * **refined** — rows that went through the pipeline:
          ``status='processed'`` (only the quality gate writes that
          status), or ``status='published'`` WITH synthesis lineage:
          non-empty ``derived_from`` OR ``source='synthesized'``
          (``pipeline/synthesize.py`` writes BOTH on its output).
          ``cluster_id`` is deliberately NOT a lineage signal: the
          clustering stage writes it on raw members
          (``pipeline/cluster.py``) and the stuck-rescue path publishes
          those members WITHOUT synthesis (``run_pipeline`` →
          ``_promote_single_memory``) — a cluster-id-bearing rescue row
          is pending, not refined. ``processed_at = updated_at`` (the
          best available proxy for the historical swap time).
        * **pending** — ``status='published'`` WITHOUT lineage: the
          Hermes bypass heritage (``publish_on_write`` +
          ``skip_quality_check`` from ``raw``), the single-memory
          passthrough placeholder promotions (quality_score=0.5, no
          LLM refinement), AND the stuck-rescue rows above. All
          genuinely lack refinement; the B2
          daemon re-refines them. Lineage is the discriminator —
          ``quality_score`` is deliberately NOT used because
          ``MemoryUpdate`` lets clients write it, so it is not a
          provenance signal.
        * **NULL** — ``raw`` / ``processing`` / ``archived`` rows
          (legacy semantics, untouched by ADR-0019 B1).

        Idempotent: every UPDATE is guarded by ``pipeline_state IS
        NULL`` and the meta flag is set in the same commit
        (``INSERT OR IGNORE`` — concurrent first-connects converge, the
        same reasoning as the C10 backfill). The UPDATEs fire the
        ``memories_au`` trigger, which reindexes the SAME rowid in the
        external-content FTS table — rowids stay stable, no FTS rebuild
        is needed (verified by the B1 migration tests).
        """
        flag = conn.execute(
            "SELECT 1 FROM meta WHERE key = ?", (_BACKFILL_PIPELINE_STATE_FLAG,)
        ).fetchone()
        if flag is not None:
            return
        refined = conn.execute(
            "UPDATE memories SET pipeline_state = 'refined', processed_at = updated_at "
            "WHERE pipeline_state IS NULL "
            "AND (status = 'processed' "
            "     OR (status = 'published' "
            "         AND ((json_valid(derived_from) "
            "               AND json_array_length(derived_from) > 0) "
            "              OR source = 'synthesized')))"
        )
        pending = conn.execute(
            "UPDATE memories SET pipeline_state = 'pending' "
            "WHERE pipeline_state IS NULL AND status = 'published'"
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value, updated_at) VALUES (?,?,?)",
            (
                _BACKFILL_PIPELINE_STATE_FLAG,
                "1",
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()
        n_refined = int(refined.rowcount or 0)
        n_pending = int(pending.rowcount or 0)
        if n_refined or n_pending:
            logger.info(
                "B1 backfill: pipeline_state classified %d rows as refined, %d as pending",
                n_refined,
                n_pending,
            )

    @staticmethod
    def _migrate_a0_edges_kinds(conn: sqlite3.Connection) -> None:
        """A0 (ADR-0030, issue #321) — rebuild ``memory_edges`` onto the final shape.

        SQLite CHECK constraints cannot be ALTERed, so the supersedes-only
        table (ADR-0018 Phase 1) is rebuilt with the two-kind CHECK
        (``supersedes``, ``relates_to``) plus ``weight`` / ``provenance`` /
        ``scope_project`` / ``scope_agent``. Detection is by column shape
        (PRAGMA table_info): fresh DBs (created in the final shape by
        _DB_SCHEMA) and already-migrated DBs skip; a missing table (no
        memory_edges at all) is left to _DB_SCHEMA. Production carried 0
        rows (live probe 2026-09-15) — the copy path still runs in full so
        an older non-empty install survives: legacy rows copy with the
        column defaults (weight 1.0, provenance 'declared', scopes NULL),
        which is exactly what those rows meant.

        CRASH SAFETY: create+copy+drop+rename run inside ONE explicit
        transaction (the A1 pattern — SQLite DDL is transactional); a
        crash before COMMIT rolls back to the intact legacy table, and the
        leading ``DROP TABLE IF EXISTS`` converges an orphan rebuild table
        from a pre-transactional crash. The post-rename schema re-exec
        stays OUTSIDE the transaction (idempotent, self-healing) and
        restores the two edge indexes in one place.
        """
        info = conn.execute("PRAGMA table_info(memory_edges)").fetchall()
        cols = {str(row[1]) for row in info}
        if "weight" in cols or not cols:
            # Final shape already (fresh/migrated) or no table — nothing
            # to rebuild; still converge a possible orphan from a crash.
            conn.execute("DROP TABLE IF EXISTS memory_edges_a0_rebuild")
            conn.commit()
            return
        total = int(conn.execute("SELECT COUNT(*) FROM memory_edges").fetchone()[0])
        try:
            # B608: the script is composed EXCLUSIVELY of static module
            # constants and literals — no user-controlled fragment ever
            # enters it. One transaction so the DDL is atomic.
            conn.executescript(
                "BEGIN IMMEDIATE;\n"  # nosec B608 - static literal
                "DROP TABLE IF EXISTS memory_edges_a0_rebuild;\n"  # nosec B608
                + _EDGES_REBUILD_DDL.rstrip()
                + ";\n"  # nosec B608 - static literal
                + "INSERT INTO memory_edges_a0_rebuild "  # nosec B608 - static copy stmt
                "(from_memory_id, to_memory_id, kind, created_at, weight, "
                " provenance, scope_project, scope_agent) "
                "SELECT from_memory_id, to_memory_id, kind, created_at, 1.0, "
                "       'declared', NULL, NULL FROM memory_edges;\n"
                "DROP TABLE memory_edges;\n"
                "ALTER TABLE memory_edges_a0_rebuild RENAME TO memory_edges;\n"
                "COMMIT;"
            )
        except Exception:
            # Roll the half-run script back so the next connect sees the
            # intact legacy table and re-runs cleanly; re-raise as-is.
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        # Restore the edge indexes (and re-assert the rest of the schema).
        conn.executescript(_DB_SCHEMA)
        conn.commit()
        logger.info(
            "A0 edge-kinds migration: memory_edges rebuilt with "
            "relates_to/weight/provenance/scope: %d rows kept",
            total,
        )

    @staticmethod
    def _migrate_a1_ccr_composite_pk(conn: sqlite3.Connection) -> None:
        """A1 — rebuild ``ccr_cache`` onto the composite PK ``(project, hash)``.

        Detection is by PK shape (PRAGMA table_info pk positions), so fresh
        DBs (already created with the composite PK by _DB_SCHEMA) and
        already-migrated DBs skip this. Legacy hash-PK tables are rebuilt:
        create the new table, copy FIRST-WRITER-WINS (one row per hash —
        the lowest rowid, i.e. the earliest stored copy; the cache is
        derived data and recompressible, so dropped duplicates are
        acceptable by committee decision), drop the old table, rename.
        Rowids are preserved through the copy so the external-content
        ``ccr_cache_fts`` stays addressable; the FTS index is rebuilt
        afterwards regardless (also repairs any pre-existing desync).
        ``_DB_SCHEMA`` is re-executed after the rename to restore the
        ccr_cache indexes and triggers in one place (all IF NOT EXISTS —
        the rest of the script no-ops).

        Review round — CRASH SAFETY: create+copy+drop+rename run inside
        ONE explicit transaction (``BEGIN IMMEDIATE … COMMIT`` via
        executescript; SQLite DDL is transactional). The pre-fix sequence
        of autocommitted statements had a crash wedge: a process death
        between CREATE ``ccr_cache_a1_rebuild`` and the RENAME left an
        orphan rebuild table, and the reopen re-ran the plain CREATE →
        OperationalError from ``_get_conn`` → the store became PERMANENTLY
        unopenable. Now a crash before COMMIT rolls back to the intact
        legacy table (reopen re-runs the migration cleanly), and the
        leading ``DROP TABLE IF EXISTS`` converges an orphan left by any
        pre-fix crash. The post-rename schema re-exec and the FTS 'rebuild'
        stay OUTSIDE the transaction (idempotent, self-healing on the next
        connect) as before.
        """
        info = conn.execute("PRAGMA table_info(ccr_cache)").fetchall()
        pk_positions = {str(row[1]): int(row[5]) for row in info}
        already_composite = pk_positions.get("project") == 1 and pk_positions.get("hash") == 2
        if already_composite or not pk_positions:
            # Already composite (fresh/migrated) or no ccr_cache table —
            # nothing to rebuild. Still converge a possible orphan from a
            # PRE-transactional crash: in the worst window the legacy table
            # was dropped, the rename never ran, and the schema script has
            # just recreated an empty composite cache — the half-built
            # ``ccr_cache_a1_rebuild`` would otherwise linger forever.
            conn.execute("DROP TABLE IF EXISTS ccr_cache_a1_rebuild")
            conn.commit()
            return
        total = int(conn.execute("SELECT COUNT(*) FROM ccr_cache").fetchone()[0])
        try:
            # B608: the script is composed EXCLUSIVELY of static module
            # constants and literals (transaction keywords + the DDL
            # constant + a fully-static copy statement) — no user-controlled
            # fragment ever enters it. One transaction so DDL is atomic.
            conn.executescript(
                "BEGIN IMMEDIATE;\n"  # nosec B608 - static literal
                # Converge an orphaned rebuild table from a pre-fix crash.
                "DROP TABLE IF EXISTS ccr_cache_a1_rebuild;\n"
                + _CCR_REBUILD_DDL.rstrip()
                + ";\n"  # nosec B608 - static literal
                + "INSERT INTO ccr_cache_a1_rebuild "  # nosec B608 - static copy stmt
                "(rowid, hash, original, project, created_at, size_bytes, "
                " retrieval_count, last_retrieved_at, secret_scan_verdict, "
                " secret_scan_at, issuer_agent, issuer_session) "
                "SELECT rowid, hash, original, project, created_at, size_bytes, "
                "       retrieval_count, last_retrieved_at, secret_scan_verdict, "
                "       secret_scan_at, issuer_agent, issuer_session "
                "FROM ccr_cache "
                "WHERE rowid IN (SELECT MIN(rowid) FROM ccr_cache GROUP BY hash);\n"
                "DROP TABLE ccr_cache;\n"
                "ALTER TABLE ccr_cache_a1_rebuild RENAME TO ccr_cache;\n"
                "COMMIT;"
            )
        except Exception:
            # Roll the half-run script back so the next connect sees the
            # intact legacy table and re-runs cleanly; re-raise as-is.
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        # Restore indexes + triggers (and re-assert the rest of the schema).
        conn.executescript(_DB_SCHEMA)
        # Re-sync the external-content FTS index with the rebuilt table.
        conn.execute("INSERT INTO ccr_cache_fts(ccr_cache_fts) VALUES('rebuild')")
        conn.commit()
        kept = int(conn.execute("SELECT COUNT(*) FROM ccr_cache").fetchone()[0])
        logger.info(
            "A1 migration: ccr_cache rebuilt onto composite PK (project, hash): "
            "%d rows kept, %d duplicate-hash rows dropped (first-writer-wins)",
            kept,
            total - kept,
        )

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn:
            conn.close()
            self._local.conn = None

    # ── Row conversion ────────────────────────────────────────────────────

    def _row_to_memory(self, row: sqlite3.Row) -> Memory:
        keys = set(row.keys())

        def _get(k: str, default: Any = None) -> Any:
            return row[k] if k in keys else default

        return Memory(
            id=row["id"],
            content=row["content"],
            title=row["title"],
            tags=json.loads(row["tags"]),
            source=MemorySource(row["source"]),
            source_url=row["source_url"],
            memory_type=MemoryType(row["memory_type"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            metadata=json.loads(row["metadata"]),
            file_path=row["file_path"],
            category=_get("category"),
            project=_get("project", ""),
            agent=_get("agent", ""),
            status=MemoryStatus(_get("status", "raw")),
            quality_score=_get("quality_score"),
            confidence=_get("confidence"),
            source_coverage=_get("source_coverage"),
            cluster_id=_get("cluster_id"),
            derived_from=json.loads(_get("derived_from") or "[]"),
            embedding_id=_get("embedding_id"),
            raw_content=_get("raw_content"),
            clean_content=_get("clean_content"),
            filter_profile=_get("filter_profile"),
            filter_stats=json.loads(_get("filter_stats")) if _get("filter_stats") else None,
            filter_version=_get("filter_version"),
            # mnemos #96 — workflow projection (read-only here; writes go
            # through set_workflow_status so the state machine cannot be
            # bypassed). NULL on legacy rows created before the migration.
            workflow_status=_get("workflow_status"),
            locked_by=_get("locked_by"),
            locked_at=_get("locked_at"),
            # ADR-0019 Phase B (B1) — pipeline lifecycle projection.
            # Strict enum (same contract as status above: values are only
            # written by the backfill and store-internal paths). NULL on
            # legacy rows / non-optimistic entries.
            pipeline_state=(
                PipelineState(_get("pipeline_state")) if _get("pipeline_state") else None
            ),
            processed_at=(
                datetime.fromisoformat(_get("processed_at")) if _get("processed_at") else None
            ),
            swap_key=_get("swap_key"),
            quarantine_reason=_get("quarantine_reason"),
            marker_version=int(_get("marker_version", 1) or 1),
        )

    def rebuild_fts_index(self) -> int:
        """Rebuild the FTS5 index from the memories table.

        Use when the FTS5 external-content table is desynced from
        ``memories`` (e.g. after INSERT OR REPLACE corruption or manual
        row deletion).  Returns the number of rows indexed.
        """
        conn = self._get_conn()
        conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
        conn.commit()
        count = int(conn.execute("SELECT count(*) FROM memories_fts").fetchone()[0])
        self._invalidate_caches()
        return count

    def _invalidate_caches(self) -> None:
        self._cache.invalidate(
            "tags",
            "projects_counts",
            "agents_counts",
            "types_counts",
            "data_health",
            "stats",
        )
        self._cache.invalidate_prefix("graph_")
        self._cache.invalidate_prefix("agent_")
        self._cache.invalidate_prefix("project_")

    # ── CRUD ──────────────────────────────────────────────────────────────

    def save(self, memory: Memory, *, trusted_rewrite_provenance: bool = False) -> None:
        """Insert or update a memory, keeping the FTS5 index consistent.

        Uses UPDATE for existing rows (fires AFTER UPDATE trigger) and
        INSERT for new rows (fires AFTER INSERT trigger).  Never uses
        INSERT OR REPLACE — that fires the INSERT trigger with a new
        rowid while the FTS5 external-content table still references the
        old rowid, causing ``missing row N from content table`` errors.

        C10 review round — TRUSTED GATE on the denormalised rewrite
        provenance: ``rewrite_source`` / ``rewrite_session`` derive from
        ``memory.metadata`` ONLY when the trusted rewrite-event path
        (``context_rewrite`` → ``MemoryManager.add``) passes
        ``trusted_rewrite_provenance=True``. The generic create paths
        (REST/MCP/CLI) accept client-controlled ``metadata`` and must
        never mint rewrite quota counters — planted
        ``source='context-rewrite'`` + ``rewrite_session`` there would
        otherwise burn the rewrite channel's per-session and per-project
        quotas (429 DoS) without ever touching the rewrite API. On the
        untrusted UPDATE path the two columns are left UNTOUCHED (an
        edited legitimate event keeps counting; a forged row never had
        them derived in the first place).

        m3 (final review) — ``rewrite_event_key`` joins the same trusted
        gate: the column backs the event-dedupe lookup index, and a key
        planted through client metadata would shadow future legitimate
        events as "already delivered" (silent write suppression).
        """
        conn = self._get_conn()
        existing = conn.execute("SELECT 1 FROM memories WHERE id = ?", (memory.id,)).fetchone()
        title = memory.auto_title()
        tags_json = json.dumps(memory.tags, ensure_ascii=False)
        meta_json = json.dumps(memory.metadata, ensure_ascii=False)
        derived_json = json.dumps(memory.derived_from, ensure_ascii=False)
        filter_stats_json = (
            json.dumps(memory.filter_stats, ensure_ascii=False) if memory.filter_stats else None
        )
        # ADR-0019 Phase B (B1) — pipeline lifecycle round-trip. The
        # generic save() persists whatever the in-memory object carries
        # (loaded rows keep their state; fresh Memory objects default to
        # NULL/1). Authoritative lifecycle transitions go through
        # update_fields instead (see the whitelist note there).
        pipeline_state_val = memory.pipeline_state.value if memory.pipeline_state else None
        processed_at_val = memory.processed_at.isoformat() if memory.processed_at else None
        # C10 — denormalised rewrite provenance (metadata is the source of
        # truth; these columns exist purely so the rewrite quota counts are
        # index-backed). Gated on the trusted caller — see the docstring.
        # Only non-empty str values are promoted; anything else (missing
        # key, JSON object, empty string) stores NULL.
        # m3 (final review) — rewrite_event_key joins the same trusted
        # derivation: the dedupe lookup index must never see a key planted
        # through client-controlled metadata.
        if trusted_rewrite_provenance:
            meta_src = memory.metadata.get("source")
            rewrite_source = meta_src if isinstance(meta_src, str) and meta_src else None
            meta_sess = memory.metadata.get("rewrite_session")
            rewrite_session = meta_sess if isinstance(meta_sess, str) and meta_sess else None
            meta_key = memory.metadata.get("rewrite_event_key")
            rewrite_event_key = meta_key if isinstance(meta_key, str) and meta_key else None
        else:
            rewrite_source = None
            rewrite_session = None
            rewrite_event_key = None
        if existing:
            if trusted_rewrite_provenance:
                conn.execute(
                    """UPDATE memories SET
                       content = ?, title = ?, tags = ?, source = ?, source_url = ?,
                       memory_type = ?, created_at = ?, updated_at = ?, metadata = ?,
                       file_path = ?, category = ?, project = ?, agent = ?, status = ?,
                       quality_score = ?, confidence = ?, source_coverage = ?,
                       cluster_id = ?, derived_from = ?, embedding_id = ?,
                       raw_content = ?, clean_content = ?, filter_profile = ?,
                       filter_stats = ?, filter_version = ?,
                       rewrite_source = ?, rewrite_session = ?, rewrite_event_key = ?,
                       pipeline_state = ?, processed_at = ?, swap_key = ?,
                       quarantine_reason = ?, marker_version = ?
                       WHERE id = ?""",
                    (
                        memory.content,
                        title,
                        tags_json,
                        memory.source.value,
                        memory.source_url,
                        memory.memory_type.value,
                        memory.created_at.isoformat(),
                        memory.updated_at.isoformat(),
                        meta_json,
                        memory.file_path,
                        memory.category,
                        memory.project,
                        memory.agent,
                        memory.status.value,
                        memory.quality_score,
                        memory.confidence,
                        memory.source_coverage,
                        memory.cluster_id,
                        derived_json,
                        memory.embedding_id,
                        memory.raw_content,
                        memory.clean_content,
                        memory.filter_profile,
                        filter_stats_json,
                        memory.filter_version,
                        rewrite_source,
                        rewrite_session,
                        rewrite_event_key,
                        pipeline_state_val,
                        processed_at_val,
                        memory.swap_key,
                        memory.quarantine_reason,
                        memory.marker_version,
                        memory.id,
                    ),
                )
            else:
                # Untrusted UPDATE: the rewrite columns are NOT in the SET
                # list — preserved as-is (cannot be forged or erased here).
                conn.execute(
                    """UPDATE memories SET
                       content = ?, title = ?, tags = ?, source = ?, source_url = ?,
                       memory_type = ?, created_at = ?, updated_at = ?, metadata = ?,
                       file_path = ?, category = ?, project = ?, agent = ?, status = ?,
                       quality_score = ?, confidence = ?, source_coverage = ?,
                       cluster_id = ?, derived_from = ?, embedding_id = ?,
                       raw_content = ?, clean_content = ?, filter_profile = ?,
                       filter_stats = ?, filter_version = ?,
                       pipeline_state = ?, processed_at = ?, swap_key = ?,
                       quarantine_reason = ?, marker_version = ?
                       WHERE id = ?""",
                    (
                        memory.content,
                        title,
                        tags_json,
                        memory.source.value,
                        memory.source_url,
                        memory.memory_type.value,
                        memory.created_at.isoformat(),
                        memory.updated_at.isoformat(),
                        meta_json,
                        memory.file_path,
                        memory.category,
                        memory.project,
                        memory.agent,
                        memory.status.value,
                        memory.quality_score,
                        memory.confidence,
                        memory.source_coverage,
                        memory.cluster_id,
                        derived_json,
                        memory.embedding_id,
                        memory.raw_content,
                        memory.clean_content,
                        memory.filter_profile,
                        filter_stats_json,
                        memory.filter_version,
                        pipeline_state_val,
                        processed_at_val,
                        memory.swap_key,
                        memory.quarantine_reason,
                        memory.marker_version,
                        memory.id,
                    ),
                )
        else:
            conn.execute(
                """INSERT INTO memories
                   (id, content, title, tags, source, source_url, memory_type,
                    created_at, updated_at, metadata, file_path, category,
                    project, agent, status, quality_score, confidence,
                    source_coverage, cluster_id, derived_from, embedding_id,
                    raw_content, clean_content, filter_profile, filter_stats,
                    filter_version, rewrite_source, rewrite_session,
                    rewrite_event_key, pipeline_state, processed_at, swap_key,
                    quarantine_reason, marker_version)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    memory.id,
                    memory.content,
                    title,
                    tags_json,
                    memory.source.value,
                    memory.source_url,
                    memory.memory_type.value,
                    memory.created_at.isoformat(),
                    memory.updated_at.isoformat(),
                    meta_json,
                    memory.file_path,
                    memory.category,
                    memory.project,
                    memory.agent,
                    memory.status.value,
                    memory.quality_score,
                    memory.confidence,
                    memory.source_coverage,
                    memory.cluster_id,
                    derived_json,
                    memory.embedding_id,
                    memory.raw_content,
                    memory.clean_content,
                    memory.filter_profile,
                    filter_stats_json,
                    memory.filter_version,
                    # NULL on the generic path regardless of metadata claims.
                    rewrite_source,
                    rewrite_session,
                    rewrite_event_key,
                    pipeline_state_val,
                    processed_at_val,
                    memory.swap_key,
                    memory.quarantine_reason,
                    memory.marker_version,
                ),
            )
        conn.commit()
        self._invalidate_caches()

    def get(self, memory_id: str) -> Memory | None:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return self._row_to_memory(row) if row else None

    def delete(self, memory_id: str) -> bool:
        conn = self._get_conn()
        cur = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        conn.commit()
        self._invalidate_caches()
        return cur.rowcount > 0

    def update_status(self, memory_id: str, status: MemoryStatus) -> bool:
        conn = self._get_conn()
        now = datetime.now(UTC).isoformat()
        cur = conn.execute(
            "UPDATE memories SET status=?, updated_at=? WHERE id=?",
            (status.value, now, memory_id),
        )
        conn.commit()
        self._invalidate_caches()
        return cur.rowcount > 0

    def update_fields(self, memory_id: str, **kwargs: Any) -> bool:
        """Update arbitrary fields on a memory row.

        M15.2 security hardening: only columns present in the module-level
        `_FIELD_UPDATERS` whitelist are accepted. Column names are taken
        from the static dict (never from kwargs), so the constructed SQL
        contains no user-controlled identifiers. All values are bound
        parameters. B608 (SQL injection) is impossible by construction.

        Unknown keys are silently dropped (defence in depth — column names
        are taken from the whitelist, not from kwargs, so the SQL body is
        safe regardless of what the caller passes).
        """
        if not kwargs:
            return False
        # Drop keys not in the whitelist before they reach the SQL builder.
        # The only filter needed: setters are built from the dict
        # values (static SQL fragments), and values are bound parameters.
        updates: dict[str, Any] = {k: kwargs[k] for k in _FIELD_UPDATERS if k in kwargs}
        if not updates:
            return False
        # Serialise JSON fields (only str accepted per the strict whitelist).
        for field in ("derived_from", "tags", "filter_stats", "metadata"):
            if field in updates and not isinstance(updates[field], str):
                updates[field] = json.dumps(updates[field], ensure_ascii=False)
        updates["updated_at"] = datetime.now(UTC).isoformat()
        # `setters` is built by joining whitelisted static fragments only —
        # no user input flows into the column names. Values are bound `?`.
        setters = ", ".join(_FIELD_UPDATERS[k] for k in updates if k != "updated_at")
        setters = setters + ", updated_at=?"
        values = [updates[k] for k in updates if k != "updated_at"]
        values.append(updates["updated_at"])
        values.append(memory_id)
        conn = self._get_conn()
        # B608: setters built from `_FIELD_UPDATERS` whitelist, not user input.
        cur = conn.execute(
            "UPDATE memories SET " + setters + " WHERE id=?",  # nosec B608
            values,
        )
        conn.commit()
        self._invalidate_caches()
        return cur.rowcount > 0

    # ── ADR-0019 Phase B2a: refine intake / claim / retry ────────────────
    #
    # The async-refinement daemon's queue lives in the orthogonal
    # ``pipeline_state`` column. These are the ONLY writers of the
    # pending→processing→{refined|failed|quarantined} transitions and of
    # the retry bookkeeping (metadata JSON keys — the schema itself is
    # closed since B1). SQL is static with bound parameters throughout.

    @staticmethod
    def _refine_intake_where(max_attempts: int, now_iso: str) -> tuple[str, list[Any]]:
        """Shared WHERE for the refine intake (SELECT and COUNT).

        A row is intake-eligible when it is ``pending``, OR ``failed``
        with retry budget left (``pipeline_retry_count`` < max) AND the
        exponential backoff has elapsed (``pipeline_retry_at`` <= now;
        the empty sentinel / missing key is always eligible — a fresh
        cycle after manual release or re-publication). ``processing``
        rows are owned by a worker (claim CAS), ``refined``/``quarantined``
        are terminal-adjacent, NULL rows are the untouched legacy flow.
        """
        where = (
            "(pipeline_state = 'pending' "
            "OR (pipeline_state = 'failed' "
            "    AND CAST(COALESCE(json_extract(metadata, '$.pipeline_retry_count'), 0) "
            "        AS INTEGER) < ? "
            "    AND COALESCE(json_extract(metadata, '$.pipeline_retry_at'), '') <= ?))"
        )
        return where, [max_attempts, now_iso]

    def list_refine_intake(
        self,
        *,
        limit: int = 100,
        project: str | None = None,
        agent: str | None = None,
        max_attempts: int = 3,
        now_iso: str | None = None,
    ) -> list[Memory]:
        """Select rows awaiting async refinement (§10 Phase B pickup)."""
        where, params = self._refine_intake_where(
            max_attempts, now_iso or datetime.now(UTC).isoformat()
        )
        q = f"SELECT * FROM memories WHERE {where}"  # nosec B608 - static fragment
        if project:
            q += " AND project=?"
            params.append(project)
        if agent:
            q += " AND agent=?"
            params.append(agent)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        conn = self._get_conn()
        return [self._row_to_memory(r) for r in conn.execute(q, params).fetchall()]

    def count_refine_intake(self, *, max_attempts: int = 3, now_iso: str | None = None) -> int:
        """COUNT over the same intake predicate (queue-depth statistics)."""
        where, params = self._refine_intake_where(
            max_attempts, now_iso or datetime.now(UTC).isoformat()
        )
        row = (
            self._get_conn()
            .execute(
                f"SELECT COUNT(*) FROM memories WHERE {where}",  # nosec B608 - static fragment
                params,
            )
            .fetchone()
        )
        return int(row[0]) if row else 0

    def claim_for_refinement(
        self,
        memory_id: str,
        *,
        max_attempts: int = 3,
        now_iso: str | None = None,
    ) -> bool:
        """Atomic grab: intake-eligible → ``processing`` (compare-and-set).

        The WHERE clause re-checks the FULL intake predicate (not just
        ``id``), so a concurrent second worker that selected the same row
        loses the race and gets ``False`` — the existing no-op convention
        of this store's retry/grab sites. A row already ``processing``,
        ``refined``, ``quarantined`` or retry-exhausted never matches.

        The claim also stamps ``updated_at`` — this is the LEASE START
        (issue #170 / ADR-0019 Phase C): a worker that crashes between
        the claim and its outcome write leaves the row ``processing``
        with a frozen clock, and the sweeper's reclaim decides expiry
        purely on ``updated_at``. Without the stamp the clock would run
        from the LAST pre-claim write (possibly the enqueue itself), and
        a long-queued row would be reclaimed out from under a live
        worker.
        """
        where, params = self._refine_intake_where(
            max_attempts, now_iso or datetime.now(UTC).isoformat()
        )
        now = datetime.now(UTC).isoformat()
        conn = self._get_conn()
        cur = conn.execute(
            f"UPDATE memories SET pipeline_state='processing', updated_at=? "  # nosec B608
            f"WHERE id=? AND {where}",  # nosec B608 - static fragment
            [now, memory_id, *params],
        )
        conn.commit()
        self._invalidate_caches()
        return cur.rowcount > 0

    def reclaim_stale_processing(
        self,
        *,
        lease_timeout_sec: int,
        limit: int = 100,
        now_iso: str | None = None,
    ) -> list[tuple[str, str]]:
        """Issue #170 (ADR-0019 Phase C): lease-expired ``processing`` →
        ``pending`` (compare-and-set), idempotently and race-safely.

        A worker crash between :meth:`claim_for_refinement` and its
        outcome write strands the row in ``processing`` forever — this is
        the reclaim path back into the intake. Lease expiry is decided
        on ``updated_at`` (stamped at the claim by this store, refreshed
        by every outcome write):

        * selection — rows still ``processing`` whose ``updated_at`` is
          older than the cutoff;
        * per-row CAS — the UPDATE re-checks BOTH ``pipeline_state=
          'processing'`` AND ``updated_at < cutoff``, so two concurrent
          sweepers (or a sweeper racing a live worker that just
          re-touched the row) collapse to exactly ONE winner; the loser's
          ``rowcount`` is 0 and the row is silently left alone.

        The retry counter in metadata is deliberately NOT touched: a
        lease expiry is an infrastructure event, not a lane-(a) failure
        (§5 counting semantics — it must not eat the row's retry budget).

        Returns ``[(id, updated_at_at_reclaim), …]`` for the audit lines
        (``outcome=lease-reclaimed age=…``) — the pre-reclaim timestamp
        is what the age is computed from.
        """
        now = now_iso or datetime.now(UTC).isoformat()
        cutoff = (datetime.fromisoformat(now) - timedelta(seconds=lease_timeout_sec)).isoformat()
        conn = self._get_conn()
        stale = conn.execute(
            "SELECT id, updated_at FROM memories "
            "WHERE pipeline_state='processing' AND updated_at < ? "
            "ORDER BY updated_at LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        reclaimed: list[tuple[str, str]] = []
        for row_id, updated_at in stale:
            cur = conn.execute(
                "UPDATE memories SET pipeline_state='pending', updated_at=? "
                "WHERE id=? AND pipeline_state='processing' AND updated_at < ?",
                (now, row_id, cutoff),
            )
            if cur.rowcount:
                reclaimed.append((row_id, updated_at))
        conn.commit()
        if reclaimed:
            self._invalidate_caches()
        return reclaimed

    def record_refine_failure(
        self,
        memory_id: str,
        *,
        attempt: int,
        next_retry_at: str | None,
    ) -> bool:
        """Lane-(a) outcome in ONE transaction: ``failed`` + retry bookkeeping.

        The retry counter lives in the metadata JSON (B1 closed the
        schema — no new column); ``json_set`` updates the keys in place
        so concurrent metadata writers are merged, not clobbered.
        ``next_retry_at=None`` means the retry budget is exhausted: the
        scheduling key is reset to the always-eligible sentinel and only
        the attempt counter still gates (the intake's ``< max`` check).
        """
        conn = self._get_conn()
        cur = conn.execute(
            "UPDATE memories SET pipeline_state='failed', "
            "metadata = json_set(COALESCE(metadata, '{}'), "
            "    '$.pipeline_retry_count', ?, '$.pipeline_retry_at', ?), "
            "updated_at=? WHERE id=?",
            (attempt, next_retry_at or "", datetime.now(UTC).isoformat(), memory_id),
        )
        conn.commit()
        self._invalidate_caches()
        return cur.rowcount > 0

    def clear_refine_retry(self, memory_id: str) -> bool:
        """Reset the retry bookkeeping for a fresh cycle.

        Writers: the publish entry point (manual re-publication of a
        ``failed`` row starts a new cycle) and ``release_quarantine`` (a
        human reviewed the row — it must not inherit the exhausted
        counter of the pre-quarantine cycles).
        """
        conn = self._get_conn()
        cur = conn.execute(
            "UPDATE memories SET metadata = json_set(COALESCE(metadata, '{}'), "
            "'$.pipeline_retry_count', 0, '$.pipeline_retry_at', ''), updated_at=? "
            "WHERE id=?",
            (datetime.now(UTC).isoformat(), memory_id),
        )
        conn.commit()
        self._invalidate_caches()
        return cur.rowcount > 0

    def list_by_pipeline_state(
        self,
        pipeline_state: PipelineState,
        *,
        limit: int = 100,
        offset: int = 0,
        project: str | None = None,
        agent: str | None = None,
    ) -> list[Memory]:
        """Select rows by the orthogonal lifecycle state (sweeper/stats).

        ``offset`` pages through the set (keyed on the same
        ``created_at DESC`` order) so a bounded-per-call consumer — the
        heal sweeper's vintage migration — can drain a set larger than
        ``limit`` over successive passes instead of re-reading the same
        head window forever.
        """
        q = "SELECT * FROM memories WHERE pipeline_state=?"
        params: list[Any] = [pipeline_state.value]
        if project:
            q += " AND project=?"
            params.append(project)
        if agent:
            q += " AND agent=?"
            params.append(agent)
        q += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.append(limit)
        params.append(max(0, offset))
        conn = self._get_conn()
        return [self._row_to_memory(r) for r in conn.execute(q, params).fetchall()]

    def count_by_pipeline_state(self) -> dict[str, int]:
        """COUNT grouped by ``pipeline_state`` (NULL rows keyed as ``legacy``)."""
        rows = (
            self._get_conn()
            .execute(
                "SELECT COALESCE(pipeline_state, 'legacy') AS s, COUNT(*) AS c "
                "FROM memories GROUP BY s"
            )
            .fetchall()
        )
        return {str(r[0]): int(r[1]) for r in rows}

    # ── Workflow lifecycle (mnemos #96) ────────────────────────────────────
    #
    # These methods are the ONLY writers of the workflow_status / locked_by /
    # locked_at columns and the memory_workflow_history table. They are
    # intentionally NOT exposed via update_fields/_FIELD_UPDATERS so the
    # state machine in MemoryManager.workflow_set cannot be bypassed by a
    # generic field update. The SQL is static (no user-controlled column
    # names) and uses bound parameters throughout, so B608 is impossible by
    # construction.

    def get_workflow_status(self, memory_id: str) -> dict[str, Any] | None:
        """Return the current workflow projection for a memory.

        Returns ``None`` when the memory does not exist so callers can map
        that to a 404. The dict always contains the three keys (values may
        be ``None`` on legacy rows).
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT workflow_status, locked_by, locked_at FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "workflow_status": row["workflow_status"],
            "locked_by": row["locked_by"],
            "locked_at": row["locked_at"],
        }

    def set_workflow_status(
        self,
        memory_id: str,
        workflow_status: str | None,
        locked_by: str | None,
        locked_at: str | None,
    ) -> bool:
        """Write the three workflow columns. Does NOT touch history.

        Caller (MemoryManager.workflow_set) is responsible for the audit
        row and for the state-machine / lock / rate-limit guardrails. This
        method is the low-level writer only.
        """
        conn = self._get_conn()
        cur = conn.execute(
            "UPDATE memories SET workflow_status=?, locked_by=?, locked_at=?, "
            "updated_at=? WHERE id=?",
            (
                workflow_status,
                locked_by,
                locked_at,
                datetime.now(UTC).isoformat(),
                memory_id,
            ),
        )
        conn.commit()
        return cur.rowcount > 0

    def add_workflow_history(self, entry: dict[str, Any]) -> None:
        """Append an immutable audit row to memory_workflow_history."""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO memory_workflow_history "
            "(id, memory_id, from_status, to_status, actor, reason, force_used, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                entry["id"],
                entry["memory_id"],
                entry["from_status"],
                entry["to_status"],
                entry["actor"],
                entry["reason"],
                entry["force_used"],
                entry["created_at"],
            ),
        )
        conn.commit()

    def get_workflow_history(self, memory_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return audit rows for a memory, newest first."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, memory_id, from_status, to_status, actor, reason, "
            "force_used, created_at "
            "FROM memory_workflow_history WHERE memory_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (memory_id, limit),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "memory_id": r["memory_id"],
                "from_status": r["from_status"],
                "to_status": r["to_status"],
                "actor": r["actor"],
                "reason": r["reason"],
                "force_used": bool(r["force_used"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def count_workflow_transitions_since(self, memory_id: str, since_iso: str) -> int:
        """Count audit rows for ``memory_id`` at/after ``since_iso``.

        Backs the per-memory rate-limit guardrail (#96 guardrail 5).
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM memory_workflow_history "
            "WHERE memory_id = ? AND created_at >= ?",
            (memory_id, since_iso),
        ).fetchone()
        return int(row["n"]) if row else 0

    # ── Listing ───────────────────────────────────────────────────────────

    def list_all(
        self,
        limit: int = 50,
        offset: int = 0,
        *,
        source: MemorySource | None = None,
        memory_type: MemoryType | None = None,
        tags: list[str] | None = None,
        status: MemoryStatus | None = None,
        project: str | None = None,
        agent: str | None = None,
        category: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[Memory]:
        conn = self._get_conn()
        q = "SELECT * FROM memories WHERE 1=1"
        params: list[Any] = []
        if source:
            q += " AND source=?"
            params.append(source.value)
        if memory_type:
            q += " AND memory_type=?"
            params.append(memory_type.value)
        if status:
            q += " AND status=?"
            params.append(status.value)
        if project:
            q += " AND project=?"
            params.append(project)
        if agent:
            q += " AND agent=?"
            params.append(agent)
        if tags:
            for tag in tags:
                q += " AND EXISTS (SELECT 1 FROM json_each(tags) WHERE json_each.value = ?)"
                params.append(tag)
        if category is not None:
            if category == "__uncategorized":
                q += " AND category IS NULL"
            else:
                q += " AND (category=? OR category LIKE ?)"
                params.extend([category, f"{category}/%"])
        if since:
            q += " AND created_at >= ?"
            params.append(since)
        if until:
            q += " AND created_at <= ?"
            params.append(until)
        q += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return [self._row_to_memory(r) for r in conn.execute(q, params).fetchall()]

    def list_all_for_mesh(
        self,
        limit: int = 50,
        *,
        projects: list[str] | None = None,
        tags: list[str] | None = None,
        since: str | None = None,
        after_rowid: int = 0,
    ) -> list[tuple[Memory, int]]:
        """Mesh export listing with storage rowids, ordered for cursor resume.

        Serves :rpc:`MnemosCore.ListMemories` (ADR-0020 cursor contract).
        Unlike :meth:`list_all` this returns ``(memory, rowid)`` pairs and
        orders by ``rowid ASC`` — a forward walk in storage-revision order,
        so every page boundary is a valid opaque-cursor checkpoint: resuming
        with ``rowid > checkpoint`` yields exactly the undelivered remainder
        (no dupes, no gaps across projects — the rowid space is global).

        Args:
            limit: Maximum rows to fetch (caller passes page_limit + 1 to
                detect ``has_more``).
            projects: Project slugs to restrict to (SQL ``IN``). ``None``
                or empty = no project filter (the caller has already
                applied the peer ACL intersection).
            tags: Require ALL of these tags (same ``json_each`` semantics
                as :meth:`list_all`).
            since: Legacy ISO lower bound on ``created_at`` (ignored by
                the caller when a resume checkpoint is present).
            after_rowid: Only rows with ``rowid > after_rowid`` (ADR-0020
                resume path; ``0`` = no bound).

        Scope stability on resume is the CALLER's duty: widening the
        project/tag scope after a checkpoint silently skips rows with
        ``rowid <= checkpoint`` that the narrower walk never delivered —
        inherent to rowid cursors (ADR-0020), so the caller must replay a
        cursor only against the scope it was minted for.

        Known limitation (accepted for Phase 0, ADR-0020): SQLite reuses a
        deleted max rowid for the next insert, so deleting the row a
        cursor points at could let one new row slip past a resume. Closing
        that needs a monotonic-sequence migration (DBA/archcom decision),
        tracked with the ADR-0020 rollout notes.
        """
        conn = self._get_conn()
        q = "SELECT rowid AS _mesh_rowid, * FROM memories WHERE 1=1"
        params: list[Any] = []
        if projects:
            placeholders = ", ".join("?" for _ in projects)
            q += f" AND project IN ({placeholders})"
            params.extend(projects)
        if tags:
            for tag in tags:
                q += " AND EXISTS (SELECT 1 FROM json_each(tags) WHERE json_each.value = ?)"
                params.append(tag)
        if since:
            q += " AND created_at >= ?"
            params.append(since)
        if after_rowid > 0:
            q += " AND rowid > ?"
            params.append(after_rowid)
        q += " ORDER BY rowid ASC LIMIT ?"
        params.append(limit)
        pairs: list[tuple[Memory, int]] = []
        for row in conn.execute(q, params).fetchall():
            pairs.append((self._row_to_memory(row), int(row["_mesh_rowid"])))
        return pairs

    def list_recent_for_agent(
        self,
        agent: str,
        *,
        project: str | None = None,
        limit: int = 20,
    ) -> list[Memory]:
        """M3 — most recent memories for a specific agent (+ optional project)."""
        conn = self._get_conn()
        q = "SELECT * FROM memories WHERE agent=?"
        params: list[Any] = [agent]
        if project:
            q += " AND project=?"
            params.append(project)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [self._row_to_memory(r) for r in conn.execute(q, params).fetchall()]

    def list_by_cluster(self, cluster_id: str) -> list[Memory]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM memories WHERE cluster_id=? ORDER BY created_at ASC",
            (cluster_id,),
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    # ── Export / import query (M17) ───────────────────────────────────────

    def list_for_export(
        self,
        *,
        project: str | None = None,
        agent: str | None = None,
        status: MemoryStatus | None = None,
        tags: list[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[Memory]:
        """Return memories matching export filters, ordered oldest-first.

        ``since`` / ``until`` apply to both ``created_at`` and ``updated_at``
        (a memory is included if either timestamp falls within the window).
        When ``since`` is set, this implements incremental export: only
        memories created or updated after the boundary are returned.
        """
        conn = self._get_conn()
        q = "SELECT * FROM memories WHERE 1=1"
        params: list[Any] = []
        if project:
            q += " AND project=?"
            params.append(project)
        if agent:
            q += " AND agent=?"
            params.append(agent)
        if status:
            q += " AND status=?"
            params.append(status.value)
        if tags:
            for tag in tags:
                q += " AND EXISTS (SELECT 1 FROM json_each(tags) WHERE json_each.value = ?)"
                params.append(tag)
        if since:
            q += " AND (created_at >= ? OR updated_at >= ?)"
            params.extend([since.isoformat(), since.isoformat()])
        if until:
            q += " AND (created_at <= ? OR updated_at <= ?)"
            params.extend([until.isoformat(), until.isoformat()])
        q += " ORDER BY created_at ASC"
        if limit is not None:
            q += " LIMIT ?"
            params.append(limit)
        return [self._row_to_memory(r) for r in conn.execute(q, params).fetchall()]

    def wipe_all(self) -> int:
        """Delete every memory row (and FTS shadow rows via triggers).

        Used by ``mnemos import --mode restore``. Returns the number of
        deleted memory rows. Schema, indexes, projects, traces, and DLQ
        are preserved — only the ``memories`` table is cleared.
        """
        conn = self._get_conn()
        cur = conn.execute("DELETE FROM memories")
        conn.commit()
        self._invalidate_caches()
        return cur.rowcount

    def wipe_projects(self) -> int:
        """Delete every project row. Used by restore mode before re-import."""
        conn = self._get_conn()
        cur = conn.execute("DELETE FROM projects")
        conn.commit()
        self._invalidate_caches()
        return cur.rowcount

    # ── FTS search ────────────────────────────────────────────────────────

    def fts_search(
        self,
        query: str,
        limit: int = 20,
        *,
        project: str | None = None,
        agent: str | None = None,
        status: MemoryStatus | None = None,
    ) -> list[tuple[Memory, float]]:
        """FTS5 full-text search with optional project/agent/status filters.

        M15.2 hardening (search v2, issue #313): the user-supplied `query`
        is sanitised by `fts_query_v2` — every token is stripped of FTS5
        query-syntax chars and emitted as a QUOTED PREFIX term (`"tok"*`),
        so no un-escaped user text reaches MATCH (no NEAR, no column
        filters, no operator injection). Multi-token queries join with
        AND; when the AND query yields zero rows the call retries ONCE
        with the same prefix terms joined by OR (bm25 ranks the wider
        recall set) and logs the fallback. The optional filter columns
        are bound parameters, never interpolated. The SQL body is built
        by string-concatenating static fragments + `?` placeholders, so
        the resulting statement contains no user-controlled identifiers
        (B608-safe).
        """
        conn = self._get_conn()
        where_parts: list[str] = ["memories_fts MATCH ?"]
        if project:
            where_parts.append("m.project = ?")
        if agent:
            where_parts.append("m.agent = ?")
        if status:
            where_parts.append("m.status = ?")
        where_clause = " AND ".join(where_parts)
        # B608: where_clause is composed of static fragments + `?` placeholders.
        # No user input is interpolated. rank column is from FTS5 itself.
        sql = (
            "SELECT m.*, f.rank "
            "FROM memories_fts f "
            "JOIN memories m ON m.id = f.id "
            "WHERE " + where_clause + " "  # nosec B608
            "ORDER BY f.rank "
            "LIMIT ?"
        )

        def _run(match_expr: str) -> list[Any]:
            params: list[Any] = [match_expr]
            if project:
                params.append(project)
            if agent:
                params.append(agent)
            if status:
                params.append(status.value)
            params.append(limit)
            try:
                return conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError as exc:
                if "missing row" in str(exc) or "content table" in str(exc):
                    logger.warning("FTS5 index corrupted, auto-rebuilding: %s", exc)
                    self.rebuild_fts_index()
                    return conn.execute(sql, params).fetchall()
                raise

        # Search v2 (issue #313): AND over per-token prefix terms first; on
        # zero rows retry ONCE with the OR join of the same terms (bm25
        # still ranks the wider recall set). Zero after both -> the
        # caller's remaining legs decide (vector leg / project
        # soft-fallback stay available). Single-token queries never
        # OR-retry: OR degenerates to the same single-term query.
        terms = fts_query_terms(query)
        rows = _run(fts_query_v2(query))
        if not rows and len(terms) > 1:
            logger.info(
                "fts_search: AND query matched 0 rows, retrying with OR join (%d terms)",
                len(terms),
            )
            rows = _run(fts_join_or(terms))
        results: list[tuple[Memory, float]] = []
        for row in rows:
            memory = self._row_to_memory(row)
            score = 1.0 / (1.0 + abs(float(row["rank"])))
            results.append((memory, score))
        return results

    @staticmethod
    def _build_fts_query(user_query: str) -> str:
        """Convert user input into a safe FTS5 MATCH expression.

        Search v2 (issue #313): thin compatibility wrapper over
        :func:`fts_query_v2` (per-token quoted PREFIX terms joined by
        AND). Kept as the single historical chokepoint so the M15.2
        security tests keep a stable symbol to pin; the strategy notes
        below describe the M15.2 baseline the wrapper superseded.

        M15.2 baseline (superseded semantics): strip the FTS5
        query-syntax special chars ``* " ' ( ) :`` and wrap the WHOLE
        input in one double-quoted phrase. That disabled operator
        injection (kept in v2: every term is individually quoted) but
        made multi-token queries adjacency phrases and killed prefix /
        morphology matching — the two defects search v2 fixes (issue
        #313: multi-token AND and inflected-form recall).
        """
        return fts_query_v2(user_query)

    # ── Aggregates ────────────────────────────────────────────────────────

    def get_all_tags(self) -> dict[str, int]:
        hit, val = self._cache.get("tags", 60)
        if hit:
            return cast(dict[str, int], val)
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT j.value AS tag, COUNT(*) AS cnt "
            "FROM memories, json_each(memories.tags) AS j "
            "GROUP BY j.value ORDER BY cnt DESC"
        ).fetchall()
        # `r` is sqlite3.Row — index access yields `Any`. The schema
        # guarantees the tag column is text and the count is int, so the
        # explicit str/int casts make the declared dict[str, int] return
        # type hold under mypy --strict. We then `cast` the comprehension
        # so the function's return type is also explicit.
        result: dict[str, int] = {str(r[0]): int(r[1]) for r in rows}
        self._cache.set("tags", result)
        return result

    def count(self) -> int:
        conn = self._get_conn()
        r = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        return int(r[0]) if r else 0

    def count_by_status(self) -> dict[str, int]:
        hit, val = self._cache.get("stats", 60)
        if hit:
            return cast(dict[str, int], val)
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT COALESCE(status,'raw') AS s, COUNT(*) AS c FROM memories GROUP BY s"
        ).fetchall()
        result: dict[str, int] = {str(r[0]): int(r[1]) for r in rows}
        self._cache.set("stats", result)
        return result

    def get_project_memory_counts(self) -> dict[str, int]:
        hit, val = self._cache.get("projects_counts", 60)
        if hit:
            return cast(dict[str, int], val)
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT project, COUNT(*) AS cnt FROM memories WHERE project != '' GROUP BY project"
        ).fetchall()
        result: dict[str, int] = {str(r[0]): int(r[1]) for r in rows}
        self._cache.set("projects_counts", result)
        return result

    def count_by_agent(self) -> dict[str, int]:
        """Count memories grouped by agent (non-empty only)."""
        hit, val = self._cache.get("agents_counts", 60)
        if hit:
            return cast(dict[str, int], val)
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT agent, COUNT(*) AS cnt FROM memories WHERE agent != '' GROUP BY agent"
        ).fetchall()
        result: dict[str, int] = {str(r[0]): int(r[1]) for r in rows}
        self._cache.set("agents_counts", result)
        return result

    def count_by_type(self) -> dict[str, int]:
        """Count memories grouped by memory_type."""
        hit, val = self._cache.get("types_counts", 60)
        if hit:
            return cast(dict[str, int], val)
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT COALESCE(memory_type,'note') AS t, COUNT(*) AS c FROM memories GROUP BY t"
        ).fetchall()
        result: dict[str, int] = {str(r[0]): int(r[1]) for r in rows}
        self._cache.set("types_counts", result)
        return result

    def count_by_date(
        self,
        *,
        days: int = 30,
        granularity: str = "day",
    ) -> list[dict[str, Any]]:
        """Return daily memory counts for the last ``days`` days.

        granularity is accepted for forward-compat (only "day" supported now).
        Returns a list of ``{"timestamp": "YYYY-MM-DD", "value": N}`` dicts
        ordered by timestamp ascending.
        """
        _ = granularity  # only "day" supported; accepted for API symmetry
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT DATE(created_at) AS d, COUNT(*) AS c "
            "FROM memories "
            "WHERE created_at > datetime('now', ?) "
            "GROUP BY d ORDER BY d ASC",
            (f"-{int(days)} days",),
        ).fetchall()
        return [{"timestamp": str(r["d"]), "value": int(r["c"])} for r in rows]

    def count_sessions(self) -> dict[str, int]:
        """Return total and active session counts.

        "active" = sessions updated within the last 24h (heuristic).
        """
        conn = self._get_conn()
        total_row = conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()
        total = int(total_row["c"]) if total_row else 0
        active_row = conn.execute(
            "SELECT COUNT(*) AS c FROM sessions WHERE updated_at > datetime('now', '-1 day')"
        ).fetchone()
        active = int(active_row["c"]) if active_row else 0
        return {"total": total, "active": active}

    def get_filter_stats(self) -> dict[str, Any]:
        """M10 — aggregate Context Filter coverage statistics.

        Returns:
            filtered: count of memories with clean_content populated
            unfiltered: count of memories without clean_content
            avg_reduction_pct: mean char reduction across filtered memories
            by_profile: {profile: count} for filtered memories
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT "
            "COUNT(*) FILTER (WHERE clean_content IS NOT NULL) AS filtered, "
            "COUNT(*) FILTER (WHERE clean_content IS NULL) AS unfiltered "
            "FROM memories"
        ).fetchone()
        filtered = int(row["filtered"]) if row else 0
        unfiltered = int(row["unfiltered"]) if row else 0

        # Per-profile counts (filtered memories only)
        profile_rows = conn.execute(
            "SELECT COALESCE(filter_profile,'default') AS p, COUNT(*) AS c "
            "FROM memories WHERE clean_content IS NOT NULL GROUP BY p"
        ).fetchall()
        by_profile: dict[str, int] = {str(r["p"]): int(r["c"]) for r in profile_rows}

        # Average char reduction: parse filter_stats JSON for each filtered
        # memory and compute (1 - final_chars / original_chars) * 100.
        avg_reduction_pct = 0.0
        if filtered > 0:
            stat_rows = conn.execute(
                "SELECT filter_stats FROM memories "
                "WHERE clean_content IS NOT NULL AND filter_stats IS NOT NULL"
            ).fetchall()
            reductions: list[float] = []
            for sr in stat_rows:
                raw_stats = sr["filter_stats"]
                if not raw_stats:
                    continue
                try:
                    parsed = json.loads(raw_stats)
                except (json.JSONDecodeError, TypeError):
                    continue
                reduction = parsed.get("reduction")
                if not isinstance(reduction, dict):
                    continue
                orig = reduction.get("original_chars")
                final = reduction.get("final_chars")
                if isinstance(orig, (int, float)) and isinstance(final, (int, float)) and orig > 0:
                    reductions.append((1.0 - final / orig) * 100.0)
            if reductions:
                avg_reduction_pct = sum(reductions) / len(reductions)

        return {
            "filtered": filtered,
            "unfiltered": unfiltered,
            "avg_reduction_pct": round(avg_reduction_pct, 2),
            "by_profile": by_profile,
        }

    def get_by_file_path(self, file_path: str) -> Memory | None:
        conn = self._get_conn()
        r = conn.execute("SELECT * FROM memories WHERE file_path=?", (file_path,)).fetchone()
        return self._row_to_memory(r) if r else None

    def get_by_source_url(self, source_url: str) -> Memory | None:
        conn = self._get_conn()
        r = conn.execute("SELECT * FROM memories WHERE source_url=?", (source_url,)).fetchone()
        return self._row_to_memory(r) if r else None

    # ── Traces (M6) ───────────────────────────────────────────────────────

    def save_trace(self, trace: Trace) -> None:
        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO traces
               (id, task_label, project, step, item_id, llm_called, llm_done,
                cache_hit, fallback_used, latency_ms, tokens_in, tokens_out,
                tokens_per_sec, rationale_summary, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trace.id,
                trace.task_label,
                trace.project,
                trace.step,
                trace.item_id,
                int(trace.llm_called),
                int(trace.llm_done),
                int(trace.cache_hit),
                int(trace.fallback_used),
                trace.latency_ms,
                trace.tokens_in,
                trace.tokens_out,
                trace.tokens_per_sec,
                trace.rationale_summary,
                trace.created_at.isoformat(),
            ),
        )
        conn.commit()

    def list_traces(
        self,
        project: str | None = None,
        task_label: str | None = None,
        limit: int = 100,
    ) -> list[Trace]:
        conn = self._get_conn()
        q = "SELECT * FROM traces WHERE 1=1"
        params: list[Any] = []
        if project:
            q += " AND project=?"
            params.append(project)
        if task_label:
            q += " AND task_label=?"
            params.append(task_label)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(q, params).fetchall()
        return [Trace.model_validate(dict(r)) for r in rows]

    # ── DLQ (M5) ──────────────────────────────────────────────────────────

    def dlq_add(
        self,
        memory_id: str,
        *,
        cluster_id: str | None = None,
        task_label: str = "synthesize",
        error_message: str = "",
        max_attempts: int = 3,
    ) -> None:
        """Add a failed item to the Dead-Letter Queue."""
        conn = self._get_conn()
        now = datetime.now(UTC).isoformat()
        conn.execute(
            """INSERT OR REPLACE INTO dlq
               (id, memory_id, cluster_id, task_label, error_message,
                attempt_count, max_attempts, next_retry_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                str(uuid.uuid4()),
                memory_id,
                cluster_id,
                task_label,
                error_message,
                1,
                max_attempts,
                now,
                now,
                now,
            ),
        )
        conn.commit()

    def dlq_list(
        self,
        *,
        task_label: str | None = None,
        ready_only: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List DLQ entries, optionally filtering to retry-ready items."""
        conn = self._get_conn()
        q = "SELECT * FROM dlq WHERE 1=1"
        params: list[Any] = []
        if task_label:
            q += " AND task_label=?"
            params.append(task_label)
        if ready_only:
            q += " AND (next_retry_at IS NULL OR next_retry_at <= ?)"
            params.append(datetime.now(UTC).isoformat())
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(q, params).fetchall()]

    def dlq_increment_attempt(self, dlq_id: str, *, backoff_sec: int = 60) -> None:
        """Bump attempt_count and set next_retry_at with exponential backoff."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT attempt_count, max_attempts FROM dlq WHERE id=?", (dlq_id,)
        ).fetchone()
        if not row:
            return
        attempt = row["attempt_count"] + 1
        next_retry = datetime.now(UTC).isoformat()
        if attempt <= row["max_attempts"]:
            # Exponential backoff with jitter cap
            delay = min(backoff_sec * (2 ** (attempt - 1)), 86400)
            next_retry = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
        conn.execute(
            "UPDATE dlq SET attempt_count=?, next_retry_at=?, updated_at=? WHERE id=?",
            (attempt, next_retry, datetime.now(UTC).isoformat(), dlq_id),
        )
        conn.commit()

    def dlq_remove(self, dlq_id: str) -> bool:
        """Remove a DLQ entry (discard or after successful retry)."""
        conn = self._get_conn()
        cur = conn.execute("DELETE FROM dlq WHERE id=?", (dlq_id,))
        conn.commit()
        return cur.rowcount > 0

    def dlq_count(self) -> int:
        conn = self._get_conn()
        row = conn.execute("SELECT COUNT(*) AS c FROM dlq").fetchone()
        return row["c"] if row else 0

    # ── Projects ──────────────────────────────────────────────────────────

    def save_project(self, project: Project) -> None:
        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO projects
               (id, name, description, paths, created_at, updated_at)
               VALUES (?,?,?,?,?,?)""",
            (
                project.id,
                project.name,
                project.description,
                json.dumps(project.paths, ensure_ascii=False),
                project.created_at.isoformat(),
                project.updated_at.isoformat(),
            ),
        )
        conn.commit()
        self._invalidate_caches()

    def get_project(self, project_id: str) -> Project | None:
        conn = self._get_conn()
        r = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return self._row_to_project(r) if r else None

    def get_project_by_name(self, name: str) -> Project | None:
        conn = self._get_conn()
        r = conn.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
        return self._row_to_project(r) if r else None

    def list_projects(self) -> list[Project]:
        conn = self._get_conn()
        return [
            self._row_to_project(r)
            for r in conn.execute("SELECT * FROM projects ORDER BY name").fetchall()
        ]

    def _row_to_project(self, row: sqlite3.Row) -> Project:
        return Project(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            paths=json.loads(row["paths"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    # ── Generic key-value metadata ────────────────────────────────────────

    def set_meta(self, key: str, value: str) -> None:
        """Upsert a metadata row (e.g. pipeline last-run timestamp)."""
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO meta (key, value, updated_at) VALUES (?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                   value=excluded.value,
                   updated_at=excluded.updated_at""",
            (key, value, datetime.now(UTC).isoformat()),
        )
        conn.commit()

    def get_meta(self, key: str) -> str | None:
        """Read a metadata row. Returns None if the key does not exist."""
        conn = self._get_conn()
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    # ── Checkpoint channel identity (mnemos #251 D0) ─────────────────────

    def bind_session_agent(self, session_id: str, agent: str) -> str:
        """Atomically bind ``session_id`` to ``agent``; first writer wins.

        mnemos #251 D0 — the session→agent binding lives in the existing
        ``meta`` key-value table (additive, migration-free surface; no
        destructive schema change). INSERT OR IGNORE + SELECT inside one
        transaction close the TOCTOU window: two racing first calls with
        different agents cannot both establish a binding. Returns the
        CANONICAL agent — the value passed in when this call created the
        binding, or the pre-existing binding otherwise. Spoofing another
        agent from that point on requires this server-recorded session id
        to keep validating.
        """
        key = _SESSION_AGENT_META_PREFIX + session_id
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value, updated_at) VALUES (?,?,?)",
                (key, agent, datetime.now(UTC).isoformat()),
            )
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise
        return str(row["value"]) if row is not None else agent

    def find_checkpoint_by_dedup_key(
        self, *, project: str, agent: str, dedup_key: str
    ) -> Memory | None:
        """Issuer-keyed checkpoint dedup lookup (mnemos #251 D0).

        Finds the newest ``mnemos:checkpoint`` memory of the exact
        ``(project, agent)`` issuer whose server-written metadata carries
        ``checkpoint_dedup_key == dedup_key``. The dedup key is computed
        over the canonical field payload INCLUDING the issuer (CWE-294
        replay control: a copy of a victim's checkpoint re-issued by a
        different agent must NOT collide).

        mnemos #251 security review (P1, defense in depth): the lookup
        also requires the row's ``checkpoint_agent`` metadata stamp to
        equal the claimed agent — only ``save_checkpoint`` mints that
        stamp, so a row whose dedup key slipped in through any other
        path (legacy/partial data) can never satisfy a genuine dedup.
        """
        conn = self._get_conn()
        row = conn.execute(
            """
            SELECT * FROM memories
            WHERE project = ? AND agent = ?
              AND json_extract(metadata, '$.checkpoint_dedup_key') = ?
              AND json_extract(metadata, '$.checkpoint_agent') = ?
              AND EXISTS (
                  SELECT 1 FROM json_each(memories.tags)
                  WHERE json_each.value = 'mnemos:checkpoint'
              )
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (project, agent, dedup_key, agent),
        ).fetchone()
        return self._row_to_memory(row) if row is not None else None

    def find_federated_duplicate(
        self, *, fed_id: str = "", title: str = "", source_agent: str = ""
    ) -> Memory | None:
        """Idempotent mesh-import duplicate lookup (vesmaro #359).

        Called by :rpc:`WriteMemory` BEFORE any create so a replayed
        one-shot pull (mnemos-mesh #34) performs no duplicate writes.
        Match keys, priority order:

        1. ``fed_id`` — the incoming ``CompactRecord.id`` vs the stored
           ``metadata.fed_id`` minted by the mesh import path. The
           ``fed:<source_agent>:<local_uuid>`` scheme makes this globally
           unique (contract §2).
        2. ``title`` + ``source_agent`` — fallback for records arriving
           without a fed id: incoming title/source_agent vs stored
           ``title`` + ``metadata.fed_source_agent``. Only rows imported
           through the mesh carry ``fed_source_agent``, so plain API
           records never satisfy the fallback (the #359 "не трогаем
           обычные API-записи" invariant on the stored side).

        Records without provenance (empty ``fed_id`` AND empty
        ``source_agent``) return ``None`` without querying — no match
        attempt at all. v1 (#359): linear ``json_extract`` scan, no
        denormalised column or index — same trade-off as
        :meth:`find_checkpoint_by_dedup_key`; revisit if mesh import
        volume makes the scan hot. Earliest match wins, so the storage
        id returned across replays is stable.
        """
        conn = self._get_conn()
        row: sqlite3.Row | None
        if fed_id:
            row = conn.execute(
                """
                SELECT * FROM memories
                WHERE json_extract(metadata, '$.fed_id') = ?
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """,
                (fed_id,),
            ).fetchone()
        elif title and source_agent:
            row = conn.execute(
                """
                SELECT * FROM memories
                WHERE title = ?
                  AND json_extract(metadata, '$.fed_source_agent') = ?
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """,
                (title, source_agent),
            ).fetchone()
        else:
            return None
        return self._row_to_memory(row) if row is not None else None

    def touch_last_fed_at(self, memory_id: str) -> bool:
        """Refresh ``metadata.last_fed_at`` on a federated record (#359).

        Fires ONLY when the stored metadata already carries the key —
        the mesh import path seeds it at first create, so the guard
        keeps foreign rows untouched ("при наличии", no key is ever
        invented here). A single guarded UPDATE via ``json_set``: no
        read-modify-write race, no other metadata key is touched, and
        content/title stay as stored (v1 decision: a duplicate is
        reported, never re-written).
        """
        now = datetime.now(UTC).isoformat()
        conn = self._get_conn()
        cur = conn.execute(
            """
            UPDATE memories
            SET metadata = json_set(metadata, '$.last_fed_at', ?),
                updated_at = ?
            WHERE id = ?
              AND json_extract(metadata, '$.last_fed_at') IS NOT NULL
            """,
            (now, now, memory_id),
        )
        conn.commit()
        self._invalidate_caches()
        return cur.rowcount > 0

    # ── S2 federation index (ADR-0021 Q10.3, archcom 2026-09-20) ──────────

    def upsert_index_entries(
        self,
        entries: Sequence[FederationIndexEntry],
        *,
        sender_peer_id: str | None = None,
        title_blocklist: Sequence[str] = (),
    ) -> IndexUpsertStats:
        """Import gate for the federation metadata index (S2 substrate).

        Upserts entries into ``federation_index`` in one transaction,
        keyed on ``id`` (the cross-peer dedup key). Conflict resolution
        is LWW-by-``timestamp`` (ADR-0021 ruling Q10.6 for v1): a row
        with an OLDER timestamp than the stored one is silently dropped
        (its data is superseded); equal-or-newer timestamps replace the
        stored row (equal = replayed same record, last write wins —
        the revision-field gap that would disambiguate this is recorded
        in the proto as ``CompactRecord.revision`` and reserved for
        ``MetadataRecord`` as an archcom-enumerated additive move).

        Origin-mutation guard (review blocker 1, CWE-284 — archcom
        ruling «index mutations come from the origin only; available
        transit is allowed»):

        * INSERT path: transit stays allowed — a NEW id with an
          explicit foreign ``origin_peer`` is exactly how a peer
          re-advertises another origin's row.
        * CONFLICT path: the stored row may only be mutated by its
          ORIGIN. On the import leg (``sender_peer_id`` set) that means
          the authenticated sender MUST equal the stored
          ``origin_peer`` — a transit re-send or a foreign ``self``
          claim from a non-origin sender is refused into
          ``rejected_by_gate`` (NO exception, not even available
          transit: a sender that is not the origin must never be able
          to tombstone, retitle or otherwise censor another origin's
          row). On the trusted local leg (``sender_peer_id=None``) the
          entry's ``origin_peer`` must simply match the stored one.
        * The stored ``origin_peer`` is NEVER rewritten: the ON
          CONFLICT UPDATE clause does not touch it (attribution
          capture is structurally impossible, not just gate-checked).

        Import-side gates applied HERE (Q10.9 — title-regex runs at
        export AND import; no-federate is belt-and-braces: the ORIGIN
        already filters it, a peer that sends it anyway is misbehaving):

        * ``mnemos:no-federate`` in ``tags`` → entry refused (not
          stored) — a no-federate index row would re-advertise on serve
          exactly what the tag forbids;
        * ``title`` matching any ``title_blocklist`` pattern → entry
          refused.

        Args:
            entries: Parsed, schema-validated entries
                (:class:`vesmaro.compact.FederationIndexEntry`). Entries
                that fail a gate are skipped — one bad entry never
                aborts the batch (a peer's index page is not atomic).
            sender_peer_id: The AUTHENTICATED sender id on the RPC
                import leg (re-stamps ``""``/``"self"`` origins to the
                sender as defence-in-depth — the RPC layer already did
                — and enables the origin-mutation guard). ``None`` =
                trusted local-materialisation leg (local rows are
                minted with ``origin_peer='self'``).
            title_blocklist: Q10.9 regex patterns (from
                :attr:`vesmaro.config.FederationConfig.
                index_title_blocklist`); empty = no title gate.

        Returns:
            :class:`IndexUpsertStats` — rows written (inserted or
            replaced), refused by a gate, and stale (silently
            superseded by LWW).
        """
        now = datetime.now(UTC).isoformat()
        written = 0
        refused = 0
        conn = self._get_conn()
        for entry in entries:
            if NO_FEDERATE_TAG in entry.tags:
                refused += 1
                logger.info(
                    "federation_index: refused entry id=%s — no-federate tag at import",
                    entry.id,
                )
                continue
            if title_blocklist and title_matches_blocklist(entry.title, title_blocklist):
                refused += 1
                logger.info(
                    "federation_index: refused entry id=%s — title blocklist at import",
                    entry.id,
                )
                continue
            incoming_origin = entry.origin_peer
            if sender_peer_id is not None and incoming_origin in ("", "self"):
                # Defence-in-depth re-stamp: the RPC layer already
                # re-stamped; a foreign ""/"self" claim must never enter
                # the local origin namespace.
                incoming_origin = sender_peer_id
            stored = conn.execute(
                "SELECT origin_peer FROM federation_index WHERE id = ?",
                (entry.id,),
            ).fetchone()
            if stored is not None:
                if sender_peer_id is not None:
                    # Import leg, conflict path: mutations only from the
                    # origin. The origin's own updates arrive as
                    # ""/"self" → re-stamped to the sender, so sender ==
                    # stored origin is the only legitimate match; an
                    # explicit foreign origin (transit) from any sender
                    # is refused — no exceptions (blocker 1).
                    if stored[0] != sender_peer_id:
                        refused += 1
                        logger.info(
                            "federation_index: refused entry id=%s — cross-origin mutation "
                            "(sender=%s stored_origin=%s)",
                            entry.id,
                            sender_peer_id,
                            stored[0],
                        )
                        continue
                    incoming_origin = stored[0]
                elif stored[0] != incoming_origin:
                    refused += 1
                    logger.info(
                        "federation_index: refused entry id=%s — origin mismatch "
                        "(incoming=%s stored_origin=%s)",
                        entry.id,
                        incoming_origin,
                        stored[0],
                    )
                    continue
            cur = conn.execute(
                """
                INSERT INTO federation_index
                    (id, type, title, tags, project, source_agent, source_peer,
                     origin_peer, content_state, timestamp, schema_version, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    type = excluded.type,
                    title = excluded.title,
                    tags = excluded.tags,
                    project = excluded.project,
                    source_agent = excluded.source_agent,
                    source_peer = excluded.source_peer,
                    content_state = excluded.content_state,
                    timestamp = excluded.timestamp,
                    schema_version = excluded.schema_version,
                    received_at = excluded.received_at
                WHERE excluded.timestamp >= federation_index.timestamp
                """,
                (
                    entry.id,
                    entry.type,
                    entry.title,
                    json.dumps(entry.tags, ensure_ascii=False),
                    entry.project,
                    entry.source_agent,
                    entry.source_peer,
                    incoming_origin,
                    entry.content_state,
                    entry.timestamp,
                    entry.schema_version,
                    entry.received_at or now,
                ),
            )
            written += cur.rowcount
        conn.commit()
        stale = len(entries) - written - refused
        logger.info(
            "federation_index: upsert batch total=%d written=%d refused=%d stale=%d",
            len(entries),
            written,
            refused,
            stale,
        )
        return IndexUpsertStats(written=written, refused=refused, stale=stale)

    def list_index(
        self,
        limit: int = 50,
        *,
        origin_peers: Sequence[str] | None = None,
        projects: Sequence[str] | None = None,
        since: str | None = None,
        after_rowid: int = 0,
        exclude_no_federate: bool = True,
    ) -> list[tuple[FederationIndexEntry, int]]:
        """Export-side listing of ``federation_index`` with resume rowids.

        The index twin of :meth:`list_all_for_mesh` (ADR-0020 cursor
        mechanic): a ``rowid ASC`` forward walk over the index's own
        rowid space, so every page boundary is a valid watermark —
        resuming with ``rowid > checkpoint`` yields exactly the
        undelivered remainder (no dupes, no gaps; the rowid space is
        global to the table). The S2 wire contract maps its int64
        watermark (``MetadataSyncRequest.since_rev`` /
        ``MetadataSyncResponse.latest_rev``) onto this rowid space.

        Args:
            limit: Maximum rows to fetch (caller passes page_limit + 1
                to detect ``has_more``).
            origin_peers: Restrict to these ``origin_peer`` values (SQL
                ``IN``). ``None`` = no filter (all origins).
            projects: Restrict to these ``project`` values (the caller
                has already applied the peer ACL intersection).
            since: Legacy ISO lower bound on ``timestamp`` (the record's
                own timestamp, NOT ``received_at``).
            after_rowid: Only rows with ``rowid > after_rowid``
                (watermark resume; ``0`` = full pass from the start).
            exclude_no_federate: Drop rows whose ``tags`` still carry
                ``mnemos:no-federate`` (they should never be in the
                index — the upsert gate refuses them; this is the
                serve-side belt-and-braces, Q10.9).

        Scope-stability caveat: identical to :meth:`list_all_for_mesh`
        — the caller must replay a watermark only against the scope it
        was minted for (widening the project/origin filter after a
        checkpoint silently skips rows the narrower walk never
        delivered).
        """
        conn = self._get_conn()
        q = "SELECT rowid AS _idx_rowid, * FROM federation_index WHERE 1=1"
        params: list[Any] = []
        if origin_peers:
            placeholders = ", ".join("?" for _ in origin_peers)
            q += f" AND origin_peer IN ({placeholders})"
            params.extend(origin_peers)
        if projects:
            placeholders = ", ".join("?" for _ in projects)
            q += f" AND project IN ({placeholders})"
            params.extend(projects)
        if since:
            q += " AND timestamp >= ?"
            params.append(since)
        if after_rowid > 0:
            q += " AND rowid > ?"
            params.append(after_rowid)
        if exclude_no_federate:
            q += (
                " AND NOT EXISTS (SELECT 1 FROM json_each(federation_index.tags) "
                "WHERE json_each.value = ?)"
            )
            params.append(NO_FEDERATE_TAG)
        q += " ORDER BY rowid ASC LIMIT ?"
        params.append(limit)
        pairs: list[tuple[FederationIndexEntry, int]] = []
        for row in conn.execute(q, params).fetchall():
            pairs.append(
                (
                    FederationIndexEntry(
                        id=row["id"],
                        type=row["type"],
                        title=row["title"],
                        tags=json.loads(row["tags"]),
                        project=row["project"],
                        source_agent=row["source_agent"],
                        source_peer=row["source_peer"],
                        origin_peer=row["origin_peer"],
                        content_state=row["content_state"],
                        timestamp=row["timestamp"],
                        schema_version=row["schema_version"],
                        received_at=row["received_at"],
                    ),
                    int(row["_idx_rowid"]),
                )
            )
        return pairs

    def get_index_entries(self, fed_ids: Sequence[str]) -> dict[str, FederationIndexEntry]:
        """Fetch ``federation_index`` rows by federation id (lazy fetch).

        Point lookup for the S2 lazy-fetch resolution step: given the
        ``--id`` list the operator asked for, return the mirrored
        metadata rows keyed by ``id``. Ids with no row are simply
        absent from the result — the CALLER decides whether a miss is
        an error (``mnemos fetch`` treats it as one: the command is
        driven by ids the mirror advertised, so an unknown id means
        the operator and the index disagree).

        Read-only; no ACL (the index is local mirror state — the
        serving-side ACL applies when rows are SERVED, the import-side
        gates when content is fetched).
        """
        if not fed_ids:
            return {}
        placeholders = ", ".join("?" for _ in fed_ids)
        conn = self._get_conn()
        rows = conn.execute(
            f"SELECT * FROM federation_index WHERE id IN ({placeholders})",
            list(fed_ids),
        ).fetchall()
        return {
            row["id"]: FederationIndexEntry(
                id=row["id"],
                type=row["type"],
                title=row["title"],
                tags=json.loads(row["tags"]),
                project=row["project"],
                source_agent=row["source_agent"],
                source_peer=row["source_peer"],
                origin_peer=row["origin_peer"],
                content_state=row["content_state"],
                timestamp=row["timestamp"],
                schema_version=row["schema_version"],
                received_at=row["received_at"],
            )
            for row in rows
        }

    def index_head(self, projects: Sequence[str] | None = None) -> int:
        """Return the max ``federation_index`` rowid under the projects filter.

        The scope HEAD for the SyncMetadata watermark (mnemos-mesh#46):
        the highest storage position a scope-filtered rowid walk can
        ever have delivered. The SyncMetadata body
        (:meth:`MnemosCoreServicer.build_metadata_sync_response` in
        ``mesh_server``) parks ``latest_rev`` here on an EMPTY page
        instead of echoing ``since_rev`` — a MIN-aggregating poller
        (the mesh CLI folds the per-scope ``latest_rev`` into one
        watermark) otherwise sticks at the old checkpoint and
        re-delivers the data scope every tick.

        Deliberately NOT filtered by ``exclude_no_federate``: the head
        is the insertion high-water mark of the scope, and a
        no-federate row is never served anyway — jumping the watermark
        past such a row is the intended skip (the same semantics a
        blocked row inside a served page already has). An empty scope
        (or empty index) yields ``0``.

        Args:
            projects: Restrict to these ``project`` values (SQL ``IN``,
                the caller's effective ACL-intersected set); ``None`` =
                no filter (the whole index).
        """
        conn = self._get_conn()
        q = "SELECT COALESCE(MAX(rowid), 0) FROM federation_index"
        params: list[Any] = []
        if projects:
            placeholders = ", ".join("?" for _ in projects)
            q += f" WHERE project IN ({placeholders})"
            params.extend(projects)
        return int(conn.execute(q, params).fetchone()[0])

    def purge_origin(self, origin_peer: str) -> int:
        """Delete every index row sourced from ``origin_peer``.

        The mirror-aging primitive (paper §4): when an operator
        decommissions a peer (or an origin store leaves the mesh), its
        mirrored pointers age out wholesale — no deletion reason crosses
        servers, the rows simply stop being advertised by THIS store.
        Local rows (``origin_peer='self'``) are deletable the same way
        (operator-scope decision, e.g. rebuilding the local index
        materialisation); nothing here touches the ``memories`` table.

        Returns:
            The number of rows deleted.
        """
        conn = self._get_conn()
        cur = conn.execute(
            "DELETE FROM federation_index WHERE origin_peer = ?",
            (origin_peer,),
        )
        conn.commit()
        logger.info(
            "federation_index: purge_origin peer=%s deleted=%d",
            origin_peer,
            cur.rowcount,
        )
        return cur.rowcount

    # ── S2 phase 2 poller watermark state ─────────────────────────────────

    #: Cap on the stored ``last_error`` text — subprocess stderr tails can
    #: be arbitrarily long; the row is telemetry, not a log sink.
    POLL_STATE_ERROR_MAX_LEN: Final[int] = 512

    def get_poll_state(self, peer_id: str) -> PollStateRow | None:
        """Read the persisted poll watermark for ``peer_id``.

        Returns ``None`` when the peer has never been polled (the
        caller starts from rev 0). Read-only; no caches involved.
        """
        row = (
            self._get_conn()
            .execute(
                "SELECT peer_id, since_rev, last_ok_at, last_error "
                "FROM federation_poll_state WHERE peer_id = ?",
                (peer_id,),
            )
            .fetchone()
        )
        if row is None:
            return None
        return PollStateRow(
            peer_id=row["peer_id"],
            since_rev=int(row["since_rev"]),
            last_ok_at=row["last_ok_at"],
            last_error=row["last_error"],
        )

    def mark_poll_ok(self, peer_id: str, since_rev: int) -> None:
        """Persist a successful poll checkpoint for ``peer_id``.

        Sets the watermark, stamps ``last_ok_at`` (UTC ISO 8601, the
        repo canon) and CLEARS ``last_error`` — a single success after
        failures must not leave a stale error on the health surface.
        Called after every successfully imported page, so a crash
        mid-peer resumes from the last good page (at-least-once; the
        idempotent LWW upsert makes replay harmless).
        """
        conn = self._get_conn()
        conn.execute(
            """
            INSERT INTO federation_poll_state (peer_id, since_rev, last_ok_at, last_error)
            VALUES (?, ?, ?, '')
            ON CONFLICT(peer_id) DO UPDATE SET
                since_rev = excluded.since_rev,
                last_ok_at = excluded.last_ok_at,
                last_error = ''
            """,
            (peer_id, int(since_rev), datetime.now(UTC).isoformat()),
        )
        conn.commit()

    def mark_poll_error(self, peer_id: str, error: str) -> None:
        """Record a failed poll for ``peer_id`` WITHOUT touching the watermark.

        The watermark only ever advances on success (mark_poll_ok) — a
        failed page is re-fetched next tick. ``last_ok_at`` is kept as
        the last known-good timestamp; ``last_error`` is truncated to
        :data:`POLL_STATE_ERROR_MAX_LEN` so a chatty subprocess cannot
        grow the row unboundedly.
        """
        conn = self._get_conn()
        conn.execute(
            """
            INSERT INTO federation_poll_state (peer_id, since_rev, last_ok_at, last_error)
            VALUES (?, 0, '', ?)
            ON CONFLICT(peer_id) DO UPDATE SET
                last_error = excluded.last_error
            """,
            (peer_id, error[: self.POLL_STATE_ERROR_MAX_LEN]),
        )
        conn.commit()

    # ── CCR cache (P1-4) ──────────────────────────────────────────────────

    def ccr_store(
        self,
        *,
        hash: str,
        original: str,
        project: str = "",
        issuer_agent: str | None = None,
        issuer_session: str | None = None,
    ) -> int:
        """Insert a CCR cache entry (idempotent on ``(project, hash)``).

        Uses an UPSERT so re-compressing the same content within the SAME
        project is a no-op for the stored row (the original is already
        cached) while still refreshing the scan verdict. A1 (ArchCom
        2026-08-27): the conflict target is the composite PK — the same
        hash stored by a DIFFERENT project inserts its own row (the
        first-writer-squatting cross-project DoS edge of the hash-only
        PK is dissolved). Returns the rowid of the stored (or
        pre-existing) entry.

        ADR-0018 P1-a — scan-at-store verdict: ``detect_secrets`` runs on
        the ORIGINAL at store time and the verdict ('clean' | 'hit' |
        'unknown') is persisted in ``secret_scan_verdict``. The stored
        original itself remains verbatim (zero-loss, committee decision);
        the flag is observability only — issuance (``retrieve_content``)
        keeps scanning unconditionally because patterns evolve and a
        store-time verdict alone would go stale. On 'hit' a WARNING is
        logged with the hash and log-safe per-pattern counts only; raw
        matched values are never logged (hard rule).

        A2 (ArchCom 2026-08-27) — issuer ledger: ``issuer_agent`` /
        ``issuer_session`` record the caller identity that FIRST stored
        this ``(project, hash)`` row. The UPSERT does NOT rewrite them
        on conflict (first-writer owns, mirroring the A1 PK rule): a
        session re-compressing already-cached identical content receives
        a marker whose row stays bound to the first issuer — strict-mode
        provenance then refuses that redemption, which is fail-closed
        and harmless (the re-compressor already holds the content it
        passed in). ``None``/empty values normalise to NULL: rows stored
        without caller identity are unverifiable and strict marker
        validation refuses them (distinct reason).
        """
        from vesmaro.secrets_detector import detect_secrets, findings_by_pattern

        conn = self._get_conn()
        now = datetime.now(UTC).isoformat()
        size_bytes = len(original.encode("utf-8"))
        issuer_agent_n = issuer_agent.strip() or None if issuer_agent else None
        issuer_session_n = issuer_session.strip() or None if issuer_session else None
        verdict = "unknown"
        try:
            findings = detect_secrets(original)
        except Exception as exc:  # pragma: no cover — defensive, non-fatal
            logger.warning("CCR store scan failed (verdict=unknown): hash=%s error=%s", hash, exc)
        else:
            verdict = "hit" if findings else "clean"
            if findings:
                # Log-safe: hash + pattern counts only, never matched values.
                logger.warning(
                    "CCR store scan hit: hash=%s patterns=%s — raw values not logged",
                    hash,
                    findings_by_pattern(findings),
                )
        conn.execute(
            "INSERT INTO ccr_cache "
            "(hash, original, project, created_at, size_bytes, retrieval_count, "
            " secret_scan_verdict, secret_scan_at, issuer_agent, issuer_session) "
            "VALUES (?,?,?,?,?,0,?,?,?,?) "
            "ON CONFLICT(project, hash) DO UPDATE SET "
            "  secret_scan_verdict=excluded.secret_scan_verdict, "
            "  secret_scan_at=excluded.secret_scan_at",
            (
                hash,
                original,
                project,
                now,
                size_bytes,
                verdict,
                now,
                issuer_agent_n,
                issuer_session_n,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT rowid FROM ccr_cache WHERE hash=? AND project=?", (hash, project)
        ).fetchone()
        return int(row["rowid"]) if row else 0

    def ccr_get(
        self,
        hash: str,
        *,
        project: str | None = None,
        bump: bool = True,
    ) -> dict[str, Any] | None:
        """Fetch a CCR cache entry by hash and bump its retrieval counter.

        ADR-0018 P1-a — project scoping: when ``project`` is given (a
        non-empty slug), the lookup additionally requires the entry to
        belong to that project. A hash stored under a different project
        returns ``None`` (cross-session marker redemption is denied,
        fail-closed) and the retrieval counter is NOT bumped. When
        ``project`` is ``None`` the lookup stays unscoped (legacy
        behavior preserved for callers without project context); under
        the A1 composite PK the same hash may exist in several projects,
        so the unscoped read resolves to the FIRST-STORED copy (lowest
        rowid / earliest created_at — first-writer-wins, the same rule
        the A1 migration used to dedup legacy rows).

        ADR-0018 P1-b review (F4) — ``bump=False`` skips the
        ``retrieval_count`` / ``last_retrieved_at`` UPDATE (and returns
        the CURRENT count): the issuance layer reads the entry unbumped,
        decides, and calls :meth:`ccr_touch` only when content is
        actually issued — a refused/denied issuance must not LRU-pin
        the entry (``ccr_evict_lru`` protects high-count entries) or
        inflate retrieval stats.

        Returns ``{"hash","original","project","created_at","size_bytes",
        "retrieval_count","secret_scan_verdict","secret_scan_at",
        "issuer_agent","issuer_session"}`` or ``None`` if not found /
        project mismatch. The issuer fields are ``None`` for rows stored
        without caller identity (A2 ledger, see :meth:`ccr_store`).
        """
        conn = self._get_conn()
        sql = "SELECT * FROM ccr_cache WHERE hash=?"
        params: list[Any] = [hash]
        if project:
            sql += " AND project=?"
            params.append(project)
        else:
            sql += " ORDER BY created_at ASC, rowid ASC LIMIT 1"
        row = conn.execute(sql, params).fetchone()
        if row is None:
            return None
        if bump:
            now = datetime.now(UTC).isoformat()
            # Bump exactly the row that was read (its own project — the
            # filter param may be None while the row is project-scoped).
            conn.execute(
                "UPDATE ccr_cache SET retrieval_count=retrieval_count+1, "
                "last_retrieved_at=? WHERE hash=? AND project=?",
                (now, hash, row["project"]),
            )
            conn.commit()
        return {
            "hash": row["hash"],
            "original": row["original"],
            "project": row["project"],
            "created_at": row["created_at"],
            "size_bytes": int(row["size_bytes"]),
            "retrieval_count": int(row["retrieval_count"]) + (1 if bump else 0),
            "secret_scan_verdict": row["secret_scan_verdict"],
            "secret_scan_at": row["secret_scan_at"],
            "issuer_agent": row["issuer_agent"],
            "issuer_session": row["issuer_session"],
        }

    def ccr_touch(self, hash: str, *, project: str | None = None) -> None:
        """Bump a CCR entry's retrieval counter (ADR-0018 P1-b review F4).

        Companion to ``ccr_get(bump=False)``: the issuance layer calls
        this only AFTER deciding to issue content, so refused/denied
        issuances leave ``retrieval_count`` / ``last_retrieved_at``
        untouched. Updating a hash that no longer exists (evicted between
        the read and the decision) is a no-op.

        A1: with the composite PK the same hash may exist in several
        projects — pass the ``project`` of the row that was actually
        issued (``MemoryManager.retrieve_content`` passes the entry's
        own project). ``project=None`` is the legacy global form and
        bumps EVERY copy of the hash (they hold identical content; the
        counter is LRU metadata, not a per-project fact).
        """
        conn = self._get_conn()
        sql = (
            "UPDATE ccr_cache SET retrieval_count=retrieval_count+1, "
            "last_retrieved_at=? WHERE hash=?"
        )
        params: list[Any] = [datetime.now(UTC).isoformat(), hash]
        if project is not None:
            sql += " AND project=?"
            params.append(project)
        conn.execute(sql, params)
        conn.commit()

    def ccr_count(self) -> int:
        """Total number of cached CCR entries."""
        conn = self._get_conn()
        return int(conn.execute("SELECT count(*) FROM ccr_cache").fetchone()[0])

    def ccr_cleanup_ttl(self, ttl_days: int) -> int:
        """Delete cache entries older than ``ttl_days``. Returns count deleted."""
        conn = self._get_conn()
        cutoff = (datetime.now(UTC)).isoformat()
        # SQLite date math: subtract ttl_days from now via the modifiers.
        cur = conn.execute(
            "DELETE FROM ccr_cache WHERE date(created_at, '+' || ? || ' days') < date(?)",
            (ttl_days, cutoff),
        )
        conn.commit()
        return cur.rowcount or 0

    def ccr_evict_lru(self, max_entries: int) -> int:
        """Evict least-retrieved entries until count <= max_entries.

        Ties on retrieval_count break by created_at (oldest first).
        Returns count evicted. A1: eviction is per-ROW (rowid-based) —
        under the composite PK the same hash may exist in several
        projects and evicting "the hash" would delete every copy at
        once; exactly ``excess`` rows are removed.
        """
        conn = self._get_conn()
        total = self.ccr_count()
        if total <= max_entries:
            return 0
        excess = total - max_entries
        cur = conn.execute(
            "DELETE FROM ccr_cache WHERE rowid IN ("
            "  SELECT rowid FROM ccr_cache "
            "  ORDER BY retrieval_count ASC, created_at ASC "
            "  LIMIT ?"
            ")",
            (excess,),
        )
        conn.commit()
        return cur.rowcount or 0

    def ccr_search(
        self,
        hash: str,
        query: str,
        limit: int = 5,
        *,
        project: str | None = None,
    ) -> list[dict[str, Any]]:
        """FTS5-ranked snippet search within a single cached original.

        Returns a list of ``{"snippet","rank}`` dicts ordered by relevance.
        Uses the same FTS5 query sanitisation as ``fts_search`` so the
        user-supplied query cannot inject FTS5 operator syntax.

        ADR-0018 P1-a — project scoping (same defect class as
        ``ccr_get``): when ``project`` is given, the entry's project is
        verified BEFORE the FTS query runs and an empty result is
        returned on mismatch. Defence in depth — ``ccr.retrieve`` already
        scopes the ``ccr_get`` lookup, this guard keeps the snippet
        channel from leaking other projects' entries when a caller
        invokes search directly.

        A1 — the FTS leg joins the content table and resolves to ONE
        copy of the hash: the caller's project when scoped, otherwise
        the first-stored copy (all copies of a hash hold identical
        content — content addressing — but WITHOUT this restriction the
        N project copies would each emit the same snippet and flood the
        limit).

        The snippet highlight markers are the module-level
        ``FTS_SNIPPET_*`` constants (single source of truth): the
        issuance-side scanner (ADR-0018 P1-b m2) strips exactly these
        markers before scanning a snippet, because they split
        multi-token secrets (e.g. a JWT whose payload token matched the
        query) so the raw marked snippet text evades ``detect_secrets``.
        """
        conn = self._get_conn()
        if project:
            owner = conn.execute(
                "SELECT 1 FROM ccr_cache WHERE hash=? AND project=?", (hash, project)
            ).fetchone()
            if owner is None:
                return []
        fts_query = self._build_fts_query(query)
        sql = (
            "SELECT snippet(ccr_cache_fts, 1, ?, ?, ?, 32) AS snip, "
            "f.rank AS rank "
            "FROM ccr_cache_fts f JOIN ccr_cache c ON c.rowid = f.rowid "
            "WHERE f.ccr_cache_fts MATCH ? AND f.hash=? "
        )
        params: list[Any] = [
            FTS_SNIPPET_START_MARK,
            FTS_SNIPPET_END_MARK,
            FTS_SNIPPET_ELLIPSIS,
            fts_query,
            hash,
        ]
        if project:
            sql += "AND c.project = ? "
            params.append(project)
        else:
            # Unscoped: first-stored copy only (identical content across
            # copies; one copy keeps snippets unique under the composite PK).
            sql += "AND c.rowid = (SELECT MIN(rowid) FROM ccr_cache WHERE hash=?) "
            params.append(hash)
        sql += "ORDER BY f.rank LIMIT ?"
        params.append(limit)
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            if "missing row" in str(exc) or "content table" in str(exc):
                logger.warning("ccr_cache_fts corrupted, skipping search: %s", exc)
                return []
            raise
        return [{"snippet": str(r["snip"]), "rank": float(r["rank"])} for r in rows]

    def ccr_delete_all(self) -> int:
        """Drop every CCR cache entry. Used by tests and `mnemos ccr purge`."""
        conn = self._get_conn()
        cur = conn.execute("DELETE FROM ccr_cache")
        conn.commit()
        return cur.rowcount or 0

    # ── ADR-0027 Ф3 (epic #308): docs-as-memory — doc-chunk rows ───────────
    #
    # The re-ingest/re-fragmentation surface. A doc_id's chunk rows live
    # in the ordinary ``memories`` table (the Ф0 metadata convention —
    # ``{doc_id, chunk_idx, heading_path}`` in the metadata JSON column,
    # zero migration). What this block adds is the TRANSACTIONAL
    # replacement + cache-version bump the ADR-0027 invariant 4 demands:
    # re-fragmentation (a re-ingest of the same doc_id replacing its
    # chunks) bumps the ccr_cache version key IN THE SAME TRANSACTION as
    # the chunk-row changes. HONEST SCOPE (review round P2-2, the
    # graph_epoch posture): the version key is a consumer-facing
    # invalidation counter — bumped transactionally, readable by any
    # assembly-cache consumer, but no in-repo consumer keys on it yet;
    # the first caching consumer MUST read it.

    def replace_doc_chunks(
        self,
        doc_id: str,
        rows: list[Memory],
        *,
        cache_version_key: str,
    ) -> dict[str, Any]:
        """Replace every chunk row of ``doc_id`` and bump the cache version.

        ADR-0027 invariant 4 (CCR-atomcity, the Ф3 leg): the chunk-row
        replacement (DELETE of the previous doc_id rows + INSERT of the
        new ones) and the ``meta`` upsert of ``cache_version_key`` run
        in ONE explicit ``BEGIN IMMEDIATE`` transaction. A crash between
        the two would otherwise leave the version counter disagreeing
        with the stored corpus — the same transactional-discipline
        class as the A1 edges rebuild (#263 / #193 test pattern).
        Honest scope (review round P2-2, the ``graph_epoch`` posture):
        the counter is a consumer-facing invalidation signal — bumped
        transactionally, exposed via stats; no in-repo consumer keys on
        it yet, and the first caching consumer MUST read it.

        The DELETE keys on the metadata JSON (``json_extract(metadata,
        '$.doc_id') = ?``) — the Ф0 convention IS the doc identity; no
        dedicated column exists (zero migration is a Phase-3 premise).

        FTS5 consistency: the DELETE fires the ``memories_ad`` AFTER
        DELETE trigger and each INSERT fires ``memories_ai`` (same
        trigger contract as the ordinary ``save``), so the external-
        content index stays in sync inside the same transaction.

        Review round (P2-1): the return carries ``deleted_rows`` — the
        row snapshots taken BEFORE the transactional DELETE — so the
        CALLER can perform the external hygiene (vector embeds + vault
        files) for the deleted chunk ids AFTER the commit. The store
        owns the atomic row/version swap; the manager-side caller owns
        the side stores (the same hygiene split as
        :meth:`MemoryManager.delete
        <vesmaro.manager.MemoryManager.delete>`: vectors.delete +
        vault.delete_file live outside the SQL transaction — a rollback
        must not have destroyed them for rows that survive).

        Args:
            doc_id: The logical document identity (validated upstream).
            rows: The NEW chunk rows (each already carries the doc-grouping
                metadata triple). Stored via the same INSERT statement
                shape as :meth:`save` (fresh ids — the caller mints them).
            cache_version_key: The ``meta`` key whose value is bumped
                (monotonic integer as text) in the same transaction.

        Returns:
            ``{"deleted": N, "inserted": M, "cache_version": V,
            "deleted_rows": [Memory, …]}`` — the per-statement row
            counts, the NEW cache version value, and the pre-DELETE
            snapshots of the replaced rows (for post-commit hygiene).
        """
        if not rows:
            raise ValueError("replace_doc_chunks: rows must be non-empty")
        conn = self._get_conn()
        now = datetime.now(UTC).isoformat()
        insert_sql = (
            "INSERT INTO memories "
            "(id, content, title, tags, source, source_url, memory_type, "
            " created_at, updated_at, metadata, file_path, category, "
            " project, agent, status, quality_score, confidence, "
            " source_coverage, cluster_id, derived_from, embedding_id, "
            " raw_content, clean_content, filter_profile, filter_stats, "
            " filter_version, pipeline_state, processed_at, swap_key, "
            " quarantine_reason, marker_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
        # Pre-DELETE snapshots of the rows being replaced — taken BEFORE
        # the transaction so the caller gets them even if the DELETE
        # itself is what the crash pins (post-commit hygiene needs the
        # ids/file_paths, not the row bodies).
        deleted_rows = self.list_doc_chunks(doc_id)
        # B608: the script is composed EXCLUSIVELY of static literals and
        # bound parameters via conn.execute — the json_extract fragment is
        # a static string with a `?` bind for doc_id. One transaction so
        # the chunk replacement and the cache-version bump are atomic.
        try:
            conn.execute("BEGIN IMMEDIATE")
            deleted = int(
                conn.execute(
                    "DELETE FROM memories WHERE json_extract(metadata, '$.doc_id') = ?",
                    (doc_id,),
                ).rowcount
            )
            for memory in rows:
                pipeline_state_val = memory.pipeline_state.value if memory.pipeline_state else None
                processed_at_val = memory.processed_at.isoformat() if memory.processed_at else None
                meta_json = json.dumps(memory.metadata, ensure_ascii=False)
                title = memory.auto_title()
                conn.execute(
                    insert_sql,
                    (
                        memory.id,
                        memory.content,
                        title,
                        json.dumps(memory.tags, ensure_ascii=False),
                        memory.source.value,
                        memory.source_url,
                        memory.memory_type.value,
                        memory.created_at.isoformat(),
                        memory.updated_at.isoformat(),
                        meta_json,
                        memory.file_path,
                        memory.category,
                        memory.project,
                        memory.agent,
                        memory.status.value,
                        memory.quality_score,
                        memory.confidence,
                        memory.source_coverage,
                        memory.cluster_id,
                        json.dumps(memory.derived_from, ensure_ascii=False),
                        memory.embedding_id,
                        memory.raw_content,
                        memory.clean_content,
                        memory.filter_profile,
                        (
                            json.dumps(memory.filter_stats, ensure_ascii=False)
                            if memory.filter_stats
                            else None
                        ),
                        memory.filter_version,
                        pipeline_state_val,
                        processed_at_val,
                        memory.swap_key,
                        memory.quarantine_reason,
                        memory.marker_version,
                    ),
                )
            inserted = len(rows)
            # The version bump: read-modify-write INSIDE the transaction.
            # BEGIN IMMEDIATE holds the write lock, so two racing re-ingests
            # serialize; each commits a strictly larger integer.
            row = conn.execute(
                "SELECT value FROM meta WHERE key=?", (cache_version_key,)
            ).fetchone()
            prev = int(row["value"]) if row is not None else 0
            new_version = prev + 1
            conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "  value=excluded.value, updated_at=excluded.updated_at",
                (cache_version_key, str(new_version), now),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.rollback()
            raise
        self._invalidate_caches()
        logger.info(
            "doc re-chunk: doc_id=%s deleted=%d inserted=%d cache_version=%d",
            doc_id[:32],
            deleted,
            inserted,
            new_version,
        )
        return {
            "deleted": deleted,
            "inserted": inserted,
            "cache_version": new_version,
            "deleted_rows": deleted_rows,
        }

    def doc_chunk_cache_version(self, cache_version_key: str) -> int:
        """Read the current ccr_cache doc-chunk version (0 when absent)."""
        value = self.get_meta(cache_version_key)
        return int(value) if value is not None else 0

    def list_doc_chunks(self, doc_id: str) -> list[Memory]:
        """Full row snapshots of every chunk of ``doc_id`` (Ф0 convention).

        The re-ingest replace path uses the pre-DELETE snapshots for the
        post-commit external hygiene (vector embeds + vault files of the
        replaced rows) — see :meth:`replace_doc_chunks`. Ordered by the
        convention ``chunk_idx`` (document order).
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM memories "
            "WHERE json_extract(metadata, '$.doc_id') = ? "
            "ORDER BY CAST(json_extract(metadata, '$.chunk_idx') AS INTEGER) ASC",
            (doc_id,),
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def list_doc_chunk_ids(self, doc_id: str) -> list[str]:
        """Ids of every chunk row of ``doc_id`` (the Ф0 metadata convention).

        Keys on ``json_extract(metadata, '$.doc_id')`` — the Ф0
        convention IS the doc identity; no dedicated column exists
        (zero migration is a Phase-3 premise). Ordered by the convention
        ``chunk_idx`` so callers see document order.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id FROM memories "
            "WHERE json_extract(metadata, '$.doc_id') = ? "
            "ORDER BY CAST(json_extract(metadata, '$.chunk_idx') AS INTEGER) ASC",
            (doc_id,),
        ).fetchall()
        return [str(r["id"]) for r in rows]

    def delete_doc_chunks(self, doc_id: str) -> int:
        """Delete every chunk row of ``doc_id`` (no cache bump — test/maintenance)."""
        conn = self._get_conn()
        cur = conn.execute(
            "DELETE FROM memories WHERE json_extract(metadata, '$.doc_id') = ?", (doc_id,)
        )
        conn.commit()
        self._invalidate_caches()
        return int(cur.rowcount)

    # ── Memory edges (ADR-0018 Phase 1 groundwork) ─────────────────────────

    def add_memory_edge(
        self,
        from_memory_id: str,
        to_memory_id: str,
        *,
        kind: str = "supersedes",
        weight: float = 1.0,
        provenance: str = "declared",
        scope_project: str | None = None,
        scope_agent: str | None = None,
    ) -> bool:
        """Add a directed edge between two memories (idempotent).

        ADR-0030 A0 (issue #321): alongside the two kinds the edge
        carries ``weight`` (DEFAULT 1.0 — auto-minted near-dup edges may
        raise it; feedback factors multiply at read time, never here),
        ``provenance`` ('declared' for explicit callers, the auto-dedupe
        rule id for minted edges) and the nullable ``scope_project`` /
        ``scope_agent`` columns reserved for I5 scoped feedback.

        Returns ``True`` when a new edge was inserted, ``False`` when an
        identical edge already existed (INSERT OR IGNORE on the
        (from, to, kind) primary key).

        Raises:
            ValueError: self-edge (``from == to``), unknown ``kind``,
                empty ``provenance``, or a non-finite / non-positive
                ``weight``. A memory superseding itself is meaningless
                and signals a caller bug — rejected here with a
                friendly error; the SQL CHECK constraint is the
                defence-in-depth backstop. Weight validation (#324
                scope-addition from the #336 review): negative / 0 /
                +inf / NaN weights are rejected BEFORE the bind — a
                NaN would otherwise bind to SQL NULL and fail the
                column's NOT NULL constraint with a confusing
                IntegrityError instead of a caller-actionable message.
            sqlite3.IntegrityError: either memory id does not exist
                (foreign key, ``PRAGMA foreign_keys=ON``).
        """
        if kind not in _EDGE_KINDS:
            raise ValueError(f"unknown edge kind {kind!r}; supported kinds: {sorted(_EDGE_KINDS)}")
        if from_memory_id == to_memory_id:
            raise ValueError("self-edges are not allowed (from_memory_id == to_memory_id)")
        if not provenance:
            raise ValueError("provenance must be a non-empty string ('declared' or a rule id)")
        # ``math.isfinite`` covers NaN and ±inf in one check; the <= 0
        # arm covers negative and zero weights (a zero-weight edge is a
        # no-op claim — write it when it means something).
        if not math.isfinite(weight) or weight <= 0.0:
            raise ValueError(
                f"weight must be a finite positive number, got {weight!r} "
                "(negative/0/inf/NaN are rejected at the write boundary)"
            )
        conn = self._get_conn()
        cur = conn.execute(
            "INSERT OR IGNORE INTO memory_edges "
            "(from_memory_id, to_memory_id, kind, created_at, weight, provenance, "
            " scope_project, scope_agent) VALUES (?,?,?,?,?,?,?,?)",
            (
                from_memory_id,
                to_memory_id,
                kind,
                datetime.now(UTC).isoformat(),
                weight,
                provenance,
                scope_project,
                scope_agent,
            ),
        )
        conn.commit()
        return cur.rowcount > 0

    def get_direct_edges(
        self,
        from_memory_id: str,
        *,
        kind: str = "supersedes",
    ) -> list[dict[str, Any]]:
        """Return direct outgoing edges for ``from_memory_id`` (no expansion).

        One hop only — graph traversal/expansion is Phase 2 (ADR-0018).
        Returns ``[{"from_memory_id","to_memory_id","kind","created_at",
        "weight","provenance"}]`` ordered by creation time ascending.
        ADR-0030 A0 (issue #322): ``weight``/``provenance`` ride along so
        minted (``auto-dedupe``) edges are distinguishable from declared
        ones at read time — the keys are additive; existing consumers
        read by name and are unaffected.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT from_memory_id, to_memory_id, kind, created_at, weight, provenance "
            "FROM memory_edges WHERE from_memory_id = ? AND kind = ? "
            "ORDER BY created_at ASC",
            (from_memory_id, kind),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_incoming_edges(
        self,
        to_memory_id: str,
        *,
        kind: str = "supersedes",
    ) -> list[dict[str, Any]]:
        """Return direct incoming edges for ``to_memory_id`` (no expansion).

        Search v2 graph leg (issue #313): the 1-hop expansion walks BOTH
        directions of ``supersedes`` — a fused hit surfaces the newer
        version that replaced it (incoming, this method) AND the older
        sibling it replaced (outgoing, ``get_direct_edges``). Same shape
        and ordering contract as ``get_direct_edges`` (weight/provenance
        included per ADR-0030 A0, issue #322).
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT from_memory_id, to_memory_id, kind, created_at, weight, provenance "
            "FROM memory_edges WHERE to_memory_id = ? AND kind = ? "
            "ORDER BY created_at ASC",
            (to_memory_id, kind),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── edge_stats: used/rejected feedback capture (ADR-0030 A0, #323) ─────

    def record_edge_stat_event(
        self,
        event_id: str,
        memory_id: str,
        *,
        kind: str,
        project: str = "",
        agent: str = "",
    ) -> Literal["inserted", "duplicate", "cap_dropped"]:
        """Append one used/rejected feedback event (idempotent, capped).

        I5 mechanics (ADR-0030 A0, issue #323) — the gates live in the
        manager wrapper (``MemoryManager.report_search_feedback``); this
        method is the pure storage leg:

        * ``event_id`` PK idempotency — ``INSERT OR IGNORE``: re-recording
          the same event (an agent retry) returns ``"duplicate"`` and
          contributes no second row, hence no double weight;
        * volume-cap per principal — when the ``(project, agent)`` bucket
          already holds ``EDGE_STATS_EVENTS_PER_PRINCIPAL_CAP`` rows the
          event is DROPPED (``"cap_dropped"``), never an error: the cap
          is a storage-DoS / APPLY pre-poisoning guard (I5), not a
          caller-facing quota. The cap is checked BEFORE the insert
          (append-only leaves no take-back), so a duplicate event id
          arriving while the bucket sits at cap reports
          ``"cap_dropped"`` rather than ``"duplicate"`` — both are
          no-op drops;
        * GLOBAL volume cap (review #338 N2) — when the table as a whole
          already holds ``EDGE_STATS_TOTAL_ROWS_CAP`` rows the event is
          likewise DROPPED. The per-bucket cap bounds one identity; this
          one bounds the table across ALL minted principals. Capture
          stays dropped until an operator reclaims rows via
          ``purge_edge_stats_oldest`` — there is NO automatic eviction
          by design (the audit trail shrinks only by an explicit,
          logged, operator action);
        * append-only — there is no UPDATE/DELETE path on edge_stats
          anywhere (the schema triggers abort both; the audit trail is a
          database guarantee). The ONE sanctioned exception is the
          operator purge (``purge_edge_stats_oldest``), which drops and
          recreates the DELETE trigger inside its own transaction and
          stamps the purge into the ``meta`` audit trail.

        Args:
            event_id: Row id; the manager derives retry-stable ids from
                the caller's logical report id.
            memory_id: The cited memory id (from the search response).
            kind: ``'used'`` or ``'rejected'``.
            project: Reporting principal's project (scope from the
                FIRST event — the table never learns it later).
            agent: Reporting principal's agent.

        Returns:
            ``"inserted"`` | ``"duplicate"`` | ``"cap_dropped"``.

        Raises:
            ValueError: unknown ``kind`` or empty ``event_id``.
        """
        if kind not in EDGE_STATS_KINDS:
            raise ValueError(
                f"unknown feedback kind {kind!r}; supported kinds: {sorted(EDGE_STATS_KINDS)}"
            )
        if not event_id:
            raise ValueError("event_id must be a non-empty string (idempotency key)")
        conn = self._get_conn()
        principal_rows = conn.execute(
            "SELECT COUNT(*) FROM edge_stats WHERE project = ? AND agent = ?",
            (project, agent),
        ).fetchone()[0]
        if int(principal_rows) >= EDGE_STATS_EVENTS_PER_PRINCIPAL_CAP:
            return "cap_dropped"
        if self.count_edge_stats() >= EDGE_STATS_TOTAL_ROWS_CAP:
            return "cap_dropped"
        cur = conn.execute(
            "INSERT OR IGNORE INTO edge_stats "
            "(event_id, memory_id, kind, project, agent, created_at) VALUES (?,?,?,?,?,?)",
            (event_id, memory_id, kind, project, agent, datetime.now(UTC).isoformat()),
        )
        conn.commit()
        return "inserted" if cur.rowcount > 0 else "duplicate"

    def get_edge_stats_counters(self, memory_id: str) -> dict[str, int]:
        """Per-memory used/rejected counters, clamped (I5 bounded clamp).

        The APPLY-ready read surface (#325 consumes this when feedback
        starts influencing rank). Clamped from day one at
        ``EDGE_STATS_COUNTER_CLAMP``: whatever the table accumulates, no
        counter exceeds the clamp — capture cannot drift before APPLY
        exists. Always returns both keys (0 for absent kinds).
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT kind, COUNT(*) AS n FROM edge_stats WHERE memory_id = ? GROUP BY kind",
            (memory_id,),
        ).fetchall()
        counters = {"used": 0, "rejected": 0}
        for row in rows:
            counters[str(row["kind"])] = min(int(row["n"]), EDGE_STATS_COUNTER_CLAMP)
        return counters

    def get_edge_stats_used_counts_batch(self, memory_ids: list[str]) -> dict[str, int]:
        """Per-memory CLAMPED ``used`` counters for a batch of ids.

        ADR-0030 A1-S2 (issue #325) — the APPLY read leg: ONE indexed
        ``WHERE memory_id IN (...) AND kind = 'used'`` point query per
        walked-block construction (the idx_edge_stats_memory index
        serves it). Values are clamped at ``EDGE_STATS_COUNTER_CLAMP``
        (the I5 bounded-counter discipline: whatever the table
        accumulates, the readable signal stays bounded — the caller's
        saturating factor then bounds the RANK delta on top, I6). An
        id with no rows simply has no entry — the caller reads absent
        as 0. ``rejected`` events are deliberately NOT read: the S2
        contract boosts by real usage only (a rejected citation is not
        anti-boosted; demotion experiments wait for the A1 telemetry
        checkpoint).
        """
        if not memory_ids:
            return {}
        conn = self._get_conn()
        placeholders = ",".join("?" * len(memory_ids))
        rows = conn.execute(
            "SELECT memory_id, COUNT(*) AS n FROM edge_stats "
            f"WHERE memory_id IN ({placeholders}) AND kind = 'used' "
            "GROUP BY memory_id",
            tuple(memory_ids),
        ).fetchall()
        return {str(row["memory_id"]): min(int(row["n"]), EDGE_STATS_COUNTER_CLAMP) for row in rows}

    def count_edge_stats(self, *, kind: str | None = None) -> int:
        """Durable edge_stats row count (telemetry; optional kind filter).

        The GLOBAL volume-cap check in ``record_edge_stat_event`` reads
        the unfiltered total here (review #338 N2). The dashboard/stats
        totals moved to ``count_edge_stats_by_kind`` (review #338 N3 —
        one aggregation instead of three scans); this stays the
        single-count primitive for callers that need exactly one number.
        """
        conn = self._get_conn()
        if kind is None:
            row = conn.execute("SELECT COUNT(*) FROM edge_stats").fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM edge_stats WHERE kind = ?", (kind,)).fetchone()
        return int(row[0])

    def count_edge_stats_by_kind(self) -> dict[str, int]:
        """Per-kind durable row counts in ONE ``GROUP BY kind`` scan.

        Review #338 N3: ``MemoryManager.feedback_capture_stats`` used to
        read its three durable numbers with three separate full scans
        (``count_edge_stats(kind=...)`` ×2 + ``count_edge_stats()``);
        this method answers all of them from a single aggregation. The
        SQL CHECK on ``kind`` guarantees no row exists outside
        ``EDGE_STATS_KINDS``, so ``sum(values())`` IS the exact table
        total — no second counting pass needed. Always returns every
        kind key (0 for absent kinds), the ``get_edge_stats_counters``
        convention.
        """
        conn = self._get_conn()
        rows = conn.execute("SELECT kind, COUNT(*) AS n FROM edge_stats GROUP BY kind").fetchall()
        counts = {k: 0 for k in EDGE_STATS_KINDS}
        for row in rows:
            counts[str(row["kind"])] = int(row["n"])
        return counts

    def purge_edge_stats_oldest(self, *, keep_last: int, dry_run: bool = True) -> dict[str, Any]:
        """Operator maintenance path: drop the OLDEST edge_stats rows.

        Review #338 N2 — the per-principal cap bounds one identity but
        the table is unbounded across minted principals, and the
        append-only triggers forbid DELETE. This is the ONE sanctioned
        shrink path, and it is explicitly an OPERATOR action (the
        ``vesmaro edge-stats purge`` CLI wraps it, dry-run by default) —
        NEVER automatic eviction.

        Mechanics (all inside ONE explicit transaction, opened with
        ``BEGIN IMMEDIATE`` before any DDL — the python sqlite3 driver
        in legacy isolation mode autocommits DDL unless a transaction
        is already open, so the explicit BEGIN is what makes an abort
        restore the pre-purge world INCLUDING the trigger):

        1. count what a purge would remove (rows beyond the newest
           ``keep_last`` — i.e. everything after the newest-``keep_last``
           prefix of ``created_at DESC, rowid DESC``; among same-
           timestamp rows the later insertion survives longer);
        2. ``BEGIN IMMEDIATE``;
        3. ``DROP TRIGGER edge_stats_no_delete``;
        4. ``DELETE`` those rows;
        5. re-``CREATE`` the trigger from the SHARED
           ``_EDGE_STATS_NO_DELETE_TRIGGER_DDL`` constant — the same
           literal ``_DB_SCHEMA`` installs (single source of truth, no
           drifting second copy);
        6. stamp the purge into ``meta`` under
           ``EDGE_STATS_LAST_PURGE_META_KEY`` — the compensating audit
           trail: the append-only table shrank, and the record of that
           shrink survives (read back by the CLI);
        7. ``COMMIT`` (any failure rolls everything back — the
           trigger included; pinned by
           ``test_mid_purge_failure_restores_trigger_and_rows``).

        Dry run (default) performs step 1 only — zero writes.

        Args:
            keep_last: Retention target — the newest N rows survive;
                must be ≥ 0 (0 = purge everything).
            dry_run: Count only, write nothing (default).

        Returns:
            ``{"rows_before", "purged", "rows_after", "dry_run"}`` —
            ``purged``/``rows_after`` are the PROJECTION in dry-run mode.

        Raises:
            ValueError: ``keep_last`` is negative.
        """
        if keep_last < 0:
            raise ValueError(f"keep_last must be >= 0, got {keep_last}")
        conn = self._get_conn()
        rows_before = int(conn.execute("SELECT COUNT(*) FROM edge_stats").fetchone()[0])
        doomed = int(
            conn.execute(
                "SELECT COUNT(*) FROM (SELECT event_id FROM edge_stats "
                "ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?)",
                (keep_last,),
            ).fetchone()[0]
        )
        if dry_run:
            return {
                "rows_before": rows_before,
                "purged": doomed,
                "rows_after": rows_before - doomed,
                "dry_run": True,
            }
        try:
            # Transactionality is EXPLICIT, not implicit (review #338
            # round 2, MAJOR): the python sqlite3 driver in legacy
            # isolation mode opens implicit transactions for DML only —
            # DDL alone AUTOCOMMITS. Without this BEGIN the DROP TRIGGER
            # below would commit immediately, and a later failure in the
            # purge would leave the DELETE guard durably absent after
            # rollback() — plain DELETEs would then succeed. BEGIN
            # IMMEDIATE (the store's executescript-transaction
            # precedent) makes drop+delete+recreate+stamp one atomic
            # unit: an abort restores the pre-purge world INCLUDING the
            # trigger.
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DROP TRIGGER IF EXISTS edge_stats_no_delete")
            deleted = int(
                conn.execute(
                    "DELETE FROM edge_stats WHERE event_id IN ("
                    "SELECT event_id FROM edge_stats "
                    "ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?)",
                    (keep_last,),
                ).rowcount
            )
            conn.execute(_EDGE_STATS_NO_DELETE_TRIGGER_DDL)
            stamp = json.dumps(
                {
                    "at": datetime.now(UTC).isoformat(),
                    "purged": deleted,
                    "keep_last": keep_last,
                }
            )
            conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                (EDGE_STATS_LAST_PURGE_META_KEY, stamp, datetime.now(UTC).isoformat()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        logger.info(
            "edge_stats operator purge: keep_last=%d purged=%d rows_after=%d",
            keep_last,
            deleted,
            rows_before - deleted,
        )
        return {
            "rows_before": rows_before,
            "purged": deleted,
            "rows_after": rows_before - deleted,
            "dry_run": False,
        }

    def get_memory_id_by_rewrite_event_key(self, event_key: str) -> str | None:
        """Return the memory id carrying ``rewrite_event_key``.

        Idempotency lookup for the ``on_context_rewrite`` event (ADR-0018,
        mnemos #125 Wave 2): the event handler computes a content-addressed
        key and consults this BEFORE any write, so a re-delivered event
        performs no duplicate writes. Deliberately a specific method, not a
        generic metadata query — the surface stays minimal (same philosophy
        as ``_EDGE_KINDS``).

        m3 (final review): reads the denormalised ``rewrite_event_key``
        column (index ``idx_memories_rewrite_event_key_created``) instead of
        a full-scan ``json_extract`` per delivery — the exact pattern C10
        eliminated for the quota count. The column is derived in ``save()``
        ONLY under ``trusted_rewrite_provenance`` and backfilled once from
        trusted-path rows, so a key planted through client metadata can
        never be found here. Returns the EARLIEST match (creation order)
        or ``None``.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT id FROM memories WHERE rewrite_event_key = ? ORDER BY created_at ASC LIMIT 1",
            (event_key,),
        ).fetchone()
        return str(row["id"]) if row is not None else None

    def count_recent_context_rewrites(
        self, project: str, session: str | None, since_iso: str
    ) -> int:
        """Count STORED rewrite-event memories for ``(project, session)`` at/after ``since_iso``.

        Backs the per-(project, session) write quota (#125 W2 review F1 —
        mirrors the #96 guardrail-5 pattern). Counts rows in ``memories``,
        i.e. stored events only: a deduplicated re-delivery performs no
        write and therefore consumes no quota. ``session=None`` matches
        rows stored without a session (null-safe ``IS`` comparison).
        C10 (ArchCom 2026-08-27): filters on the denormalised
        ``rewrite_source`` / ``rewrite_session`` columns (maintained by
        ``save()`` and the one-time backfill) so the count is served by
        ``idx_memories_project_rewrite_source_created`` instead of a
        ``json_extract`` full scan per call.
        """
        from vesmaro.context_rewrite import SOURCE_CONTEXT_REWRITE

        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM memories "
            "WHERE project = ? AND created_at >= ? AND rewrite_source = ? "
            "AND rewrite_session IS ?",
            (project, since_iso, SOURCE_CONTEXT_REWRITE, session),
        ).fetchone()
        return int(row["n"]) if row else 0

    def count_recent_context_rewrites_by_project(
        self, project: str, since_iso: str
    ) -> tuple[int, int]:
        """Return ``(rows, distinct_sessions)`` for rewrite events in a project.

        C10 (ArchCom 2026-08-27) — backs the SECONDARY per-project
        aggregate write quota: the row count trips the ceiling, the
        distinct-session count is the noisy-neighbor signal carried in
        the log line and the 429 message (one session burning the whole
        project budget vs many sessions). NULL-session events count as
        rows AND as one session bucket (``COALESCE`` — session slugs are
        validated non-empty, so ``''`` cannot collide with a real one).
        Index-backed via ``idx_memories_project_rewrite_source_created``.
        """
        from vesmaro.context_rewrite import SOURCE_CONTEXT_REWRITE

        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS n, "
            "COUNT(DISTINCT COALESCE(rewrite_session, '')) AS s "
            "FROM memories "
            "WHERE project = ? AND created_at >= ? AND rewrite_source = ?",
            (project, since_iso, SOURCE_CONTEXT_REWRITE),
        ).fetchone()
        if row is None:  # pragma: no cover — aggregate always returns a row
            return 0, 0
        return int(row["n"]), int(row["s"])
