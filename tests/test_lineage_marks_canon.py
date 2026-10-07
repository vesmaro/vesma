"""ADR-0037 Wave A — engine alignment for ``metadata.canon.lineage_marks``.

Covers the three ratified divergences (ADR-0037 §Decision, owner-ratified
2026-10-06) against the real MemoryManager, the same fixture discipline as
tests/test_canon_warn_validator.py:

  Д1 — ``lineage_marks`` is an allowed key in EVERY envelope type
       (``ENVELOPE_ALLOWED_KEYS``): a canon-valid record carrying the
       marks array passes the engine's warn gate WITHOUT
       ``CANON-E-ENVELOPE`` (the «valid field rejected» blocker class).
  Д2 — the per-item validator ``_lineage_violations`` /
       :func:`vesma.canon_validate.validate_canon_record` enforces the
       frozen lineage_marks schema shape (vesma-canon v1.2, W6): array
       minItems 1, ``kind`` ∈ the three ratified kinds, ``at`` the
       ISO-8601 date-time pattern's shape, ``ref`` a non-empty string
       when present, ``additionalProperties: false`` per mark. Failure →
       ``CANON-E-LINEAGE`` through the EXISTING warn/strict gate — no
       new enforcement machinery.
  Д5 — a client-supplied ``lineage_marks`` array strips on the generic
       create/update paths (and inside a kept envelope on the untrusted
       JSON import) in the ``CHECKPOINT_STAMP_KEYS`` discipline: marks
       are server-minted / arbitration-minted (W1-W3), never client data.

Out of scope (ADR-0037 Phases): the merge-arbiter itself (Wave B), schema
text changes, corpus work.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from vesma.canon_validate import (
    ENVELOPE_ALLOWED_KEYS,
    LINEAGE_MARK_KINDS,
    CanonViolationError,
    validate_canon_record,
)
from vesma.cli.import_ import ImportMode, run_import
from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.models import CHECKPOINT_STAMP_KEYS, MemoryCreate, MemoryUpdate

from ._canon_sibling import canon_sibling_file

# ---------------------------------------------------------------------------
# Helpers (mirror tests/test_canon_warn_validator.py)
# ---------------------------------------------------------------------------

_CP_ENVELOPE: dict[str, Any] = {
    "schema_version": "1",
    "type": "checkpoint",
    "status": "active",
    "language": "en",
    "session_ref": None,
}

_CP_BODY = (
    "# Session checkpoint — 2026-09-27T00:00:00Z\n\n"
    "## Goals\nship it\n\n"
    "## Completed\n(nothing completed yet.)\n\n"
    "## In Progress\n(nothing in progress at this point.)\n\n"
    "## Decisions\n(no decisions recorded.)\n\n"
    "## Context\n(no continuation context recorded.)\n"
)


def _cp_trusted_add(mgr: MemoryManager, **meta: Any) -> object:
    """A trusted-internal create that CARRIES an envelope (the W1-W3
    stand-in: only trusted server channels mint marks in Wave A)."""
    return mgr.add(
        MemoryCreate(
            content=_CP_BODY,
            tags=["project:canonproj", "agent:server", "mnemos:checkpoint"],
            metadata={"canon": {**_CP_ENVELOPE, **meta}},
        ),
        project="canonproj",
        agent="server",
        trusted_checkpoint_stamps=True,  # the trusted save_checkpoint channel
        mint_relates_to=False,
    )


def _task_envelope(**extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": "1",
        "type": "task",
        "status": "active",
        "language": "en",
        "owner_slug": "tech-lead",
        "priority": "P1",
        "size": "M",
    }
    base.update(extra)
    return base


_TASK_BODY = (
    "## Why\nthe canon needs a write-path gate\n\n"
    "## Acceptance\npytest green\n\n"
    "## Out of scope\nrender changes\n\n"
    "## References\ncanon §9\n"
)

_MARK_OK: dict[str, Any] = {
    "kind": "same-message-other-envelope",
    "at": "2026-10-05T09:30:00Z",
    "ref": "the-other-record-id",
}

_MARK_ARB: dict[str, Any] = {
    "kind": "merged-by-arbiter",
    "at": "2026-10-06T08:00:00.500Z",
    "ref": "folded-twin-id",
}


@pytest.fixture
def mgr():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        settings = Settings(
            mnemos={
                "vault_path": str(tmp / "vault"),
                "data_dir": str(tmp / "data"),
                "db_name": "test.db",
            },
            embedding={"provider": "onnx"},
            scanner={"enabled": False},
        )
        settings.resolve_paths()
        manager = MemoryManager(settings)
        mock_embedder = MagicMock()
        mock_embedder.embed.return_value = [0.1] * 384
        manager._embedder = mock_embedder
        yield manager
        manager.close()


# ---------------------------------------------------------------------------
# Д1 — envelope allowlist: marks allowed in every type
# ---------------------------------------------------------------------------


def test_d1_lineage_marks_in_every_allowed_key_set() -> None:
    """The allowlist mirrors the canon envelope (`$ref` wiring): the
    optional ``lineage_marks`` key is legal in EVERY type."""
    for ctype, allowed in ENVELOPE_ALLOWED_KEYS.items():
        assert "lineage_marks" in allowed, ctype


def test_d1_canon_valid_record_with_marks_passes_without_canon_e_envelope(
    mgr: MemoryManager,
) -> None:
    """THE Д1 regression: a canon-valid record carrying marks rides the
    warn gate with ZERO envelope violations (cdd648ac blocker class)."""
    memory = _cp_trusted_add(mgr, lineage_marks=[dict(_MARK_OK), dict(_MARK_ARB)])
    assert memory is not None
    codes = [w["code"] for w in memory.metadata.get("canon_warnings", [])]
    assert "CANON-E-ENVELOPE" not in codes, codes
    # The full array persisted verbatim (zero-loss pass-through).
    stored = mgr.sqlite.get(memory.id)
    assert stored is not None
    assert stored.metadata["canon"]["lineage_marks"] == [
        dict(_MARK_OK),
        dict(_MARK_ARB),
    ]


def test_d1_client_task_envelope_with_marks_persists_silently(mgr: MemoryManager) -> None:
    """On the generic path too: a valid client task envelope with marks
    persists (SEC P2-2) and validates clean — no CANON-E-ENVELOPE."""
    memory = mgr.add(
        MemoryCreate(
            content=_TASK_BODY,
            title="marked task row",
            tags=["project:canonproj", "agent:alice"],
            metadata={"canon": _task_envelope(lineage_marks=[dict(_MARK_OK)])},
        ),
        project="canonproj",
        agent="alice",
    )
    codes = [w["code"] for w in memory.metadata.get("canon_warnings", [])]
    assert "CANON-E-ENVELOPE" not in codes


# ---------------------------------------------------------------------------
# Д2 — per-item validator (kind / at / ref / additionalProperties / array)
# ---------------------------------------------------------------------------


def _lineage_codes(canon: dict[str, Any]) -> list[str]:
    violations = validate_canon_record(content=_CP_BODY, title=None, metadata={"canon": canon})
    return sorted({v.code for v in violations})


def test_d2_valid_marks_arrays_pass_silently() -> None:
    for marks in (
        [dict(_MARK_OK)],
        [dict(_MARK_OK), dict(_MARK_ARB)],
        [{**dict(_MARK_OK), "ref": _MARK_OK["ref"], "kind": "split-from"}],
        # ref optional: absent is fine
        [{"kind": "split-from", "at": "2026-10-06T00:00:00+00:00"}],
        # fractional seconds and lowercase z are the pattern's shape
        [{"kind": "split-from", "at": "2026-10-06t09:30:00.123+03:00"}],
    ):
        canon = {**_CP_ENVELOPE, "lineage_marks": marks}
        codes = _lineage_codes(canon)
        assert codes == [], (marks, codes)


def test_d2_pin_lineage_mark_kinds_literal() -> None:
    """The kind enum is the pinned ratified set (canon schema freeze)."""
    assert set(LINEAGE_MARK_KINDS) == {
        "same-message-other-envelope",
        "merged-by-arbiter",
        "split-from",
    }


def test_d2_unknown_kind_fails(mgr: MemoryManager) -> None:
    canon = {**_CP_ENVELOPE, "lineage_marks": [{"kind": "unknown-kind", "at": _MARK_OK["at"]}]}
    codes = _lineage_codes(canon)
    assert "CANON-E-LINEAGE" in codes
    violations = validate_canon_record(content=_CP_BODY, title=None, metadata={"canon": canon})
    assert any("unknown-kind" in v.detail for v in violations if v.code == "CANON-E-LINEAGE")
    # The canon-repo negative fixture gets the SAME engine verdict (live
    # when the sibling exists, the committed twin otherwise).
    neg = canon_sibling_file("examples", "negative", "bad-lineage-marks-kind.json")
    record = (
        json.loads(neg.read_text(encoding="utf-8"))
        if neg is not None
        else {
            "body": _TASK_BODY,
            "metadata": {"canon": _task_envelope(lineage_marks=[dict(canon["lineage_marks"][0])])},
        }
    )
    codes = _lineage_codes(record["metadata"]["canon"])
    assert "CANON-E-LINEAGE" in codes


def test_d2_non_iso_at_shapes_fail() -> None:
    bad_at = [
        "2026-10-05",  # date-only, no time part
        "2026-10-05 09:30:00",  # space separator, pattern requires T
        "2026-10-05T09:30",  # truncated, no seconds
        "09:30:00T2026-10-05",  # nonsense order
        "not-a-date",
        20261005,
        None,
    ]
    for at in bad_at:
        canon = {**_CP_ENVELOPE, "lineage_marks": [{"kind": "split-from", "at": at}]}
        assert "CANON-E-LINEAGE" in _lineage_codes(canon), at


def test_d2_ref_rules_fail() -> None:
    for bad_ref in ("", 42, None):
        canon = {
            **_CP_ENVELOPE,
            "lineage_marks": [{**_MARK_OK, "ref": bad_ref}],
        }
        assert "CANON-E-LINEAGE" in _lineage_codes(canon), bad_ref
    # optional: absent ref is NOT a violation — and a None ref strips of
    # the "ref present" branch by not being there at all.
    canon = {**_CP_ENVELOPE, "lineage_marks": [{"kind": "split-from", "at": _MARK_OK["at"]}]}
    assert "CANON-E-LINEAGE" not in _lineage_codes(canon)


def test_d2_additional_properties_false_per_mark() -> None:
    canon = {
        **_CP_ENVELOPE,
        "lineage_marks": [{**_MARK_OK, "forged": "extra"}],
    }
    codes = _lineage_codes(canon)
    assert "CANON-E-LINEAGE" in codes
    violations = validate_canon_record(content=_CP_BODY, title=None, metadata={"canon": canon})
    assert any(
        "forged" in v.detail and "additionalProperties" in v.detail
        for v in violations
        if v.code == "CANON-E-LINEAGE"
    )


def test_d2_array_shape_anchored() -> None:
    # non-array value
    canon = {**_CP_ENVELOPE, "lineage_marks": "merged-by-arbiter"}
    assert "CANON-E-LINEAGE" in _lineage_codes(canon)
    # empty array (minItems 1)
    canon = {**_CP_ENVELOPE, "lineage_marks": []}
    assert "CANON-E-LINEAGE" in _lineage_codes(canon)
    # non-object item
    canon = {**_CP_ENVELOPE, "lineage_marks": ["same-message-other-envelope"]}
    assert "CANON-E-LINEAGE" in _lineage_codes(canon)
    # missing required kind / at
    assert "CANON-E-LINEAGE" in _lineage_codes(
        {**_CP_ENVELOPE, "lineage_marks": [{"at": _MARK_OK["at"]}]}
    )
    assert "CANON-E-LINEAGE" in _lineage_codes(
        {**_CP_ENVELOPE, "lineage_marks": [{"kind": "split-from"}]}
    )


def test_d2_bad_marks_warn_and_store_via_existing_gate(
    mgr: MemoryManager, caplog: pytest.LogCaptureFixture
) -> None:
    """No new enforcement machinery: the violation rides the existing
    warn/strict gate — warn mode stores the record WITH canon_warnings."""
    with caplog.at_level(logging.WARNING, logger="vesma.manager"):
        memory = _cp_trusted_add(mgr, lineage_marks=[{**_MARK_OK, "kind": "unknown-kind"}])
    assert memory is not None
    codes = [w["code"] for w in memory.metadata["canon_warnings"]]
    assert "CANON-E-LINEAGE" in codes
    lines = [r.message for r in caplog.records if "canon_violation:" in r.message]
    assert any("CANON-E-LINEAGE" in line and memory.id[:8] in line for line in lines)
    assert mgr.sqlite.get(memory.id) is not None  # the write succeeded


def test_d2_bad_marks_reject_strict_create(mgr: MemoryManager) -> None:
    mgr.settings.vesma.canon_mode = "strict"
    with pytest.raises(CanonViolationError) as excinfo:
        _cp_trusted_add(mgr, lineage_marks=[{**_MARK_OK, "at": "2026-10-05"}])
    assert any(v.code == "CANON-E-LINEAGE" for v in excinfo.value.violations)
    assert mgr.stats()["total"] == 0


def test_d2_bad_marks_update_path_warn_only(mgr: MemoryManager) -> None:
    """canon §10 / ADR-0003 obligation 6 holds for the new code too.

    A client replacement metadata dict can NEVER carry marks into the
    gate (Д5 strips them before it — see the D5 tests); the warn-only
    update leg is therefore exercised through the trusted channel: a
    minted row with a shape-violating marks array stores (warn mode) and
    a later CONTENT-only update re-warns instead of failing."""
    memory = _cp_trusted_add(mgr, lineage_marks=[{**_MARK_OK, "at": "2026-10-05"}])
    assert memory is not None
    assert [w["code"] for w in memory.metadata["canon_warnings"]] == ["CANON-E-LINEAGE"]
    updated = mgr.update(memory.id, MemoryUpdate(content=_CP_BODY + "\nan extra line"))
    assert updated is not None
    codes = [w["code"] for w in updated.metadata["canon_warnings"]]
    assert "CANON-E-LINEAGE" in codes


# ---------------------------------------------------------------------------
# Д5 — client-supplied marks strip on generic create/update (and import)
# ---------------------------------------------------------------------------


def test_d5_generic_create_strips_client_marks(mgr: MemoryManager) -> None:
    """A client cannot mint marks through the generic create."""
    memory = mgr.add(
        MemoryCreate(
            content="forged marks row",
            tags=["project:canonproj", "agent:mallory"],
            metadata={"canon": _task_envelope(lineage_marks=[dict(_MARK_OK)]), "harmless": 1},
        ),
        project="canonproj",
        agent="mallory",
    )
    assert memory.metadata["harmless"] == 1  # innocent keys survive
    assert "lineage_marks" not in memory.metadata["canon"], memory.metadata["canon"]
    stored = mgr.sqlite.get(memory.id)
    assert stored is not None
    assert "lineage_marks" not in stored.metadata["canon"]


def test_d5_update_strips_client_marks(mgr: MemoryManager) -> None:
    """A client metadata replacement on update cannot smuggle marks in."""
    memory = mgr.add(
        MemoryCreate(
            content=_TASK_BODY,
            title="update strip",
            tags=["project:canonproj", "agent:alice"],
            metadata={"canon": _task_envelope()},
        ),
        project="canonproj",
        agent="alice",
    )
    replaced = _task_envelope(status="draft", lineage_marks=[dict(_MARK_OK)])
    updated = mgr.update(memory.id, MemoryUpdate(metadata={"canon": replaced}))
    assert updated is not None
    assert updated.metadata["canon"]["status"] == "draft"  # client data stands
    assert "lineage_marks" not in updated.metadata["canon"]  # forged marks do not
    stored = mgr.sqlite.get(memory.id)
    assert stored is not None
    assert "lineage_marks" not in stored.metadata["canon"]


def test_d5_checkpoint_row_minted_marks_survive_update(mgr: MemoryManager) -> None:
    """The strip targets ONLY the client-supplied copy: the
    merge-back-protected minted envelope of a checkpoint row — the future
    W1 carrier — keeps its marks on any update (Д5 «re-derive/protect»)."""
    memory = _cp_trusted_add(mgr, lineage_marks=[dict(_MARK_ARB)])
    assert memory is not None
    updated = mgr.update(memory.id, MemoryUpdate(metadata={"note": 1}))
    assert updated is not None
    assert updated.metadata["note"] == 1
    assert updated.metadata["canon"]["lineage_marks"] == [dict(_MARK_ARB)]
    # The replacement envelope is checkpoint-type → whole ``canon`` is
    # merge-protected; here only verify honest marks are never dropped by
    # a same-type replacement carrying none.
    updated2 = mgr.update(memory.id, MemoryUpdate(metadata={"canon": dict(_CP_ENVELOPE)}))
    assert updated2 is not None
    assert updated2.metadata["canon"] == {**_CP_ENVELOPE, "lineage_marks": [dict(_MARK_ARB)]}


def test_d5_import_strips_marks_inside_kept_envelope(mgr, tmp_path: Path) -> None:
    """The untrusted JSON import keeps a client task envelope but strips
    its marks (same class as the manager create/update strips)."""
    payload = {
        "format_version": "1.0",
        "mnemos_version": "test",
        "memories": [
            {
                "id": "lineage-import-row-0003",
                "content": "imported row with forged marks",
                "tags": ["project:mnemos", "agent:tech-lead", "mnemos:learning"],
                "source": "cli",
                "status": "published",
                "metadata": {"canon": _task_envelope(lineage_marks=[dict(_MARK_OK)])},
            }
        ],
        "projects": [],
    }
    out = tmp_path / "crafted-lineage.json"
    out.write_text(json.dumps(payload), encoding="utf-8")

    result = run_import(mgr, out, mode=ImportMode.MERGE)
    assert result.imported == 1
    assert not result.errors
    stored = mgr.sqlite.get("lineage-import-row-0003")
    assert stored is not None
    assert stored.metadata["canon"]["type"] == "task"  # envelope kept
    assert "lineage_marks" not in stored.metadata["canon"]  # marks stripped
    assert any("canon.lineage_marks" in w for w in result.warnings)


def test_d5_trusted_restore_keeps_marks(mgr, tmp_path: Path) -> None:
    """``--trusted-restore`` keeps marks verbatim (the operator asserts a
    trusted self-backup — the migrate/backfill legit path)."""
    payload = {
        "format_version": "1.0",
        "mnemos_version": "test",
        "memories": [
            {
                "id": "lineage-restore-row-0004",
                "content": "restored row with honest marks",
                "tags": ["project:mnemos", "agent:tech-lead", "mnemos:learning"],
                "source": "cli",
                "status": "published",
                "metadata": {"canon": _task_envelope(lineage_marks=[dict(_MARK_ARB)])},
            }
        ],
        "projects": [],
    }
    out = tmp_path / "trusted-lineage.json"
    out.write_text(json.dumps(payload), encoding="utf-8")

    result = run_import(mgr, out, mode=ImportMode.MERGE, trusted_restore=True)
    assert result.imported == 1
    assert not result.errors
    stored = mgr.sqlite.get("lineage-restore-row-0004")
    assert stored is not None
    assert stored.metadata["canon"]["lineage_marks"] == [dict(_MARK_ARB)]
    assert not any("canon.lineage_marks" in w for w in result.warnings)


def test_d5_strip_helper_shared_rule() -> None:
    """The helper rule lives once: non-dict and absent pass through."""
    from vesma.canon_validate import strip_client_lineage_marks

    canon, stripped = strip_client_lineage_marks(None)
    assert stripped is False and canon is None
    canon, stripped = strip_client_lineage_marks("task")
    assert stripped is False and canon == "task"
    canon, stripped = strip_client_lineage_marks({**_CP_ENVELOPE})
    assert stripped is False and canon == _CP_ENVELOPE
    canon_dict, stripped = strip_client_lineage_marks(
        {**_CP_ENVELOPE, "lineage_marks": [dict(_MARK_OK)]}
    )
    assert stripped is True
    assert "lineage_marks" not in canon_dict
    assert canon_dict["type"] == "checkpoint"


def test_d5_stamps_strip_untouched() -> None:
    """Д5 adds nothing to the top-level strip sets: the checkpoint stamp
    class itself is unchanged (membership pinned)."""
    expected_stamps = {"checkpoint_agent", "checkpoint_session", "checkpoint_dedup_key", "canon"}
    assert set(CHECKPOINT_STAMP_KEYS) == expected_stamps


# ---------------------------------------------------------------------------
# Drift pin — engine kinds vs the canon schema (live / frozen fallback)
# ---------------------------------------------------------------------------


def test_lineage_kind_literal_matches_canon_schema() -> None:
    """Live leg: the schema's kind enum equals LINEAGE_MARK_KINDS. The
    frozen fallback is the PIN itself — test_d2_pin_lineage_mark_kinds_literal
    holds the W6 enum even without the sibling checkout."""
    lineage_schema = canon_sibling_file("schemas", "lineage_marks.schema.json")
    if lineage_schema is not None:
        schema = json.loads(lineage_schema.read_text(encoding="utf-8"))
        enum = schema["items"]["properties"]["kind"]["enum"]
        assert set(enum) == set(LINEAGE_MARK_KINDS)
