"""ADR-0025 E1 — deterministic retrieval lanes tests (mnemos #253).

Acceptance map (issue #253 acceptance clause → test):

* lanes on → blocks carry ``lane``, rules/decisions surface
  deterministically → ``TestLanesHappyPath``
* empty lane results (no rules/decisions in store → no crash, no empty
  sections) → ``TestEmptyLanes``
* FLAG-OFF EQUIVALENCE — assemble output byte-identical with the flag
  off vs the pre-change code path → ``TestFlagOffEquivalence`` (the
  ``*_FIXTURE_SHA256`` constants were captured from the PRISTINE
  b8968df tree before any lanes code existed: frozen assemble clock,
  UUID-normalized ``json.dumps(sort_keys=True)`` — see
  ``test_flag_off_reproduces_pre_change_fixture``)
* ordering stability — same session, repeated assemblies → byte-stable
  block order under lane ordering; interleaved scores do not reorder
  across runs → ``TestOrderingStability`` (integration) and
  ``TestBudgetOrderingUnit`` (pure sort semantics)
* cascade-ready / awareness-ready contracts (R2/R3) →
  ``TestCascadeReadyContracts`` / ``TestAwarenessContracts``
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests._seeded_ids import seeded_memory_ids
from vesma.assemble import _budget_stage, _Candidate
from vesma.config import Settings
from vesma.lanes import (
    AWARENESS_CURSOR_PREFIX,
    B0_TYPE_BOOST_FACTOR,
    Lane,
    assert_foreign_lanes_tail_only,
    awareness_cursor_key,
    deterministic_lane_results,
    lane_sort_key,
    read_awareness_cursor,
    write_awareness_cursor,
)
from vesma.manager import MemoryManager
from vesma.models import (
    Memory,
    MemoryCreate,
    MemorySource,
    MemoryStatus,
    PipelineState,
)

PROJECT = "asm-proj"
AGENT = "asm-agent"
SESSION = "sess-e1"

# ── Flag-off equivalence fixtures ───────────────────────────────────────────
#
# Captured on pristine b8968df (pre-lanes), then RE-CAPTURED after the
# hybrid_alpha 0.7→0.5 re-tune (#300): the fusion weight IS observable
# assemble output on the default path, so the fixture moved with the
# re-tune — a registered act (the alpha re-tune PR), not silent drift.
# RE-CAPTURED again for the Phase-1 deterministic id tiebreak (#280, TL
# decision 2026-09-14): equal-score groups now order by Memory.id. Only
# the NO-FILE fixture moved — its derived-query recall carries one
# equal-score tie group, whose order is now the (seeded, fixed) id
# order instead of the pre-change insertion order. The WITH-FILE
# fixture (explicit query + applyTo pinning) has NO tie groups and is
# byte-identical to the pre-tiebreak capture. The generator now pins
# the fixed seeded-id draw (tests/_seeded_ids.py) instead of per-run
# uuid4 luck — same corpus + frozen assemble clock. UUIDs are
# normalized (memory ids are per-run) — everything else in the dump
# (block order, scores, provenance, key sets, text) is compared
# byte-for-byte via sha256. With LanesConfig.enabled=False the code
# must reproduce these hashes; ANY unregistered observable output
# change on the default path fails here.

NO_FILE_FIXTURE_SHA256 = "9e08a4496199fc7a6444e1e6927b5ed22cdf2eab5c72a94f9ceec89e0660caf1"
WITH_FILE_FIXTURE_SHA256 = "3ed0db35035dc6e3a6d9fa8157da18fed3d31ce7a10cb460180723ba5294ac30"

FROZEN_ISO = "2026-09-13T12:00:00+00:00"
FROZEN = datetime.fromisoformat(FROZEN_ISO)

RULE_APPLYTO = "# Handler rule\nAlways run ruff before committing handler code."
RULE_PLAIN = "# Release rule\nCut releases only from green CI."
DECISION = "Decision: use SQLite WAL mode for the handler store."
KNOW_A = (
    "Deployment guide for the handler service.\n"
    "The service is configured through the deployment manifest and the\n"
    "access policy is rotated quarterly per the security baseline.\n"
)
KNOW_B = (
    "import json\n"
    "import logging\n"
    "\n"
    "def handler(event, context):\n"
    '    """Route the deployment event."""\n'
    "    return json.dumps(event)\n"
)
KNOW_C = "Incident notes: the handler queue backed up on 2026-08-01 due to a slow consumer."


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz: object = None) -> datetime:
        return FROZEN if tz is not None else FROZEN.replace(tzinfo=None)


_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _normalized_sha256(result: dict[str, Any]) -> str:
    raw = json.dumps(result, sort_keys=True, indent=1)
    return hashlib.sha256(_UUID_RE.sub("<uuid>", raw).encode()).hexdigest()


# ── Shared helpers ────────────────────────────────────────────────────────────


def _settings(tmp: Path, *, lanes_enabled: bool = False, type_boost: bool = False) -> Settings:
    settings = Settings(
        vesma={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
        },
        scanner={"enabled": False},
        ccr={
            "min_size_chars": 100,
            "max_entries": 100,
            "ttl_days": 1,
        },  # type: ignore[arg-type]
        lanes={"enabled": lanes_enabled, "type_boost": type_boost},
    )
    settings.resolve_paths()
    return settings


def _manager(settings: Settings) -> MemoryManager:
    mgr = MemoryManager(settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    return mgr


@pytest.fixture
def manager() -> Iterator[MemoryManager]:
    """Lanes OFF (the default) — the pre-E1 code path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = _manager(_settings(Path(tmpdir)))
        yield mgr
        mgr.close()


@pytest.fixture
def lanes_manager() -> Iterator[MemoryManager]:
    """Lanes ON (``LanesConfig.enabled=True`` — the E1 treatment path)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = _manager(_settings(Path(tmpdir), lanes_enabled=True))
        yield mgr
        mgr.close()


def _add(mgr: MemoryManager, content: str, tags: list[str]) -> Memory:
    return mgr.add(
        MemoryCreate(
            content=content,
            tags=tags,
            source=MemorySource.MCP,
            status=MemoryStatus.PUBLISHED,
        ),
        project=PROJECT,
        agent=AGENT,
    )


def _corpus(mgr: MemoryManager) -> None:
    """The EXACT corpus of the flag-off fixture (order included)."""
    _add(mgr, KNOW_A, [f"project:{PROJECT}", f"agent:{AGENT}", "mnemos:learning"])
    _add(mgr, RULE_APPLYTO, [f"project:{PROJECT}", "mnemos:rule", "applyTo:**/*.py"])
    _add(mgr, DECISION, [f"project:{PROJECT}", "mnemos:decision"])
    _add(mgr, KNOW_B, [f"project:{PROJECT}", f"agent:{AGENT}", "mnemos:learning"])
    _add(mgr, RULE_PLAIN, [f"project:{PROJECT}", "mnemos:rule"])
    _add(mgr, KNOW_C, [f"project:{PROJECT}", f"agent:{AGENT}", "mnemos:learning"])


def _freeze_assemble_clock(monkeypatch: pytest.MonkeyPatch, target: MemoryManager) -> None:
    """Freeze the retrieval timestamp source for byte-identical outputs.

    mnemos #282: the provenance ``retrieved=<iso>`` stamp moved from a
    per-call ``assemble.py`` ``datetime.now()`` to the manager's
    session-keyed registry (``MemoryManager.retrieval_iso``), so the
    freeze point moves with it. ``_FrozenDatetime`` pins the first
    assembly of every session to FROZEN_ISO; later assemblies reuse the
    registry entry, preserving the frozen-clock byte-identity guarantees
    these tests exist to hold.
    """
    monkeypatch.setattr("vesma.manager.datetime", _FrozenDatetime)
    # Sessions already stamped (a manager pre-warmed by an earlier call
    # in the same test) must also freeze — drop their registry entries so
    # the next assembly re-stamps under the frozen clock.
    target._retrieval_iso.pop(SESSION, None)


# ── Happy path: lanes on ──────────────────────────────────────────────────────


class TestLanesHappyPath:
    def test_blocks_carry_lane_and_governance_leads(self, lanes_manager: MemoryManager) -> None:
        _corpus(lanes_manager)
        result = lanes_manager.assemble_context(
            session=SESSION, project=PROJECT, query="handler deployment"
        )
        lanes = [b["lane"] for b in result["blocks"]]
        assert lanes, "expected assembled blocks"
        # Fixed lane order: rules → decisions → knowledge, regardless of
        # RRF scores (the deterministic anti-drowning guarantee).
        order = {lane: i for i, lane in enumerate(("rules", "decisions", "knowledge"))}
        positions = [order[lane] for lane in lanes]
        assert positions == sorted(positions), f"lane order violated: {lanes}"
        assert "rules" in lanes and "decisions" in lanes and "knowledge" in lanes

    def test_governance_surfaces_deterministically_without_query_match(
        self, lanes_manager: MemoryManager
    ) -> None:
        """Rules/decisions surface even when the derived query has zero
        lexical overlap with them — deterministic SQL, not ranking."""
        _corpus(lanes_manager)
        result = lanes_manager.assemble_context(session=SESSION, project=PROJECT)
        lanes = [b["lane"] for b in result["blocks"]]
        assert "rules" in lanes
        assert "decisions" in lanes

    def test_governance_appears_exactly_once(self, lanes_manager: MemoryManager) -> None:
        _corpus(lanes_manager)
        result = lanes_manager.assemble_context(
            session=SESSION, project=PROJECT, query="handler deployment"
        )
        ids = [b["memory_id"] for b in result["blocks"]]
        assert len(ids) == len(set(ids)), "governance row surfaced twice (RRF + lane)"

    def test_governance_excluded_from_knowledge_leg(self, lanes_manager: MemoryManager) -> None:
        _corpus(lanes_manager)
        result = lanes_manager.assemble_context(session=SESSION, project=PROJECT, query="ruff")
        # The applyTo rule's content IS the query — RRF would rank it; the
        # knowledge leg must exclude it (it rides the rules lane).
        assert result["stats"]["recall"]["lanes"]["governance_excluded_from_knowledge"] >= 1

    def test_lane_telemetry_counts(self, lanes_manager: MemoryManager) -> None:
        _corpus(lanes_manager)
        result = lanes_manager.assemble_context(session=SESSION, project=PROJECT)
        lane_stats = result["stats"]["recall"]["lanes"]
        assert lane_stats["rules"] == 2
        assert lane_stats["decisions"] == 1
        assert lane_stats["knowledge"] >= 1

    def test_lane_trace_row_recorded(self, lanes_manager: MemoryManager) -> None:
        _corpus(lanes_manager)
        lanes_manager.assemble_context(session=SESSION, project=PROJECT)
        traces = lanes_manager.sqlite.list_traces(project=PROJECT, task_label="assemble_lanes")
        assert traces, "lane telemetry trace row missing"
        assert traces[0].rationale_summary == "decisions=1 rules=2"
        assert traces[0].step == "recall"

    def test_applyto_pins_within_lane_not_across(self, lanes_manager: MemoryManager) -> None:
        _corpus(lanes_manager)
        result = lanes_manager.assemble_context(
            session=SESSION, project=PROJECT, file="src/handler.py", query="handler deployment"
        )
        assert result["stats"]["recall"]["applyto_pinned"] == 1
        blocks = result["blocks"]
        # The applyTo rule leads its OWN lane…
        assert blocks[0]["lane"] == "rules"
        # …but decisions still follow every rules block (no cross-lane
        # reordering by the M8 pin).
        lane_seq = [b["lane"] for b in blocks]
        assert lane_seq.index("decisions") > lane_seq.index("rules")

    def test_raw_governance_row_never_surfaces(self, lanes_manager: MemoryManager) -> None:
        """The ADR-0018 status gate composes with the deterministic lanes —
        a RAW rule is invisible on the lane channel too."""
        raw = lanes_manager.add(
            MemoryCreate(
                content="# Raw rule\nRaw rule content for the lanes gate test.",
                tags=[f"project:{PROJECT}", "mnemos:rule"],
                source=MemorySource.MCP,
                status=MemoryStatus.RAW,
            ),
            project=PROJECT,
            agent=AGENT,
        )
        result = lanes_manager.assemble_context(session=SESSION, project=PROJECT)
        assert raw.id not in {b["memory_id"] for b in result["blocks"]}
        assert result["stats"]["recall"]["lanes"]["rules"] == 0

    def test_quarantined_published_governance_row_never_surfaces(
        self, lanes_manager: MemoryManager
    ) -> None:
        """PUBLISHED + ``pipeline_state=quarantined`` (the ADR-0019 §5
        composition in ``is_context_admissible``) is excluded from the
        deterministic lanes — pinning the quarantine half behaviorally
        (the RAW half is covered above)."""
        mem = _add(
            lanes_manager,
            "# Rule\nQuarantined rule content for the lanes gate test.",
            [f"project:{PROJECT}", "mnemos:rule"],
        )
        assert lanes_manager.sqlite.update_fields(
            mem.id,
            pipeline_state=PipelineState.QUARANTINED,
            quarantine_reason="secret",
        )
        result = lanes_manager.assemble_context(session=SESSION, project=PROJECT)
        assert mem.id not in {b["memory_id"] for b in result["blocks"]}
        assert result["stats"]["recall"]["lanes"]["rules"] == 0

    def test_dual_tagged_row_emitted_once_into_first_lane(
        self, lanes_manager: MemoryManager
    ) -> None:
        """Review P2-1 regression: a row tagged BOTH ``mnemos:rule`` and
        ``mnemos:decision`` is contract-legal (the subtype set requires
        "at least one", not uniqueness) — it must surface exactly once,
        in the FIRST lane of the pinned order, with telemetry counting
        the emitted (post-dedup) rows."""
        dual = _add(
            lanes_manager,
            "# Dual rule\nTagged both rule and decision at once.",
            [f"project:{PROJECT}", "mnemos:rule", "mnemos:decision"],
        )
        result = lanes_manager.assemble_context(session=SESSION, project=PROJECT)
        ids = [b["memory_id"] for b in result["blocks"]]
        assert len(ids) == len(set(ids)), "dual-tagged row surfaced twice"
        assert ids.count(dual.id) == 1
        block = next(b for b in result["blocks"] if b["memory_id"] == dual.id)
        assert block["lane"] == "rules", "first lane in pinned order wins"
        assert result["stats"]["recall"]["lanes"]["rules"] == 1
        assert result["stats"]["recall"]["lanes"]["decisions"] == 0
        traces = lanes_manager.sqlite.list_traces(project=PROJECT, task_label="assemble_lanes")
        assert traces[0].rationale_summary == "decisions=0 rules=1"


# ── Empty lane results ────────────────────────────────────────────────────────


class TestEmptyLanes:
    def test_no_governance_in_store_no_crash(self, lanes_manager: MemoryManager) -> None:
        _add(lanes_manager, KNOW_A, [f"project:{PROJECT}", "mnemos:learning"])
        result = lanes_manager.assemble_context(
            session=SESSION, project=PROJECT, query="deployment guide"
        )
        # No crash, no empty sections: every block is a knowledge block.
        assert result["blocks"], "knowledge row should surface"
        assert all(b["lane"] == "knowledge" for b in result["blocks"])
        assert result["stats"]["recall"]["lanes"] == {
            "rules": 0,
            "decisions": 0,
            "knowledge": len(result["blocks"]),
            "governance_excluded_from_knowledge": 0,
        }

    def test_empty_project_lanes_on(self, lanes_manager: MemoryManager) -> None:
        result = lanes_manager.assemble_context(session=SESSION, project=PROJECT)
        assert result["text"] == ""
        assert result["blocks"] == []
        assert result["stats"]["recall"]["lanes"]["rules"] == 0
        assert result["stats"]["stages"] == [
            "recall",
            "ccr",
            "filter",
            "scan",
            "align",
            "budget",
        ], "STAGE_ORDER unchanged — lanes are a SUB-stage of recall"


# ── Flag-off equivalence (the E1 rollback proof) ─────────────────────────────


class TestFlagOffEquivalence:
    def test_flag_off_reproduces_pre_change_fixture(
        self, manager: MemoryManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Strong version: the flag-off output of the POST-change code is
        byte-identical (UUID-normalized, frozen clock, seeded ids) to the
        registered fixture — originally captured on pristine b8968df
        (pre-lanes), re-captured with the hybrid_alpha 0.5 re-tune
        (#300, a registered composition change per ADR-0020), re-captured
        again for the Phase-1 deterministic id tiebreak (#280, TL
        decision 2026-09-14: equal-score groups now order by id, so the
        fixture needs the fixed seeded-id draw of ``tests/_seeded_ids.py``
        instead of per-run uuid4 luck), and re-captured once more for
        search v2 (issue #313, ADR-0029: the FTS leg now emits per-token
        prefix terms with the ranked OR fallback — the query-bearing
        assembly's recall output changed, a registered issuance-path
        change per ADR-0020; the query-less no-file output is unchanged
        in shape and re-pinned for continuity)."""
        _freeze_assemble_clock(monkeypatch, manager)
        with seeded_memory_ids("lanes-flag-off"):
            _corpus(manager)
            no_file = manager.assemble_context(session=SESSION, project=PROJECT)
            with_file = manager.assemble_context(
                session=SESSION, project=PROJECT, file="src/handler.py", query="handler deployment"
            )
        assert _normalized_sha256(no_file) == NO_FILE_FIXTURE_SHA256
        assert _normalized_sha256(with_file) == WITH_FILE_FIXTURE_SHA256

    def test_flag_off_runs_no_lane_queries(
        self,
        manager: MemoryManager,
        lanes_manager: MemoryManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The only new query channel is ``list_all`` — with the flag off it
        must never run during assembly."""
        _corpus(manager)
        calls: list[str] = []
        real_off = manager.sqlite.list_all

        def _spy_off(*args: object, **kwargs: object) -> object:
            calls.append("list_all")
            return real_off(*args, **kwargs)  # type: ignore[no-any-return]

        monkeypatch.setattr(manager.sqlite, "list_all", _spy_off)
        manager.assemble_context(session=SESSION, project=PROJECT, query="handler deployment")
        assert calls == [], f"flag-off assemble ran lane queries: {calls}"

        _corpus(lanes_manager)
        lanes_calls: list[str] = []
        real_on = lanes_manager.sqlite.list_all

        def _spy_on(*args: object, **kwargs: object) -> object:
            lanes_calls.append("list_all")
            return real_on(*args, **kwargs)  # type: ignore[no-any-return]

        monkeypatch.setattr(lanes_manager.sqlite, "list_all", _spy_on)
        lanes_manager.assemble_context(session=SESSION, project=PROJECT, query="handler deployment")
        assert len(lanes_calls) == 2, "lanes-on assemble should query rules + decisions once each"

    def test_flag_off_writes_no_lane_trace_rows(self, manager: MemoryManager) -> None:
        """Review P3-1: the trace row is the second flag-on side channel
        (the spy above covers ``list_all`` only) — with the flag off,
        assembling writes ZERO ``assemble_lanes`` trace rows."""
        _corpus(manager)
        manager.assemble_context(session=SESSION, project=PROJECT, query="handler")
        assert manager.sqlite.list_traces(project=PROJECT, task_label="assemble_lanes") == []

    def test_flag_off_output_has_no_lane_keys(
        self, manager: MemoryManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Recursive walk: the default-path output carries no ``lane`` /
        ``lanes`` keys anywhere — the block/stats shape is the pre-E1
        shape, not a default-valued extension of it."""

        def _walk(node: object) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    assert key not in ("lane", "lanes"), f"unexpected lanes key: {key}"
                    _walk(value)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)

        _corpus(manager)
        _freeze_assemble_clock(monkeypatch, manager)
        _walk(manager.assemble_context(session=SESSION, project=PROJECT))
        _walk(
            manager.assemble_context(
                session=SESSION, project=PROJECT, file="src/handler.py", query="handler deployment"
            )
        )

    def test_flag_off_repeat_runs_byte_identical(
        self, manager: MemoryManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _freeze_assemble_clock(monkeypatch, manager)
        _corpus(manager)
        first = manager.assemble_context(session=SESSION, project=PROJECT, query="handler")
        second = manager.assemble_context(session=SESSION, project=PROJECT, query="handler")
        assert first == second
        assert first["text"] == second["text"]

    def test_default_settings_lanes_disabled(self) -> None:
        assert Settings().lanes.enabled is False, "LanesConfig.enabled must default to False"

    def test_env_override_enables_lanes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VESMA_LANES__ENABLED", "true")
        assert Settings().lanes.enabled is True

    def test_config_model_is_single_switch(self) -> None:
        """LanesConfig field set is closed: ``enabled`` (E1) + ``type_boost``
        (E0 §1.1 leg B0, issue #277). Still no second enablement path for
        LANES: ``type_boost`` is an alternative TREATMENT (a leg is exactly
        one of A/B0/B), not a way to also turn lanes on — the two are
        mutually exclusive by model validator (next test), both default
        off, and both-off stays the byte-identical pre-E1 path."""
        from vesma.config import LanesConfig

        assert set(LanesConfig.model_fields) == {"enabled", "type_boost"}
        assert LanesConfig().enabled is False
        assert LanesConfig().type_boost is False

    def test_treatments_are_mutually_exclusive(self) -> None:
        """E0 §1.1 — enabling lanes (leg B) and type_boost (leg B0) together
        is an unregistered fourth leg: refused at the config boundary."""
        import pydantic

        from vesma.config import LanesConfig

        with pytest.raises(pydantic.ValidationError, match="mutually exclusive"):
            LanesConfig(enabled=True, type_boost=True)

    def test_type_boost_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Canonical env override for the B0 treatment, mirroring the
        ``VESMA_LANES__ENABLED`` override test above."""
        monkeypatch.setenv("VESMA_LANES__TYPE_BOOST", "true")
        assert Settings().lanes.type_boost is True
        assert Settings().lanes.enabled is False

    def test_treatments_mutex_enforced_at_point_of_use(self, lanes_manager: MemoryManager) -> None:
        """The construction-time validator can be bypassed by direct
        attribute assignment on a built manager; assemble_context
        re-checks at the point of use — both treatments on at assemble
        time would silently run an unregistered fourth leg (review P3)."""
        _corpus(lanes_manager)
        assert lanes_manager.settings.lanes.enabled is True
        lanes_manager.settings.lanes.type_boost = True  # bypass on purpose
        with pytest.raises(AssertionError, match="mutually exclusive"):
            lanes_manager.assemble_context(session=SESSION, project=PROJECT, query="handler")
        # the single-treatment configuration still assembles normally
        lanes_manager.settings.lanes.type_boost = False
        assert lanes_manager.assemble_context(session=SESSION, project=PROJECT, query="handler")


# ── Ordering stability (H2 surface) ──────────────────────────────────────────


class TestOrderingStability:
    def test_repeated_assemblies_byte_stable(
        self, lanes_manager: MemoryManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same session, repeated assemblies → byte-stable block order under
        lane ordering (the deterministic KV-cache prefix, H2)."""
        _freeze_assemble_clock(monkeypatch, lanes_manager)
        _corpus(lanes_manager)
        first = lanes_manager.assemble_context(
            session=SESSION, project=PROJECT, query="handler deployment"
        )
        second = lanes_manager.assemble_context(
            session=SESSION, project=PROJECT, query="handler deployment"
        )
        assert first == second
        assert first["text"] == second["text"]

    def test_high_knowledge_score_does_not_reorder_governance(
        self, lanes_manager: MemoryManager
    ) -> None:
        """Interleaved scores: the knowledge entry with the strongest query
        match still renders AFTER every rules/decisions block — lane order
        dominates score."""
        _corpus(lanes_manager)
        result = lanes_manager.assemble_context(
            session=SESSION, project=PROJECT, query="ruff before committing handler code"
        )
        lanes = [b["lane"] for b in result["blocks"]]
        assert "knowledge" in lanes
        first_knowledge = lanes.index("knowledge")
        assert set(lanes[:first_knowledge]) == {"rules", "decisions"}


def _cand(lane: str, score: float, content: str = "stable candidate content") -> _Candidate:
    return _Candidate(
        memory=Memory(content=content, status=MemoryStatus.PUBLISHED, project=PROJECT),
        score=score,
        search_type="lane",
        content_type="prose",
        content=content,
        lane=lane,
    )


class TestBudgetOrderingUnit:
    """Pure sort semantics of the budget-stage lane ordering."""

    def test_interleaved_scores_collapse_into_lane_order(self) -> None:
        # Knowledge scores deliberately DOMINATE lane scores (RRF scores
        # are typically ≪ 1.0; deterministic lanes carry 1.0 — but the
        # test must hold even if a knowledge score beats 1.0).
        candidates = [
            _cand("knowledge", 9.9, "k-high"),
            _cand("rules", 1.0, "r-1"),
            _cand("knowledge", 8.8, "k-mid"),
            _cand("decisions", 1.0, "d-1"),
            _cand("rules", 1.0, "r-2"),
            _cand("knowledge", 7.7, "k-low"),
        ]
        blocks, texts, stats = _budget_stage(
            candidates, budget=4096, project=PROJECT, retrieved_iso=FROZEN_ISO, lanes_enabled=True
        )
        assert [b["lane"] for b in blocks] == [
            "rules",
            "rules",
            "decisions",
            "knowledge",
            "knowledge",
            "knowledge",
        ]
        # Within a lane: score desc; the stable tiebreak keeps recall order
        # (r-1 before r-2 — equal scores, first-seen wins).
        assert [b["content"] for b in blocks[:2]] == ["r-1", "r-2"]
        assert [b["content"] for b in blocks[3:]] == ["k-high", "k-mid", "k-low"]
        assert stats["blocks_included"] == 6
        assert len(texts) == 6

    def test_off_path_never_sorts_and_never_renders_lane(self) -> None:
        candidates = [
            _cand("knowledge", 1.0, "k"),
            _cand("rules", 1.0, "r"),  # defensive: even a lane-tagged candidate
        ]
        blocks, _, _ = _budget_stage(
            candidates, budget=4096, project=PROJECT, retrieved_iso=FROZEN_ISO
        )
        # No sort (recall order stands) and no lane key rendered.
        assert [b["content"] for b in blocks] == ["k", "r"]
        assert all("lane" not in b for b in blocks)

    def test_synthesized_sorts_after_pinned_prefix(self) -> None:
        candidates = [
            _cand("knowledge", 5.0, "k"),
            _cand("synthesized", 5.0, "s"),
            _cand("rules", 1.0, "r"),
        ]
        blocks, _, _ = _budget_stage(
            candidates, budget=4096, project=PROJECT, retrieved_iso=FROZEN_ISO, lanes_enabled=True
        )
        assert [b["lane"] for b in blocks] == ["rules", "knowledge", "synthesized"]

    def test_lane_sort_key_order(self) -> None:
        ordered = sorted(
            ["awareness", "synthesized", "knowledge", "decisions", "rules"],
            key=lane_sort_key,
        )
        assert ordered == ["rules", "decisions", "knowledge", "synthesized", "awareness"]


# ── Cascade-ready contracts (R2 — fields only, no mechanics) ─────────────────


class TestCascadeReadyContracts:
    def test_lane_enum_includes_synthesized(self) -> None:
        assert Lane.SYNTHESIZED.value == "synthesized"
        assert {lane.value for lane in Lane} == {
            "rules",
            "decisions",
            "knowledge",
            "synthesized",
        }

    def test_nothing_populates_synthesized_or_knowledge_via_sql_leg(
        self, manager: MemoryManager
    ) -> None:
        _corpus(manager)
        for lane in (Lane.SYNTHESIZED, Lane.KNOWLEDGE):
            with pytest.raises(ValueError, match="no deterministic SQL leg"):
                deterministic_lane_results(manager, lane=lane, project=PROJECT)

    def test_collapse_level_convention_documented(self) -> None:
        """The collapse cascade's ``collapse_level`` key convention lives in
        the lanes.py docstring: metadata key, never tags, never schema."""
        import vesma.lanes as lanes_mod

        text = " ".join((lanes_mod.__doc__ or "").split())
        assert "collapse_level" in text
        assert "the metadata key is exactly" in text

    def test_tag_contract_not_extended(self) -> None:
        from vesma.models import ALLOWED_OPTIONAL_PREFIXES

        assert "area" not in ALLOWED_OPTIONAL_PREFIXES, "tag contract must stay closed in v0"


# ── Awareness-ready contracts (R3 — fields only) ─────────────────────────────


class TestAwarenessContracts:
    def test_cursor_roundtrip_and_key_format(self, manager: MemoryManager) -> None:
        assert read_awareness_cursor(manager, project=PROJECT, agent=AGENT, session=SESSION) is None
        write_awareness_cursor(
            manager, project=PROJECT, agent=AGENT, session=SESSION, cursor="mem-001"
        )
        # Exact length-prefixed encoding (#254 review P3): plain
        # ":"-joins alias when a session id contains ":" — components
        # are prefixed with their character lengths.
        key = f"{AWARENESS_CURSOR_PREFIX}8:{PROJECT}:9:{AGENT}:7:{SESSION}"
        assert manager.sqlite.get_meta(key) == "mem-001"  # exact encoded key format
        assert (
            read_awareness_cursor(manager, project=PROJECT, agent=AGENT, session=SESSION)
            == "mem-001"
        )

    def test_cursor_key_no_tuple_aliasing(self) -> None:
        """The crafted-tuple alias: distinct tuples whose plain ":"-joins
        coincide must produce DISTINCT keys (session ids may contain ":")."""
        from vesma.lanes import awareness_cursor_key

        first = awareness_cursor_key(project="a", agent="b:c", session="d")
        second = awareness_cursor_key(project="a:b", agent="c", session="d")
        assert first != second, "length-prefixed encoding must not alias tuples"
        third = awareness_cursor_key(project="a", agent="b", session="c:d:e")
        fourth = awareness_cursor_key(project="a", agent="b:c", session="d:e")
        assert third != fourth

    def test_cursor_upsert_overwrites_in_place(self, manager: MemoryManager) -> None:
        write_awareness_cursor(
            manager, project=PROJECT, agent=AGENT, session=SESSION, cursor="mem-001"
        )
        write_awareness_cursor(
            manager, project=PROJECT, agent=AGENT, session=SESSION, cursor="mem-002"
        )
        assert (
            read_awareness_cursor(manager, project=PROJECT, agent=AGENT, session=SESSION)
            == "mem-002"
        )

    def test_cursor_key_validation(self) -> None:
        with pytest.raises(ValueError, match="agent"):
            awareness_cursor_key(project=PROJECT, agent="", session=SESSION)
        with pytest.raises(ValueError, match="session"):
            awareness_cursor_key(project=PROJECT, agent=AGENT, session="  ")

    def test_cursor_write_rejects_empty(self, manager: MemoryManager) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            write_awareness_cursor(
                manager, project=PROJECT, agent=AGENT, session=SESSION, cursor=" "
            )

    def test_guard_passes_for_tail_only_foreign_lanes(self) -> None:
        # Pinned-only ordering: fine. Foreign lanes (synthesized / the
        # future awareness lane) in a contiguous tail: fine — awareness
        # renders LAST. All calls must simply not raise.
        assert_foreign_lanes_tail_only(["rules", "decisions", "knowledge"])
        assert_foreign_lanes_tail_only(["rules", "knowledge", "synthesized", "awareness"])
        assert_foreign_lanes_tail_only([])

    def test_guard_fires_when_foreign_lane_enters_pinned_prefix(self) -> None:
        with pytest.raises(AssertionError, match="pinned prefix"):
            assert_foreign_lanes_tail_only(["rules", "awareness", "knowledge"])
        with pytest.raises(AssertionError, match="pinned prefix"):
            assert_foreign_lanes_tail_only(["synthesized", "rules"])

    def test_awareness_conventions_documented(self) -> None:
        import vesma.lanes as lanes_mod

        text = lanes_mod.__doc__ or ""
        # Per-agent delta slot (one agent = max one delta block)…
        assert "AT MOST one delta block" in text
        # …cursor key format (length-prefixed tuple, #254 review P3)…
        assert "LENGTH-PREFIXED" in text
        # …and the renders-LAST invariant.
        assert "renders LAST" in text or "Awareness renders LAST" in text


# ── E0 §1.1 leg B0 — the trivial type-boost treatment (issue #277) ───────────


class TestB0TypeBoost:
    """B0 = "type-boost of rules/decisions at recall — one ranking line,
    zero meta-level" (E0 §1.1). It shares NO lanes mechanics: no lane
    queries, no ``lane`` block field, no lane stats — only the boosted
    score re-ranking of the ordinary RRF candidates."""

    @pytest.fixture
    def b0_manager(self) -> Iterator[MemoryManager]:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _manager(_settings(Path(tmpdir), type_boost=True))
            yield mgr
            mgr.close()

    def test_b0_lifts_governance_above_knowledge(self, b0_manager: MemoryManager) -> None:
        _corpus(b0_manager)
        result = b0_manager.assemble_context(
            session=SESSION, project=PROJECT, query="handler deployment"
        )
        assert result["blocks"], "expected assembled blocks"
        # Identify rows by their deterministic first lines (blocks carry
        # no tags): governance = the two rules + the decision of _corpus.
        gov_prefixes = ("# Handler rule", "# Release rule", "Decision:")
        blocks = [(b["content"].splitlines()[0], b["score"]) for b in result["blocks"]]
        governance = [score for title, score in blocks if title.startswith(gov_prefixes)]
        knowledge = [score for title, score in blocks if not title.startswith(gov_prefixes)]
        # The boost multiplies governance scores by B0_TYPE_BOOST_FACTOR:
        # every governance block outranks every knowledge block — the
        # trivial type-lift, ahead of the RRF-winning knowledge rows.
        assert len(governance) == 3, blocks
        assert knowledge, blocks
        assert min(governance) > max(knowledge), blocks

    def test_b0_stats_key_additive_only_when_on(
        self, b0_manager: MemoryManager, manager: MemoryManager
    ) -> None:
        _corpus(b0_manager)
        _corpus(manager)
        on = b0_manager.assemble_context(session=SESSION, project=PROJECT, query="handler")
        off = manager.assemble_context(session=SESSION, project=PROJECT, query="handler")
        boost_stats = on["stats"]["recall"]["type_boost"]
        assert boost_stats["boosted"] == 3  # 2 rules + 1 decision in _corpus
        assert boost_stats["factor"] == B0_TYPE_BOOST_FACTOR
        assert "type_boost" not in off["stats"]["recall"]
        assert "lanes" not in on["stats"]["recall"]  # B0 shares no lanes mechanics
        assert all("lane" not in b for b in on["blocks"])

    def test_b0_preserves_applyto_pinning(self, b0_manager: MemoryManager) -> None:
        """M8 pinning must survive the boost: the sort runs BEFORE the
        applyTo partition, so the applyTo-matching rule still floats to
        the absolute top (B0 must not break M8)."""
        _corpus(b0_manager)
        result = b0_manager.assemble_context(
            session=SESSION, project=PROJECT, file="src/handler.py", query="handler deployment"
        )
        assert result["blocks"], "expected assembled blocks"
        assert result["blocks"][0]["content"].startswith("# Handler rule"), result["blocks"][0][
            "content"
        ][:60]

    def test_b0_flag_off_output_unchanged(self, manager: MemoryManager) -> None:
        """Both flags off = the pre-E1 code path: no boost sort, no
        extra stats keys (the flag-off fixture tests above pin the
        bytes; this asserts the B0 telemetry discipline)."""
        _corpus(manager)
        result = manager.assemble_context(session=SESSION, project=PROJECT, query="handler")
        assert "type_boost" not in result["stats"]["recall"]
