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
    MemoryCreate,
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
