"""Project code graph indexing (ADR-0032 PG-0 slices 2+3).

The INDEXING side of the project graph — a sibling of ``storage``
(slice 1 owns the sidecar store), never part of it. This package
parses sources with tree-sitter and publishes nodes/edges through the
``CodeGraphStore`` bulk API; the storage layer stays parser-free and
this layer stays persistence-ignorant beyond the store contract.

Wave 1 is Python-only by committee ruling (ArchCom 2026-09-28 §1: the
price of a language is its import resolution). Adding a language is a
REGISTRY ENTRY (``languages.py``) plus its import resolver — the
design target for wave PG-3, not a rewrite.
"""

from vesmaro.codegraph.incremental import index_project, staleness_check
from vesmaro.codegraph.indexer import IndexResult, ProjectIndexer

__all__ = [
    "IndexResult",
    "ProjectIndexer",
    "index_project",
    "staleness_check",
]
