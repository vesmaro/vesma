"""ADR-0018 — ``on_context_rewrite`` lifecycle event tests (#125, Wave 2).

Wave 2 acceptance for the rewrite event:

* **idempotency** — a re-delivered event performs no duplicate writes
  (content-addressed event key; the advisory diff is excluded from the
  key — not load-bearing);
* **version-less** — the receipt carries no ordering/version fields;
  replacement lineage is a ``supersedes`` edge (P1-a store surface);
* **normal pipeline path** — the original enters at ``raw`` via
  ``MemoryManager.add`` (Layer-1 write scan tags ``mnemos:no-federate``
  on a secret hit; the advisory diff gets its own Layer-1 verdict and
  tags the record too);
* **tag contract** — ``project:<slug>`` / ``agent:<slug>`` enforced with
  the caller's strictness knob; provenance metadata
  (``source=context-rewrite``, session, event key);
* **rehydrate roundtrip** — rewrite-stored originals surface through the
  EXISTING gated channels only: raw is invisible to
  ``assemble_context`` until the pipeline advances the entry
  (simulated via a status update), then it assembles with provenance and
  issuance redaction; the CCR marker (when requested) redeems through
  ``retrieve_content`` (project-scoped, issuance-scanned);
* **write-surface guardrails (W2 review F1)** — per-(project, session)
  stored-event rate limit (dedupe re-deliveries consume no quota; 0
  disables; 429 on REST, flagged error dict on MCP) and boundary size
  caps for content/diff;
* **project-scoped supersedes (W2 review F2)** — a target of another
  project is rejected with the SAME message as a nonexistent target (no
  global existence oracle), no edge, no write;
* **surfaces** — MCP ``vesma_context_rewrite`` and REST
  ``POST /context/rewrite`` ride the same manager path.

All secrets below are obviously fake EXAMPLE-style values built from the
detector's own pattern catalogue (src/mnemos/secrets_detector.py); real
credentials never appear in this file.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import vesmaro.mcp_server as mcp_mod
from vesmaro.api import main as api_main
from vesmaro.api.main import app, lifespan
from vesmaro.config import Settings
from vesmaro.context_rewrite import ContextRewriteRateLimitError
from vesmaro.manager import MemoryManager
from vesmaro.mcp_server import _dispatch
from vesmaro.models import MemoryCreate, MemorySource, MemoryStatus, MemoryUpdate

# ── Fake (EXAMPLE-style) secret from the detector's own regexes ───────────────

# aws-key pattern: AKIA + 16 chars of [0-9A-Z].
FAKE_AWS_KEY = "AKIAEXAMPLEABCDEFGH1"

PROJECT = "crw-proj"
AGENT = "crw-agent"
SESSION = "sess-crw-7"

ORIGINAL = (
    "Deployment notes for the unobtanium gateway service.\n"
    "The service is configured through the deployment manifest and\n"
    "access policies rotate quarterly per the security baseline.\n"
)

# Distinct-enough second block for a chained rewrite.
ORIGINAL_V2 = (
    "Deployment notes for the unobtanium gateway service, revision two.\n"
    "The manifest gained a canary stage and policy rotation moved to\n"
    "monthly per the updated security baseline.\n"
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _settings(
    tmp: Path,
    *,
    mnemos_extra: dict[str, object] | None = None,
    **ccr_overrides: object,
) -> Settings:
    ccr: dict[str, object] = {"min_size_chars": 100, "max_entries": 100, "ttl_days": 1}
    ccr.update(ccr_overrides)
    mnemos_cfg: dict[str, object] = {
        "vault_path": str(tmp / "vault"),
        "data_dir": str(tmp / "data"),
        "db_name": "test.db",
    }
    mnemos_cfg.update(mnemos_extra or {})
    settings = Settings(
        mnemos=mnemos_cfg,  # type: ignore[arg-type]
        scanner={"enabled": False},
        ccr=ccr,  # type: ignore[arg-type]
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
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = _manager(_settings(Path(tmpdir)))
        yield mgr
        mgr.close()


@pytest.fixture
def tight_manager() -> Iterator[MemoryManager]:
    """Small guardrail knobs: 3 events/min, content ≤ 200 chars, diff ≤ 50."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = _manager(
            _settings(
                Path(tmpdir),
                mnemos_extra={
                    "context_rewrite_rate_limit_per_minute": 3,
                    "context_rewrite_max_content_chars": 200,
                    "context_rewrite_max_diff_chars": 50,
                },
            )
        )
        yield mgr
        mgr.close()


@pytest.fixture
def rest_client(manager: MemoryManager) -> Iterator[TestClient]:
    """TestClient wired to the shared ``manager`` fixture (test_p1b pattern)."""
    api_main._manager = manager
    test_app = FastAPI(title="Mnemos-Rewrite-Test", version="0.1.0", lifespan=lifespan)
    for route in app.routes:
        test_app.routes.append(route)
    with TestClient(test_app) as tc:
        yield tc
    api_main._manager = None


@pytest.fixture
def tight_rest_client(tight_manager: MemoryManager) -> Iterator[TestClient]:
    """TestClient wired to ``tight_manager`` (rate-limit surface tests)."""
    api_main._manager = tight_manager
    test_app = FastAPI(title="Mnemos-Rewrite-Tight-Test", version="0.1.0", lifespan=lifespan)
    for route in app.routes:
        test_app.routes.append(route)
    with TestClient(test_app) as tc:
        yield tc
    api_main._manager = None


def _add_old_block(mgr: MemoryManager, content: str = ORIGINAL) -> str:
    """Store the pre-rewrite block the way the harness would have it."""
    memory = mgr.add(
        MemoryCreate(
            content=content,
            tags=[f"project:{PROJECT}", f"agent:{AGENT}"],
            source=MemorySource.MCP,
            status=MemoryStatus.PUBLISHED,
        ),
        project=PROJECT,
        agent=AGENT,
    )
    return memory.id


def _rewrite(mgr: MemoryManager, **overrides: object) -> dict[str, object]:
    """Call the event with W2 defaults; ``overrides`` patch single args."""
    kwargs: dict[str, object] = {
        "content": ORIGINAL_V2,
        "project": PROJECT,
        "agent": AGENT,
    }
    kwargs.update(overrides)
    return mgr.context_rewrite(**kwargs)  # type: ignore[arg-type]


def _advance(mgr: MemoryManager, memory_id: str, status: MemoryStatus) -> None:
    """Simulate the knowledge pipeline advancing an entry."""
    updated = mgr.update(memory_id, MemoryUpdate(status=status))
    assert updated is not None


# ── Idempotency ───────────────────────────────────────────────────────────────


class TestIdempotency:
    def test_double_delivery_writes_one_memory(self, manager: MemoryManager) -> None:
        first = _rewrite(manager, session=SESSION)
        second = _rewrite(manager, session=SESSION)

        assert first["status"] == "stored"
        assert second["status"] == "deduplicated"
        assert second["memory_id"] == first["memory_id"]
        assert second["event_key"] == first["event_key"]

        # Exactly one row carries the content — no duplicate write.
        conn = manager.sqlite._get_conn()
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE content = ?", (ORIGINAL_V2,)
        ).fetchone()
        assert rows["n"] == 1

    def test_diff_excluded_from_event_key(self, manager: MemoryManager) -> None:
        """The advisory diff is not load-bearing: a re-delivery with a
        different diff is still the same event and deduplicates."""
        first = _rewrite(manager, session=SESSION, diff="gateway config: was → became")
        second = _rewrite(manager, session=SESSION, diff="something else entirely")

        assert second["status"] == "deduplicated"
        assert second["memory_id"] == first["memory_id"]
        stored = manager.sqlite.get(str(first["memory_id"]))
        assert stored is not None
        # The FIRST diff stays (the original event's metadata is not rewritten).
        assert stored.metadata["rewrite_diff"] == "gateway config: was → became"

    def test_different_session_is_a_new_event(self, manager: MemoryManager) -> None:
        first = _rewrite(manager, session="sess-one")
        second = _rewrite(manager, session="sess-two")

        assert second["status"] == "stored"
        assert second["memory_id"] != first["memory_id"]
        assert second["event_key"] != first["event_key"]

    def test_dedup_redelivery_relinks_edge_idempotently(self, manager: MemoryManager) -> None:
        old_id = _add_old_block(manager)
        first = _rewrite(manager, session=SESSION, supersedes=old_id)
        second = _rewrite(manager, session=SESSION, supersedes=old_id)

        assert first["supersedes"]["edge_created"] is True
        assert second["supersedes"]["edge_created"] is False
        # Still exactly one edge (PK idempotency).
        edges = manager.get_memory_edges(str(first["memory_id"]))
        assert len(edges) == 1
        assert edges[0]["to_memory_id"] == old_id

    def test_deduplicated_receipt_still_carries_marker(self, manager: MemoryManager) -> None:
        """Review gap: dedupe + include_marker — the re-delivery receipt
        still carries the CCR marker (the caller may have lost the first
        receipt and re-asks with the marker flag set)."""
        first = _rewrite(manager, session=SESSION, include_marker=True)
        second = _rewrite(manager, session=SESSION, include_marker=True)

        assert first["status"] == "stored"
        assert first["ccr_marker"]["marker"]
        assert second["status"] == "deduplicated"
        assert second["ccr_marker"]["marker"], "dedupe receipt keeps the marker"
        assert second["ccr_marker"]["hash"] == first["ccr_marker"]["hash"]


# ── Supersedes linkage ────────────────────────────────────────────────────────


class TestSupersedes:
    def test_edge_created_and_queryable(self, manager: MemoryManager) -> None:
        old_id = _add_old_block(manager)
        receipt = _rewrite(manager, session=SESSION, supersedes=old_id)

        new_id = str(receipt["memory_id"])
        assert receipt["supersedes"] == {"to_memory_id": old_id, "edge_created": True}
        edges = manager.get_memory_edges(new_id)
        assert len(edges) == 1
        assert edges[0]["kind"] == "supersedes"
        assert edges[0]["from_memory_id"] == new_id
        assert edges[0]["to_memory_id"] == old_id

    def test_unknown_target_raises_valueerror(self, manager: MemoryManager) -> None:
        ghost = "00000000-0000-0000-0000-000000000000"
        with pytest.raises(ValueError, match=r"not found in project 'crw-proj'$"):
            _rewrite(manager, session=SESSION, supersedes=ghost)
        # Nothing was written — the pre-flight runs before the store.
        conn = manager.sqlite._get_conn()
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE content = ?", (ORIGINAL_V2,)
        ).fetchone()
        assert rows["n"] == 0


# ── Project-scoped supersedes (W2 review F2) ─────────────────────────────────


class TestCrossProjectSupersedes:
    def _foreign_block(self, mgr: MemoryManager) -> str:
        """A memory that EXISTS — but under another project."""
        memory = mgr.add(
            MemoryCreate(
                content=ORIGINAL,
                tags=["project:crw-other", f"agent:{AGENT}", "mnemos:session"],
                source=MemorySource.MCP,
                status=MemoryStatus.PUBLISHED,
            ),
            project="crw-other",
            agent=AGENT,
        )
        return memory.id

    def test_cross_project_target_rejected_no_oracle(self, manager: MemoryManager) -> None:
        """F2: a foreign-project target is rejected with the SAME message
        template as a nonexistent one — the error must not double as a
        global existence oracle (mirrors P1-a ccr_get semantics)."""
        foreign_id = self._foreign_block(manager)

        with pytest.raises(ValueError, match=r"not found in project 'crw-proj'$"):
            _rewrite(manager, session=SESSION, supersedes=foreign_id)
        with pytest.raises(ValueError, match=r"not found in project 'crw-proj'$"):
            _rewrite(
                manager,
                session=SESSION,
                supersedes="00000000-0000-0000-0000-000000000000",
            )

        # No write, no edge — only the foreign block exists.
        conn = manager.sqlite._get_conn()
        total = conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()
        assert total["n"] == 1
        edges = conn.execute("SELECT COUNT(*) AS n FROM memory_edges").fetchone()
        assert edges["n"] == 0

    def test_same_project_target_still_links(self, manager: MemoryManager) -> None:
        old_id = _add_old_block(manager)
        receipt = _rewrite(manager, session=SESSION, supersedes=old_id)
        assert receipt["supersedes"]["edge_created"] is True


# ── Advisory diff ─────────────────────────────────────────────────────────────


class TestAdvisoryDiff:
    def test_stored_as_metadata_not_content_not_echoed(self, manager: MemoryManager) -> None:
        receipt = _rewrite(manager, session=SESSION, diff="was: quarterly → became: monthly")
        stored = manager.sqlite.get(str(receipt["memory_id"]))
        assert stored is not None

        assert stored.metadata["rewrite_diff"] == "was: quarterly → became: monthly"
        assert stored.content == ORIGINAL_V2, "the ORIGINAL is the content, never the diff"
        assert "diff" not in receipt, "the diff is never echoed in the receipt"

    def test_secret_in_diff_tags_no_federate_verdict_hit(self, manager: MemoryManager) -> None:
        """The diff is part of the persisted record — a secret there must
        not federate unflagged (its own Layer-1 verdict)."""
        receipt = _rewrite(
            manager,
            session=SESSION,
            diff=f"was: rotate {FAKE_AWS_KEY} quarterly → became: monthly",
        )
        stored = manager.sqlite.get(str(receipt["memory_id"]))
        assert stored is not None

        assert "mnemos:no-federate" in stored.tags
        assert stored.metadata["rewrite_diff_scan_verdict"] == "hit"
        # Zero-loss: the diff itself is stored verbatim.
        assert FAKE_AWS_KEY in stored.metadata["rewrite_diff"]

    def test_clean_diff_verdict_clean(self, manager: MemoryManager) -> None:
        _rewrite(manager, session=SESSION, diff="was: v1 → became: v2")
        conn = manager.sqlite._get_conn()
        row = conn.execute(
            "SELECT metadata FROM memories WHERE content = ?", (ORIGINAL_V2,)
        ).fetchone()
        assert json.loads(row["metadata"])["rewrite_diff_scan_verdict"] == "clean"


# ── Tag contract + provenance metadata ────────────────────────────────────────


class TestTagContractAndProvenance:
    def test_project_agent_tags_and_denormalised_fields(self, manager: MemoryManager) -> None:
        receipt = _rewrite(manager, session=SESSION)
        stored = manager.sqlite.get(str(receipt["memory_id"]))
        assert stored is not None

        assert f"project:{PROJECT}" in stored.tags
        assert f"agent:{AGENT}" in stored.tags
        assert "mnemos:session" in stored.tags, "closest existing subtype (ratify?)"
        assert stored.project == PROJECT
        assert stored.agent == AGENT

    def test_provenance_metadata_shape(self, manager: MemoryManager) -> None:
        receipt = _rewrite(manager, session=SESSION)
        stored = manager.sqlite.get(str(receipt["memory_id"]))
        assert stored is not None

        assert stored.metadata["source"] == "context-rewrite"
        assert stored.metadata["rewrite_session"] == SESSION
        assert stored.metadata["rewrite_event_key"] == receipt["event_key"]

    def test_invalid_project_slug_rejected_strict(self, manager: MemoryManager) -> None:
        """strict_tag_contract defaults True — a malformed slug is a clean
        ValueError, not a silently-invalid tag."""
        with pytest.raises(ValueError, match="project:"):
            _rewrite(manager, session=SESSION, project="Crw Proj")

    def test_omitted_session_leaves_key_out(self, manager: MemoryManager) -> None:
        receipt = _rewrite(manager)
        stored = manager.sqlite.get(str(receipt["memory_id"]))
        assert stored is not None
        assert "rewrite_session" not in stored.metadata


# ── Version-less event shape ─────────────────────────────────────────────────


class TestVersionLessShape:
    def test_receipt_has_no_version_or_ordering_fields(self, manager: MemoryManager) -> None:
        receipt = _rewrite(manager, session=SESSION)
        assert set(receipt) == {
            "status",
            "memory_id",
            "memory_status",
            "event_key",
            "project",
            "agent",
            "session",
            "supersedes",
        }
        assert receipt["memory_status"] == "raw", "enters via the normal pipeline"


# ── Boundary validation ───────────────────────────────────────────────────────


class TestValidation:
    def test_empty_content_rejected(self, manager: MemoryManager) -> None:
        with pytest.raises(ValueError, match="content is required"):
            _rewrite(manager, content="   ")

    def test_empty_project_rejected(self, manager: MemoryManager) -> None:
        with pytest.raises(ValueError, match="project is required"):
            _rewrite(manager, project="")

    def test_empty_agent_rejected(self, manager: MemoryManager) -> None:
        with pytest.raises(ValueError, match="agent is required"):
            _rewrite(manager, agent="")

    @pytest.mark.parametrize("field", ["session", "supersedes", "diff"])
    def test_blank_optional_strings_rejected(self, manager: MemoryManager, field: str) -> None:
        with pytest.raises(ValueError, match=f"{field} must be a non-empty string"):
            _rewrite(manager, **{field: "   "})


# ── Write-surface guardrails (W2 review F1) ──────────────────────────────────


class TestRateLimit:
    def test_over_limit_raises_and_blocks_write(self, tight_manager: MemoryManager) -> None:
        for i in range(3):
            receipt = _rewrite(
                tight_manager, content=f"distinct block number {i} for the service", session=SESSION
            )
            assert receipt["status"] == "stored"
        with pytest.raises(ContextRewriteRateLimitError, match="rate limit exceeded"):
            _rewrite(
                tight_manager,
                content="fourth distinct block for the service",
                session=SESSION,
            )

        conn = tight_manager.sqlite._get_conn()
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE project = ?", (PROJECT,)
        ).fetchone()
        assert rows["n"] == 3, "the blocked event must not write"

    def test_dedup_redelivery_consumes_no_quota(self, tight_manager: MemoryManager) -> None:
        """The quota counts STORED events — retry storms of an already
        stored event stay harmless (at-least-once delivery)."""
        _rewrite(tight_manager, content="block alpha for the gateway service", session=SESSION)
        for _ in range(5):
            again = _rewrite(
                tight_manager, content="block alpha for the gateway service", session=SESSION
            )
            assert again["status"] == "deduplicated"
        # Quota still has room for two more DISTINCT events.
        assert (
            _rewrite(tight_manager, content="block beta for the gateway service", session=SESSION)[
                "status"
            ]
            == "stored"
        )
        assert (
            _rewrite(tight_manager, content="block gamma for the gateway service", session=SESSION)[
                "status"
            ]
            == "stored"
        )
        with pytest.raises(ContextRewriteRateLimitError):
            _rewrite(tight_manager, content="block delta for the gateway service", session=SESSION)

    def test_quota_is_per_project_and_session(self, tight_manager: MemoryManager) -> None:
        for i in range(3):
            _rewrite(tight_manager, content=f"session block number {i}", session=SESSION)
        with pytest.raises(ContextRewriteRateLimitError):
            _rewrite(tight_manager, content="session block number three", session=SESSION)
        # Another session in the same project: its own bucket.
        assert (
            _rewrite(tight_manager, content="other session block", session="sess-other")["status"]
            == "stored"
        )
        # Another project: its own bucket entirely.
        assert (
            _rewrite(
                tight_manager,
                content="other project block",
                project="crw-other",
                session=SESSION,
            )["status"]
            == "stored"
        )

    def test_session_none_is_its_own_bucket(self, tight_manager: MemoryManager) -> None:
        for i in range(3):
            _rewrite(tight_manager, content=f"sessionless block number {i}")
        with pytest.raises(ContextRewriteRateLimitError):
            _rewrite(tight_manager, content="sessionless block number three")
        # A sessioned event in the same project is a different bucket.
        assert (
            _rewrite(tight_manager, content="sessioned block", session=SESSION)["status"]
            == "stored"
        )

    def test_zero_disables_the_limiter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _manager(
                _settings(Path(tmpdir), mnemos_extra={"context_rewrite_rate_limit_per_minute": 0})
            )
            try:
                for i in range(35):
                    assert (
                        _rewrite(mgr, content=f"unlimited block number {i:02d}", session=SESSION)[
                            "status"
                        ]
                        == "stored"
                    )
            finally:
                mgr.close()

    def test_mcp_rate_limited_shape(self, tight_manager: MemoryManager, monkeypatch) -> None:
        monkeypatch.setattr(mcp_mod, "_manager", tight_manager)
        loop = asyncio.new_event_loop()
        for i in range(3):
            loop.run_until_complete(
                _dispatch(
                    "vesma_context_rewrite",
                    {
                        "content": f"mcp block number {i}",
                        "project": PROJECT,
                        "agent": AGENT,
                        "session": SESSION,
                    },
                )
            )
        blocked = loop.run_until_complete(
            _dispatch(
                "vesma_context_rewrite",
                {
                    "content": "mcp block number three",
                    "project": PROJECT,
                    "agent": AGENT,
                    "session": SESSION,
                },
            )
        )
        loop.close()
        assert blocked.get("rate_limited") is True
        assert "rate limit exceeded" in str(blocked.get("error", ""))

    def test_rest_surfaces_429(
        self, tight_manager: MemoryManager, tight_rest_client: TestClient
    ) -> None:
        for i in range(3):
            resp = tight_rest_client.post(
                "/context/rewrite",
                json={
                    "content": f"rest block number {i}",
                    "project": PROJECT,
                    "agent": AGENT,
                    "session": SESSION,
                },
            )
            assert resp.status_code == 200
        blocked = tight_rest_client.post(
            "/context/rewrite",
            json={
                "content": "rest block number three",
                "project": PROJECT,
                "agent": AGENT,
                "session": SESSION,
            },
        )
        assert blocked.status_code == 429, "backpressure is 429, not 500/422"
        assert "rate limit exceeded" in blocked.json()["detail"]


class TestSizeCaps:
    def test_content_over_cap_rejected_no_write(self, tight_manager: MemoryManager) -> None:
        with pytest.raises(ValueError, match="content exceeds the rewrite size cap"):
            _rewrite(tight_manager, content="x" * 201, session=SESSION)
        conn = tight_manager.sqlite._get_conn()
        rows = conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()
        assert rows["n"] == 0

    def test_diff_over_cap_rejected_no_write(self, tight_manager: MemoryManager) -> None:
        with pytest.raises(ValueError, match="diff exceeds the rewrite size cap"):
            _rewrite(tight_manager, diff="d" * 51)
        conn = tight_manager.sqlite._get_conn()
        rows = conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()
        assert rows["n"] == 0

    def test_boundary_at_cap_accepted(self, tight_manager: MemoryManager) -> None:
        """Exactly at the cap is NOT over the cap (≤ semantics)."""
        receipt = _rewrite(tight_manager, content="y" * 200, session=SESSION, diff="d" * 50)
        assert receipt["status"] == "stored"

    def test_documented_defaults(self) -> None:
        from vesmaro.config import VesmaConfig

        cfg = VesmaConfig()
        assert cfg.context_rewrite_rate_limit_per_minute == 30
        assert cfg.context_rewrite_max_content_chars == 1_048_576  # 1 MiB
        assert cfg.context_rewrite_max_diff_chars == 262_144  # 256 KiB


# ── Rehydrate roundtrip (existing gated paths only) ──────────────────────────


class TestRehydrateRoundtrip:
    def test_raw_invisible_until_pipeline_advances(self, manager: MemoryManager) -> None:
        receipt = _rewrite(manager, session=SESSION)
        new_id = str(receipt["memory_id"])

        # Entry invariant: raw is unreachable from context issuance.
        before = manager.assemble_context(session=SESSION, project=PROJECT)
        assert new_id not in {b["memory_id"] for b in before["blocks"]}

        _advance(manager, new_id, MemoryStatus.PROCESSED)

        after = manager.assemble_context(session=SESSION, project=PROJECT)
        block = next((b for b in after["blocks"] if b["memory_id"] == new_id), None)
        assert block is not None, "advanced rewrite original must rehydrate"
        assert block["status"] == "processed"
        assert block["provenance"].startswith(f"[mnemos:{new_id} project={PROJECT}")

    def test_marker_redeems_through_retrieve_content(self, manager: MemoryManager) -> None:
        receipt = _rewrite(manager, session=SESSION, include_marker=True)

        ccr = receipt.get("ccr_marker")
        assert isinstance(ccr, dict)
        assert ccr["marker"], "compress marker must be present for the window"
        assert ccr["hash"]

        # The existing scanned, project-scoped rehydrate channel redeems it.
        result = manager.retrieve_content(str(ccr["hash"]), project=PROJECT)
        assert result["found"] is True
        assert result["original"] == ORIGINAL_V2

    def test_marker_omitted_by_default(self, manager: MemoryManager) -> None:
        receipt = _rewrite(manager, session=SESSION)
        assert "ccr_marker" not in receipt


# ── Scan behaviour on secret in the original ─────────────────────────────────


class TestSecretInOriginal:
    def test_layer1_tags_no_federate_and_issuance_redacts(self, manager: MemoryManager) -> None:
        secret_original = (
            "Deployment notes for the unobtanium gateway service.\n"
            f"The service authenticates with api key {FAKE_AWS_KEY}\n"
            "Rotate quarterly per the security baseline.\n"
        )
        receipt = _rewrite(manager, session=SESSION, content=secret_original)
        new_id = str(receipt["memory_id"])

        # Layer 1 (write path, normal add): the record is excluded from
        # external exchange; the original is stored unchanged.
        stored = manager.sqlite.get(new_id)
        assert stored is not None
        assert "mnemos:no-federate" in stored.tags
        assert FAKE_AWS_KEY in stored.content, "zero-loss storage"

        # Issuance (assemble channel): redacted, counted.
        _advance(manager, new_id, MemoryStatus.PROCESSED)
        result = manager.assemble_context(session=SESSION, project=PROJECT)
        block = next(b for b in result["blocks"] if b["memory_id"] == new_id)
        assert FAKE_AWS_KEY not in block["content"]
        assert "<REDACTED:aws-key>" in block["content"]
        assert block["redactions"] >= 1


# ── Surfaces: MCP + REST ride the manager path ───────────────────────────────


class TestSurfaces:
    def test_mcp_tool_stored_and_deduplicated(self, manager: MemoryManager, monkeypatch) -> None:
        monkeypatch.setattr(mcp_mod, "_manager", manager)
        loop = asyncio.new_event_loop()
        args = {"content": ORIGINAL_V2, "project": PROJECT, "agent": AGENT, "session": SESSION}
        first = loop.run_until_complete(_dispatch("vesma_context_rewrite", dict(args)))
        second = loop.run_until_complete(_dispatch("vesma_context_rewrite", dict(args)))
        loop.close()

        assert first["status"] == "stored"
        assert second["status"] == "deduplicated"
        assert second["memory_id"] == first["memory_id"]

    def test_mcp_tool_error_shapes(self, manager: MemoryManager, monkeypatch) -> None:
        monkeypatch.setattr(mcp_mod, "_manager", manager)
        loop = asyncio.new_event_loop()
        missing = loop.run_until_complete(
            _dispatch("vesma_context_rewrite", {"project": PROJECT, "agent": AGENT})
        )
        bad_session = loop.run_until_complete(
            _dispatch(
                "vesma_context_rewrite",
                {"content": ORIGINAL_V2, "project": PROJECT, "agent": AGENT, "session": 42},
            )
        )
        ghost = loop.run_until_complete(
            _dispatch(
                "vesma_context_rewrite",
                {
                    "content": ORIGINAL_V2,
                    "project": PROJECT,
                    "agent": AGENT,
                    "supersedes": "00000000-0000-0000-0000-000000000000",
                },
            )
        )
        loop.close()

        assert missing == {"error": "content is required and must be a non-empty string"}
        assert bad_session == {"error": "session must be a non-empty string when provided"}
        assert "not found in project" in str(ghost.get("error", ""))

    def test_rest_endpoint_stored_and_dedup(
        self, manager: MemoryManager, rest_client: TestClient
    ) -> None:
        body = {"content": ORIGINAL_V2, "project": PROJECT, "agent": AGENT, "session": SESSION}
        first = rest_client.post("/context/rewrite", json=body)
        second = rest_client.post("/context/rewrite", json=body)

        assert first.status_code == 200
        assert second.status_code == 200, "idempotent event: re-delivery is not a 201"
        assert first.json()["status"] == "stored"
        assert second.json()["status"] == "deduplicated"
        assert second.json()["memory_id"] == first.json()["memory_id"]

    def test_rest_endpoint_validation_422(
        self, manager: MemoryManager, rest_client: TestClient
    ) -> None:
        missing = rest_client.post(
            "/context/rewrite", json={"content": ORIGINAL_V2, "agent": AGENT}
        )
        ghost = rest_client.post(
            "/context/rewrite",
            json={
                "content": ORIGINAL_V2,
                "project": PROJECT,
                "agent": AGENT,
                "supersedes": "00000000-0000-0000-0000-000000000000",
            },
        )
        assert missing.status_code == 422
        assert ghost.status_code == 422
