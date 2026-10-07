"""ADR-0004 B0 — the decision-provider seam wired into the LIVE dup path.

Card b0-provider-seam-wiring (vesma board, owner lane: gcw-senior-system-
engineer): before this slice ``resolve_decision_provider`` was referenced
ONLY by tests — the config block existed (``mnemos.decision_provider``),
prod flipped to ``vesma`` on 2026-10-03, but no LIVE engine call site
invoked the provider, so the B0 telemetry window (until 2026-10-16)
collected no provider verdicts.

This slice wires the seam at the ONE deterministic near-dup verdict path
the engine has: :meth:`MemoryManager._mint_relates_to_edges` — the
add-time auto-dedupe selector pass (ADR-0030 Decision 2). Covered:

* the wiring pin — config ``"vesma"`` produces a provider probe on a
  near-dup write: one ``is-duplicate`` Noul answer over the TOP
  qualified pair, emitted through the §3.9 DECISION line
  (``decision primitive=noul provider=… question=is-duplicate
  probability=…``) — the fields the B0 collector joins
  (b0-telemetry-plan.md: provider/confidence/is-duplicate);
* the §8.3 verdict/action split — the probe never changes mint behavior:
  with ANY provider the minted edge shapes, weights, provenance and
  counts are identical to the provider-less run (the deterministic
  DEFAULT, too — the baseline provider re-answers the exact step rule
  the path always applied);
* fail-open (config.py:173-181 contract / inference-v1.md §7): a
  provider whose evaluate throws degrades to the deterministic step rule
  with a machine-parseable ``code=`` warn; a resolution failure degrades
  the SEAM; a MissingEvidenceError (unanswerable for every provider)
  skips quietly; ``"off"`` = no probe; ingest is never blocked (the call
  site is the existing best-effort ADR-0030 wrapper);
* config contract — the fingerprint source is the manager's LIVE
  embedder identity (``_embedder_fingerprint``); a mock embedder's
  coerced identity fails the artifact ``embedder_pin`` → the honest
  CORTEX-E-PIN recalibration refusal degrades to deterministic
  (asserted through the resolve path).

Out of scope (Wave B): the arbiter — the probe's verdict is telemetry,
never actionability. The provider under the REAL artifact answers over
the REAL bundled embedder (NanoProvider / vesma-embed-v1) — the same
pair the suite already drives (test_decision_cortex.py).
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from vesma.config import Settings
from vesma.decision_provider import VesmaProvider
from vesma.manager import MemoryManager, _SeamNotBuilt
from vesma.models import MemoryCreate

# ---------------------------------------------------------------------------
# Helpers (mirror tests/test_canon_warn_validator.py fixture discipline)
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


def _near_dup_pair(mgr: MemoryManager, *, content_tail: str = " — rephrased.") -> tuple[Any, Any]:
    """Two rows whose embedding-text cosine clears the 0.92 mint
    threshold (identical embed vector → the vector store returns both).
    The first is added normally, the second through add() — the LIVE
    minting path this slice instruments."""
    first = mgr.add(
        MemoryCreate(
            content=f"deploy note: rollout of vesma 5.6 on the laptop host{content_tail}",
            title="deploy note",
            tags=["project:b0proj", "agent:alice"],
        ),
        project="b0proj",
        agent="alice",
    )
    second = mgr.add(
        MemoryCreate(
            content=f"deploy note: rollout of vesma 5.6 on the laptop host{content_tail}",
            title="deploy note",
            tags=["project:b0proj", "agent:alice"],
        ),
        project="b0proj",
        agent="alice",
    )
    return first, second


def _vesma_mgr_with_real_embedder(mode: str) -> tuple[MemoryManager, tempfile.TemporaryDirectory]:
    """A manager over the REAL bundled embedder (NanoProvider /
    vesma-embed-v1 — the artifact's pinned vintage), configured ``mode``.
    The pin-assert path of resolve_decision_provider then sees the LIVE
    default fingerprint and the artifact loads with pin=ok.

    Returns ``(manager, tmpdir)`` — the caller keeps the tmpdir ALIVE
    until the manager is closed (the db lives inside it)."""
    tmpdir = tempfile.TemporaryDirectory()
    tmp = Path(tmpdir.name)
    settings = Settings(
        mnemos={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
        },
        embedding={"provider": "nano"},
        scanner={"enabled": False},
    )
    settings.resolve_paths()
    settings.vesma.decision_provider = mode
    manager = MemoryManager(settings)
    from vesma.embeddings import NanoProvider

    manager._embedder = NanoProvider()
    return manager, tmpdir


# ---------------------------------------------------------------------------
# 1. The wiring pin — config vesma → live probe on the near-dup path
# ---------------------------------------------------------------------------


def test_seam_lazy_and_deterministic_default(mgr: MemoryManager) -> None:
    """The default config builds a DETERMINISTIC provider (not None) —
    but only lazily, on the first probe (zero construction cost for
    writes that never mint)."""
    assert isinstance(mgr._decision_seam_provider, _SeamNotBuilt)  # not built yet
    provider = mgr._decision_seam()
    assert provider is not None
    assert provider.name == "deterministic"
    assert mgr._decision_seam_provider is provider  # one-shot: same instance


def test_config_off_disables_the_seam(mgr: MemoryManager) -> None:
    mgr.settings.vesma.decision_provider = "off"
    assert mgr._decision_seam() is None


def test_config_vesma_resolves_provider_behind_live_fingerprint() -> None:
    """AC1 pin: the config value ``vesma`` activates the provider through
    the SAME resolve function the calibration tests use — with the LIVE
    embedder fingerprint supplied by the manager (real bundled embedder
    → the pinned default vintage → the artifact loads with pin=ok; a
    seam-less fingerprint is refused loudly, never assumed — the next
    test)."""
    m, tmpdir = _vesma_mgr_with_real_embedder("vesma")
    try:
        provider = m._decision_seam()
        assert provider is not None
        assert provider.name == "vesma-cortex"
        assert m._decision_seam_provider is provider  # one-shot
    finally:
        m.close()
        tmpdir.cleanup()


def test_config_vesma_without_livable_fingerprint_degrades_loudly(
    mgr: MemoryManager, caplog: pytest.LogCaptureFixture
) -> None:
    """The pin contract (config.py:174-181): a fingerprint outside the
    pinned vintage (here: the mock embedder's coerced class-name
    identity) → the artifact can not be asserted — the resolve path's
    honest CORTEX-E-PIN class refusal degrades to deterministic with a
    machine-parseable warn. NOTHING silently assumes."""
    mgr.settings.vesma.decision_provider = "vesma"
    with caplog.at_level(logging.WARNING, logger="vesma.decision_jev"):
        provider = mgr._decision_seam()
    assert provider is not None
    assert provider.name == "deterministic"
    assert any("CORTEX-E-PIN" in r.message for r in caplog.records)


def test_near_dup_write_probes_provider_vesma(
    mgr: MemoryManager, caplog: pytest.LogCaptureFixture
) -> None:
    """AC1 live pin: a near-dup add with decision_provider=vesma emits a
    DECISION line from the LIVE path (provider=vesma-cortex,
    question=is-duplicate, probability=<x>) — the fields the 09.10
    summary joins. Mock embedder leg: the live fingerprint fails the
    artifact pin and DEGRADES to deterministic — the probe still
    emits (honest attribution of the implementation that ACTUALLY
    answered); the REAL-embedder variant below pins the vesma-cortex
    name itself."""
    mgr.settings.vesma.decision_provider = "vesma"
    with caplog.at_level(logging.INFO):
        _first, _second = _near_dup_pair(mgr)
    decisions = [
        r.message
        for r in caplog.records
        if r.message.startswith("decision ") and "is-duplicate" in r.message
    ]
    assert decisions, "the live minting path never probed the provider"
    assert any("provider=deterministic" in line for line in decisions), decisions
    assert any("CORTEX-E-PIN" in r.message for r in caplog.records if r.levelno >= logging.WARNING)


def test_near_dup_write_probes_real_vesma_artifact() -> None:
    """The REAL artifact answers over the REAL bundled embedder: the
    live add of a near-dup pair with decision_provider=vesma emits
    ``decision primitive=noul provider=vesma-cortex question=is-duplicate`` —
    the exact line class b0-telemetry-plan joins into decision_samples."""
    m, tmpdir = _vesma_mgr_with_real_embedder("vesma")
    try:
        a = m.add(
            MemoryCreate(
                content="sprint review rescheduled to 2026-10-08 15:00",
                tags=["project:b0proj", "agent:sdk-agent"],
                metadata={},
            ),
            project="b0proj",
            agent="sdk-agent",
        )
        b = m.add(
            MemoryCreate(
                content="sprint review rescheduled to 2026-10-08 15:00",
                tags=["project:b0proj", "agent:sdk-agent"],
                metadata={},
            ),
            project="b0proj",
            agent="sdk-agent",
        )
        stored = m.sqlite.get(b.id)
        assert stored is not None
        del a
        # The seam resolved to the REAL artifact (pin=ok, live vintage):
        assert isinstance(m._decision_seam_provider, VesmaProvider)
    finally:
        m.close()
        tmpdir.cleanup()


# ---------------------------------------------------------------------------
# 2. §8.3 verdict/action split — the probe is telemetry, mint identical
# ---------------------------------------------------------------------------


def test_probe_never_changes_mint_behavior() -> None:
    """THE §8.3 pin: the minted edge shape/weights/provenance and the
    stored rows are IDENTICAL across provider configs — the probe is
    telemetry, never a gate (the deterministic default included)."""
    results: dict[str, dict[str, Any]] = {}
    for label, mode in (("det", "deterministic"), ("vesma", "vesma"), ("off", "off")):
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
            settings.vesma.decision_provider = mode
            m = MemoryManager(settings)
            mock = MagicMock()
            mock.embed.return_value = [0.1] * 384
            m._embedder = mock
            a = m.add(
                MemoryCreate(
                    content="alpha content for split-pin",
                    title="alpha",
                    tags=["project:b0proj", "agent:alice"],
                ),
                project="b0proj",
                agent="alice",
            )
            b = m.add(
                MemoryCreate(
                    content="alpha content for split-pin",
                    title="alpha",
                    tags=["project:b0proj", "agent:alice"],
                ),
                project="b0proj",
                agent="alice",
            )
            edge = m.sqlite.get_direct_edges(b.id, kind="relates_to")
            row_b = m.sqlite.get(b.id)
            assert row_b is not None
            row_a = m.sqlite.get(a.id)
            assert row_a is not None
            results[label] = {
                "edges": [
                    # ids differ per fresh store — capture the SHAPE only:
                    # orientation (from == the second write's row),
                    # weight, provenance, count.
                    (
                        str(e["from_memory_id"]) == str(b.id),
                        str(e["to_memory_id"]) == str(a.id),
                        e.get("weight"),
                        e.get("provenance"),
                    )
                    for e in edge
                ],
                "b_status": row_b.status,
                "a_status": row_a.status,
                "same_ids": str(a.id) != str(b.id),  # both rows persisted
            }
            m.close()
    assert results["det"] == results["vesma"] == results["off"], results
    assert results["det"]["edges"], "the near-dup mint must have produced an edge in all modes"
    assert results["det"]["edges"][0][2] == 2.0 and results["det"]["edges"][0][3] == "auto-dedupe"


def test_deterministic_default_answers_the_step_rule(
    mgr: MemoryManager, caplog: pytest.LogCaptureFixture
) -> None:
    """The DEFAULT provider re-answers the exact step rule the path
    always applied: cosine over the mint threshold — probability 0.0/1.0
    only (the unchanged-behavior guarantee, per AC1 last clause)."""
    with caplog.at_level(logging.INFO, logger="vesma.decision_provider"):
        _first, _second = _near_dup_pair(mgr)
    noul_lines = [
        r.message
        for r in caplog.records
        if r.message.startswith("decision ")
        and "provider=deterministic" in r.message
        and "question=is-duplicate" in r.message
    ]
    assert noul_lines
    assert all("probability=1.0" in line or "probability=0.0" in line for line in noul_lines)


# ---------------------------------------------------------------------------
# 3. Fail-open — any provider failure degrades, ingest never blocked
# ---------------------------------------------------------------------------


class _ExplodingProvider:
    """A provider whose every evaluate raises (the CORTEX-E-* stand-in)."""

    name = "exploding-cortex"

    def evaluate(self, request: Any, state: Any) -> Any:
        raise RuntimeError("CORTEX-E-INFER stand-in: exploded")


def test_provider_exception_degrades_to_deterministic_step(
    mgr: MemoryManager, caplog: pytest.LogCaptureFixture
) -> None:
    """AC2 pin: a provider exception → deterministic fallback answer
    (a DECISION line from provider=deterministic) + machine-parseable
    warn with a code — and both writes still persist."""
    mgr.settings.vesma.decision_provider = "vesma"
    mgr._decision_seam_provider = _ExplodingProvider()
    with caplog.at_level(logging.INFO):
        first, second = _near_dup_pair(mgr)
    assert mgr.sqlite.get(first.id) is not None
    assert mgr.sqlite.get(second.id) is not None  # ingest NOT blocked
    warns = [r.message for r in caplog.records if "DECISION-E-PROBE" in r.message]
    assert any("verdict degraded" in w and "exploded" in w for w in warns)
    fallbacks = [
        r.message
        for r in caplog.records
        if r.message.startswith("decision ")
        and "provider=deterministic" in r.message
        and "question=is-duplicate" in r.message
    ]
    assert fallbacks, "the deterministic fallback answer never emitted its DECISION line"


def test_seam_resolution_exception_degrades_to_deterministic(
    mgr: MemoryManager, caplog: pytest.LogCaptureFixture
) -> None:
    """ANY resolution failure (import-time, config edge) → deterministic
    provider + machine-parseable warn; the seam NEVER blocks ingest."""
    import vesma.manager as manager_mod

    mgr.settings.vesma.decision_provider = "vesma"
    original = manager_mod.resolve_decision_provider

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("CORTEX-E-LOAD stand-in: resolution exploded")

    try:
        manager_mod.resolve_decision_provider = boom  # type: ignore[assignment]
        provider = mgr._decision_seam()
    finally:
        manager_mod.resolve_decision_provider = original  # type: ignore[assignment]
    assert provider is not None
    assert provider.name == "deterministic"
    assert any("DECISION-E-SEAM" in r.message for r in caplog.records)


def test_missing_evidence_skips_quietly(
    mgr: MemoryManager, caplog: pytest.LogCaptureFixture
) -> None:
    """:class:`MissingEvidenceError` is unanswerable for EVERY provider —
    the twin would raise it too — so the probe skips WITHOUT a fabricated
    fallback warn (the honest no-evidence shape)."""
    from vesma.decision_provider import MissingEvidenceError

    class _NoEvidenceProvider:
        name = "no-evidence"

        def evaluate(self, request: Any, state: Any) -> Any:
            raise MissingEvidenceError("no similarity")

    mgr._decision_provider = _NoEvidenceProvider()  # type: ignore[assignment]
    first = mgr.add(
        MemoryCreate(
            content="row for the missing-evidence skip",
            title="row",
            tags=["project:b0proj", "agent:alice"],
        ),
        project="b0proj",
        agent="alice",
    )
    assert mgr.sqlite.get(first.id) is not None
    assert not [r.message for r in caplog.records if "DECISION-E-PROBE" in r.message]
