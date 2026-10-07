"""The project-graph node-source seam (ADR-0032 extension point).

The generic indexer (ADR-0032 PG-0) parses FILES with tree-sitter and
knows nothing about any particular project's architecture. OPTIONAL
kinds — e.g. the vesma engine's own CLI commands and REST routes (card
vesma-graph-command-route-nodes) — enter the graph through THIS seam:

* a HOST application (the engine wiring in
  :func:`vesma.codegraph.service.get_graph_service`) registers a
  :class:`ProjectNodeSource` implementation;
* on every FULL index run (:meth:`ProjectIndexer.index_full`) the
  indexer asks every registered source whether it APPLIES to the
  indexed root (marker files inside the root — never a hardcoded
  absolute path) and, when it does, collects its contribution;
* contributions ride the SAME atomic publish as the parsed graph — a
  project's graph is never half-parsed half-extension (ADR-0032 §3.2
  atomicity), and a limit breach still aborts the WHOLE index.

Dependency direction (binding): THIS module and the indexer know the
abstract source contract only — they never import any concrete source.
A concrete source (``vesma.graph_surface_ext``) imports this module
and the storage node/edge dataclasses. A source that raises or fails
to import degrades to NO contribution (honest absence, logged) — an
extension must never break the index.

Handler binding: a source receives ``symbols`` — the indexed project's
PATH-SCOPED symbol map (``repo-relative path → {store qname → node
id}``) — so its nodes can carry edges to the functions that implement
them. Store qnames are file-scoped (a Python top-level def is its bare
name, a method its ``Cls.name``; Go qnames carry the package
prefix), so binding by (path, qname) is exact and needs no cross-file
guesswork. Unresolvable handlers produce NO edge (the same honesty
discipline as unresolved imports: garbage edges are worse than absent
ones).
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from vesma.storage.code_graph_store import CodeGraphEdge, CodeGraphNode

logger = logging.getLogger(__name__)


def aux_node_id(project: str, rel_path: str, symbol: str) -> str:
    """Canonical id for an extension-contributed node — the SAME
    length-prefixed ``<project>#<rel_path>#<symbol>#<line>`` scheme the
    indexer uses (slice-1 contract), with ``line=0``: surface nodes do
    not span source lines. The ``symbol`` segment must be prefix-namespaced
    (``cli:…`` / route method+path) so it can never collide with a real
    symbol at the same path."""
    return f"{len(project)}:{project}#{rel_path}#{symbol}#0"


@dataclass(slots=True)
class SourceContribution:
    """Nodes + edges one source adds to ONE project's publish."""

    nodes: list[CodeGraphNode] = field(default_factory=list)
    edges: list[CodeGraphEdge] = field(default_factory=list)


class ProjectNodeSource(Protocol):
    """One optional node-source extension.

    ``name`` identifies the source in logs (registrations are
    idempotent by name — re-registering replaces). ``applies`` decides
    by the ROOT's content (marker files), never by an absolute path.
    """

    name: str

    def applies(self, root: str | os.PathLike[str]) -> bool: ...

    def contribute(
        self,
        project: str,
        root: str | os.PathLike[str],
        symbols: Mapping[str, Mapping[str, str]],
    ) -> SourceContribution: ...


_SOURCES: dict[str, ProjectNodeSource] = {}
_SOURCES_GUARD = threading.Lock()


def register_node_source(source: ProjectNodeSource) -> None:
    """Register (or replace, by name) a node source. Idempotent: a host
    that wires the same extension on every service construction does
    not duplicate it."""
    with _SOURCES_GUARD:
        _SOURCES[source.name] = source


def node_sources() -> tuple[ProjectNodeSource, ...]:
    """Snapshot of the registered sources (registration order is NOT
    part of the contract)."""
    with _SOURCES_GUARD:
        return tuple(_SOURCES.values())


def clear_node_sources() -> None:
    """Drop every registration (test isolation only)."""
    with _SOURCES_GUARD:
        _SOURCES.clear()


def run_node_sources(
    project: str,
    root: str | os.PathLike[str],
    symbols: Mapping[str, Mapping[str, str]],
) -> SourceContribution:
    """Collect every applicable source's contribution for ONE project.

    Per-source isolation: a failing source logs a warning and
    contributes NOTHING — the index publish proceeds with the parsed
    graph alone (an extension must never break an index, the beacon
    degradation precedent). A source whose ``applies`` is False is
    skipped silently — a foreign repo must stay unpolluted.
    """
    merged = SourceContribution()
    for source in node_sources():
        try:
            if not source.applies(root):
                continue
            contribution = source.contribute(project, root, symbols)
        except Exception:
            logger.warning(
                "codegraph: node source %r failed for project %s — contributing nothing",
                source.name,
                project,
                exc_info=True,
            )
            continue
        merged.nodes.extend(contribution.nodes)
        merged.edges.extend(contribution.edges)
        if contribution.nodes:
            logger.info(
                "codegraph: node source %r contributed %d node(s), %d edge(s) to %s",
                source.name,
                len(contribution.nodes),
                len(contribution.edges),
                project,
            )
    return merged
