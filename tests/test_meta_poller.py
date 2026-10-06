"""Tests for the S2 phase 2 meta-poller (`vesmaro.meta_poller`).

Coverage map (task brief):

* config — default-off, interval clamp, peers validation, old configs
  parse unchanged;
* store — ``federation_poll_state`` watermark round-trip (ok advances
  the rev + clears the error, error keeps the watermark);
* parser — the mesh-CLI JSON envelope contract (extra fields ignored,
  bad envelope rejected), per-record tolerance;
* poller — watermark resume via ``--since``, has_more pagination, page
  cap, peer failure contained (last_error + loop continues), upsert
  called with the authenticated ``sender_peer_id``, batch-200 chunking;
* CLI — ``mnemos meta-poll`` happy path + failure exits;
* serve wiring — the FastAPI lifespan starts/stops the poller only
  when ``federation.meta_poll.enabled``.

The mesh CLI (Go track, parallel) does not exist yet — the poller is
exercised through its injection point ``meta_poll.mesh_bin``: a Python
script double driven through the REAL subprocess machinery
(``asyncio.create_subprocess_exec``), so argv construction, exit-code
handling, stdout JSON parsing and the timeout path are all covered
against real OS processes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError
from typer.testing import CliRunner

from vesmaro.cli._manager import reset_manager
from vesmaro.cli.main import app
from vesmaro.config import FederationConfig, MetaPollConfig, PeerConfig
from vesmaro.manager import MemoryManager
from vesmaro.meta_poller import (
    META_POLL_BATCH,
    META_POLL_MAX_PAGES,
    META_POLL_PAGE_TIMEOUT_S,
    MetaPoller,
    MetaPollError,
    PeerPollResult,
    SyncMetaPage,
    _parse_sync_meta_stdout,
)
from vesmaro.storage.sqlite_store import SQLiteStore

runner = CliRunner()

#: The script double — installed as ``mesh_bin`` so the poller spawns a
#: REAL subprocess. Behaviour per peer is driven by the JSON state file
#: (env ``MNEMOS_TEST_MESH_STATE``): ``pages`` (consumed one per call),
#: ``loop_page`` (same page forever — cap tests), ``fail``/``fail_code``
#: (non-zero exit + stderr), ``bad_json`` (garbage stdout — a string is
#: printed verbatim so tests can shape the garbage, e.g. a broken
#: ``{``-opening fragment), ``noise`` (N INFO lines printed to STDOUT
#: before the page JSON — the noisy-stdout hardening), ``sleep_s``
#: (timeout tests; the double records its pid into ``pids`` BEFORE
#: sleeping so tests can assert the child's fate). Every invocation
#: records its argv into ``calls`` and the ``--since`` value into
#: ``seen_since`` for assertions.
FAKE_MESH_CLI = '''#!/usr/bin/env python3
"""Script double for `mnemos-mesh sync-meta` (test fixture)."""
import json
import os
import sys
import time


def main() -> int:
    argv = sys.argv[1:]
    state_path = os.environ["MNEMOS_TEST_MESH_STATE"]
    with open(state_path) as f:
        state = json.load(f)
    peer = argv[argv.index("--peer") + 1] if "--peer" in argv else ""
    since = argv[argv.index("--since") + 1] if "--since" in argv else None
    state.setdefault("calls", []).append(argv)
    state.setdefault("seen_since", []).append(since)
    spec = state.get("peers", {}).get(peer, {})

    if spec.get("fail"):
        with open(state_path, "w") as f:
            json.dump(state, f)
        sys.stderr.write(str(spec["fail"]))
        return int(spec.get("fail_code", 3))
    # Noisy stdout (hardening tests): INFO chatter BEFORE the envelope.
    for i in range(int(spec.get("noise") or 0)):
        sys.stdout.write(f"INFO 2026-09-21T10:00:00Z mesh leg chatter line {i}\\n")
    if spec.get("bad_json"):
        with open(state_path, "w") as f:
            json.dump(state, f)
        bad = spec["bad_json"]
        sys.stdout.write(bad if isinstance(bad, str) else "this is not json")
        return 0
    if spec.get("sleep_s"):
        state.setdefault("pids", []).append(os.getpid())
        with open(state_path, "w") as f:
            json.dump(state, f)
        time.sleep(float(spec["sleep_s"]))

    if "loop_page" in spec:
        page = dict(spec["loop_page"])
    elif spec.get("pages"):
        page = spec["pages"].pop(0)
    else:
        page = {"records": [], "latest_rev": int(since or 0), "has_more": False}
    with open(state_path, "w") as f:
        json.dump(state, f)
    sys.stdout.write(json.dumps(page))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _record(i: int, *, peer: str = "peer-a", **overrides: Any) -> dict[str, Any]:
    """One valid wire MetadataRecord."""
    base: dict[str, Any] = {
        "id": f"fed:agent:{i:04d}",
        "type": "decision",
        "title": f"Record {i}",
        "tags": ["project:demo", "kind:decision"],
        "project": "demo",
        "source_agent": "agent",
        "source_peer": peer,
        "origin_peer": "self",
        "content_state": "available",
        "timestamp": "2026-09-21T10:00:00Z",
        "schema_version": "mnemos.federation.metadata.v1",
    }
    base.update(overrides)
    return base


def _page(records: list[dict[str, Any]], latest_rev: int, has_more: bool = False) -> dict[str, Any]:
    return {"records": records, "latest_rev": latest_rev, "has_more": has_more}


class MeshDouble:
    """Fixture handle: install the fake CLI + its JSON state file."""

    def __init__(self, tmp_path: Path) -> None:
        self.bin_path = tmp_path / "fake-mnemos-mesh"
        self.bin_path.write_text(FAKE_MESH_CLI)
        self.bin_path.chmod(0o755)
        self.state_path = tmp_path / "mesh-state.json"
        self.state: dict[str, Any] = {"peers": {}}
        self._flush()
        self.env = {**os.environ, "MNEMOS_TEST_MESH_STATE": str(self.state_path)}

    def _flush(self) -> None:
        self.state_path.write_text(json.dumps(self.state))

    def set_peer(self, peer_id: str, spec: dict[str, Any]) -> None:
        self.state["peers"][peer_id] = spec
        self._flush()

    def reload(self) -> dict[str, Any]:
        self.state = json.loads(self.state_path.read_text())
        return self.state

    @property
    def calls(self) -> list[list[str]]:
        return self.reload().get("calls", [])

    @property
    def seen_since(self) -> list[str | None]:
        return self.reload().get("seen_since", [])


def make_federation(
    mesh: MeshDouble,
    peers: tuple[str, ...] = ("peer-a",),
    poll_peers: list[str] | None = None,
    **meta_poll_overrides: Any,
) -> FederationConfig:
    """A FederationConfig wired to the script double.

    ``poll_peers`` sets an EXPLICIT ``meta_poll.peers`` list (subset
    polling); ``None`` leaves the ``"all"`` default.
    """
    meta_poll: dict[str, Any] = {
        "enabled": True,
        "mesh_bin": str(mesh.bin_path),
        "mesh_config_path": "/nonexistent/mesh.yaml",  # the double ignores it
        **meta_poll_overrides,
    }
    if poll_peers is not None:
        meta_poll["peers"] = poll_peers
    return FederationConfig(
        peers={p: PeerConfig(bearer_token_env="VESMARO_TEST_TOKEN") for p in peers},
        meta_poll=meta_poll,
    )


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    s = SQLiteStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def mesh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MeshDouble:
    """Script double with its env var exported for spawned subprocesses."""
    double = MeshDouble(tmp_path)
    monkeypatch.setenv("MNEMOS_TEST_MESH_STATE", str(double.state_path))
    return double


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ── Config ────────────────────────────────────────────────────────────────────


class TestMetaPollConfig:
    def test_defaults_are_off_and_safe(self) -> None:
        cfg = MetaPollConfig()
        assert cfg.enabled is False
        assert cfg.interval_seconds == 300
        assert cfg.peers == "all"
        assert cfg.mesh_config_path == ""
        assert cfg.mesh_bin == "mnemos-mesh"

    def test_old_configs_parse_unchanged(self) -> None:
        """No ``meta_poll`` key → default section, bit-for-bit phase 1."""
        fed = FederationConfig.model_validate(
            {"shared_projects": ["demo"], "peers": {"a": {"bearer_token_env": "X"}}}
        )
        assert fed.meta_poll == MetaPollConfig()

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(1, 60), (30, 60), (60, 60), (300, 300), (90_000, 86_400)],
    )
    def test_interval_clamped_not_rejected(self, raw: int, expected: int) -> None:
        assert MetaPollConfig(interval_seconds=raw).interval_seconds == expected

    def test_peers_string_must_be_all(self) -> None:
        with pytest.raises(ValidationError, match="string form must be 'all'"):
            MetaPollConfig(peers="some")

    def test_peers_blank_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="blank peer id"):
            MetaPollConfig(peers=["a", "  "])

    def test_enabled_requires_mesh_config_path(self) -> None:
        with pytest.raises(ValidationError, match="mesh_config_path"):
            MetaPollConfig(enabled=True)

    def test_unknown_explicit_peer_is_a_config_error(self) -> None:
        with pytest.raises(ValidationError, match="ghost"):
            FederationConfig(
                peers={"a": {"bearer_token_env": "X"}},
                meta_poll={"peers": ["a", "ghost"]},
            )

    def test_explicit_known_peers_accepted(self) -> None:
        fed = FederationConfig(
            peers={"a": {"bearer_token_env": "X"}, "b": {"bearer_token_env": "Y"}},
            meta_poll={"peers": ["b"]},
        )
        assert fed.meta_poll.peers == ["b"]

    def test_peers_all_tracks_federation_map(self, tmp_path: Path) -> None:
        federation = FederationConfig(
            peers={"b": {"bearer_token_env": "Y"}, "a": {"bearer_token_env": "X"}}
        )
        store = SQLiteStore(tmp_path / "peerlist.db")
        try:
            poller = MetaPoller(store, federation)
            assert poller.peer_ids() == ["a", "b"]  # deterministic order
        finally:
            store.close()


# ── Envelope parser ───────────────────────────────────────────────────────────


class TestSyncMetaPage:
    def test_contract_shape(self) -> None:
        page = SyncMetaPage.model_validate(
            {"records": [_record(1)], "latest_rev": 5, "has_more": True}
        )
        assert page.latest_rev == 5
        assert page.has_more is True
        assert len(page.records) == 1

    def test_extra_fields_ignored_forward_compat(self) -> None:
        page = SyncMetaPage.model_validate(
            {"records": [], "latest_rev": 1, "trigger_code": "EXHAUSTIVE"}
        )
        assert page.has_more is False  # missing has_more = stop paging (safe side)

    def test_negative_latest_rev_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SyncMetaPage.model_validate({"records": [], "latest_rev": -1})

    def test_records_kept_raw_for_per_record_tolerance(self) -> None:
        bad = _record(1, content_state="nuked")  # unknown content_state
        page = SyncMetaPage.model_validate({"records": [bad], "latest_rev": 2})
        entries, invalid = MetaPoller._parse_entries("peer-a", page.records)
        assert invalid == 1 and entries == []


class TestEnvelopeExtraction:
    """Noise-hardening of :func:`meta_poller._parse_sync_meta_stdout`.

    The poller must survive a noisy stdout even though the mesh track
    moved its logs to stderr: (a) whole-stdout JSON, (b) first ``{``-
    opening line accumulated to EOF, (c) honest error with the tail.
    """

    def test_strategy_a_whole_stdout_parses(self) -> None:
        page = _parse_sync_meta_stdout("peer-a", b'{"records": [], "latest_rev": 3}')
        assert page.latest_rev == 3 and not page.has_more

    def test_strategy_b_prefix_noise_envelope_last(self) -> None:
        stdout = (
            b"INFO 2026-09-21T10:00:00Z chatter line 0\n"
            b"INFO 2026-09-21T10:00:00Z chatter line 1\n"
            b"INFO 2026-09-21T10:00:00Z chatter line 2\n"
            b'{"records": [], "latest_rev": 9, "has_more": false}\n'
        )
        page = _parse_sync_meta_stdout("peer-a", stdout)
        assert page.latest_rev == 9 and page.records == []

    def test_strategy_c_no_brace_line_is_error_with_tail(self) -> None:
        with pytest.raises(MetaPollError, match="not valid JSON") as excinfo:
            _parse_sync_meta_stdout("peer-a", b"INFO line\nstill not json")
        assert "still not json" in str(excinfo.value)  # the tail is surfaced

    def test_trailing_noise_after_envelope_is_rejected(self) -> None:
        # The envelope must END the output — chatter after it means the
        # contract is broken; report, never guess.
        stdout = b'{"records": [], "latest_rev": 1}\nINFO trailing chatter'
        with pytest.raises(MetaPollError, match="not valid JSON"):
            _parse_sync_meta_stdout("peer-a", stdout)

    def test_broken_brace_fragment_is_error_with_tail(self) -> None:
        with pytest.raises(MetaPollError, match="not valid JSON") as excinfo:
            _parse_sync_meta_stdout("peer-a", b'INFO noise\n{"records": [')
        assert '{"records": [' in str(excinfo.value)

    def test_valid_json_wrong_shape_is_contract_error(self) -> None:
        with pytest.raises(MetaPollError, match="violates contract"):
            _parse_sync_meta_stdout("peer-a", b"[1, 2, 3]")

    def test_undecodable_bytes_surface_as_error_not_silence(self) -> None:
        with pytest.raises(MetaPollError):
            _parse_sync_meta_stdout("peer-a", b"\xff\xfe garbage")


# ── Store watermark ───────────────────────────────────────────────────────────


class TestPollStateStore:
    def test_absent_peer_starts_from_zero(self, store: SQLiteStore) -> None:
        assert store.get_poll_state("peer-a") is None

    def test_ok_advances_rev_and_clears_error(self, store: SQLiteStore) -> None:
        store.mark_poll_error("peer-a", "boom")
        store.mark_poll_ok("peer-a", 7)
        row = store.get_poll_state("peer-a")
        assert row is not None
        assert row.since_rev == 7
        assert row.last_error == ""
        assert row.last_ok_at  # stamped

    def test_error_keeps_watermark_and_last_ok(self, store: SQLiteStore) -> None:
        store.mark_poll_ok("peer-a", 9)
        ok_at = store.get_poll_state("peer-a").last_ok_at
        store.mark_poll_error("peer-a", "mesh leg down")
        row = store.get_poll_state("peer-a")
        assert row.since_rev == 9
        assert row.last_ok_at == ok_at
        assert row.last_error == "mesh leg down"

    def test_error_truncated_to_row_budget(self, store: SQLiteStore) -> None:
        store.mark_poll_error("peer-a", "x" * 10_000)
        assert len(store.get_poll_state("peer-a").last_error) == store.POLL_STATE_ERROR_MAX_LEN


# ── Poller via the real subprocess double ─────────────────────────────────────


class TestPollPeer:
    def test_happy_path_imports_and_logs(
        self, store: SQLiteStore, mesh: MeshDouble, caplog: pytest.LogCaptureFixture
    ) -> None:
        mesh.set_peer("peer-a", {"pages": [_page([_record(1), _record(2)], latest_rev=5)]})
        poller = MetaPoller(store, make_federation(mesh))
        with caplog.at_level(logging.INFO, logger="vesmaro.meta_poller"):
            result = run(poller.poll_peer("peer-a"))
        assert result.ok and result.fetched == 2 and result.accepted == 2
        assert result.latest_rev == 5 and result.pages == 1
        # Rows landed with the sender re-stamped as origin (import leg).
        rows = store.list_index(limit=10, exclude_no_federate=False)
        assert sorted(e.id for e, _ in rows) == ["fed:agent:0001", "fed:agent:0002"]
        assert all(e.origin_peer == "peer-a" for e, _ in rows)
        # Required log line format.
        assert any(
            re.fullmatch(
                r"meta poll peer=peer-a fetched=2 accepted=2 rejected_by_gate=0 stale=0 "
                r"latest_rev=5 next=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
                msg,
            )
            for msg in caplog.messages
        ), caplog.messages
        assert store.get_poll_state("peer-a").since_rev == 5

    def test_upsert_called_with_authenticated_sender(
        self, store: SQLiteStore, mesh: MeshDouble, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mesh.set_peer("peer-a", {"pages": [_page([_record(1)], latest_rev=1)]})
        senders: list[str | None] = []
        real = store.upsert_index_entries

        def spy(entries: Any, **kwargs: Any) -> Any:
            senders.append(kwargs.get("sender_peer_id"))
            return real(entries, **kwargs)

        monkeypatch.setattr(store, "upsert_index_entries", spy)
        run(MetaPoller(store, make_federation(mesh)).poll_peer("peer-a"))
        assert senders == ["peer-a"]

    def test_first_poll_omits_since_then_resumes(
        self, store: SQLiteStore, mesh: MeshDouble
    ) -> None:
        mesh.set_peer(
            "peer-a",
            {
                "pages": [
                    _page([_record(1)], latest_rev=3, has_more=True),
                    _page([_record(2)], latest_rev=6),
                ]
            },
        )
        poller = MetaPoller(store, make_federation(mesh))
        run(poller.poll_peer("peer-a"))
        assert mesh.seen_since == [None, "3"]  # first call no --since, second --since 3
        assert store.get_poll_state("peer-a").since_rev == 6
        # Next tick: CLI gets the persisted watermark; empty page keeps it.
        result = run(poller.poll_peer("peer-a"))
        assert result.ok and result.fetched == 0 and result.latest_rev == 6
        assert mesh.seen_since[-1] == "6"

    def test_watermark_never_regresses(self, store: SQLiteStore, mesh: MeshDouble) -> None:
        mesh.set_peer("peer-a", {"loop_page": _page([], latest_rev=1, has_more=False)})
        store.mark_poll_ok("peer-a", 100)  # pre-existing higher checkpoint
        result = run(MetaPoller(store, make_federation(mesh)).poll_peer("peer-a"))
        assert result.ok and result.latest_rev == 100
        assert store.get_poll_state("peer-a").since_rev == 100

    def test_has_more_pages_until_exhaustion(self, store: SQLiteStore, mesh: MeshDouble) -> None:
        pages = [_page([_record(i)], latest_rev=i + 1, has_more=True) for i in range(1, 4)]
        pages.append(_page([_record(4)], latest_rev=5))
        mesh.set_peer("peer-a", {"pages": pages})
        result = run(MetaPoller(store, make_federation(mesh)).poll_peer("peer-a"))
        assert result.pages == 4 and result.fetched == 4 and result.latest_rev == 5
        assert len([c for c in mesh.calls if "sync-meta" in c]) == 4

    def test_page_cap_resumes_next_tick(
        self, store: SQLiteStore, mesh: MeshDouble, caplog: pytest.LogCaptureFixture
    ) -> None:
        mesh.set_peer("peer-a", {"loop_page": _page([_record(1)], latest_rev=9, has_more=True)})
        poller = MetaPoller(store, make_federation(mesh))
        with caplog.at_level(logging.INFO, logger="vesmaro.meta_poller"):
            result = run(poller.poll_peer("peer-a"))
        assert result.pages == META_POLL_MAX_PAGES
        assert len(mesh.calls) == META_POLL_MAX_PAGES
        assert store.get_poll_state("peer-a").since_rev == 9
        assert any("page cap reached" in m for m in caplog.messages)

    def test_batches_of_200(
        self, store: SQLiteStore, mesh: MeshDouble, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        records = [_record(i) for i in range(META_POLL_BATCH * 2 + 50)]
        mesh.set_peer("peer-a", {"pages": [_page(records, latest_rev=1)]})
        calls: list[int] = []
        real = store.upsert_index_entries

        def spy(entries: Any, **kwargs: Any) -> Any:
            calls.append(len(entries))
            return real(entries, **kwargs)

        monkeypatch.setattr(store, "upsert_index_entries", spy)
        result = run(MetaPoller(store, make_federation(mesh)).poll_peer("peer-a"))
        assert calls == [META_POLL_BATCH, META_POLL_BATCH, 50]
        assert result.accepted == 450

    def test_invalid_record_counted_as_gate_rejection(
        self, store: SQLiteStore, mesh: MeshDouble
    ) -> None:
        page = _page([_record(1), _record(2, content_state="nuked"), _record(3)], latest_rev=1)
        mesh.set_peer("peer-a", {"pages": [page]})
        result = run(MetaPoller(store, make_federation(mesh)).poll_peer("peer-a"))
        assert result.fetched == 3 and result.accepted == 2
        assert result.rejected_by_gate == 1

    def test_nonzero_exit_is_honest_error(
        self, store: SQLiteStore, mesh: MeshDouble, caplog: pytest.LogCaptureFixture
    ) -> None:
        mesh.set_peer("peer-a", {"fail": "mesh leg down", "fail_code": 3})
        store.mark_poll_ok("peer-a", 4)  # pre-existing watermark must survive
        with caplog.at_level(logging.INFO, logger="vesmaro.meta_poller"):
            result = run(MetaPoller(store, make_federation(mesh)).poll_peer("peer-a"))
        assert result.error is not None and "exited 3" in result.error
        assert store.get_poll_state("peer-a").since_rev == 4
        assert store.get_poll_state("peer-a").last_error == result.error
        assert any("meta poll peer=peer-a error=" in m for m in caplog.messages)

    def test_broken_json_is_honest_error(self, store: SQLiteStore, mesh: MeshDouble) -> None:
        mesh.set_peer("peer-a", {"bad_json": True})
        result = run(MetaPoller(store, make_federation(mesh)).poll_peer("peer-a"))
        assert result.error is not None and "not valid JSON" in result.error
        assert store.get_poll_state("peer-a").last_error

    def test_noisy_stdout_envelope_still_extracted(
        self, store: SQLiteStore, mesh: MeshDouble
    ) -> None:
        """3 INFO lines printed to stdout BEFORE the JSON (a CLI that
        missed the logs-to-stderr fix): the poller extracts the envelope
        (strategy b) and the page imports normally."""
        mesh.set_peer("peer-a", {"noise": 3, "pages": [_page([_record(1)], latest_rev=7)]})
        result = run(MetaPoller(store, make_federation(mesh)).poll_peer("peer-a"))
        assert result.ok and result.fetched == 1 and result.accepted == 1
        assert result.latest_rev == 7
        assert store.get_poll_state("peer-a").since_rev == 7
        assert [e.id for e, _ in store.list_index(limit=10)] == ["fed:agent:0001"]

    def test_noisy_stdout_broken_json_is_honest_error(
        self, store: SQLiteStore, mesh: MeshDouble
    ) -> None:
        """Noise + a broken ``{``-opening JSON fragment: extraction (b)
        fails, the error is honest and carries the stdout tail."""
        mesh.set_peer("peer-a", {"noise": 3, "bad_json": '{"records": ['})
        result = run(MetaPoller(store, make_federation(mesh)).poll_peer("peer-a"))
        assert result.error is not None and "not valid JSON" in result.error
        assert '{"records": [' in result.error  # the tail is in last_error
        assert store.get_poll_state("peer-a").last_error == result.error
        assert store.get_poll_state("peer-a").since_rev == 0  # watermark untouched

    def test_missing_binary_is_honest_error(self, store: SQLiteStore, tmp_path: Path) -> None:
        federation = FederationConfig(
            peers={"peer-a": PeerConfig(bearer_token_env="X")},
            meta_poll={
                "enabled": True,
                "mesh_bin": str(tmp_path / "no-such-binary"),
                "mesh_config_path": "/nonexistent/mesh.yaml",
            },
        )
        result = run(MetaPoller(store, federation).poll_peer("peer-a"))
        assert result.error is not None and "cannot execute mesh bin" in result.error

    def test_subprocess_timeout_kills_and_errors(
        self, store: SQLiteStore, mesh: MeshDouble, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import vesmaro.meta_poller as mp

        monkeypatch.setattr(mp, "META_POLL_PAGE_TIMEOUT_S", 0.3)
        mesh.set_peer("peer-a", {"sleep_s": 5, "pages": []})
        result = run(MetaPoller(store, make_federation(mesh)).poll_peer("peer-a"))
        assert result.error is not None and "timed out" in result.error

    def test_timeout_kill_race_with_external_reaper_is_honest_error(
        self, store: SQLiteStore, mesh: MeshDouble, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #510 side note: under ``service run`` the supervisor's
        reaper thread drains ``waitpid(-1, WNOHANG)`` and may reap the
        timed-out child before the poller's kill — the OLD bare
        ``proc.kill()`` surfaced ``ProcessLookupError("[Errno 3] No such
        process")`` as a bogus peer error. The kill is now best-effort:
        the honest ``timed out`` error stands either way, no ESRCH
        escapes."""
        import threading

        import vesmaro.meta_poller as mp

        # A supervisor-shaped reaper: drain waitpid(-1, WNOHANG) in a loop.
        reaping = threading.Event()

        def reaper() -> None:
            while not reaping.is_set():
                try:
                    pid, _status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    continue
                if pid == 0:
                    reaping.wait(0.01)
                    continue
                return  # a child was reaped — the race materialized

        thread = threading.Thread(target=reaper, daemon=True)
        thread.start()
        try:
            monkeypatch.setattr(mp, "META_POLL_PAGE_TIMEOUT_S", 0.3)
            mesh.set_peer("peer-a", {"sleep_s": 5, "pages": []})
            result = run(MetaPoller(store, make_federation(mesh)).poll_peer("peer-a"))
        finally:
            reaping.set()
            thread.join(timeout=2)
            # Drain anything left behind so the suite process stays
            # zombie-free (pytest here is not a PID 1 with an auto-reaper).
            while True:
                try:
                    pid, _status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if pid == 0:
                    break
        assert result.error is not None and "timed out" in result.error
        assert "No such process" not in (result.error or "")


class TestTickLoop:
    def test_peer_error_does_not_kill_the_tick(self, store: SQLiteStore, mesh: MeshDouble) -> None:
        mesh.set_peer("peer-a", {"fail": "down"})
        mesh.set_peer("peer-b", {"pages": [_page([_record(1, peer="peer-b")], latest_rev=2)]})
        poller = MetaPoller(store, make_federation(mesh, peers=("peer-a", "peer-b")))
        results = run(poller.tick())
        by_peer = {r.peer_id: r for r in results}
        assert by_peer["peer-a"].error is not None
        assert by_peer["peer-b"].ok and by_peer["peer-b"].accepted == 1
        assert store.get_poll_state("peer-b").since_rev == 2

    def test_explicit_peer_subset_polled_only(self, store: SQLiteStore, mesh: MeshDouble) -> None:
        mesh.set_peer("peer-a", {"pages": []})
        mesh.set_peer("peer-b", {"pages": []})
        poller = MetaPoller(
            store,
            make_federation(mesh, peers=("peer-a", "peer-b"), poll_peers=["peer-a"]),
        )
        results = run(poller.tick())
        assert [r.peer_id for r in results] == ["peer-a"]

    def test_run_loop_survives_tick_exception_and_stops(
        self, store: SQLiteStore, mesh: MeshDouble
    ) -> None:
        mesh.set_peer("peer-a", {"pages": []})
        poller = MetaPoller(store, make_federation(mesh))
        real_tick = poller.tick
        ticks: list[int] = []

        async def flaky_tick() -> list[PeerPollResult]:
            ticks.append(1)
            if len(ticks) == 1:
                raise RuntimeError("unexpected tick failure")
            return await real_tick()

        poller.tick = flaky_tick  # type: ignore[method-assign]

        async def scenario() -> None:
            poller.start()
            await asyncio.sleep(0.4)  # first tick raises
            assert poller.running  # the loop survived
            await poller.stop()
            assert not poller.running

        run(scenario())
        assert ticks  # at least the raising tick ran

    def test_stop_wakes_interval_sleep(self, store: SQLiteStore, mesh: MeshDouble) -> None:
        mesh.set_peer("peer-a", {"pages": []})
        poller = MetaPoller(store, make_federation(mesh, interval_seconds=86_400))

        async def scenario() -> None:
            poller.start()
            await asyncio.sleep(0.4)
            assert poller.running
            await poller.stop()  # must not wait 24h
            assert not poller.running

        run(scenario())

    def test_stop_grace_cancel_kills_inflight_child(
        self, store: SQLiteStore, mesh: MeshDouble
    ) -> None:
        """Review thread: a stop() whose grace expires MID-``communicate()``
        must KILL the mesh CLI child, not merely cancel the await — a
        cancelled ``communicate()`` tears down the pipe readers but
        leaves the subprocess running until its own 30s page timeout.

        The double sleeps 60s and records its pid before sleeping, so
        the test observes the child's fate directly.
        """
        mesh.set_peer("peer-a", {"sleep_s": 60, "pages": []})
        poller = MetaPoller(store, make_federation(mesh))

        async def scenario() -> tuple[int, float] | None:
            poller.start()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 5
            pid: int | None = None
            while loop.time() < deadline:
                pids = mesh.reload().get("pids", [])
                if pids:
                    pid = pids[-1]
                    break
                await asyncio.sleep(0.05)
            assert pid is not None, "mesh double never recorded its pid (child never spawned?)"
            t0 = loop.time()
            await poller.stop(grace_s=0.3)
            elapsed = loop.time() - t0
            assert elapsed < META_POLL_PAGE_TIMEOUT_S  # did not ride out the page timeout
            assert not poller.running
            # Give the loop a moment to reap the killed child (SIGCHLD).
            for _ in range(30):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return pid, elapsed  # dead AND reaped
                await asyncio.sleep(0.1)
            return pid, elapsed

        outcome = run(scenario())
        assert outcome is not None
        pid, _elapsed = outcome
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)  # the child is gone — not orphaned for 60s


# ── CLI ───────────────────────────────────────────────────────────────────────


def _cli_config(tmp_path: Path, mesh: MeshDouble, *, enable: bool = True) -> Path:
    cfg: dict[str, Any] = {
        "mnemos": {
            "data_dir": str(tmp_path / "data"),
            "vault_path": str(tmp_path / "vault"),
            "db_name": "meta-poll-cli.db",
        },
        "embedding": {"provider": "nano"},
        "federation": {
            "peers": {"peer-a": {"bearer_token_env": "VESMARO_TEST_TOKEN"}},
            "meta_poll": {
                "enabled": enable,
                "mesh_bin": str(mesh.bin_path),
                "mesh_config_path": str(tmp_path / "mesh.yaml"),
            },
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


class TestMetaPollCLI:
    @pytest.fixture(autouse=True)
    def _reset(self) -> Any:
        """Reset the CLI manager singleton AND snapshot root logging.

        The meta-poll command calls ``setup_logging()`` which clears
        root handlers — without the restore, the NEXT test's caplog
        (e.g. test_pipeline_state's audit assertions) silently loses
        records. Same pattern as ``tests/test_serve_mesh_wiring.py``.
        """
        import logging

        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        saved_level = root.level
        reset_manager()
        yield
        reset_manager()
        root.handlers = saved_handlers
        root.setLevel(saved_level)

    def test_one_shot_poll_prints_summary(self, tmp_path: Path, mesh: MeshDouble) -> None:
        mesh.set_peer("peer-a", {"pages": [_page([_record(1), _record(2)], latest_rev=5)]})
        cfg = _cli_config(tmp_path, mesh)
        result = runner.invoke(app, ["meta-poll", "--peer", "peer-a", "--config", str(cfg)])
        assert result.exit_code == 0, result.output
        assert "peer=peer-a fetched=2 accepted=2" in result.output
        assert "latest_rev=5" in result.output
        assert "summary peers=1 failed=0" in result.output
        # The watermark and the imported rows live in the configured DB.
        store = SQLiteStore(tmp_path / "data" / "meta-poll-cli.db")
        try:
            assert store.get_poll_state("peer-a").since_rev == 5
            assert len(store.list_index(limit=10, exclude_no_federate=False)) == 2
        finally:
            store.close()

    def test_missing_mesh_config_path_exits_1(self, tmp_path: Path, mesh: MeshDouble) -> None:
        cfg = _cli_config(tmp_path, mesh)
        doc = yaml.safe_load(cfg.read_text())
        del doc["federation"]["meta_poll"]["mesh_config_path"]
        doc["federation"]["meta_poll"]["enabled"] = False  # manual run, unconfigured
        cfg.write_text(yaml.safe_dump(doc))
        result = runner.invoke(app, ["meta-poll", "--config", str(cfg)])
        assert result.exit_code == 1
        assert "mesh_config_path" in result.output

    def test_unknown_peer_exits_1(self, tmp_path: Path, mesh: MeshDouble) -> None:
        cfg = _cli_config(tmp_path, mesh)
        result = runner.invoke(app, ["meta-poll", "--peer", "ghost", "--config", str(cfg)])
        assert result.exit_code == 1
        assert "ghost" in result.output

    def test_no_peers_exits_1(self, tmp_path: Path, mesh: MeshDouble) -> None:
        cfg = _cli_config(tmp_path, mesh)
        doc = yaml.safe_load(cfg.read_text())
        doc["federation"]["peers"] = {}
        cfg.write_text(yaml.safe_dump(doc))
        result = runner.invoke(app, ["meta-poll", "--config", str(cfg)])
        assert result.exit_code == 1
        assert "no federation peers" in result.output

    def test_failing_peer_exits_1(self, tmp_path: Path, mesh: MeshDouble) -> None:
        mesh.set_peer("peer-a", {"fail": "mesh leg down"})
        cfg = _cli_config(tmp_path, mesh)
        result = runner.invoke(app, ["meta-poll", "--config", str(cfg)])
        assert result.exit_code == 1
        assert "error=" in result.output

    def test_disabled_background_still_allows_manual_run(
        self, tmp_path: Path, mesh: MeshDouble
    ) -> None:
        mesh.set_peer("peer-a", {"pages": [_page([_record(1)], latest_rev=1)]})
        cfg = _cli_config(tmp_path, mesh, enable=False)
        result = runner.invoke(app, ["meta-poll", "--config", str(cfg)])
        assert result.exit_code == 0, result.output
        assert "accepted=1" in result.output


# ── Serve (lifespan) wiring ───────────────────────────────────────────────────


class TestServeWiring:
    @pytest.fixture(autouse=True)
    def _poller_spy(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        """Replace MetaPoller with a lifecycle recorder."""
        events: dict[str, Any] = {"constructed": 0, "started": 0, "stopped": 0}

        class _SpyPoller:
            def __init__(self, store: Any, federation: Any) -> None:
                events["constructed"] += 1
                events["store"] = store
                events["federation"] = federation

            def start(self) -> None:
                events["started"] += 1

            async def stop(self, grace_s: float = 5.0) -> None:
                events["stopped"] += 1

        monkeypatch.setattr("vesmaro.meta_poller.MetaPoller", _SpyPoller)
        return events

    def _client_config(self, tmp_path: Path, *, enable: bool) -> Path:
        cfg: dict[str, Any] = {
            "mnemos": {
                "data_dir": str(tmp_path / "data"),
                "vault_path": str(tmp_path / "vault"),
            },
            "embedding": {"provider": "nano"},
            "federation": {
                "peers": {"peer-a": {"bearer_token_env": "VESMARO_TEST_TOKEN"}},
                "meta_poll": {
                    "enabled": enable,
                    "mesh_bin": "/nonexistent/mnemos-mesh",
                    "mesh_config_path": str(tmp_path / "mesh.yaml"),
                },
            },
        }
        path = tmp_path / "lifespan-config.yaml"
        path.write_text(yaml.safe_dump(cfg))
        return path

    @pytest.fixture
    def lifespan_app(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
        """Seed the api.main manager with an isolated real one."""
        from vesmaro.api import main as api_main
        from vesmaro.config import load_settings

        cfg = self._client_config(tmp_path, enable=True)
        manager = MemoryManager(load_settings(str(cfg)))
        monkeypatch.setattr(api_main, "get_manager", lambda config=None: manager)
        yield api_main.app
        manager.close()

    def test_lifespan_starts_and_stops_poller(
        self, lifespan_app: Any, _poller_spy: dict[str, Any]
    ) -> None:
        with TestClient(lifespan_app) as client:
            assert client.get("/health").status_code == 200
            assert _poller_spy["constructed"] == 1
            assert _poller_spy["started"] == 1
        assert _poller_spy["stopped"] == 1

    def test_lifespan_disabled_constructs_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _poller_spy: dict[str, Any]
    ) -> None:
        from vesmaro.api import main as api_main
        from vesmaro.config import load_settings

        cfg = self._client_config(tmp_path, enable=False)
        manager = MemoryManager(load_settings(str(cfg)))
        monkeypatch.setattr(api_main, "get_manager", lambda config=None: manager)
        try:
            with TestClient(api_main.app) as client:
                assert client.get("/health").status_code == 200
            assert _poller_spy["constructed"] == 0
        finally:
            manager.close()
