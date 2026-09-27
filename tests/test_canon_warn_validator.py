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
    CANON_WARN_CODES): exactly the six ratified codes."""
    assert set(CANON_WARN_CODES) == {
        "CANON-E-ENVELOPE",
        "CANON-E-SECTION",
        "CANON-E-STATUS",
        "CANON-E-TITLE",
        "CANON-E-LANGUAGE",
        "CANON-E-DATE",
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
# 5. Drift pin — CANON_REQUIRED_SECTIONS vs the schemas' x-canon-sections
# ---------------------------------------------------------------------------


def _canon_repo_sections() -> dict[str, list[str]] | None:
    """Read x-canon-sections from the pinned canon schemas when the sibling
    checkout exists (same pin protocol as the S1 drift test)."""
    candidate = Path(__file__).resolve().parents[2] / "vesmaro-canon" / "schemas"
    if not candidate.is_dir():
        return None
    out: dict[str, list[str]] = {}
    for rtype in ("checkpoint", "task", "decision", "report"):
        schema = candidate / f"{rtype}.schema.json"
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
