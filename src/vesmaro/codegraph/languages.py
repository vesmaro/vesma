"""Language registry for the code graph indexer (ADR-0032 PG-0 slice 2).

Wave 1 was Python-only (ArchCom 2026-09-28 §1); issue #470 adds Go.
The registry maps a file extension to the language that parses it;
everything else is simply NOT indexed — an unknown extension never
falls through to a wrong parser. Adding a language is one
``LanguageSpec`` entry plus its import resolver; the indexer looks
everything up here and dispatches to the language's file parser.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class LanguageSpec:
    """One registered language.

    Fields:
        name: Store-side ``lang`` value (``project_nodes.lang``).
        extensions: File extensions (lowercase, dot-prefixed) this
            language owns. The ``file_surface`` allowlist is derived
            from the union over all specs.
        language_factory: Zero-arg factory returning the tree-sitter
            ``Language`` object (imports the grammar package lazily —
            tree-sitter is a CORE dependency since the owner
            default-on decision of 2026-09-28; the lazy import keeps
            THIS module cheap to import and surfaces a missing
            grammar at first index, not at server startup).
        import_resolver: Callable mapping a dotted module name to a
            repo-relative source path candidate list, or ``None`` when
            the language has no resolution story yet.
        test_file_predicates: Name predicates marking a file as a test
            file for the TESTS edge heuristic.
    """

    name: str
    extensions: tuple[str, ...]
    language_factory: Callable[[], Any]
    import_resolver: Callable[[str], list[str]] | None = None
    test_file_predicates: tuple[Callable[[str], bool], ...] = ()


def _python_language() -> Any:
    import tree_sitter_python
    from tree_sitter import Language

    return Language(tree_sitter_python.language())


def _python_import_resolver(module: str) -> list[str]:
    """Dotted module name -> repo-relative path candidates.

    ``a.b`` maps to ``a/b.py`` and ``a/b/__init__.py``. This is the
    sys.path-free half of the resolution; the indexer layers
    relative-import and repo-root heuristics on top (see
    ``indexer._resolve_python_import``).
    """
    parts = module.split(".")
    return ["/".join(parts) + ".py", "/".join(parts) + "/__init__.py"]


def _python_test_file(name: str) -> bool:
    return name.startswith("test_") or name.endswith("_test.py")


#: Python (wave 1 — the first first-class language).
PYTHON = LanguageSpec(
    name="python",
    extensions=(".py",),
    language_factory=_python_language,
    import_resolver=_python_import_resolver,
    test_file_predicates=(_python_test_file,),
)


def _go_language() -> Any:
    import tree_sitter_go
    from tree_sitter import Language

    return Language(tree_sitter_go.language())


def _go_import_resolver(import_path: str) -> list[str]:
    """Go import path -> candidate package DIRECTORIES (repo-relative).

    A Go import path (``example.com/org/repo/pkg/api``) maps onto a
    directory inside the repo only as a SUFFIX match — the module
    prefix outside the repo is unknown. Every trailing-segments
    candidate is returned (``pkg/api``, ``api``, ...) longest first;
    the indexer picks the longest directory that actually exists in
    the indexed tree. Only repo-internal packages can match — stdlib
    and external paths resolve to nothing, silently.
    """
    parts = [p for p in import_path.split("/") if p]
    return ["/".join(parts[i:]) for i in range(len(parts))]


def _go_test_file(name: str) -> bool:
    return name.endswith("_test.go")


#: Go (issue #470) — the second first-class language.
GO = LanguageSpec(
    name="go",
    extensions=(".go",),
    language_factory=_go_language,
    import_resolver=_go_import_resolver,
    test_file_predicates=(_go_test_file,),
)

#: All registered languages, keyed by name.
LANGUAGES: dict[str, LanguageSpec] = {spec.name: spec for spec in (PYTHON, GO)}

#: Extension -> language lookup (denylist/allowlist boundary input).
EXTENSION_TO_LANGUAGE: dict[str, LanguageSpec] = {
    ext: spec for spec in LANGUAGES.values() for ext in spec.extensions
}


def language_for_path(path: str) -> LanguageSpec | None:
    """Language spec for a repo-relative path, or ``None`` when the
    extension is not in the indexed surface."""
    dot = path.rfind(".")
    slash = path.rfind("/")
    if dot == -1 or dot < slash:
        return None
    return EXTENSION_TO_LANGUAGE.get(path[dot:].lower())


def is_test_file(spec: LanguageSpec, path: str) -> bool:
    """Whether a file matches the language's test-file predicates."""
    name = path.rsplit("/", 1)[-1]
    return any(pred(name) for pred in spec.test_file_predicates)
