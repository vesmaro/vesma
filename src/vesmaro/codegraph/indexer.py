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
* Go (issue #470): ``function_declaration`` → Function,
  ``method_declaration`` → Method (qname ``<pkg>.<Type>.<name>``),
  struct/interface ``type_spec`` → Class, other types/aliases → Type.
  Symbol qnames carry the dotted package-directory prefix — Go
  identifier resolution is package-scoped, and unambiguous qnames are
  what trace/search resolve against. Import paths (the ONE string
  content ever read — structural, used for resolution, never stored)
  resolve by longest repo-directory suffix match; unresolved paths
  produce NO edge. Call honesty: a PLAIN identifier call resolving to
  a package-level func/type is CALLS (Go package scope makes the name
  unique); ``pkg.Ident`` with a resolved import is CALLS;
  ``obj.Method`` is USES heuristic (no type inference — the receiver
  type is unknown); type references (composite literals, new/make
  type arguments) are USES heuristic (name-matched, not type-proven).

Limits (PG7, fail-closed): ``index_max_files``/``index_max_source_mb``
are checked BEFORE the publish transaction; a breach raises
:class:`IndexLimitError` and the project keeps its previous graph —
a partial graph is never published.

Call attribution: a CALLS/USES edge is attributed to the
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
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from vesmaro.codegraph.file_surface import FileSurface, SurfaceFile
from vesmaro.codegraph.languages import (
    GO,
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
    #: (alias-or-None, import path) pairs — Go only (Python dotted
    #: names carry no alias; the Go qualifier is alias or last segment).
    import_pairs: list[tuple[str | None, str]] = field(default_factory=list)
    #: (type head, enclosing-definition qname or None) pairs — Go type
    #: references from composite literals and new/make type arguments.
    type_refs: list[tuple[str, str | None]] = field(default_factory=list)
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


def _go_dir(rel_path: str) -> str:
    """Package directory of a Go file: ``a/b/c.go`` -> ``a/b``, root -> ``""``."""
    return rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""


def _go_package_prefix(rel_path: str) -> str:
    """Dotted package-directory prefix for Go symbol qnames:
    ``a/b/c.go`` -> ``a.b.``, root files carry no prefix."""
    directory = _go_dir(rel_path)
    return directory.replace("/", ".") + "." if directory else ""


def _go_type_shape(src: bytes, type_node: Any) -> str:
    """Signature shape of ONE Go type expression — identifiers and type
    names only, space-joined (the Python ``_sig_part_text`` flattening:
    punctuation such as ``*``/``[]`` is structural, not a name)."""
    if type_node is None or not type_node.is_named or type_node.type in _LITERAL_TYPES:
        return ""
    if type_node.type == "qualified_type":
        pkg = type_node.child_by_field_name("package")
        name = type_node.child_by_field_name("name")
        return (
            f"{_text(src, pkg)}.{_text(src, name)}" if pkg is not None and name is not None else ""
        )
    if type_node.type in ("identifier", "type_identifier", "package_identifier"):
        return _text(src, type_node)
    inner = [_go_type_shape(src, c) for c in type_node.named_children]
    return " ".join(s for s in inner if s)


def _go_signature_of(src: bytes, def_node: Any) -> str | None:
    """Signature SHAPE of a Go function/method — parameter names and
    type names only. Go has no parameter defaults, so there is nothing
    to drop; comments are not named children and never contribute."""
    params_node = def_node.child_by_field_name("parameters")
    if params_node is None or params_node.type != "parameter_list":
        return None
    parts: list[str] = []
    for p in params_node.named_children:
        name_node = p.child_by_field_name("name")
        name = _text(src, name_node) if name_node is not None else ""
        shape = _go_type_shape(src, p.child_by_field_name("type"))
        text = f"{name} {shape}".strip()
        if text:
            parts.append(text)
    return "(" + ", ".join(parts) + ")" if parts else None


def _go_receiver_type(src: bytes, method_node: Any) -> str | None:
    """The receiver TYPE name of a method declaration — the first type
    identifier inside the receiver list (``*T``/``T``/generic forms
    unwrap to ``T``); ``None`` when the receiver is unparseable."""
    receiver = method_node.child_by_field_name("receiver")

    def first_type_identifier(node: Any) -> str | None:
        if node.type == "type_identifier":
            return _text(src, node)
        for child in node.named_children:
            found = first_type_identifier(child)
            if found is not None:
                return found
        return None

    if receiver is None:
        return None
    for param in receiver.named_children:
        type_node = param.child_by_field_name("type")
        if type_node is not None:
            found = first_type_identifier(type_node)
            if found is not None:
                return found
    return None


def _go_string_text(src: bytes, literal: Any) -> str | None:
    """The CONTENT of one Go string literal — used ONLY for import
    paths (structural resolution input; it never reaches the store).
    Every other string in a Go file stays unread (PG1)."""
    for child in literal.named_children:
        if child.type in ("interpreted_string_literal_content", "raw_string_literal_content"):
            return _text(src, child)
    text = _text(src, literal).strip('"`')
    return text or None


def _go_embedded_bases(type_node: Any, src: bytes) -> list[str]:
    """Embedded type names of a struct/interface body — the Go shape of
    «base classes»: nameless struct fields (``pkg.Base`` / ``Base``)
    and interface ``type_elem`` entries. Named fields and interface
    ``method_elem`` signatures are skipped."""
    bases: list[str] = []
    if type_node.type == "struct_type":
        for field in type_node.named_children:
            if field.type != "field_declaration_list":
                continue
            for entry in field.named_children:
                if (
                    entry.type != "field_declaration"
                    or entry.child_by_field_name("name") is not None
                ):
                    continue
                entry_type = entry.child_by_field_name("type")
                if entry_type is not None and entry_type.type in (
                    "type_identifier",
                    "qualified_type",
                ):
                    bases.append(_text(src, entry_type))
    elif type_node.type == "interface_type":
        for elem in type_node.named_children:
            if elem.type != "type_elem":
                continue
            for entry in elem.named_children:
                if entry.type in ("type_identifier", "qualified_type"):
                    bases.append(_text(src, entry))
    return bases


def _go_collect_type_names(
    src: bytes, node: Any, out: list[tuple[str, str | None]], enclosing: str | None
) -> None:
    """Collect the type names named inside ONE type expression (the
    ``new(T)``/``make(...T...)`` argument): ``*T``, ``[]T``,
    ``map[K]V`` contribute each named type they mention."""
    if not node.is_named or node.type in _LITERAL_TYPES:
        return
    if node.type in ("type_identifier", "qualified_type"):
        out.append((_text(src, node), enclosing))
        return
    for child in node.named_children:
        _go_collect_type_names(src, child, out, enclosing)


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


class GoFileParser:
    """Parses ONE Go file into nodes/edges (pre-resolution) — the Go
    twin of :class:`PythonFileParser`, same extraction contract:

    * File + Module nodes; the Module is the Go PACKAGE: qname is the
      package directory (``pkg/util``), name is the ``package`` clause
      identifier (falling back to the directory basename). One Module
      node per file — multi-file packages share the qname.
    * ``function_declaration`` → Function, ``method_declaration`` →
      Method (qname ``<pkg>.<Receiver>.<name>``), struct/interface
      ``type_spec`` → Class, other defined types and ``type_alias`` →
      Type. Symbol qnames carry the dotted package-directory prefix —
      Go resolution is package-scoped and trace resolves on qnames.
    * Package-level names (funcs + types, NOT methods — Go methods are
      type-scoped) land in ``top_level_defs`` for the resolution pass;
      embedded struct fields / interface ``type_elem`` land in
      ``class_bases`` for INHERITS.
    * Import paths are collected with their aliases. The path string is
      the ONE string content this parser ever reads — structural
      resolution input that never reaches the store (PG1: every other
      string/comment node type is skipped by the traversal).
    * ``call_expression`` heads (identifier / selector text) and type
      references (composite-literal types, ``new``/``make`` type
      arguments) are collected as (head, enclosing qname) pairs for the
      project-wide honesty pass — see the module docstring for what
      resolves to CALLS vs heuristic USES vs nothing.

    Interface method signatures (``method_elem``) are NOT separate
    nodes — the interface is indexed whole, like a Python class body.
    Function literals (closures) are not nodes; their calls attribute
    to the enclosing top-level function.
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
        package_name = self._package_name(root, source, rel_path)
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
                    name=package_name,
                    qname=_go_dir(rel_path) or ".",
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
        self._walk(root, source, project, rel_path, extraction, None)
        return extraction

    # ── traversal ──────────────────────────────────────────────────────────

    def _walk(
        self,
        block: Any,
        source: bytes,
        project: str,
        rel_path: str,
        extraction: _FileExtraction,
        enclosing: str | None,
    ) -> None:
        """Single disciplined traversal: named children only; bodies of
        functions/methods recurse with the definition's qname as the
        enclosing scope; comment/string node types are never read."""
        for child in block.named_children:
            ctype = child.type
            if ctype == "import_declaration":
                self._collect_imports(child, source, extraction)
                continue
            if ctype in ("function_declaration", "method_declaration"):
                qname = self._define_function(child, source, project, rel_path, extraction)
                body = child.child_by_field_name("body")
                if body is not None:
                    self._walk(body, source, project, rel_path, extraction, qname)
                continue
            if ctype == "type_declaration":
                self._define_types(child, source, project, rel_path, extraction)
                continue
            if ctype == "call_expression":
                self._collect_call(child, source, extraction, enclosing)
            elif ctype == "composite_literal":
                self._collect_composite(child, source, extraction, enclosing)
            self._walk(child, source, project, rel_path, extraction, enclosing)

    # ── definitions ────────────────────────────────────────────────────────

    def _define_function(
        self,
        node: Any,
        source: bytes,
        project: str,
        rel_path: str,
        extraction: _FileExtraction,
    ) -> str:
        """Emit the Function/Method node; return its qname (the
        enclosing scope for the body's references)."""
        spec = language_for_path(rel_path)
        assert spec is not None
        name_node = node.child_by_field_name("name")
        name = _text(source, name_node) if name_node is not None else ""
        if node.type == "method_declaration":
            receiver = _go_receiver_type(source, node)
            qname = f"{_go_package_prefix(rel_path)}{receiver}.{name}" if receiver else name
            kind = "Method"
        else:
            qname = f"{_go_package_prefix(rel_path)}{name}"
            kind = "Function"
            if name:
                extraction.top_level_defs.add(name)
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
            signature=_go_signature_of(source, node),
            metadata={"poisoned": True} if extraction.poisoned else None,
        )
        extraction.nodes.append(graph_node)
        if qname:
            extraction.qname_to_node[qname] = graph_node
        extraction.edges.append(
            CodeGraphEdge(from_id=_file_id(project, rel_path), to_id=graph_node.id, kind="DEFINES")
        )
        return qname

    def _define_types(
        self,
        decl: Any,
        source: bytes,
        project: str,
        rel_path: str,
        extraction: _FileExtraction,
    ) -> None:
        """``type_declaration`` → one node per ``type_spec`` /
        ``type_alias``: struct/interface → Class, everything else →
        Type. Embedded struct fields and interface ``type_elem`` feed
        the INHERITS pass."""
        spec = language_for_path(rel_path)
        assert spec is not None
        prefix = _go_package_prefix(rel_path)
        for spec_node in decl.named_children:
            name_node = spec_node.child_by_field_name("name")
            if name_node is None:
                continue
            name = _text(source, name_node)
            if spec_node.type == "type_alias":
                kind = _KIND_TYPE
                bases: list[str] = []
            else:
                type_node = spec_node.child_by_field_name("type")
                composite = type_node is not None and type_node.type in (
                    "struct_type",
                    "interface_type",
                )
                kind = _KIND_CLASS if composite else _KIND_TYPE
                bases = _go_embedded_bases(type_node, source) if composite else []
            qname = f"{prefix}{name}"
            graph_node = CodeGraphNode(
                id=_node_id(project, rel_path, name, spec_node.start_point[0] + 1),
                project=project,
                kind=kind,
                name=name,
                qname=qname,
                path=rel_path,
                start_line=spec_node.start_point[0] + 1,
                end_line=spec_node.end_point[0] + 1,
                lang=spec.name,
                signature=None,
                metadata={"poisoned": True} if extraction.poisoned else None,
            )
            extraction.nodes.append(graph_node)
            extraction.qname_to_node[qname] = graph_node
            extraction.top_level_defs.add(name)
            if bases:
                extraction.class_bases[qname] = bases
            extraction.edges.append(
                CodeGraphEdge(
                    from_id=_file_id(project, rel_path), to_id=graph_node.id, kind="DEFINES"
                )
            )

    # ── reference collection ───────────────────────────────────────────────

    def _collect_imports(self, decl: Any, source: bytes, extraction: _FileExtraction) -> None:
        """import_spec paths (+ aliases). The path string is read for
        resolution only — it never reaches the store (PG1); blank and
        dot aliases are dropped (blank = side-effect import with no
        qualifier, dot-imports have no package qualifier at all)."""

        def specs(node: Any) -> Iterator[Any]:
            for child in node.named_children:
                if child.type == "import_spec":
                    yield child
                elif child.type == "import_spec_list":
                    yield from specs(child)

        for imp in specs(decl):
            path_node = imp.child_by_field_name("path")
            if path_node is None:
                continue
            path = _go_string_text(source, path_node)
            if not path:
                continue
            alias_node = imp.child_by_field_name("name")
            alias = _text(source, alias_node) if alias_node is not None else None
            if alias in (".", "_"):
                alias = None
            extraction.imports.append(path)
            extraction.import_pairs.append((alias, path))

    def _collect_call(
        self,
        node: Any,
        source: bytes,
        extraction: _FileExtraction,
        enclosing: str | None,
    ) -> None:
        """``call_expression`` head +, for ``new``/``make``, the type
        argument's type names. The head is an identifier, a selector or
        a conversion's type name — never an argument literal (PG1)."""
        fn = node.child_by_field_name("function")
        if fn is not None and fn.type in ("identifier", "selector_expression", "type_identifier"):
            extraction.call_refs.append((_text(source, fn), enclosing))
        if fn is not None and fn.type == "identifier" and _text(source, fn) in ("new", "make"):
            args = node.child_by_field_name("arguments")
            if args is not None and args.named_children:
                _go_collect_type_names(
                    source, args.named_children[0], extraction.type_refs, enclosing
                )

    def _collect_composite(
        self,
        node: Any,
        source: bytes,
        extraction: _FileExtraction,
        enclosing: str | None,
    ) -> None:
        """``&Foo{…}`` / ``pkg.Foo{…}`` — the canonical Go construction
        site; the composite's type name becomes a type reference."""
        type_node = node.child_by_field_name("type")
        if type_node is not None and type_node.type in ("type_identifier", "qualified_type"):
            extraction.type_refs.append((_text(source, type_node), enclosing))

    def _package_name(self, root: Any, source: bytes, rel_path: str) -> str:
        """The ``package`` clause identifier, falling back to the
        directory basename (a parse-broken file still gets a name)."""
        for child in root.named_children:
            if child.type == "package_clause":
                ident = child.child_by_field_name("name")
                if ident is not None:
                    return _text(source, ident)
        return _go_dir(rel_path).rsplit("/", 1)[-1] or rel_path


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


def _go_package_dir(import_path: str, go_dirs: dict[str, list[str]]) -> str | None:
    """Longest repo-directory suffix match for one Go import path.

    The module prefix outside the repo is unknown, so the path matches
    a package only as a trailing-segments suffix (``example.com/o/r/pkg/api``
    tries ``pkg/api`` after ``api``); the LONGEST existing directory
    wins (most specific). Only directories that contain indexed .go
    files can match — stdlib/external imports resolve to ``None`` and
    produce no edge, silently.
    """
    parts = [p for p in import_path.split("/") if p]
    for i in range(len(parts)):
        candidate = "/".join(parts[i:])
        if candidate in go_dirs:
            return candidate
    return None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _FileParser(Protocol):
    """The per-language file-parser surface the indexer dispatches to."""

    def parse(self, project: str, rel_path: str, source: bytes) -> _FileExtraction: ...


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
        self._parsers: dict[str, _FileParser] = {}
        # Issue #449: the allowlist is fixed for the indexer's lifetime
        # (config is immutable here), so the parsers are built with it.
        self._secret_allowlist = tuple(self._config.secret_allowlist)

    def _parser_for(self, spec: LanguageSpec) -> _FileParser:
        cached = self._parsers.get(spec.name)
        if cached is None:
            language = spec.language_factory()
            parser: _FileParser
            if spec.name == GO.name:
                parser = GoFileParser(language, self._secret_allowlist)
            else:
                parser = PythonFileParser(language, self._secret_allowlist)
            self._parsers[spec.name] = parser
            return parser
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
        surface = FileSurface(root, self._config.exclude_globs).collect()
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

        Go (issue #470) runs the same pass shape over package indexes:
        package dirs → package-level defs (merged across a package's
        files), per-file import qualifiers → package dirs (longest
        repo-directory suffix match). Plain-identifier calls and
        ``pkg.Ident`` calls resolve to CALLS; method and type-name
        references resolve to heuristic USES; everything else is
        silently dropped.
        """
        path_index: dict[str, str] = {sf.rel_path: sf.rel_path for sf in surface}
        module_index: dict[str, str] = {_module_qname(sf.rel_path): sf.rel_path for sf in surface}
        fallback = _ImportFallbackIndex(module_index, os.path.basename(os.path.abspath(root)))
        qname_index: dict[str, CodeGraphNode] = {}
        for ex in extractions.values():
            for qname, node in ex.qname_to_node.items():
                qname_index[qname] = node

        go_dirs, go_packages, go_imports = self._go_indexes(surface, extractions)
        go_names: dict[str, list[CodeGraphNode]] = {}
        for package_defs in go_packages.values():
            for name, node in package_defs.items():
                go_names.setdefault(name, []).append(node)

        nodes: list[CodeGraphNode] = []
        edges: list[CodeGraphEdge] = []
        for ex in extractions.values():
            nodes.extend(ex.nodes)
            edges.extend(ex.edges)

        # IMPORTS (module-to-module, alias-free).
        for rel, ex in extractions.items():
            mid = _module_id(project, rel)
            spec = language_for_path(rel)
            if spec is not None and spec.name == GO.name:
                # Go: the edge lands on the package's first (sorted)
                # file's Module node — the package is the semantic unit;
                # per-file modules are a store-shape artifact.
                for _, import_path in ex.import_pairs:
                    package_dir = _go_package_dir(import_path, go_dirs)
                    if package_dir is None:
                        continue  # stdlib/external/unmatched: NO edge
                    representative = go_dirs[package_dir][0]
                    if representative == rel:
                        continue
                    edges.append(
                        CodeGraphEdge(
                            from_id=mid,
                            to_id=_module_id(project, representative),
                            kind="IMPORTS",
                        )
                    )
                continue
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
        for rel, ex in extractions.items():
            for qname, bases in ex.class_bases.items():
                class_node = ex.qname_to_node.get(qname)
                if class_node is None:
                    continue
                spec = language_for_path(rel)
                go_embed = spec is not None and spec.name == GO.name
                for base in bases:
                    if go_embed:
                        # Go embedding: qualified ``pkg.Base`` resolves
                        # through the file's import qualifiers, a plain
                        # name through the own package first, then a
                        # PROJECT-UNIQUE name (an ambiguous name never
                        # invents an edge).
                        base_node = self._go_base_node(base, rel, go_packages, go_imports, go_names)
                    else:
                        base_node = qname_index.get(base) or qname_index.get(
                            base.rsplit(".", 1)[-1]
                        )
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
            if spec.name == GO.name:
                tested_rel = self._go_tested_target(sf.rel_path, go_dirs)
            else:
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
            spec = language_for_path(rel)
            if spec is not None and spec.name == GO.name:
                edges.extend(self._resolve_go_refs(rel, ex, mid, go_packages, go_imports))
                continue
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

    # ── Go resolution (issue #470) ─────────────────────────────────────────

    def _go_indexes(
        self,
        surface: list[SurfaceFile],
        extractions: dict[str, _FileExtraction],
    ) -> tuple[
        dict[str, list[str]], dict[str, dict[str, CodeGraphNode]], dict[str, dict[str, str]]
    ]:
        """Go resolution indexes, built once per run:

        * dirs — package directory -> sorted .go rel paths;
        * packages — package directory -> package-level def name -> node
          (funcs + types merged across the package's files; Go package
          scope makes these names unique — methods are type-scoped and
          excluded);
        * imports — file rel path -> call qualifier -> package dir
          (alias if the import names one, else the path's last segment).
        """
        go_dirs: dict[str, list[str]] = {}
        for sf in surface:
            spec = language_for_path(sf.rel_path)
            if spec is not None and spec.name == GO.name:
                go_dirs.setdefault(_go_dir(sf.rel_path), []).append(sf.rel_path)
        for rels in go_dirs.values():
            rels.sort()
        go_packages: dict[str, dict[str, CodeGraphNode]] = {}
        go_imports: dict[str, dict[str, str]] = {}
        for rel, ex in extractions.items():
            spec = language_for_path(rel)
            if spec is None or spec.name != GO.name:
                continue
            bucket = go_packages.setdefault(_go_dir(rel), {})
            for node in ex.qname_to_node.values():
                if node.name in ex.top_level_defs:
                    bucket[node.name] = node
            qualifiers: dict[str, str] = {}
            for alias, import_path in ex.import_pairs:
                package_dir = _go_package_dir(import_path, go_dirs)
                if package_dir is None:
                    continue
                qualifiers[alias or import_path.rsplit("/", 1)[-1]] = package_dir
            go_imports[rel] = qualifiers
        return go_dirs, go_packages, go_imports

    def _go_tested_target(self, test_rel: str, go_dirs: dict[str, list[str]]) -> str | None:
        """``foo_test.go`` tests its OWN package: the heuristic target is
        the first (sorted) non-test .go file in the same directory.
        A test-only package gets NO edge."""
        for rel in go_dirs.get(_go_dir(test_rel), ()):
            if rel != test_rel and not is_test_file(GO, rel):
                return rel
        return None

    def _go_base_node(
        self,
        base: str,
        rel: str,
        go_packages: dict[str, dict[str, CodeGraphNode]],
        go_imports: dict[str, dict[str, str]],
        go_names: dict[str, list[CodeGraphNode]],
    ) -> CodeGraphNode | None:
        """The node an embedded type name points at: a qualified
        embedding (``pkg.Base``) resolves through the embedding file's
        import qualifiers; a plain name through the own package first,
        then a PROJECT-UNIQUE name (ambiguous → ``None`` — no edge)."""
        if "." in base:
            qual, _, tail = base.rpartition(".")
            return go_packages.get(go_imports.get(rel, {}).get(qual, ""), {}).get(tail)
        own = go_packages.get(_go_dir(rel), {}).get(base)
        if own is not None:
            return own
        candidates = go_names.get(base, ())
        return candidates[0] if len(candidates) == 1 else None

    def _resolve_go_refs(
        self,
        rel: str,
        ex: _FileExtraction,
        mid: str,
        go_packages: dict[str, dict[str, CodeGraphNode]],
        go_imports: dict[str, dict[str, str]],
    ) -> list[CodeGraphEdge]:
        """CALLS/USES edges for ONE Go file — the honesty ladder:

        * ``ident(…)`` hitting a package-level def → CALLS (proven: Go
          package scope makes the name unique — functions AND type
          conversions, mirroring the Python class-ctor rule);
        * ``pkg.Ident(…)`` with ``pkg`` a resolved import qualifier and
          ``Ident`` a package-level def → CALLS (proven);
        * anything else (``obj.Method(…)`` — the receiver type is not
          inferred) falling back to a known in-file symbol tail → USES
          heuristic; unknown heads get NO edge;
        * type references (composite literals, new/make type args)
          name-matching a package-level type (own or imported package)
          → USES heuristic (the name match is not a type identity).
        """
        edges: list[CodeGraphEdge] = []
        package_defs = go_packages.get(_go_dir(rel), {})
        qualifiers = go_imports.get(rel, {})
        for head, enclosing in ex.call_refs:
            caller = self._caller_id(ex, mid, enclosing)
            if "." not in head:
                target = package_defs.get(head)
                if target is not None and target.id != caller:
                    edges.append(CodeGraphEdge(from_id=caller, to_id=target.id, kind="CALLS"))
                    continue
                known = self._known_symbol(ex, head, head)
                if known is not None and known.id != caller:
                    edges.append(
                        CodeGraphEdge(
                            from_id=caller, to_id=known.id, kind="USES", provenance="heuristic"
                        )
                    )
                continue
            qual, _, tail = head.rpartition(".")
            imported_defs = go_packages.get(qualifiers.get(qual, ""), {})
            target = imported_defs.get(tail)
            if target is not None and target.id != caller:
                edges.append(CodeGraphEdge(from_id=caller, to_id=target.id, kind="CALLS"))
                continue
            known = self._known_symbol(ex, head, tail)
            if known is not None and known.id != caller:
                edges.append(
                    CodeGraphEdge(
                        from_id=caller, to_id=known.id, kind="USES", provenance="heuristic"
                    )
                )
        for type_head, enclosing in ex.type_refs:
            caller = self._caller_id(ex, mid, enclosing)
            if "." in type_head:
                qual, _, tail = type_head.rpartition(".")
                target = go_packages.get(qualifiers.get(qual, ""), {}).get(tail)
            else:
                target = package_defs.get(type_head)
            if target is not None and target.id != caller:
                edges.append(
                    CodeGraphEdge(
                        from_id=caller, to_id=target.id, kind="USES", provenance="heuristic"
                    )
                )
        return edges

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
