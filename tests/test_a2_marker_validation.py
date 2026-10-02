"""A2 (ArchCom 2026-08-27) — strong-form CCR marker validation.

Committee decision ``archcom-2026-08-27-deferrals-triage``: A2 is the W3
automation gate and must be existence + provenance, NOT existence-only
(same-project seeding survives an existence check). These tests verify:

  - the issuer ledger recorded at store time (compress paths thread the
    caller's agent/session);
  - ``MemoryManager.validate_marker`` — existence (project-scoped, after
    A1), ``original_chars`` integrity, provenance against the trusted
    issuer context;
  - strict mode on ``retrieve_content`` (MCP + REST surfaces): marker-
    shaped requests that fail ANY check are refused with no content
    (fail-closed), plain hash-only retrieves are unaffected;
  - migration round-trips: legacy rows with NULL issuers are
    unverifiable and strict mode refuses them with the distinct
    ``unverifiable legacy marker`` reason.

Accepted residual (ADR-0018 residual register): same-project seeding by
a trusted harness with compress access — a single-operator threat-model
acceptance, not a bug these tests chase.
"""

from __future__ import annotations

import logging
import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import vesmaro.mcp_server as mcp_mod
from vesmaro.api import main as api_main
from vesmaro.api.main import app, lifespan
from vesmaro.ccr import content_hash, parse_marker
from vesmaro.config import Settings
from vesmaro.manager import MemoryManager
from vesmaro.models import MemoryCreate, MemorySource, MemoryStatus
from vesmaro.storage.sqlite_store import SQLiteStore

PROJECT = "a2-proj"
OTHER_PROJECT = "a2-other"
AGENT = "a2-agent"
SESSION = "a2-sess-1"
OTHER_SESSION = "a2-sess-2"

# Benign (secret-scanner-clean) content well above min_size_chars=100.
CONTENT = (
    "Deployment runbook for the unobtanium gateway service.\n"
    "The service listens on an internal port and proxies requests to the\n"
    "upstream cluster pool. Rolling restarts drain connections over thirty\n"
    "seconds before the old process exits. Health probes hit the local\n"
    "status endpoint every ten seconds and the orchestrator marks a node\n"
    "unhealthy after three consecutive failures.\n"
) * 3


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _settings(tmp: Path, *, validate_markers: bool = False) -> Settings:
    settings = Settings(
        mnemos={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
        },
        scanner={"enabled": False},
        ccr={
            "min_size_chars": 100,
            "max_entries": 100,
            "ttl_days": 1,
            "validate_markers": validate_markers,
        },
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
    """Manager with the strict knob OFF (per-call opt-in only)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = _manager(_settings(Path(tmpdir)))
        yield mgr
        mgr.close()


@pytest.fixture
def strict_manager() -> Iterator[MemoryManager]:
    """Manager with ``ccr.validate_markers=True`` (W3 automation config)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = _manager(_settings(Path(tmpdir), validate_markers=True))
        yield mgr
        mgr.close()


@pytest.fixture
def mcp_wired(manager: MemoryManager) -> Iterator[MemoryManager]:
    """Inject ``manager`` into the MCP server module global."""
    mcp_mod._manager = manager
    yield manager
    mcp_mod._manager = None


@pytest.fixture
def rest_client(manager: MemoryManager) -> Iterator[TestClient]:
    """TestClient wired to ``manager`` (knob OFF, per-call opt-in)."""
    api_main._manager = manager
    test_app = FastAPI(title="Mnemos-A2-Test", version="0.1.0", lifespan=lifespan)
    for route in app.routes:
        test_app.routes.append(route)
    with TestClient(test_app) as tc:
        yield tc
    api_main._manager = None


@pytest.fixture
def strict_rest_client(strict_manager: MemoryManager) -> Iterator[TestClient]:
    """TestClient wired to ``strict_manager`` (knob ON)."""
    api_main._manager = strict_manager
    test_app = FastAPI(title="Mnemos-A2-Strict-Test", version="0.1.0", lifespan=lifespan)
    for route in app.routes:
        test_app.routes.append(route)
    with TestClient(test_app) as tc:
        yield tc
    api_main._manager = None


def _mint(
    mgr: MemoryManager,
    *,
    agent: str | None = AGENT,
    session: str | None = SESSION,
    project: str = PROJECT,
    text: str = CONTENT,
) -> dict[str, Any]:
    """Compress ``text`` in the given issuer context; return marker fields."""
    result = mgr.compress_content(text, project=project, agent=agent, session=session)
    assert result["cached"] is True
    parsed = parse_marker(result["compressed_text"])
    assert parsed is not None
    return parsed


# ── validate_marker: all three checks ────────────────────────────────────────


def test_valid_marker_passes_all_checks(manager: MemoryManager) -> None:
    marker = _mint(manager)
    verdict = manager.validate_marker(
        marker["hash"],
        project=PROJECT,
        original_chars=marker["original_chars"],
        trusted_issuers={(AGENT, SESSION)},
    )
    assert verdict == {"valid": True, "reason": None, "check": None}


def test_wrong_project_hash_fails_existence(manager: MemoryManager) -> None:
    marker = _mint(manager)
    verdict = manager.validate_marker(
        marker["hash"],
        project=OTHER_PROJECT,
        original_chars=marker["original_chars"],
        trusted_issuers={(AGENT, SESSION)},
    )
    assert verdict["valid"] is False
    assert verdict["check"] == "existence"
    assert "not in cache" in verdict["reason"]


def test_unknown_hash_fails_existence(manager: MemoryManager) -> None:
    verdict = manager.validate_marker(
        "f" * 64,
        project=PROJECT,
        original_chars=1000,
        trusted_issuers={(AGENT, SESSION)},
    )
    assert verdict["valid"] is False
    assert verdict["check"] == "existence"


def test_missing_project_scope_fails_existence(manager: MemoryManager) -> None:
    """Strict validation REQUIRES a project scope (unscoped would redeem
    against the first-stored copy of any project under the A1 PK)."""
    marker = _mint(manager)
    verdict = manager.validate_marker(
        marker["hash"],
        project=None,
        original_chars=marker["original_chars"],
        trusted_issuers={(AGENT, SESSION)},
    )
    assert verdict["valid"] is False
    assert verdict["check"] == "existence"
    assert "project scope required" in verdict["reason"]


def test_chars_mismatch_fails_integrity(manager: MemoryManager) -> None:
    marker = _mint(manager)
    verdict = manager.validate_marker(
        marker["hash"],
        project=PROJECT,
        original_chars=marker["original_chars"] + 1,
        trusted_issuers={(AGENT, SESSION)},
    )
    assert verdict["valid"] is False
    assert verdict["check"] == "integrity"
    assert "original_chars mismatch" in verdict["reason"]


def test_missing_original_chars_fails_integrity(manager: MemoryManager) -> None:
    """An unverifiable dimension is a FAILED dimension (fail-closed)."""
    marker = _mint(manager)
    verdict = manager.validate_marker(
        marker["hash"],
        project=PROJECT,
        original_chars=None,
        trusted_issuers={(AGENT, SESSION)},
    )
    assert verdict["valid"] is False
    assert verdict["check"] == "integrity"
    assert "original_chars not provided" in verdict["reason"]


def test_wrong_session_fails_provenance(manager: MemoryManager) -> None:
    marker = _mint(manager)
    verdict = manager.validate_marker(
        marker["hash"],
        project=PROJECT,
        original_chars=marker["original_chars"],
        trusted_issuers={(AGENT, OTHER_SESSION)},
    )
    assert verdict["valid"] is False
    assert verdict["check"] == "provenance"
    assert "issuer mismatch" in verdict["reason"]


def test_wrong_agent_fails_provenance(manager: MemoryManager) -> None:
    marker = _mint(manager)
    verdict = manager.validate_marker(
        marker["hash"],
        project=PROJECT,
        original_chars=marker["original_chars"],
        trusted_issuers={("other-agent", SESSION)},
    )
    assert verdict["valid"] is False
    assert verdict["check"] == "provenance"


def test_agent_only_identity_matches_null_issuer_session(manager: MemoryManager) -> None:
    """A spec session of None matches ONLY a NULL issuer session
    (component-wise equality — never a wildcard)."""
    marker = _mint(manager, session=None)
    same = manager.validate_marker(
        marker["hash"],
        project=PROJECT,
        original_chars=marker["original_chars"],
        trusted_issuers={(AGENT, None)},
    )
    assert same["valid"] is True
    crossed = manager.validate_marker(
        marker["hash"],
        project=PROJECT,
        original_chars=marker["original_chars"],
        trusted_issuers={(AGENT, SESSION)},
    )
    assert crossed["valid"] is False
    assert crossed["check"] == "provenance"


def test_trusted_issuer_allowlist(manager: MemoryManager) -> None:
    """The allowlist form of the same predicate: several trusted pairs."""
    marker = _mint(manager)
    verdict = manager.validate_marker(
        marker["hash"],
        project=PROJECT,
        original_chars=marker["original_chars"],
        trusted_issuers={("other-agent", None), (AGENT, SESSION), ("third", "s3")},
    )
    assert verdict["valid"] is True


def test_empty_agent_in_spec_is_rejected(manager: MemoryManager) -> None:
    marker = _mint(manager)
    with pytest.raises(ValueError, match="non-empty slugs"):
        manager.validate_marker(
            marker["hash"],
            project=PROJECT,
            original_chars=marker["original_chars"],
            trusted_issuers={("", None)},
        )


# ── Strict mode on retrieve_content ──────────────────────────────────────────


def test_strict_mode_valid_marker_issues_content(strict_manager: MemoryManager) -> None:
    marker = _mint(strict_manager)
    result = strict_manager.retrieve_content(
        marker["hash"],
        project=PROJECT,
        original_chars=marker["original_chars"],
        agent=AGENT,
        session=SESSION,
    )
    assert result["found"] is True
    assert result.get("refused") is not True
    assert result["original"] == CONTENT


def test_successful_retrieve_never_echoes_issuer(manager: MemoryManager) -> None:
    """N1: the issuer ledger is internal gate data — issuer_agent/
    issuer_session must not appear in ANY successful response (full,
    snippet, strict-validated, or knob-off): echoing them would
    gratuitously disclose session-capability handles on the MCP/REST
    surfaces."""
    marker = _mint(manager)

    full = manager.retrieve_content(marker["hash"], project=PROJECT)
    assert full["found"] is True
    assert "issuer_agent" not in full
    assert "issuer_session" not in full

    snippets = manager.retrieve_content(
        marker["hash"], project=PROJECT, query="runbook", snippet_count=2
    )
    assert snippets["found"] is True
    assert "issuer_agent" not in snippets
    assert "issuer_session" not in snippets

    strict = manager.retrieve_content(
        marker["hash"],
        project=PROJECT,
        validate_marker=True,
        original_chars=marker["original_chars"],
        agent=AGENT,
        session=SESSION,
    )
    assert strict["found"] is True
    assert strict.get("refused") is not True
    assert "issuer_agent" not in strict
    assert "issuer_session" not in strict


def test_strict_mode_refuses_on_each_failure(strict_manager: MemoryManager) -> None:
    """Each failed check → refused shape, reason naming the check, and NO
    content keys in the response (fail-closed)."""
    marker = _mint(strict_manager)
    n = marker["original_chars"]
    cases: list[tuple[str, dict[str, Any], str]] = [
        # existence: wrong project scope
        (
            "existence",
            {"project": OTHER_PROJECT, "agent": AGENT, "session": SESSION},
            "existence",
        ),
        # integrity: marker N does not match the stored original
        (
            "integrity",
            {
                "project": PROJECT,
                "original_chars": n + 5,
                "agent": AGENT,
                "session": SESSION,
            },
            "integrity",
        ),
        # provenance: right agent, wrong session
        (
            "provenance",
            {
                "project": PROJECT,
                "original_chars": n,
                "agent": AGENT,
                "session": OTHER_SESSION,
            },
            "provenance",
        ),
    ]
    for label, kwargs, check in cases:
        result = strict_manager.retrieve_content(marker["hash"], **kwargs)
        assert result.get("refused") is True, label
        assert result["reason"].startswith("marker validation failed:"), label
        assert f": {check}:" in result["reason"], (label, result["reason"])
        # Fail-closed: NO content in the response.
        assert "original" not in result, label
        assert "snippets" not in result, label
        assert result["found"] is (check != "existence"), label


def test_knob_off_hash_only_retrieve_unaffected(manager: MemoryManager) -> None:
    """Knob OFF (default deployments): plain hash-only retrieve keeps the
    full CCR UX — no validation, no F2 closure (review F2 scope: strict
    deployments are automation contexts by design)."""
    marker = _mint(manager)
    result = manager.retrieve_content(marker["hash"], project=PROJECT)
    assert result["found"] is True
    assert result["original"] == CONTENT
    assert result.get("refused") is not True


def test_strict_mode_without_agent_context_refuses(strict_manager: MemoryManager) -> None:
    """Marker-shaped but identity-less: no trusted context to prove
    provenance against → fail-closed refusal."""
    marker = _mint(strict_manager)
    result = strict_manager.retrieve_content(
        marker["hash"],
        project=PROJECT,
        original_chars=marker["original_chars"],
    )
    assert result["refused"] is True
    assert "no trusted issuer context" in result["reason"]
    assert "original" not in result


def test_knob_off_per_call_opt_in_refuses(manager: MemoryManager) -> None:
    """Knob OFF (default): strict is per-call ``validate_marker=True``."""
    marker = _mint(manager)
    result = manager.retrieve_content(
        marker["hash"],
        project=PROJECT,
        validate_marker=True,
        original_chars=marker["original_chars"],
        agent=AGENT,
        session=OTHER_SESSION,
    )
    assert result["refused"] is True
    assert "provenance" in result["reason"]
    assert "original" not in result


def test_knob_on_per_call_opt_out_issues(strict_manager: MemoryManager) -> None:
    """Explicit ``validate_marker=False`` overrides the knob (operator
    escape hatch for a trusted debug redemption)."""
    marker = _mint(strict_manager)
    result = strict_manager.retrieve_content(
        marker["hash"],
        project=PROJECT,
        validate_marker=False,
        original_chars=marker["original_chars"],
        agent=AGENT,
        session=OTHER_SESSION,
    )
    assert result["found"] is True
    assert result.get("refused") is not True
    assert result["original"] == CONTENT


def test_failed_validation_never_bumps_retrieval_count(strict_manager: MemoryManager) -> None:
    """P1-b review F4 semantics: a refused validation must not LRU-pin
    the entry or inflate retrieval stats."""
    marker = _mint(strict_manager)
    for _ in range(3):
        result = strict_manager.retrieve_content(
            marker["hash"],
            project=PROJECT,
            original_chars=marker["original_chars"] + 1,  # integrity fail
            agent=AGENT,
            session=SESSION,
        )
        assert result["refused"] is True
    entry = strict_manager.sqlite.ccr_get(marker["hash"], project=PROJECT, bump=False)
    assert entry is not None
    assert entry["retrieval_count"] == 0


# ── Issuer ledger: first-writer semantics ────────────────────────────────────


def test_first_writer_owns_issuer_columns(manager: MemoryManager) -> None:
    """A2 mirrors the A1 rule: the UPSERT never rewrites the issuer, so
    a session re-compressing identical content receives a marker bound
    to the FIRST issuer. Strict provenance refuses its redemption —
    fail-closed and harmless (the re-compressor already holds the
    content it passed in)."""
    first = _mint(manager, agent=AGENT, session=SESSION)
    second = manager.compress_content(
        CONTENT, project=PROJECT, agent="second-agent", session="second-sess"
    )
    assert second["hash"] == first["hash"]
    entry = manager.sqlite.ccr_get(first["hash"], project=PROJECT, bump=False)
    assert entry is not None
    assert entry["issuer_agent"] == AGENT
    assert entry["issuer_session"] == SESSION
    verdict = manager.validate_marker(
        second["hash"],
        project=PROJECT,
        original_chars=second["original_size"],
        trusted_issuers={("second-agent", "second-sess")},
    )
    assert verdict["valid"] is False
    assert verdict["check"] == "provenance"


def test_identity_less_store_is_unverifiable(manager: MemoryManager) -> None:
    """Rows stored without caller identity (legacy or identity-less
    compress) carry NULL issuers → distinct refusal reason."""
    result = manager.compress_content(CONTENT, project=PROJECT)
    assert result["cached"] is True
    verdict = manager.validate_marker(
        result["hash"],
        project=PROJECT,
        original_chars=result["original_size"],
        trusted_issuers={(AGENT, SESSION)},
    )
    assert verdict["valid"] is False
    assert verdict["check"] == "provenance"
    assert verdict["reason"] == "unverifiable legacy marker"
    # And strict mode refuses with the distinct reason.
    refused = manager.retrieve_content(
        result["hash"],
        project=PROJECT,
        validate_marker=True,
        original_chars=result["original_size"],
        agent=AGENT,
        session=SESSION,
    )
    assert refused["refused"] is True
    assert "unverifiable legacy marker" in refused["reason"]
    assert "original" not in refused


# ── Issuer inventory: every compress path threads identity ───────────────────


def test_issuer_populated_by_manager_compress(manager: MemoryManager) -> None:
    marker = _mint(manager)
    entry = manager.sqlite.ccr_get(marker["hash"], project=PROJECT, bump=False)
    assert entry is not None
    assert entry["issuer_agent"] == AGENT
    assert entry["issuer_session"] == SESSION


def test_issuer_populated_by_context_rewrite(manager: MemoryManager) -> None:
    """W2's include_marker path is the W3 automation channel: the marker
    is minted in the event's own issuer context."""
    receipt = manager.context_rewrite(
        content=CONTENT,
        project=PROJECT,
        agent=AGENT,
        session=SESSION,
        include_marker=True,
    )
    ccr_result = receipt["ccr_marker"]
    assert ccr_result["cached"] is True
    entry = manager.sqlite.ccr_get(ccr_result["hash"], project=PROJECT, bump=False)
    assert entry is not None
    assert entry["issuer_agent"] == AGENT
    assert entry["issuer_session"] == SESSION


def test_issuer_populated_by_mcp_compress(mcp_wired: MemoryManager) -> None:
    import asyncio

    result = asyncio.run(
        mcp_mod._dispatch(
            "vesma_compress",
            {"text": CONTENT, "project": PROJECT, "agent": AGENT, "session": SESSION},
        )
    )
    entry = mcp_wired.sqlite.ccr_get(result["hash"], project=PROJECT, bump=False)
    assert entry is not None
    assert entry["issuer_agent"] == AGENT
    assert entry["issuer_session"] == SESSION


def test_mcp_retrieve_strict_refusal(mcp_wired: MemoryManager) -> None:
    import asyncio

    marker = _mint(mcp_wired)
    result = asyncio.run(
        mcp_mod._dispatch(
            "vesma_retrieve",
            {
                "hash": marker["hash"],
                "project": PROJECT,
                "validate_marker": True,
                "original_chars": marker["original_chars"],
                "agent": AGENT,
                "session": OTHER_SESSION,  # provenance fail
            },
        )
    )
    assert result["refused"] is True
    assert result["reason"].startswith("marker validation failed: provenance")
    assert "original" not in result


def test_mcp_boundary_type_guards(mcp_wired: MemoryManager) -> None:
    """Malformed A2 args get a clean error dict (vesma_context_rewrite
    boundary pattern), not an AttributeError in the manager."""
    import asyncio

    for tool, bad in (
        ("vesma_compress", {"text": CONTENT, "agent": 42}),
        ("vesma_retrieve", {"hash": "f" * 64, "agent": 42}),
        ("vesma_retrieve", {"hash": "f" * 64, "original_chars": "500"}),
        ("vesma_retrieve", {"hash": "f" * 64, "original_chars": True}),
    ):
        result = asyncio.run(mcp_mod._dispatch(tool, bad))
        assert "error" in result, (tool, bad)
        assert "must be" in result["error"], (tool, bad)


# ── REST surface ─────────────────────────────────────────────────────────────


def test_rest_compress_records_issuer_and_strict_retrieve_roundtrip(
    rest_client: TestClient,
) -> None:
    compress = rest_client.post(
        "/compress", json={"text": CONTENT, "project": PROJECT, "agent": AGENT, "session": SESSION}
    )
    assert compress.status_code == 200
    marker = parse_marker(compress.json()["compressed_text"])
    assert marker is not None

    ok = rest_client.post(
        "/retrieve",
        json={
            "hash": marker["hash"],
            "project": PROJECT,
            "validate_marker": True,
            "original_chars": marker["original_chars"],
            "agent": AGENT,
            "session": SESSION,
        },
    )
    assert ok.status_code == 200
    assert ok.json()["original"] == CONTENT

    refused = rest_client.post(
        "/retrieve",
        json={
            "hash": marker["hash"],
            "project": PROJECT,
            "validate_marker": True,
            "original_chars": marker["original_chars"],
            "agent": AGENT,
            "session": OTHER_SESSION,
        },
    )
    assert refused.status_code == 200
    body = refused.json()
    assert body["refused"] is True
    assert "marker validation failed: provenance" in body["reason"]
    assert "original" not in body


def test_rest_knob_on_refuses_marker_shaped(strict_rest_client: TestClient) -> None:
    compress = strict_rest_client.post(
        "/compress", json={"text": CONTENT, "project": PROJECT, "agent": AGENT, "session": SESSION}
    )
    assert compress.status_code == 200
    marker = parse_marker(compress.json()["compressed_text"])
    assert marker is not None
    refused = strict_rest_client.post(
        "/retrieve",
        json={
            "hash": marker["hash"],
            "project": PROJECT,
            "original_chars": 1,  # integrity fail — no per-call param needed
            "agent": AGENT,
            "session": SESSION,
        },
    )
    assert refused.status_code == 200
    body = refused.json()
    assert body["refused"] is True
    assert "marker validation failed: integrity" in body["reason"]
    assert "original" not in body


# ── A2 review round ──────────────────────────────────────────────────────────


def test_refusal_reasons_are_non_oracle(manager: MemoryManager) -> None:
    """F1: refusal reasons are FIXED strings — a reason echoing the
    stored length or the stored issuer pair is a two-call oracle (read
    the true values, re-call with them) that defeats provenance."""
    marker = _mint(manager)
    stored_len = len(CONTENT)

    integrity = manager.validate_marker(
        marker["hash"],
        project=PROJECT,
        original_chars=stored_len + 7,
        trusted_issuers={(AGENT, SESSION)},
    )
    assert integrity["reason"] == "original_chars mismatch"
    assert str(stored_len) not in integrity["reason"]
    assert str(stored_len + 7) not in integrity["reason"]

    provenance = manager.validate_marker(
        marker["hash"],
        project=PROJECT,
        original_chars=marker["original_chars"],
        trusted_issuers={(AGENT, OTHER_SESSION)},
    )
    assert provenance["reason"] == "issuer mismatch"
    # The STORED issuer pair must not appear (the caller's own values
    # may — they supplied them).
    assert AGENT not in provenance["reason"]
    assert SESSION not in provenance["reason"]

    # Same guarantee through the strict-mode refusal surface.
    refused = manager.retrieve_content(
        marker["hash"],
        project=PROJECT,
        validate_marker=True,
        original_chars=stored_len + 7,
        agent=AGENT,
        session=SESSION,
    )
    assert refused["reason"] == "marker validation failed: integrity: original_chars mismatch"
    assert str(stored_len) not in refused["reason"]


def test_spec_components_normalized(manager: MemoryManager) -> None:
    """F4: spec components are stripped inside validate_marker,
    mirroring the ccr_store issuer normalisation."""
    marker = _mint(manager)
    verdict = manager.validate_marker(
        marker["hash"],
        project=PROJECT,
        original_chars=marker["original_chars"],
        trusted_issuers={(f"  {AGENT}  ", f"  {SESSION}  ")},
    )
    assert verdict == {"valid": True, "reason": None, "check": None}
    with pytest.raises(ValueError, match="strings or None"):
        manager.validate_marker(
            marker["hash"],
            project=PROJECT,
            original_chars=marker["original_chars"],
            trusted_issuers={(AGENT, 42)},
        )


def test_strict_hash_only_issuer_stamped_refused(strict_manager: MemoryManager) -> None:
    """F2: stripping the optional args must NOT bypass the gate — a
    hash-only retrieve of an issuer-stamped row in strict mode is
    refused with the distinct reason and no content."""
    marker = _mint(strict_manager)
    result = strict_manager.retrieve_content(marker["hash"], project=PROJECT)
    assert result["found"] is True
    assert result["refused"] is True
    assert result["reason"] == "marker validation required"
    assert "original" not in result
    assert "snippets" not in result
    # Refusal never bumps (F4 semantics).
    entry = strict_manager.sqlite.ccr_get(marker["hash"], project=PROJECT, bump=False)
    assert entry is not None
    assert entry["retrieval_count"] == 0


def test_per_call_strict_hash_only_refused(manager: MemoryManager) -> None:
    """F2 scope: per-call validate_marker=True triggers the hash-only
    closure even with the knob OFF."""
    marker = _mint(manager)
    result = manager.retrieve_content(marker["hash"], project=PROJECT, validate_marker=True)
    assert result["refused"] is True
    assert result["reason"] == "marker validation required"
    assert "original" not in result


def test_per_call_opt_out_disables_hash_only_closure(strict_manager: MemoryManager) -> None:
    """The explicit validate_marker=False escape hatch disables BOTH
    strict gates (validation and the F2 closure)."""
    marker = _mint(strict_manager)
    result = strict_manager.retrieve_content(marker["hash"], project=PROJECT, validate_marker=False)
    assert result["found"] is True
    assert result.get("refused") is not True
    assert result["original"] == CONTENT


def test_strict_hash_only_legacy_allowed_with_warn(
    strict_manager: MemoryManager, caplog: pytest.LogCaptureFixture
) -> None:
    """F2 legacy line: NULL-issuer rows are unverifiable by construction
    — hash-only stays ALLOWED under strict mode with a WARNING (refusing
    would brick all pre-A2 caches for zero marginal adversary
    resistance)."""
    stamped = strict_manager.compress_content(CONTENT, project=PROJECT)  # identity-less
    assert stamped["cached"] is True
    with caplog.at_level(logging.WARNING, logger="vesmaro.manager"):
        result = strict_manager.retrieve_content(stamped["hash"], project=PROJECT)
    assert result["found"] is True
    assert result.get("refused") is not True
    assert result["original"] == CONTENT
    assert any(
        "unverifiable legacy" in rec.message and "A2 review F2" in rec.message
        for rec in caplog.records
    )


def test_mcp_validate_marker_type_guard(mcp_wired: MemoryManager) -> None:
    """F3: no bool() coercion — the string "false" (or any non-bool
    value) is a boundary error, not a truthy strict-mode opt-in."""
    import asyncio

    bad = asyncio.run(
        mcp_mod._dispatch("vesma_retrieve", {"hash": "f" * 64, "validate_marker": "false"})
    )
    assert bad == {"error": "validate_marker must be a boolean when provided"}
    # A real bool passes the guard untouched (None-follows-kob default
    # preserved: explicit True below).
    marker = _mint(mcp_wired)
    refused = asyncio.run(
        mcp_mod._dispatch(
            "vesma_retrieve",
            {"hash": marker["hash"], "project": PROJECT, "validate_marker": True},
        )
    )
    assert refused["refused"] is True  # F2: hash-only + strict → refused
    assert refused["reason"] == "marker validation required"


def _marker_memory(mgr: MemoryManager, *, agent: str | None, session: str | None) -> str:
    """Compress CONTENT in the given issuer context and store a published
    memory carrying the inline marker; return the marker hash."""
    compressed = mgr.compress_content(CONTENT, project=PROJECT, agent=agent, session=session)
    assert compressed["cached"] is True
    mgr.add(
        MemoryCreate(
            content=(
                "Context block summary for the deployment run.\n"
                f"{compressed['marker']}\n"
                "(original available via the marker above)"
            ),
            tags=[f"project:{PROJECT}", f"agent:{AGENT}", "mnemos:learning"],
            source=MemorySource.MCP,
            status=MemoryStatus.PUBLISHED,
        ),
        project=PROJECT,
        agent=AGENT,
    )
    return str(compressed["hash"])


def test_assemble_strict_expands_with_matching_identity(strict_manager: MemoryManager) -> None:
    """F2 assemble composition: agent+session threaded → the expansion
    runs under the caller's issuer context and succeeds."""
    _marker_memory(strict_manager, agent=AGENT, session=SESSION)
    result = strict_manager.assemble_context(
        session=SESSION,
        project=PROJECT,
        expand_ccr=True,
        budget=8192,
        agent=AGENT,
    )
    assert result["stats"]["ccr"]["expanded"] == 1
    assert "Deployment runbook" in result["text"]


def test_assemble_strict_skips_without_identity(strict_manager: MemoryManager) -> None:
    """No full identity → strict deployment SKIPS the expansion of
    issuer-stamped markers (the marker stays; counted as refused)."""
    _marker_memory(strict_manager, agent=AGENT, session=SESSION)
    result = strict_manager.assemble_context(
        session=SESSION, project=PROJECT, expand_ccr=True, budget=8192
    )
    ccr = result["stats"]["ccr"]
    assert ccr["markers_found"] == 1
    assert ccr["expanded"] == 0
    assert ccr["skipped_refused"] == 1
    assert ccr["skipped_missing"] == 0
    assert "[compressed:" in result["text"]


def test_assemble_strict_wrong_identity_skips(strict_manager: MemoryManager) -> None:
    """Full but WRONG identity → the validation gate refuses → expansion
    skipped, marker stays (fail-closed on the automation channel)."""
    _marker_memory(strict_manager, agent=AGENT, session=SESSION)
    result = strict_manager.assemble_context(
        session=OTHER_SESSION,
        project=PROJECT,
        expand_ccr=True,
        budget=8192,
        agent=AGENT,
    )
    ccr = result["stats"]["ccr"]
    assert ccr["expanded"] == 0
    assert ccr["skipped_refused"] == 1
    assert "[compressed:" in result["text"]


def test_assemble_strict_legacy_null_expands(strict_manager: MemoryManager) -> None:
    """Legacy NULL-issuer rows still expand under strict mode even
    without identity (WARN-allowed line)."""
    _marker_memory(strict_manager, agent=None, session=None)
    result = strict_manager.assemble_context(
        session=SESSION, project=PROJECT, expand_ccr=True, budget=8192
    )
    assert result["stats"]["ccr"]["expanded"] == 1
    assert "Deployment runbook" in result["text"]


def test_assemble_knob_off_expands_without_identity(manager: MemoryManager) -> None:
    """Knob-off deployments: unchanged assemble UX — expansion works
    without identity (no validation, no closure)."""
    _marker_memory(manager, agent=AGENT, session=SESSION)
    result = manager.assemble_context(
        session=SESSION, project=PROJECT, expand_ccr=True, budget=8192
    )
    assert result["stats"]["ccr"]["expanded"] == 1


# ── Migration round-trips ────────────────────────────────────────────────────


def test_migration_legacy_composite_rows_keep_null_issuer(tmp_path: Path) -> None:
    """Post-A1 / pre-A2 database: ccr_cache has the composite PK but no
    issuer columns. Opening the store adds the columns; legacy rows are
    NULL-issuer → strict mode refuses them as unverifiable legacy
    markers."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    db_path = data_dir / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE ccr_cache (
            hash             TEXT NOT NULL,
            original         TEXT NOT NULL,
            project          TEXT NOT NULL DEFAULT '',
            created_at       TEXT NOT NULL,
            size_bytes       INTEGER NOT NULL DEFAULT 0,
            retrieval_count  INTEGER NOT NULL DEFAULT 0,
            last_retrieved_at TEXT,
            secret_scan_verdict TEXT,
            secret_scan_at      TEXT,
            PRIMARY KEY (project, hash)
        );
        """
    )
    h = content_hash(CONTENT)
    conn.execute(
        "INSERT INTO ccr_cache (hash, original, project, created_at, size_bytes) "
        "VALUES (?,?,?,datetime('now'),?)",
        (h, CONTENT, PROJECT, len(CONTENT.encode("utf-8"))),
    )
    conn.commit()
    conn.close()

    # The manager's store connects to the legacy DB in place — migrations
    # run on first connect, then the NULL-issuer row is checked.
    mgr = _manager(_settings(tmp_path))
    assert mgr.sqlite.db_path == db_path
    verdict = mgr.validate_marker(
        h,
        project=PROJECT,
        original_chars=len(CONTENT),
        trusted_issuers={(AGENT, SESSION)},
    )
    assert verdict["valid"] is False
    assert verdict["check"] == "provenance"
    assert verdict["reason"] == "unverifiable legacy marker"
    refused = mgr.retrieve_content(
        h,
        project=PROJECT,
        validate_marker=True,
        original_chars=len(CONTENT),
        agent=AGENT,
        session=SESSION,
    )
    assert refused["refused"] is True
    assert "unverifiable legacy marker" in refused["reason"]
    assert "original" not in refused
    mgr.close()


def test_migration_pre_a1_hash_pk_rebuild_keeps_row(tmp_path: Path) -> None:
    """Pre-A1 hash-PK database: the A1 rebuild copy list must stay
    aligned with the rebuild DDL (issuer columns included) or the
    migration crashes; the surviving row is NULL-issuer."""
    db_path = tmp_path / "pre_a1.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE ccr_cache (
            hash             TEXT NOT NULL PRIMARY KEY,
            original         TEXT NOT NULL,
            project          TEXT NOT NULL DEFAULT '',
            created_at       TEXT NOT NULL,
            size_bytes       INTEGER NOT NULL DEFAULT 0,
            retrieval_count  INTEGER NOT NULL DEFAULT 0,
            last_retrieved_at TEXT
        );
        """
    )
    h = content_hash(CONTENT)
    conn.execute(
        "INSERT INTO ccr_cache (hash, original, project, created_at, size_bytes) "
        "VALUES (?,?,?,datetime('now'),?)",
        (h, CONTENT, PROJECT, len(CONTENT.encode("utf-8"))),
    )
    conn.commit()
    conn.close()

    store = SQLiteStore(db_path)
    entry = store.ccr_get(h, project=PROJECT, bump=False)
    assert entry is not None
    assert entry["original"] == CONTENT
    assert entry["issuer_agent"] is None
    pk = [(str(r[1]), int(r[5])) for r in store._get_conn().execute("PRAGMA table_info(ccr_cache)")]
    assert dict((c, p) for c, p in pk if p)["project"] == 1
    assert dict((c, p) for c, p in pk if p)["hash"] == 2
    store.close()
