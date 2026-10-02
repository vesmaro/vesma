"""Docs canon: the 5.1.0 project-graph user docs are present, cross-linked
and language-symmetric.

The 5.1.0 wave shipped the project graph with tools/REST/watch docs, but the
dedicated user guide was missing (the owner's complaint: «whole new mechanics
— and nothing in the docs»). This pin forbids the whole class:

- the EN and RU guides both exist;
- the landing, feature map and getting-started pages reference them in BOTH
  languages;
- the EN and RU guides keep a 1:1 section structure (same heading count and
  levels);
- internal doc links resolve on disk (no dead .md targets);
- config.example.yaml's code_graph block stays in lockstep with the real
  ``CodeGraphConfig`` fields — every field documented, no phantom keys
  (a phantom auto_index key would be a docs-lie this test catches);
- the user docs DESCRIBE the PG-0.5 native auto-indexing (guide section,
  getting-started auto step, integration-guide zero-harness note) and the
  stale 5.1.0-era claim «nothing indexes itself» never returns.

Offline by construction: only local file reads.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

GUIDES = {
    "en": REPO / "docs" / "en" / "user" / "project-graph.md",
    "ru": REPO / "docs" / "ru" / "user" / "project-graph.md",
}

#: (file, needle) pairs — every page must reference the guide.
REFERRERS = [
    ("docs/en/index.md", "user/project-graph.md"),
    ("docs/ru/index.md", "user/project-graph.md"),
    ("docs/en/features.md", "user/project-graph.md"),
    ("docs/ru/features.md", "user/project-graph.md"),
    ("docs/en/user/getting-started.md", "project-graph.md"),
    ("docs/ru/user/getting-started.md", "project-graph.md"),
]

_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+\.md(?:#[^)\s]*)?)\)")
_HEADING = re.compile(r"^(#{1,6}) ", re.MULTILINE)


def _read(rel: str | Path) -> str:
    path = REPO / rel
    assert path.exists(), f"missing file: {path}"
    return path.read_text(encoding="utf-8")


# ── Existence and cross-references ───────────────────────────────────────────


def test_project_graph_guides_exist_in_both_languages() -> None:
    for lang, path in GUIDES.items():
        assert path.is_file(), f"{lang}: project-graph.md guide missing"
        text = path.read_text(encoding="utf-8")
        assert "# " in text and "## " in text, f"{lang}: guide has no structure"
        for lang_other, other in GUIDES.items():
            if lang_other != lang:
                assert str(other.relative_to(REPO)).split("docs/")[-1] in text, (
                    f"{lang}: guide does not link its {lang_other} twin"
                )


def test_landing_features_getting_started_reference_the_guide() -> None:
    for rel, needle in REFERRERS:
        text = _read(rel)
        assert needle in text, f"{rel}: no reference to {needle}"


# ── EN/RU structural parity ──────────────────────────────────────────────────


def test_en_ru_guides_have_identical_section_structure() -> None:
    """Sections must be 1:1: same number of headings at every level."""
    en = _HEADING.findall(GUIDES["en"].read_text(encoding="utf-8"))
    ru = _HEADING.findall(GUIDES["ru"].read_text(encoding="utf-8"))
    assert en, "EN guide parsed no headings"
    assert len(en) == len(ru), f"EN/RU heading count drift: EN={len(en)} RU={len(ru)}"
    for i, (a, b) in enumerate(zip(en, ru, strict=True)):
        assert a == b, f"heading level drift at #{i}: EN {a!r} vs RU {b!r}"


# ── Internal links resolve ───────────────────────────────────────────────────


def test_project_graph_guides_internal_links_resolve() -> None:
    for lang, path in GUIDES.items():
        text = path.read_text(encoding="utf-8")
        for target in _MD_LINK.findall(text):
            rel, _, _anchor = target.partition("#")
            if not rel:  # same-page anchor — the target heading exists below
                continue
            resolved = (path.parent / rel).resolve()
            assert resolved.exists(), f"{lang}: dead link target {target!r}"


def test_refererring_pages_md_links_resolve() -> None:
    pages = sorted({REPO / rel for rel, _ in REFERRERS})
    pages += [
        REPO / "docs" / "en" / "user" / "integration-guide.md",
        REPO / "docs" / "ru" / "user" / "integration-guide.md",
    ]
    for page in pages:
        text = page.read_text(encoding="utf-8")
        for target in _MD_LINK.findall(text):
            rel, _, _anchor = target.partition("#")
            if not rel:
                continue
            resolved = (page.parent / rel).resolve()
            assert resolved.exists(), f"{page.name}: dead link target {target!r}"


# ── PG-0.5 native auto-indexing is described, not denied ────────────────────


#: (file, needle) pairs — the docs must carry the auto-indexing mechanics.
#: Keys mirror ``CodeGraphConfig`` (src/vesma/config.py) and the audit
#: reasons in ``src/vesma/codegraph/autoindex.py``.
AUTO_INDEX_NEEDLES = [
    ("docs/en/user/project-graph.md", "auto_index"),
    ("docs/ru/user/project-graph.md", "auto_index"),
    ("docs/en/user/project-graph.md", "Native auto-indexing"),
    ("docs/ru/user/project-graph.md", "Нативная авто-индексация"),
    ("docs/en/user/project-graph.md", "auto-register-reused"),
    ("docs/ru/user/project-graph.md", "auto-register-reused"),
    ("docs/en/user/getting-started.md", "indexes itself"),
    ("docs/ru/user/getting-started.md", "индексируется сам"),
    ("docs/en/user/getting-started.md", "auto_index"),
    ("docs/ru/user/getting-started.md", "auto_index"),
    ("docs/en/user/integration-guide.md", "auto-register"),
    ("docs/ru/user/integration-guide.md", "авторегистрировать"),
]

#: Claims that were honest for 5.1.0 and became lies when PG-0.5 landed —
#: they must not return to the guides.
STALE_NO_AUTO_CLAIMS = {
    "docs/en/user/project-graph.md": ["nothing indexes itself"],
    "docs/ru/user/project-graph.md": ["ничего не индексируется само"],
}


def test_user_docs_describe_native_auto_indexing() -> None:
    for rel, needle in AUTO_INDEX_NEEDLES:
        assert needle in _read(rel), (
            f"{rel}: no mention of {needle!r} — PG-0.5 auto-indexing docs regressed"
        )


def test_user_docs_do_not_pin_absence_of_auto_indexing() -> None:
    for rel, claims in STALE_NO_AUTO_CLAIMS.items():
        text = _read(rel)
        for claim in claims:
            assert claim not in text, f"{rel}: stale pre-PG-0.5 claim {claim!r} returned"


# ── config.example.yaml ↔ CodeGraphConfig lockstep ──────────────────────────


def _config_py_fields() -> set[str]:
    src = (REPO / "src" / "vesma" / "config.py").read_text(encoding="utf-8")
    block = re.search(r"class CodeGraphConfig\(BaseModel\):.*?(?=\nclass )", src, re.DOTALL)
    assert block, "CodeGraphConfig not found in config.py"
    return set(re.findall(r"^    ([a-z_]+):", block.group(0), re.MULTILINE))


def _example_block_keys(example: str) -> set[str]:
    """Keys of the ``code_graph:`` block, by linear line scan. A regex
    with a nested ``(...)+`` over indented lines is a backtracking bomb
    on this file — do not replace this with one."""
    keys: set[str] = set()
    inside = False
    for line in example.splitlines():
        if line == "code_graph:":
            inside = True
            continue
        if not inside:
            continue
        if line and not line[0].isspace():
            break  # next top-level block
        m = re.match(r" {2}([a-z_]+):", line)
        if m:
            keys.add(m.group(1))
    return keys


def test_config_example_code_graph_matches_codegraphconfig() -> None:
    """Every CodeGraphConfig field must be in the example block — and no
    phantom keys (documenting a flag the code does not parse is a lie)."""
    example = (REPO / "config.example.yaml").read_text(encoding="utf-8")
    assert "code_graph:" in example, "config.example.yaml has no code_graph block"
    example_keys = _example_block_keys(example)
    code_fields = _config_py_fields()
    missing = code_fields - example_keys
    phantom = example_keys - code_fields
    assert not missing, f"config.example.yaml omits CodeGraphConfig fields: {sorted(missing)}"
    assert not phantom, (
        f"config.example.yaml documents flags CodeGraphConfig does not have: {sorted(phantom)}"
    )
