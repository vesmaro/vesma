"""Tree-sitter indexer core for the project code graph (ADR-0032 PG-0 slice 2).

Extraction contract (ArchCom 2026-09-28 §3.2):

* ONLY named nodes are traversed; ``comment`` and ``string`` node
  types are never read — PG1 (zero source bytes) is enforced
  technically, not declaratively. Docstrings, comments and literal
  default values physically cannot enter the store because their
  node types are skipped by the traversal.
* Node kinds: File/Module/Class/Function/Method/Type with name,
  qname, start_line/end_line; signatures carry identifiers and type
  NAMES only, never default values.
* Edge kinds: CONTAINS_FILE (Project→File), DEFINES (File→symbol),
  IMPORTS (Module→Module by resolution), CALLS only when proven by
  resolution, otherwise USES with ``provenance='heuristic'``
  (USAGE-honesty: an unproven call is NEVER CALLS), INHERITS,
  TESTS (test-file name heuristic).
* Python import resolution: relative imports plus a sys.path-style
  repo-root heuristic with a dotted-tail fallback for roots that are
  not the sys.path entry (src-layout); an unresolved import produces
  NO edge, silently — garbage edges are worse than absent ones.

Limits (PG7, fail-closed): ``index_max_files``/``index_max_source_mb``
are checked BEFORE the publish transaction; a breach raises
:class:`IndexLimitError` and the project keeps its previous graph —
a partial graph is never published.

Wave-1 call attribution: a CALLS/USES edge is attributed to the
ENCLOSING function/method node of the call site when there is one
(the module node otherwise) — the edge endpoints live in one file,
so the resolution is local and needs no cross-file index.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vesmaro.codegraph.file_surface import FileSurface, SurfaceFile
from vesmaro.codegraph.languages import (
    LanguageSpec,
    is_test_file,
    language_for_path,
)
from vesmaro.config import CodeGraphConfig
from vesmaro.secrets_detector import detect_secrets
from vesmaro.storage.code_graph_store import (
    CodeGraphEdge,
    CodeGraphNode,
    CodeGraphStore,
    GraphFileRecord,
    MainStoreMeta,
    bump_project_graph_epoch,
)

logger = logging.getLogger(__name__)

_KIND_PROJECT = "Project"
_KIND_FILE = "File"
_KIND_MODULE = "Module"
_KIND_CLASS = "Class"
_KIND_TYPE = "Type"

#: Node types whose text is NEVER read — PG1 at the parser level.
_LITERAL_TYPES: frozenset[str] = frozenset(
    {"string", "comment", "integer", "float", "true", "false", "none"}
)

#: Marker rendered in place of a dropped default value — ``f(x=…)``
#: instead of ``f(x=SOME_NAME)`` (contract §5: a NAME in default
#: position is still residual risk; the value is unread, whole).
_SIG_DEFAULT_MARKER = "…"


def _secret_allowlisted(rel_path: str, patterns: tuple[str, ...]) -> bool:
    """Issue #449: does the repo-relative path match an allowlist glob?

    ``fnmatch`` semantics over the repo-relative path (the surface the
    config documents); an empty pattern tuple never matches, so the
    default config keeps PG3 byte-identical.
    """
    return any(fnmatch.fnmatch(rel_path, pattern) for pattern in patterns)


class IndexLimitError(Exception):
    """PG7 fail-closed limit breach — the whole index is aborted.

    Deliberately an explicit ``Exception`` subclass: the caller
    distinguishes "too big to index" from a programming error, and the
    store write is never started.
    """


@dataclass(slots=True)
class _FileExtraction:
    """Everything parsed out of ONE source file (pre-publish)."""

    nodes: list[CodeGraphNode]
    edges: list[CodeGraphEdge]
    #: qname (dotted, scoped) -> node, for intra-file references.
    qname_to_node: dict[str, CodeGraphNode]
    imports: list[str] = field(default_factory=list)
    top_level_defs: set[str] = field(default_factory=set)
    class_bases: dict[str, list[str]] = field(default_factory=dict)
    #: (call head, enclosing-definition qname or None) pairs.
    call_refs: list[tuple[str, str | None]] = field(default_factory=list)
    poisoned: bool = False
    parse_ok: bool = True
    parse_error: str | None = None


@dataclass(slots=True)
class IndexResult:
    """Report of one indexing run (facade return, input to slices 4/6).

    ``parse_errors`` maps repo-relative paths to the failure reason —
    «clean ≠ proof»: the agent sees parse failures, the same honesty
    rule as ``graph_files.parse_error``. Not frozen: the indexer
    accumulates counters into it as the run progresses.
    """

    nodes: int = 0
    edges: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    files_reparsed: int = 0
    poisoned: list[str] = field(default_factory=list)
    #: Issue #449: previously-poisoned paths removed from the sidecar
    #: set by THIS run's allowlist pass (audited by the service layer
    #: with reason ``allowlist-unpoison`` — the un-poison is never
    #: silent).
    unpoisoned: list[str] = field(default_factory=list)
    parse_errors: dict[str, str] = field(default_factory=dict)
    duration: float = 0.0
    incremental: bool = False
    status: str = "ok"


def _node_id(project: str, rel_path: str, symbol: str, line: int) -> str:
    """Canonical id ``<project>#<rel_path>#<symbol>#<line>`` (slice-1
    contract). The project segment is length-prefixed so a project
    name containing ``#`` cannot forge ids into another project."""
    return f"{len(project)}:{project}#{rel_path}#{symbol}#{line}"


def _file_id(project: str, rel_path: str) -> str:
    return _node_id(project, rel_path, "", 0)


def _module_id(project: str, rel_path: str) -> str:
    return _file_id(project, rel_path) + "#module"


def _text(src: bytes, node: Any) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _sig_part_text(src: bytes, part: Any) -> str:
    """Signature shape of ONE parameter/type part — identifiers and
    type names only. Anonymous parts are dropped (PG1: a node whose
    ``is_named`` is False never contributes), literal parts are dropped
    (PG1: no default values, no string annotations).

    A parameter WITH a default (``default_parameter`` /
    ``typed_default_parameter``) keeps only its name — and type names
    for the annotated form — and renders the ``=…`` marker: the default
    VALUE is dropped WHOLE, whether it is a literal, a bare NAME
    (``f(x=SOME_NAME)`` — names from default positions are residual
    risk, contract §5) or a call (review 10173a2a-1)."""
    if not part.is_named or part.type in _LITERAL_TYPES:
        return ""
    if part.type in ("default_parameter", "typed_default_parameter"):
        name_node = part.child_by_field_name("name")
        name = _text(src, name_node) if name_node is not None else ""
        annotation = ""
        if part.type == "typed_default_parameter":
            type_node = part.child_by_field_name("type")
            if type_node is not None:
                annotation = _sig_part_text(src, type_node)
        return (
            f"{name}: {annotation}={_SIG_DEFAULT_MARKER}"
            if annotation
            else (f"{name}={_SIG_DEFAULT_MARKER}")
        )
    if part.type in ("identifier", "type_identifier"):
        return _text(src, part)
    inner = [_sig_part_text(src, c) for c in part.children]
    return " ".join(s for s in inner if s)


def _signature_of(src: bytes, def_node: Any) -> str | None:
    """Signature SHAPE for a definition — identifiers and type names
    survive; default values, docstrings and literals are physically
    unread (their node types are skipped)."""
    params_node = def_node.child_by_field_name("parameters")
    if params_node is None or params_node.type != "parameters":
        return None
    parts: list[str] = []
    for p in params_node.children:
        text = _sig_part_text(src, p)
        if text:
            parts.append(text)
    return "(" + ", ".join(parts) + ")" if parts else None


def _class_bases(src: bytes, class_node: Any) -> list[str]:
    """Base-class names from the class argument list — identifier and
    attribute heads only (``a.b.C`` keeps its dotted head; keyword
    arguments such as ``metaclass=type`` are dropped)."""
    bases: list[str] = []
    for arg_list in class_node.children:
        if arg_list.type != "argument_list":
            continue
        for arg in arg_list.children:
            if not arg.is_named or arg.type in ("keyword_argument", "string"):
                continue
            head = _text(src, arg)
            if head:
                bases.append(head)
    return bases


def _module_qname(rel_path: str) -> str:
    """``a/b/c.py`` / ``a/b/__init__.py`` -> ``a.b.c`` / ``a.b``."""
    if rel_path.endswith("__init__.py"):
        stem = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
        parts = [p for p in stem.split("/") if p]
    else:
        stem = rel_path.removesuffix(".py")
        parts = [p for p in stem.split("/") if p]
    return ".".join(parts) or "."


def _collect_call_heads(
    node: Any, src: bytes, out: list[tuple[str, str | None]], enclosing: str | None
) -> None:
    """Walk named nodes collecting (call head, enclosing def qname).

    The ``function`` field of a ``call`` is an identifier or attribute
    — never argument literals; comments/strings are not named children
    and are unreachable (PG1). ``enclosing`` tracks the innermost
    function/method definition so a CALLS/USES edge is attributed to
    the calling function, not the module, whenever possible.
    """
    for child in node.named_children:
        child_enclosing = enclosing
        if child.type == "function_definition":
            name_node = child.child_by_field_name("name")
            if name_node is not None:
                child_enclosing = _text(src, name_node)
        if child.type == "call":
            fn = child.child_by_field_name("function")
            if fn is not None:
                out.append((_text(src, fn), enclosing))
        _collect_call_heads(child, src, out, child_enclosing)


class PythonFileParser:
    """Parses ONE Python file into nodes/edges (pre-resolution).

    Emits File + Module nodes, Class/Function/Method/Type definition
    nodes with DEFINES edges (File→symbol), and collects imports,
    class bases and (call head, enclosing def) pairs for the
    project-wide resolution pass. ``comment`` and ``string`` node
    types are never read — PG1 at the parser level.

    ``secret_allowlist`` (issue #449): repo-relative fnmatch globs whose
    files SKIP poison-marking — the detector is not even run for them
    (the file is still indexed normally; only the ``secret-detected``
    marking is skipped). Empty tuple = today's PG3 behavior.
    """

    def __init__(self, language: Any, secret_allowlist: tuple[str, ...] = ()) -> None:
        from tree_sitter import Parser

        self._parser = Parser(language)
        self._secret_allowlist = secret_allowlist

    def parse(self, project: str, rel_path: str, source: bytes) -> _FileExtraction:
        tree = self._parser.parse(source)
        root = tree.root_node
        spec = language_for_path(rel_path)
        assert spec is not None  # the surface passed the allowlist

        poisoned = not _secret_allowlisted(rel_path, self._secret_allowlist) and bool(
            detect_secrets(source.decode("utf-8", "replace"))
        )
        meta: dict[str, Any] | None = {"poisoned": True} if poisoned else None
        fid = _file_id(project, rel_path)
        mid = _module_id(project, rel_path)

        extraction = _FileExtraction(
            nodes=[
                CodeGraphNode(
                    id=fid,
                    project=project,
                    kind=_KIND_FILE,
                    name=rel_path.rsplit("/", 1)[-1],
                    qname=rel_path,
                    path=rel_path,
                    start_line=1,
                    end_line=root.end_point[0] + 1,
                    lang=spec.name,
                    metadata=meta,
                ),
                CodeGraphNode(
                    id=mid,
                    project=project,
                    kind=_KIND_MODULE,
                    name=_module_qname(rel_path).rsplit(".", 1)[-1] or rel_path,
                    qname=_module_qname(rel_path),
                    path=rel_path,
                    lang=spec.name,
                    metadata=meta,
                ),
            ],
            edges=[CodeGraphEdge(from_id=fid, to_id=mid, kind="DEFINES")],
            qname_to_node={},
            poisoned=poisoned,
            parse_ok=not root.has_error,
            parse_error="syntax-error" if root.has_error else None,
        )
        self._walk(root, source, project, rel_path, "", False, extraction)
        _collect_call_heads(root, source, extraction.call_refs, None)
        return extraction

    def _walk(
        self,
        block: Any,
        source: bytes,
        project: str,
        rel_path: str,
        prefix: str,
        in_class: bool,
        extraction: _FileExtraction,
    ) -> None:
        """Single disciplined traversal: named children only; the
        bodies of definitions recurse with the nested qname prefix."""
        for child in block.named_children:
            ctype = child.type
            if ctype == "import_statement":
                imp = self._flat_import(child, source)
                if imp is not None:
                    extraction.imports.append(imp)
            elif ctype == "import_from_statement":
                module = self._from_import(child, source)
                if module is not None:
                    extraction.imports.append(module)
            elif ctype in (
                "class_definition",
                "function_definition",
                "type_alias_statement",
            ):
                name_node = child.child_by_field_name("name")
                if name_node is None:
                    continue
                name = _text(source, name_node)
                self._define(child, name, source, project, rel_path, prefix, in_class, extraction)
                body = child.child_by_field_name("body")
                if body is not None:
                    self._walk(
                        body,
                        source,
                        project,
                        rel_path,
                        f"{prefix}{name}.",
                        ctype == "class_definition" or in_class,
                        extraction,
                    )
            # comment/string/expression nodes: intentionally unread (PG1)

    def _define(
        self,
        node: Any,
        name: str,
        source: bytes,
        project: str,
        rel_path: str,
        prefix: str,
        in_class: bool,
        extraction: _FileExtraction,
    ) -> None:
        qname = f"{prefix}{name}"
        if node.type == "class_definition":
            kind = _KIND_CLASS
            signature = _signature_of(source, node)
            extraction.class_bases[qname] = _class_bases(source, node)
        elif node.type == "function_definition":
            kind = "Method" if in_class else "Function"
            signature = _signature_of(source, node)
        else:
            kind = _KIND_TYPE  # type_alias_statement (PEP 695)
            signature = None
        spec = language_for_path(rel_path)
        assert spec is not None
        graph_node = CodeGraphNode(
            id=_node_id(project, rel_path, name, node.start_point[0] + 1),
            project=project,
            kind=kind,
            name=name,
            qname=qname,
            path=rel_path,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            lang=spec.name,
            signature=signature,
            metadata={"poisoned": True} if extraction.poisoned else None,
        )
        extraction.nodes.append(graph_node)
        extraction.qname_to_node[qname] = graph_node
        if not prefix:  # top level of the module
            extraction.top_level_defs.add(name)
        extraction.edges.append(
            CodeGraphEdge(from_id=_file_id(project, rel_path), to_id=graph_node.id, kind="DEFINES")
        )

    def _flat_import(self, stmt: Any, source: bytes) -> str | None:
        """``import a.b`` -> ``a.b`` (alias ignored — the IMPORTS edge
        is module-to-module, not local-name-to-module)."""
        for c in stmt.children:
            if c.type == "dotted_name":
                return _text(source, c)
        return None

    def _from_import(self, stmt: Any, source: bytes) -> str | None:
        """``from X import ...`` -> ``X`` (dotted or relative — the
        relative form keeps its leading dots for the resolver)."""
        for c in stmt.children:
            if c.type in ("dotted_name", "relative_import"):
                return _text(source, c)
        return None


def _resolve_import(
    imported: str,
    rel_path: str,
    path_index: dict[str, str],
    fallback: _ImportFallbackIndex | None = None,
) -> str | None:
    """Resolve ONE import to a repo-relative path, or ``None``.

    Wave-1 Python heuristics (contract §3.2):

    * absolute ``a.b.c`` -> ``a/b/c.py`` or ``a/b/c/__init__.py``,
      then a prefix walk (``from a.b import C`` imports module
      ``a.b``) — the project root is the single sys.path entry;
    * relative ``.mod`` / ``..pkg`` resolve against the importing
      file's package directory (one dot = current package, each
      extra dot climbs one level);
    * dotted-tail fallback (root NOT the sys.path entry — a
      src-layout indexed at the package dir or at the repo root):
      the alias is matched against known module qnames as a dotted
      suffix in both directions. Only modules that exist in the
      project can match; anything unproven still gets NO edge.
    """
    if imported.startswith("."):
        dots = len(imported) - len(imported.lstrip("."))
        leaf = imported.lstrip(".")
        package_dir = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
        parts = [p for p in package_dir.split("/") if p]
        climb = dots - 1
        base_parts = parts[: len(parts) - climb]
        base = "/".join(base_parts)
        if not leaf:  # ``from . import sibling`` -> the package itself
            candidates = ["__init__.py"] if not base else [f"{base}/__init__.py"]
        else:
            candidates = [
                f"{base}/{leaf}.py" if base else f"{leaf}.py",
                f"{base}/{leaf}/__init__.py" if base else f"{leaf}/__init__.py",
            ]
        for candidate in candidates:
            if candidate in path_index:
                return path_index[candidate]
        return None
    candidate = imported.replace(".", "/") + ".py"
    if candidate in path_index:
        return path_index[candidate]
    candidate = imported.replace(".", "/") + "/__init__.py"
    if candidate in path_index:
        return path_index[candidate]
    parts = imported.split(".")
    while len(parts) > 1:
        parts.pop()
        for suffix in (".py", "/__init__.py"):
            prefix = "/".join(parts) + suffix
            if prefix in path_index:
                return path_index[prefix]
    if fallback is not None:
        return fallback.resolve(imported)
    return None


class _ImportFallbackIndex:
    """Dotted-tail import fallback for roots that are not the sys.path entry.

    The wave-1 resolver assumes the indexing root IS the only sys.path
    entry. When it is not (a src-layout indexed at the package dir
    ``src/vesmaro``, or at the repo root above ``src/``), the dotted
    import path (``vesmaro.util``) and the module's dotted path from
    the root (``util`` / ``src.vesmaro.util``) differ by a constant
    dotted prefix. The fallback matches the two on dotted tails —
    built in one pass over the module index, O(alias segments) per
    lookup:

    * the alias is a dotted suffix of exactly ONE module qname (root
      indexed ABOVE the package top);
    * a module qname is a dotted suffix of the alias AND the stripped
      alias head is exactly the indexing root's own basename (root
      indexed AT the package top: alias ``vesmaro.util`` vs qname
      ``util`` with root ``.../src/vesmaro``).

    Honesty limits: only modules that exist in the project can match;
    an ambiguous dotted tail (two modules share it) matches nothing;
    stdlib-topped aliases (``os.path``) never fall back — a project
    module may share the tail name and that edge would be fiction.
    """

    def __init__(self, module_index: dict[str, str], root_name: str) -> None:
        self._modules = module_index
        self._root_name = root_name
        tails: dict[str, list[str]] = {}
        for qname in module_index:
            parts = qname.split(".")
            for i in range(len(parts)):
                tails.setdefault(".".join(parts[i:]), []).append(qname)
        # A dotted tail shared by 2+ modules is ambiguous: refuse it.
        self._by_tail = {t: q[0] for t, q in tails.items() if len(q) == 1}

    def resolve(self, alias: str) -> str | None:
        if alias.partition(".")[0] in sys.stdlib_module_names:
            return None
        # Root above the package top: the alias is a dotted suffix of
        # exactly one known module qname.
        qname = self._by_tail.get(alias)
        if qname is not None:
            return self._modules[qname]
        # Root at/below the package top: a known module qname is a
        # dotted suffix of the alias and the stripped head names the
        # indexing root itself.
        parts = alias.split(".")
        for i in range(1, len(parts)):
            rel = self._modules.get(".".join(parts[i:]))
            if rel is not None and ".".join(parts[:i]) == self._root_name:
                return rel
        return None


def _tested_module_name(test_rel: str) -> str:
    """``tests/test_thing.py`` / ``test_thing.py`` -> ``thing``."""
    name = test_rel.rsplit("/", 1)[-1].removesuffix(".py")
    if name.startswith("test_"):
        return name[len("test_") :]
    if name.endswith("_test"):
        return name[: -len("_test")]
    return name


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ProjectIndexer:
    """Indexes one project root into the sidecar store.

    Orchestrates: surface walk → limit checks (fail-closed, before
    any store write) → parse → secret scan (poison) → project-wide
    resolution pass (IMPORTS/INHERITS/TESTS/CALLS/USES) → publish
    (DELETE project subtree, then bulk insert via the store bulk
    API — the slice-1 atomicity surface). The caller owns
    serialization (``incremental.py`` holds the per-project locks).
    """

    def __init__(
        self,
        store: CodeGraphStore,
        data: CodeGraphConfig | None = None,
    ) -> None:
        self.store = store
        self._config = data or CodeGraphConfig()
        self._parsers: dict[str, PythonFileParser] = {}
        # Issue #449: the allowlist is fixed for the indexer's lifetime
        # (config is immutable here), so the parsers are built with it.
        self._secret_allowlist = tuple(self._config.secret_allowlist)

    def _parser_for(self, spec: LanguageSpec) -> PythonFileParser:
        cached = self._parsers.get(spec.name)
        if cached is None:
            cached = PythonFileParser(spec.language_factory(), self._secret_allowlist)
            self._parsers[spec.name] = cached
        return cached

    @property
    def config(self) -> CodeGraphConfig:
        return self._config

    def parse_file(self, project: str, rel_path: str, source: bytes) -> _FileExtraction:
        """Parse ONE file into its extraction (no store writes)."""
        spec = language_for_path(rel_path)
        assert spec is not None
        return self._parser_for(spec).parse(project, rel_path, source)

    # ── limits (PG7, fail-closed) ──────────────────────────────────────────

    def apply_secret_allowlist(self, project: str) -> list[str]:
        """Un-poison allowlist-matching paths (issue #449, explicit).

        Reads the sidecar poisoned set and removes every path whose
        repo-relative form matches a ``secret_allowlist`` glob — the
        operator's escape hatch for known-fake secret fixtures. Returns
        the removed paths (sorted; empty = nothing matched) so the
        caller reports them; the SERVICE layer audits every non-empty
        removal with reason ``allowlist-unpoison`` — the un-poison is
        logged, never silent. Called on EVERY index run (publish or
        fresh), so a config-only allowlist change takes effect on the
        next index call without waiting for a file to change.
        """
        if not self._secret_allowlist:
            return []
        poisoned = self.store.get_poisoned_paths(project)
        matching = sorted(p for p in poisoned if _secret_allowlisted(p, self._secret_allowlist))
        if not matching:
            return []
        removed = self.store.remove_poisoned_paths(project, matching)
        if removed:
            logger.info(
                "codegraph: secret_allowlist un-poisoned %d path(s) for %s: %s",
                len(removed),
                project,
                ", ".join(removed),
            )
        return removed

    def check_limits(self, surface: list[SurfaceFile], root: str) -> None:
        """File count and total source bytes, checked BEFORE any store
        write — a partial graph is never published."""
        if len(surface) > self._config.index_max_files:
            raise IndexLimitError(
                f"project graph: {len(surface)} files exceed "
                f"index_max_files={self._config.index_max_files} "
                f"(root={root}); index aborted — no partial graph published"
            )
        cap_bytes = self._config.index_max_source_mb * 1024 * 1024
        total = 0
        for sf in surface:
            try:
                total += os.stat(sf.abs_path).st_size
            except OSError:
                continue
            if total > cap_bytes:
                raise IndexLimitError(
                    f"project graph: source bytes exceed "
                    f"index_max_source_mb={self._config.index_max_source_mb} "
                    f"(root={root}); index aborted — no partial graph published"
                )

    # ── full index (atomic publish) ────────────────────────────────────────

    def index_full(self, project: str, root: str | os.PathLike[str]) -> IndexResult:
        """Full index: DELETE the project subtree + bulk insert. A PG7
        limit breach raises :class:`IndexLimitError` BEFORE any store
        write — the previous graph survives untouched."""
        started = time.perf_counter()
        root = os.fspath(root)
        surface = FileSurface(root).collect()
        self.check_limits(surface, root)
        result = IndexResult()
        _, extractions, records = self._parse_all(project, surface, result)
        nodes, edges = self._resolve(project, surface, extractions, root)
        nodes.append(
            CodeGraphNode(
                id=_node_id(project, "", "", 0),
                project=project,
                kind=_KIND_PROJECT,
                name=project,
                qname=project,
            )
        )
        self._publish(project, surface, nodes, edges, records)
        # PG3 «навсегда»: newly-detected poisoned paths are UNIONED into
        # the sidecar set — a reindex must never launder a poisoned file
        # (the store refuses to touch the set on the publish path).
        self.store.add_poisoned_paths(project, result.poisoned)
        # Issue #449: the one sanctioned exception — allowlist-matching
        # paths LEAVE the set (explicit, audited upstream in the service).
        result.unpoisoned = self.apply_secret_allowlist(project)
        result.nodes = len(nodes)
        result.edges = len(edges)
        result.files_indexed = len(records)
        result.duration = time.perf_counter() - started
        logger.info(
            "codegraph: full index %s: %d nodes, %d edges, %d files, %d poisoned, "
            "%d parse errors, %.2fs",
            project,
            result.nodes,
            result.edges,
            result.files_indexed,
            len(result.poisoned),
            len(result.parse_errors),
            result.duration,
        )
        return result

    def _parse_all(
        self,
        project: str,
        surface: list[SurfaceFile],
        result: IndexResult,
    ) -> tuple[IndexResult, dict[str, _FileExtraction], list[GraphFileRecord]]:
        """Read + parse + secret-scan every surface file; build the
        ``graph_files`` records (poisoned files keep parse_ok=0 with
        ``secret-detected`` — the flag the issuance side will obey)."""
        extractions: dict[str, _FileExtraction] = {}
        records: list[GraphFileRecord] = []
        for sf in surface:
            try:
                source = Path(sf.abs_path).read_bytes()
            except OSError as exc:
                logger.warning("codegraph: unreadable file skipped %s: %s", sf.rel_path, exc)
                result.files_skipped += 1
                continue
            extraction = self.parse_file(project, sf.rel_path, source)
            extractions[sf.rel_path] = extraction
            parse_ok = extraction.parse_ok
            parse_error: str | None = extraction.parse_error
            if extraction.poisoned:
                parse_ok = False
                parse_error = "secret-detected"
                result.poisoned.append(sf.rel_path)
            elif not parse_ok:
                result.parse_errors[sf.rel_path] = parse_error or "parse-error"
            records.append(
                GraphFileRecord(
                    project=project,
                    path=sf.rel_path,
                    mtime=os.stat(sf.abs_path).st_mtime,
                    size=len(source),
                    hash=_sha256(source),
                    parse_ok=int(parse_ok),
                    parse_error=parse_error,
                )
            )
        return result, extractions, records

    def _publish(
        self,
        project: str,
        surface: list[SurfaceFile],
        nodes: list[CodeGraphNode],
        edges: list[CodeGraphEdge],
        records: list[GraphFileRecord],
    ) -> None:
        """Atomic publish: CONTAINS_FILE edges for the surface, then the
        slice-4 ONE-TRANSACTION publish (delete subtree + insert nodes,
        edges and file records together — the previous graph survives
        any failure)."""
        project_node_id = _node_id(project, "", "", 0)
        edges.extend(
            CodeGraphEdge(
                from_id=project_node_id,
                to_id=_file_id(project, sf.rel_path),
                kind="CONTAINS_FILE",
            )
            for sf in surface
        )
        # Self-loop guard (store CHECK from_id <> to_id): a recursive
        # call inside its own definition would otherwise fail the batch.
        edges = [e for e in edges if e.from_id != e.to_id]
        self.store.publish_project_graph(project, nodes, edges, records)

    def _finish(
        self,
        result: IndexResult,
        nodes: list[CodeGraphNode],
        edges: list[CodeGraphEdge],
        records: list[GraphFileRecord],
        started: float,
        *,
        incremental: bool,
    ) -> IndexResult:
        """Fill the result counters after a publish (shared by the full
        and incremental entry points)."""
        result.nodes = len(nodes)
        result.edges = len(edges)
        result.files_indexed = len(records)
        result.incremental = incremental
        result.duration = time.perf_counter() - started
        return result

    # ── resolution pass ────────────────────────────────────────────────────

    def _resolve(
        self,
        project: str,
        surface: list[SurfaceFile],
        extractions: dict[str, _FileExtraction],
        root: str,
    ) -> tuple[list[CodeGraphNode], list[CodeGraphEdge]]:
        """Project-wide passes after every file is parsed.

        1. path/module indexes — the sys.path-root heuristic base, plus
           the dotted-tail fallback index for roots that are NOT the
           sys.path entry (src-layout).
        2. IMPORTS: dotted/relative imports resolved against the path
           index; unresolved → NO edge (silent, no garbage edges).
        3. INHERITS: base-name lookup against known classes (dotted
           head first, then the tail identifier).
        4. TESTS: test-file heuristic → edge from the test Module to
           the module under test, when that module exists.
        5. CALLS/USES: a call head resolving to a top-level def of
           this file or of an imported module gets CALLS (proven);
           a head matching a known in-file symbol otherwise gets USES
           with ``provenance='heuristic'``; unknown heads get no edge
           at all (external/stdlib surface is not wave-1 material).
        """
        path_index: dict[str, str] = {sf.rel_path: sf.rel_path for sf in surface}
        module_index: dict[str, str] = {_module_qname(sf.rel_path): sf.rel_path for sf in surface}
        fallback = _ImportFallbackIndex(module_index, os.path.basename(os.path.abspath(root)))
        qname_index: dict[str, CodeGraphNode] = {}
        for ex in extractions.values():
            for qname, node in ex.qname_to_node.items():
                qname_index[qname] = node

        nodes: list[CodeGraphNode] = []
        edges: list[CodeGraphEdge] = []
        for ex in extractions.values():
            nodes.extend(ex.nodes)
            edges.extend(ex.edges)

        # IMPORTS (module-to-module, alias-free).
        for rel, ex in extractions.items():
            mid = _module_id(project, rel)
            for imported in ex.imports:
                target = _resolve_import(imported, rel, path_index, fallback)
                if target is None or target == rel:
                    continue  # unresolved or self-import: NO edge
                edges.append(
                    CodeGraphEdge(
                        from_id=mid,
                        to_id=_module_id(project, target),
                        kind="IMPORTS",
                    )
                )

        # INHERITS (provenance: tree-sitter — the base IS in source).
        for ex in extractions.values():
            for qname, bases in ex.class_bases.items():
                class_node = ex.qname_to_node.get(qname)
                if class_node is None:
                    continue
                for base in bases:
                    base_node = qname_index.get(base) or qname_index.get(base.rsplit(".", 1)[-1])
                    if base_node is not None and base_node.id != class_node.id:
                        edges.append(
                            CodeGraphEdge(
                                from_id=class_node.id, to_id=base_node.id, kind="INHERITS"
                            )
                        )

        # TESTS (name heuristic — provenance says so).
        for sf in surface:
            spec = language_for_path(sf.rel_path)
            if spec is None or not is_test_file(spec, sf.rel_path):
                continue
            tested_rel = self._tested_target(sf.rel_path, module_index, path_index)
            if tested_rel is not None and tested_rel != sf.rel_path:
                edges.append(
                    CodeGraphEdge(
                        from_id=_module_id(project, sf.rel_path),
                        to_id=_module_id(project, tested_rel),
                        kind="TESTS",
                        provenance="heuristic",
                    )
                )

        # CALLS (proven) / USES (heuristic fallback — USAGE-honesty).
        for rel, ex in extractions.items():
            mid = _module_id(project, rel)
            imported_modules = {imp for imp in ex.imports if not imp.startswith(".")}
            imported_top: set[str] = set()
            for imp in imported_modules:
                target_rel = _resolve_import(imp, rel, path_index, fallback)
                if target_rel is not None:
                    imported_top |= extractions[target_rel].top_level_defs
            for head, enclosing in ex.call_refs:
                local_name = head.split(".")[0]
                tail = head.rsplit(".", 1)[-1]
                caller = self._caller_id(ex, mid, enclosing)
                # (a) proven: top-level def of THIS file (incl. class ctor)
                local_def = self._top_def_node(ex, head, local_name, tail)
                if local_def is not None and local_def.id != caller:
                    edges.append(CodeGraphEdge(from_id=caller, to_id=local_def.id, kind="CALLS"))
                    continue
                # (b) proven: top-level def of an IMPORTED module
                target_id = self._imported_def_id(
                    project, rel, imported_modules, tail, path_index, extractions, fallback
                )
                if target_id is not None and target_id != caller:
                    edges.append(CodeGraphEdge(from_id=caller, to_id=target_id, kind="CALLS"))
                    continue
                # (c) heuristic: a known symbol in-file (method/attr ref)
                known = self._known_symbol(ex, head, tail)
                if known is not None and known.id != caller:
                    edges.append(
                        CodeGraphEdge(
                            from_id=caller,
                            to_id=known.id,
                            kind="USES",
                            provenance="heuristic",
                        )
                    )
                # (d) unknown head: NO edge (contract: silent drop)
        return nodes, edges

    def _known_symbol(self, ex: _FileExtraction, head: str, tail: str) -> CodeGraphNode | None:
        """The in-file symbol a heuristic reference points at: exact
        qname first, then a qname whose dotted TAIL matches the head
        (``Thing().run`` → the ``Thing.run`` method). Same-file only —
        the cross-file surface rides the CALLS resolution above."""
        node = ex.qname_to_node.get(head) or ex.qname_to_node.get(tail)
        if node is not None:
            return node
        for qname, node in ex.qname_to_node.items():
            if qname.rsplit(".", 1)[-1] == tail:
                return node
        return None

    def _tested_target(
        self,
        test_rel: str,
        module_index: dict[str, str],
        path_index: dict[str, str],
    ) -> str | None:
        """rel_path of the module under test, or ``None``.

        The heuristic (contract §3.2): ``test_<module>.py`` tests the
        same-named module. Candidate paths are tried in order — the
        sibling path (``tests/test_x.py`` → ``x.py``), then any path
        whose module qname tail matches. Unresolved → NO edge.
        """
        stem = _tested_module_name(test_rel)
        sibling = stem + ".py"
        if sibling in path_index:
            return path_index[sibling]
        for qname, rel in module_index.items():
            if qname.rsplit(".", 1)[-1] == stem and rel != test_rel:
                return rel
        return None

    def _caller_id(self, ex: _FileExtraction, mid: str, enclosing: str | None) -> str:
        """Node id of the call site's enclosing function/method (falls
        back to the module node when the call is at module level or
        the enclosing def failed to parse into a node)."""
        if enclosing is not None:
            node = ex.qname_to_node.get(enclosing)
            if node is not None:
                return node.id
            # nested defs carry a dotted qname: innermost tail match
            tail = enclosing.rsplit(".", 1)[-1]
            for qname, node in ex.qname_to_node.items():
                if qname.rsplit(".", 1)[-1] == tail:
                    return node.id
        return mid

    def _top_def_node(
        self, ex: _FileExtraction, head: str, local_name: str, tail: str
    ) -> CodeGraphNode | None:
        """The top-level def node a call head proves in THIS file."""
        for name in (head, local_name, tail):
            node = ex.qname_to_node.get(name)
            if node is not None and name in ex.top_level_defs:
                return node
        return None

    def _imported_def_id(
        self,
        project: str,
        rel: str,
        imported_modules: set[str],
        symbol: str,
        path_index: dict[str, str],
        extractions: dict[str, _FileExtraction],
        fallback: _ImportFallbackIndex,
    ) -> str | None:
        """Id of ``symbol`` defined at top level of an imported module
        (the ``from x import helper`` + ``helper()`` case)."""
        for imp in imported_modules:
            target_rel = _resolve_import(imp, rel, path_index, fallback)
            if target_rel is None:
                continue
            target_ex = extractions.get(target_rel)
            if target_ex is not None and symbol in target_ex.top_level_defs:
                node = target_ex.qname_to_node.get(symbol)
                if node is not None:
                    return node.id
        return None


def stamp_freshness(store: CodeGraphStore, main_store: MainStoreMeta, project: str) -> int:
    """Write sidecar freshness meta + bump the MAIN-DB epoch.

    Called only when an index actually (re)published — the staleness
    check reads, it never bumps. Returns the new epoch value."""
    store.set_meta(f"last_indexed:{len(project)}:{project}", datetime.now(UTC).isoformat())
    return bump_project_graph_epoch(main_store, project)
