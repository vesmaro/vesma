"""ADR-0051 (W4) — the resilience plane: state machine, spool, replay,
front integration, DR-24, config.

The contract pins (ADR-0051 §Решение + spec §7 v2):

* machine — HEALTHY ──(3 failed probes)──▶ DEGRADED; probe OK ──▶ REPLAY
  ──(100% verified, spool atomically removed)──▶ HEALTHY; TTL expiry
  ──▶ BLOCKED_WRITES (typed DegradedTTLExceeded on writes, reads stay
  available); BLOCKED + recovered core ──▶ REPLAY; restarts and time
  jumps keep the persisted anchor honest;
* spool — append-only JSONL, per-row hash chain (tamper detectable by
  the same uid), fsync, 0600/0700, secret-scan at write (rows MARKED),
  byte quota (exhaustion ⇒ BLOCKED_WRITES), torn tail tolerated
  (skip-and-report) and repaired on the next append;
* arbitration — a valid spool header OLDER than the state file wins;
* replay — verified, ordered, under the front service token; counters
  planned/replayed/superseded/rejected-reauthz/failed; the spool is
  removed ONLY after 100%; a refused identity aborts the run;
* front — W2 byte-identical without the coordinator; degraded writes
  get a receipt + X-Vesma-Degraded; reads stay honest 503s; healthz
  carries the degraded section;
* DR-24 — the full matrix (age/%TTL, mode/owner, chain tamper, orphan
  tails, state-without-spool, config posture);
* config — fail-closed (no section = refuse-writes posture; an
  incomplete section refuses the boot; extra=forbid).
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from vesma.config import Settings
from vesma.mcp_front import McpFront, build_resilience
from vesma.mcp_namespace import McpIdentity, reset_request_identity, set_request_identity
from vesma.resilience import (
    DEFAULT_MAX_DURATION_SEC,
    DegradedTTLExceeded,
    FrontResilienceSettings,
    MachineState,
    ReplayAuthzError,
    ReplayTransientError,
    ResilienceCoordinator,
    ResilienceMode,
    Spool,
    SpoolQuotaExceeded,
    SpoolRow,
    StateStore,
    classify_tool_call,
    core_answer_accepted,
    core_answer_transient,
    jsonrpc_result,
    oldest_header_since,
    parse_duration,
    replay_envelope,
    resolve_instance,
    resolve_state_home,
    run_replay,
    sanitize_label,
    tool_call_from_body,
)
from vesma.service import doctor_checks
from vesma.service.doctor_checks import Severity

# ── Fakes and fixtures ────────────────────────────────────────────────


class _Clock:
    """A controllable wall clock (the TTL/jump tests need time travel)."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _RealClock(_Clock):
    """A controllable clock STARTING at the real wall clock — for tests
    that hand the state to consumers reading the REAL ``time.time()``
    (the DR-24 doctor leg)."""

    def __init__(self) -> None:
        import time

        super().__init__(start=time.time())


class _Harness:
    """Coordinator + captured probe/core-call/notify calls."""

    def __init__(
        self,
        tmp_path: Path,
        clock: _Clock,
        *,
        probe_ok: bool = True,
        core_ok: bool = True,
        core_transport_down: bool = False,
        settings: FrontResilienceSettings | None = None,
        service_token: str | None = "svc-token",
    ) -> None:
        self.clock = clock
        self.probes = 0
        self.probe_ok = probe_ok
        self.core_calls: list[tuple[str, dict[str, Any]]] = []
        self.core_ok = core_ok
        self.core_transport_down = core_transport_down
        self.notifies: list[str] = []
        if settings is None:
            settings = _settings(service_token=service_token)
        self.settings = settings
        self.coordinator = ResilienceCoordinator(
            settings,
            state_home=tmp_path / "state",
            clock=clock,
            probe=self._probe,
            core_call=self._core_call,
            notify=self._notify,
        )

    async def _probe(self) -> bool:
        self.probes += 1
        return self.probe_ok

    async def _core_call(self, op: str, payload: dict[str, Any]) -> bool:
        if self.core_transport_down:
            raise RuntimeError("transport down")
        self.core_calls.append((op, payload))
        return self.core_ok

    async def _notify(self, url: str) -> None:
        self.notifies.append(url)


def _settings(**overrides: Any) -> FrontResilienceSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "max_duration_sec": 3600,
        "poll_interval_sec": 30,
        "backoff_max_sec": 600,
        "spool_max_bytes": 1_000_000,
        "signage_interval_sec": 900,
        "service_token": "svc-token",
        "instance": "testinst",
        "notify_url": "http://127.0.0.1:9/hook",
    }
    base.update(overrides)
    return FrontResilienceSettings(**base)


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def harness(tmp_path: Path, clock: _Clock) -> _Harness:
    return _Harness(tmp_path, clock)


def _degraded(h: _Harness, *, since: float | None = None) -> MachineState:
    """Seed the machine into DEGRADED (as a completed transition would)."""
    state = MachineState(
        mode=ResilienceMode.DEGRADED,
        degraded_since=since if since is not None else h.clock(),
        ttl_sec=h.settings.max_duration_sec,
        last_seen_wall=h.clock(),
    )
    h.coordinator._save(state)
    return state


def _seed_row(
    h: _Harness, *, op: str = "vesma_add", payload: dict[str, Any] | None = None
) -> SpoolRow:
    state = h.coordinator.state()
    spool = h.coordinator.open_spool(h.coordinator.actor, state)
    row = spool.append(
        op=op,
        payload=payload or {"content": "hello", "tags": ["project:p"]},
        clock=h.clock,
    )
    return row


# ── parse_duration ────────────────────────────────────────────────────


class TestParseDuration:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("7d", 7 * 86400),
            ("12h", 12 * 3600),
            ("30m", 1800),
            ("600s", 600),
            ("2w", 14 * 86400),
            ("90", 90),
            (90, 90),
            (" 15M ", 900),  # case-insensitive suffix, surrounding space
        ],
    )
    def test_accepted_forms(self, raw: str | int, expected: int) -> None:
        assert parse_duration(raw) == expected

    @pytest.mark.parametrize("raw", ["", "banana", "7x", "-5", "7.5d", 0, -10, True, "d7"])
    def test_refused_forms(self, raw: str | int) -> None:
        with pytest.raises(ValueError):
            parse_duration(raw)


# ── sanitize_label / resolvers ────────────────────────────────────────


class TestLabels:
    def test_plain_slug_accepted(self) -> None:
        assert sanitize_label("vesma-default", what="instance") == "vesma-default"

    @pytest.mark.parametrize("bad", ["", "..", "../evil", "a/b", "a b", ".hidden", "x" * 129])
    def test_traversal_and_garbage_refused(self, bad: str) -> None:
        with pytest.raises(ValueError):
            sanitize_label(bad, what="label")

    def test_state_home_xdg_precedence(self) -> None:
        env = {"VESMA_STATE_HOME": "/s1", "XDG_STATE_HOME": "/s2", "HOME": "/h"}
        assert resolve_state_home(env) == Path("/s1")
        assert resolve_state_home({"XDG_STATE_HOME": "/s2", "HOME": "/h"}) == Path("/s2")
        assert resolve_state_home({"HOME": "/h"}) == Path("/h/.local/state")

    def test_instance_env_over_file_over_default(self, tmp_path: Path) -> None:
        assert resolve_instance({"VESMA_FRONT_INSTANCE": "box-1"}) == "box-1"
        store = tmp_path / "data"
        store.mkdir()
        (store / "instance-id").write_text("minted-7\n", encoding="utf-8")
        assert resolve_instance({}, store_home=store) == "minted-7"
        (store / "instance-id").write_text("", encoding="utf-8")
        assert resolve_instance({}, store_home=store) == "default"
        assert resolve_instance({}, store_home=tmp_path / "absent") == "default"

    def test_instance_refuses_hostile_env_label(self) -> None:
        with pytest.raises(ValueError):
            resolve_instance({"VESMA_FRONT_INSTANCE": "../escape"})


# ── Op classifier ─────────────────────────────────────────────────────


class TestClassifier:
    @pytest.mark.parametrize(
        "name",
        [
            "vesma_add",
            "vesma_save_context",
            "vesma_filter",
            "vesma_tags_rename",
            "vesma_import",
            "vesma_ingest_url",
            "vesma_ingest_document",
            "vesma_context_rewrite",
            "vesma_compress",
            "vesma_reprocess",
            "vesma_watch_start",
            "vesma_watch_stop",
        ],
    )
    def test_write_tools_classified_write(self, name: str) -> None:
        assert classify_tool_call(name, {}) == "write"

    @pytest.mark.parametrize(
        "name",
        [
            "vesma_search",
            "vesma_recall_context",
            "vesma_agent_recall",
            "vesma_list_recent",
            "vesma_list_tags",
            "vesma_stats",
            "vesma_retrieve",
            "vesma_export",
            "vesma_usage_report",
            "vesma_unknown_future_tool",
        ],
    )
    def test_read_tools_classified_read(self, name: str) -> None:
        assert classify_tool_call(name, {}) == "read"

    def test_dual_tools_route_on_action(self) -> None:
        assert classify_tool_call("vesma_tags", {"action": "get"}) == "read"
        assert classify_tool_call("vesma_tags", {"action": "add"}) == "write"
        assert classify_tool_call("vesma_tags", {"action": "remove"}) == "write"
        assert classify_tool_call("vesma_workflow", {"action": "history"}) == "read"
        assert classify_tool_call("vesma_workflow", {"action": "set"}) == "write"
        assert classify_tool_call("vesma_awareness", {"action": "pre_flight"}) == "read"
        assert classify_tool_call("vesma_awareness", {"action": "record_abstention"}) == "write"
        assert classify_tool_call("vesma_hooks", {"action": "pre_llm_call"}) == "read"
        assert classify_tool_call("vesma_hooks", {"action": "post_tool_call"}) == "write"

    def test_dual_tool_with_missing_action_fails_safe_to_write(self) -> None:
        """An unclassifiable dual call must never be dropped (a dropped
        write costs data; a replayed read costs nothing)."""
        assert classify_tool_call("vesma_tags", {}) == "write"
        assert classify_tool_call("vesma_workflow", None) == "write"

    def test_registered_tools_all_classified(self) -> None:
        """Every canonical tool name classifies without crashing and the
        WRITE set names only real tools (the classifier stays in sync
        with the registry — a renamed tool must land here)."""
        import asyncio

        from vesma import mcp_server

        names = {t.name for t in asyncio.run(mcp_server._canonical_tools())}
        assert "vesma_add" in names
        assert {"vesma_save_context", "vesma_search"} <= names
        for name in names:
            verdict = classify_tool_call(name, {"action": "get"})
            assert verdict in ("read", "write")
        assert classify_tool_call("vesma_add", None) == "write"

    def test_replay_envelope_carries_op_id(self) -> None:
        row = SpoolRow(
            op_id="op-1",
            ts_utc="2026-11-04T00:00:00Z",
            ts=0.0,
            actor="a",
            op="vesma_add",
            payload={"content": "x"},
            prev_hash=None,
            row_hash="h",
        )
        envelope = replay_envelope(row)
        assert envelope["vesma_degraded_op_id"] == "op-1"
        assert envelope["content"] == "x"


# ── JSON-RPC helpers ──────────────────────────────────────────────────


class TestJsonRpc:
    def test_tool_call_parsed(self) -> None:
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "vesma_add", "arguments": {"content": "hi"}},
            }
        ).encode()
        parsed = tool_call_from_body(body)
        assert parsed is not None
        name, arguments, jsonrpc_id = parsed
        assert (name, arguments, jsonrpc_id) == ("vesma_add", {"content": "hi"}, 7)

    @pytest.mark.parametrize(
        "body",
        [
            b"",
            b"not json",
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call"}).encode(),
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}}).encode(),
        ],
    )
    def test_non_write_or_malformed_bodies_yield_none(self, body: bytes) -> None:
        assert tool_call_from_body(body) is None

    def test_arguments_missing_defaults_empty(self) -> None:
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": None,
                "method": "tools/call",
                "params": {"name": "vesma_stats"},
            }
        ).encode()
        parsed = tool_call_from_body(body)
        assert parsed is not None
        assert parsed[1] == {}

    def test_result_envelope_shape(self) -> None:
        answer = jsonrpc_result(3, "receipt text")
        assert answer["jsonrpc"] == "2.0"
        assert answer["id"] == 3
        assert answer["result"]["content"][0]["text"] == "receipt text"
        assert answer["result"]["isError"] is False
        refusal = jsonrpc_result(3, "refused", is_error=True)
        assert refusal["result"]["isError"] is True


# ── Spool ─────────────────────────────────────────────────────────────


class TestSpool:
    def _spool(self, tmp_path: Path) -> Spool:
        return Spool.open(
            tmp_path / "spool",
            instance="inst",
            actor="mcp-front",
            degraded_since=1000.0,
            max_duration_sec=3600,
            clock=lambda: 1000.0,
        )

    def test_open_creates_private_tree_and_header(self, tmp_path: Path) -> None:
        spool = self._spool(tmp_path)
        header = spool.header()
        assert header is not None
        assert header["instance"] == "inst"
        assert header["actor"] == "mcp-front"
        assert header["degraded_since"] == 1000.0
        assert header["ttl_sec"] == 3600
        assert header["chain_origin"] is None
        assert stat.S_IMODE(spool.dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(spool.header_path.stat().st_mode) == 0o600

    def test_open_is_idempotent_header_kept(self, tmp_path: Path) -> None:
        spool = self._spool(tmp_path)
        again = self._spool(tmp_path)
        assert again.header() == spool.header()

    def test_append_chains_rows(self, tmp_path: Path) -> None:
        spool = self._spool(tmp_path)
        first = spool.append(op="vesma_add", payload={"content": "a"})
        second = spool.append(op="vesma_save_context", payload={"goals": "g"})
        assert first.prev_hash is None
        assert second.prev_hash == first.row_hash
        assert first.secret_flagged == ()
        assert stat.S_IMODE(spool.ops_path.stat().st_mode) == 0o600

    def test_verify_chain_clean(self, tmp_path: Path) -> None:
        spool = self._spool(tmp_path)
        for i in range(3):
            spool.append(op="vesma_add", payload={"content": f"c{i}"})
        rows, problems = spool.verify_chain()
        assert problems == []
        assert len(rows) == 3

    def test_tampered_payload_detected(self, tmp_path: Path) -> None:
        spool = self._spool(tmp_path)
        spool.append(op="vesma_add", payload={"content": "a"})
        spool.append(op="vesma_add", payload={"content": "b"})
        raw = spool.ops_path.read_text(encoding="utf-8").splitlines()
        row = json.loads(raw[0])
        row["payload"]["content"] = "FORGED"
        raw[0] = json.dumps(row, sort_keys=True, separators=(",", ":"))
        spool.ops_path.write_text("\n".join(raw) + "\n", encoding="utf-8")
        rows, problems = spool.verify_chain()
        assert len(rows) == 2
        assert problems and "row_hash mismatch" in problems[0]

    def test_deleted_row_detected_by_chain_break(self, tmp_path: Path) -> None:
        spool = self._spool(tmp_path)
        for i in range(3):
            spool.append(op="vesma_add", payload={"content": f"c{i}"})
        raw = spool.ops_path.read_text(encoding="utf-8").splitlines()
        del raw[1]  # silently drop the middle row
        spool.ops_path.write_text("\n".join(raw) + "\n", encoding="utf-8")
        _rows, problems = spool.verify_chain()
        assert any("does not chain" in p for p in problems)

    def test_torn_tail_tolerated_on_read(self, tmp_path: Path) -> None:
        spool = self._spool(tmp_path)
        spool.append(op="vesma_add", payload={"content": "a"})
        with spool.ops_path.open("ab") as fh:
            fh.write(b'{"op_id": "torn", "payload": {"con')  # partial append
        rows, torn = spool.read_rows()
        assert torn is True
        assert len(rows) == 1

    def test_recover_for_append_truncates_and_continues_chain(self, tmp_path: Path) -> None:
        spool = self._spool(tmp_path)
        first = spool.append(op="vesma_add", payload={"content": "a"})
        with spool.ops_path.open("ab") as fh:
            fh.write(b'{"op_id": "torn"')  # the process died mid-append
        revived = Spool(spool.dir, actor="mcp-front")
        revived.recover_for_append()
        second = revived.append(op="vesma_add", payload={"content": "b"})
        assert second.prev_hash == first.row_hash
        rows, problems = revived.verify_chain()
        assert problems == []
        assert [r.payload["content"] for r in rows] == ["a", "b"]

    def test_secret_scan_marks_row_with_pattern_names_only(self, tmp_path: Path) -> None:
        spool = self._spool(tmp_path)
        payload = {"content": "my token is ghp_" + "A" * 36}
        row = spool.append(op="vesma_add", payload=payload)
        assert row.secret_flagged, "a GitHub-token-shaped value must flag"
        assert all(isinstance(name, str) for name in row.secret_flagged)
        # The flag carries NAMES, never the value itself.
        for name in row.secret_flagged:
            assert "A" * 36 not in name
        disk = spool.ops_path.read_text(encoding="utf-8")
        assert "secret_flagged" in disk  # the mark is persisted

    def test_clean_payload_not_flagged(self, tmp_path: Path) -> None:
        spool = self._spool(tmp_path)
        row = spool.append(op="vesma_add", payload={"content": "plain notes"})
        assert row.secret_flagged == ()

    def test_bytes_used_accounts_header_and_ops(self, tmp_path: Path) -> None:
        spool = self._spool(tmp_path)
        base = spool.bytes_used()
        assert base > 0
        spool.append(op="vesma_add", payload={"content": "a"})
        assert spool.bytes_used() > base

    def test_mark_replayed_and_remove_is_atomic_and_total(self, tmp_path: Path) -> None:
        spool = self._spool(tmp_path)
        spool.append(op="vesma_add", payload={"content": "a"})
        spool.mark_replayed_and_remove()
        assert not spool.dir.exists()
        assert not (spool.dir.parent).exists() or not any(spool.dir.parent.iterdir())

    def test_discover_finds_actor_spools_sorted(self, tmp_path: Path) -> None:
        root = tmp_path / "spool"
        for actor in ("beta", "alpha"):
            Spool.open(
                root,
                instance="inst",
                actor=actor,
                degraded_since=1000.0,
                max_duration_sec=3600,
                clock=lambda: 1000.0,
            )
        found = Spool.discover(root / "inst")
        assert [s.actor for s in found] == ["alpha", "beta"]


# ── State store + arbitration ─────────────────────────────────────────


class TestStateArbitration:
    def _instance_dir(self, tmp_path: Path) -> Path:
        return tmp_path / "state" / "vesma" / "spool" / "inst"

    def test_default_state_is_healthy(self, tmp_path: Path) -> None:
        store = StateStore(self._instance_dir(tmp_path))
        state = store.load()
        assert state.mode == ResilienceMode.HEALTHY
        assert state.degraded_since is None

    def test_round_trip(self, tmp_path: Path) -> None:
        store = StateStore(self._instance_dir(tmp_path))
        state = MachineState(
            mode=ResilienceMode.DEGRADED,
            degraded_since=42.0,
            ttl_sec=100,
            probe_failures=2,
            poll_backoff_sec=120,
        )
        store.save(state)
        loaded = store.load()
        assert loaded.mode == ResilienceMode.DEGRADED
        assert loaded.degraded_since == 42.0
        assert loaded.ttl_sec == 100
        assert loaded.probe_failures == 2
        assert loaded.poll_backoff_sec == 120

    def test_garbage_state_falls_back_to_healthy(self, tmp_path: Path) -> None:
        instance_dir = self._instance_dir(tmp_path)
        instance_dir.mkdir(parents=True)
        (instance_dir / "state.json").write_text("{broken", encoding="utf-8")
        assert StateStore(instance_dir).load().mode == ResilienceMode.HEALTHY

    def test_state_file_mode_is_private(self, tmp_path: Path) -> None:
        store = StateStore(self._instance_dir(tmp_path))
        store.save(MachineState())
        mode = stat.S_IMODE(store.state_path.stat().st_mode)
        assert mode == 0o600

    def test_header_older_than_healthy_state_restores_degraded(self, tmp_path: Path) -> None:
        instance_dir = self._instance_dir(tmp_path)
        Spool.open(
            tmp_path / "state" / "vesma" / "spool",
            instance="inst",
            actor="mcp-front",
            degraded_since=500.0,
            max_duration_sec=3600,
            clock=lambda: 600.0,
        )
        store = StateStore(instance_dir)
        store.save(MachineState(mode=ResilienceMode.HEALTHY, last_seen_wall=600.0))
        state = store.load()
        assert state.mode == ResilienceMode.DEGRADED
        assert state.degraded_since == 500.0

    def test_header_older_than_state_anchor_adopted(self, tmp_path: Path) -> None:
        instance_dir = self._instance_dir(tmp_path)
        Spool.open(
            tmp_path / "state" / "vesma" / "spool",
            instance="inst",
            actor="mcp-front",
            degraded_since=100.0,
            max_duration_sec=3600,
            clock=lambda: 200.0,
        )
        store = StateStore(instance_dir)
        store.save(MachineState(mode=ResilienceMode.DEGRADED, degraded_since=300.0, ttl_sec=3600))
        state = store.load()
        assert state.degraded_since == 100.0

    def test_state_anchor_older_than_header_wins(self, tmp_path: Path) -> None:
        instance_dir = self._instance_dir(tmp_path)
        Spool.open(
            tmp_path / "state" / "vesma" / "spool",
            instance="inst",
            actor="mcp-front",
            degraded_since=900.0,
            max_duration_sec=3600,
            clock=lambda: 950.0,
        )
        store = StateStore(instance_dir)
        store.save(MachineState(mode=ResilienceMode.DEGRADED, degraded_since=700.0, ttl_sec=3600))
        state = store.load()
        assert state.degraded_since == 700.0

    def test_unparsable_header_is_not_an_anchor(self, tmp_path: Path) -> None:
        instance_dir = self._instance_dir(tmp_path)
        actor_dir = instance_dir / "mcp-front"
        actor_dir.mkdir(parents=True)
        (actor_dir / Spool.HEADER_NAME).write_text("garbage", encoding="utf-8")
        store = StateStore(instance_dir)
        store.save(MachineState(mode=ResilienceMode.HEALTHY))
        assert store.load().mode == ResilienceMode.HEALTHY
        assert oldest_header_since(instance_dir) is None


# ── FrontResilienceSettings (env.d, fail-closed) ──────────────────────


class TestFrontSettings:
    def test_missing_max_duration_refuses_writes_with_line(self) -> None:
        settings, refusal = FrontResilienceSettings.from_env({})
        assert settings.enabled is False
        assert refusal is not None
        assert "refuse-writes" in refusal
        assert "VESMA_FRONT_DEGRADED_MAX_DURATION" in refusal

    def test_present_max_duration_enables(self) -> None:
        env = {"VESMA_FRONT_DEGRADED_MAX_DURATION": "7d"}
        settings, refusal = FrontResilienceSettings.from_env(env)
        assert refusal is None
        assert settings.enabled is True
        assert settings.max_duration_sec == DEFAULT_MAX_DURATION_SEC

    def test_full_slot_parses(self) -> None:
        env = {
            "VESMA_FRONT_DEGRADED_MAX_DURATION": "12h",
            "VESMA_FRONT_DEGRADED_POLL_INTERVAL": "45s",
            "VESMA_FRONT_DEGRADED_BACKOFF_MAX": "300s",
            "VESMA_FRONT_DEGRADED_SIGNAGE_INTERVAL": "5m",
            "VESMA_FRONT_SPOOL_MAX_BYTES": "2048",
            "VESMA_FRONT_PROBE_FAILURE_THRESHOLD": "2",
            "VESMA_FRONT_DEGRADED_NOTIFY_URL": "http://127.0.0.1:9/hook",
            "VESMA_FRONT_SERVICE_TOKEN": "svc",
            "VESMA_FRONT_INSTANCE": "boxx",
        }
        settings, refusal = FrontResilienceSettings.from_env(env)
        assert refusal is None
        assert settings.max_duration_sec == 12 * 3600
        assert settings.poll_interval_sec == 45
        assert settings.backoff_max_sec == 300
        assert settings.signage_interval_sec == 300
        assert settings.spool_max_bytes == 2048
        assert settings.probe_failure_threshold == 2
        assert settings.notify_url == "http://127.0.0.1:9/hook"
        assert settings.service_token == "svc"
        assert settings.instance == "boxx"

    @pytest.mark.parametrize(
        "env",
        [
            {"VESMA_FRONT_DEGRADED_MAX_DURATION": "banana"},
            {"VESMA_FRONT_DEGRADED_MAX_DURATION": "7d", "VESMA_FRONT_SPOOL_MAX_BYTES": "0"},
            {
                "VESMA_FRONT_DEGRADED_MAX_DURATION": "7d",
                "VESMA_FRONT_PROBE_FAILURE_THRESHOLD": "0",
            },
            {
                "VESMA_FRONT_DEGRADED_MAX_DURATION": "7d",
                "VESMA_FRONT_DEGRADED_POLL_INTERVAL": "nope",
            },
        ],
    )
    def test_malformed_present_value_raises(self, env: dict[str, str]) -> None:
        with pytest.raises(ValueError):
            FrontResilienceSettings.from_env(env)

    def test_token_absence_is_survivable_but_reported(self) -> None:
        settings, refusal = FrontResilienceSettings.from_env(
            {"VESMA_FRONT_DEGRADED_MAX_DURATION": "1h"}
        )
        assert refusal is None
        assert settings.service_token is None


# ── State machine (coordinator) ───────────────────────────────────────


class TestStateMachine:
    def test_threshold_three_failures_flip_to_degraded(self, harness: _Harness) -> None:
        harness.probe_ok = False
        harness.clock.advance(1)
        outcome1 = _run(harness.coordinator.poll_once())
        outcome2 = _run(harness.coordinator.poll_once())
        assert outcome1.mode_after == ResilienceMode.HEALTHY
        assert outcome2.mode_after == ResilienceMode.HEALTHY
        outcome3 = _run(harness.coordinator.poll_once())
        assert outcome3.mode_after == ResilienceMode.DEGRADED

    def test_observe_request_drives_the_machine(self, harness: _Harness) -> None:
        """Traffic outcomes double as probes: three failed requests flip
        HEALTHY → DEGRADED without any poller."""
        harness.coordinator.observe_request(ok=False)
        harness.coordinator.observe_request(ok=False)
        assert harness.coordinator.current_mode() == ResilienceMode.HEALTHY
        harness.coordinator.observe_request(ok=False)
        assert harness.coordinator.current_mode() == ResilienceMode.DEGRADED

    def test_success_resets_failure_counter(self, harness: _Harness) -> None:
        harness.coordinator.observe_request(ok=False)
        harness.coordinator.observe_request(ok=False)
        harness.coordinator.observe_request(ok=True)
        harness.coordinator.observe_request(ok=False)
        harness.coordinator.observe_request(ok=False)
        assert harness.coordinator.current_mode() == ResilienceMode.HEALTHY

    def test_degraded_write_receipt_via_spool(self, harness: _Harness) -> None:
        _degraded(harness)
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "vesma_add", "arguments": {"content": "saved"}},
            }
        ).encode()
        intercepted = harness.coordinator.intercept_write(body)
        assert intercepted is not None
        result, receipt = intercepted
        assert result["result"]["isError"] is False
        assert "op_id=" in receipt
        assert "ttl_left=" in receipt
        assert "WILL be delivered" not in receipt, "F2: no delivery promise"
        assert "DR-24" in receipt
        rows, _torn = harness.coordinator.discover_spools()[0].read_rows()
        assert rows[0].payload == {"content": "saved"}
        assert rows[0].op == "vesma_add"

    def test_degraded_read_interception_returns_none(self, harness: _Harness) -> None:
        _degraded(harness)
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "vesma_search", "arguments": {"query": "x"}},
            }
        ).encode()
        assert harness.coordinator.intercept_write(body) is None

    def test_ttl_expiry_moves_to_blocked_and_refuses_typed(self, harness: _Harness) -> None:
        _degraded(harness)
        harness.clock.advance(3601)  # beyond max_duration_sec=3600
        with pytest.raises(DegradedTTLExceeded) as excinfo:
            harness.coordinator.check_write_allowed()
        assert "DegradedTTLExceeded" in str(excinfo.value)
        assert harness.coordinator.current_mode() == ResilienceMode.BLOCKED_WRITES

    def test_blocked_state_persists_across_restart(self, harness: _Harness) -> None:
        _degraded(harness)
        harness.clock.advance(3601)
        with pytest.raises(DegradedTTLExceeded):
            harness.coordinator.check_write_allowed()
        revived = ResilienceCoordinator(
            harness.settings,
            state_home=harness.coordinator.spool_root.parent.parent,
            clock=harness.clock,
            probe=harness.coordinator._probe,
        )
        assert revived.current_mode() == ResilienceMode.BLOCKED_WRITES

    def test_probe_ok_in_degraded_flips_to_replay_and_back_to_healthy(
        self, harness: _Harness
    ) -> None:
        _degraded(harness)
        _seed_row(harness)
        harness.probe_ok = True
        outcome = _run(harness.coordinator.poll_once())
        assert outcome.mode_before == ResilienceMode.DEGRADED
        assert outcome.mode_after == ResilienceMode.HEALTHY
        assert outcome.replay is not None
        assert outcome.replay.replayed == 1
        assert harness.coordinator.discover_spools() == []

    def test_transport_failed_replay_keeps_spool_and_degrades(self, harness: _Harness) -> None:
        """A TRANSIENT failure (transport) fail-stops the pass: the row
        stays for the next pass, the machine goes back to DEGRADED."""
        _degraded(harness)
        _seed_row(harness)
        harness.probe_ok = True
        harness.core_transport_down = True
        outcome = _run(harness.coordinator.poll_once())
        assert outcome.mode_after == ResilienceMode.DEGRADED
        assert outcome.replay is not None
        assert outcome.replay.failed == 1
        assert len(harness.coordinator.discover_spools()) == 1

    def test_refused_replay_row_pockets_and_completes(self, harness: _Harness) -> None:
        """F2: a core REFUSAL (e.g. a namespace violation under the
        service token) lands in the rejected-permanent pocket — consumed,
        never retried — and the machine completes to HEALTHY (the spool
        held nothing pending anymore)."""
        _degraded(harness)
        _seed_row(harness)
        harness.probe_ok = True
        harness.core_ok = False  # the core refuses the operation
        outcome = _run(harness.coordinator.poll_once())
        assert outcome.mode_after == ResilienceMode.HEALTHY
        assert outcome.replay is not None
        assert outcome.replay.rejected_permanent == 1
        assert outcome.replay.failed == 0
        assert len(harness.coordinator.discover_spools()) == 0
        assert "rejected-permanent" in " ".join(outcome.replay.problems)
        assert outcome.replay.summary() in harness.coordinator.state().last_replay_summary

    def test_blocked_recovers_through_replay_when_core_returns(self, harness: _Harness) -> None:
        _degraded(harness)
        harness.clock.advance(3601)
        with pytest.raises(DegradedTTLExceeded):
            harness.coordinator.check_write_allowed()
        assert harness.coordinator.current_mode() == ResilienceMode.BLOCKED_WRITES
        harness.probe_ok = True
        outcome = _run(harness.coordinator.poll_once())
        assert outcome.mode_after == ResilienceMode.HEALTHY

    def test_backoff_grows_and_caps(self, harness: _Harness) -> None:
        harness.probe_ok = False
        _run(harness.coordinator.poll_once())
        assert harness.coordinator.state().poll_backoff_sec == 60
        _run(harness.coordinator.poll_once())
        assert harness.coordinator.state().poll_backoff_sec == 120
        for _ in range(6):
            _run(harness.coordinator.poll_once())
        assert harness.coordinator.state().poll_backoff_sec == 600

    def test_backoff_resets_on_recovery(self, harness: _Harness) -> None:
        harness.probe_ok = False
        _run(harness.coordinator.poll_once())
        _run(harness.coordinator.poll_once())
        harness.probe_ok = True
        _run(harness.coordinator.poll_once())
        assert harness.coordinator.state().poll_backoff_sec == 30

    def test_time_jump_forward_signposted_and_ttl_evaluated(
        self, harness: _Harness, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        _degraded(harness)
        _run(harness.coordinator.poll_once())  # stamps last_seen_wall
        with caplog.at_level(logging.WARNING, logger="vesma.resilience"):
            harness.clock.advance(7 * 3600)  # beyond the 6h jump threshold
            outcome = _run(harness.coordinator.poll_once())
        assert outcome.time_jump is not None
        assert "wall-clock jump" in outcome.time_jump
        assert any("wall-clock jump" in rec.message for rec in caplog.records)

    def test_time_jump_past_ttl_blocks_writes(self, harness: _Harness) -> None:
        harness.probe_ok = False  # the machine stays DEGRADED across the jump
        _degraded(harness)
        _run(harness.coordinator.poll_once())
        harness.clock.advance(10 * 3600)  # jump beyond both threshold and TTL
        outcome = _run(harness.coordinator.poll_once())
        assert outcome.mode_after == ResilienceMode.BLOCKED_WRITES

    def test_time_jump_backwards_keeps_anchor(self, tmp_path: Path) -> None:
        clock = _Clock(start=2000.0)
        h = _Harness(tmp_path, clock)
        h.probe_ok = False  # stay DEGRADED through the jump
        state = _degraded(h, since=1000.0)
        _run(h.coordinator.poll_once())  # stamps last_seen_wall = 2000
        clock.now = 2000 - 8 * 3600  # a backwards NTP jump
        outcome = _run(h.coordinator.poll_once())
        assert outcome.time_jump is not None
        assert outcome.mode_after == ResilienceMode.DEGRADED
        after = h.coordinator.state()
        assert after.degraded_since == state.degraded_since

    def test_quota_exhaustion_blocks_writes(self, tmp_path: Path, clock: _Clock) -> None:
        settings = _settings(spool_max_bytes=1)
        h = _Harness(tmp_path, clock, settings=settings)
        _degraded(h)
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "vesma_add", "arguments": {"content": "x"}},
            }
        ).encode()
        intercepted = h.coordinator.intercept_write(body)
        assert intercepted is not None  # the first write lands (and blocks the rest)
        assert h.coordinator.current_mode() == ResilienceMode.BLOCKED_WRITES
        with pytest.raises(SpoolQuotaExceeded):
            h.coordinator.intercept_write(body)

    def test_quota_blocked_persists_until_drained(self, tmp_path: Path, clock: _Clock) -> None:
        settings = _settings(spool_max_bytes=1)
        h = _Harness(tmp_path, clock, settings=settings)
        _degraded(h)
        h.coordinator.intercept_write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "vesma_add", "arguments": {"content": "x"}},
                }
            ).encode()
        )
        with pytest.raises(SpoolQuotaExceeded):
            h.coordinator.check_write_allowed()
        # A successful replay drains the spool and the machine returns to
        # HEALTHY (quota_blocked cleared) — the drain loop closes.
        h.probe_ok = True
        outcome = _run(h.coordinator.poll_once())
        assert outcome.mode_after == ResilienceMode.HEALTHY
        assert h.coordinator.state().quota_blocked is False

    def test_signage_cadence(self, harness: _Harness, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        _degraded(harness)
        with caplog.at_level(logging.WARNING, logger="vesma.resilience"):
            harness.coordinator._signage(harness.coordinator.state(), force=True)
            first = sum(
                1
                for r in caplog.records
                if r.levelno == logging.WARNING and "DEGRADED for" in r.message
            )
            harness.clock.advance(60)  # inside the 15m window — suppressed
            harness.coordinator._signage(harness.coordinator.state())
            harness.clock.advance(900)  # past the window — fires again
            harness.coordinator._signage(harness.coordinator.state())
        assert first == 1
        fires = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "DEGRADED for" in r.message
        ]
        assert len(fires) >= 2

    def test_notify_fires_once_per_degradation(self, harness: _Harness) -> None:
        harness.probe_ok = False  # the core stays down — machine stays DEGRADED
        _degraded(harness)
        _run(harness.coordinator.poll_once())
        assert len(harness.notifies) == 1  # the DEGRADED-entry webhook
        _run(harness.coordinator.poll_once())
        assert len(harness.notifies) == 1  # no duplicate

    def test_notify_fires_at_80_percent_ttl(self, harness: _Harness) -> None:
        harness.probe_ok = False
        _degraded(harness)
        _run(harness.coordinator.poll_once())
        assert len(harness.notifies) == 1
        harness.clock.advance(0.85 * 3600)  # past the 80% mark
        _run(harness.coordinator.poll_once())
        assert len(harness.notifies) == 2

    def test_notify_failure_is_best_effort(self, tmp_path: Path, clock: _Clock) -> None:
        class _Broken:
            def __init__(self) -> None:
                self.calls = 0

            async def __call__(self, url: str) -> None:
                self.calls += 1
                raise RuntimeError("webhook down")

        broken = _Broken()
        h = _Harness(tmp_path, clock)
        h.probe_ok = False
        h.coordinator._notify_fn = broken
        _degraded(h)
        _run(h.coordinator.poll_once())
        assert broken.calls == 1
        assert h.coordinator.state().notify_degraded_sent is True

    def test_observe_failures_lower_replay_to_degraded(self, harness: _Harness) -> None:
        """F10: the same threshold lowers REPLAY back to DEGRADED when the
        'live' core flaps away; consumed rows stay consumed."""
        _degraded(harness)
        state = harness.coordinator.state()
        state.mode = ResilienceMode.REPLAY
        harness.coordinator._save(state)
        harness.coordinator.observe_request(ok=False)
        harness.coordinator.observe_request(ok=False)
        assert harness.coordinator.current_mode() == ResilienceMode.REPLAY
        harness.coordinator.observe_request(ok=False)
        assert harness.coordinator.current_mode() == ResilienceMode.DEGRADED

    def test_row_stamps_share_the_coordinator_clock(self, harness: _Harness) -> None:
        """F7: row stamps come from the OWNER's clock (no time.time()
        divergence against the state file's notion of now)."""
        _degraded(harness)
        row = _seed_row(harness)
        assert row.ts == harness.clock.now, "the row stamp uses the injected clock"

    def test_healthz_snapshot_fields(self, harness: _Harness) -> None:
        _degraded(harness)
        _seed_row(harness)
        snapshot = harness.coordinator.healthz_snapshot()
        degraded = snapshot["degraded"]
        assert degraded["mode"] == ResilienceMode.DEGRADED
        assert degraded["since_utc"] is not None
        assert degraded["ttl_left_sec"] is not None
        assert degraded["spool_ops_pending"] == 1
        assert degraded["spool_bytes"] > 0

    async def test_poller_stops_on_stop_event(self, harness: _Harness) -> None:
        stop = asyncio.Event()
        stop.set()
        task = asyncio.ensure_future(harness.coordinator.run_poller(stop))
        done, _pending = await asyncio.wait([task], timeout=2)
        assert task in done


def _run(awaitable: Any) -> Any:
    """Run one coroutine to completion (sync tests drive async units)."""
    import asyncio

    return asyncio.run(awaitable)


# ── Replay ────────────────────────────────────────────────────────────


class TestReplay:
    def test_happy_path_counters_and_removal(self, tmp_path: Path) -> None:
        spool = _make_spool(tmp_path, rows=2)
        calls: list[tuple[str, dict[str, Any]]] = []

        async def core_call(op: str, payload: dict[str, Any]) -> bool:
            calls.append((op, payload))
            return True

        report = _run(run_replay([spool], core_call=core_call, service_token_available=True))
        assert report.planned == 2
        assert report.replayed == 2
        assert report.failed == 0
        assert report.rejected_reauthz == 0
        assert report.superseded == 0
        assert report.spools_removed == 1
        assert report.all_applied
        assert not spool.dir.exists()
        assert [op for op, _p in calls] == ["vesma_add", "vesma_add"]

    def test_order_preserved_within_spool(self, tmp_path: Path) -> None:
        spool = _make_spool(tmp_path, rows=3)
        seen: list[str] = []

        async def core_call(op: str, payload: dict[str, Any]) -> bool:
            seen.append(payload["content"])
            return True

        _run(run_replay([spool], core_call=core_call))
        assert seen == ["c0", "c1", "c2"]

    def test_transport_failure_fails_stops_and_keeps_spool(self, tmp_path: Path) -> None:
        """A transient transport error fail-stops the spool: rows stay on
        disk (strict order — nothing leaps over an unconsumed row)."""
        spool = _make_spool(tmp_path, rows=2)
        calls: list[str] = []

        async def core_call(op: str, payload: dict[str, Any]) -> bool:
            calls.append(op)
            raise RuntimeError("transport down")

        report = _run(run_replay([spool], core_call=core_call))
        assert report.failed == 1
        assert len(calls) == 1, "fail-stop: the second row is not attempted"
        assert not report.all_applied
        assert spool.dir.exists(), "a failed row must keep every row on disk"

    def test_refused_rows_pocket_permanent_and_release_spool(self, tmp_path: Path) -> None:
        """F2: core refusals are the rejected-permanent pocket — consumed,
        never retried; when nothing is pending the spool completes."""
        spool = _make_spool(tmp_path, rows=2)
        calls: list[str] = []

        async def core_call(op: str, payload: dict[str, Any]) -> bool:
            calls.append(op)
            return False  # the core refuses the operation

        report = _run(run_replay([spool], core_call=core_call))
        assert report.rejected_permanent == 2
        assert report.failed == 0
        assert report.all_applied
        assert not spool.dir.exists(), "nothing pending — the spool completes"

    def test_chain_tamper_aborts_spool(self, tmp_path: Path) -> None:
        spool = _make_spool(tmp_path, rows=2)
        raw = spool.ops_path.read_text(encoding="utf-8").splitlines()
        row = json.loads(raw[0])
        row["payload"]["content"] = "FORGED"
        raw[0] = json.dumps(row, sort_keys=True, separators=(",", ":"))
        spool.ops_path.write_text("\n".join(raw) + "\n", encoding="utf-8")
        calls: list[str] = []

        async def core_call(op: str, payload: dict[str, Any]) -> bool:
            calls.append(op)
            return True

        report = _run(run_replay([spool], core_call=core_call))
        assert report.chain_problems
        assert calls == [], "tampered rows must never be replayed"
        assert spool.dir.exists()

    def test_torn_tail_skipped_and_reported(self, tmp_path: Path) -> None:
        spool = _make_spool(tmp_path, rows=1)
        with spool.ops_path.open("ab") as fh:
            fh.write(b'{"op_id": "torn"')
        calls: list[str] = []

        async def core_call(op: str, payload: dict[str, Any]) -> bool:
            calls.append(op)
            return True

        report = _run(run_replay([spool], core_call=core_call))
        assert report.skipped_tail is True
        assert report.replayed == 1
        assert any("torn" in p for p in report.problems)
        assert report.all_applied
        assert not spool.dir.exists()

    def test_secret_dirty_row_not_applied(self, tmp_path: Path) -> None:
        spool = _make_spool(tmp_path, rows=0)
        spool.append(op="vesma_add", payload={"content": "token ghp_" + "B" * 36})
        calls: list[str] = []

        async def core_call(op: str, payload: dict[str, Any]) -> bool:
            calls.append(op)
            return True

        report = _run(run_replay([spool], core_call=core_call))
        assert report.failed == 1
        assert calls == []
        assert any("secret re-scan" in p for p in report.problems)
        assert spool.dir.exists()

    def test_actor_policy_refusal_counts_rejected_reauthz(self, tmp_path: Path) -> None:
        spool = _make_spool(tmp_path, rows=2)
        calls: list[str] = []

        async def core_call(op: str, payload: dict[str, Any]) -> bool:
            calls.append(op)
            return True

        def policy(row: SpoolRow) -> bool:
            return False  # the actor's authorization no longer holds

        report = _run(run_replay([spool], core_call=core_call, actor_policy=policy))
        assert report.rejected_reauthz == 2
        assert calls == []
        assert report.all_applied
        assert any("rejected-reauthz" in p for p in report.problems)
        assert not spool.dir.exists(), "all rows consumed — the spool completes"

    def test_missing_service_token_refuses_everything(self, tmp_path: Path) -> None:
        spool = _make_spool(tmp_path, rows=2)
        calls: list[str] = []

        async def core_call(op: str, payload: dict[str, Any]) -> bool:
            calls.append(op)
            return True

        report = _run(run_replay([spool], core_call=core_call, service_token_available=False))
        assert report.planned == 2
        assert report.rejected_reauthz == 2
        assert calls == []
        assert any("service token" in p for p in report.problems)
        assert spool.dir.exists()

    def test_identity_refusal_aborts_the_run(self, tmp_path: Path) -> None:
        spool_a = _make_spool(tmp_path, rows=2, actor="alpha")
        spool_b = _make_spool(tmp_path, rows=1, actor="beta")
        calls: list[str] = []

        async def core_call(op: str, payload: dict[str, Any]) -> bool:
            calls.append(op)
            if len(calls) == 2:  # the core revokes the service token mid-run
                from vesma.resilience import ReplayAuthzError

                raise ReplayAuthzError("401")
            return True

        report = _run(run_replay([spool_a, spool_b], core_call=core_call))
        assert len(calls) == 2
        assert report.replayed == 1
        assert report.rejected_reauthz == 1, "the row the identity refusal hit"
        assert any("aborted" in p for p in report.problems)
        assert any("aborted" in p for p in report.problems)
        assert spool_a.dir.exists() and spool_b.dir.exists()

    def test_multiple_spools_apply_independently(self, tmp_path: Path) -> None:
        spool_a = _make_spool(tmp_path, rows=1, actor="alpha")
        spool_b = _make_spool(tmp_path, rows=1, actor="beta")
        calls: list[str] = []

        async def core_call(op: str, payload: dict[str, Any]) -> bool:
            calls.append(payload["content"])
            return True

        report = _run(run_replay([spool_a, spool_b], core_call=core_call))
        assert report.replayed == 2
        assert report.spools_removed == 2
        assert calls == ["c0", "c0"]  # alpha first, then beta (deterministic)

    def test_envelope_key_reaches_the_core(self, tmp_path: Path) -> None:
        spool = _make_spool(tmp_path, rows=1)
        captured: list[dict[str, Any]] = []

        async def core_call(op: str, payload: dict[str, Any]) -> bool:
            captured.append(payload)
            return True

        _run(run_replay([spool], core_call=core_call))
        assert captured
        assert "vesma_degraded_op_id" in captured[0]


def _make_spool(tmp_path: Path, *, rows: int, actor: str = "mcp-front") -> Spool:
    spool = Spool.open(
        tmp_path / "spool",
        instance="inst",
        actor=actor,
        degraded_since=1000.0,
        max_duration_sec=3600,
        clock=lambda: 1000.0,
    )
    for i in range(rows):
        spool.append(op="vesma_add", payload={"content": f"c{i}"})
    return spool


# ── Front integration ─────────────────────────────────────────────────


class _FakeCore:
    """A Starlette app standing in for the core /mcp plane."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []
        self.app = Starlette(
            routes=[
                Route(
                    "/{path:path}",
                    self._handle,
                    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
                )
            ]
        )

    async def _handle(self, request: Request) -> Any:
        body = await request.body()
        self.seen.append({"method": request.method, "headers": dict(request.headers), "body": body})
        return JSONResponse({"jsonrpc": "2.0", "id": 1, "result": {"content": []}})


class _DeadCore(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)


def _front_with(coordinator: ResilienceCoordinator, core: Any) -> McpFront:
    """A front wired to an in-process fake core (the wrapper or its app)."""
    app = getattr(core, "app", core)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
    return McpFront(client=client, resilience=coordinator)


async def _call(front: McpFront, content: bytes | str = b"x", **kwargs: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=front)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as caller:
        return await caller.request("POST", "/mcp", content=content, **kwargs)


def _tool_body(name: str, arguments: dict[str, Any] | None = None, jsonrpc_id: int = 1) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": jsonrpc_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
    ).encode()


class TestFrontDegraded:
    async def test_without_coordinator_w2_behavior_unchanged(self, tmp_path: Path) -> None:
        core = _FakeCore()
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=core.app))
        front = McpFront(client=client)
        response = await _call(front, content=_tool_body("vesma_add"))
        assert response.status_code == 200
        assert "x-vesma-degraded" not in response.headers

        dead = McpFront(client=httpx.AsyncClient(transport=_DeadCore()))
        down = await _call(dead, content=_tool_body("vesma_add"))
        assert down.status_code == 503
        assert "x-vesma-degraded" not in down.headers

    async def test_three_failures_flip_to_degraded(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path, _Clock())
        front = McpFront(client=httpx.AsyncClient(transport=_DeadCore()), resilience=h.coordinator)
        for _ in range(3):
            response = await _call(front, content=b"ping")
            assert response.status_code == 503
        assert h.coordinator.current_mode() == ResilienceMode.DEGRADED

    async def test_degraded_write_gets_receipt_and_spools(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path, _Clock())
        _degraded(h)
        front = _front_with(h.coordinator, _FakeCore())
        response = await _call(front, content=_tool_body("vesma_add", {"content": "x"}))
        assert response.status_code == 200
        assert response.headers["x-vesma-degraded"].startswith("since=")
        answer = response.json()
        assert answer["id"] == 1
        assert answer["result"]["isError"] is False
        assert "op_id=" in answer["result"]["content"][0]["text"]
        rows, _torn = h.coordinator.discover_spools()[0].read_rows()
        assert rows[0].payload == {"content": "x"}

    async def test_degraded_read_with_dead_core_stays_honest_503(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path, _Clock())
        _degraded(h)
        front = McpFront(client=httpx.AsyncClient(transport=_DeadCore()), resilience=h.coordinator)
        response = await _call(front, content=_tool_body("vesma_search", {"query": "q"}))
        assert response.status_code == 503
        assert "x-vesma-degraded" in response.headers

    async def test_degraded_read_with_live_core_proxies_and_flips_replay(
        self, tmp_path: Path
    ) -> None:
        """A successful read IS a liveness proof: the machine moves to
        REPLAY (the poller's next pass delivers the spool)."""
        h = _Harness(tmp_path, _Clock())
        _degraded(h)
        front = _front_with(h.coordinator, _FakeCore())
        response = await _call(front, content=_tool_body("vesma_search", {"query": "q"}))
        assert response.status_code == 200
        assert "x-vesma-degraded" in response.headers
        assert h.coordinator.current_mode() == ResilienceMode.REPLAY

    async def test_degraded_garbage_body_proxies_transparently(self, tmp_path: Path) -> None:
        """An unparseable POST is a read for our purposes — the core
        answers it with its own error once it is back."""
        h = _Harness(tmp_path, _Clock())
        _degraded(h)
        front = _front_with(h.coordinator, _FakeCore())
        response = await _call(front, content=b"\x00not-json")
        assert response.status_code == 200

    async def test_blocked_write_answers_typed_refusal(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path, _Clock())
        _degraded(h)
        h.clock.advance(3601)
        front = _front_with(h.coordinator, _FakeCore())
        response = await _call(front, content=_tool_body("vesma_add", {"content": "x"}))
        assert response.status_code == 200
        answer = response.json()
        assert answer["result"]["isError"] is True
        assert "DegradedTTLExceeded" in answer["result"]["content"][0]["text"]
        assert "x-vesma-degraded" in response.headers

    async def test_replay_mode_signage_on_proxied_responses(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path, _Clock())
        state = MachineState(mode=ResilienceMode.REPLAY, degraded_since=h.clock(), ttl_sec=3600)
        h.coordinator._save(state)
        core = _FakeCore()
        front = _front_with(h.coordinator, core)
        response = await _call(front, content=_tool_body("vesma_search"))
        assert response.status_code == 200
        assert "x-vesma-degraded" in response.headers
        assert core.seen, "REPLAY mode still proxies live traffic"

    async def test_healthz_carries_degraded_section(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path, _Clock())
        _degraded(h)
        _seed_row(h)
        front = _front_with(h.coordinator, _FakeCore())
        transport = httpx.ASGITransport(app=front)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as caller:
            health = await caller.get("/healthz")
        assert health.status_code == 200
        body = health.json()
        assert body["degraded"]["mode"] == ResilienceMode.DEGRADED
        assert body["degraded"]["spool_ops_pending"] == 1
        assert "x-vesma-degraded" in health.headers

    async def test_recovered_core_replays_through_its_plane(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path, _Clock())
        _degraded(h)
        _seed_row(h, payload={"content": "late write"})
        front = _front_with(h.coordinator, _FakeCore())
        # A live read proves the core is back: the machine moves to REPLAY.
        response = await _call(front, content=_tool_body("vesma_search"))
        assert response.status_code == 200
        assert h.coordinator.current_mode() == ResilienceMode.REPLAY
        outcome = await h.coordinator.poll_once()
        assert outcome.replay is not None
        assert outcome.replay.replayed == 1
        assert h.coordinator.current_mode() == ResilienceMode.HEALTHY
        assert h.coordinator.discover_spools() == []
        # A post-recovery write passes THROUGH to the core (no receipt).
        core = _FakeCore()
        front2 = _front_with(h.coordinator, core)
        response = await _call(front2, content=_tool_body("vesma_add", {"content": "y"}))
        assert response.status_code == 200
        assert len(core.seen) == 1

    async def test_lifespan_starts_and_stops_the_poller(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path, _Clock())
        front = _front_with(h.coordinator, _FakeCore())
        started: list[bool] = []
        cancelled: list[bool] = []

        async def spy_poller(stop: Any = None) -> None:
            started.append(True)
            try:
                await asyncio.sleep(3600)  # would hang the test if not cancelled
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        h.coordinator.run_poller = spy_poller  # type: ignore[method-assign]
        messages = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])

        async def receive() -> Any:
            await asyncio.sleep(0)  # yield so the spawned poller task steps
            return next(messages)

        sent: list[Any] = []

        async def send(message: Any) -> None:
            sent.append(message)

        await front({"type": "lifespan"}, receive, send)
        kinds = [m["type"] for m in sent]
        assert "lifespan.startup.complete" in kinds
        assert "lifespan.shutdown.complete" in kinds
        assert started == [True], "startup must spawn the poller task"
        assert cancelled == [True], "shutdown must cancel the poller task"
        assert front._resilience_poller is None


class TestProductionReplayLeg:
    """The httpx-backed production core_call (MockTransport — no
    sockets; the client factory is patched for the duration of a test)."""

    def _front(
        self, monkeypatch: pytest.MonkeyPatch, handler: Any, token: str | None = "svc"
    ) -> McpFront:
        real_client = httpx.AsyncClient

        def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs.pop("transport", None)
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        front = McpFront(target="http://127.0.0.1:8787/mcp/")
        front._resilience = ResilienceCoordinator(
            _settings(service_token=token),
            state_home=Path("/tmp/never-used-prod-leg"),
        )
        return front

    async def test_401_raises_identity_refusal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["authorization"] == "Bearer svc"
            return httpx.Response(401)

        front = self._front(monkeypatch, handler)
        with pytest.raises(ReplayAuthzError):
            await front._production_core_call("vesma_add", {})

    async def test_is_error_result_counts_as_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"content": [], "isError": True},
                },
            )

        front = self._front(monkeypatch, handler)
        assert await front._production_core_call("vesma_add", {}) is False

    async def test_jsonrpc_error_counts_as_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {}})

        front = self._front(monkeypatch, handler)
        assert await front._production_core_call("vesma_add", {}) is False

    async def test_transport_failure_counts_as_row_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        front = self._front(monkeypatch, handler)
        with pytest.raises(RuntimeError):
            await front._production_core_call("vesma_add", {})

    async def test_happy_result_counts_as_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["method"] == "tools/call"
            assert body["params"]["name"] == "vesma_add"
            assert "vesma_degraded_op_id" in body["params"]["arguments"]
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"content": []}})

        front = self._front(monkeypatch, handler)
        row = SpoolRow(
            op_id="op-9",
            ts_utc="2026-11-04T00:00:00Z",
            ts=0.0,
            actor="a",
            op="vesma_add",
            payload={"content": "x"},
            prev_hash=None,
            row_hash="h",
        )
        # The production leg receives the ALREADY-enveloped payload (the
        # envelope is run_replay's job) — mirror that here.
        assert await front._production_core_call("vesma_add", replay_envelope(row)) is True

    async def test_missing_service_token_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        front = self._front(monkeypatch, lambda request: httpx.Response(200), token=None)
        with pytest.raises(ReplayAuthzError):
            await front._production_core_call("vesma_add", {})


# ── DR-24 ─────────────────────────────────────────────────────────────


class _SettingsStub:
    def __init__(self, degraded: Any) -> None:
        self.resilience = type("R", (), {"degraded": degraded})()


def _cfg(**overrides: Any) -> Any:
    from vesma.config import ResilienceDegradedConfig

    values: dict[str, Any] = {"enabled": True, "max_duration": 3600}
    values.update(overrides)
    return ResilienceDegradedConfig(**values)


class TestDr24:
    def _loader(self, degraded: Any) -> Any:
        return lambda: _SettingsStub(degraded)

    def test_no_artifacts_and_no_config_is_ok(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("VESMA_STATE_HOME", str(tmp_path / "state"))
        monkeypatch.setenv("VESMA_FRONT_INSTANCE", "inst")
        finding = doctor_checks._dr24(self._loader(None))
        assert finding.severity == Severity.OK
        assert "refuse-writes posture" in finding.detail

    def test_no_artifacts_with_config_is_ok(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("VESMA_STATE_HOME", str(tmp_path / "state"))
        monkeypatch.setenv("VESMA_FRONT_INSTANCE", "inst")
        finding = doctor_checks._dr24(self._loader(_cfg()))
        assert finding.severity == Severity.OK

    def test_config_load_failure_fails(self) -> None:
        def broken() -> Any:
            raise RuntimeError("no config")

        finding = doctor_checks._dr24(broken)
        assert finding.severity == Severity.FAIL

    def test_artifacts_without_config_warn(self, tmp_path, monkeypatch) -> None:
        harness = _Harness(tmp_path, _RealClock())
        _degraded(harness)
        _seed_row(harness)
        monkeypatch.setenv("VESMA_STATE_HOME", str(harness.coordinator.spool_root.parent.parent))
        monkeypatch.setenv("VESMA_FRONT_INSTANCE", "testinst")
        finding = doctor_checks._dr24(self._loader(None))
        assert finding.severity == Severity.WARN
        assert "orphan of a previously-enabled deployment" in finding.detail

    def test_young_degraded_spool_warns(self, tmp_path, monkeypatch) -> None:
        harness = _Harness(tmp_path, _RealClock())
        _degraded(harness)
        _seed_row(harness)
        monkeypatch.setenv("VESMA_STATE_HOME", str(harness.coordinator.spool_root.parent.parent))
        monkeypatch.setenv("VESMA_FRONT_INSTANCE", "testinst")
        finding = doctor_checks._dr24(self._loader(_cfg()))
        assert finding.severity == Severity.WARN
        assert "pending ops=1" in finding.detail

    def test_ttl_over_80_percent_fails(self, tmp_path, monkeypatch) -> None:
        import time as _time

        harness = _Harness(tmp_path, _RealClock())
        _degraded(harness, since=_time.time() - int(0.9 * 3600))
        _seed_row(harness)
        monkeypatch.setenv("VESMA_STATE_HOME", str(harness.coordinator.spool_root.parent.parent))
        monkeypatch.setenv("VESMA_FRONT_INSTANCE", "testinst")
        finding = doctor_checks._dr24(self._loader(_cfg()))
        assert finding.severity == Severity.FAIL
        assert "TTL escalation" in finding.detail

    def test_blocked_mode_fails(self, tmp_path, monkeypatch) -> None:
        import time as _time

        harness = _Harness(tmp_path, _RealClock())
        _degraded(harness, since=_time.time() - 7200)
        _seed_row(harness)
        monkeypatch.setenv("VESMA_STATE_HOME", str(harness.coordinator.spool_root.parent.parent))
        monkeypatch.setenv("VESMA_FRONT_INSTANCE", "testinst")
        finding = doctor_checks._dr24(self._loader(_cfg()))
        assert finding.severity == Severity.FAIL

    def test_weakened_mode_fails(self, tmp_path, monkeypatch) -> None:
        harness = _Harness(tmp_path, _RealClock())
        _degraded(harness)
        _seed_row(harness)
        spool = harness.coordinator.discover_spools()[0]
        os.chmod(spool.ops_path, 0o644)
        monkeypatch.setenv("VESMA_STATE_HOME", str(harness.coordinator.spool_root.parent.parent))
        monkeypatch.setenv("VESMA_FRONT_INSTANCE", "testinst")
        finding = doctor_checks._dr24(self._loader(_cfg()))
        assert finding.severity == Severity.FAIL
        assert "weakened file mode" in finding.detail

    def test_chain_tamper_fails(self, tmp_path, monkeypatch) -> None:
        harness = _Harness(tmp_path, _RealClock())
        _degraded(harness)
        _seed_row(harness)
        _seed_row(harness)
        spool = harness.coordinator.discover_spools()[0]
        raw = spool.ops_path.read_text(encoding="utf-8").splitlines()
        row = json.loads(raw[0])
        row["payload"]["content"] = "FORGED"
        raw[0] = json.dumps(row, sort_keys=True, separators=(",", ":"))
        spool.ops_path.write_text("\n".join(raw) + "\n", encoding="utf-8")
        monkeypatch.setenv("VESMA_STATE_HOME", str(harness.coordinator.spool_root.parent.parent))
        monkeypatch.setenv("VESMA_FRONT_INSTANCE", "testinst")
        finding = doctor_checks._dr24(self._loader(_cfg()))
        assert finding.severity == Severity.FAIL
        assert any("chain:" in s for s in finding.detail.split("; "))

    def test_orphan_replay_tail_warns(self, tmp_path, monkeypatch) -> None:
        harness = _Harness(tmp_path, _RealClock())
        _degraded(harness)
        _seed_row(harness)
        spool = harness.coordinator.discover_spools()[0]
        (spool.dir / (Spool.OPS_NAME + Spool.DONE_SUFFIX)).write_text("", encoding="utf-8")
        monkeypatch.setenv("VESMA_STATE_HOME", str(harness.coordinator.spool_root.parent.parent))
        monkeypatch.setenv("VESMA_FRONT_INSTANCE", "testinst")
        finding = doctor_checks._dr24(self._loader(_cfg()))
        assert finding.severity == Severity.WARN
        assert "orphaned replay tail" in finding.detail

    def test_healthy_state_with_spool_artifacts_warns(self, tmp_path, monkeypatch) -> None:
        harness = _Harness(tmp_path, _RealClock())
        _degraded(harness)
        _seed_row(harness)
        state = MachineState(mode=ResilienceMode.HEALTHY)
        harness.coordinator._save(state)
        # A CORRUPTED header is not an arbitration anchor — the leftover
        # tree under a HEALTHY state is exactly the orphan shape.
        spool = harness.coordinator.discover_spools()[0]
        spool.header_path.write_text("corrupted", encoding="utf-8")
        monkeypatch.setenv("VESMA_STATE_HOME", str(harness.coordinator.spool_root.parent.parent))
        monkeypatch.setenv("VESMA_FRONT_INSTANCE", "testinst")
        finding = doctor_checks._dr24(self._loader(_cfg()))
        assert finding.severity == Severity.WARN
        assert "orphaned replay tail" in finding.detail or "HEALTHY machine" in finding.detail

    def test_state_file_without_spool_is_info(self, tmp_path, monkeypatch) -> None:
        harness = _Harness(tmp_path, _RealClock())
        _degraded(harness)
        # No rows appended — state exists, no actor spool dir yet.
        monkeypatch.setenv("VESMA_STATE_HOME", str(harness.coordinator.spool_root.parent.parent))
        monkeypatch.setenv("VESMA_FRONT_INSTANCE", "testinst")
        finding = doctor_checks._dr24(self._loader(_cfg()))
        assert finding.severity == Severity.OK
        assert "header-arbitration corner" in finding.detail


# ── Config (Settings.resilience) ──────────────────────────────────────


def _settings_kwargs(**resilience: Any) -> dict[str, Any]:
    return {
        "vesma": {"vault_path": "/tmp/rs-v", "data_dir": "/tmp/rs-d"},
        "embedding": {"provider": "onnx"},
        "scanner": {"enabled": False},
        "resilience": resilience,
    }


class TestResilienceConfig:
    def test_section_absent_is_refuse_writes_posture(self) -> None:
        settings = Settings(
            vesma={"vault_path": "/tmp/rs-v", "data_dir": "/tmp/rs-d"},
            embedding={"provider": "onnx"},
            scanner={"enabled": False},
        )
        assert settings.resilience.degraded is None

    def test_full_section_parses(self) -> None:
        settings = Settings(
            **_settings_kwargs(
                degraded={
                    "enabled": True,
                    "max_duration": "7d",
                    "poll_interval": "45s",
                    "backoff_max": "300s",
                    "spool_max_bytes": 2048,
                    "signage": {"log_interval": "5m"},
                    "notify_url": "https://hooks.example/vesma",
                }
            )
        )
        degraded = settings.resilience.degraded
        assert degraded is not None
        assert degraded.enabled is True
        assert degraded.max_duration == 7 * 86400
        assert degraded.poll_interval == 45
        assert degraded.backoff_max == 300
        assert degraded.spool_max_bytes == 2048
        assert degraded.signage.log_interval == 300
        assert degraded.notify_url == "https://hooks.example/vesma"

    def test_max_duration_required_no_default(self) -> None:
        with pytest.raises(ValidationError):
            Settings(**_settings_kwargs(degraded={"enabled": True}))

    def test_enabled_required_no_default(self) -> None:
        with pytest.raises(ValidationError):
            Settings(**_settings_kwargs(degraded={"max_duration": "7d"}))

    def test_extra_forbid_refuses_unknown_key(self) -> None:
        with pytest.raises(ValidationError):
            Settings(
                **_settings_kwargs(degraded={"enabled": True, "max_duration": "1h", "knob": 1})
            )

    def test_notify_url_scheme_guarded(self) -> None:
        with pytest.raises(ValidationError):
            Settings(
                **_settings_kwargs(
                    degraded={"enabled": True, "max_duration": "1h", "notify_url": "file:///x"}
                )
            )

    def test_disabled_section_is_legal(self) -> None:
        settings = Settings(**_settings_kwargs(degraded={"enabled": False, "max_duration": "1h"}))
        assert settings.resilience.degraded is not None
        assert settings.resilience.degraded.enabled is False

    def test_example_config_file_parses(self) -> None:
        """The shipped example (with the resilience section) must parse —
        the documented shape never drifts from the schema."""
        import yaml

        raw = yaml.safe_load(
            (Path(__file__).resolve().parent.parent / "config.example.yaml").read_text(
                encoding="utf-8"
            )
        )
        settings = Settings(
            **{
                "vesma": {"vault_path": "/tmp/rs-v", "data_dir": "/tmp/rs-d"},
                "embedding": {"provider": "onnx"},
                "scanner": {"enabled": False},
                **raw,
            }
        )
        assert settings.resilience.degraded is not None
        assert settings.resilience.degraded.max_duration == 7 * 86400


# ── build_resilience (front child wiring) ─────────────────────────────


class TestBuildResilience:
    def test_missing_env_returns_none(self) -> None:
        assert build_resilience({}) is None

    def test_env_builds_coordinator(self, tmp_path: Path) -> None:
        coordinator = build_resilience(
            {"VESMA_FRONT_DEGRADED_MAX_DURATION": "2h", "VESMA_FRONT_INSTANCE": "instx"},
            state_home=tmp_path,
        )
        assert coordinator is not None
        assert coordinator.settings.max_duration_sec == 7200
        assert coordinator.instance_dir == tmp_path / "vesma" / "spool" / "instx"

    def test_malformed_env_raises(self) -> None:
        with pytest.raises(ValueError):
            build_resilience({"VESMA_FRONT_DEGRADED_MAX_DURATION": "junk"})


# ── Cascade-review fix slice (F1..F11) ────────────────────────────────


class TestCoreAnswerAccepted:
    """F1b: the front's acceptance predicate (defense-in-depth)."""

    def test_happy_result_accepted(self) -> None:
        answer = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "ok"}]},
        }
        assert core_answer_accepted(answer) is True

    def test_is_error_refused(self) -> None:
        answer = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "boom"}], "isError": True},
        }
        assert core_answer_accepted(answer) is False

    def test_loud_refusal_prefix_refused_even_without_is_error(self) -> None:
        """Defense-in-depth: the ❌ rendering is recognizable even when a
        core regression drops isError — a regression must degrade to loud
        re-delivery, never to silent spool deletion."""
        answer = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "❌ Namespace violation: …"}]},
        }
        assert core_answer_accepted(answer) is False

    def test_jsonrpc_error_object_refused(self) -> None:
        assert core_answer_accepted({"jsonrpc": "2.0", "id": 1, "error": {"code": -1}}) is False

    def test_malformed_no_result_refused(self) -> None:
        assert core_answer_accepted({"jsonrpc": "2.0", "id": 1}) is False
        assert core_answer_accepted("not a dict") is False


class TestF1RealOnCallTool:
    """F1 pin through the REAL `_on_call_tool`: a genuine tool refusal
    carries isError=True and the front predicate refuses it."""

    @pytest.fixture
    def real_store(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        from unittest.mock import MagicMock

        from vesma import mcp_server
        from vesma.manager import MemoryManager

        tmp = Path(tempfile.mkdtemp())
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
        mgr = MemoryManager(settings)
        mock_embedder = MagicMock()
        mock_embedder.embed.return_value = [0.1] * 384
        mgr._embedder = mock_embedder
        monkeypatch.setattr(mcp_server, "_manager", mgr)
        yield mgr
        mcp_server._manager = None  # type: ignore[attr-defined]
        mgr.close()

    async def test_refusal_is_error_and_predicate_refuses(self, real_store: Any) -> None:
        from mcp.types import CallToolRequestParams

        from vesma import mcp_server

        identity = set_request_identity(McpIdentity(agent_id="harness-a", jti="jti-f1"))
        try:
            result = await mcp_server._on_call_tool(
                None,
                CallToolRequestParams(
                    name="vesma_add",
                    arguments={
                        "content": "foreign mint under the service token",
                        "tags": [
                            "project:f1-pin",
                            "agent:other-agent",
                            "vesma:learning",
                        ],
                    },
                ),
            )
        finally:
            reset_request_identity(identity)
        assert result.is_error is True, (
            "F1a: the ❌ namespace violation sets the semantic-error flag"
        )
        answer = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": c.type, "text": c.text} for c in result.content],
                "isError": result.is_error,  # the WIRE key (SDK alias)
            },
        }
        assert core_answer_accepted(answer) is False, (
            "F1: a refused row must be counted a refusal by the replay leg — "
            "never a success that would delete the spool"
        )

    async def test_success_is_not_error_and_predicate_accepts(self, real_store: Any) -> None:
        from mcp.types import CallToolRequestParams

        from vesma import mcp_server

        identity = set_request_identity(McpIdentity(agent_id="harness-a", jti="jti-f1"))
        try:
            result = await mcp_server._on_call_tool(
                None,
                CallToolRequestParams(
                    name="vesma_add",
                    arguments={
                        "content": "clean write under the matching namespace",
                        "tags": [
                            "project:f1-pin",
                            "agent:harness-a",
                            "vesma:learning",
                        ],
                    },
                ),
            )
        finally:
            reset_request_identity(identity)
        assert result.is_error is False
        answer = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": c.type, "text": c.text} for c in result.content],
                "isError": result.is_error,  # the WIRE key (SDK alias)
            },
        }
        assert core_answer_accepted(answer) is True


class TestF2PerRowCommit:
    def test_poison_row_does_not_multiply_siblings(self, tmp_path: Path) -> None:
        """THE F2 blocker scenario: a refused row (identity-tagged write
        under the service token) must not hold the spool and re-apply its
        siblings on every pass. Pass 1 = crash on the poison row's
        transport; pass 2 resumes from the watermark — the applied sibling
        is NOT re-applied."""
        spool = _make_spool(tmp_path, rows=0)
        spool.append(op="vesma_add", payload={"content": "good-1"})
        row2 = spool.append(op="vesma_add", payload={"content": "poison-client-tag"})
        row3 = spool.append(op="vesma_add", payload={"content": "good-3"})
        watermarks: dict[str, str] = {}
        applied: list[str] = []
        crashing = True  # pass 1: the transport dies on good-3

        async def core_call(op: str, payload: dict[str, Any]) -> bool:
            content = str(payload.get("content"))
            if "poison" in content:
                return False  # the core refuses: namespace violation
            if crashing and content == "good-3":
                raise RuntimeError("transport down")  # pass-1 crash leg
            applied.append(content)
            return True

        def on_consumed(s: Spool, row: SpoolRow) -> None:
            watermarks[s.actor] = row.op_id

        report1 = _run(
            run_replay(
                [spool],
                core_call=core_call,
                resume_from=dict(watermarks),
                on_row_consumed=on_consumed,
            )
        )
        assert report1.replayed == 1  # good-1
        assert report1.rejected_permanent == 1  # the poison row, pocketed
        assert report1.failed == 1  # good-3's transport crash
        assert watermarks == {"mcp-front": row2.op_id}, (
            "per-row commit persisted: good-1 AND the pocketed poison row"
        )
        assert spool.dir.exists()

        crashing = False  # pass 2: the transport is back
        report2 = _run(
            run_replay(
                [spool],
                core_call=core_call,
                resume_from=dict(watermarks),
                on_row_consumed=on_consumed,
            )
        )
        # good-1 applied EXACTLY ONCE across both passes — no duplicates;
        # the pocketed poison row never retried.
        assert applied == ["good-1", "good-3"], (
            "the applied sibling must not re-apply after the per-row commit"
        )
        assert report2.rejected_permanent == 0
        assert report2.replayed == 1
        assert watermarks == {"mcp-front": row3.op_id}
        assert not spool.dir.exists(), "all rows consumed — the spool completes"

    def test_report_counters_and_pocket_visibility(self, tmp_path: Path) -> None:
        spool = _make_spool(tmp_path, rows=1)

        async def core_call(op: str, payload: dict[str, Any]) -> bool:
            return False

        report = _run(run_replay([spool], core_call=core_call))
        assert report.rejected_permanent == 1
        assert "rejected_permanent=1" in report.summary()


class TestF3MidFileCorruption:
    def test_mid_file_broken_line_aborts_loudly_and_preserves_rows(self, tmp_path: Path) -> None:
        """F3: a COMPLETE line that does not parse (mid-file) is hard
        corruption — the replay aborts loudly, the surviving rows stay on
        disk, and no auto-truncate touches the file."""
        spool = _make_spool(tmp_path, rows=2)
        raw = spool.ops_path.read_text(encoding="utf-8").splitlines(keepends=True)
        raw[0] = '{"corrupted": tru\n'  # complete line (has its newline), garbage
        spool.ops_path.write_text("".join(raw), encoding="utf-8")

        _rows, torn = spool.read_rows()
        assert torn is False, "a newline-terminated garbage line is NOT the torn-tail class"
        _rows, problems = spool.verify_chain()
        assert any("does not parse" in p for p in problems)

        before = spool.ops_path.read_bytes()
        Spool(spool.dir, actor=spool.actor).recover_for_append()
        assert spool.ops_path.read_bytes() == before, "no auto-repair of a mid-file corruption"

        calls: list[str] = []

        async def core_call(op: str, payload: dict[str, Any]) -> bool:
            calls.append(op)
            return True

        report = _run(run_replay([spool], core_call=core_call))
        assert report.chain_problems, "the spool's replay aborts loudly"
        assert calls == [], "corrupted rows are never replayed"
        assert spool.dir.exists()
        assert "c1" in spool.ops_path.read_text(encoding="utf-8"), "the surviving row stays on disk"

    def test_torn_tail_still_only_the_last_segment(self, tmp_path: Path) -> None:
        """The torn-tail class stays exactly the trailing segment without
        its newline — tolerated on read, repaired on the next append."""
        spool = _make_spool(tmp_path, rows=2)
        with spool.ops_path.open("ab") as fh:
            fh.write(b'{"op_id": "torn"')
        rows, torn = spool.read_rows()
        assert torn is True
        assert len(rows) == 2
        spool.recover_for_append()
        rows2, problems = spool.verify_chain()
        assert problems == []
        assert len(rows2) == 2


class TestF4NoFlapping:
    def test_restart_with_replayed_leftover_does_not_flap(self, tmp_path: Path) -> None:
        """The crash between the atomic rename and the tree removal leaves
        header + ops.jsonl.replayed; the restarted machine must purge it
        in the REPLAY pass and settle at HEALTHY (no DEGRADED flapping
        against a live core)."""
        h = _Harness(tmp_path, _Clock())
        _degraded(h)
        spool = h.coordinator.open_spool(h.coordinator.actor, h.coordinator.state())
        spool.append(op="vesma_add", payload={"content": "x"})
        os.replace(
            spool.ops_path,
            spool.ops_path.with_name(Spool.OPS_NAME + Spool.DONE_SUFFIX),
        )

        revived = ResilienceCoordinator(
            h.settings,
            state_home=tmp_path / "state",
            clock=h.clock,
            probe=h._probe,
            core_call=h._core_call,
        )
        # Arbitration restores DEGRADED from the header (correct context).
        assert revived.current_mode() == ResilienceMode.DEGRADED
        outcome = _run(revived.poll_once())  # probe ok → REPLAY → purge
        assert outcome.mode_after == ResilienceMode.HEALTHY
        assert outcome.replay is not None
        assert outcome.replay.spools_purged >= 1
        assert revived.discover_spools() == []
        # The load AFTER the purge stays HEALTHY — no arbitration flap.
        assert revived.current_mode() == ResilienceMode.HEALTHY

    def test_headerless_with_rows_never_auto_deleted(self, tmp_path: Path) -> None:
        """F-N2: a headerless directory HOLDING ROWS is never
        auto-deleted (the F3 manual-resolution semantics) — the purge
        touches only truly empty leftovers."""
        h = _Harness(tmp_path, _Clock())
        _degraded(h)
        spool = h.coordinator.open_spool(h.coordinator.actor, h.coordinator.state())
        spool.append(op="vesma_add", payload={"content": "precious"})
        # A crashed creation: header gone, the ops file (with rows) left.
        spool.header_path.unlink()
        _run(h.coordinator.poll_once())
        assert spool.dir.exists(), "headerless + rows survive (never auto-delete)"
        assert "precious" in spool.ops_path.read_text(encoding="utf-8")

    def test_headerless_empty_leftover_purged(self, tmp_path: Path) -> None:
        h = _Harness(tmp_path, _Clock())
        _degraded(h)
        spool = h.coordinator.open_spool(h.coordinator.actor, h.coordinator.state())
        spool.append(op="vesma_add", payload={"content": "x"})
        # A crashed creation: header gone, the ops file later consumed —
        # an ops-less directory is the only headerless purge case.
        spool.header_path.unlink()
        spool.ops_path.unlink()
        _run(h.coordinator.poll_once())
        assert not spool.dir.exists(), "the headerless EMPTY leftover is purged"


def test_concurrent_actor_appends_interleave_cleanly(tmp_path: Path) -> None:
    """Two actors appending concurrently: per-actor files keep their
    chains independent and verifiable; discovery finds both."""
    spool_a = _make_spool(tmp_path, rows=0, actor="alpha")
    spool_b = _make_spool(tmp_path, rows=0, actor="beta")

    async def interleave() -> None:
        for i in range(5):
            spool_a.append(op="vesma_add", payload={"content": f"a{i}"})
            spool_b.append(op="vesma_add", payload={"content": f"b{i}"})
            await asyncio.sleep(0)

    _run(interleave())
    for spool, prefix in ((spool_a, "a"), (spool_b, "b")):
        rows, problems = spool.verify_chain()
        assert problems == []
        assert [r.payload["content"] for r in rows] == [f"{prefix}{i}" for i in range(5)]
    root = spool_a.dir.parent
    found = Spool.discover(root)
    assert {s.actor for s in found} == {"alpha", "beta"}


# ── F5: the curated classifier map (full registry coverage) ──────────


#: EVERY canonical tool name with its RATIFIED classification. The graph
#: tools (incl. the graph-INDEX mutations ``vesma_register_project`` /
#: ``vesma_index_project`` / ``vesma_delete_graph_project``) are
#: deliberate READ exceptions: during degradation they answer the loud
#: 503 and re-run after recovery — the SAFE direction (a spool-and-replay
#: of an index deletion under the service token, outside the graph
#: attribution contracts, would be the unsafe one). Nothing is lost: the
#: caller retries when the core is back.
_CURATED_CLASSIFIER_MAP: dict[str, str] = {
    # durable memory/store writes
    "vesma_add": "write",
    "vesma_save_context": "write",
    "vesma_filter": "write",
    "vesma_tags_rename": "write",
    "vesma_import": "write",
    "vesma_reprocess": "write",
    "vesma_context_rewrite": "write",
    "vesma_compress": "write",
    "vesma_ingest_url": "write",
    "vesma_ingest_document": "write",
    "vesma_watch_start": "write",
    "vesma_watch_stop": "write",
    # reads (incl. the documented graph/register exceptions)
    "vesma_search": "read",
    "vesma_agent_recall": "read",
    "vesma_recall_context": "read",
    "vesma_list_recent": "read",
    "vesma_list_tags": "read",
    "vesma_stats": "read",
    "vesma_retrieve": "read",
    "vesma_export": "read",
    "vesma_usage_report": "read",
    "vesma_align_prefix": "read",
    "vesma_assemble_context": "read",
    "vesma_ambient_brief": "read",
    "vesma_auto_collect_status": "read",
    "vesma_watch_status": "read",
    # graph plane — the documented exceptions live here
    "vesma_search_graph": "read",
    "vesma_trace_path": "read",
    "vesma_get_code_snippet": "read",
    "vesma_get_file_outline": "read",
    "vesma_get_graph_schema": "read",
    "vesma_check_graph_coverage": "read",
    "vesma_list_graph_projects": "read",
    "vesma_project_graph_status": "read",
    "vesma_register_project": "read",
    "vesma_index_project": "read",
    "vesma_delete_graph_project": "read",
}


class TestF5CuratedClassifierMap:
    def test_map_covers_the_whole_registry(self) -> None:
        import asyncio

        from vesma import mcp_server

        names = {t.name for t in asyncio.run(mcp_server._canonical_tools())}
        mapped = set(_CURATED_CLASSIFIER_MAP) | set(_DUAL_MODE_TOOLS_TEST)
        assert names == mapped, (
            "the curated map must name EVERY canonical tool exactly once — "
            f"missing={sorted(names - mapped)}, extra={sorted(mapped - names)}"
        )

    def test_every_entry_classifies_as_curated(self) -> None:
        for name, expected in _CURATED_CLASSIFIER_MAP.items():
            assert classify_tool_call(name, {"action": "get"}) == expected, name

    def test_dual_tools_route_by_action(self) -> None:
        assert classify_tool_call("vesma_tags", {"action": "add"}) == "write"
        assert classify_tool_call("vesma_tags", {"action": "get"}) == "read"
        assert classify_tool_call("vesma_workflow", {"action": "set"}) == "write"
        assert classify_tool_call("vesma_workflow", {"action": "history"}) == "read"
        assert classify_tool_call("vesma_awareness", {"action": "record_abstention"}) == "write"
        assert classify_tool_call("vesma_awareness", {"action": "pre_flight"}) == "read"
        assert classify_tool_call("vesma_hooks", {"action": "post_tool_call"}) == "write"
        assert classify_tool_call("vesma_hooks", {"action": "on_session_start"}) == "read"


#: The dual-mode map mirrored from vesma.resilience (kept here as a
#: test-local copy so the curated-map coverage test can compose it).
_DUAL_MODE_TOOLS_TEST = {"vesma_tags", "vesma_workflow", "vesma_awareness", "vesma_hooks"}


class TestF9F11:
    def test_env_notify_url_scheme_guarded(self) -> None:
        with pytest.raises(ValueError, match=r"http\(s\)"):
            FrontResilienceSettings.from_env(
                {
                    "VESMA_FRONT_DEGRADED_MAX_DURATION": "1h",
                    "VESMA_FRONT_DEGRADED_NOTIFY_URL": "file:///tmp/hook",
                }
            )

    def test_env_notify_url_http_accepted(self) -> None:
        settings, refusal = FrontResilienceSettings.from_env(
            {
                "VESMA_FRONT_DEGRADED_MAX_DURATION": "1h",
                "VESMA_FRONT_DEGRADED_NOTIFY_URL": "https://hooks.example/vesma",
            }
        )
        assert refusal is None
        assert settings.notify_url == "https://hooks.example/vesma"

    async def test_core_degraded_header_is_dropped_not_forwarded(self, tmp_path: Path) -> None:
        """F11: the core's own X-Vesma-Degraded header never rides through
        — the front's signage is the plane's single wire truth."""
        core = _FakeCore()
        core.app.router.routes.append(
            Route(
                "/{path:path}",
                lambda request: JSONResponse(
                    {},
                    headers={"X-Vesma-Degraded": "since=core-side, ttl_left=1"},
                ),
                methods=["GET", "POST"],
            )
        )
        h = _Harness(tmp_path, _Clock())
        _degraded(h)  # the front adds its own header in the degraded branch
        front = _front_with(h.coordinator, core)
        response = await _call(front, content=b"x")
        values = response.headers.get_list("x-vesma-degraded")
        assert len(values) == 1, "exactly ONE degraded header — the front's own"
        assert "core-side" not in values[0], "the core's header never rides through"


# ── Final-slice pins (F-N1 / F-N2) ────────────────────────────────────


class TestFN1TransientClassification:
    """F-N1: infra-class outcomes must NEVER pocket a row — they are
    transient failures (failed + fail-stop), the row stays in the spool."""

    def _production_front(self, monkeypatch: pytest.MonkeyPatch, handler: Any) -> McpFront:
        """The production replay leg over a MockTransport (no sockets)."""
        real_client = httpx.AsyncClient

        def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs.pop("transport", None)
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        front = McpFront(target="http://127.0.0.1:8787/mcp/")
        front._resilience = ResilienceCoordinator(
            _settings(service_token="svc"),
            state_home=Path("/tmp/never-used-fn1"),
        )
        return front

    async def test_http_503_raises_transient(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        front = self._production_front(monkeypatch, handler)
        with pytest.raises(ReplayTransientError):
            await front._production_core_call("vesma_add", {})

    async def test_http_502_504_raise_transient(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for status in (502, 504):
            front = self._production_front(monkeypatch, lambda request, s=status: httpx.Response(s))
            with pytest.raises(ReplayTransientError):
                await front._production_core_call("vesma_add", {})

    async def test_generic_error_rendering_raises_transient(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The generic `❌ Error:` catch-all carries INFRA trouble (store
        locked, disk full) — transient, never pocketed."""
        answer = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": "❌ Error: database is locked"}],
                "isError": True,
            },
        }
        front = self._production_front(
            monkeypatch, lambda request: httpx.Response(200, json=answer)
        )
        with pytest.raises(ReplayTransientError):
            await front._production_core_call("vesma_add", {})

    async def test_semantic_refusal_still_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The TYPED renderings (namespace violation class) stay the
        pocket path: accepted=False, no exception."""
        answer = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {"type": "text", "text": "❌ Namespace violation (ADR-0050): agent:foreign"}
                ],
                "isError": True,
            },
        }
        front = self._production_front(
            monkeypatch, lambda request: httpx.Response(200, json=answer)
        )
        assert await front._production_core_call("vesma_add", {}) is False

    async def test_503_answer_keeps_the_row_in_the_spool(self, tmp_path: Path) -> None:
        """THE F-N1 pin: a 503 from the core → the row STAYS in the spool
        (failed/fail-stop), never pocketed, never deleted."""
        spool = _make_spool(tmp_path, rows=1)

        async def core_call(op: str, payload: dict[str, Any]) -> bool:
            raise ReplayTransientError("core answered HTTP 503")

        report = await run_replay([spool], core_call=core_call)
        assert report.failed == 1
        assert report.rejected_permanent == 0
        assert spool.dir.exists(), "the transient row STAYS in the spool"

    async def test_generic_locked_answer_keeps_the_row(self, tmp_path: Path) -> None:
        """THE F-N1 pin: `❌ Error: database is locked` → the row STAYS."""
        spool = _make_spool(tmp_path, rows=1)

        async def core_call(op: str, payload: dict[str, Any]) -> bool:
            # The production leg's verdict for the generic rendering.
            answer = {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{"type": "text", "text": "❌ Error: database is locked"}],
                    "isError": True,
                },
            }
            if core_answer_transient(answer):
                raise ReplayTransientError("generic error rendering — infra-class")
            return core_answer_accepted(answer)

        report = await run_replay([spool], core_call=core_call)
        assert report.failed == 1
        assert report.rejected_permanent == 0
        assert spool.dir.exists(), "the infra-class row STAYS in the spool"

    def test_core_answer_transient_predicate(self) -> None:
        generic = {"result": {"content": [{"type": "text", "text": "❌ Error: store locked"}]}}
        semantic = {
            "result": {"content": [{"type": "text", "text": "❌ Tag contract violation: …"}]}
        }
        assert core_answer_transient(generic) is True
        assert core_answer_transient(semantic) is False
        assert core_answer_transient({"result": {}}) is False
        assert core_answer_transient("not a dict") is False


class TestFN2PurgeSafety:
    def test_leftover_plus_fresh_row_replays_never_purged(self, tmp_path: Path) -> None:
        """THE F-N2 pin: a rename-crash leftover (header + .replayed)
        flapped the machine to DEGRADED and a FRESH receipted write
        landed in a NEW ops.jsonl of the SAME directory — the replay pass
        must REPLAY that row, not purge the directory."""
        h = _Harness(tmp_path, _Clock())
        _degraded(h)
        spool = h.coordinator.open_spool(h.coordinator.actor, h.coordinator.state())
        spool.append(op="vesma_add", payload={"content": "old-applied"})
        # Rename-crash: ops.jsonl → .replayed, tree removal never happened.
        os.replace(
            spool.ops_path,
            spool.ops_path.with_name(Spool.OPS_NAME + Spool.DONE_SUFFIX),
        )
        # The flap lands a FRESH receipted write into the SAME directory.
        revived = ResilienceCoordinator(
            h.settings,
            state_home=tmp_path / "state",
            clock=h.clock,
            probe=h._probe,
            core_call=h._core_call,
        )
        assert revived.current_mode() == ResilienceMode.DEGRADED
        state = revived.state()
        fresh_spool = revived.open_spool(revived.actor, state)
        fresh_row = fresh_spool.append(op="vesma_add", payload={"content": "fresh-receipted"})
        assert fresh_spool.ops_path.exists(), "the fresh write landed a NEW ops.jsonl"

        # The REPLAY pass: probe ok → purge MUST NOT destroy the fresh row.
        h.probe_ok = True
        outcome = _run(revived.poll_once())
        assert outcome.replay is not None
        assert outcome.replay.replayed == 1, "the receipted fresh row REPLAYED"
        assert outcome.replay.rejected_permanent == 0
        assert outcome.replay.failed == 0
        assert outcome.mode_after == ResilienceMode.HEALTHY
        assert not fresh_spool.dir.exists(), "consumed afterwards — normal completion"
        assert [payload for _op, payload in h.core_calls] == [
            {"content": "fresh-receipted", "vesma_degraded_op_id": fresh_row.op_id}
        ], (
            "exactly the fresh row was delivered — the stale .replayed copy "
            "was set aside, not re-applied"
        )

    def test_leftover_without_live_rows_still_purged(self, tmp_path: Path) -> None:
        """The F4 anti-flap leg survives: a leftover with NO ops.jsonl is
        still purged."""
        h = _Harness(tmp_path, _Clock())
        _degraded(h)
        spool = h.coordinator.open_spool(h.coordinator.actor, h.coordinator.state())
        spool.append(op="vesma_add", payload={"content": "x"})
        os.replace(
            spool.ops_path,
            spool.ops_path.with_name(Spool.OPS_NAME + Spool.DONE_SUFFIX),
        )
        revived = ResilienceCoordinator(
            h.settings,
            state_home=tmp_path / "state",
            clock=h.clock,
            probe=h._probe,
            core_call=h._core_call,
        )
        outcome = _run(revived.poll_once())
        assert outcome.mode_after == ResilienceMode.HEALTHY
        assert outcome.replay is not None
        assert outcome.replay.spools_purged >= 1
        assert not spool.dir.exists()
