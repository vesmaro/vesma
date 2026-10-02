"""Tests for the federation server (B-side, Phase 2, contract §3.2).

Covers:

* Auth — no peers → 403, wrong token → 403, correct token → 200, mTLS
  pinning (configured + presented → OK; configured + missing → 403;
  not configured → OK).
* Rate limit — over the per-peer limit → 429.
* ACL — disallowed project → 403 + REFUSED, allowed project → OK,
  ``["*"]`` wildcard → OK.
* Anti-correlation — second request on the same topic after EXHAUSTIVE
  → ALREADY_EXHAUSTED without re-running search.
* Moderation — record with ``mnemos:no-federate`` → excluded;
  record with a secret → redacted in the shipped record;
  record all-secret → refused → PARTIAL.
* Trigger codes — no candidates → EXHAUSTIVE (empty); all clean →
  EXHAUSTIVE; some refused → PARTIAL; ALREADY_EXHAUSTED short-circuit.
* Access log — one entry written per request; topic_hash is SHA-256
  (never plaintext); ALREADY_EXHAUSTED entry has empty record_ids.

All fixtures use RFC-reserved dummy values per
``sensitive-data.instructions.md``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from vesma.config import FederationConfig, PeerConfig, Settings
from vesma.federation_access_log import AccessLogEntry, FederationAccessLog, hash_topic
from vesma.federation_server import (
    PullRequest,
    PullResponse,
    RateLimiter,
    handle_pull,
    verify_mtls_fingerprint,
)
from vesma.manager import MemoryManager
from vesma.models import MemoryCreate, MemoryStatus
from vesma.trigger_codes import TriggerCode

# RFC-reserved constants — never real credentials / identifiers.
PEER_A = "mnemos-A"
PEER_B = "mnemos-B"
PROJECT = "project-mnemos"
SECRET_PROJECT = "project-secret"
TOKEN_ENV = "VESMA_FED_PEER_VESMARO_A_TOKEN"
TOKEN_VALUE = "mnk_fed_mnemos-A_exampletoken123"
FAKE_AWS_KEY = "AKIA" + "T" * 16  # obviously fake
# Long fake OpenAI-style key (sk- + 100 alnum). Used by the refusal test to
# push the redacted fraction above the 0.8 refuse_threshold while keeping
# the query phrase for FTS matching. Never a real credential.
FAKE_LONG_OPENAI_KEY = "sk-" + "a" * 100
# 96-byte hex SHA-256 fingerprint (clearly fake). Wrapped to fit 100 cols.
MTLS_FP = (
    "aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99:"
    "aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99"
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    """Settings with one configured peer and an isolated SQLite store."""
    os.environ[TOKEN_ENV] = TOKEN_VALUE
    settings = Settings(
        vesma={
            "vault_path": str(tmp_path / "vault"),
            "data_dir": str(tmp_path / "data"),
            "db_name": "test.db",
        },
        embedding={"provider": "onnx"},
        scanner={"enabled": False},
        federation={
            "shared_projects": [PROJECT],
            "peers": {
                PEER_A: PeerConfig(
                    bearer_token_env=TOKEN_ENV,
                    allowed_projects=[PROJECT],
                    allowed_types=["decision", "learning"],
                    rate_limit_per_minute=5,
                ),
            },
        },
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def manager(tmp_settings: Settings) -> MemoryManager:
    mgr = MemoryManager(tmp_settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    yield mgr
    mgr.close()


@pytest.fixture
def access_log(tmp_path: Path) -> FederationAccessLog:
    return FederationAccessLog(tmp_path / "fed-access.jsonl")


@pytest.fixture
def fresh_limiter() -> RateLimiter:
    return RateLimiter()


def _add_memory(
    manager: MemoryManager,
    content: str,
    *,
    tags: list[str],
    title: str | None = None,
    project: str = PROJECT,
    agent: str = "gcw-tech-lead",
) -> None:
    """Add a memory via the manager (published so search returns it)."""
    mem = manager.add(
        MemoryCreate(content=content, title=title, tags=list(tags)),
        project=project,
        agent=agent,
    )
    # Promote to published so the default search returns it.
    manager.sqlite.update_status(mem.id, MemoryStatus.PUBLISHED)


def _pull(
    manager: MemoryManager,
    access_log: FederationAccessLog,
    settings: Settings,
    *,
    peer_id: str = PEER_A,
    query: str = "federation threat model",
    project_scope: str = PROJECT,
    token: str | None = TOKEN_VALUE,
    mtls: str | None = None,
    limiter: RateLimiter | None = None,
    now: datetime | None = None,
) -> tuple[PullResponse, int]:
    return cast(
        "tuple[PullResponse, int]",
        handle_pull(
            PullRequest(peer_id=peer_id, query=query, project_scope=project_scope),
            settings=settings,
            manager=manager,
            access_log=access_log,
            presented_token=token,
            presented_mtls_fingerprint=mtls,
            rate_limiter=limiter or RateLimiter(),
            now=now or datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC),
        ),
    )


class TestAuth:
    def test_no_peers_configured_returns_403(
        self, manager: MemoryManager, access_log: FederationAccessLog, tmp_path: Path
    ) -> None:
        settings = Settings(
            vesma={
                "vault_path": str(tmp_path / "vault"),
                "data_dir": str(tmp_path / "data"),
                "db_name": "test.db",
            },
            embedding={"provider": "onnx"},
            scanner={"enabled": False},
            federation=FederationConfig(),
        )
        settings.resolve_paths()
        resp, status = _pull(manager, access_log, settings)
        assert status == 403
        assert resp.trigger_code == TriggerCode.REFUSED

    def test_unknown_peer_returns_403(
        self, manager: MemoryManager, access_log: FederationAccessLog, tmp_settings: Settings
    ) -> None:
        resp, status = _pull(manager, access_log, tmp_settings, peer_id="mnemos-Z")
        assert status == 403
        assert resp.trigger_code == TriggerCode.REFUSED

    def test_wrong_token_returns_403(
        self, manager: MemoryManager, access_log: FederationAccessLog, tmp_settings: Settings
    ) -> None:
        resp, status = _pull(manager, access_log, tmp_settings, token="wrong")
        assert status == 403
        assert resp.trigger_code == TriggerCode.REFUSED

    def test_missing_token_returns_403(
        self, manager: MemoryManager, access_log: FederationAccessLog, tmp_settings: Settings
    ) -> None:
        resp, status = _pull(manager, access_log, tmp_settings, token=None)
        assert status == 403
        assert resp.trigger_code == TriggerCode.REFUSED

    def test_correct_token_returns_200(
        self, manager: MemoryManager, access_log: FederationAccessLog, tmp_settings: Settings
    ) -> None:
        _add_memory(
            manager,
            "federation threat model: We chose bearer+TOTP 2FA for remote sessions.",
            tags=["project:project-mnemos", "agent:gcw-tech-lead", "mnemos:decision"],
            title="ADR-0014 auth decision",
        )
        resp, status = _pull(manager, access_log, tmp_settings)
        assert status == 200
        assert resp.trigger_code == TriggerCode.EXHAUSTIVE
        assert len(resp.records) == 1


class TestMTLSFingerprint:
    def test_no_expected_fingerprint_allows(self) -> None:
        assert verify_mtls_fingerprint("anything", None) is True

    def test_expected_but_not_presented_refuses(self) -> None:
        assert verify_mtls_fingerprint(None, MTLS_FP) is False

    def test_expected_and_matching_allows(self) -> None:
        assert verify_mtls_fingerprint(MTLS_FP, MTLS_FP) is True

    def test_expected_and_mismatch_refuses(self) -> None:
        assert verify_mtls_fingerprint("00:11:22", MTLS_FP) is False

    def test_case_insensitive(self) -> None:
        assert verify_mtls_fingerprint(MTLS_FP.upper(), MTLS_FP.lower()) is True

    def test_mtls_mismatch_writes_access_log_entry(
        self,
        manager: MemoryManager,
        access_log: FederationAccessLog,
        tmp_path: Path,
    ) -> None:
        """An mTLS cert mismatch (peer known, cert wrong) writes a REFUSED entry.

        The peer is resolved (past the unknown-peer gate) but the presented
        cert fingerprint does not match the pinned one — a forensic signal
        per contract §10. Only this path is logged; no-peers / unknown-peer /
        token-mismatch paths stay unlogged (untrusted peer_id).
        """
        os.environ[TOKEN_ENV] = TOKEN_VALUE
        settings = Settings(
            vesma={
                "vault_path": str(tmp_path / "vault"),
                "data_dir": str(tmp_path / "data"),
                "db_name": "test.db",
            },
            embedding={"provider": "onnx"},
            scanner={"enabled": False},
            federation={
                "shared_projects": [PROJECT],
                "peers": {
                    PEER_A: PeerConfig(
                        bearer_token_env=TOKEN_ENV,
                        allowed_projects=[PROJECT],
                        allowed_types=["*"],
                        rate_limit_per_minute=60,
                        mtls_cert_fingerprint=MTLS_FP,
                    ),
                },
            },
        )
        settings.resolve_paths()
        # Present a wrong fingerprint → mismatch.
        resp, status = _pull(manager, access_log, settings, mtls="00:11:22:33:44")
        assert status == 403
        assert resp.trigger_code == TriggerCode.REFUSED
        # One REFUSED entry was appended.
        entries = access_log.query_recent(PEER_A, since=datetime(2020, 1, 1, tzinfo=UTC))
        assert len(entries) == 1
        assert entries[0].peer_id == PEER_A
        assert entries[0].trigger_code == TriggerCode.REFUSED
        assert entries[0].record_ids_accessed == []

    def test_mtls_missing_cert_when_pinned_writes_access_log_entry(
        self,
        manager: MemoryManager,
        access_log: FederationAccessLog,
        tmp_path: Path,
    ) -> None:
        """A missing presented cert when pinning is on writes a REFUSED entry.

        The peer is resolved and pinning is configured, but no cert was
        presented (proxy stripped it / connection was not mTLS) → fail-closed
        refusal, audited as a REFUSED entry.
        """
        os.environ[TOKEN_ENV] = TOKEN_VALUE
        settings = Settings(
            vesma={
                "vault_path": str(tmp_path / "vault"),
                "data_dir": str(tmp_path / "data"),
                "db_name": "test.db",
            },
            embedding={"provider": "onnx"},
            scanner={"enabled": False},
            federation={
                "shared_projects": [PROJECT],
                "peers": {
                    PEER_A: PeerConfig(
                        bearer_token_env=TOKEN_ENV,
                        allowed_projects=[PROJECT],
                        allowed_types=["*"],
                        rate_limit_per_minute=60,
                        mtls_cert_fingerprint=MTLS_FP,
                    ),
                },
            },
        )
        settings.resolve_paths()
        # Present no fingerprint → fail-closed refusal, audited.
        resp, status = _pull(manager, access_log, settings, mtls=None)
        assert status == 403
        assert resp.trigger_code == TriggerCode.REFUSED
        entries = access_log.query_recent(PEER_A, since=datetime(2020, 1, 1, tzinfo=UTC))
        assert len(entries) == 1
        assert entries[0].trigger_code == TriggerCode.REFUSED

    def test_mtls_pinning_off_does_not_write_access_log_on_pass(
        self,
        manager: MemoryManager,
        access_log: FederationAccessLog,
        tmp_settings: Settings,
    ) -> None:
        """When pinning is off (fingerprint=None), mTLS check passes — no mismatch log.

        The default tmp_settings peer has mtls_cert_fingerprint=None (pinning
        off). A pull with no presented cert passes the mTLS gate and proceeds
        to the normal flow — no REFUSED-mismatch entry is written for the
        mTLS gate (the normal access-log entry for a successful pull is fine).
        """
        _add_memory(
            manager,
            "federation threat model decision",
            tags=["project:project-mnemos", "agent:gcw-tech-lead", "mnemos:decision"],
        )
        _resp, status = _pull(manager, access_log, tmp_settings, mtls=None)
        assert status == 200
        # The only entry is the normal successful-pull entry (EXHAUSTIVE).
        entries = access_log.query_recent(PEER_A, since=datetime(2020, 1, 1, tzinfo=UTC))
        assert len(entries) == 1
        assert entries[0].trigger_code == TriggerCode.EXHAUSTIVE


# ── Rate limit ────────────────────────────────────────────────────────────────


class TestRateLimit:
    def test_over_limit_returns_429(
        self,
        manager: MemoryManager,
        access_log: FederationAccessLog,
        tmp_settings: Settings,
        fresh_limiter: RateLimiter,
    ) -> None:
        # peer limit is 5/min — exhaust it.
        for _ in range(5):
            _pull(manager, access_log, tmp_settings, limiter=fresh_limiter)
        resp, status = _pull(manager, access_log, tmp_settings, limiter=fresh_limiter)
        assert status == 429
        assert resp.trigger_code == TriggerCode.REFUSED

    def test_rate_limit_refusal_writes_access_log_entry(
        self,
        manager: MemoryManager,
        access_log: FederationAccessLog,
        tmp_settings: Settings,
        fresh_limiter: RateLimiter,
    ) -> None:
        """A 429 rate-limit refusal writes a REFUSED access-log entry (§10).

        Post-auth refusals must be audited per contract §10 — the peer is
        known and authenticated, so the rate-limit refusal is a forensic
        signal (peer is hammering B past its quota).
        """
        # peer limit is 5/min — exhaust it with 5 successful pulls.
        for _ in range(5):
            _pull(manager, access_log, tmp_settings, limiter=fresh_limiter)
        # The 6th pull hits the rate limit and must write a REFUSED entry.
        before = len(access_log.query_recent(PEER_A, since=datetime(2020, 1, 1, tzinfo=UTC)))
        _pull(manager, access_log, tmp_settings, limiter=fresh_limiter)
        after = access_log.query_recent(PEER_A, since=datetime(2020, 1, 1, tzinfo=UTC))
        # Exactly one new entry was appended.
        assert len(after) == before + 1
        # The new entry is REFUSED with empty record_ids.
        new_entry = after[-1]
        assert new_entry.peer_id == PEER_A
        assert new_entry.trigger_code == TriggerCode.REFUSED
        assert new_entry.record_ids_accessed == []


# ── ACL ───────────────────────────────────────────────────────────────────────


class TestACL:
    def test_disallowed_project_returns_403_refused(
        self, manager: MemoryManager, access_log: FederationAccessLog, tmp_settings: Settings
    ) -> None:
        _add_memory(
            manager,
            "internal-only decision",
            tags=["project:project-mnemos", "agent:gcw-tech-lead", "mnemos:decision"],
        )
        resp, status = _pull(manager, access_log, tmp_settings, project_scope="project-other")
        assert status == 403
        assert resp.trigger_code == TriggerCode.REFUSED

    def test_allowed_project_returns_200(
        self, manager: MemoryManager, access_log: FederationAccessLog, tmp_settings: Settings
    ) -> None:
        _add_memory(
            manager,
            "shared decision",
            tags=["project:project-mnemos", "agent:gcw-tech-lead", "mnemos:decision"],
        )
        resp, status = _pull(manager, access_log, tmp_settings)
        assert status == 200
        assert resp.trigger_code == TriggerCode.EXHAUSTIVE

    def test_wildcard_allowed_projects(
        self, manager: MemoryManager, access_log: FederationAccessLog, tmp_path: Path
    ) -> None:
        os.environ[TOKEN_ENV] = TOKEN_VALUE
        settings = Settings(
            vesma={
                "vault_path": str(tmp_path / "vault"),
                "data_dir": str(tmp_path / "data"),
                "db_name": "test.db",
            },
            embedding={"provider": "onnx"},
            scanner={"enabled": False},
            federation={
                "peers": {
                    PEER_A: PeerConfig(
                        bearer_token_env=TOKEN_ENV,
                        allowed_projects=["*"],
                        allowed_types=["*"],
                        rate_limit_per_minute=60,
                    ),
                },
            },
        )
        settings.resolve_paths()
        _add_memory(
            manager,
            "any project decision",
            tags=["project:project-mnemos", "agent:gcw-tech-lead", "mnemos:decision"],
        )
        resp, status = _pull(manager, access_log, settings, project_scope="project-mnemos")
        assert status == 200
        assert resp.trigger_code == TriggerCode.EXHAUSTIVE

    def test_empty_allowed_projects_fail_closed(
        self, manager: MemoryManager, access_log: FederationAccessLog, tmp_path: Path
    ) -> None:
        os.environ[TOKEN_ENV] = TOKEN_VALUE
        settings = Settings(
            vesma={
                "vault_path": str(tmp_path / "vault"),
                "data_dir": str(tmp_path / "data"),
                "db_name": "test.db",
            },
            embedding={"provider": "onnx"},
            scanner={"enabled": False},
            federation={
                "peers": {
                    PEER_A: PeerConfig(
                        bearer_token_env=TOKEN_ENV,
                        allowed_projects=[],
                        allowed_types=["*"],
                        rate_limit_per_minute=60,
                    ),
                },
            },
        )
        settings.resolve_paths()
        resp, status = _pull(manager, access_log, settings)
        assert status == 403
        assert resp.trigger_code == TriggerCode.REFUSED

    def test_disallowed_type_excluded(
        self, manager: MemoryManager, access_log: FederationAccessLog, tmp_settings: Settings
    ) -> None:
        # peer allows only decision/learning; a session record is excluded.
        _add_memory(
            manager,
            "session checkpoint",
            tags=["project:project-mnemos", "agent:gcw-tech-lead", "mnemos:checkpoint"],
        )
        resp, status = _pull(manager, access_log, tmp_settings)
        assert status == 200
        # The checkpoint record is filtered out → EXHAUSTIVE with empty records.
        assert resp.trigger_code == TriggerCode.EXHAUSTIVE
        assert resp.records == []


# ── Anti-correlation ──────────────────────────────────────────────────────────


class TestAntiCorrelation:
    def test_second_request_same_topic_returns_already_exhausted(
        self, manager: MemoryManager, access_log: FederationAccessLog, tmp_settings: Settings
    ) -> None:
        _add_memory(
            manager,
            "federation threat model decision",
            tags=["project:project-mnemos", "agent:gcw-tech-lead", "mnemos:decision"],
        )
        # First pull → EXHAUSTIVE.
        resp1, status1 = _pull(manager, access_log, tmp_settings)
        assert status1 == 200
        assert resp1.trigger_code == TriggerCode.EXHAUSTIVE
        assert len(resp1.records) == 1

        # Second pull on the same topic → ALREADY_EXHAUSTED, no records.
        resp2, status2 = _pull(
            manager, access_log, tmp_settings, now=datetime(2026, 7, 21, 12, 1, 0, tzinfo=UTC)
        )
        assert status2 == 200
        assert resp2.trigger_code == TriggerCode.ALREADY_EXHAUSTED
        assert resp2.records == []

    def test_different_topic_does_not_short_circuit(
        self, manager: MemoryManager, access_log: FederationAccessLog, tmp_settings: Settings
    ) -> None:
        _add_memory(
            manager,
            "federation threat model decision",
            tags=["project:project-mnemos", "agent:gcw-tech-lead", "mnemos:decision"],
        )
        resp1, _ = _pull(manager, access_log, tmp_settings, query="topic one")
        assert resp1.trigger_code == TriggerCode.EXHAUSTIVE
        resp2, _ = _pull(manager, access_log, tmp_settings, query="topic two")
        # Different topic → not short-circuited (search runs).
        assert resp2.trigger_code == TriggerCode.EXHAUSTIVE


# ── Moderation ───────────────────────────────────────────────────────────────


class TestModeration:
    def test_no_federate_record_excluded(
        self, manager: MemoryManager, access_log: FederationAccessLog, tmp_settings: Settings
    ) -> None:
        _add_memory(
            manager,
            "internal-only decision",
            tags=[
                "project:project-mnemos",
                "agent:gcw-tech-lead",
                "mnemos:decision",
                "mnemos:no-federate",
            ],
        )
        resp, status = _pull(manager, access_log, tmp_settings)
        assert status == 200
        assert resp.trigger_code == TriggerCode.EXHAUSTIVE
        assert resp.records == []

    def test_secret_record_is_redacted_in_shipped_record(
        self, manager: MemoryManager, access_log: FederationAccessLog, tmp_settings: Settings
    ) -> None:
        # The write-path secrets scanner (Layer 1) auto-tags any record
        # containing a secret with ``mnemos:no-federate``, which the server
        # pre-filter excludes before moderation runs. This test verifies
        # moderation's REDACT path in transit, so we disable the
        # auto-tagging here: the record must reach moderation, get its
        # AWS key redacted to ``<REDACTED:aws-key>``, and still ship.
        # (ADR-0019 Phase A: _scan_and_tag now also returns the scan
        # verdict for the ingest audit — the stub mirrors the 2-tuple.)
        with patch.object(
            MemoryManager,
            "_scan_and_tag",
            staticmethod(lambda tags, content: (list(tags), {})),
        ):
            _add_memory(
                manager,
                f"federation threat model: Use the key {FAKE_AWS_KEY} for production access.",
                tags=["project:project-mnemos", "agent:gcw-tech-lead", "mnemos:decision"],
            )
        resp, status = _pull(manager, access_log, tmp_settings)
        assert status == 200
        # Secret redacted but record still shipped (redact verdict, not refuse).
        # Fraction is 20/80 = 0.25, well under the 0.8 refuse_threshold.
        assert resp.trigger_code == TriggerCode.EXHAUSTIVE
        assert len(resp.records) == 1
        assert FAKE_AWS_KEY not in resp.records[0].summary
        assert "<REDACTED:aws-key>" in resp.records[0].summary

    def test_all_secret_record_refused_returns_refused_not_partial(
        self, manager: MemoryManager, access_log: FederationAccessLog, tmp_settings: Settings
    ) -> None:
        # Moderation refuses when the redacted fraction exceeds the
        # refuse_threshold (default 0.8). The content keeps the query
        # phrase so FTS matches it as a candidate, then a long fake
        # OpenAI-style key fills the remainder so the fraction is
        # 103/127 ≈ 0.81 > 0.8 → refuse → candidate present, none shipped
        # → REFUSED (per ``_select_trigger_code`` decision table).
        #
        # The write-path scanner is patched out for the same reason as
        # the redact test: without the patch the record is auto-tagged
        # ``mnemos:no-federate`` and excluded by the server pre-filter
        # before moderation runs, which would yield EXHAUSTIVE-empty
        # instead of the REFUSED this test asserts.
        with patch.object(
            MemoryManager,
            "_scan_and_tag",
            staticmethod(lambda tags, content: (list(tags), {})),
        ):
            _add_memory(
                manager,
                f"federation threat model {FAKE_LONG_OPENAI_KEY}",
                tags=["project:project-mnemos", "agent:gcw-tech-lead", "mnemos:decision"],
            )
        resp, status = _pull(manager, access_log, tmp_settings)
        assert status == 200
        assert resp.trigger_code == TriggerCode.REFUSED
        assert resp.records == []


# ── Trigger code selection ───────────────────────────────────────────────────


class TestTriggerCodeSelection:
    def test_no_records_found_returns_exhaustive_empty(
        self, manager: MemoryManager, access_log: FederationAccessLog, tmp_settings: Settings
    ) -> None:
        # No memory on the topic → EXHAUSTIVE with empty records.
        resp, status = _pull(manager, access_log, tmp_settings, query="topic with nothing on B")
        assert status == 200
        assert resp.trigger_code == TriggerCode.EXHAUSTIVE
        assert resp.records == []

    def test_all_clean_records_returns_exhaustive(
        self, manager: MemoryManager, access_log: FederationAccessLog, tmp_settings: Settings
    ) -> None:
        _add_memory(
            manager,
            "federation threat model: clean decision one",
            tags=["project:project-mnemos", "agent:gcw-tech-lead", "mnemos:decision"],
        )
        _add_memory(
            manager,
            "federation threat model: clean learning two",
            tags=["project:project-mnemos", "agent:gcw-tech-lead", "mnemos:learning"],
        )
        resp, status = _pull(manager, access_log, tmp_settings)
        assert status == 200
        assert resp.trigger_code == TriggerCode.EXHAUSTIVE
        assert len(resp.records) >= 1

    def test_all_refused_returns_refused_not_partial(
        self, manager: MemoryManager, access_log: FederationAccessLog, tmp_settings: Settings
    ) -> None:
        # Direct unit test of ``_select_trigger_code``: when candidates
        # were found, all were refused, and 0 records shipped, the code
        # must be REFUSED — not PARTIAL. Per contract §9: REFUSED means
        # A falls back to local search; PARTIAL means A may refine
        # against shipped records. With 0 records there is nothing to
        # refine, so PARTIAL would be misleading.
        from vesma.federation_server import _select_trigger_code

        assert (
            _select_trigger_code(
                candidate_count=3,
                refused_count=3,
                records_count=0,
            )
            == TriggerCode.REFUSED
        )


# ── Access log ───────────────────────────────────────────────────────────────


class TestAccessLog:
    def test_one_entry_written_per_request(
        self, manager: MemoryManager, access_log: FederationAccessLog, tmp_settings: Settings
    ) -> None:
        _add_memory(
            manager,
            "decision",
            tags=["project:project-mnemos", "agent:gcw-tech-lead", "mnemos:decision"],
        )
        _pull(manager, access_log, tmp_settings)
        entries = list(_iter_entries(access_log))
        assert len(entries) == 1
        assert entries[0].peer_id == PEER_A
        assert entries[0].project_scope == PROJECT
        assert entries[0].trigger_code == TriggerCode.EXHAUSTIVE

    def test_topic_hash_is_sha256_never_plaintext(
        self, manager: MemoryManager, access_log: FederationAccessLog, tmp_settings: Settings
    ) -> None:
        _add_memory(
            manager,
            "decision",
            tags=["project:project-mnemos", "agent:gcw-tech-lead", "mnemos:decision"],
        )
        query = "unique-query-topic-12345"
        _pull(manager, access_log, tmp_settings, query=query)
        entries = list(_iter_entries(access_log))
        assert entries[0].topic_hash == hash_topic(query)
        # The raw query string must NOT appear in the log file.
        raw = access_log.path.read_text()
        assert query not in raw

    def test_already_exhausted_entry_has_empty_record_ids(
        self, manager: MemoryManager, access_log: FederationAccessLog, tmp_settings: Settings
    ) -> None:
        _add_memory(
            manager,
            "decision",
            tags=["project:project-mnemos", "agent:gcw-tech-lead", "mnemos:decision"],
        )
        _pull(manager, access_log, tmp_settings)
        _pull(manager, access_log, tmp_settings, now=datetime(2026, 7, 21, 12, 1, 0, tzinfo=UTC))
        entries = list(_iter_entries(access_log))
        assert len(entries) == 2
        assert entries[0].trigger_code == TriggerCode.EXHAUSTIVE
        assert entries[1].trigger_code == TriggerCode.ALREADY_EXHAUSTED
        assert entries[1].record_ids_accessed == []

    def test_refused_acl_writes_refused_entry(
        self, manager: MemoryManager, access_log: FederationAccessLog, tmp_settings: Settings
    ) -> None:
        _pull(manager, access_log, tmp_settings, project_scope="project-other")
        entries = list(_iter_entries(access_log))
        assert len(entries) == 1
        assert entries[0].trigger_code == TriggerCode.REFUSED


# ── Response shape ────────────────────────────────────────────────────────────


class TestResponseShape:
    def test_ttl_class_is_ephemeral(
        self, manager: MemoryManager, access_log: FederationAccessLog, tmp_settings: Settings
    ) -> None:
        _add_memory(
            manager,
            "decision",
            tags=["project:project-mnemos", "agent:gcw-tech-lead", "mnemos:decision"],
        )
        resp, _ = _pull(manager, access_log, tmp_settings)
        assert resp.ttl_class == "ephemeral"

    def test_peer_id_set_on_success(
        self, manager: MemoryManager, access_log: FederationAccessLog, tmp_settings: Settings
    ) -> None:
        _add_memory(
            manager,
            "decision",
            tags=["project:project-mnemos", "agent:gcw-tech-lead", "mnemos:decision"],
        )
        resp, _ = _pull(manager, access_log, tmp_settings)
        assert resp.peer_id == "mnemos-B"


def _iter_entries(log: FederationAccessLog) -> Iterator[AccessLogEntry]:
    """Read the raw JSONL log — bypasses the public iter (for assertions)."""
    if not log.path.exists():
        return
    with open(log.path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield AccessLogEntry.model_validate_json(line)
