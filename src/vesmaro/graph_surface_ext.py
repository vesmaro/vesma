"""The vesma surface as project-graph nodes: CLI commands + REST routes.

Card vesma-graph-command-route-nodes. The engine's own CLI commands and
REST routes become first-class graph nodes (``Command`` / ``Route``) so
an agent discovers the surface with ONE bounded ``search_graph`` call
(~300 tokens) instead of a ~18.7k-token ``--help`` / route dump.

Seam discipline (ADR-0032): THIS is a concrete node-source extension —
the generic indexer never imports it. The host wiring registers it
(:func:`register_surface_source`, called from
``vesmaro.codegraph.service.get_graph_service``); a project whose root
carries the engine's cli/api package markers gets the nodes, ANY other
repo gets none.

NO new parser. Both surfaces come from the LIVE engine's own
registries — the exact source of truth the shell-completion engine
(``vesmaro.cli.complete_cmd``) and FastAPI routing already use:

* commands — the typer/click tree built from :mod:`vesmaro.cli.main`;
* routes — ``app.routes`` of :mod:`vesmaro.api.main` (covers every
  included router: sessions, auth, federation).

Repo binding: nodes live in the graph of the repo BEING INDEXED (the
marker-detected one), and each node binds to its implementing function
with an ``INVOKES`` (Command→handler) / ``HANDLES`` (Route→endpoint)
edge whenever that function resolves in the indexed tree. Store qnames
are FILE-SCOPED, so the resolution is exact, by address: the live
function's import module maps back to the repo-relative file (the
marker's package prefix — ``src/`` or flat — decides the layout), then
the file's ``qname → node id`` map is probed with the qualname.
Unresolved handlers get NO edge — the unresolved-import honesty
discipline.

Token hygiene (the ~300-token answer): a node carries the full
invocation path, a ONE-LINE help (first line only, never the full
text), the OPTION COUNT, and at most 16 compact ``param → one-line
help`` entries. Every help string passes the PG4 secrets detector —
a hit drops the string (never the node).

Cross-project note: commands/routes are code of the DEFINING repo —
an agent of another project queries them with
``project_id`` = the defining repo's project (see
``docs/<lang>/user/project-graph.md``).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from vesmaro.codegraph.node_sources import (
    SourceContribution,
    aux_node_id,
    register_node_source,
)
from vesmaro.secrets_detector import detect_secrets
from vesmaro.storage.code_graph_store import CodeGraphEdge, CodeGraphNode

logger = logging.getLogger(__name__)

#: Repo-relative marker paths (relative to the INDEXED ROOT — never an
#: absolute path): the engine's own cli/api packages. Presence of either
#: package makes the root a defining repo for that surface.
_CLI_MARKERS = ("src/vesmaro/cli/main.py", "vesmaro/cli/main.py")
_API_MARKERS = ("src/vesmaro/api/main.py", "vesmaro/api/main.py")

#: Program name of the CLI tree (the click root's name; the marker
#: constant documents the contract, the walk reads the live root).
_PROG_NAME = "vesma"

#: Compaction caps (the ~300-token search answer): hard ceilings, not
#: suggestions. A pathological help string is truncated, never issued
#: whole.
_HELP_CAP = 200
_PARAM_HELP_CAP = 96
_MAX_LISTED_PARAMS = 16

#: Defensive ceilings (PG7 spirit): the live trees are bounded (~40
#: commands, ~60 routes); the caps only stop a runaway future tree from
#: flooding one publish.
_MAX_COMMAND_NODES = 512
_MAX_ROUTE_NODES = 1024


def _first_line(text: Any) -> str:
    """First stripped line of a help/docstring text ('' when absent)."""
    if not text:
        return ""
    stripped = str(text).strip()
    return stripped.splitlines()[0].strip() if stripped else ""


def _safe(text: str) -> str:
    """PG4 discipline for issued surface text: a secrets-detector hit
    drops the STRING (the node stays, honestly less decorated)."""
    return text if text and not detect_secrets(text) else ""


def _first_marker(root: Path, markers: tuple[str, ...]) -> str | None:
    """The repo-relative marker path that exists under ``root``."""
    for marker in markers:
        if (root / marker).is_file():
            return marker
    return None


def _package_prefix(marker_rel_path: str) -> str:
    """Repo-relative layout prefix implied by a marker path:
    ``src/vesmaro/cli/main.py`` → ``src/``; a flat marker
    (``vesmaro/cli/main.py``) → ``""``."""
    idx = marker_rel_path.find("vesmaro/")
    return marker_rel_path[:idx] if idx > 0 else ""


def _module_rel_candidates(module: str, prefix: str) -> Iterator[str]:
    """Repo-relative file candidates for an import module path:
    ``vesmaro.cli.graph_cmd`` → ``{prefix}vesmaro/cli/graph_cmd.py``."""
    yield f"{prefix}{'/'.join(module.split('.'))}.py"


def _resolve_handler(
    symbols: Mapping[str, Mapping[str, str]],
    module: str | None,
    qualname: str | None,
    prefix: str = "",
) -> str | None:
    """Resolve a live function (module + qualname) to an indexed node id.

    Exact, address-based (the store's qnames are FILE-scoped — a bare
    name for a top-level def, ``Cls.name`` for a method): the module
    maps to the repo-relative file via the marker-derived layout
    prefix, the file's symbol map is probed with the qualname. A
    missing module, a ``<locals>`` qualname or an unresolvable address
    → None → NO edge (never a garbage edge).
    """
    if not module or not qualname or "<locals>" in qualname:
        return None
    for rel in _module_rel_candidates(module, prefix):
        file_symbols = symbols.get(rel)
        if file_symbols is not None:
            return file_symbols.get(qualname)
    return None


class VesmaSurfaceSource:
    """The engine's own CLI + REST surface as ``Command``/``Route`` nodes."""

    name = "vesma-surface"

    def applies(self, root: str | os.PathLike[str]) -> bool:
        base = Path(os.fspath(root))
        return _first_marker(base, _CLI_MARKERS) is not None or (
            _first_marker(base, _API_MARKERS) is not None
        )

    def contribute(
        self,
        project: str,
        root: str | os.PathLike[str],
        symbols: Mapping[str, Mapping[str, str]],
    ) -> SourceContribution:
        base = Path(os.fspath(root))
        contribution = SourceContribution()
        cli_path = _first_marker(base, _CLI_MARKERS)
        if cli_path is not None:
            self._collect_commands(
                project, cli_path, _package_prefix(cli_path), symbols, contribution
            )
        api_path = _first_marker(base, _API_MARKERS)
        if api_path is not None:
            self._collect_routes(
                project, api_path, _package_prefix(api_path), symbols, contribution
            )
        return contribution

    # ── CLI commands (the live typer/click tree — the completion engine's registry) ──

    def _collect_commands(
        self,
        project: str,
        rel_path: str,
        prefix: str,
        symbols: Mapping[str, Mapping[str, str]],
        out: SourceContribution,
    ) -> None:
        try:
            import typer.main

            from vesmaro.cli.main import app
        except Exception:
            logger.warning(
                "codegraph: vesma surface — cli tree unavailable, no Command nodes for %s",
                project,
                exc_info=True,
            )
            return
        try:
            click_root = typer.main.get_command(app)
        except Exception:
            logger.warning(
                "codegraph: vesma surface — cli tree build failed, no Command nodes for %s",
                project,
                exc_info=True,
            )
            return
        for path, cmd in self._walk_commands(click_root):
            if len(out.nodes) >= _MAX_COMMAND_NODES:
                logger.warning(
                    "codegraph: vesma surface — command cap %d reached for %s",
                    _MAX_COMMAND_NODES,
                    project,
                )
                return
            invocation = " ".join(path)
            node_id = aux_node_id(project, rel_path, f"cli:{invocation}")
            help_text = _safe(_first_line(getattr(cmd, "help", None)))[:_HELP_CAP]
            params, options = _params_meta(cmd)
            out.nodes.append(
                CodeGraphNode(
                    id=node_id,
                    project=project,
                    kind="Command",
                    name=invocation,
                    qname=invocation,
                    path=rel_path,
                    metadata={"help": help_text, "options": options, "params": params},
                )
            )
            handler = getattr(cmd, "callback", None)
            target = _resolve_handler(
                symbols,
                getattr(handler, "__module__", None),
                getattr(handler, "__qualname__", None),
                prefix,
            )
            if target is not None:
                out.edges.append(
                    CodeGraphEdge(
                        from_id=node_id,
                        to_id=target,
                        kind="INVOKES",
                        provenance="surface-introspection",
                    )
                )

    @staticmethod
    def _walk_commands(root: Any) -> Iterator[tuple[list[str], Any]]:
        """Every visible node of the click tree (groups AND leaves) with
        its full invocation path — the completion engine's traversal
        semantics (hidden commands are excluded, they are plumbing)."""
        start = getattr(root, "name", None) or _PROG_NAME
        stack: list[tuple[list[str], Any]] = [([start], root)]
        while stack:
            path, cmd = stack.pop()
            yield path, cmd
            for name, sub in dict(getattr(cmd, "commands", None) or {}).items():
                if getattr(sub, "hidden", False):
                    continue
                stack.append(([*path, name], sub))

    # ── REST routes (FastAPI app.routes introspection) ──────────────────────────────

    def _collect_routes(
        self,
        project: str,
        rel_path: str,
        prefix: str,
        symbols: Mapping[str, Mapping[str, str]],
        out: SourceContribution,
    ) -> None:
        try:
            from fastapi.routing import APIRoute

            from vesmaro.api.main import app
        except Exception:
            logger.warning(
                "codegraph: vesma surface — api app unavailable, no Route nodes for %s",
                project,
                exc_info=True,
            )
            return
        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue  # starlette plumbing (404 mounts, etc.)
            endpoint = route.endpoint
            for method in sorted(getattr(route, "methods", None) or ()):
                if len(out.nodes) >= _MAX_ROUTE_NODES:
                    logger.warning(
                        "codegraph: vesma surface — route cap %d reached for %s",
                        _MAX_ROUTE_NODES,
                        project,
                    )
                    return
                name = f"{method} {route.path}"
                node_id = aux_node_id(project, rel_path, name)
                help_text = _safe(
                    _first_line(getattr(route, "summary", None))
                    or _first_line(getattr(endpoint, "__doc__", None))
                )[:_HELP_CAP]
                out.nodes.append(
                    CodeGraphNode(
                        id=node_id,
                        project=project,
                        kind="Route",
                        name=name,
                        qname=name,
                        path=rel_path,
                        metadata={
                            "method": method,
                            "path": route.path,
                            "endpoint": f"{endpoint.__module__}.{endpoint.__qualname__}",
                            "help": help_text,
                        },
                    )
                )
                target = _resolve_handler(
                    symbols,
                    getattr(endpoint, "__module__", None),
                    getattr(endpoint, "__qualname__", None),
                    prefix,
                )
                if target is not None:
                    out.edges.append(
                        CodeGraphEdge(
                            from_id=node_id,
                            to_id=target,
                            kind="HANDLES",
                            provenance="surface-introspection",
                        )
                    )


def _params_meta(cmd: Any) -> tuple[dict[str, str], int]:
    """Compact ``param → one-line help`` map + the total long-option
    count (the "число опций" hygiene rule: full help texts never ride
    the node). Long options preferred as keys; positional arguments
    keep their name. Truncation caps are the compaction contract."""
    params: dict[str, str] = {}
    options = 0
    for param in getattr(cmd, "params", None) or []:
        long_opts = [opt for opt in getattr(param, "opts", None) or [] if opt.startswith("--")]
        if long_opts:
            options += len(long_opts)
            key = long_opts[0]
        else:
            key = str(getattr(param, "name", None) or "")
        if not key or key in params:
            continue
        params[key] = _safe(_first_line(getattr(param, "help", None)))[:_PARAM_HELP_CAP]
    if len(params) > _MAX_LISTED_PARAMS:
        listed = dict(list(params.items())[:_MAX_LISTED_PARAMS])
        listed[f"... +{len(params) - _MAX_LISTED_PARAMS}"] = ""
        return listed, options
    return params, options


#: The module-level singleton the host wiring registers.
SURFACE_SOURCE = VesmaSurfaceSource()


def register_surface_source() -> None:
    """Host registration entry point (idempotent). Called from
    ``get_graph_service`` — the generic indexer stays extension-blind."""
    register_node_source(SURFACE_SOURCE)


__all__ = ["SURFACE_SOURCE", "VesmaSurfaceSource", "register_surface_source"]
