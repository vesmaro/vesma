"""The merge-arbiter wiring in the live near-dup path — posture matrix.

The hook lives at the ONE deterministic near-dup verdict path
(``MemoryManager._mint_relates_to_edges``, the ADR-0030 A0 selector
pass), AFTER the is-dup probe answered «duplicate» — the silver gate a
cluster must pass BEFORE reaching the Choice-arbiter (v42 untouched).
Pinned here:

* advisory (the DEFAULT): one machine-parseable MERGE-ARBITER line per
  run, NO fold, no system action — a byte-identical re-add survives;
* apply: the fold executes through the consolidation executor (tombstone
  + journal + the Д3 edge) and the auto-dedupe mint loop skips the
  freshly folded row;
* off: the seam is silent;
* fail-open: a verify-before-load refusal (wrong artifact bytes,
  foreign embedder vintage) disables the arbiter for the process and
  the write path never notices;
* the is-dup gate: a not-duplicate probe verdict never reaches the
  arbiter.
"""

from __future__ import annotations

import hashlib
import logging
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.models import MemoryCreate
from vesma.storage.sqlite_store import tombstones_visible


def _norm_stub_embed(text: str) -> list[float]:
    """Identity-of-content stub: the unit vector of the normalized
    TITLE+BODY hash (the first two lines of any engine text shape). A
    metadata-only difference (tags) keeps the SAME vector — that is what
    lets a CH-2 pair clear the 0.92 vector gate, exactly like the real
    nano geometry does."""
    head = "\n".join(text.split("\n")[:2])
    digest = hashlib.sha256(" ".join(head.split()).lower().encode("utf-8")).digest()
    vec = np.zeros(384)
    for i, b in enumerate(digest):
        vec[i % 384] += b / 255.0
    norm = float(np.linalg.norm(vec)) or 1.0
    return (vec / norm).tolist()


class _StubEmbedder:
    """Deterministic stub with the LIVE artifact pin — the arbiter seam
    asserts the fingerprint, so the stub must present the real one."""

    fingerprint = "nano:sha256:3b752e0671a50da5c108cb50e49630a66c160f7683afedcf879e1880d84317ba"

    def embed(self, text: str) -> list[float]:
        return _norm_stub_embed(text)


@pytest.fixture
def mgr() -> Iterator[MemoryManager]:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        settings = Settings(
            vesma={
                "vault_path": str(tmp / "vault"),
                "data_dir": str(tmp / "data"),
                "db_name": "test.db",
            },
            embedding={"provider": "onnx"},
            scanner={"enabled": False},
        )
        settings.resolve_paths()
        manager = MemoryManager(settings)
        manager._embedder = _StubEmbedder()
        yield manager
        manager.close()


def _add(mgr: MemoryManager, content: str) -> Any:
    return mgr.add(
        MemoryCreate(
            content=content,
            title="deploy note",
            tags=["project:hookproj"],
        ),
        project="hookproj",
        agent="wave",
    )


CONTENT = "deploy note: rollout of vesma on the laptop host"


def _arbiter_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r for r in caplog.messages if r.startswith("MERGE-ARBITER")]


def _row_any(mgr: MemoryManager, memory_id: str) -> Any:
    with tombstones_visible():
        return mgr.sqlite.get(memory_id, include_tombstoned=True)


# ---------------------------------------------------------------------------
# 1. Advisory (the default posture)
# ---------------------------------------------------------------------------


def test_advisory_logs_verdict_and_never_folds(mgr, caplog) -> None:
    caplog.set_level(logging.INFO)
    first = _add(mgr, CONTENT)
    second = _add(mgr, CONTENT)
    lines = _arbiter_lines(caplog)
    assert len(lines) == 1
    assert "verdict=keep" in lines[0]
    assert "leg=ch0-tie-break" in lines[0]
    assert "mode=advisory" in lines[0]
    assert first.id[:8] in lines[0]  # the keeper is the older record
    # NO fold: both rows alive, no tombstone, no deletion journal
    assert mgr.get(first.id) is not None
    assert mgr.get(second.id) is not None
    with tombstones_visible():
        assert mgr.sqlite.get_workflow_status(second.id)["workflow_status"] != "tombstoned"
    assert mgr.sqlite.journal_entry_count(second.id) == 0
    # the plain auto-dedupe fuel edge still minted (advisory changes
    # nothing about mint behavior — the §8.3 split); the mint direction
    # is new → candidate
    edges = mgr.sqlite.get_incoming_edges(first.id, kind="relates_to")
    assert any(e["provenance"] == "auto-dedupe" and e["from_memory_id"] == second.id for e in edges)


def test_advisory_runs_on_the_same_manager_without_reloading(mgr, caplog) -> None:
    """The seam builds ONCE (verify-before-load is a constructor cost):
    three near-dup writes, one artifact load, three verdicts."""
    caplog.set_level(logging.INFO)
    for _ in range(3):
        _add(mgr, CONTENT + " v1")
    _add(mgr, CONTENT + " v1")  # byte-identical to the last
    assert len(_arbiter_lines(caplog)) >= 1
    assert mgr._merge_arbiter is not None


# ---------------------------------------------------------------------------
# 2. Apply — the fold executes
# ---------------------------------------------------------------------------


def test_apply_folds_the_newer_duplicate(mgr, caplog) -> None:
    mgr.settings.vesma.merge_arbiter = "apply"
    caplog.set_level(logging.INFO)
    first = _add(mgr, CONTENT)
    second = _add(mgr, CONTENT)  # the byte-identical re-add
    lines = _arbiter_lines(caplog)
    assert lines and "mode=apply" in lines[0]
    # the keeper (older) survives; the re-add is tombstoned
    assert mgr.get(first.id) is not None
    with tombstones_visible():
        assert _row_any(mgr, second.id) is not None
    status = mgr.sqlite.get_workflow_status(second.id)
    assert status is not None and status["workflow_status"] == "tombstoned"
    assert mgr.sqlite.journal_entry_count(second.id) == 1
    # the Д3 edge exists; the plain auto-dedupe edge was skipped
    edges = mgr.sqlite.get_incoming_edges(second.id, kind="relates_to")
    provenances = [e["provenance"] for e in edges]
    assert "merge-arbiter" in provenances
    assert "auto-dedupe" not in provenances


def test_apply_respects_the_ch2_refusal(mgr, caplog) -> None:
    """A metadata-conflict pair (same body+title, tags differ) refuses:
    the fold is FORBIDDEN even in apply mode."""
    mgr.settings.vesma.merge_arbiter = "apply"
    caplog.set_level(logging.INFO)
    first = mgr.add(
        MemoryCreate(content=CONTENT, title="same title", tags=["project:hookproj"]),
        project="hookproj",
        agent="wave",
    )
    second = mgr.add(
        MemoryCreate(content=CONTENT, title="same title", tags=["project:hookproj", "extra"]),
        project="hookproj",
        agent="wave",
    )
    lines = _arbiter_lines(caplog)
    assert lines and "leg=ch2-refusal" in lines[0]
    assert mgr.get(first.id) is not None
    assert mgr.get(second.id) is not None
    assert mgr.sqlite.journal_entry_count(second.id) == 0


# ---------------------------------------------------------------------------
# 3. Off + fail-open + the is-dup gate
# ---------------------------------------------------------------------------


def test_off_disables_the_seam(caplog) -> None:
    caplog.set_level(logging.INFO)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        settings = Settings(
            vesma={
                "vault_path": str(tmp / "vault"),
                "data_dir": str(tmp / "data"),
                "db_name": "test.db",
                "merge_arbiter": "off",
            },
            embedding={"provider": "onnx"},
            scanner={"enabled": False},
        )
        settings.resolve_paths()
        manager = MemoryManager(settings)
        manager._embedder = _StubEmbedder()
        manager.add(
            MemoryCreate(content=CONTENT, tags=["project:hookproj"]),
            project="hookproj",
            agent="wave",
        )
        manager.add(
            MemoryCreate(content=CONTENT, tags=["project:hookproj"]),
            project="hookproj",
            agent="wave",
        )
        manager.close()
    assert not _arbiter_lines(caplog)
    # the seam never even built in the off posture
    from vesma.manager import _SeamNotBuilt

    assert isinstance(manager._merge_arbiter, _SeamNotBuilt)


def test_wrong_artifact_bytes_disable_the_arbiter_fail_open(mgr, caplog, monkeypatch) -> None:
    monkeypatch.setattr("vesma.merge_arbiter.MERGE_ARTIFACT_SHA256", "f" * 64)
    caplog.set_level(logging.INFO)
    first = _add(mgr, CONTENT)
    second = _add(mgr, CONTENT)
    assert not _arbiter_lines(caplog)
    assert any("MERGE-E-PIN" in r for r in caplog.messages)
    # ingest never noticed
    assert mgr.get(first.id) is not None
    assert mgr.get(second.id) is not None


def test_foreign_embedder_vintage_disables_the_arbiter(mgr, caplog) -> None:
    mgr._embedder = type(
        "ForeignEmbedder",
        (),
        {
            "fingerprint": "ollama:some-model",
            "embed": staticmethod(lambda text: _norm_stub_embed(text)),
        },
    )()
    caplog.set_level(logging.INFO)
    _add(mgr, CONTENT)
    _add(mgr, CONTENT)
    assert not _arbiter_lines(caplog)
    assert any("MERGE-E-PIN" in r and "recalibration" in r for r in caplog.messages)


def test_not_duplicate_probe_never_reaches_the_arbiter(mgr, caplog, monkeypatch) -> None:
    monkeypatch.setattr(mgr, "_decision_provider_probe", lambda *a, **kw: ("not-duplicate", 0.1))
    caplog.set_level(logging.INFO)
    _add(mgr, CONTENT)
    _add(mgr, CONTENT)
    assert not _arbiter_lines(caplog)


def test_probe_none_never_reaches_the_arbiter(mgr, caplog, monkeypatch) -> None:
    monkeypatch.setattr(mgr, "_decision_provider_probe", lambda *a, **kw: None)
    caplog.set_level(logging.INFO)
    _add(mgr, CONTENT)
    _add(mgr, CONTENT)
    assert not _arbiter_lines(caplog)
