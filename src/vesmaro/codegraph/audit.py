"""PG7 audit trail for the project graph (ADR-0032 §3.6 invariant 7).

Every index / reindex / delete / snippet-read / graph-read lands in the
append-only ``graph_audit`` table of the SIDECAR database (the table
ships with the slice-1 schema) — the same ``code_graph.db`` file, but
through its OWN connection: one sidecar, two writers, WAL-mediated (the
``CodeGraphStore.db_path`` property documents the contract). Reads are
attributed per-agent (``actor`` = agent_id, ``session`` = session_id
when the caller binds one); a call WITHOUT agent attribution is refused
at the tool layer BEFORE any read happens — this module never sees an
anonymous actor.

``details`` is a JSON object for coarse counters only (query shape,
verdicts, byte totals) — NEVER source bytes, snippet text or query
literals that could smuggle content into the audit trail (PG1 applies
to the audit too).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

#: Audit ``action`` values (the closed vocabulary of §3.6).
#: ``auto-register`` joined in PG-0.5 (owner directive 2026-09-29): the
#: native auto-indexer's marker-gated project registration is its own
#: audited verb — recorded with the triggering agent as the actor.
#: ``auto-register-reused`` / ``auto-register-capped`` joined in the
#: PG-0.5 fix-slice (PR #443 review P2-1): one root = one graph (a name
#: hint over an already-registered root reuses the project instead of
#: duplicating it) and the ``auto_register_max_projects`` cap refuse —
#: both silent skips on the auto path, both audit-first-class.
#: ``manual-register`` / ``manual-register-reused`` (#454) and
#: ``repoint`` (#450) joined the project-lifecycle wave: agent-side
#: explicit registration (idempotent on the same root) and the operator
#: re-point of a ghost registration whose root moved on disk.
#: ``manual-register-refused`` / ``repoint-refused`` joined in #464
#: (P3-2): every manual register/repoint refusal is audit-first-class —
#: the refusal reason rides the row's ``reason`` field, matching the
#: ``auto-register-capped`` precedent (a refusal the operator cannot
#: see is a silent scope event).
#: ``search-walk`` joined in PG-1 M2 (ADR-0038 conditions 7-8): every
#: search_walk execution writes its OWN row with the details (nodes /
#: edges / k / truncated) PLUS the token-economics pair ``out_tokens``
#: and ``avoided_bytes`` — the walk prices itself from day one.
AUDIT_ACTIONS = (
    "index",
    "reindex",
    "delete",
    "snippet-read",
    "graph-read",
    "search-walk",
    "auto-register",
    "auto-register-reused",
    "auto-register-capped",
    "manual-register",
    "manual-register-reused",
    "manual-register-refused",
    "repoint",
    "repoint-refused",
    "delete-refused",
)


class GraphAudit:
    """Append-only writer for the sidecar ``graph_audit`` table.

    A dedicated thread-local connection (NOT the store's) keeps the
    audit path independent of the store's transactions — an audit row
    for a failed index is exactly the row the operator needs, so the
    audit write must not share the store's rollback scope.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = str(db_path)
        self._local = threading.local()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    def record(
        self,
        project: str,
        action: str,
        actor: str,
        *,
        session: str | None = None,
        reason: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Append one audit row. ``action`` must be in the closed
        vocabulary; ``actor`` is the non-empty agent id (the PG7
        binding — the tool layer refuses anonymous callers before this
        runs). A bad row raises (fail-closed): audit is part of the
        operation, not best-effort telemetry."""
        if action not in AUDIT_ACTIONS:
            raise ValueError(f"unknown audit action: {action!r}")
        if not actor or not actor.strip():
            raise ValueError("audit requires a non-empty actor (agent attribution, PG7)")
        try:
            self._conn().execute(
                "INSERT INTO graph_audit (project, action, actor, session, reason, details, ts) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    project,
                    action,
                    actor,
                    session,
                    reason,
                    json.dumps(details or {}, ensure_ascii=False),
                    datetime.now(UTC).isoformat(),
                ),
            )
            self._conn().commit()
        except sqlite3.Error:
            logger.exception("graph audit write failed (project=%s action=%s)", project, action)
            raise

    def has_search(self, actor: str, session: str | None) -> bool:
        """True when a prior ``search`` graph-read row exists for this
        (actor, session) pair — NULL-safe, so a session-less caller is
        scoped by the actor alone (card vesma-graph-audit-firstcall-
        marking). The searcher asks BEFORE recording: a ``False`` answer
        marks the task's FIRST search call, which the row then carries
        as ``details.first_search``.

        The marker makes the graph-first share computable from
        graph_audit rows ALONE:

        ```sql
        -- Share of graph-using tasks whose FIRST search was answered
        -- from symbols (productive graph-first), per actor:
        SELECT actor,
               100.0 * SUM(CASE
                   WHEN json_extract(details, '$.first_search') = 1
                    AND json_extract(details, '$.literal_fallback') = 0
                    AND json_extract(details, '$.total') > 0
                   THEN 1 ELSE 0 END)
               / COUNT(DISTINCT actor || ':' || COALESCE(session, ''))
               AS graph_first_share_pct
        FROM graph_audit
        WHERE action = 'graph-read' AND reason = 'search'
        GROUP BY actor;
        ```
        """
        row = (
            self._conn()
            .execute(
                "SELECT 1 FROM graph_audit "
                "WHERE action='graph-read' AND reason='search' "
                "AND actor=? AND session IS ? LIMIT 1",
                (actor, session),
            )
            .fetchone()
        )
        return row is not None

    def recent(self, project: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Newest audit rows (debug/eyes surface; never source bytes)."""
        limit = max(1, min(int(limit), 500))
        sql = "SELECT project, action, actor, session, reason, details, ts FROM graph_audit"
        params: tuple[Any, ...] = ()
        if project is not None:
            sql += " WHERE project=?"
            params = (project,)
        sql += " ORDER BY id DESC LIMIT ?"
        rows = self._conn().execute(sql, (*params, limit)).fetchall()
        return [
            {
                "project": r[0],
                "action": r[1],
                "actor": r[2],
                "session": r[3],
                "reason": r[4],
                "details": json.loads(r[5]) if r[5] else {},
                "ts": r[6],
            }
            for r in rows
        ]

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
