"""The project-graph control layer: CodeGraphService (ADR-0032 PG-0 slice 4).

One object owns the sidecar store, the indexer facade and the PG7 audit
trail, and exposes the 10-tool surface (contract §3.3) to BOTH the MCP
and REST twins — the tools are thin adapters, the policy lives here:

* **PG2 confinement** — every operation resolves the project through
  the MAIN-DB ``projects`` table; a graph key is minted ONLY from a
  registered project and the index root is ONLY the operator-registered
  path. Arbitrary filesystem paths from agents never reach the indexer
  or the reader.
* **Token contract (§3.4, DeusData line-for-line)** —
  ``max_output_tokens`` 128-1M (default 3200); a deterministic
  ceiling of 4 UTF-8 bytes per token (budget in BYTES = tokens x 4);
  WHOLE-ROW
  drop (output is never cut mid-row by a byte count); ranked rows
  survive raw rows (ranking happens BEFORE the budget cut); ``has_more``
  plus STRICTLY ADVANCING cursors — a budget that cannot fit even one
  row is a REFUSAL («запроси больше бюджета»), never an empty answer
  with the same cursor (no self-looping).
* **PG4 issuance** — snippets are read from disk at request time with
  mtime/size + sha256 verification against ``graph_files``; any
  divergence yields the staleness marker, NEVER content; the issued
  range is secret-scanned and ANY finding refuses the whole snippet
  fail-closed with the reason (refuse-mode is not config-optional on
  this surface — PG4 is a binding invariant). No snippet cache exists,
  by design.
* **PG3 «навсегда»** — a poisoned path refuses snippets even after
  reindexation with the same content; only tool 10
  (``delete_graph_project`` → ``CodeGraphStore.purge_project``) clears
  the set.
"""

from __future__ import annotations

import logging
import os
import weakref
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from vesmaro.codegraph import incremental as incremental_mod
from vesmaro.codegraph.audit import GraphAudit
from vesmaro.codegraph.indexer import IndexLimitError, IndexResult
from vesmaro.config import CodeGraphConfig
from vesmaro.secrets_detector import detect_secrets, findings_by_pattern
from vesmaro.storage.code_graph_store import (
    EDGE_KINDS,
    NODE_KINDS,
    CodeGraphStore,
    bump_project_graph_epoch,
)

if TYPE_CHECKING:
    # Annotation-only (the protocol below names the concrete model).
    from vesmaro.models import Project

logger = logging.getLogger(__name__)

#: Token contract (§3.4): default and hard range of ``max_output_tokens``.
DEFAULT_MAX_OUTPUT_TOKENS = 3200
MIN_MAX_OUTPUT_TOKENS = 128
MAX_MAX_OUTPUT_TOKENS = 1_000_000

#: Deterministic ceiling: 4 UTF-8 bytes per token (§3.4).
BYTES_PER_TOKEN = 4

#: Trace caps (tool 4) — the ADR-0030 walk pattern: per-node fanout cap
#: bounds hub symbols, the total-work cap bounds the whole BFS.
TRACE_MAX_DEPTH = 2
TRACE_FANOUT_CAP = 32
TRACE_TOTAL_WORK_CAP = 512

#: Search surface: hard row ceiling of ONE page regardless of budget.
SEARCH_ROW_CAP = 200

#: Sidecar schema version reported by ``get_graph_schema``.
GRAPH_SCHEMA_VERSION = 1

#: Sidecar ``graph_meta`` key prefix for the auto-path suspension flag
#: (PG-0.5 fix-slice, PR #443 review P2-2): set to ``1`` when a FIRST
#: auto index fails, so a failing tree is not re-walked on every hint;
#: cleared (``0``) by a successful ``index_project`` publish (manual or
#: watch — the watch poll rides the same method) or by
#: ``delete_graph_project``. The ``{len}`` segment keeps the key
#: colon-safe, the same discipline as every other graph_meta key.
_AUTO_SUSPENDED_PREFIX = "auto_suspended:"

#: Packaging manifests accepted as project-root markers (PR #443 review
#: P2-2): the operator's footprint on disk, the canonical manifests of
#: the supported ecosystems. A bare ``.git`` deliberately does NOT
#: qualify for AUTO-registration — a dotfiles ``$HOME`` is a repo, not a
#: project — and neither do lockfiles (generated artifacts, not
#: declarations). Lives here (not in ``autoindex``) since PG-0.5: the
#: manual registration/repoint paths (#454/#450) share the same
#: root-shape gate; ``autoindex`` re-exports it for its tests.
PROJECT_MARKERS: tuple[str, ...] = (
    "pyproject.toml",
    "setup.py",
    "package.json",
    "go.mod",
    "Cargo.toml",
)

#: One line appended to every unregistered-project confinement refusal
#: (#454): the caller is told HOW to register, not just that it cannot.
REGISTER_HINT = (
    " — to register it: the mnemos_register_project tool "
    "(agent attribution required) or 'vesma graph register <project> <root>'"
)


def project_marker(cwd: str) -> str | None:
    """The FIRST packaging manifest found in ``cwd`` (a stat per
    candidate — cheap by contract), or ``None`` when the directory is
    not a project root. A non-directory ``cwd`` never registers; a bare
    ``.git`` is not an auto marker (P2-2) but DOES qualify as a root
    shape for the manual repoint path (#450 — an operator pointing at a
    checkout knows what they are doing; the AUTO path stays stricter)."""
    if not os.path.isdir(cwd):
        return None
    for marker in PROJECT_MARKERS:
        if os.path.exists(os.path.join(cwd, marker)):
            return marker
    return None


def _is_forbidden_root(cwd: str) -> bool:
    """``$HOME`` and the filesystem root are NEVER graph roots (PR #443
    review P2-2, now shared by the manual paths #450/#454): even a
    manifest sitting there (a dotfiles repo exporting a
    ``package.json`` into ``$HOME``) must not turn the server's own home
    into a graph project."""
    root = Path(cwd)
    if str(root) == root.anchor:  # "/" on POSIX, "C:\\" on Windows
        return True
    try:
        return root == Path.home()
    except RuntimeError:  # no resolvable home — the marker gate decides
        return False


def auto_suspended_key(project: str) -> str:
    """The sidecar ``graph_meta`` key of the auto-path suspension flag
    for one project (graph key)."""
    return f"{_AUTO_SUSPENDED_PREFIX}{len(project)}:{project}"


class GraphToolError(ValueError):
    """Base for refused graph operations (mapped to tool errors, never
    tracebacks — the reason strings are safe to issue)."""


class GraphDisabledError(GraphToolError):
    """The code_graph master flag is OFF (default; wave gate)."""


class GraphAttributionError(GraphToolError):
    """PG7 binding: the call carries no agent attribution."""


class GraphConfinementError(GraphToolError):
    """PG2: the project is not registered / has no registered root, or
    a path escapes the registered root."""


class GraphBudgetError(GraphToolError):
    """Token contract self-loop guard: the budget cannot fit even one
    output row — the caller must ask for a bigger budget."""


class ProjectRecord(Protocol):
    """The slice of the main-DB ``Project`` the service needs."""

    id: str
    name: str
    description: str
    paths: list[str]


class ProjectDirectory(Protocol):
    """Main-store surface the service is allowed to see: project
    registration (PG2) plus the meta surface the epoch helpers use.
    ``save_project`` joined in PG-0.5 — the auto-indexer's marker-gated
    auto-registration writes through the same boundary (never raw SQL)."""

    def get_project(self, project_id: str) -> ProjectRecord | None: ...

    def get_project_by_name(self, name: str) -> ProjectRecord | None: ...

    def list_projects(self) -> list[ProjectRecord]: ...

    def save_project(self, project: Project) -> None: ...

    def get_meta(self, key: str) -> str | None: ...

    def set_meta(self, key: str, value: str) -> None: ...


@dataclass(frozen=True, slots=True)
class _RegisteredRoot:
    """A PG2-resolved project: the graph key (project name — the same
    slug every Vesma surface keys projects by) and the operator-
    registered index root."""

    graph_key: str
    root: str


def _clone_project_with_paths(project: ProjectRecord, paths: list[str]) -> Project:
    """Rebuild a project row with new ``paths`` (register/repoint).

    A real ``Project`` model is copied field-for-field (``created_at``
    preserved, ``updated_at`` bumped); a duck-typed record (test fakes)
    falls back to a fresh model over the protocol's four fields."""
    from datetime import UTC, datetime

    from vesmaro.models import Project as _Project

    if isinstance(project, _Project):
        return project.model_copy(update={"paths": paths, "updated_at": datetime.now(UTC)})
    return _Project(
        id=project.id,
        name=project.name,
        description=project.description or "",
        paths=paths,
    )


def resolve_token_budget(raw: Any) -> int:
    """Validate ``max_output_tokens`` (§3.4): int, 128..1M, default 3200.

    A bool is an int in Python but is still refused (a ``true`` budget
    is a caller bug, not 128 tokens)."""
    if raw is None:
        return DEFAULT_MAX_OUTPUT_TOKENS
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise GraphBudgetError(
            f"max_output_tokens must be an integer in "
            f"[{MIN_MAX_OUTPUT_TOKENS}, {MAX_MAX_OUTPUT_TOKENS}]"
        )
    if not MIN_MAX_OUTPUT_TOKENS <= raw <= MAX_MAX_OUTPUT_TOKENS:
        raise GraphBudgetError(
            f"max_output_tokens must be an integer in "
            f"[{MIN_MAX_OUTPUT_TOKENS}, {MAX_MAX_OUTPUT_TOKENS}], got {raw}"
        )
    return raw


def _row_bytes(row: dict[str, Any]) -> int:
    """Serialized size of one output row + its newline — the byte
    ceiling is per-ROW, and a row is NEVER split (whole-row drop)."""
    import json

    return len(json.dumps(row, ensure_ascii=False, default=str).encode("utf-8")) + 1


def window_rows(
    rows: list[dict[str, Any]],
    max_output_tokens: int,
    cursor: int,
) -> tuple[list[dict[str, Any]], bool, int]:
    """The token-contract window over RANKED output rows.

    Budget in bytes = tokens x 4 (deterministic ceiling); whole rows
    are kept in rank order until the budget is exhausted (ranked rows
    survive raw rows); ``has_more`` says whether rows remain; the
    returned cursor is the offset of the NEXT page and strictly
    advances because at least one row is always consumed — a budget
    that fits zero rows raises :class:`GraphBudgetError` (the
    self-loop refusal: never an empty answer with the same cursor).
    """
    budget = resolve_token_budget(max_output_tokens) * BYTES_PER_TOKEN
    if cursor < 0 or cursor >= len(rows):
        return [], False, cursor if cursor >= 0 else 0
    spent = 0
    kept: list[dict[str, Any]] = []
    for row in rows[cursor:]:
        cost = _row_bytes(row)
        if kept and spent + cost > budget:
            return kept, True, cursor + len(kept)
        if not kept and cost > budget:
            raise GraphBudgetError(
                f"max_output_tokens={max_output_tokens} cannot fit even one output row "
                f"(smallest row is {cost - 1} bytes = {cost // BYTES_PER_TOKEN + 1} tokens) — "
                "request a bigger budget"
            )
        kept.append(row)
        spent += cost
        if spent >= budget:
            break
    has_more = cursor + len(kept) < len(rows)
    next_cursor = cursor + len(kept) if has_more else cursor
    return kept, has_more, next_cursor


class CodeGraphService:
    """Owner of the project-graph operations (store + indexer + audit).

    Built over the MAIN store (project registration + meta) and the
    sidecar ``data_dir``; one instance per process per manager (the
    :func:`get_graph_service` registry below).
    """

    def __init__(
        self,
        main_store: ProjectDirectory,
        data_dir: Path | str,
        config: CodeGraphConfig | None = None,
    ) -> None:
        self._main = main_store
        self._config = config or CodeGraphConfig()
        self._store = CodeGraphStore(Path(data_dir))
        self._audit = GraphAudit(self._store.db_path)

    @property
    def store(self) -> CodeGraphStore:
        return self._store

    @property
    def config(self) -> CodeGraphConfig:
        return self._config

    @property
    def main(self) -> ProjectDirectory:
        """The main-store project directory (read surface for the PG-0.5
        auto-indexer's registration step — a separate accessor keeps
        ``_main`` private everywhere else)."""
        return self._main

    @property
    def audit(self) -> GraphAudit:
        """The PG7 audit writer (the auto-indexer's registration event
        is an audit-first-class action, ``auto-register``)."""
        return self._audit

    def close(self) -> None:
        self._store.close()
        self._audit.close()

    # ── guards (PG7 binding, flag gate) ────────────────────────────────────

    def _require_attribution(self, agent: Any, session: Any) -> tuple[str, str | None]:
        """PG7: a graph call WITHOUT an agent id is refused — the
        binding that makes multi-agent audit meaningful. ``session`` is
        optional but must be a string when present."""
        if not isinstance(agent, str) or not agent.strip():
            raise GraphAttributionError(
                "agent attribution is required (PG7): pass the caller's agent id"
            )
        if session is not None and (not isinstance(session, str) or not session.strip()):
            raise GraphAttributionError("session, when provided, must be a non-empty string")
        return agent.strip(), session.strip() if isinstance(session, str) else None

    def _ensure_enabled(self) -> None:
        if not self._config.enabled:
            raise GraphDisabledError(
                "project graph is disabled (settings.code_graph.enabled=false); "
                "the operator has disabled the graph (on by default since 2026-09-28)"
            )

    # ── PG2: registration resolution ────────────────────────────────────────

    def _resolve_root(self, project_id: Any) -> _RegisteredRoot:
        """Resolve a project THROUGH the main-DB projects table.

        Accepts the project id or its unique name — both are operator-
        registrations; anything else (unregistered slug, arbitrary
        path, empty string) is a confinement refusal. The index root is
        the FIRST registered path; an empty/invalid registration is a
        refusal, not a fallback. The graph key is the project NAME (the
        slug every other Vesma surface keys projects by; the node-id
        length-prefix makes it collision-safe)."""
        if not isinstance(project_id, str) or not project_id.strip():
            raise GraphConfinementError("project_id is required and must be a non-empty string")
        wanted = project_id.strip()
        project = self._main.get_project(wanted) or self._main.get_project_by_name(wanted)
        if project is None:
            raise GraphConfinementError(
                f"project {wanted!r} is not registered in the projects table "
                f"(PG2: the graph indexes only registered roots){REGISTER_HINT}"
            )
        registered = [p for p in (project.paths or []) if isinstance(p, str) and p.strip()]
        if not registered:
            raise GraphConfinementError(
                f"project {project.name!r} has no registered path to index "
                f"(PG2: register the root via the project's paths){REGISTER_HINT}"
            )
        root = registered[0]
        if not os.path.isabs(root):
            raise GraphConfinementError(
                f"project {project.name!r} registered path is not absolute: {root!r}"
            )
        if not os.path.isdir(root):
            raise GraphConfinementError(
                f"project {project.name!r} registered root is missing on disk: {root!r}"
            )
        return _RegisteredRoot(graph_key=project.name, root=root)

    def _confine_path(self, root: str, rel_path: Any) -> str:
        """Normalize a caller path into a repo-relative POSIX path that
        cannot escape the registered root (no absolute paths, no ``..``,
        no backslash smuggles). Returns the normalized RELATIVE path."""
        if not isinstance(rel_path, str) or not rel_path.strip():
            raise GraphConfinementError("path is required and must be a non-empty string")
        if "\\" in rel_path:
            raise GraphConfinementError("path must use POSIX separators")
        if rel_path.startswith("/") or os.path.isabs(rel_path):
            raise GraphConfinementError("path must be repo-relative (PG2)")
        candidate = os.path.normpath(os.path.join(root, rel_path))
        root_abs = os.path.abspath(root)
        if candidate != root_abs and not candidate.startswith(root_abs + os.sep):
            raise GraphConfinementError("path escapes the registered project root (PG2)")
        rel = os.path.relpath(candidate, root_abs).replace(os.sep, "/")
        if rel in (".", "") or rel.startswith("../"):
            raise GraphConfinementError("path escapes the registered project root (PG2)")
        return rel

    # ── PG2 root lookup (PG-0.5 fix-slice: one root = one graph) ────────────

    def find_project_by_root(self, root: str) -> ProjectRecord | None:
        """The ONE-root-ONE-graph lookup (PG-0.5 fix-slice, PR #443
        review P2-1): the registered project whose ``paths`` contain
        this absolute root, or ``None``. The projects table is small
        and operator-shaped, so a full scan is fine — but it lives in
        THIS one method (the auto path never grows per-hint blind SQL);
        path comparison is ``normpath``-normalized on both sides so a
        trailing-slash or ``.`` spelling still matches."""
        wanted = os.path.normpath(os.path.abspath(root))
        for project in self._main.list_projects():
            for registered in project.paths or []:
                if (
                    isinstance(registered, str)
                    and registered.strip()
                    and os.path.normpath(os.path.abspath(registered)) == wanted
                ):
                    return project
        return None

    # ── registration lifecycle (#450 repoint / #454 manual register) ─────

    def _validate_new_root(self, raw_root: Any, *, what: str) -> str:
        """Validate a caller-supplied absolute root for register/repoint.

        Confinement gates shared with the auto path (#454/#450): the
        root must be an existing directory, must look like a project
        root (a packaging manifest OR a ``.git`` — the repoint/git leg
        is the operator's explicit act, so a bare checkout qualifies;
        the auto path stays marker-only), and must never be ``$HOME`` or
        the filesystem root. Returns the normalized absolute path;
        refusals are loud (they name the failed gate)."""
        if not isinstance(raw_root, str) or not raw_root.strip():
            raise GraphConfinementError(f"{what} root is required and must be a non-empty string")
        root = os.path.abspath(os.path.expanduser(raw_root.strip()))
        if not os.path.isabs(raw_root.strip()):
            raise GraphConfinementError(
                f"{what} root must be an absolute path (PG2), got {raw_root!r}"
            )
        if _is_forbidden_root(root):
            raise GraphConfinementError(
                f"{what} root {root!r} is $HOME or the filesystem root — refused"
            )
        if not os.path.isdir(root):
            raise GraphConfinementError(
                f"{what} root does not exist on disk: {root!r} "
                "(move the tree first, then repoint/register)"
            )
        marker = project_marker(root)
        if marker is None and not os.path.isdir(os.path.join(root, ".git")):
            raise GraphConfinementError(
                f"{what} root {root!r} has no packaging manifest and no .git — "
                "refusing to point the graph at an unrelated directory"
            )
        return root

    def register_project(
        self,
        project_id: str,
        root: str,
        *,
        agent: str,
        session: str | None = None,
    ) -> dict[str, Any]:
        """Register a project root WITHOUT the operator's Python REPL
        (#454): the agent-facing path for «graph tools answer not
        registered».

        Confinement: same gates as the auto path (existing dir, marker
        or ``.git``, not ``$HOME``/fs-root) plus the one-root-one-graph
        rule — a root already registered under another name is REUSED
        (audit ``manual-register-reused``, the ``auto-register-reused``
        precedent), never duplicated. A project NAME that already
        exists under a DIFFERENT root is a loud refusal (the existing
        registration wins; ``vesma graph repoint`` is the operator's
        tool for moved roots, #450). An existing project with NO paths
        (the common case: auto-created by memory writes) gets the root
        attached. Explicit registration does NOT count against
        ``auto_register_max_projects`` — that cap bounds the AUTO path
        only (provenance lives in the description marker the cap
        counts, which manual rows never carry)."""
        self._ensure_enabled()
        actor, sess = self._require_attribution(agent, session)
        if not isinstance(project_id, str) or not project_id.strip():
            raise GraphConfinementError("project_id is required and must be a non-empty string")
        name = project_id.strip()
        new_root = self._validate_new_root(root, what="registration")
        existing_by_root = self.find_project_by_root(new_root)
        if existing_by_root is not None and existing_by_root.name != name:
            # One root = one graph: the hint rides the existing project.
            self._audit.record(
                existing_by_root.name,
                "manual-register-reused",
                actor,
                session=sess,
                reason="manual-register-reused (root already registered)",
                details={"hint": name, "root": os.path.basename(new_root)},
            )
            return {
                "project": existing_by_root.name,
                "status": "already-registered",
                "root": new_root,
                "note": f"root already registered under project {existing_by_root.name!r}",
            }
        project = self._main.get_project(name) or self._main.get_project_by_name(name)
        if project is not None:
            current = [p for p in (project.paths or []) if isinstance(p, str) and p.strip()]
            if current and os.path.normpath(os.path.abspath(current[0])) == os.path.normpath(
                new_root
            ):
                self._audit.record(
                    project.name,
                    "manual-register-reused",
                    actor,
                    session=sess,
                    reason="manual-register-reused (same root)",
                    details={"root": os.path.basename(new_root)},
                )
                return {"project": project.name, "status": "already-registered", "root": new_root}
            if current:
                raise GraphConfinementError(
                    f"project {project.name!r} is already registered at "
                    f"{current[0]!r} — refusing to re-point an existing root "
                    f"from the register tool (moved roots: vesma graph repoint)"
                )
            # The orphan case: a project row created by memory writes,
            # no paths — attach the root.
            updated = _clone_project_with_paths(project, [new_root])
            self._main.save_project(updated)
            self._audit.record(
                updated.name,
                "manual-register",
                actor,
                session=sess,
                reason="manual-register (root attached to existing project)",
                details={"root": os.path.basename(new_root)},
            )
            return {"project": updated.name, "status": "registered", "root": new_root}
        from datetime import UTC, datetime

        from vesmaro.models import Project as _NewProject

        created = _NewProject(
            name=name,
            paths=[new_root],
            description=(
                f"manually registered by {actor} at {datetime.now(UTC).isoformat()} "
                "(mnemos_register_project; PG2)"
            ),
        )
        self._main.save_project(created)
        self._audit.record(
            created.name,
            "manual-register",
            actor,
            session=sess,
            reason="manual-register",
            details={"root": os.path.basename(new_root)},
        )
        logger.info("codegraph: project %s manually registered at %s by %s", name, new_root, actor)
        return {"project": created.name, "status": "registered", "root": new_root}

    def repoint_project(
        self,
        project_id: str,
        new_root: str,
        *,
        agent: str,
        session: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Re-point a GHOST registration at its moved root (#450).

        A registration whose root no longer exists on disk is stuck
        (auto-index no-ops, explicit index refuses confinement, the only
        exit was nuclear delete). This method resolves the project BY
        NAME/ID — deliberately NOT through ``_resolve_root``, which
        refuses exactly the missing-root state being repaired — validates
        the new root through the shared confinement gates, rewrites
        ``paths[0]`` and purges the stale index: the sidecar describes
        the OLD tree, so it is dropped (derived, rebuildable data) and
        the next index run rebuilds fresh. The one-root-one-graph rule
        binds: a new root already claimed by ANOTHER project is a loud
        refusal. Audit action ``repoint`` (the reason defaults to
        ``graph-repoint``); the sidecar row carries BASENAMES only
        (review 10173a2a-4), the response keeps full paths."""
        self._ensure_enabled()
        actor, sess = self._require_attribution(agent, session)
        if not isinstance(project_id, str) or not project_id.strip():
            raise GraphConfinementError("project_id is required and must be a non-empty string")
        wanted = project_id.strip()
        project = self._main.get_project(wanted) or self._main.get_project_by_name(wanted)
        if project is None:
            raise GraphConfinementError(
                f"project {wanted!r} is not registered in the projects table (PG2){REGISTER_HINT}"
            )
        root = self._validate_new_root(new_root, what="repoint")
        claimed = self.find_project_by_root(root)
        if claimed is not None and claimed.name != project.name:
            raise GraphConfinementError(
                f"root {root!r} is already registered under project {claimed.name!r} "
                "(one root = one graph; refusing the cross-jump)"
            )
        old = [p for p in (project.paths or []) if isinstance(p, str) and p.strip()]
        if old and os.path.normpath(os.path.abspath(old[0])) == os.path.normpath(root):
            return {
                "project": project.name,
                "status": "unchanged",
                "root": root,
                "note": "the registration already points at this root",
            }
        paths = [root, *old[1:]] if old else [root]
        updated = _clone_project_with_paths(project, paths)
        self._main.save_project(updated)
        key = updated.name
        purged = self._store.purge_project(key)
        # Fresh start, the delete-tool semantics: suspension lifted,
        # consumers see a new epoch (the index they cached is gone).
        self._store.set_meta(auto_suspended_key(key), "0")
        bump_project_graph_epoch(self._main, key)
        self._audit.record(
            key,
            "repoint",
            actor,
            session=sess,
            reason=reason or "graph-repoint",
            details={
                "old_root": os.path.basename(old[0]) if old else None,
                "new_root": os.path.basename(root),
                "purged_nodes": purged,
            },
        )
        logger.info(
            "codegraph: project %s re-pointed %s -> %s by %s (%d stale nodes purged)",
            key,
            old[0] if old else "<none>",
            root,
            actor,
            purged,
        )
        return {"project": key, "status": "repointed", "root": root, "purged_nodes": purged}

    # ── tool 1: index_project ───────────────────────────────────────────────
    def index_project(
        self,
        project_id: str,
        *,
        agent: str,
        session: str | None = None,
        incremental: bool = True,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Full or incremental indexation of a REGISTERED project root.

        Serialized per project (a concurrent call gets ``in-progress``
        immediately, contract §3.2 (c)); a PG7 limit breach aborts the
        whole index (fail-closed, the previous graph survives). The
        audit event lands only when an actual (re)publish happened."""
        self._ensure_enabled()
        actor, sess = self._require_attribution(agent, session)
        registered = self._resolve_root(project_id)
        pre_files = self._store.count_files(registered.graph_key)
        try:
            result = incremental_mod.index_project(
                registered.graph_key,
                registered.root,
                self._store,
                self._main,  # MainStoreMeta ⊂ ProjectDirectory (structural)
                self._config,
                incremental=incremental,
            )
        except IndexLimitError as exc:
            # A limit breach is a REFUSAL, not a publish — the previous
            # graph survives; audit the attempt as a failed index. The
            # audit reason carries the root BASENAME only (review
            # 10173a2a-4): the sidecar trail leaves the process, so an
            # absolute path must not ride it; the RAISED message keeps
            # the full root — that one stays in the process log.
            audit_reason = str(exc).replace(registered.root, os.path.basename(registered.root))
            self._audit.record(
                registered.graph_key,
                "index" if pre_files == 0 else "reindex",
                actor,
                session=sess,
                reason=audit_reason,
                details={
                    "outcome": "limit-refused",
                    "trigger": reason,
                    "root": os.path.basename(registered.root),
                },
            )
            raise
        payload = self._index_payload(result)
        # Issue #449: the allowlist un-poison is EXPLICIT — every
        # non-empty removal gets its own audit row (reason
        # ``allowlist-unpoison``) on every path that reaches here,
        # fresh runs included: config-only allowlist changes must show
        # in the sidecar trail, never silently shrink the set.
        if result.unpoisoned:
            self._audit.record(
                registered.graph_key,
                "reindex" if pre_files else "index",
                actor,
                session=sess,
                reason="allowlist-unpoison",
                details={"paths": sorted(result.unpoisoned)},
            )
        no_change = result.status in (
            incremental_mod.STATUS_FRESH,
            incremental_mod.STATUS_IN_PROGRESS,
        )
        if no_change:
            payload["staleness"] = None  # nothing changed; no fake freshness
            return payload
        self._audit.record(
            registered.graph_key,
            "index" if pre_files == 0 else "reindex",
            actor,
            session=sess,
            reason=reason,
            details={
                "nodes": result.nodes,
                "edges": result.edges,
                "files": result.files_indexed,
                "poisoned": len(result.poisoned),
                "parse_errors": len(result.parse_errors),
                "incremental": result.incremental,
            },
        )
        # A successful publish lifts the auto-path suspension (PG-0.5
        # fix-slice): manual runs and the watch poll both ride this
        # method, and an auto-stale run implies a working tree anyway.
        self._store.set_meta(auto_suspended_key(registered.graph_key), "0")
        payload["staleness"] = self._staleness_payload(registered.graph_key, registered.root)
        return payload

    @staticmethod
    def _index_payload(result: IndexResult) -> dict[str, Any]:
        return {
            "status": result.status,
            "nodes": result.nodes,
            "edges": result.edges,
            "files_indexed": result.files_indexed,
            "files_skipped": result.files_skipped,
            "poisoned": sorted(result.poisoned),
            # Issue #449: paths un-poisoned by THIS run's allowlist pass
            # (audited with reason ``allowlist-unpoison``).
            "unpoisoned": sorted(result.unpoisoned),
            "parse_errors": dict(sorted(result.parse_errors.items())),
            "duration_sec": round(result.duration, 3),
            "incremental": result.incremental,
        }

    # ── freshness (§3.2 trigger (c): cheap, read-only) ──────────────────────

    def _staleness_payload(self, graph_key: str, root: str) -> dict[str, Any]:
        report = incremental_mod.staleness_check(graph_key, root, self._store)
        return {
            "total_files": report.total_files,
            "fresh_percent": report.fresh_percent,
            "changed_files": report.changed_files,
            "last_indexed_at": report.last_indexed_at,
        }

    # ── slice-6 wiring: watch registrar probe + the recall beacon (§3.5) ────

    def watch_probe(self, project_id: str) -> tuple[str, str]:
        """PG2 resolution for the watch registrar (slice 6): the graph
        key + the registered root, refusing unregistered projects. The
        poll itself re-resolves through ``index_project`` on every
        actual run — a project deregistered mid-watch fails there and
        the scheduler drops the dead registration."""
        registered = self._resolve_root(project_id)
        return registered.graph_key, registered.root

    def beacon_line(self, project_id: str) -> str | None:
        """The recall beacon v1 (ADR-0032 §3.5): ONE line describing
        the project graph's freshness, or ``None`` when it must not
        show (master/beacon flag off, unregistered project, no index).

        The numbers are the CHEAP classification (mtime+size against
        ``graph_files`` — zero byte reads, no sha256 anywhere on this
        path: the session-start latency contract). The line never
        raises — the beacon is a guest in the assembly pipeline and
        degrades to absence on any failure.
        """
        try:
            if not self._config.enabled or not self._config.beacon:
                return None
            try:
                registered = self._resolve_root(project_id)
            except GraphToolError:
                return None  # unregistered/unresolvable project — silent skip
            key = registered.graph_key
            if self._store.count_files(key) == 0:
                return None  # no index yet — nothing to advertise
            report = incremental_mod.staleness_check(key, registered.root, self._store)
            stale = len(report.changed_files)
            total = report.total_files
            fresh = max(total - stale, 0)
            poisoned = len(self._store.get_poisoned_paths(key))
            stamp = (report.last_indexed_at or "unknown")[:19]
            tail = (
                f" indexed {stamp}, {fresh}/{total} files fresh "
                f"({stale} stale, {poisoned} poisoned) — call mnemos_search_graph"
            )
            return _fit_beacon_line(key, tail)
        except Exception:
            logger.debug("codegraph: beacon_line skipped (service error)", exc_info=True)
            return None

    # ── tool 2: project_graph_status ────────────────────────────────────────

    def status(self, project_id: str, *, agent: str, session: str | None = None) -> dict[str, Any]:
        """Volumes, freshness, parse failures and the poisoned count."""
        self._ensure_enabled()
        actor, sess = self._require_attribution(agent, session)
        registered = self._resolve_root(project_id)
        key = registered.graph_key
        failures = self._store.get_parse_failures(key)
        payload = {
            "project": key,
            "nodes": self._store.count_nodes(key),
            "edges": self._store.count_edges(key),
            "files": self._store.count_files(key),
            "parse_errors": failures,
            "parse_error_count": len(failures),
            "poisoned_count": len(self._store.get_poisoned_paths(key)),
            "staleness": self._staleness_payload(key, registered.root),
        }
        self._audit.record(
            key,
            "graph-read",
            actor,
            session=sess,
            reason="status",
            details={"files": payload["files"], "nodes": payload["nodes"]},
        )
        return payload

    # ── tool 3: search_graph ────────────────────────────────────────────────

    def search_graph(
        self,
        project_id: str,
        query: str,
        *,
        agent: str,
        session: str | None = None,
        kind: str | None = None,
        limit: int = 50,
        cursor: int = 0,
        max_output_tokens: Any = None,
        include_signature: bool = False,
    ) -> dict[str, Any]:
        """Substring search over name/qname/path; ranked rows under the
        token contract (exact name/qname hits outrank prefix hits,
        prefix outranks substring — ranking BEFORE the budget cut).

        ``total_matches`` is the HONEST count of the predicate over the
        whole graph (``CodeGraphStore.count_search_nodes``); the cursor
        pages the top-``limit`` ranked slice (review 10173a2a-5)."""
        self._ensure_enabled()
        actor, sess = self._require_attribution(agent, session)
        registered = self._resolve_root(project_id)
        key = registered.graph_key
        if not isinstance(query, str) or not query.strip():
            raise GraphToolError("query is required and must be a non-empty string")
        if kind is not None and kind not in NODE_KINDS:
            raise GraphToolError(f"kind must be one of: {', '.join(NODE_KINDS)}")
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            raise GraphToolError("cursor must be a non-negative integer")
        total = self._store.count_search_nodes(key, query, kind=kind)
        matches = self._store.search_nodes(
            key, query, kind=kind, limit=min(max(int(limit), 1), SEARCH_ROW_CAP) * 2
        )
        rows = [self._ranked_row(asdict(n), query) for n in matches]
        rows.sort(key=lambda r: (-r.pop("score"), str(r["qname"])))
        rows = rows[: max(int(limit), 1)]
        # Detail flags are OPT-IN (§3.4): signatures ride only when asked.
        if not include_signature:
            for row in rows:
                row.pop("signature", None)
        page, has_more, next_cursor = window_rows(rows, max_output_tokens, cursor)
        self._audit.record(
            key,
            "graph-read",
            actor,
            session=sess,
            reason="search",
            details={"matches": len(rows), "returned": len(page), "total": total},
        )
        return {
            "project": key,
            "query_kind": kind,
            "results": page,
            "total_matches": total,
            "cursor": next_cursor,
            "has_more": has_more,
            "last_indexed_at": self._store.get_meta(self._last_indexed_key(key)),
        }

    @staticmethod
    def _ranked_row(node_dict: dict[str, Any], query: str) -> dict[str, Any]:
        q = query.strip()
        name = str(node_dict.get("name") or "")
        qname = str(node_dict.get("qname") or "")
        if qname == q or name == q:
            score = 3
        elif qname.startswith(q) or qname.endswith("." + q) or name.startswith(q):
            score = 2
        else:
            score = 1
        return {"score": score, **node_dict}

    @staticmethod
    def _last_indexed_key(project: str) -> str:
        return f"last_indexed:{len(project)}:{project}"

    # ── tool 4: trace_path (ADR-0030 walk pattern) ──────────────────────────

    def trace_path(
        self,
        project_id: str,
        qname: str,
        *,
        agent: str,
        session: str | None = None,
        depth: int = 2,
        max_output_tokens: Any = None,
    ) -> dict[str, Any]:
        """BFS over outgoing project_edges from a symbol — depth ≤ 2,
        per-node fanout cap, total-work cap (the ADR-0030 discipline)."""
        self._ensure_enabled()
        actor, sess = self._require_attribution(agent, session)
        registered = self._resolve_root(project_id)
        key = registered.graph_key
        if not isinstance(qname, str) or not qname.strip():
            raise GraphToolError("qname is required and must be a non-empty string")
        if (
            not isinstance(depth, int)
            or isinstance(depth, bool)
            or not 1 <= depth <= TRACE_MAX_DEPTH
        ):
            raise GraphToolError(f"depth must be an integer in [1, {TRACE_MAX_DEPTH}]")
        start = self._resolve_symbol(key, qname.strip())
        visited: dict[str, dict[str, Any]] = {
            start.id: self._trace_node(start, 0),
        }
        edges_out: list[dict[str, Any]] = []
        truncated = False
        frontier = [start.id]
        for level in range(1, depth + 1):
            if not frontier or len(visited) >= TRACE_TOTAL_WORK_CAP:
                truncated = truncated or bool(frontier)
                break
            next_frontier: list[str] = []
            for node_id in frontier:
                if len(visited) >= TRACE_TOTAL_WORK_CAP:
                    truncated = True
                    break
                fanout = self._store.get_edges(key, from_id=node_id, limit=TRACE_FANOUT_CAP + 1)
                if len(fanout) > TRACE_FANOUT_CAP:
                    fanout = fanout[:TRACE_FANOUT_CAP]
                    truncated = True
                for edge in fanout:
                    edges_out.append(
                        {
                            "from": edge.from_id,
                            "to": edge.to_id,
                            "kind": edge.kind,
                            "provenance": edge.provenance,
                        }
                    )
                    if edge.to_id in visited:
                        continue
                    target = self._store.get_node(edge.to_id)
                    if target is None:  # cascade promise not yet materialized
                        continue
                    visited[edge.to_id] = self._trace_node(target, level)
                    next_frontier.append(edge.to_id)
            frontier = next_frontier
        rows = sorted(visited.values(), key=lambda r: (r["depth"], str(r["qname"])))
        page, has_more, next_cursor = window_rows(rows, max_output_tokens, 0)
        self._audit.record(
            key,
            "graph-read",
            actor,
            session=sess,
            reason="trace",
            details={"start": start.qname, "visited": len(rows), "edges": len(edges_out)},
        )
        return {
            "project": key,
            "start": start.qname,
            "depth": depth,
            "nodes": page,
            "edges": edges_out,
            "truncated": truncated,
            "cursor": next_cursor,
            "has_more": has_more,
            "last_indexed_at": self._store.get_meta(self._last_indexed_key(key)),
        }

    def _resolve_symbol(self, project: str, qname: str) -> Any:
        """Exact-qname lookup first, then a UNIQUE dotted-tail match;
        ambiguous or missing symbols refuse (never a silent guess)."""
        exact = self._store.get_nodes(project, qname=qname, limit=1)
        if exact:
            return exact[0]
        tail = qname.rsplit(".", 1)[-1]
        candidates = self._store.search_nodes(project, qname, limit=2)
        if len(candidates) == 1:
            return candidates[0]
        tail_hits = [n for n in candidates if n.qname.rsplit(".", 1)[-1] == tail]
        if len(tail_hits) == 1:
            return tail_hits[0]
        raise GraphToolError(
            f"symbol {qname!r} not found or ambiguous in the project graph — "
            "use search_graph to locate the exact qname"
        )

    @staticmethod
    def _trace_node(node: Any, depth: int) -> dict[str, Any]:
        return {
            "id": node.id,
            "qname": node.qname,
            "kind": node.kind,
            "path": node.path,
            "start_line": node.start_line,
            "end_line": node.end_line,
            "depth": depth,
        }

    # ── tool 5: get_file_outline ────────────────────────────────────────────

    def get_file_outline(
        self,
        project_id: str,
        path: str,
        *,
        agent: str,
        session: str | None = None,
        max_output_tokens: Any = None,
        cursor: int = 0,
    ) -> dict[str, Any]:
        """Symbol tree of one indexed file (PG1: shapes, never bodies)."""
        self._ensure_enabled()
        actor, sess = self._require_attribution(agent, session)
        registered = self._resolve_root(project_id)
        key = registered.graph_key
        rel = self._confine_path(registered.root, path)
        record = self._record_or_refuse(key, rel)
        nodes = self._store.get_nodes(key, path=rel, limit=SEARCH_ROW_CAP)
        rows = [
            {
                "kind": n.kind,
                "name": n.name,
                "qname": n.qname,
                "start_line": n.start_line,
                "end_line": n.end_line,
                "signature": n.signature,
            }
            for n in nodes
            if n.kind != "Project"
        ]
        rows.sort(key=lambda r: (r["start_line"] is None, r["start_line"], str(r["qname"])))
        page, has_more, next_cursor = window_rows(rows, max_output_tokens, cursor)
        self._audit.record(
            key,
            "graph-read",
            actor,
            session=sess,
            reason="outline",
            details={"path": rel, "symbols": len(rows)},
        )
        return {
            "project": key,
            "path": rel,
            "lang": nodes[1].lang if len(nodes) > 1 else None,
            "outline": page,
            "parse_error": record.parse_error,  # «clean ≠ proof» honesty marker
            "cursor": next_cursor,
            "has_more": has_more,
            "last_indexed_at": record.indexed_at,
        }

    def _record_or_refuse(self, project: str, rel: str) -> Any:
        """The graph_files record of one path, or an unindexed refusal."""
        for rec in self._store.get_file_records(project):
            if rec.path == rel:
                return rec
        raise GraphToolError(
            f"path {rel!r} is not indexed for this project — call mnemos_index_project first"
        )

    # ── tool 6: get_code_snippet (PG4) ──────────────────────────────────────

    def get_code_snippet(
        self,
        project_id: str,
        path: str,
        start_line: int,
        end_line: int,
        *,
        agent: str,
        session: str | None = None,
        max_output_tokens: Any = None,
    ) -> dict[str, Any]:
        """Read a line range FROM DISK with the full PG4 sequence:
        poisoned refusal (permanent) → confinement → indexed check →
        mtime/size + sha256 freshness → issuance secret scan (ANY
        finding refuses fail-closed with the reason) → whole-line token
        window. There is NO snippet cache — every call re-reads."""
        self._ensure_enabled()
        actor, sess = self._require_attribution(agent, session)
        registered = self._resolve_root(project_id)
        key = registered.graph_key
        rel = self._confine_path(registered.root, path)
        if rel in self._store.get_poisoned_paths(key):
            # «навсегда»: refuses even when a reindex scan now misses.
            self._audit.record(
                key,
                "snippet-read",
                actor,
                session=sess,
                reason="poisoned-refusal",
                details={"path": rel},
            )
            raise GraphToolError(
                f"path {rel!r} is POISONED (hit the secrets detector at index time); "
                "snippet issuance is refused permanently (PG3) — only "
                "mnemos_delete_graph_project clears it"
            )
        record = self._record_or_refuse(key, rel)
        for name, value in (("start_line", start_line), ("end_line", end_line)):
            if not isinstance(value, int) or isinstance(value, bool):
                raise GraphToolError(f"{name} must be an integer")
        if start_line < 1 or end_line < start_line:
            raise GraphToolError("require 1 <= start_line <= end_line")
        abs_path = Path(registered.root) / rel
        try:
            stat = os.stat(abs_path)
        except OSError as exc:
            raise GraphToolError(
                f"path {rel!r} vanished from disk since indexation (stale graph — reindex needed)"
            ) from exc
        freshness_key = (record.mtime, record.size)
        if None not in freshness_key and (stat.st_mtime, stat.st_size) != freshness_key:
            raise GraphToolError(
                f"path {rel!r} CHANGED on disk since indexation — staleness marker, "
                "no content issued; reindex to refresh (PG4)"
            )
        try:
            source = abs_path.read_bytes()
        except OSError as exc:
            raise GraphToolError(
                f"path {rel!r} is unreadable since indexation — staleness marker, "
                "no content issued; reindex to refresh (PG4)"
            ) from exc
        import hashlib

        if record.hash is not None and hashlib.sha256(source).hexdigest() != record.hash:
            raise GraphToolError(
                f"path {rel!r} content hash diverges from the indexed record — "
                "staleness marker, no content issued; reindex to refresh (PG4)"
            )
        lines = source.decode("utf-8", "replace").splitlines()
        clamped_end = min(end_line, len(lines))
        if start_line > len(lines):
            raise GraphToolError(
                f"range [{start_line}, {end_line}] is beyond the file ({len(lines)} lines)"
            )
        requested = lines[start_line - 1 : clamped_end]
        findings = detect_secrets("\n".join(requested))
        if findings:
            patterns = findings_by_pattern(findings)
            self._audit.record(
                key,
                "snippet-read",
                actor,
                session=sess,
                reason="secret-refusal",
                details={"path": rel, "patterns": patterns},
            )
            raise GraphToolError(
                f"snippet of {rel!r} refused fail-closed (PG4): secret pattern(s) "
                f"{sorted(patterns)} hit in the requested range; nothing issued"
            )
        rows = [{"line": start_line + i, "text": text} for i, text in enumerate(requested)]
        page, has_more, _ = window_rows(rows, max_output_tokens, 0)
        first_kept = page[0]["line"] if page else start_line
        last_kept = page[-1]["line"] if page else start_line - 1
        self._audit.record(
            key,
            "snippet-read",
            actor,
            session=sess,
            reason="issued",
            details={"path": rel, "lines": f"{first_kept}-{last_kept}"},
        )
        return {
            "project": key,
            "path": rel,
            "start_line": first_kept,
            "end_line": last_kept,
            "content": "\n".join(str(r["text"]) for r in page),
            "total_file_lines": len(lines),
            "has_more": has_more,
            "next_start_line": last_kept + 1 if has_more else None,
            "scanned": True,
            "stale": False,
        }

    # ── tool 7: check_graph_coverage ────────────────────────────────────────

    def check_coverage(
        self,
        project_id: str,
        paths: list[str],
        *,
        agent: str,
        session: str | None = None,
    ) -> dict[str, Any]:
        """Per-path verdicts: indexed / stale / parse-error / unindexed /
        missing (coverage honesty — «clean ≠ proof» stays visible).

        ``missing`` (#452b): the path does not exist under the project
        root — its own verdict, distinct from ``unindexed`` (a REAL
        file the index has not covered; before, both answered
        ``unindexed`` and a typo read as an index gap).
        """
        self._ensure_enabled()
        actor, sess = self._require_attribution(agent, session)
        registered = self._resolve_root(project_id)
        key = registered.graph_key
        if not isinstance(paths, list) or not paths:
            raise GraphToolError("paths must be a non-empty list of repo-relative paths")
        records = {rec.path: rec for rec in self._store.get_file_records(key)}
        poisoned = self._store.get_poisoned_paths(key)
        verdicts: list[dict[str, Any]] = []
        for raw in paths:
            rel = self._confine_path(registered.root, raw)
            rec = records.get(rel)
            verdict: dict[str, Any] = {"path": rel}
            if not os.path.exists(Path(registered.root) / rel):
                verdict["verdict"] = "missing"
                verdict["reason"] = "path does not exist under the project root"
            elif rel in poisoned:
                verdict["verdict"] = "poisoned"
                verdict["reason"] = "secret-detected (permanent)"
            elif rec is None:
                verdict["verdict"] = "unindexed"
            else:
                abs_path = Path(registered.root) / rel
                try:
                    stat = os.stat(abs_path)
                    drifted = (
                        rec.mtime is not None
                        and rec.size is not None
                        and (stat.st_mtime, stat.st_size) != (rec.mtime, rec.size)
                    )
                except OSError:
                    drifted = True
                if drifted:
                    verdict["verdict"] = "stale"
                elif rec.parse_ok == 0:
                    verdict["verdict"] = "parse-error"
                    verdict["reason"] = rec.parse_error
                else:
                    verdict["verdict"] = "indexed"
            verdicts.append(verdict)
        self._audit.record(
            key,
            "graph-read",
            actor,
            session=sess,
            reason="coverage",
            details={"paths": len(verdicts)},
        )
        return {"project": key, "coverage": verdicts}

    # ── tool 8: get_graph_schema ────────────────────────────────────────────

    def get_graph_schema(
        self,
        project_id: str | None = None,
        *,
        agent: str,
        session: str | None = None,
    ) -> dict[str, Any]:
        """Node/edge kinds, limits and schema version — the agent-facing
        contract card. Optional project adds its volumes."""
        self._ensure_enabled()
        actor, sess = self._require_attribution(agent, session)
        payload: dict[str, Any] = {
            "schema_version": GRAPH_SCHEMA_VERSION,
            "node_kinds": list(NODE_KINDS),
            "edge_kinds": list(EDGE_KINDS),
            "token_contract": {
                "max_output_tokens_default": DEFAULT_MAX_OUTPUT_TOKENS,
                "max_output_tokens_min": MIN_MAX_OUTPUT_TOKENS,
                "max_output_tokens_max": MAX_MAX_OUTPUT_TOKENS,
                "bytes_per_token": BYTES_PER_TOKEN,
            },
            "limits": {
                "index_max_files": self._config.index_max_files,
                "index_max_source_mb": self._config.index_max_source_mb,
            },
            "trace": {
                "max_depth": TRACE_MAX_DEPTH,
                "fanout_cap": TRACE_FANOUT_CAP,
                "total_work_cap": TRACE_TOTAL_WORK_CAP,
            },
        }
        if project_id is not None:
            registered = self._resolve_root(project_id)
            key = registered.graph_key
            payload["project"] = key
            payload["volumes"] = {
                "nodes": self._store.count_nodes(key),
                "edges": self._store.count_edges(key),
                "files": self._store.count_files(key),
            }
        self._audit.record(
            "<none>" if project_id is None else payload.get("project", "<none>"),
            "graph-read",
            actor,
            session=sess,
            reason="schema",
            details={},
        )
        return payload

    # ── tool 9: list_graph_projects ─────────────────────────────────────────

    def list_graph_projects(self, *, agent: str, session: str | None = None) -> dict[str, Any]:
        """Registered projects (main DB) JOIN their index status
        (sidecar) — registered-but-never-indexed stays visible, and a
        registration whose root is MISSING on disk is marked
        ``root_missing`` (#450: ghosts must be visible, not silent)."""
        self._ensure_enabled()
        actor, sess = self._require_attribution(agent, session)
        indexed = {entry["project"]: entry for entry in self._store.list_graph_projects()}
        rows: list[dict[str, Any]] = []
        for project in self._main.list_projects():
            entry = indexed.pop(project.name, None)
            registered_paths = [
                p for p in (project.paths or []) if isinstance(p, str) and p.strip()
            ]
            root_missing = bool(registered_paths) and not os.path.isdir(registered_paths[0])
            rows.append(
                {
                    "project": project.name,
                    "registered": True,
                    "has_root": bool(registered_paths),
                    "root_missing": root_missing,
                    "nodes": entry["nodes"] if entry else 0,
                    "edges": entry["edges"] if entry else 0,
                    "files": entry["files"] if entry else 0,
                    "poisoned": entry["poisoned"] if entry else 0,
                    "last_indexed_at": entry["last_indexed_at"] if entry else None,
                }
            )
        for name, entry in sorted(indexed.items()):  # indexed orphan (project deregistered)
            rows.append(
                {
                    "project": name,
                    "registered": False,
                    "has_root": False,
                    "root_missing": False,
                    "nodes": entry["nodes"],
                    "edges": entry["edges"],
                    "files": entry["files"],
                    "poisoned": entry["poisoned"],
                    "last_indexed_at": entry["last_indexed_at"],
                }
            )
        page, has_more, next_cursor = window_rows(rows, MAX_MAX_OUTPUT_TOKENS, 0)
        self._audit.record(
            "<none>",
            "graph-read",
            actor,
            session=sess,
            reason="list-projects",
            details={"projects": len(rows)},
        )
        return {"projects": page, "has_more": has_more, "cursor": next_cursor}

    # ── tool 10: delete_graph_project ───────────────────────────────────────

    def delete_graph_project(
        self,
        project_id: str,
        *,
        agent: str,
        session: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Drop the INDEX (sidecar subtree + poisoned set + freshness
        stamp) — never the project entity in the main DB. Audit first-
        class event."""
        self._ensure_enabled()
        actor, sess = self._require_attribution(agent, session)
        registered = self._resolve_root(project_id)
        key = registered.graph_key
        deleted = self._store.purge_project(key)
        # The purge clears only the poisoned set + last_indexed stamp;
        # the auto-path suspension flag is this operation's to lift too
        # (fresh start — the next hint may auto-index from scratch).
        self._store.set_meta(auto_suspended_key(key), "0")
        bump_project_graph_epoch(self._main, key)
        self._audit.record(
            key,
            "delete",
            actor,
            session=sess,
            reason=reason,
            details={"deleted_nodes": deleted},
        )
        logger.info("codegraph: project graph %s deleted (%d nodes) by %s", key, deleted, actor)
        return {"project": key, "deleted_nodes": deleted, "status": "deleted"}


#: Beacon v1 (§3.5) budget discipline: the whole line is capped at 200
#: UTF-8 bytes and lives OUTSIDE the assembly token budget entirely.
BEACON_LINE_MAX_BYTES = 200
BEACON_LINE_PREFIX = "project-graph: "
BEACON_CALL_HINT = "call mnemos_search_graph"


def _fit_beacon_line(project: str, tail: str) -> str:
    """Assemble the beacon line under the 200-byte cap: the fixed
    prefix, the call hint and the freshness tail are load-bearing and
    never truncated — overflow eats the PROJECT SLUG (byte-safe, never
    mid-codepoint)."""
    room = (
        BEACON_LINE_MAX_BYTES - len(BEACON_LINE_PREFIX.encode("utf-8")) - len(tail.encode("utf-8"))
    )
    if room < 1:
        # Degenerate only with an absurd tail (numbers cannot reach it):
        # drop the slug entirely rather than break the byte cap.
        return (
            (BEACON_LINE_PREFIX + tail.lstrip())
            .encode("utf-8")[:BEACON_LINE_MAX_BYTES]
            .decode("utf-8", "ignore")
        )
    name = project.encode("utf-8")[:room].decode("utf-8", "ignore")
    return f"{BEACON_LINE_PREFIX}{name}{tail}"


# ── per-manager registry (MCP/REST access; manager wiring is slice 6) ─────────


_SERVICE_REGISTRY: weakref.WeakKeyDictionary[Any, CodeGraphService] = weakref.WeakKeyDictionary()


def get_graph_service(manager: Any) -> CodeGraphService:
    """The service singleton for a manager (weak-keyed: a discarded
    manager takes its sidecar connections with it)."""
    service = _SERVICE_REGISTRY.get(manager)
    if service is None:
        service = CodeGraphService(
            main_store=manager.sqlite,
            data_dir=manager.settings.mnemos.data_dir,
            config=manager.settings.code_graph,
        )
        _SERVICE_REGISTRY[manager] = service
    return service


def close_graph_service(manager: Any) -> bool:
    """Close the manager's graph service IF one was built — never builds
    one (unlike :func:`get_graph_service`), so it is safe on every
    ``MemoryManager.close()`` path.

    Returns whether a service existed and was closed. The weak-keyed
    registry drops the entry too: a later ``get_graph_service`` on the
    same manager builds a FRESH service instead of handing out a closed
    one. ``CodeGraphService.close`` itself is idempotent (store and
    audit tolerates a second call) — review 10173a2a-2."""
    service = _SERVICE_REGISTRY.pop(manager, None)
    if service is None:
        return False
    service.close()
    return True
