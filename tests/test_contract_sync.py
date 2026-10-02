"""Contract-sync regression test: proto ``CompactRecord`` ↔ Pydantic ``CompactRecord``.

The ``federation.proto`` file (``mnemos/federation/proto/federation.proto``)
is the *source of truth* for the peer-to-peer federation contract and is
explicitly documented to mirror the Pydantic model
``vesma.compact.CompactRecord`` field-by-field (see the proto header
comment: *"Source of truth: mnemos/src/mnemos/compact.py::CompactRecord"*).

Today the two sides are kept in sync **manually**. This regression test
catches drift automatically: it parses the proto ``CompactRecord`` message
fields (name + proto type) with a lightweight regex (no ``protobuf``
runtime dependency), imports the Pydantic model, and asserts that the two
field sets agree on **name** and **type** semantics.

Why regex and not ``grpc_tools.protoc``? Adding a protobuf descriptor
parser as a test-only dependency is overkill for catching field drift.
The proto file is hand-maintained and follows a single-message-per-block
layout that a small parser handles cleanly. The test is intentionally
fragile-by-design: any structural surprise in the proto (nested messages,
oneofs inside ``CompactRecord``) would surface here as a parse failure
that a human must investigate.

Mapping table (proto type → expected Python type):
    string          → str
    bytes           → bytes
    int64 / int32   → int
    bool            → bool
    repeated <T>    → list[<python T>]

Reserved field numbers and names (``reserved 9, 10, 11; reserved
"schema_version";``) are intentionally excluded — they mark *forward-
compat slots that must never hold a field*, so they have no Pydantic
counterpart by design.

See:
    * ``federation/proto/federation.proto`` — proto contract (source of truth).
    * ``src/mnemos/compact.py`` — Pydantic ``CompactRecord`` model.
    * ArchCom 2026-07-17 federation contract §2.3 (Compact exchange format).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args, get_origin

import pytest

from vesma.compact import CompactRecord

# Type signatures are compared as strings (``"str"``, ``"list[str]"``) rather
# than live ``type`` objects. This keeps ``mypy --strict`` happy (no
# ``list[...]`` subscript on a ``type`` return) and yields clearer diff
# messages — the reader sees ``proto `repeated string` -> expected
# `list[str]`, got Pydantic `list[bytes]```` instead of opaque reprs.

# ── Paths ────────────────────────────────────────────────────────────────────

#: Repository root (``mnemos/`` — the dir that contains ``src/`` and ``tests/``).
_REPO_ROOT = Path(__file__).resolve().parent.parent

#: Path to the federation proto contract.
_PROTO_FILE = _REPO_ROOT / "federation" / "proto" / "federation.proto"

#: Proto-side-only CompactRecord fields — live wire fields with NO Pydantic
#: counterpart BY DESIGN (allow-listed here so the drift tests stay green).
#:
#:   * ``revision`` (ADR-0021 ruling Q10.6, archcom 2026-09-20): reserved
#:     in proto BEFORE S2 starts; carries STORAGE provenance (SQLite rowid
#:     at export) filled by the exporter at proto-marshal time, so it is
#:     deliberately absent from the hand-written Pydantic envelope — the
#:     mesh never parses it (criterion 1, dumb transport). See the
#:     ``CompactRecord.revision`` field comment in federation.proto.
_PROTO_ONLY_FIELDS: frozenset[str] = frozenset({"revision"})

# ── Proto parsing ────────────────────────────────────────────────────────────

#: Matches a single proto field declaration inside a message body.
#:
#: Captures (optionally repeated, type, name, number). Handles both
#: scalar fields (``string id = 1;``) and repeated fields
#: (``repeated string key_points = 5;``). Comment trailers (``// ...``)
#: are stripped before matching so they do not interfere.
_FIELD_RE = re.compile(
    r"""
    ^\s*
    (?P<repeated>repeated\s+)?
    (?P<type>[a-zA-Z_][\w.]*)    # field type (scalar, enum, or message)
    \s+
    (?P<name>[a-zA-Z_]\w*)       # field name
    \s*=\s*
    (?P<number>\d+)              # field number
    \s*;                         # terminating semicolon
    """,
    re.MULTILINE | re.VERBOSE,
)

#: Matches a ``reserved`` statement (field numbers or names) so the parser
#: can skip it — reserved slots have no Pydantic counterpart by design.
_RESERVED_RE = re.compile(
    r"^\s*reserved\s+[^;]+;",
    re.MULTILINE,
)


def _extract_message_block(proto_text: str, message_name: str) -> str:
    """Return the inner body of the proto ``message <name> { ... }`` block.

    The matcher is brace-aware: it scans from ``message <name> {`` to the
    matching closing brace at the *same* nesting level, so nested
    messages/enums inside ``CompactRecord`` (if any are added later) are
    handled without truncating the block early. Comments are stripped
    before matching to avoid brace-like characters inside ``//`` or
    ``/* */`` comments confusing the scanner.
    """
    # Strip block comments first, then line comments.
    cleaned = re.sub(r"/\*.*?\*/", "", proto_text, flags=re.DOTALL)
    cleaned = re.sub(r"//[^\n]*", "", cleaned)

    header = re.compile(rf"\bmessage\s+{re.escape(message_name)}\s*\{{")
    m = header.search(cleaned)
    if m is None:  # pragma: no cover - regression guard
        pytest.fail(
            f"Proto contract drift: message `{message_name}` not found in "
            f"{_PROTO_FILE}. The proto file structure changed — investigate "
            "before adjusting this test."
        )

    # Walk the brace depth from the opening ``{`` to its matching close.
    depth = 0
    start = m.end()  # position just after the opening ``{``
    for idx in range(start, len(cleaned)):
        char = cleaned[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == -1:
                return cleaned[start:idx]
    # pragma: no cover - only triggers on a malformed proto
    pytest.fail(
        f"Proto contract drift: message `{message_name}` in {_PROTO_FILE} "
        "has no matching closing brace — the proto file is malformed."
    )
    return ""  # for mypy: unreachable


def _parse_proto_fields(message_body: str) -> dict[str, str]:
    """Parse proto field declarations into a ``{name: proto_type}`` map.

    ``repeated <T>`` is normalised to ``repeated <T>`` (kept verbatim) so
    the type-compatibility check can distinguish scalar from list fields.
    ``reserved`` statements are stripped first — they must never map to a
    Pydantic field.
    """
    body = _RESERVED_RE.sub("", message_body)
    fields: dict[str, str] = {}
    for m in _FIELD_RE.finditer(body):
        proto_type = m.group("type")
        if m.group("repeated"):
            proto_type = f"repeated {proto_type}"
        name = m.group("name")
        # First declaration wins (proto forbids duplicate names, but be
        # defensive — a duplicate would itself be a proto-level bug).
        if name not in fields:
            fields[name] = proto_type
    return fields


# ── Pydantic model introspection ──────────────────────────────────────────────

#: Proto scalar type → expected Python type signature string. ``repeated <T>``
#: is handled separately (mapped to ``list[<py T>]``) by
#: :func:`_expected_python_type_sig`.
_PROTO_SCALAR_TO_PY_SIG: dict[str, str] = {
    "string": "str",
    "bytes": "bytes",
    "int64": "int",
    "int32": "int",
    "uint64": "int",
    "uint32": "int",
    "sint64": "int",
    "sint32": "int",
    "fixed64": "int",
    "fixed32": "int",
    "sfixed64": "int",
    "sfixed32": "int",
    "bool": "bool",
    "double": "float",
    "float": "float",
}


def _inner_scalar_sig(inner: str) -> str:
    """Resolve the scalar element type signature of a ``repeated <T>`` field."""
    if inner in _PROTO_SCALAR_TO_PY_SIG:
        return _PROTO_SCALAR_TO_PY_SIG[inner]
    pytest.fail(
        f"Proto contract drift: CompactRecord has `repeated {inner}` whose "
        "element type has no Python mapping in this test."
    )
    return ""  # for mypy: unreachable


def _expected_python_type_sig(proto_type: str) -> str:
    """Map a proto type string to the expected Python type signature.

    ``repeated <T>`` → ``list[<py T>]``; scalar types use
    :data:`_PROTO_SCALAR_TO_PY_SIG`. Unknown proto types (e.g. nested
    message or enum names) raise via :func:`pytest.fail` — the
    CompactRecord contract uses only scalars + repeated string, so any
    other type is itself drift worth flagging.
    """
    if proto_type.startswith("repeated "):
        inner = proto_type[len("repeated ") :]
        return f"list[{_inner_scalar_sig(inner)}]"
    if proto_type in _PROTO_SCALAR_TO_PY_SIG:
        return _PROTO_SCALAR_TO_PY_SIG[proto_type]
    pytest.fail(
        f"Proto contract drift: CompactRecord field has proto type "
        f"`{proto_type}` which has no Python mapping in this test. Either "
        "the proto added a nested-message/enum field (contract change) or "
        "the type-mapping table here needs extending."
    )
    return ""  # for mypy: unreachable


def _pydantic_field_type_sig(annotation: object) -> str:
    """Normalise a Pydantic field annotation to a Python type-signature string.

    ``list[str]`` → ``"list[str]"`` (preserved via ``get_origin``/``get_args``);
    ``str`` → ``"str"``. ``Field(default=...)`` / ``default_factory=...`` do
    not affect the annotation, so they are invisible here — only the type
    is inspected, mirroring the proto side which only sees types.
    """
    origin = get_origin(annotation)
    if origin is list:
        args = get_args(annotation)
        inner = args[0] if args else "Unknown"
        return f"list[{_pydantic_field_type_sig(inner)}]"
    if isinstance(annotation, type):
        return annotation.__name__
    pytest.fail(
        f"Pydantic CompactRecord field has annotation `{annotation!r}` that "
        "this test cannot normalise to a Python type signature — extend "
        "the parser."
    )
    return ""  # for mypy: unreachable


# ── Fixture: parsed proto fields ──────────────────────────────────────────────


@pytest.fixture(scope="module")
def proto_compact_fields() -> dict[str, str]:
    """Parse ``CompactRecord`` from the proto file once per module."""
    if not _PROTO_FILE.is_file():  # pragma: no cover - repo layout guard
        pytest.fail(
            f"Proto contract file not found at {_PROTO_FILE}. The repo "
            "layout changed — update the path in this test."
        )
    text = _PROTO_FILE.read_text(encoding="utf-8")
    body = _extract_message_block(text, "CompactRecord")
    return _parse_proto_fields(body)


@pytest.fixture(scope="module")
def pydantic_compact_fields() -> dict[str, str]:
    """Introspect the Pydantic ``CompactRecord`` model fields once per module.

    Returns ``{field_name: python_type_sig}`` where ``python_type_sig`` is a
    short string (``"str"``, ``"list[str]"``) used for type-compatibility
    comparison against the proto side.
    """
    raw = CompactRecord.model_fields
    return {name: _pydantic_field_type_sig(f.annotation) for name, f in raw.items()}


# ── Tests ────────────────────────────────────────────────────────────────────


def test_proto_compact_record_is_present(proto_compact_fields: dict[str, str]) -> None:
    """Sanity: the parser found at least one field in the proto message.

    A zero-field result usually means the regex parser drifted from the
    proto syntax — catching that here produces a clearer failure than a
    vague "field sets mismatch" diff downstream.
    """
    assert proto_compact_fields, (
        "Proto parser returned no fields for `CompactRecord`. The proto "
        "file structure changed and the regex parser needs updating — "
        f"inspect {_PROTO_FILE}."
    )


def test_pydantic_compact_record_is_present(pydantic_compact_fields: dict[str, str]) -> None:
    """Sanity: the Pydantic model exposes at least one field."""
    assert pydantic_compact_fields, (
        "Pydantic `CompactRecord` exposes no model fields — "
        "did the model definition change or move?"
    )


def test_compact_record_field_names_match(
    proto_compact_fields: dict[str, str],
    pydantic_compact_fields: dict[str, str],
) -> None:
    """Every proto field has a Pydantic counterpart and vice versa.

    Field names use snake_case on both sides (proto convention + Pydantic
    convention), so no name transformation is applied — a direct set
    comparison catches drift in either direction. Fields listed in
    :data:`_PROTO_ONLY_FIELDS` are excluded from the proto side (live
    wire fields with no Pydantic counterpart by design, e.g. the ADR-0021
    Q10.6 ``revision`` reserve).
    """
    proto_names = set(proto_compact_fields) - _PROTO_ONLY_FIELDS
    py_names = set(pydantic_compact_fields)

    missing_in_pydantic = proto_names - py_names
    extra_in_pydantic = py_names - proto_names

    if missing_in_pydantic or extra_in_pydantic:
        lines = ["CompactRecord field-name drift detected (proto ↔ Pydantic):"]
        if missing_in_pydantic:
            lines.append(
                "  In proto but NOT in Pydantic: " + ", ".join(sorted(missing_in_pydantic))
            )
        if extra_in_pydantic:
            lines.append("  In Pydantic but NOT in proto: " + ", ".join(sorted(extra_in_pydantic)))
        lines.append(
            f"  Proto fields:    {sorted(proto_names)}\n  Pydantic fields:  {sorted(py_names)}"
        )
        lines.append(
            "Resolution: the proto file declares itself the mirror of "
            "src/mnemos/compact.py::CompactRecord. Update whichever side "
            "drifted so the two field sets agree (contract §2.3)."
        )
        pytest.fail("\n".join(lines))


def test_compact_record_field_types_match(
    proto_compact_fields: dict[str, str],
    pydantic_compact_fields: dict[str, str],
) -> None:
    """Each shared field's proto type maps to its Pydantic annotation.

    Runs only over the intersection of the two field sets (the name test
    above already guards the set equality); a type mismatch here is a
    semantic drift even when names agree (e.g. ``string`` → ``bytes``).
    """
    mismatches: list[str] = []
    for name in sorted(proto_compact_fields):
        if name not in pydantic_compact_fields:
            # Name drift is reported by the dedicated test above; skip here
            # to keep this failure focused on type semantics.
            continue
        proto_type = proto_compact_fields[name]
        expected = _expected_python_type_sig(proto_type)
        actual = pydantic_compact_fields[name]
        if expected != actual:
            mismatches.append(
                f"  {name}: proto `{proto_type}` → expected `{expected}`, got Pydantic `{actual}`"
            )
    if mismatches:
        pytest.fail(
            "CompactRecord field-type drift detected (proto ↔ Pydantic):\n"
            + "\n".join(mismatches)
            + "\nUpdate the proto type or the Pydantic annotation so they "
            "agree (contract §2.3)."
        )


def test_compact_record_field_order_matches(
    proto_compact_fields: dict[str, str],
    pydantic_compact_fields: dict[str, str],
) -> None:
    """Proto and Pydantic fields appear in the same declaration order.

    The proto comment states the fields *"mirror the Pydantic model 1:1
    in field-name, type semantics, and field order"*. Field order matters
    for proto wire compatibility (tag numbers) and for human readability
    of the contract — a reordered field set is a contract drift even when
    names and types still agree. Proto-side-only fields
    (:data:`_PROTO_ONLY_FIELDS`) are appended after the mirrored block
    and are filtered out before the comparison.
    """
    proto_order = [f for f in proto_compact_fields if f not in _PROTO_ONLY_FIELDS]
    py_order = list(pydantic_compact_fields)
    if proto_order != py_order:
        pytest.fail(
            "CompactRecord field-order drift detected (proto ↔ Pydantic):\n"
            f"  Proto order:    {proto_order}\n"
            f"  Pydantic order: {py_order}\n"
            "The proto comment promises field-order parity with compact.py — "
            "restore the order on whichever side drifted (contract §2.3)."
        )
