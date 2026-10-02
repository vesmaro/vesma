"""VesmaSDK facade (mnemos #125, Wave 3) — delegation contract.

The facade owns no DOMAIN logic (src/vesma/sdk.py): every verb is a
one-line delegation to a ``MemoryManager`` method — EXCEPT the two
channel-boundary duties the W3 security review pinned (F1/F2): ``recall``
scans every echoed item at issuance (mirroring ``vesma_search``) and
``remember`` validates caller tags against the tag contract (mirroring
``vesma_add``). These tests pin the delegation (spy/monkeypatch the
manager methods), the two channel duties, and the facade's other
boundary behaviours (constructor exactly-one-of, ``forget``'s project
guard). The delegated-to paths themselves are covered by their own
suites (test_assemble_context.py, test_context_rewrite.py, …).

All secrets below are obviously fake EXAMPLE-style values built from
the detector's own pattern catalogue; real credentials never appear.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.models import MemoryCreate, MemoryStatus, TagContractError
from vesma.sdk import VesmaSDK

PROJECT = "sdk-proj"
AGENT = "sdk-agent"
SESSION = "sdk-session"
FAKE_AWS_KEY = "AKIAEXAMPLEABCDEFGH1"


def _settings(tmp: Path, **ccr: Any) -> Settings:
    settings = Settings(
        vesma={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
        },
        ccr={"min_size_chars": 100, **ccr},  # type: ignore[arg-type]
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def manager() -> Iterator[MemoryManager]:
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = MemoryManager(_settings(Path(tmpdir)))
        yield mgr
        mgr.close()


@pytest.fixture
def refuse_manager() -> Iterator[MemoryManager]:
    """Refuse-mode deployment (ccr.retrieve_refuse_on_secret=True)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = MemoryManager(_settings(Path(tmpdir), retrieve_refuse_on_secret=True))
        yield mgr
        mgr.close()


@pytest.fixture
def sdk(manager: MemoryManager) -> VesmaSDK:
    return VesmaSDK(manager=manager)


# ── Constructor ───────────────────────────────────────────────────────────────


class TestConstructor:
    def test_both_args_rejected(self, manager: MemoryManager) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            VesmaSDK(_settings(Path("/nonexistent")), manager=manager)

    def test_neither_arg_rejected(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            VesmaSDK()

    def test_manager_exposed(self, manager: MemoryManager, sdk: VesmaSDK) -> None:
        assert sdk.manager is manager


# ── remember → add ────────────────────────────────────────────────────────────


class TestRemember:
    def test_delegates_to_add(self, sdk: VesmaSDK, manager: MemoryManager) -> None:
        calls: list[dict[str, Any]] = []
        original = manager.add

        def _spy(data: MemoryCreate, **kw: Any) -> Any:
            calls.append({"data": data, **kw})
            return original(data, **kw)

        manager.add = _spy  # type: ignore[method-assign]

        memory = sdk.remember("note body", PROJECT, AGENT, title="a title")

        assert memory.content == "note body"
        assert memory.project == PROJECT
        assert memory.agent == AGENT
        assert len(calls) == 1
        assert calls[0]["data"].title == "a title", "kw passes through to MemoryCreate"
        assert calls[0]["project"] == PROJECT
        assert calls[0]["agent"] == AGENT

    def test_invalid_mnemos_subtype_rejected_no_write(
        self, sdk: VesmaSDK, manager: MemoryManager
    ) -> None:
        """F2: the tag contract runs at the facade — an invalid reserved
        ``mnemos:*`` subtype raises BEFORE any write (the store count is
        unchanged), mirroring the vesma_add channel."""
        before = manager.sqlite.count()
        with pytest.raises(TagContractError):
            sdk.remember(
                "note body",
                PROJECT,
                AGENT,
                tags=[f"project:{PROJECT}", f"agent:{AGENT}", "mnemos:bogus-subtype"],
            )
        assert manager.sqlite.count() == before, "rejected tags must not write"

    def test_valid_tags_pass_through(self, sdk: VesmaSDK, manager: MemoryManager) -> None:
        memory = sdk.remember(
            "note body",
            PROJECT,
            AGENT,
            tags=[f"project:{PROJECT}", f"agent:{AGENT}", "mnemos:learning"],
        )
        assert "mnemos:learning" in memory.tags


# ── recall → search + ISSUANCE SCAN (F1) ──────────────────────────────────────


class TestRecall:
    def test_delegates_to_search(self, sdk: VesmaSDK, manager: MemoryManager) -> None:
        calls: list[dict[str, Any]] = []
        original = manager.search

        def _spy(query: str, **kw: Any) -> list[Any]:
            calls.append({"query": query, **kw})
            return original(query, **kw)

        manager.search = _spy  # type: ignore[method-assign]

        sdk.remember("recall target body about quokka-sdk", PROJECT, AGENT)
        results = sdk.recall("quokka-sdk", PROJECT, limit=3)

        assert isinstance(results, list)
        assert len(calls) == 1
        assert calls[0]["query"] == "quokka-sdk"
        assert calls[0]["project"] == PROJECT
        assert calls[0]["limit"] == 3, "kw passes through to search"


class TestRecallIssuanceScan:
    """F1 (W3 review): the SDK returns SCANNED items, never stored rows."""

    def _published_secret_row(self, mgr: MemoryManager) -> None:
        """A published row carrying a planted fake secret.

        ADR-0019 Phase A + N1: neither the publish stage nor a
        direct-seed create can mint this row anymore (danger gate), so
        it is planted with a store-level status flip — the legacy /
        introduced-by-processing residual the recall issuance scan
        guards."""
        memory = mgr.add(
            MemoryCreate(
                content=f"deploy note with api key {FAKE_AWS_KEY} inline",
                tags=[f"project:{PROJECT}", f"agent:{AGENT}", "mnemos:learning"],
                status=MemoryStatus.RAW,
            ),
            project=PROJECT,
            agent=AGENT,
        )
        mgr.sqlite.update_status(memory.id, MemoryStatus.PUBLISHED)
        # Recall in this fixture is vector-driven (the content carries no
        # "quokka" lexical marker) — restore the embed the store-level
        # flip skipped, mirroring what add() does for clean publications.
        mgr.vectors.upsert(
            memory.id,
            mgr.embed_for(memory),
            {"project": memory.project, "agent": memory.agent},
        )

    def test_secret_in_recalled_row_masked_via_sdk(
        self, sdk: VesmaSDK, manager: MemoryManager
    ) -> None:
        self._published_secret_row(manager)

        items = sdk.recall("quokka", PROJECT)

        assert items, "the row must be recalled"
        for item in items:
            assert FAKE_AWS_KEY not in item["content"], "raw secret leaked via SDK content"
            assert FAKE_AWS_KEY not in item["title"], "raw secret leaked via SDK title"
        assert any("<REDACTED:aws-key>" in i["content"] for i in items)
        assert any("<REDACTED:aws-key>" in i["title"] for i in items)
        # 2 = content span + title span (auto_title derives from the first
        # line, which carries the key — the P1-b F1 both-fields semantics).
        assert any(i["redactions"] == 2 for i in items)
        assert any(i.get("redacted_patterns") == {"aws-key": 2} for i in items)

    def test_refuse_mode_drops_the_item(self, refuse_manager: MemoryManager) -> None:
        sdk = VesmaSDK(manager=refuse_manager)
        self._published_secret_row(refuse_manager)

        items = sdk.recall("quokka", PROJECT)

        assert items == [], "refuse mode must drop the secret-carrying item entirely"

    def test_clean_row_carries_zero_redactions(self, sdk: VesmaSDK, manager: MemoryManager) -> None:
        data = MemoryCreate(
            content="clean deploy note about quokka-plain rotation",
            tags=[f"project:{PROJECT}", f"agent:{AGENT}", "mnemos:learning"],
            status=MemoryStatus.PUBLISHED,
        )
        manager.add(data, project=PROJECT, agent=AGENT)

        items = sdk.recall("quokka-plain", PROJECT)

        assert items, "the row must be recalled"
        assert items[0]["redactions"] == 0
        assert "redacted_patterns" not in items[0]
        assert "quokka-plain" in items[0]["content"]


# ── forget → get + delete (project-guarded) ──────────────────────────────────


class TestForget:
    def test_own_project_deletes(self, sdk: VesmaSDK, manager: MemoryManager) -> None:
        memory = sdk.remember("forget me", PROJECT, AGENT)
        assert sdk.forget(memory.id, PROJECT) is True
        assert manager.get(memory.id) is None

    def test_unknown_id_is_false(self, sdk: VesmaSDK) -> None:
        assert sdk.forget("no-such-id", PROJECT) is False

    def test_cross_project_denied(self, sdk: VesmaSDK, manager: MemoryManager) -> None:
        memory = sdk.remember("other project memory", "other-proj", AGENT)
        deleted: list[str] = []
        original = manager.delete

        def _spy(memory_id: str) -> bool:
            deleted.append(memory_id)
            return original(memory_id)

        manager.delete = _spy  # type: ignore[method-assign]

        with pytest.raises(ValueError, match="different project"):
            sdk.forget(memory.id, PROJECT)

        assert deleted == [], "delete must not run for another project's memory"
        assert manager.get(memory.id) is not None, "memory must survive"


# ── stats → stats (project slice is presentation) ─────────────────────────────


class TestStats:
    def test_global_verbatim(self, sdk: VesmaSDK, manager: MemoryManager) -> None:
        envelope = manager.stats()
        assert sdk.stats() == envelope

    def test_project_slice_keys(self, sdk: VesmaSDK, manager: MemoryManager) -> None:
        sdk.remember("counted", PROJECT, AGENT)
        result = sdk.stats(PROJECT)
        assert result["project"] == PROJECT
        assert result["project_total"] == 1
        assert result["status"] == "ok", "the full manager envelope is preserved"


# ── assemble_context → assemble_context ───────────────────────────────────────


class TestAssembleContext:
    def test_delegates_with_passthrough(self, sdk: VesmaSDK, manager: MemoryManager) -> None:
        memory = sdk.remember("assembled body quokka-asm", PROJECT, AGENT)
        manager.publish(memory.id, skip_quality_check=True)

        calls: list[dict[str, Any]] = []
        original = manager.assemble_context

        def _spy(**kw: Any) -> dict[str, Any]:
            calls.append(kw)
            return original(**kw)

        manager.assemble_context = _spy  # type: ignore[method-assign]

        result = sdk.assemble_context(SESSION, PROJECT, query="quokka-asm", budget=512)

        assert result["session"] == SESSION
        assert result["project"] == PROJECT
        assert calls == [
            {
                "session": SESSION,
                "project": PROJECT,
                "query": "quokka-asm",
                "budget": 512,
            }
        ]


# ── rewrite → context_rewrite ─────────────────────────────────────────────────


class TestRewrite:
    def test_delegates_to_context_rewrite(self, sdk: VesmaSDK, manager: MemoryManager) -> None:
        calls: list[dict[str, Any]] = []
        original = manager.context_rewrite

        def _spy(**kw: Any) -> dict[str, Any]:
            calls.append(kw)
            return original(**kw)

        manager.context_rewrite = _spy  # type: ignore[method-assign]

        result = sdk.rewrite("original block body", PROJECT, AGENT, SESSION, diff="was->becomes")

        assert result["status"] in ("stored", "deduplicated")
        assert calls == [
            {
                "content": "original block body",
                "project": PROJECT,
                "agent": AGENT,
                "session": SESSION,
                "diff": "was->becomes",
            }
        ]
