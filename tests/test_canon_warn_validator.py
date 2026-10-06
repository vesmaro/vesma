"""vesmaro-canon v1.0.0 — write-path warn-mode canon validator (W2-S2).

Covers store card vesmaro-canon-w2-s2-warn-validator against a REAL
MemoryManager (tmp SQLite), canon.md §9 (warn/strict levels + the §9
transitional scope rule) and ADR-0003 obligations 5-6:

  1. Warn mode (default): a record WITH ``metadata.canon`` that violates a
     canon rule is stored WITH machine-parseable warnings — one
     ``canon_violation:`` log line per violation (id + code + rule +
     detail) AND ``metadata["canon_warnings"]`` on the stored row; the
     write ALWAYS succeeds.
  2. Valid canon records pass silently — zero warnings, zero
     ``canon_warnings`` metadata (incl. every trusted ``save_checkpoint``
     mint).
  3. Scope rule (canon §9 transitional — HIGHEST priority): a record
     WITHOUT ``metadata.canon`` is OUT OF CANON SCOPE — no validation, no
     warning, ever, in BOTH modes (legacy is not a violation).
  4. Strict mode (``mnemos.canon_mode="strict"``): violations REJECT the
     write on the CREATE path only; update paths are warn-only (canon §10
     "new records only" — a legacy row's update never fails retroactively).
  5. One test per warn code: CANON-E-ENVELOPE / -SECTION / -STATUS /
     -TITLE / -LANGUAGE / -DATE.
  6. Drift pin: the engine's required-sections map equals the canon
     schemas' ``x-canon-sections`` annotations when the canon repo is
     present (same pin protocol as the S1 drift test).

The validator logic itself is additionally exercised DIRECTLY
(vesmaro.canon_validate.validate_canon_record) for the rule-code matrix.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from vesmaro.canon_validate import (
    CANON_REQUIRED_SECTIONS,
    CANON_WARN_CODES,
    CanonViolation,
    CanonViolationError,
    validate_canon_record,
)
from vesmaro.config import Settings
from vesmaro.manager import MemoryManager
from vesmaro.models import CHECKPOINT_SECTION_TITLES, MemoryCreate, MemoryUpdate

from ._canon_sibling import canon_sibling_file

# ---------------------------------------------------------------------------
# Helpers
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
    """A trusted-internal create that CARRIES an envelope (the only way a
    canon row can enter at the generic add path — client-supplied copies
    are stripped by the S1 stamp discipline)."""
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


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/test_checkpoint_canon_envelope.py::mgr)
# ---------------------------------------------------------------------------


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
# 1. Warn mode (default) — violations warn + store, valid rows silent
# ---------------------------------------------------------------------------


def test_default_mode_is_warn(mgr: MemoryManager) -> None:
    assert mgr.settings.mnemos.canon_mode == "warn"


def test_valid_canon_record_passes_silently(mgr: MemoryManager) -> None:
    memory = _cp_trusted_add(mgr)
    assert isinstance(memory, object) and "canon_warnings" not in memory.metadata
    stored = mgr.sqlite.get(memory.id)
    assert stored is not None
    assert "canon_warnings" not in stored.metadata


def test_every_minted_checkpoint_passes_silently(mgr: MemoryManager) -> None:
    """The trusted save_checkpoint mint must never warn in warn mode —
    acceptance 1 (a valid envelope + always-five-sections render)."""
    for lang in ("ru", "en"):
        memory, _dup = mgr.save_checkpoint(
            {"goals": "g", "in_progress": "wip"}, project="canonproj", language=lang
        )
        assert "canon_warnings" not in memory.metadata


def test_violation_warns_and_stores_with_machine_parseable_metadata(
    mgr: MemoryManager,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="vesmaro.manager"):
        # Bad language on an otherwise-checkpoint envelope (trusted path).
        memory = _cp_trusted_add(mgr, language="fr")
    assert memory is not None
    stored = mgr.sqlite.get(memory.id)
    assert stored is not None
    warnings = stored.metadata["canon_warnings"]
    assert [w["code"] for w in warnings] == ["CANON-E-LANGUAGE"]
    assert set(warnings[0]) == {"code", "rule", "detail"}
    # The write SUCCEEDED (warn mode never blocks)...
    assert mgr.sqlite.get(memory.id) is not None
    # ...and one machine-parseable line carries id + code.
    lines = [r.message for r in caplog.records if "canon_violation:" in r.message]
    assert any(memory.id[:8] in line and "CANON-E-LANGUAGE" in line for line in lines)


def test_sdk_remember_surfaces_canon_warnings(mgr: MemoryManager) -> None:
    """The SDK remember channel echoes the codes at its edge (log) and the
    returned Memory carries metadata['canon_warnings'] (non-fatal field)."""
    # Envelope via the trusted channel, read back through the SDK surface.
    memory = _cp_trusted_add(mgr, language="de")
    assert memory is not None
    assert memory.metadata["canon_warnings"][0]["code"] == "CANON-E-LANGUAGE"


# ---------------------------------------------------------------------------
# 2. Scope rule (canon §9) — no envelope → zero canon warnings, ever
# ---------------------------------------------------------------------------


def test_no_envelope_produces_zero_canon_warnings(mgr: MemoryManager) -> None:
    """A generic create (stamps stripped) is legacy — never validated."""
    memory = mgr.add(
        MemoryCreate(
            content="27.09 done yesterday — legacy prose, out of canon scope",
            title=None,
            tags=["project:canonproj", "agent:legacy"],
        ),
        project="canonproj",
        agent="legacy",
    )
    assert "canon" not in memory.metadata
    assert "canon_warnings" not in memory.metadata


def test_no_envelope_stays_silent_in_strict_mode(mgr: MemoryManager) -> None:
    """Scope rule holds in BOTH modes: strict never rejects a legacy row."""
    mgr.settings.mnemos.canon_mode = "strict"
    memory = mgr.add(
        MemoryCreate(
            content="27.09 done yesterday — legacy prose",
            tags=["project:canonproj", "agent:legacy"],
        ),
        project="canonproj",
        agent="legacy",
    )
    assert "canon_warnings" not in memory.metadata


def test_update_of_no_envelope_row_never_warns(mgr: MemoryManager) -> None:
    memory = mgr.add(
        MemoryCreate(
            content="legacy row",
            tags=["project:canonproj", "agent:legacy"],
        ),
        project="canonproj",
        agent="legacy",
    )
    updated = mgr.update(memory.id, MemoryUpdate(content="edited 27.09 legacy prose"))
    assert updated is not None
    assert "canon_warnings" not in updated.metadata


# ---------------------------------------------------------------------------
# 3. One test per warn code (via the validator + one end-to-end probe)
# ---------------------------------------------------------------------------


def test_code_catalog_is_complete_and_stable() -> None:
    """The single documented home of the codes (module docstring +
    CANON_WARN_CODES): exactly the seven ratified codes (ADR-0037 Wave A
    added CANON-E-LINEAGE — the lineage_marks schema alignment)."""
    assert set(CANON_WARN_CODES) == {
        "CANON-E-ENVELOPE",
        "CANON-E-SECTION",
        "CANON-E-STATUS",
        "CANON-E-TITLE",
        "CANON-E-LANGUAGE",
        "CANON-E-DATE",
        "CANON-E-LINEAGE",
    }


def test_code_canon_e_envelope(mgr: MemoryManager) -> None:
    # (a) unknown field inside the envelope (unevaluatedProperties: false)
    v = validate_canon_record(
        content=_CP_BODY,
        title=None,
        metadata={"canon": {**_CP_ENVELOPE, "affets": "typo"}},
    )
    assert any(x.code == "CANON-E-ENVELOPE" and "affets" in x.detail for x in v)
    # (b) wrong type / missing required field
    v = validate_canon_record(
        content=_CP_BODY, title=None, metadata={"canon": {"schema_version": 1}}
    )
    codes = {x.code for x in v}
    assert "CANON-E-ENVELOPE" in codes
    # (c) missing per-type extra (task without size)
    v = validate_canon_record(
        content=_TASK_BODY,
        title="ok",
        metadata={"canon": _task_envelope(size=None)},
    )
    assert any("size" in x.detail for x in v if x.code == "CANON-E-ENVELOPE")
    # (d) wrong-typed extra (reversible not boolean)
    v = validate_canon_record(
        content="## Decision\nx\n\n## Why\ny\n\n## Alternatives\nz",
        title="ok",
        metadata={
            "canon": {
                "schema_version": "1",
                "type": "decision",
                "status": "active",
                "language": "en",
                "reversible": "yes",
            }
        },
    )
    assert any("reversible" in x.detail for x in v if x.code == "CANON-E-ENVELOPE")
    # end-to-end: stored with the warning, write succeeded
    memory = _cp_trusted_add(mgr, affets="forged-typo")
    assert memory is not None
    assert any(w["code"] == "CANON-E-ENVELOPE" for w in memory.metadata["canon_warnings"])


def test_code_canon_e_section(mgr: MemoryManager) -> None:
    v = validate_canon_record(
        content="# body without sections",
        title=None,
        metadata={"canon": dict(_CP_ENVELOPE)},
    )
    assert [x.code for x in v] == ["CANON-E-SECTION"]
    assert "## Goals" in v[0].detail
    # end-to-end: a content edit can REMOVE a required section — warn-only
    memory, _dup = mgr.save_checkpoint({"goals": "g"}, project="canonproj")
    updated = mgr.update(memory.id, MemoryUpdate(content="sections removed"))
    assert updated is not None
    assert [w["code"] for w in updated.metadata["canon_warnings"]] == ["CANON-E-SECTION"]


def test_code_canon_e_status(mgr: MemoryManager) -> None:
    # invalid enum
    v = validate_canon_record(
        content=_CP_BODY, title=None, metadata={"canon": {**_CP_ENVELOPE, "status": "done"}}
    )
    assert any(x.code == "CANON-E-STATUS" and "done" in x.detail for x in v)
    # status x type conflict: resolved restricted to task/report (canon §7)
    v = validate_canon_record(
        content=_CP_BODY,
        title=None,
        metadata={"canon": {**_CP_ENVELOPE, "status": "resolved"}},
    )
    assert any(
        x.code == "CANON-E-STATUS" and "resolved" in x.detail and "checkpoint" in x.detail
        for x in v
    )
    # resolved IS valid for task/report (no conflict warning)
    v = validate_canon_record(
        content=_TASK_BODY,
        title="ok",
        metadata={"canon": _task_envelope(status="resolved")},
    )
    assert not any(x.code == "CANON-E-STATUS" for x in v)


def test_code_canon_e_title(mgr: MemoryManager) -> None:
    for bad_title in ("", "x" * 81, "two\nlines"):
        v = validate_canon_record(
            content=_CP_BODY, title=bad_title, metadata={"canon": dict(_CP_ENVELOPE)}
        )
        assert any(x.code == "CANON-E-TITLE" for x in v), bad_title


def test_code_canon_e_language(mgr: MemoryManager) -> None:
    v = validate_canon_record(
        content=_CP_BODY, title=None, metadata={"canon": {**_CP_ENVELOPE, "language": "fr"}}
    )
    assert [x.code for x in v] == ["CANON-E-LANGUAGE"]


def test_code_canon_e_date(mgr: MemoryManager) -> None:
    v = validate_canon_record(
        content=_TASK_BODY + "\ndone 27.09, shipped yesterday, versions 3.14.3 clean",
        title="ok",
        metadata={"canon": _task_envelope()},
    )
    details = [x.detail for x in v if x.code == "CANON-E-DATE"]
    assert any("27.09" in d for d in details)
    assert any("yesterday" in d for d in details)
    # version literals and ISO dates never warn
    assert not any("3.14.3" in d for d in details)


# ---------------------------------------------------------------------------
# 4. Strict mode — create-path reject only; update paths warn-only
# ---------------------------------------------------------------------------


def test_strict_rejects_create_path_violation(mgr: MemoryManager) -> None:
    mgr.settings.mnemos.canon_mode = "strict"
    with pytest.raises(CanonViolationError) as excinfo:
        _cp_trusted_add(mgr, language="fr")
    assert any(v.code == "CANON-E-LANGUAGE" for v in excinfo.value.violations)
    assert mgr.stats()["total"] == 0  # nothing stored


def test_strict_create_path_accepts_valid_record(mgr: MemoryManager) -> None:
    mgr.settings.mnemos.canon_mode = "strict"
    memory = _cp_trusted_add(mgr)
    assert "canon_warnings" not in memory.metadata
    assert mgr.stats()["total"] == 1


def test_strict_update_path_is_warn_only(mgr: MemoryManager) -> None:
    """ADR-0003 obligation 6 / canon §10: update paths never fail
    retroactively — a violation on update warns and stores, even in
    strict mode."""
    mgr.settings.mnemos.canon_mode = "strict"
    memory, _dup = mgr.save_checkpoint({"goals": "g"}, project="canonproj")
    updated = mgr.update(memory.id, MemoryUpdate(content="sections all removed"))
    assert updated is not None  # NOT rejected
    assert [w["code"] for w in updated.metadata["canon_warnings"]] == ["CANON-E-SECTION"]


def test_strict_update_of_legacy_row_never_fails(mgr: MemoryManager) -> None:
    """The ADR-0003 headline case: pre-canon row updated in strict mode."""
    mgr.settings.mnemos.canon_mode = "strict"
    legacy = mgr.add(
        MemoryCreate(content="pre-canon row", tags=["project:canonproj", "agent:legacy"]),
        project="canonproj",
        agent="legacy",
    )
    updated = mgr.update(legacy.id, MemoryUpdate(content="27.09 edited legacy prose"))
    assert updated is not None
    assert "canon_warnings" not in updated.metadata


def test_off_mode_disables_validation(mgr: MemoryManager) -> None:
    mgr.settings.mnemos.canon_mode = "off"
    memory = _cp_trusted_add(mgr, language="fr")
    assert memory is not None
    assert "canon_warnings" not in memory.metadata


# ---------------------------------------------------------------------------
# 4.5 canon_warnings lifecycle (cascade review SEC P2-1 + QA P1-1) —
# the gate is the SINGLE writer: client copies never land, and a fixing
# edit CLEARS the key (the gate removes it when the fresh violation list
# is empty instead of early-returning on the stale value).
# ---------------------------------------------------------------------------


_FORGED_WARNINGS = [{"code": "CANON-E-TITLE", "rule": "canon §3", "detail": "forged"}]


def test_generic_create_strips_client_canon_warnings(mgr: MemoryManager) -> None:
    """A client cannot forge warnings onto a clean generic create."""
    memory = mgr.add(
        MemoryCreate(
            content="clean row, out of canon scope",
            tags=["project:canonproj", "agent:mallory"],
            metadata={"canon_warnings": _FORGED_WARNINGS, "harmless": 1},
        ),
        project="canonproj",
        agent="mallory",
    )
    assert memory.metadata["harmless"] == 1
    assert "canon_warnings" not in memory.metadata
    stored = mgr.sqlite.get(memory.id)
    assert stored is not None
    assert "canon_warnings" not in stored.metadata


def test_update_strips_client_canon_warnings(mgr: MemoryManager) -> None:
    """A client metadata replacement cannot smuggle warnings in (the gate
    re-derives the key — it is never merged back like internal keys)."""
    plain = mgr.add(
        MemoryCreate(content="plain row", tags=["project:canonproj", "agent:bob"]),
        project="canonproj",
        agent="bob",
    )
    updated = mgr.update(
        plain.id,
        MemoryUpdate(metadata={"canon_warnings": _FORGED_WARNINGS, "ok": 2}),
    )
    assert updated is not None
    assert updated.metadata["ok"] == 2
    assert "canon_warnings" not in updated.metadata


def test_update_cannot_delete_honest_canon_warnings(mgr: MemoryManager) -> None:
    """A client metadata replacement cannot DELETE honest warnings from a
    violating row either: the strip drops the client copy, the gate
    re-attaches the fresh (still-violating) list."""
    violating = _cp_trusted_add(mgr, language="fr")
    assert violating is not None
    assert "canon_warnings" in violating.metadata
    updated = mgr.update(violating.id, MemoryUpdate(metadata={"note": 1}))
    assert updated is not None
    assert updated.metadata["note"] == 1
    assert [w["code"] for w in updated.metadata["canon_warnings"]] == ["CANON-E-LANGUAGE"]


def test_fixing_update_clears_canon_warnings(mgr: MemoryManager) -> None:
    """THE regression (QA P1-1): violating record → fixing update → the
    key is ABSENT (the gate removes it on a fresh empty violation list;
    the pre-fix gate early-returned and left the stale warnings stuck).
    The fixing edit carries a METADATA replacement too, so the path also
    exercises the strip-client-copy → gate-re-derives ordering."""
    # A valid canon row broken by a content edit (section removed → warns).
    memory = _cp_trusted_add(mgr)
    assert memory is not None
    broken = mgr.update(memory.id, MemoryUpdate(content="sections removed"))
    assert broken is not None
    assert [w["code"] for w in broken.metadata["canon_warnings"]] == ["CANON-E-SECTION"]

    # The fixing edit: restored body + an unrelated metadata replacement
    # (a client canon_warnings copy here would be stripped, never merged).
    fixed = mgr.update(
        memory.id,
        MemoryUpdate(content=_CP_BODY, metadata={"note": 1}),
    )
    assert fixed is not None
    assert fixed.metadata["note"] == 1
    assert "canon_warnings" not in fixed.metadata, "fixing edit must clear stale warnings"
    stored = mgr.sqlite.get(memory.id)
    assert stored is not None
    assert "canon_warnings" not in stored.metadata


def test_fixing_content_edit_clears_canon_warnings(mgr: MemoryManager) -> None:
    """Same lifecycle via a CONTENT-only fix (no metadata replacement):
    the gate re-validates on every update leg and removes the key when
    the body passes."""
    memory, _dup = mgr.save_checkpoint({"goals": "g"}, project="canonproj")
    broken = mgr.update(memory.id, MemoryUpdate(content="sections removed"))
    assert broken is not None
    assert [w["code"] for w in broken.metadata["canon_warnings"]] == ["CANON-E-SECTION"]
    # Restore a canon-shaped body — the fresh violation list is empty.
    fixed = mgr.update(memory.id, MemoryUpdate(content=_CP_BODY))
    assert fixed is not None
    assert "canon_warnings" not in fixed.metadata


# ---------------------------------------------------------------------------
# 4.6 Client envelopes persist (cascade review SEC P2-2, TL ruling:
# engine returns to ratified canon §2) — task/decision/report envelopes
# are CLIENT data and survive the generic create/update strip; only the
# CHECKPOINT type (and malformed canon values) strip.
# ---------------------------------------------------------------------------


def test_sdk_remember_task_envelope_persists_and_validates(mgr: MemoryManager) -> None:
    """SDK remember with a task envelope: a VALID one persists silently
    (no canon_warnings); an INVALID one persists WITH canon_warnings."""
    from vesmaro.sdk import VesmaSDK

    sdk = VesmaSDK(manager=mgr)

    valid = sdk.remember(
        _TASK_BODY,
        project="canonproj",
        agent="alice",
        title="valid task row",
        tags=["project:canonproj", "agent:alice", "mnemos:open-question"],
        metadata={"canon": _task_envelope()},
    )
    assert valid.metadata["canon"] == _task_envelope()
    assert "canon_warnings" not in valid.metadata
    stored = mgr.sqlite.get(valid.id)
    assert stored is not None
    assert stored.metadata["canon"]["type"] == "task"

    invalid = sdk.remember(
        _TASK_BODY,
        project="canonproj",
        agent="alice",
        title="broken task row",
        tags=["project:canonproj", "agent:alice", "mnemos:open-question"],
        metadata={"canon": _task_envelope(size="HUGE")},
    )
    assert invalid.metadata["canon"]["size"] == "HUGE"  # envelope persisted
    assert any(
        w["code"] == "CANON-E-ENVELOPE" and "size" in w["detail"]
        for w in invalid.metadata["canon_warnings"]
    )


def test_generic_create_checkpoint_type_envelope_still_stripped(
    mgr: MemoryManager,
) -> None:
    """The checkpoint type stays server-minted: a client-forged
    checkpoint envelope never lands (the W2-S1 strip, unchanged)."""
    memory = mgr.add(
        MemoryCreate(
            content="forged checkpoint envelope",
            tags=["project:canonproj", "agent:mallory"],
            metadata={"canon": dict(_CP_ENVELOPE), "harmless": 1},
        ),
        project="canonproj",
        agent="mallory",
    )
    assert memory.metadata["harmless"] == 1
    assert "canon" not in memory.metadata


def test_generic_create_malformed_canon_stripped(mgr: MemoryManager) -> None:
    """A malformed canon value (non-dict) strips whole, like a forged
    stamp — it can never satisfy the envelope shape the gate validates."""
    memory = mgr.add(
        MemoryCreate(
            content="malformed envelope row",
            tags=["project:canonproj", "agent:mallory"],
            metadata={"canon": "task", "harmless": 1},
        ),
        project="canonproj",
        agent="mallory",
    )
    assert memory.metadata["harmless"] == 1
    assert "canon" not in memory.metadata
    # A dict with an unknown/missing type is not a client envelope either.
    memory2 = mgr.add(
        MemoryCreate(
            content="unknown type envelope row",
            tags=["project:canonproj", "agent:mallory"],
            metadata={"canon": {"schema_version": "1", "type": "rumour"}},
        ),
        project="canonproj",
        agent="mallory",
    )
    assert "canon" not in memory2.metadata


def test_strict_mode_rejects_violating_task_envelope_on_create(
    mgr: MemoryManager,
) -> None:
    """Strict mode is now MEANINGFUL for the client types: a violating
    task envelope on the generic create path raises CanonViolationError."""
    mgr.settings.mnemos.canon_mode = "strict"
    with pytest.raises(CanonViolationError) as excinfo:
        mgr.add(
            MemoryCreate(
                content=_TASK_BODY,
                title="strict task reject",
                tags=["project:canonproj", "agent:alice"],
                metadata={"canon": _task_envelope(priority="URGENT")},
            ),
            project="canonproj",
            agent="alice",
        )
    assert any(
        v.code == "CANON-E-ENVELOPE" and "priority" in v.detail for v in excinfo.value.violations
    )
    assert mgr.stats()["total"] == 0  # nothing stored


def test_update_of_task_envelope_record_keeps_corrected_envelope(
    mgr: MemoryManager,
) -> None:
    """A task-envelope row is corrected FORWARD through update: the
    client's replacement envelope stands (client data, no merge-back of
    the old value)."""
    memory = mgr.add(
        MemoryCreate(
            content=_TASK_BODY,
            title="task row to correct",
            tags=["project:canonproj", "agent:alice"],
            metadata={"canon": _task_envelope(status="draft")},
        ),
        project="canonproj",
        agent="alice",
    )
    assert memory.metadata["canon"]["status"] == "draft"

    corrected = mgr.update(
        memory.id,
        MemoryUpdate(metadata={"canon": _task_envelope(status="active"), "note": 1}),
    )
    assert corrected is not None
    assert corrected.metadata["note"] == 1
    assert corrected.metadata["canon"]["status"] == "active", (
        "the corrected client envelope must stand (no stale merge-back)"
    )
    assert "canon_warnings" not in corrected.metadata


def test_update_cannot_convert_checkpoint_row_to_client_type(
    mgr: MemoryManager,
) -> None:
    """The merge-back stays server-owned for CHECKPOINT rows: a client
    sending a task envelope on a minted checkpoint row is clobbered —
    a checkpoint row stays a checkpoint row."""
    memory, _dup = mgr.save_checkpoint({"goals": "g"}, project="canonproj")
    minted = memory.metadata["canon"]
    updated = mgr.update(
        memory.id,
        MemoryUpdate(metadata={"canon": _task_envelope(), "note": 2}),
    )
    assert updated is not None
    assert updated.metadata["canon"] == minted
    assert updated.metadata["note"] == 2


# ---------------------------------------------------------------------------
# 4.7 Gate ordering (cascade review SEC P3-1) — strict reject leaves NO
# vault trace: _canon_gate runs BEFORE the Obsidian markdown write, the
# same fail-loud-before-persist discipline as the doc-grouping gate.
# ---------------------------------------------------------------------------


def test_strict_reject_leaves_no_vault_trace(mgr: MemoryManager) -> None:
    """A strict-mode reject on the create path must not leave the refused
    content persisted in the vault directory (the pre-fix order wrote the
    markdown BEFORE the gate raise)."""
    mgr.settings.mnemos.canon_mode = "strict"
    vault = Path(mgr.settings.mnemos.vault_path)
    with pytest.raises(CanonViolationError):
        mgr.add(
            MemoryCreate(
                content=_TASK_BODY,
                title="strict vault reject",
                tags=["project:canonproj", "agent:alice"],
                metadata={"canon": _task_envelope(priority="URGENT")},
            ),
            project="canonproj",
            agent="alice",
        )
    assert mgr.stats()["total"] == 0  # nothing stored
    assert not vault.exists() or not any(vault.rglob("*.md")), (
        "a rejected write must leave no vault file behind"
    )


# ---------------------------------------------------------------------------
# 5. Drift pin — CANON_REQUIRED_SECTIONS vs the schemas' x-canon-sections
# ---------------------------------------------------------------------------


def _canon_repo_sections() -> dict[str, list[str]] | None:
    """Read x-canon-sections from the pinned canon schemas when the sibling
    checkout exists (same pin protocol as the S1 drift test). Worktree-safe
    resolution (cascade QA P2-2 — shared helper, issue #433 class)."""
    schemas_dir = canon_sibling_file("schemas")
    if schemas_dir is None or not schemas_dir.is_dir():
        return None
    out: dict[str, list[str]] = {}
    for rtype in ("checkpoint", "task", "decision", "report"):
        schema = schemas_dir / f"{rtype}.schema.json"
        if not schema.is_file():
            return None

        out[rtype] = list(json.loads(schema.read_text(encoding="utf-8"))["x-canon-sections"])
    return out


def test_required_sections_match_x_canon_sections() -> None:
    live = {t: list(CANON_REQUIRED_SECTIONS[t]) for t in CANON_REQUIRED_SECTIONS}
    pinned = _canon_repo_sections()
    if pinned is not None:
        assert live == pinned
    # Fallback pin: the checkpoint tuple equals the S1 render map (the
    # envelope mint renders exactly these sections in this order).
    assert live["checkpoint"] == [
        CHECKPOINT_SECTION_TITLES[f]
        for f in ("goals", "completed", "in_progress", "decisions", "context")
    ]


def test_violation_as_dict_shape() -> None:
    v = CanonViolation(code="CANON-E-TITLE", rule="canon §3", detail="d")
    assert v.as_dict() == {"code": "CANON-E-TITLE", "rule": "canon §3", "detail": "d"}


# ---------------------------------------------------------------------------
_SNAPSHOT_PATH = Path(__file__).parent / "data" / "w4b_corpus_snapshot.json"

# 6. Date-rule table (cascade fix-b B1 — issue #437 + QA P2-1 + SEC P3-2):
# dotted fragments flag only as UNAMBIGUOUS RU day-first dates; standalone
# relative words/phrases are case-insensitive; decimals and versions pass.
# ---------------------------------------------------------------------------

_DATE_CASES = [
    # (body fragment, expected: True = CANON-E-DATE flagged)
    ("сделано 27.09", True),
    ("сделано 27.09.2026", True),
    ("сделано 15.07", True),
    ("версия 3.14.3", False),
    ("версия 0.0.31", False),
    ("бамп 1.5", False),
    ("vesmaro 6.0", False),
    ("vesmaro 3.0", False),
    ("десятичная 12.05", False),
    ("откат вчера", True),
    ("Откат Вчера", True),
    ("done Today", True),
    ("review Yesterday", True),
    ("встреча на следующей неделе", True),
    ("sync Last Week", True),
    ("дата 2026-09-28", False),
    ("неделя 2026-W39", False),
]


@pytest.mark.parametrize(("fragment", "expected_flag"), _DATE_CASES)
def test_date_rule_table(fragment: str, expected_flag: bool) -> None:
    """Table-driven §5 pin (issue #437 + cascade QA P2-1)."""
    violations = validate_canon_record(
        content=f"- {fragment}",
        title="t",
        metadata={
            "canon": {
                "schema_version": "1",
                "type": "report",
                "status": "active",
                "language": "ru",
                "period": None,
            }
        },
    )
    codes = {v.code for v in violations}
    if expected_flag:
        assert "CANON-E-DATE" in codes, (fragment, codes)
    else:
        assert "CANON-E-DATE" not in codes, (fragment, codes)


# ---------------------------------------------------------------------------
# 7. Intake conformance (cascade fix-b B2 — QA P1-2): the engine validator
# against the canon-repo intake corpus (sibling live leg) and the committed
# snapshot (CI fallback leg). The checker asymmetry (§5 dates are
# engine-only) is pinned rather than just documented.
# ---------------------------------------------------------------------------


#: Edge fixtures carry INDIVIDUAL expected verdicts (canon-repo intake
#: README table): «edge» means tricky-to-classify, not «must pass». The
#: date entries also pin the checker asymmetry (§5 is engine-only).
_EDGE_EXPECTED: dict[str, object] = {
    "edge-date-relative-word.json": "CANON-E-DATE",
    "edge-date-version-lookalike.json": None,
    "edge-placeholder-heavy.json": None,
    "edge-resolved-checkpoint.json": "CANON-E-STATUS",
    "edge-resolved-report.json": None,
    "edge-title-exactly-80.json": None,
    "edge-title-multiline-crlf.json": "CANON-E-TITLE",
}


def _intake_expected_code(name: str) -> str | None:
    """bad-intake-e-<code-lower>.json → 'CANON-E-<CODE UPPER>'; others None."""
    if not name.startswith("bad-intake-e-"):
        return None
    return "CANON-E-" + name.removeprefix("bad-intake-e-").removesuffix(".json").upper()


def test_intake_fixtures_conform_to_engine_validator() -> None:
    """Live leg: every canon-repo intake fixture gets its expected engine
    verdict — positives/edges-clean pass, negatives fail with the code
    their filename names (AGW-20 contract, vesmaro-agent v0.5)."""
    sibling = canon_sibling_file("schemas", "envelope.schema.json")
    intake = sibling.parent.parent / "examples" / "intake" if sibling else None
    if intake is None or not intake.is_dir():
        pytest.skip("canon sibling checkout not available")
    checked = 0
    for sub in ("fixtures-positive", "fixtures-negative", "fixtures-edge"):
        for path in sorted((intake / sub).glob("*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            violations = validate_canon_record(
                content=record.get("body") or "",
                title=record.get("title"),
                metadata=record.get("metadata") or {},
            )
            codes = sorted({v.code for v in violations})
            expected = _EDGE_EXPECTED.get(path.name, _intake_expected_code(path.name))
            if expected is None:
                assert not codes, f"{path.name}: unexpected {codes}"
            elif isinstance(expected, str):
                assert expected in codes, f"{path.name}: expected {expected}, got {codes}"
            else:
                assert codes == set(expected), f"{path.name}: expected {expected}, got {codes}"
            checked += 1
    assert checked >= 17, "the intake corpus must stay substantive"


def test_snapshot_date_edges_match_engine_validator() -> None:
    """CI fallback leg (no sibling): the snapshot's date edges get the same
    engine verdicts the intake README documents — the checker asymmetry is
    a PIN, not prose."""
    snapshot = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    expectations = {
        "edge-date-relative-word.json": {"CANON-E-DATE"},
        "edge-date-version-lookalike.json": set(),
    }
    for entry in snapshot["files"]:
        name = entry["path"].rsplit("/", 1)[-1]
        if name not in expectations:
            continue
        record = entry["record"]
        record_type = record.get("record_type") or "report"
        canon = {
            "schema_version": "1",
            "type": record_type,
            "status": "active" if record_type != "task" else "draft",
            "language": record.get("language") or "ru",
        }
        if record_type == "task":
            canon.update({"owner_slug": "tech-lead", "priority": "P2", "size": "S"})
        if record_type == "report":
            canon["period"] = None
        codes = {
            v.code
            for v in validate_canon_record(
                content=record.get("body") or "",
                title=record.get("title"),
                metadata={"canon": canon},
            )
        }
        assert codes == expectations[name], (name, codes)


# ---------------------------------------------------------------------------
# 8. Vendored-schema literal pin (cascade fix-b B4 — QA P2-3): the
# validator literals stay in lockstep with the vendored canon schemas
# (in-repo bytes — works in CI without the sibling).
# ---------------------------------------------------------------------------


def test_validator_literals_pinned_to_vendored_schemas() -> None:
    from vesmaro.canon_validate import (
        ENVELOPE_STATUSES,
        ENVELOPE_TYPES,
        RESOLVED_ALLOWED_TYPES,
    )

    envelope = json.loads(
        (
            Path(__file__).parent.parent / "integrations" / "schemas" / "envelope.schema.json"
        ).read_text(encoding="utf-8")
    )
    props = envelope["properties"]
    assert set(props["type"]["enum"]) == ENVELOPE_TYPES
    assert set(props["status"]["enum"]) == ENVELOPE_STATUSES
    assert set(props["language"]["enum"]) == {"ru", "en"}
    # resolved restricted to task/report: the conditional branch for
    # checkpoint/decision excludes it.
    branches = [
        conditional
        for conditional in envelope["allOf"]
        if conditional.get("if", {}).get("properties", {}).get("type", {}).get("enum")
        == ["checkpoint", "decision"]
    ]
    assert branches, "checkpoint/decision status if/then branch must exist"
    refs = [branch.get("then", {}).get("$ref") for branch in branches]
    assert refs == ["#/$defs/status-excluding-resolved"]
    excluded_statuses = set(
        envelope["$defs"]["status-excluding-resolved"]["properties"]["status"]["enum"]
    )
    assert "resolved" not in excluded_statuses
    task_extras = envelope["$defs"]["task-extras"]["properties"]
    assert set(task_extras["priority"]["enum"]) == {"P0", "P1", "P2", "P3"}
    assert set(task_extras["size"]["enum"]) == {"XS", "S", "M", "L"}
    assert "resolved" in ENVELOPE_STATUSES
    for not_resolved in ENVELOPE_TYPES - RESOLVED_ALLOWED_TYPES:
        assert not_resolved in {"checkpoint", "decision"}


# ---------------------------------------------------------------------------
# 9. SDK real-channel echo (cascade fix-b B5 — QA P3-3): the sdk.remember
# canon-warnings echo asserted through the REAL channel, not a trusted-add
# proxy.
# ---------------------------------------------------------------------------


def test_sdk_remember_echoes_codes_through_real_channel(
    mgr: MemoryManager, caplog: pytest.LogCaptureFixture
) -> None:
    from vesmaro.sdk import VesmaSDK

    sdk = VesmaSDK(manager=mgr)
    with caplog.at_level(logging.WARNING):
        memory = sdk.remember(
            "встреча вчера",
            project="canonproj",
            agent="sdk-agent",
            tags=["project:canonproj", "agent:sdk-agent", "mnemos:learning"],
            metadata={
                "canon": {
                    "schema_version": "1",
                    "type": "report",
                    "status": "active",
                    "language": "ru",
                    "period": None,
                }
            },
        )
    codes = [w["code"] for w in memory.metadata.get("canon_warnings", [])]
    assert "CANON-E-DATE" in codes
    assert "sdk.remember: canon warnings" in caplog.text
    assert "CANON-E-DATE" in caplog.text
