"""Tests for the federation access log (Phase 1 prerequisite).

Covers contract §10 — append-only JSONL, SHA-256 topic hash (never
plaintext), anti-correlation query (most recent for peer+topic),
audit queries (recent, count by trigger code), and concurrent-append
safety. All fixtures use RFC-reserved dummy values per
``sensitive-data.instructions.md``.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pydantic
import pytest

from vesma.federation_access_log import (
    AccessLogEntry,
    FederationAccessLog,
    hash_topic,
)
from vesma.trigger_codes import TriggerCode

# ── Fixtures ──────────────────────────────────────────────────────────────────

# RFC 5737 / RFC 3849 reserved values — never real identifiers.
PEER_A = "mnemos-A"
PEER_B = "mnemos-B"
PROJECT = "project-mnemos"
TOPIC_X = "federation threat model"
TOPIC_Y = "deferred features backlog"
TOPIC_X_HASH = hash_topic(TOPIC_X)
TOPIC_Y_HASH = hash_topic(TOPIC_Y)


def _entry(
    *,
    peer_id: str = PEER_A,
    topic_hash: str = TOPIC_X_HASH,
    timestamp: datetime | None = None,
    project_scope: str = PROJECT,
    trigger_code: TriggerCode = TriggerCode.EXHAUSTIVE,
    record_ids: list[str] | None = None,
) -> AccessLogEntry:
    """Build an entry with sensible RFC-reserved defaults."""
    return AccessLogEntry(
        peer_id=peer_id,
        topic_hash=topic_hash,
        timestamp=timestamp or datetime(2026, 7, 17, 15, 30, 0, tzinfo=UTC),
        project_scope=project_scope,
        trigger_code=trigger_code,
        record_ids_accessed=record_ids if record_ids is not None else ["fed:A:B:uuid1"],
    )


@pytest.fixture()
def log(tmp_path: Path) -> FederationAccessLog:
    """A fresh access log pointing at a tmp file."""
    return FederationAccessLog(path=tmp_path / "federation-access.jsonl")


# ── hash_topic ────────────────────────────────────────────────────────────────


def test_hash_topic_is_sha256_hex() -> None:
    """hash_topic returns a 64-char SHA-256 hex digest, not plaintext."""
    digest = hash_topic(TOPIC_X)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_hash_topic_is_deterministic() -> None:
    """Same topic → same digest (anti-correlation matching relies on this)."""
    assert hash_topic(TOPIC_X) == hash_topic(TOPIC_X)


def test_hash_topic_different_topics_differ() -> None:
    """Different topics → different digests."""
    assert hash_topic(TOPIC_X) != hash_topic(TOPIC_Y)


def test_hash_topic_handles_unicode() -> None:
    """Non-ASCII topics hash without raising (UTF-8 encoded)."""
    digest = hash_topic("корреляция запросов")
    assert len(digest) == 64


# ── append + query ───────────────────────────────────────────────────────────


def test_append_creates_file_and_writes_jsonl_line(log: FederationAccessLog) -> None:
    """append() creates the file and writes one JSON line."""
    log.append(_entry())
    assert log.path.exists()
    content = log.path.read_text(encoding="utf-8")
    lines = content.splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["peer_id"] == PEER_A
    assert parsed["trigger_code"] == "EXHAUSTIVE"


def test_append_writes_multiple_lines(log: FederationAccessLog) -> None:
    """Repeated appends produce one JSON line each (append-only)."""
    for i in range(3):
        log.append(_entry(record_ids=[f"fed:A:B:uuid{i}"]))
    lines = log.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3


def test_query_returns_most_recent_for_peer_topic(log: FederationAccessLog) -> None:
    """query() returns the latest entry for a (peer, topic_hash) pair."""
    t0 = datetime(2026, 7, 17, 15, 30, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=5)
    t2 = t0 + timedelta(minutes=10)
    log.append(_entry(timestamp=t0))
    log.append(_entry(timestamp=t1, trigger_code=TriggerCode.PARTIAL))
    log.append(_entry(timestamp=t2, trigger_code=TriggerCode.ALREADY_EXHAUSTED))
    result = log.query(PEER_A, TOPIC_X_HASH)
    assert result is not None
    assert result.timestamp == t2
    assert result.trigger_code == TriggerCode.ALREADY_EXHAUSTED


def test_query_returns_none_when_no_match(log: FederationAccessLog) -> None:
    """query() returns None when no prior entry exists for the pair."""
    log.append(_entry(peer_id=PEER_A, topic_hash=TOPIC_X_HASH))
    result = log.query(PEER_B, TOPIC_X_HASH)
    assert result is None
    result = log.query(PEER_A, TOPIC_Y_HASH)
    assert result is None


def test_query_on_empty_log_returns_none(log: FederationAccessLog) -> None:
    """query() on a log file that does not exist yet returns None."""
    assert not log.path.exists()
    assert log.query(PEER_A, TOPIC_X_HASH) is None


# ── query_recent ─────────────────────────────────────────────────────────────


def test_query_recent_filters_by_peer_and_since(log: FederationAccessLog) -> None:
    """query_recent() returns only entries for the peer at or after `since`."""
    base = datetime(2026, 7, 17, 15, 0, 0, tzinfo=UTC)
    log.append(_entry(peer_id=PEER_A, timestamp=base))
    log.append(_entry(peer_id=PEER_A, timestamp=base + timedelta(hours=1)))
    log.append(_entry(peer_id=PEER_B, timestamp=base + timedelta(hours=2)))
    cutoff = base + timedelta(minutes=30)
    results = log.query_recent(PEER_A, since=cutoff)
    assert len(results) == 1
    assert results[0].timestamp == base + timedelta(hours=1)


def test_query_recent_returns_all_when_since_is_old(log: FederationAccessLog) -> None:
    """A `since` far in the past returns every entry for the peer."""
    base = datetime(2026, 7, 17, 15, 0, 0, tzinfo=UTC)
    log.append(_entry(peer_id=PEER_A, timestamp=base))
    log.append(_entry(peer_id=PEER_A, timestamp=base + timedelta(hours=1)))
    results = log.query_recent(PEER_A, since=base - timedelta(days=1))
    assert len(results) == 2


# ── count_by_trigger_code ────────────────────────────────────────────────────


def test_count_by_trigger_code_aggregates(log: FederationAccessLog) -> None:
    """count_by_trigger_code() returns zero-filled counts per code."""
    base = datetime(2026, 7, 17, 15, 0, 0, tzinfo=UTC)
    log.append(_entry(trigger_code=TriggerCode.EXHAUSTIVE, timestamp=base))
    log.append(_entry(trigger_code=TriggerCode.EXHAUSTIVE, timestamp=base + timedelta(minutes=1)))
    log.append(_entry(trigger_code=TriggerCode.PARTIAL, timestamp=base + timedelta(minutes=2)))
    log.append(_entry(trigger_code=TriggerCode.REFUSED, timestamp=base + timedelta(minutes=3)))
    counts = log.count_by_trigger_code(PEER_A, since=base - timedelta(hours=1))
    assert counts[TriggerCode.EXHAUSTIVE] == 2
    assert counts[TriggerCode.PARTIAL] == 1
    assert counts[TriggerCode.REFUSED] == 1
    assert counts[TriggerCode.ALREADY_EXHAUSTED] == 0
    assert counts[TriggerCode.OFFLINE_LITE] == 0
    # All five codes present (zero-filled)
    assert set(counts.keys()) == set(TriggerCode)


def test_count_by_trigger_code_filters_peer(log: FederationAccessLog) -> None:
    """count_by_trigger_code() only counts the named peer."""
    base = datetime(2026, 7, 17, 15, 0, 0, tzinfo=UTC)
    log.append(_entry(peer_id=PEER_A, trigger_code=TriggerCode.EXHAUSTIVE, timestamp=base))
    log.append(_entry(peer_id=PEER_B, trigger_code=TriggerCode.EXHAUSTIVE, timestamp=base))
    counts_a = log.count_by_trigger_code(PEER_A, since=base - timedelta(hours=1))
    assert counts_a[TriggerCode.EXHAUSTIVE] == 1


# ── Privacy: no plaintext topic ──────────────────────────────────────────────


def test_no_plaintext_topic_in_log_file(log: FederationAccessLog) -> None:
    """The JSONL file stores topic_hash, never the plaintext topic."""
    log.append(_entry(topic_hash=TOPIC_X_HASH))
    content = log.path.read_text(encoding="utf-8")
    assert TOPIC_X not in content
    assert "topic_hash" in content
    assert TOPIC_X_HASH in content


def test_entry_does_not_carry_plaintext_topic() -> None:
    """AccessLogEntry has no field for the plaintext topic — only topic_hash."""
    fields = set(AccessLogEntry.model_fields.keys())
    assert "topic_hash" in fields
    assert "topic" not in fields
    assert "query" not in fields


# ── Frozen + append-only ─────────────────────────────────────────────────────


def test_entry_is_frozen() -> None:
    """AccessLogEntry is frozen — an audit trail cannot be mutated."""
    entry = _entry()
    with pytest.raises((pydantic.ValidationError, AttributeError)):
        entry.peer_id = PEER_B  # type: ignore[misc]


# ── Malformed JSONL line (CR#4) ──────────────────────────────────────────────


def test_malformed_line_raises_value_error_with_context(
    log: FederationAccessLog, tmp_path: Path
) -> None:
    """A malformed JSONL line makes query() raise ValueError with line context.

    A corrupted audit log is a signal, not something to silently swallow
    (per ``lint-and-validate.instructions.md``: fix the cause, do not
    suppress). The error message must include the line number so the
    operator can locate the corrupt line.
    """
    # Write one valid entry, then a malformed line, then another valid entry.
    log.append(_entry(record_ids=["fed:A:B:uuid1"]))
    with open(log.path, "a", encoding="utf-8") as fh:
        fh.write("{not valid json at all}\n")
    log.append(_entry(record_ids=["fed:A:B:uuid3"]))

    with pytest.raises(ValueError) as exc_info:
        log.query(PEER_A, TOPIC_X_HASH)

    # The error must carry line context so the operator can locate it.
    msg = str(exc_info.value)
    assert "malformed line" in msg
    assert "2" in msg  # the malformed line is line 2


def test_malformed_line_raises_for_query_recent(
    log: FederationAccessLog,
) -> None:
    """query_recent() also raises on a malformed line (same _iter_entries path)."""
    log.append(_entry())
    with open(log.path, "a", encoding="utf-8") as fh:
        fh.write("<<<garbage>>>\n")
    with pytest.raises(ValueError) as exc_info:
        log.query_recent(PEER_A, since=datetime(2020, 1, 1, tzinfo=UTC))
    assert "malformed line" in str(exc_info.value)


def test_malformed_line_raises_for_count_by_trigger_code(
    log: FederationAccessLog,
) -> None:
    """count_by_trigger_code() also raises on a malformed line."""
    log.append(_entry())
    with open(log.path, "a", encoding="utf-8") as fh:
        fh.write("not-json\n")
    with pytest.raises(ValueError) as exc_info:
        log.count_by_trigger_code(PEER_A, since=datetime(2020, 1, 1, tzinfo=UTC))
    assert "malformed line" in str(exc_info.value)


# ── Concurrent appends ───────────────────────────────────────────────────────


def test_concurrent_appends_do_not_corrupt_lines(log: FederationAccessLog) -> None:
    """Concurrent appends from many threads produce one valid JSON line each."""
    n_threads = 20
    n_per_thread = 10
    barrier = threading.Barrier(n_threads)

    def worker(tid: int) -> None:
        barrier.wait()
        for i in range(n_per_thread):
            log.append(
                _entry(
                    peer_id=f"peer-{tid}",
                    record_ids=[f"rec-{tid}-{i}"],
                    topic_hash=hash_topic(f"topic-{tid}-{i}"),
                )
            )

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = log.path.read_text(encoding="utf-8").splitlines()
    # Every line is valid JSON and parses back to an AccessLogEntry.
    assert len(lines) == n_threads * n_per_thread
    for line in lines:
        parsed = AccessLogEntry.model_validate_json(line)
        assert parsed.peer_id.startswith("peer-")
