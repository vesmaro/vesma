"""ADR-0019 Phase B slice B2b — the outward semantics.

Coverage map (one section per B2b deliverable):

* **Immediate visibility + policy (§2)** — the ``vesmaro.visibility``
  config knob (``immediate`` default / ``curated``); records created
  WITHOUT an explicit ``status`` pass the SAME Phase A gate at ingest
  (``path=ingest``): clean ⇒ PUBLISHED + ``pipeline_state='pending'``
  and findable at once (no sleeps); a refusal or scanner error ⇒ stored
  RAW, invisible, zero-loss; curated ⇒ RAW + pending until the refine
  cycle completes and the publication gate admits the projection (a
  refusal at THAT point is the lane-(b) quarantine). An explicit
  ``status=`` keeps the pre-B2b N1 contract.
* **Retraction on direct access (§5)** — a quarantined row answers
  direct-id reads (manager.get / REST GET /memories/{id}, the
  ``include_raw`` drill-down included) with the ``[retracted:
  <iso-ts>]`` render instead of its content; raw_content/clean_content/
  title are withheld, lifecycle metadata stays visible; the CCR
  cached-original channel serves the same render when the payload
  belongs to a quarantined row; same-ID semantics (no tombstone row),
  and manual release restores the real content.
* **CLI embed routing (N1 review #163)** — the federated sync/import
  re-embeds go through ``MemoryManager.upsert_embedding`` so the
  vectors carry the ``content_hash`` freshness key and the sweeper can
  heal them.
* **F7** — an external ``update(metadata=...)`` cannot wipe the
  internal lifecycle keys (retry counter / backoff gate).
* **F8** — a clean content edit of a ``refined`` row re-enters the
  refine intake (``pending``); ``swap_key`` is deliberately kept.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

import vesmaro.mcp_server as mcp_mod
from vesmaro.api import main as api_main
from vesmaro.api.main import app, lifespan
from vesmaro.cli.import_ import _reembed
from vesmaro.cli.sync import run_sync_export, run_sync_import
from vesmaro.config import Settings, VesmaConfig
from vesmaro.danger_detectors import DetectionResult
from vesmaro.manager import MemoryManager
from vesmaro.models import (
    Memory,
    MemoryCreate,
    MemorySource,
    MemoryStatus,
    MemoryUpdate,
    PipelineState,
    render_retraction,
)

PROJECT = "b2b-proj"
AGENT = "b2b-agent"

# Fake high-confidence secrets (the detector's own regex shapes — never real).
FAKE_AWS_KEY = "AKIAEXAMPLEABCDEFGH1"

# Cause-neutral retraction render (ArchCom amendment, CWE-209): the
# content-side render carries ONLY the iso-ts — never the detector class.
RETRACTION_RE = re.compile(r"^\[retracted: (?P<ts>\S+)\]$")

TAGS = [f"project:{PROJECT}", f"agent:{AGENT}", "mnemos:learning"]


# ── Fixtures / helpers ─────────────────────────────────────────────────────────


def _settings(tmp: Path, *, visibility: str | None = None) -> Settings:
    mnemos: dict[str, object] = {
        "vault_path": str(tmp / "vault"),
        "data_dir": str(tmp / "data"),
        "db_name": "test.db",
    }
    if visibility is not None:
        mnemos["visibility"] = visibility
    settings = Settings(
        mnemos=mnemos,  # type: ignore[arg-type]
        scanner={"enabled": False},
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
    """Immediate-mode manager (the B2b default)."""
    with TemporaryDirectory() as tmpdir:
        mgr = _manager(_settings(Path(tmpdir)))
        yield mgr
        mgr.close()


@pytest.fixture
def curated_manager() -> Iterator[MemoryManager]:
    with TemporaryDirectory() as tmpdir:
        mgr = _manager(_settings(Path(tmpdir), visibility="curated"))
        yield mgr
        mgr.close()


@pytest.fixture
def rest_client(manager: MemoryManager) -> Iterator[TestClient]:
    api_main._manager = manager
    test_app = FastAPI(title="Mnemos-B2b-Test", version="0.1.0", lifespan=lifespan)
    for route in app.routes:
        test_app.routes.append(route)
    with TestClient(test_app) as tc:
        yield tc
    api_main._manager = None


def _add(
    mgr: MemoryManager,
    content: str,
    *,
    status: MemoryStatus | None = None,
    tags: list[str] | None = None,
) -> Memory:
    """add() with an OPTIONAL explicit status (None = the B2b policy leg)."""
    kwargs: dict[str, object] = {
        "content": content,
        "tags": tags or TAGS,
        "source": MemorySource.MCP,
    }
    if status is not None:
        kwargs["status"] = status
    return mgr.add(MemoryCreate(**kwargs), project=PROJECT, agent=AGENT)  # type: ignore[arg-type]


def _quarantined(mgr: MemoryManager, memory_id: str, *, reason: str = "secret") -> Memory:
    assert mgr.quarantine_entry(memory_id, reason=reason, source="test")
    row = mgr.sqlite.get(memory_id)
    assert row is not None
    return row


# ── 1. Config: the visibility knob ────────────────────────────────────────────


class TestVisibilityConfig:
    def test_default_is_immediate(self) -> None:
        assert VesmaConfig().visibility == "immediate"
        assert Settings().mnemos.visibility == "immediate"

    def test_curated_accepted(self) -> None:
        assert VesmaConfig(visibility="curated").visibility == "curated"

    def test_unknown_value_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VesmaConfig(visibility="curved")

    def test_env_override_canonical_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VESMA_MNEMOS__VISIBILITY", "curated")
        assert Settings().mnemos.visibility == "curated"
        monkeypatch.setenv("VESMA_MNEMOS__VISIBILITY", "immediate")
        assert Settings().mnemos.visibility == "immediate"


# ── 2. Immediate: ingest gate + instant findability (§2) ──────────────────────


class TestImmediateIngest:
    def test_clean_add_published_pending_findable_at_once(
        self, manager: MemoryManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        """§2: clean content ⇒ PUBLISHED + pipeline_state=pending, and
        the row is findable IMMEDIATELY (the FTS leg — no sleeps)."""
        with caplog.at_level("INFO", logger="vesmaro.manager"):
            mem = _add(manager, "immediate visibility probe about axolotl")
        assert mem.status == MemoryStatus.PUBLISHED
        assert mem.pipeline_state == PipelineState.PENDING
        audit = [r for r in caplog.records if "publish gate" in r.message]
        assert audit and "verdict=pass" in audit[-1].message
        assert "path=ingest" in audit[-1].message
        # Findable at once: the FTS leg of search, no sleep anywhere.
        hits = manager.search("axolotl", project=PROJECT)
        assert mem.id in {h.memory.id for h in hits}
        # Embed stamped with the freshness key (single write point).
        assert manager.vectors.has(mem.id)
        meta = manager.vectors.get_metadata([mem.id])[mem.id]
        assert meta["content_hash"] == manager._embed_content_hash(manager._embedding_text(mem))

    def test_secret_add_stored_raw_invisible_zero_loss(
        self, manager: MemoryManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Ingest refusal: stored RAW, pipeline_state NULL, invisible;
        the content itself is kept (zero-loss) and audited."""
        with caplog.at_level("WARNING", logger="vesmaro.manager"):
            mem = _add(manager, f"deploy note with api key {FAKE_AWS_KEY} inline")
        assert mem.status == MemoryStatus.RAW
        assert mem.pipeline_state is None
        stored = manager.sqlite.get(mem.id)
        assert stored is not None
        assert stored.status == MemoryStatus.RAW
        assert FAKE_AWS_KEY in stored.content  # zero-loss
        # Invisible to the DEFAULT issuance (the include_raw drill-down is
        # the documented caller-side widening and legitimately sees it).
        assert manager.search("deploy", project=PROJECT) == []
        audit = [r for r in caplog.records if "publish gate" in r.message]
        assert audit and "verdict=refused" in audit[-1].message
        assert "path=ingest" in audit[-1].message
        assert "reason=danger-detector" in audit[-1].message
        assert FAKE_AWS_KEY not in audit[-1].message  # raw values never logged

    def test_scanner_error_fail_closed_at_ingest(
        self, manager: MemoryManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "vesmaro.manager.detect",
            lambda content, title=None: DetectionResult(error="boom"),
        )
        mem = _add(manager, "clean body but the scanner is down")
        assert mem.status == MemoryStatus.RAW
        assert mem.pipeline_state is None
        assert manager.sqlite.get(mem.id) is not None  # stored zero-loss

    def test_explicit_status_respects_pre_b2b_contract(self, manager: MemoryManager) -> None:
        """An explicit ``status=`` never meets the policy leg: RAW stays
        the legacy invisible row; a clean explicit PUBLISHED keeps the
        N1 direct-seed audit path."""
        raw = _add(manager, "explicit raw body", status=MemoryStatus.RAW)
        assert raw.status == MemoryStatus.RAW
        assert raw.pipeline_state is None
        pub = _add(manager, "explicit published body", status=MemoryStatus.PUBLISHED)
        assert pub.status == MemoryStatus.PUBLISHED
        assert pub.pipeline_state is None  # direct-seed does not enqueue (B1)

    def test_rest_create_inherits_policy(self, rest_client: TestClient) -> None:
        resp = rest_client.post(
            "/memories",
            json={"content": "rest default visibility body about narwhal", "tags": TAGS},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "published"
        assert body["pipeline_state"] == "pending"
        found = rest_client.post("/search", json={"query": "narwhal", "project": PROJECT}).json()
        assert body["id"] in {item["id"] for item in found}

    def test_mcp_add_inherits_policy(self, manager: MemoryManager) -> None:
        from vesmaro.mcp_server import _dispatch

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(mcp_mod, "_manager", manager)
            result = asyncio.new_event_loop().run_until_complete(
                _dispatch("vesma_add", {"content": "mcp default body about ibex", "tags": TAGS})
            )
        assert result["status"] == "published"
        row = manager.sqlite.get(result["id"])
        assert row is not None
        assert row.pipeline_state == PipelineState.PENDING


# ── 3. Curated: invisible until the refine cycle gates the projection ─────────


class TestCuratedVisibility:
    def test_curated_add_raw_pending_invisible_then_visible_after_refine(
        self, curated_manager: MemoryManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        mgr = curated_manager
        mem = _add(mgr, "curated body awaiting refinement about okapi")
        assert mem.status == MemoryStatus.RAW
        assert mem.pipeline_state == PipelineState.PENDING
        # Invisible to the default issuance until the cycle completes (the
        # include_raw drill-down is a documented caller-side widening).
        assert mgr.search("okapi", project=PROJECT) == []

        # Lone entry ⇒ honest noop; the completion still owes the
        # publication verdict → clean ⇒ PUBLISHED now.
        with (
            caplog.at_level("INFO", logger="vesmaro.pipeline.refine"),
            caplog.at_level("INFO", logger="vesmaro.manager"),
        ):
            summary = mgr.refine_pending()
        assert summary["refined_noop"] == 1
        row = mgr.sqlite.get(mem.id)
        assert row is not None
        assert row.pipeline_state == PipelineState.REFINED
        assert row.status == MemoryStatus.PUBLISHED
        hits = mgr.search("okapi", project=PROJECT)
        assert mem.id in {h.memory.id for h in hits}
        assert mgr.vectors.has(mem.id)  # the publication embedded the row
        audit = [r for r in caplog.records if "curated-published" in r.message]
        assert audit

    def test_curated_secret_refused_at_publication_quarantined(
        self, curated_manager: MemoryManager
    ) -> None:
        mgr = curated_manager
        mem = _add(mgr, f"curated secret carrier {FAKE_AWS_KEY} about puffin")
        assert mem.pipeline_state == PipelineState.PENDING  # stored, waiting
        summary = mgr.refine_pending()
        assert summary["refined_noop"] == 1  # the cycle ran to completion
        row = mgr.sqlite.get(mem.id)
        assert row is not None
        assert row.pipeline_state == PipelineState.QUARANTINED
        assert row.quarantine_reason  # detector class code(s)
        assert row.status == MemoryStatus.RAW  # never became visible
        assert mgr.search("puffin", project=PROJECT, include_raw=True) == []

    def test_curated_swap_publishes_refined_content_with_visibility(
        self, curated_manager: MemoryManager
    ) -> None:
        """Cluster of ≥2 admissible mates ⇒ a real artifact: the curated
        row gets the refined projection AND its visibility together."""
        mgr = curated_manager
        # Explicit-status seeds keep the legacy contract: PUBLISHED mates
        # (admissible cluster context for the stub producer).
        mate1 = _add(mgr, "curated mate one loon unique", status=MemoryStatus.PUBLISHED)
        mate2 = _add(mgr, "curated mate two plover unique", status=MemoryStatus.PUBLISHED)
        target = _add(mgr, "curated swap target about rhea")
        for row in (mate1, mate2, target):
            mgr.sqlite.update_fields(row.id, cluster_id="cl-curated")
        assert mgr.sqlite.get(target.id).pipeline_state == PipelineState.PENDING

        summary = mgr.refine_pending()
        assert summary["refined"] >= 1
        swapped = mgr.sqlite.get(target.id)
        assert swapped is not None
        assert swapped.pipeline_state == PipelineState.REFINED
        assert swapped.status == MemoryStatus.PUBLISHED  # visibility landed
        assert "plover" in swapped.effective_content()  # refined content served
        hits = mgr.search("plover", project=PROJECT)
        assert target.id in {h.memory.id for h in hits}
        meta = mgr.vectors.get_metadata([target.id])[target.id]
        assert meta["content_hash"] == mgr._embed_content_hash(
            mgr._embedding_text(mgr.sqlite.get(target.id))
        )

    def test_policy_switch_flips_behavior(
        self, manager: MemoryManager, curated_manager: MemoryManager
    ) -> None:
        """Same payload, two knobs: immediate publishes, curated holds."""
        immediate_mem = _add(manager, "switch probe body about takin")
        curated_mem = _add(curated_manager, "switch probe body about takin")
        assert immediate_mem.status == MemoryStatus.PUBLISHED
        assert curated_mem.status == MemoryStatus.RAW
        assert curated_mem.pipeline_state == PipelineState.PENDING


# ── 4. Retraction on direct access (§5) ───────────────────────────────────────


class TestRetraction:
    def _seed_quarantined(self, mgr: MemoryManager) -> Memory:
        mem = _add(mgr, "soon to be retracted body about lynx")
        return _quarantined(mgr, mem.id, reason="secret")

    def test_manager_get_serves_render_not_content(self, manager: MemoryManager) -> None:
        row = self._seed_quarantined(manager)
        served = manager.get(row.id)
        assert served is not None
        m = RETRACTION_RE.match(served.content)
        assert m is not None, served.content
        # Cause-neutral: the detector class never enters the render…
        assert "secret" not in served.content
        # …it stays operator-side metadata on the same response.
        assert served.quarantine_reason == "secret"
        # Nothing content-like leaves the row.
        assert served.raw_content is None
        assert served.clean_content is None
        assert served.title is None
        # Lifecycle metadata stays visible for forensics.
        assert served.id == row.id
        assert served.status == row.status
        assert served.pipeline_state == PipelineState.QUARANTINED
        assert served.updated_at is not None
        # Same-ID semantics: no tombstone row was created.
        assert len(manager.sqlite.list_all(limit=100)) == 1
        # Zero-loss: the store still holds the source for maintenance.
        stored = manager.sqlite.get(row.id)
        assert stored is not None
        assert stored.content == row.content

    def test_render_retraction_format(self) -> None:
        mem = Memory(content="x", quarantine_reason="injection")
        no_ts = mem.model_copy(update={"updated_at": None})
        # Neutral even though the row carries a reason (CWE-209).
        assert render_retraction(no_ts) == "[retracted: unknown]"
        assert render_retraction(mem).startswith("[retracted: 2")
        assert "injection" not in render_retraction(mem)

    def test_rest_get_serves_render_both_raw_variants(
        self, rest_client: TestClient, manager: MemoryManager
    ) -> None:
        """Auth OFF (the loopback single-operator default): the retraction
        render is cause-neutral and the reason stays visible to the
        operator-caller; include_raw must not resurrect the source."""
        row = self._seed_quarantined(manager)
        for url in (f"/memories/{row.id}", f"/memories/{row.id}?include_raw=true"):
            resp = rest_client.get(url)
            assert resp.status_code == 200
            body = resp.json()
            assert RETRACTION_RE.match(body["content"])
            assert "secret" not in body["content"]  # neutral render
            assert body["raw_content"] is None
            assert body["clean_content"] is None
            assert body["title"] is None
            assert body["pipeline_state"] == "quarantined"
            assert body["quarantine_reason"] == "secret"  # operator context (auth off)

    def test_rest_get_reason_hidden_without_operator_context_when_auth_on(
        self, manager: MemoryManager
    ) -> None:
        """Auth ON: an unadmitted request loses the quarantine_reason FIELD
        (not the response); an admitted one keeps it."""
        from starlette.middleware.base import BaseHTTPMiddleware

        row = self._seed_quarantined(manager)
        manager.settings.api.auth_enabled = True
        test_app = FastAPI(title="Mnemos-B2b-Auth", version="0.1.0", lifespan=lifespan)
        for route in app.routes:
            test_app.routes.append(route)

        class _AdmitStub(BaseHTTPMiddleware):
            """Mimics AuthMiddleware: the X-Operator header admits."""

            async def dispatch(self, request, call_next):
                if request.headers.get("X-Operator") == "1":
                    request.state.auth_session = {"sub": "operator"}
                return await call_next(request)

        test_app.add_middleware(_AdmitStub)
        api_main._manager = manager  # lifespan must not build a real manager
        try:
            with TestClient(test_app) as tc:
                anon = tc.get(f"/memories/{row.id}").json()
                assert RETRACTION_RE.match(anon["content"])  # render still served
                assert anon["quarantine_reason"] is None  # field hidden, not the response
                op = tc.get(f"/memories/{row.id}", headers={"X-Operator": "1"}).json()
                assert op["quarantine_reason"] == "secret"
        finally:
            api_main._manager = None

    def test_rest_get_404_for_missing_still_distinct(
        self, rest_client: TestClient, manager: MemoryManager
    ) -> None:
        row = self._seed_quarantined(manager)
        assert rest_client.get(f"/memories/{row.id}").status_code == 200  # retracted, not 404
        assert rest_client.get("/memories/00000000-0000-0000-0000-000000000000").status_code == 404

    def test_non_quarantined_get_untouched(self, manager: MemoryManager) -> None:
        mem = _add(manager, "ordinary visible body about tapir")
        served = manager.get(mem.id)
        assert served is not None
        assert served.content == mem.content

    def test_release_restores_real_content(self, manager: MemoryManager) -> None:
        """Terminality exit: after the manual release the same id serves
        the stored content again (same-ID semantics end-to-end)."""
        row = self._seed_quarantined(manager)
        assert manager.release_quarantine(row.id, source="test")
        served = manager.get(row.id)
        assert served is not None
        assert served.content == row.content

    def test_ccr_retrieve_original_serves_render_for_quarantined_source(
        self, manager: MemoryManager
    ) -> None:
        """§5 on the CCR channel: a cached original that belongs to a
        quarantined row is issued as the retraction render, never as the
        payload. Clean sources keep the full-original contract."""
        payload = ("quarantine ccr original about heron — " + "filler line of text. " * 60)[:2000]
        mem = _add(manager, payload)
        cached = manager.compress_content(payload, profile="log", project=PROJECT)
        assert cached["cached"] is True
        # Pre-quarantine: the original is issued.
        before = manager.retrieve_content(cached["hash"], project=PROJECT)
        assert before["found"] is True
        assert before.get("original") == payload

        _quarantined(manager, mem.id, reason="secret")
        after = manager.retrieve_content(cached["hash"], project=PROJECT)
        assert after["found"] is True
        assert RETRACTION_RE.match(after["original"])
        assert after.get("retracted") is True
        assert payload not in after["original"]

    def test_mcp_retrieve_serves_render_for_quarantined_source(
        self, manager: MemoryManager
    ) -> None:
        from vesmaro.mcp_server import _dispatch

        payload = ("mcp quarantine ccr original about egret — " + "filler line. " * 80)[:2000]
        mem = _add(manager, payload)
        cached = manager.compress_content(payload, profile="log", project=PROJECT)
        _quarantined(manager, mem.id, reason="injection")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(mcp_mod, "_manager", manager)
            result = asyncio.new_event_loop().run_until_complete(
                _dispatch("vesma_retrieve", {"hash": cached["hash"], "project": PROJECT})
            )
        assert result["found"] is True
        assert RETRACTION_RE.match(result["original"])
        assert result.get("retracted") is True


# ── 5. CLI embed routing through the single write point ───────────────────────


class TestCliEmbedRouting:
    def test_sync_import_embed_carries_content_hash(self, tmp_path: Path) -> None:
        with TemporaryDirectory() as src_tmp, TemporaryDirectory() as dst_tmp:
            src = _manager(_settings(Path(src_tmp)))
            dst = _manager(_settings(Path(dst_tmp)))
            try:
                _add(src, "federated embed probe about gannet")
                out = tmp_path / "sync-payload.json"
                run_sync_export(src, output=out, shared_projects_arg=PROJECT)
                result = run_sync_import(dst, source=out)
                assert result.errors == []
                # The federated id (``fed:<agent>:<uuid>``) — not the source
                # id — is what the receiving side persists.
                rows = dst.sqlite.list_all(limit=10)
                assert len(rows) == 1
                fed_id = rows[0].id
                assert rows[0].status == MemoryStatus.PUBLISHED
                # The federated embed went through upsert_embedding:
                # stamped with the freshness key.
                assert dst.vectors.has(fed_id)
                meta = dst.vectors.get_metadata([fed_id])[fed_id]
                row = dst.sqlite.get(fed_id)
                assert row is not None
                assert meta["content_hash"] == dst._embed_content_hash(dst._embedding_text(row))
            finally:
                src.close()
                dst.close()

    def test_import_reembed_carries_content_hash(self, manager: MemoryManager) -> None:
        mem = _add(manager, "json import embed probe about curlew")
        manager.vectors.delete(mem.id)
        _reembed(manager, manager.sqlite.get(mem.id))
        assert manager.vectors.has(mem.id)
        meta = manager.vectors.get_metadata([mem.id])[mem.id]
        row = manager.sqlite.get(mem.id)
        assert row is not None
        assert meta["content_hash"] == manager._embed_content_hash(manager._embedding_text(row))

    def test_import_reembed_skips_non_published(self, manager: MemoryManager) -> None:
        mem = _add(manager, "unpublished import body about dotterel", status=MemoryStatus.RAW)
        _reembed(manager, manager.sqlite.get(mem.id))
        assert not manager.vectors.has(mem.id)


# ── 6. F7 — internal lifecycle metadata keys survive external updates ─────────


class TestF7InternalMetadataMerge:
    def test_external_metadata_replace_keeps_retry_counter(self, manager: MemoryManager) -> None:
        mem = _add(manager, "f7 retry counter carrier about saki")
        assert manager.sqlite.record_refine_failure(mem.id, attempt=2, next_retry_at="x")
        updated = manager.update(mem.id, MemoryUpdate(metadata={"owner_note": "curated by alice"}))
        assert updated is not None
        assert updated.metadata["owner_note"] == "curated by alice"  # external key applied
        assert updated.metadata["pipeline_retry_count"] == 2  # internal key survived
        assert updated.metadata["pipeline_retry_at"] == "x"

    def test_external_cannot_forge_internal_keys(self, manager: MemoryManager) -> None:
        mem = _add(manager, "f7 forge attempt body about uakari")
        assert manager.sqlite.record_refine_failure(mem.id, attempt=3, next_retry_at=None)
        updated = manager.update(
            mem.id, MemoryUpdate(metadata={"pipeline_retry_count": 0, "mood": "chill"})
        )
        assert updated is not None
        assert updated.metadata["pipeline_retry_count"] == 3  # merge wins, not the payload
        assert updated.metadata["mood"] == "chill"

    def test_fresh_row_injects_no_internal_keys(self, manager: MemoryManager) -> None:
        mem = _add(manager, "f7 clean row body about tarsier")
        updated = manager.update(mem.id, MemoryUpdate(metadata={"k": "v"}))
        assert updated is not None
        assert updated.metadata == {"k": "v"}


# ── 7. F8 — content edit of a refined row re-enters the refine intake ─────────


class TestF8RefinedContentEditRequeues:
    def _refined_row(self, mgr: MemoryManager, content: str) -> Memory:
        mem = _add(mgr, content)
        assert mgr.sqlite.update_fields(
            mem.id,
            pipeline_state=PipelineState.REFINED,
            swap_key="swap-key-f8",
            processed_at="2026-08-30T00:00:00+00:00",
        )
        return mgr.sqlite.get(mem.id)

    def test_clean_content_edit_requeues_pending_keeps_swap_key(
        self, manager: MemoryManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        row = self._refined_row(manager, "f8 refined body about goral")
        with caplog.at_level("INFO", logger="vesmaro.manager"):
            updated = manager.update(
                row.id, MemoryUpdate(content="f8 edited clean body about goral")
            )
        assert updated is not None
        assert updated.status == MemoryStatus.PUBLISHED  # clean edit stays visible
        assert updated.pipeline_state == PipelineState.PENDING  # re-queued
        assert updated.swap_key == "swap-key-f8"  # kept; the new cycle recomputes
        audit = [r for r in caplog.records if "outcome=enqueued" in r.message]
        assert audit and "from=refined" in audit[-1].message

    def test_title_only_edit_keeps_refined(self, manager: MemoryManager) -> None:
        row = self._refined_row(manager, "f8 title edit body about chamois")
        updated = manager.update(row.id, MemoryUpdate(title="Clean new title"))
        assert updated is not None
        assert updated.pipeline_state == PipelineState.REFINED  # artifact still valid

    def test_dirty_edit_demotes_without_touching_pipeline_state(
        self, manager: MemoryManager
    ) -> None:
        """B1 invariant pinned: N1 demotions write RAW-status side
        effects only — never a pipeline_state transition."""
        row = self._refined_row(manager, "f8 dirty edit body about serow")
        updated = manager.update(row.id, MemoryUpdate(content=f"now with {FAKE_AWS_KEY}"))
        assert updated is not None
        assert updated.status == MemoryStatus.RAW
        assert updated.pipeline_state == PipelineState.REFINED  # untouched by the demotion
