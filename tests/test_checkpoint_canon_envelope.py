"""vesmaro-canon v1.0.0 — checkpoint envelope + always-five-sections render.

Covers the W2-S1 slice (store card vesmaro-canon-w2-s1-envelope) against a
REAL MemoryManager (tmp SQLite), per ADR-0003 (verdict B option A) and
canon v1.0.0:

  1. Envelope mint — ``save_checkpoint`` stamps ``metadata.canon``
     (``schema_version="1"``, ``type="checkpoint"``, ``status="active"``,
     ``language``, nullable ``session_ref``); the language comes from the
     ``language=`` parameter with the ``mnemos.checkpoint_language``
     config default; clients never set the envelope.
  2. Server-minted-only discipline — ``"canon"`` joined
     ``CHECKPOINT_STAMP_KEYS``: generic create/update paths strip a
     client-forged ``metadata.canon`` (the same pattern that already
     strips the identity stamps), and an external update can neither
     forge nor drop a minted envelope (merge-back).
  3. Canon §3 option A — the render ALWAYS emits the five EN sections in
     CHECKPOINT_FIELDS order; an empty field gets the deterministic
     per-language placeholder line (never skipped, order never depends
     on payload emptiness).
  4. Drift pin — the explicit field→title map (``.title()`` removed,
     ADR-0003 obligation 2) stays equal to the schemas'
     ``x-canon-sections`` annotation.
  5. Downstream safety — placeholder-only Goals never becomes an
     awareness goal title; a dedup hit returns the first-minted row with
     its own envelope (language never rewrites a stored record, canon
     §10).

Transitional rule (canon §9): records WITHOUT ``metadata.canon`` are
outside canon scope — asserted for a pre-W2 row (not a violation).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from vesmaro.config import Settings
from vesmaro.manager import MemoryManager
from vesmaro.models import (
    CANON_SCHEMA_VERSION,
    CHECKPOINT_FIELDS,
    CHECKPOINT_PLACEHOLDER_LINES,
    CHECKPOINT_PLACEHOLDERS,
    CHECKPOINT_SECTION_TITLES,
    CHECKPOINT_STAMP_KEYS,
    Memory,
    MemoryCreate,
    MemorySource,
    MemoryStatus,
    MemoryUpdate,
    checkpoint_canon_envelope,
)

# The pinned canon sections annotation of vesmaro-canon/schemas/
# checkpoint.schema.json (x-canon-sections). The drift test compares the
# live constants against THIS list; when the pinned repo is absent the
# fallback is the same literal (byte-exact) — the test then still pins
# the map against the frozen canon values in this file.
_CANON_REPO_SECTIONS_FALLBACK: list[str] = [
    "Goals",
    "Completed",
    "In Progress",
    "Decisions",
    "Context",
]


def _canon_sections() -> list[str]:
    """Read ``x-canon-sections`` from the pinned canon schemas if present.

    The canon repo is a sibling checkout in this workspace; the drift
    test prefers the LIVE annotation over an in-repo copy so a canon-side
    change breaks the engine test LOUDLY (the pin protocol, canon §9).
    """
    candidate = (
        Path(__file__).resolve().parents[2] / "vesmaro-canon" / "schemas" / "checkpoint.schema.json"
    )
    if candidate.is_file():
        annotation: Any = json.loads(candidate.read_text(encoding="utf-8"))["x-canon-sections"]
        return list(annotation)
    return list(_CANON_REPO_SECTIONS_FALLBACK)


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/test_save_context_identity.py)
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


@pytest.fixture
def client_factory(mgr):
    """FastAPI TestClient factory over the shared isolated manager
    (mirrors tests/test_save_context_identity.py::client)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from vesmaro.api import main as api_main
    from vesmaro.api.main import app, lifespan

    class _Factory:
        def __enter__(self) -> TestClient:
            test_app = FastAPI(title="Mnemos-Test-Canon", version="0.1.0", lifespan=lifespan)
            for route in app.routes:
                test_app.routes.append(route)
            api_main._manager = mgr
            self._tc = TestClient(test_app)
            return self._tc

        def __exit__(self, *exc: object) -> None:
            self._tc.close()
            api_main._manager = None

    return _Factory()


# ---------------------------------------------------------------------------
# 1. Envelope mint
# ---------------------------------------------------------------------------


def test_envelope_minted_with_default_language(mgr: MemoryManager) -> None:
    memory, duplicate = mgr.save_checkpoint({"goals": "ship canon"}, project="canonproj")
    assert duplicate is False
    canon = memory.metadata["canon"]
    assert canon == {
        "schema_version": CANON_SCHEMA_VERSION,
        "type": "checkpoint",
        "status": "active",
        "language": mgr.settings.mnemos.checkpoint_language,
        "session_ref": None,
    }


def test_envelope_language_param_overrides_config(mgr: MemoryManager) -> None:
    mgr.settings.mnemos.checkpoint_language = "en"
    memory, _dup = mgr.save_checkpoint({"goals": "g"}, project="canonproj", session="sess-l1")
    memory_ru, _dup2 = mgr.save_checkpoint(
        {"goals": "g", "in_progress": "x"}, project="canonproj", language="ru"
    )
    assert memory.metadata["canon"]["language"] == "en"
    assert memory_ru.metadata["canon"]["language"] == "ru"


def test_envelope_session_ref_carries_the_session(mgr: MemoryManager) -> None:
    memory, _dup = mgr.save_checkpoint({"goals": "g"}, project="canonproj", session="sess-42")
    assert memory.metadata["canon"]["session_ref"] == "sess-42"


def test_envelope_invalid_language_fails_loud(mgr: MemoryManager) -> None:
    with pytest.raises(ValueError, match="language must be one of"):
        mgr.save_checkpoint({"goals": "g"}, project="canonproj", language="fr")
    with pytest.raises(ValueError, match="language must be one of"):
        mgr.settings.mnemos.checkpoint_language = "de"  # type: ignore[assignment]
        mgr.save_checkpoint({"goals": "g2"}, project="canonproj")
    assert mgr.stats()["total"] == 0  # nothing stored by either rejected call


def test_rest_language_pass_through(client_factory: Any, mgr: MemoryManager) -> None:
    """The REST twin threads the per-call language into the envelope;
    an unknown language is a caller error (422 at the pydantic boundary)."""
    with client_factory as tc:
        ok = tc.post(
            "/context/save",
            json={"project": "p251", "goals": "g", "language": "en"},
        )
        assert ok.status_code == 201
        memory = mgr.sqlite.get(ok.json()["id"])
        assert memory is not None
        assert memory.metadata["canon"]["language"] == "en"

        bad = tc.post(
            "/context/save",
            json={"project": "p251", "goals": "g2", "language": "de"},
        )
        assert bad.status_code == 422  # pydantic Literal rejects before the manager
        assert mgr.stats()["total"] == 1  # only the valid save stored


async def test_mcp_language_pass_through(mgr: MemoryManager) -> None:
    from unittest.mock import patch

    from vesmaro.mcp_server import _dispatch

    with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
        out = await _dispatch(
            "mnemos_save_context",
            {"project": "p251", "goals": "g", "language": "en"},
        )
        assert isinstance(out, str) and "Context saved" in out
    memory = mgr.list_recent(limit=1, project="p251")[0]
    assert memory.metadata["canon"]["language"] == "en"


def test_envelope_mint_helper_rejects_unknown_language() -> None:
    with pytest.raises(ValueError, match="language must be one of"):
        checkpoint_canon_envelope(language="fr")


# ---------------------------------------------------------------------------
# 2. Server-minted-only discipline (client-forged metadata.canon stripped)
# ---------------------------------------------------------------------------


def test_generic_create_strips_forged_canon_envelope(mgr: MemoryManager) -> None:
    forged = {
        "schema_version": "1",
        "type": "checkpoint",
        "status": "active",
        "language": "ru",
        "session_ref": None,
    }
    memory = mgr.add(
        MemoryCreate(
            content="forged envelope row",
            tags=["project:canonproj", "agent:mallory", "mnemos:checkpoint"],
            metadata={"canon": forged, "harmless": 1},
        ),
        project="canonproj",
        agent="mallory",
    )
    assert memory.metadata["harmless"] == 1
    assert "canon" not in memory.metadata
    assert "checkpoint_agent" not in memory.metadata


def test_update_cannot_forge_or_drop_canon_envelope(mgr: MemoryManager) -> None:
    memory, _dup = mgr.save_checkpoint({"goals": "g"}, project="canonproj")
    minted = memory.metadata["canon"]

    # An external update cannot REWRITE the minted envelope...
    updated = mgr.update(
        memory.id,
        MemoryUpdate(metadata={"canon": {"schema_version": "9", "type": "task"}, "note": 1}),
    )
    assert updated is not None
    assert updated.metadata["canon"] == minted
    assert updated.metadata["note"] == 1

    # ...nor DROP it (the merge-back restores the server value).
    stripped = mgr.update(memory.id, MemoryUpdate(metadata={"other": 2}))
    assert stripped is not None
    assert stripped.metadata["canon"] == minted


def test_canon_envelope_key_is_in_stamp_keys() -> None:
    """The envelope key rides CHECKPOINT_STAMP_KEYS — one strip pattern,
    every create/update path covered (spec: mirror manager.py:813-824)."""
    assert "canon" in CHECKPOINT_STAMP_KEYS


def test_pre_canon_row_without_envelope_is_out_of_canon_scope(
    mgr: MemoryManager,
) -> None:
    """Transitional rule (canon §9): a record without metadata.canon is
    outside canon scope — not a violation. Pre-W2-style rows (no canon
    key) must still save and read normally."""
    legacy = mgr.add(
        MemoryCreate(
            content="legacy row without an envelope",
            tags=["project:canonproj", "agent:legacy", "mnemos:note"],
        ),
        project="canonproj",
        agent="legacy",
    )
    assert "canon" not in legacy.metadata  # no envelope minted on generic creates


# ---------------------------------------------------------------------------
# 3. Option A — the render always emits the five sections
# ---------------------------------------------------------------------------


def _section_order(content: str) -> list[str]:
    """The `^## <Name>$` headers of a rendered checkpoint, in order."""
    headers = [line.removeprefix("## ") for line in content.splitlines() if line.startswith("## ")]
    return headers


def test_render_emits_all_five_sections_even_when_fields_empty(
    mgr: MemoryManager,
) -> None:
    memory, _dup = mgr.save_checkpoint({"goals": "the only filled field"}, project="canonproj")
    assert _section_order(memory.content) == _canon_sections()
    # Empty fields carry the deterministic EN placeholder lines (default lang ru).
    placeholders = [CHECKPOINT_PLACEHOLDERS[(f, "ru")] for f in CHECKPOINT_FIELDS if f != "goals"]
    for line in placeholders:
        assert line in memory.content


def test_section_order_is_independent_of_payload_shape(mgr: MemoryManager) -> None:
    """Determinism: the same sections in the same order for any payload —
    full five, single field, or sparse pairs."""
    order = _canon_sections()
    for fields in (
        {"goals": "g", "completed": "c", "in_progress": "i", "decisions": "d", "context": "x"},
        {"goals": "g"},
        {"decisions": "d", "context": "x"},
    ):
        memory, _dup = mgr.save_checkpoint(fields, project="canonproj")
        assert _section_order(memory.content) == order


def test_placeholder_lines_match_the_language(mgr: MemoryManager) -> None:
    memory_en, _dup = mgr.save_checkpoint({"goals": "g"}, project="canonproj", language="en")
    memory_ru, _dup2 = mgr.save_checkpoint({"in_progress": "i"}, project="canonproj", language="ru")
    assert CHECKPOINT_PLACEHOLDERS[("completed", "en")] in memory_en.content
    assert CHECKPOINT_PLACEHOLDERS[("completed", "ru")] not in memory_en.content
    assert CHECKPOINT_PLACEHOLDERS[("goals", "ru")] in memory_ru.content


def test_placeholder_set_is_the_single_membership_truth() -> None:
    """CHECKPOINT_PLACEHOLDER_LINES == the values of the placeholder map —
    the awareness guard and the render share one source."""
    assert set(CHECKPOINT_PLACEHOLDERS.values()) == CHECKPOINT_PLACEHOLDER_LINES


def test_dedup_hit_returns_the_first_row_with_its_own_envelope(
    mgr: MemoryManager,
) -> None:
    first, dup1 = mgr.save_checkpoint({"goals": "same payload"}, project="canonproj", language="en")
    second, dup2 = mgr.save_checkpoint(
        {"goals": "same payload", "in_progress": ""}, project="canonproj", language="ru"
    )
    assert dup1 is False and dup2 is True
    assert first.id == second.id
    # The stored envelope is the FIRST mint, never rewritten by the hit.
    assert second.metadata["canon"]["language"] == "en"


# ---------------------------------------------------------------------------
# 4. Drift pin — explicit title map vs the schemas' x-canon-sections
# ---------------------------------------------------------------------------


def test_title_map_matches_x_canon_sections() -> None:
    """ADR-0003 obligation 2: the field→title map must equal the canon
    schemas' annotation (the schema is the pinned source of truth)."""
    live = [CHECKPOINT_SECTION_TITLES[f] for f in CHECKPOINT_FIELDS]
    assert live == _canon_sections()


def test_title_map_covers_every_checkpoint_field() -> None:
    assert set(CHECKPOINT_SECTION_TITLES) == set(CHECKPOINT_FIELDS)


def test_titles_are_canon_frozen_literals() -> None:
    """Byte-exact canon §3 headers — the map is explicit, never derived
    (e.g. ``in_progress`` must be "In Progress", not Python's
    ``str.title()`` idiom which a future stdlib change could drift)."""
    assert CHECKPOINT_SECTION_TITLES["in_progress"] == "In Progress"
    assert CHECKPOINT_SECTION_TITLES["goals"] == "Goals"


def test_render_uses_the_map_not_str_title(mgr: MemoryManager) -> None:
    memory, _dup = mgr.save_checkpoint({"in_progress": "wip"}, project="canonproj")
    assert "## In Progress\n" in memory.content


# ---------------------------------------------------------------------------
# 5. Downstream safety — placeholders never become awareness goal titles
# ---------------------------------------------------------------------------


def test_placeholder_goals_never_reach_conflict_hints(mgr: MemoryManager) -> None:
    from vesmaro.awareness import checkpoint_goal_title

    # Two agents whose Goals sections carry ONLY the placeholder must not
    # manufacture a conflict hint (placeholder == template, not a goal).
    agent_a, _dup_a = mgr.save_checkpoint(
        {"completed": "c"}, project="canonproj", agent="alpha", language="en"
    )
    agent_b, _dup_b = mgr.save_checkpoint(
        {"completed": "c"}, project="canonproj", agent="beta", language="en"
    )
    assert checkpoint_goal_title(agent_a) is None
    assert checkpoint_goal_title(agent_b) is None

    # A real goal still yields a title — the guard is placeholder-only.
    real, _dup_real = mgr.save_checkpoint(
        {"goals": "ship the retry budget fix"}, project="canonproj", agent="gamma"
    )
    assert checkpoint_goal_title(real) == "ship the retry budget fix"


def test_placeholder_goals_cannot_manufacture_hints_even_if_leaked() -> None:
    """Belt-and-braces under the hint threshold: even if a placeholder
    leaked past the goal-title guard into the tokenizer, no two
    placeholder lines share the >= 2 non-stopword tokens that fire a
    conflict hint (CONFLICT_HINT_MIN_SHARED_TOKENS)."""
    import itertools

    from vesmaro.awareness import _goal_tokens

    lines = sorted(CHECKPOINT_PLACEHOLDER_LINES)
    for a, b in itertools.combinations(lines, 2):
        assert len(_goal_tokens(a) & _goal_tokens(b)) < 2, (a, b)


# ---------------------------------------------------------------------------
# 6. W2-S3 — dedup guards (store card vesmaro-canon-w2-s3-dedup-guards)
# ---------------------------------------------------------------------------


def _planted_violation_row(mgr: MemoryManager) -> Memory:
    """A stored canon checkpoint whose BODY violates the canon (§5 date
    rule) while the envelope stays intact — planted through the
    store-internal ``update_fields`` (no manager gate re-run), the same
    way the strata materializer plants legacy rows."""
    from vesmaro.canon_validate import CANON_WARN_CODES, validate_canon_record

    memory, _dup = mgr.save_checkpoint({"goals": "same payload"}, project="canonproj")
    assert "canon_warnings" not in memory.metadata
    body_violation = memory.content + "\nreviewed 27.09, fixed yesterday\n"
    assert mgr.sqlite.update_fields(memory.id, content=body_violation)
    # The planted row WOULD violate on a real gate run (sanity for the
    # pin below — the validator is the same one _canon_gate calls).
    violations = validate_canon_record(
        content=body_violation,
        title=None,
        metadata=memory.metadata,
    )
    assert [v.code for v in violations], "planting must produce a violation"
    assert set(CANON_WARN_CODES) >= {v.code for v in violations}
    return memory


def test_dedup_hit_skips_the_canon_gate(
    mgr: MemoryManager,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S3-1 (canon §9/§10): a dedup hit is NOT a write — the existing
    record is returned WITHOUT running the canon gate on it. A
    violation-shaped stored row coming back from dedup must carry no
    ``canon_warnings`` and emit no ``canon_violation:`` line."""
    import logging

    planted = _planted_violation_row(mgr)
    with caplog.at_level(logging.WARNING, logger="vesmaro.manager"):
        existing, duplicate = mgr.save_checkpoint({"goals": "same payload"}, project="canonproj")
    assert duplicate is True
    assert existing.id == planted.id
    # The hit returned the stored record untouched by the gate.
    assert "canon_warnings" not in existing.metadata
    stored = mgr.sqlite.get(existing.id)
    assert stored is not None
    assert "canon_warnings" not in stored.metadata
    # And the gate never spoke (contrast: the S2 warn-mode tests pin the
    # 'canon_violation:' line on real writes).
    assert not [r for r in caplog.records if "canon_violation:" in r.message]


def test_trivial_reject_precedes_render_and_canon_gate(
    mgr: MemoryManager,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S3-2: an all-empty checkpoint is a CALLER bug (canon §9) —
    ValueError BEFORE any store side effect, so it can never produce a
    record with five placeholder sections nor reach the canon gate."""
    import logging

    with (
        caplog.at_level(logging.WARNING, logger="vesmaro.manager"),
        pytest.raises(ValueError, match="checkpoint rejected: all fields"),
    ):
        mgr.save_checkpoint(
            {"goals": "", "completed": None, "in_progress": "", "decisions": None},
            project="canonproj",
        )
    assert mgr.stats()["total"] == 0  # zero-loss: nothing stored, no placeholder shell
    assert not [r for r in caplog.records if "canon_violation:" in r.message]

    # Same verdict with a session presented — the reject still fires
    # before any binding/dedup/envelope work.
    with pytest.raises(ValueError, match="checkpoint rejected: all fields"):
        mgr.save_checkpoint(
            {"goals": None, "context": ""}, project="canonproj", session="sess-empty"
        )
    assert mgr.stats()["total"] == 0


def test_trivial_reject_precedes_the_envelope_language_gate(mgr: MemoryManager) -> None:
    """Ordering pin: the trivial-reject is the FIRST content gate — an
    all-empty call with an invalid language fails with the checkpoint
    message, never the envelope's language error (the envelope is minted
    later, only for records that passed the reject)."""
    with pytest.raises(ValueError, match="checkpoint rejected: all fields"):
        mgr.save_checkpoint({"goals": ""}, project="canonproj", language="fr")


def test_partial_empty_checkpoint_still_stores(mgr: MemoryManager) -> None:
    """The reject is for ALL-empty only — placeholders exist for SOME
    empty fields (canon §3 option A); one real field is a legal record."""
    memory, duplicate = mgr.save_checkpoint({"decisions": "keep the gate"}, project="canonproj")
    assert duplicate is False
    assert CHECKPOINT_SECTION_TITLES["goals"] in memory.content  # placeholder section present


def test_dedup_hash_is_exactly_the_field_payload(mgr: MemoryManager) -> None:
    """S3-3 (the load-bearing pin): the dedup key is SHA-256 over
    ``[project, agent, *(normalized[f] for f in CHECKPOINT_FIELDS)]``
    with None normalized to "" — NOTHING else. Recomputed here from the
    spec formula and compared against the stored stamp."""
    import hashlib
    import json

    fields = {"goals": "g", "in_progress": "wip"}
    normalized = [(fields.get(f) or "") for f in CHECKPOINT_FIELDS]
    canonical = json.dumps(
        ["canonproj", "hasher", *normalized],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    memory, _dup = mgr.save_checkpoint(fields, project="canonproj", agent="hasher")
    assert memory.metadata["checkpoint_dedup_key"] == expected

    # The stored stamp IS the lookup key — a re-save collides on it.
    again, duplicate = mgr.save_checkpoint(fields, project="canonproj", agent="hasher")
    assert duplicate is True and again.id == memory.id


def test_dedup_key_excludes_render_and_language(mgr: MemoryManager) -> None:
    """S3-3: a test that changes the RENDER (placeholders, titles — here
    via the per-call language) cannot move the dedup key: two saves
    differing only in render-only details dedup-collide; the hash does
    NOT include the language/config (canon §10 — a dedup hit returns the
    first-minted row, the new call's language never rewrites it)."""
    mgr.settings.mnemos.checkpoint_language = "en"
    first, dup1 = mgr.save_checkpoint(
        {"goals": "render-blind payload"}, project="canonproj", agent="rend"
    )
    assert dup1 is False
    # Flip ONLY the render inputs (config-driven placeholder set changes).
    mgr.settings.mnemos.checkpoint_language = "ru"
    second, dup2 = mgr.save_checkpoint(
        {"goals": "render-blind payload"}, project="canonproj", agent="rend"
    )
    assert dup2 is True
    assert second.id == first.id
    # The stored render is the FIRST mint (en placeholders), untouched.
    assert CHECKPOINT_PLACEHOLDERS[("completed", "en")] in second.content
    assert CHECKPOINT_PLACEHOLDERS[("completed", "ru")] not in second.content
    assert second.metadata["canon"]["language"] == "en"


def test_dedup_key_excludes_per_call_language_param(mgr: MemoryManager) -> None:
    """Same pin at the per-call surface (no config mutation): identical
    payloads with different ``language=`` arguments collide."""
    first, _dup1 = mgr.save_checkpoint(
        {"goals": "lang-param payload"}, project="canonproj", language="ru"
    )
    second, dup2 = mgr.save_checkpoint(
        {"goals": "lang-param payload"}, project="canonproj", language="en"
    )
    assert dup2 is True and second.id == first.id


# ---------------------------------------------------------------------------
# 7. W2-S4 — envelope-aware reads (store card vesmaro-canon-w2-s4-renderers)
# ---------------------------------------------------------------------------


def _import_goal_title():
    from vesmaro.awareness import checkpoint_goal_title

    return checkpoint_goal_title


def test_canon_record_goal_via_the_frozen_section_map(mgr: MemoryManager) -> None:
    """S4-1 envelope-present path: the Goals body of a canon record is
    located by the frozen H2 map and returned as the title."""
    checkpoint_goal_title = _import_goal_title()
    memory, _dup = mgr.save_checkpoint(
        {"goals": "land the dedup guards", "in_progress": "wip"},
        project="canonproj",
        language="ru",
    )
    assert memory.metadata["canon"]["language"] == "ru"
    assert checkpoint_goal_title(memory) == "land the dedup guards"


def test_canon_record_placeholder_goals_yield_none_in_both_languages(
    mgr: MemoryManager,
) -> None:
    """S4-1: the per-language placeholder from the ENVELOPE's declared
    language is template, not a goal — for ru AND en records."""
    checkpoint_goal_title = _import_goal_title()
    ru, _dup_ru = mgr.save_checkpoint({"context": "c"}, project="canonproj", language="ru")
    en, _dup_en = mgr.save_checkpoint({"context": "c"}, project="canonproj", language="en")
    assert checkpoint_goal_title(ru) is None
    assert checkpoint_goal_title(en) is None


def test_canon_record_uses_the_per_language_placeholder_not_the_full_set(
    mgr: MemoryManager,
) -> None:
    """S4-1 language-awareness: a canon ru record whose Goals body
    carries the EN placeholder line as user prose still yields it as a
    goal — the OTHER language's placeholder is not a template for this
    record (canon §6: one language per record). The legacy branch (full
    literal set) would have wrongly skipped it."""
    checkpoint_goal_title = _import_goal_title()
    from vesmaro.models import Memory as _Memory
    from vesmaro.models import MemorySource

    en_goals_placeholder = CHECKPOINT_PLACEHOLDERS[("goals", "en")]
    row = _Memory(
        content="\n".join(
            [
                "# Session checkpoint — probe",
                f"## {CHECKPOINT_SECTION_TITLES['goals']}",
                en_goals_placeholder,
                f"## {CHECKPOINT_SECTION_TITLES['completed']}",
                CHECKPOINT_PLACEHOLDERS[("completed", "ru")],
            ]
        ),
        tags=["project:canonproj", "agent:seed", "mnemos:checkpoint"],
        source=MemorySource.MCP,
        status=MemoryStatus.PUBLISHED,
        metadata={
            "checkpoint_agent": "seed",
            "checkpoint_session": "sess-seed",
            "checkpoint_dedup_key": "probe-key",
            "canon": {
                "schema_version": CANON_SCHEMA_VERSION,
                "type": "checkpoint",
                "status": "active",
                "language": "ru",
                "session_ref": "sess-seed",
            },
        },
        project="canonproj",
        agent="seed",
    )
    mgr.sqlite.save(row)
    restamped = mgr.sqlite.get(row.id)
    assert restamped is not None
    # Envelope-aware branch: the foreign placeholder is USER PROSE here.
    assert checkpoint_goal_title(restamped) == en_goals_placeholder


def test_pre_canon_row_keeps_the_first_line_heuristic(mgr: MemoryManager) -> None:
    """S4-1 pre-canon path (canon §9 transitional): a legacy checkpoint
    row WITHOUT ``metadata.canon`` still resolves through the regex
    fallback — materializer-shaped rows keep working unchanged."""
    checkpoint_goal_title = _import_goal_title()
    from vesmaro.models import Memory as _Memory
    from vesmaro.models import MemorySource

    legacy = _Memory(
        content="# Session checkpoint — old render\n## Goals\npre-canon goal line\n",
        tags=["project:canonproj", "agent:legacy", "mnemos:checkpoint"],
        source=MemorySource.MCP,
        status=MemoryStatus.PUBLISHED,
        metadata={"checkpoint_agent": "legacy"},  # no envelope
        project="canonproj",
        agent="legacy",
    )
    assert "canon" not in legacy.metadata
    assert checkpoint_goal_title(legacy) == "pre-canon goal line"

    # And an empty-goals legacy row (no Goals header at all) stays None.
    legacy_empty = _Memory(
        content="# Session checkpoint — old render\n",
        tags=["project:canonproj", "agent:legacy", "mnemos:checkpoint"],
        source=MemorySource.MCP,
        status=MemoryStatus.PUBLISHED,
        metadata={"checkpoint_agent": "legacy"},
        project="canonproj",
        agent="legacy",
    )
    assert checkpoint_goal_title(legacy_empty) is None


def test_malformed_envelope_falls_back_to_the_legacy_heuristic(mgr: MemoryManager) -> None:
    """S4-1 defensive pin: a record with a checkpoint_agent stamp whose
    envelope carries an out-of-enum language reads through the legacy
    heuristic — never a wrong-language placeholder pass."""
    checkpoint_goal_title = _import_goal_title()
    from vesmaro.models import Memory as _Memory
    from vesmaro.models import MemorySource

    row = _Memory(
        content=(
            "# Session checkpoint — x\n## Goals\nheuristic goal\n"
            "## Completed\n(nothing completed yet.)\n"
        ),
        tags=["project:canonproj", "agent:x", "mnemos:checkpoint"],
        source=MemorySource.MCP,
        status=MemoryStatus.PUBLISHED,
        metadata={
            "checkpoint_agent": "x",
            "canon": {"schema_version": "1", "type": "checkpoint", "language": "de"},
        },
        project="canonproj",
        agent="x",
    )
    assert checkpoint_goal_title(row) == "heuristic goal"


def test_canon_goal_title_is_bounded(mgr: MemoryManager) -> None:
    """The envelope-aware branch keeps the GOAL_TITLE_MAX_CHARS bound."""
    checkpoint_goal_title = _import_goal_title()
    from vesmaro.awareness import GOAL_TITLE_MAX_CHARS

    memory, _dup = mgr.save_checkpoint(
        {"goals": "g" * (GOAL_TITLE_MAX_CHARS + 50)}, project="canonproj"
    )
    title = checkpoint_goal_title(memory)
    assert title is not None and len(title) == GOAL_TITLE_MAX_CHARS


def _plant_legacy_goal_checkpoint(mgr: MemoryManager, *, agent: str, session: str) -> None:
    """Plant a PRE-CANON checkpoint row (stamped, no envelope) the way
    the strata materializer does — via the trusted internal add."""
    from vesmaro.models import MemoryCreate

    body = "\n".join(
        [
            "# Session checkpoint — legacy",
            f"## {CHECKPOINT_SECTION_TITLES['goals']}",
            "legacy real goal",
            f"## {CHECKPOINT_SECTION_TITLES['completed']}",
            CHECKPOINT_PLACEHOLDERS[("completed", "ru")],
        ]
    )
    mgr.add(
        MemoryCreate(
            content=body,
            tags=["project:canonproj", f"agent:{agent}", "mnemos:checkpoint"],
            source=MemorySource.MCP,
            metadata={"checkpoint_agent": agent, "checkpoint_session": session},
        ),
        project="canonproj",
        agent=agent,
        trusted_checkpoint_stamps=True,
    )


def test_delta_surfaces_never_leak_placeholders(mgr: MemoryManager) -> None:
    """S4-2 audit, pinned as a test: project_delta surfaces goal titles
    ONLY through ``checkpoint_goal_title`` (placeholder-guarded) and the
    render embeds slot facts + that guarded title — no body text, so no
    placeholder line can leak into any awareness surface. Envelope-present
    and pre-canon neighbors both audited."""
    from vesmaro.awareness import project_delta, render_awareness_section

    # Envelope-present neighbor: placeholder-only Goals → no goal title.
    mgr.save_checkpoint({"completed": "c"}, project="canonproj", agent="neighbor-a", language="ru")
    # Pre-canon neighbor with a real goal (heuristic path must keep working).
    _plant_legacy_goal_checkpoint(mgr, agent="neighbor-b", session="sess-b")

    delta = project_delta(mgr, project="canonproj", since=_hour_ago_iso(), exclude_agent="caller")
    titles = {a["agent"]: a.get("goal_title") for a in delta["agents"]}
    assert titles["neighbor-a"] is None  # placeholder never surfaced
    assert titles["neighbor-b"] == "legacy real goal"

    rendered = render_awareness_section(delta, [])
    for line in CHECKPOINT_PLACEHOLDER_LINES:
        assert line not in rendered
    assert "legacy real goal" in rendered


def _hour_ago_iso() -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(hours=1)).isoformat()
