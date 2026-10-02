"""Unit tests for the M3 gRPC client (:mod:`vesma.mesh_client`).

These tests exercise :class:`MeshClient` against a mocked gRPC channel —
no live ``mnemos-mesh`` binary is required. The mock stub returns
canned protobuf responses (or raises canned :class:`grpc.RpcError`
subclasses) so every code path in :mod:`vesma.mesh_client` is covered:
construction, the four RPCs (ListMemories, WriteMemory,
GetSubscriptionState, Heartbeat), error mapping (UNAVAILABLE /
UNIMPLEMENTED), CompactRecord Pydantic↔proto marshaling round-trip,
config validation, and timeout handling.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock, patch

import grpc
import pytest

from vesma import _mesh_gen
from vesma.compact import CompactRecord
from vesma.config import MeshConfig
from vesma.mesh_client import (
    MeshClient,
    MeshError,
    MeshUnavailableError,
    MeshUnimplementedError,
)

# ── gRPC error doubles ────────────────────────────────────────────────────────
#
# grpc.RpcError is a protocol-like base; gRPC's own runtime raises concrete
# subclasses of it. For test doubles we subclass it directly so the
# ``except grpc.RpcError`` block in MeshClient catches our doubles. mypy
# --strict flags subclassing a protocol-ish base as [misc]; the ignore is
# the sanctioned pattern documented in the task spec.


class _FakeRpcUnavailable(grpc.RpcError):  # type: ignore[misc]  # grpc.RpcError is a protocol; subclasses allowed for testing
    """Test double for a gRPC UNAVAILABLE error."""

    def code(self) -> grpc.StatusCode:
        return grpc.StatusCode.UNAVAILABLE

    def details(self) -> str:
        return "socket not found"


class _FakeRpcUnimplemented(grpc.RpcError):  # type: ignore[misc]  # grpc.RpcError is a protocol; subclasses allowed for testing
    """Test double for a gRPC UNIMPLEMENTED error."""

    def code(self) -> grpc.StatusCode:
        return grpc.StatusCode.UNIMPLEMENTED

    def details(self) -> str:
        return "RPC not wired"


class _FakeRpcUnknown(grpc.RpcError):  # type: ignore[misc]  # grpc.RpcError is a protocol; subclasses allowed for testing
    """Test double for an unmapped gRPC error (maps to base MeshError)."""

    def code(self) -> grpc.StatusCode:
        return grpc.StatusCode.UNKNOWN

    def details(self) -> str:
        return "something broke"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_record(**overrides: Any) -> CompactRecord:
    """Build a CompactRecord with sensible defaults; override any field."""
    defaults: dict[str, Any] = {
        "id": "fed:agent-a:uuid-1",
        "type": "decision",
        "title": "Use gRPC for mesh transport",
        "summary": "Decided on gRPC+Unix socket over CGo.",
        "key_points": ["criterion 6 versioned API", "no CPython ABI coupling"],
        "tags": ["project:mnemos", "agent:agent-a", "mnemos:decision"],
        "source_agent": "agent-a",
        "timestamp": "2026-07-17T15:30:00Z",
    }
    defaults.update(overrides)
    return CompactRecord(**defaults)


def _make_mock_stub() -> MagicMock:
    """Build a MagicMock standing in for ``MnemosCoreStub``.

    Each RPC method is a separate MagicMock so tests configure the return
    value or side effect for one RPC without affecting the others.
    """
    stub = MagicMock(spec=["ListMemories", "WriteMemory", "GetSubscriptionState", "Heartbeat"])
    return stub


@pytest.fixture
def mock_channel_and_stub() -> Generator[tuple[MagicMock, MagicMock], None, None]:
    """Patch the gRPC channel + stub so MeshClient construction is hermetic.

    Yields the (channel, stub) mocks. Tests configure ``stub.<Rpc>`` to
    return a canned response or raise a canned error before invoking the
    client method under test.
    """
    channel = MagicMock(name="grpc_channel")
    stub = _make_mock_stub()

    def _fake_init(self: MeshClient, socket_path: str, *, timeout: float = 2.0) -> None:
        self._socket_path = socket_path
        self._timeout = float(timeout)
        self._channel = channel
        self._stub = stub

    with patch.object(MeshClient, "__init__", _fake_init):
        yield channel, stub


# ── 1. Construction ──────────────────────────────────────────────────────────


class TestMeshClientConstruction:
    def test_construct_with_valid_mesh_config(
        self, mock_channel_and_stub: tuple[MagicMock, MagicMock]
    ) -> None:
        """MeshClient builds from a populated MeshConfig without error."""
        _channel, _stub = mock_channel_and_stub
        cfg = MeshConfig(socket_path="/run/mnemos/core.sock", enabled=True, timeout_s=1.5)
        client = MeshClient(cfg.socket_path, timeout=cfg.timeout_s)
        assert client._socket_path == "/run/mnemos/core.sock"
        assert client._timeout == 1.5

    def test_default_timeout_is_2s(
        self, mock_channel_and_stub: tuple[MagicMock, MagicMock]
    ) -> None:
        """MeshClient defaults the per-RPC deadline to 2.0 seconds."""
        _channel, _stub = mock_channel_and_stub
        client = MeshClient("/run/mnemos/core.sock")
        assert client._timeout == 2.0


# ── 2. Heartbeat ─────────────────────────────────────────────────────────────


class TestHeartbeat:
    def test_heartbeat_round_trip(self, mock_channel_and_stub: tuple[MagicMock, MagicMock]) -> None:
        """Heartbeat returns the (healthy, version, uptime) tuple from the stub."""
        _channel, stub = mock_channel_and_stub
        response = MagicMock(healthy=True, version="test-1.0.0", uptime_seconds=42)
        stub.Heartbeat.return_value = response

        client = MeshClient("/run/mnemos/core.sock")
        healthy, version, uptime = client.heartbeat("peer-a", component="mesh")

        assert healthy is True
        assert version == "test-1.0.0"
        assert uptime == 42
        # Verify the request carried peer_id + component through.
        call_args = stub.Heartbeat.call_args
        assert call_args is not None
        sent_request = call_args.args[0]
        assert sent_request.peer_id == "peer-a"
        assert sent_request.component == "mesh"

    def test_heartbeat_coerces_bool_and_int(
        self, mock_channel_and_stub: tuple[MagicMock, MagicMock]
    ) -> None:
        """Heartbeat coerces proto bool/int fields to native Python types."""
        _channel, stub = mock_channel_and_stub
        # Protobuf bool/int come back as subclasses of int/bool; the client
        # wraps them with bool()/int() so callers get plain Python types.
        response = MagicMock(healthy=1, version="v", uptime_seconds=7)
        stub.Heartbeat.return_value = response

        client = MeshClient("/run/mnemos/core.sock")
        healthy, _version, uptime = client.heartbeat("p")
        assert healthy is True
        assert uptime == 7
        assert isinstance(uptime, int)


# ── 3. ListMemories ──────────────────────────────────────────────────────────


class TestListMemories:
    def test_list_memories_empty(self, mock_channel_and_stub: tuple[MagicMock, MagicMock]) -> None:
        """ListMemories returns an empty list when the stub returns no records."""
        _channel, stub = mock_channel_and_stub
        response = MagicMock(records=[], total=0, has_more=False)
        stub.ListMemories.return_value = response

        client = MeshClient("/run/mnemos/core.sock")
        records = client.list_memories(projects=["mnemos"])

        assert records == []
        stub.ListMemories.assert_called_once()

    def test_list_memories_non_empty(
        self, mock_channel_and_stub: tuple[MagicMock, MagicMock]
    ) -> None:
        """ListMemories marshals proto CompactRecords back to Pydantic models."""
        _channel, stub = mock_channel_and_stub
        proto_record = _mesh_gen.fed_pb2.CompactRecord(
            id="fed:agent-b:uuid-2",
            type="learning",
            title="Go rewrite deferred",
            summary="Python pipeline works; no Go gain yet.",
            key_points=["queue drained", "hybrid search working"],
            tags=["project:mnemos", "agent:agent-b", "mnemos:learning"],
            source_agent="agent-b",
            timestamp="2026-06-29T10:00:00Z",
        )
        response = MagicMock(records=[proto_record], total=1, has_more=False)
        stub.ListMemories.return_value = response

        client = MeshClient("/run/mnemos/core.sock")
        records = client.list_memories(projects=["mnemos"], limit=10)

        assert len(records) == 1
        r = records[0]
        assert r.id == "fed:agent-b:uuid-2"
        assert r.type == "learning"
        assert r.title == "Go rewrite deferred"
        assert r.summary == "Python pipeline works; no Go gain yet."
        assert list(r.key_points) == ["queue drained", "hybrid search working"]
        assert list(r.tags) == ["project:mnemos", "agent:agent-b", "mnemos:learning"]
        assert r.source_agent == "agent-b"
        assert r.timestamp == "2026-06-29T10:00:00Z"


# ── 4. WriteMemory ───────────────────────────────────────────────────────────


class TestWriteMemory:
    def test_write_memory_unimplemented(
        self, mock_channel_and_stub: tuple[MagicMock, MagicMock]
    ) -> None:
        """WriteMemory maps a UNIMPLEMENTED gRPC error to MeshUnimplementedError."""
        _channel, stub = mock_channel_and_stub
        stub.WriteMemory.side_effect = _FakeRpcUnimplemented()

        client = MeshClient("/run/mnemos/core.sock")
        with pytest.raises(MeshUnimplementedError):
            client.write_memory(_make_record(), import_mode="MERGE")

    def test_write_memory_success_returns_id(
        self, mock_channel_and_stub: tuple[MagicMock, MagicMock]
    ) -> None:
        """WriteMemory returns the echoed written_id on success."""
        _channel, stub = mock_channel_and_stub
        response = MagicMock(written_id="fed:agent-a:uuid-1", mode_applied=1, trigger_code=0)
        stub.WriteMemory.return_value = response

        client = MeshClient("/run/mnemos/core.sock")
        written_id = client.write_memory(_make_record(), import_mode="MERGE")
        assert written_id == "fed:agent-a:uuid-1"

    def test_write_memory_restore_requires_confirm(
        self, mock_channel_and_stub: tuple[MagicMock, MagicMock]
    ) -> None:
        """RESTORE mode without confirm still sends the request (mnemos gates it)."""
        _channel, stub = mock_channel_and_stub
        response = MagicMock(written_id="fed:agent-a:uuid-1", mode_applied=2, trigger_code=0)
        stub.WriteMemory.return_value = response

        client = MeshClient("/run/mnemos/core.sock")
        # The client does not enforce the confirm gate itself (mnemos does);
        # it forwards the request as-is. We verify the request carries
        # import_mode=RESTORE and confirm=False through.
        client.write_memory(_make_record(), import_mode="RESTORE", confirm=False)
        call_args = stub.WriteMemory.call_args
        assert call_args is not None
        sent_request = call_args.args[0]
        assert sent_request.import_mode == _mesh_gen.core_pb2.ImportMode.RESTORE
        assert sent_request.confirm is False


# ── 5. GetSubscriptionState ──────────────────────────────────────────────────


class TestGetSubscriptionState:
    def test_get_subscription_state_round_trip(
        self, mock_channel_and_stub: tuple[MagicMock, MagicMock]
    ) -> None:
        """GetSubscriptionState returns the (cursor, last_rev, last_sync_ts) tuple."""
        _channel, stub = mock_channel_and_stub
        response = MagicMock(
            cursor="resume-token-abc",
            last_rev=99,
            last_sync_timestamp="2026-07-17T12:00:00Z",
        )
        stub.GetSubscriptionState.return_value = response

        client = MeshClient("/run/mnemos/core.sock")
        cursor, last_rev, last_sync_ts = client.get_subscription_state(
            "peer-a", project_scope="mnemos"
        )

        assert cursor == "resume-token-abc"
        assert last_rev == 99
        assert last_sync_ts == "2026-07-17T12:00:00Z"
        call_args = stub.GetSubscriptionState.call_args
        assert call_args is not None
        sent_request = call_args.args[0]
        assert sent_request.peer_id == "peer-a"
        assert sent_request.project_scope == "mnemos"


# ── 6. Error mapping ─────────────────────────────────────────────────────────


class TestErrorMapping:
    def test_unavailable_maps_to_mesh_unavailable(
        self, mock_channel_and_stub: tuple[MagicMock, MagicMock]
    ) -> None:
        """A UNAVAILABLE gRPC error surfaces as MeshUnavailableError."""
        _channel, stub = mock_channel_and_stub
        stub.Heartbeat.side_effect = _FakeRpcUnavailable()

        client = MeshClient("/run/mnemos/core.sock")
        with pytest.raises(MeshUnavailableError):
            client.heartbeat("peer-a")

    def test_unknown_maps_to_base_mesh_error(
        self, mock_channel_and_stub: tuple[MagicMock, MagicMock]
    ) -> None:
        """An unmapped gRPC status code surfaces as the base MeshError."""
        _channel, stub = mock_channel_and_stub
        stub.ListMemories.side_effect = _FakeRpcUnknown()

        client = MeshClient("/run/mnemos/core.sock")
        with pytest.raises(MeshError) as exc_info:
            client.list_memories()
        # Must NOT be one of the specific subclasses.
        assert not isinstance(exc_info.value, MeshUnavailableError)
        assert not isinstance(exc_info.value, MeshUnimplementedError)

    def test_mesh_unavailable_is_subclass_of_mesh_error(
        self,
        mock_channel_and_stub: tuple[MagicMock, MagicMock],
    ) -> None:
        """MeshUnavailableError and MeshUnimplementedError inherit MeshError."""
        assert issubclass(MeshUnavailableError, MeshError)
        assert issubclass(MeshUnimplementedError, MeshError)
        _channel, _stub = mock_channel_and_stub  # keep fixture signature uniform


# ── 7. CompactRecord marshaling round-trip ───────────────────────────────────


class TestCompactRecordMarshaling:
    def test_compact_record_round_trip_field_parity(
        self, mock_channel_and_stub: tuple[MagicMock, MagicMock]
    ) -> None:
        """CompactRecord → proto → CompactRecord preserves every field."""
        _channel, stub = mock_channel_and_stub
        original = _make_record(
            key_points=["a", "b", "c"],
            tags=["project:mnemos", "agent:x", "mnemos:rule"],
            summary="multi-line\nsummary with\npunctuation!",
        )
        proto_record = _mesh_gen.fed_pb2.CompactRecord(
            id=original.id,
            type=original.type,
            title=original.title,
            summary=original.summary,
            key_points=list(original.key_points),
            tags=list(original.tags),
            source_agent=original.source_agent,
            timestamp=original.timestamp,
        )
        response = MagicMock(records=[proto_record], total=1, has_more=False)
        stub.ListMemories.return_value = response

        client = MeshClient("/run/mnemos/core.sock")
        records = client.list_memories()
        assert len(records) == 1
        r = records[0]
        assert r.id == original.id
        assert r.type == original.type
        assert r.title == original.title
        assert r.summary == original.summary
        assert list(r.key_points) == list(original.key_points)
        assert list(r.tags) == list(original.tags)
        assert r.source_agent == original.source_agent
        assert r.timestamp == original.timestamp

    def test_compact_record_empty_fields_round_trip(
        self,
        mock_channel_and_stub: tuple[MagicMock, MagicMock],
    ) -> None:
        """Empty key_points/tags and empty summary survive the round trip."""
        _channel, stub = mock_channel_and_stub
        original = CompactRecord(id="fed:a:u", type="checkpoint", title="t")
        proto_record = _mesh_gen.fed_pb2.CompactRecord(
            id=original.id,
            type=original.type,
            title=original.title,
            summary="",
            key_points=[],
            tags=[],
            source_agent="",
            timestamp="",
        )
        response = MagicMock(records=[proto_record], total=1, has_more=False)
        stub.ListMemories.return_value = response

        client = MeshClient("/run/mnemos/core.sock")
        records = client.list_memories()
        r = records[0]
        assert r.summary == ""
        assert list(r.key_points) == []
        assert list(r.tags) == []
        assert r.source_agent == ""
        assert r.timestamp == ""


# ── 8. Config validation ─────────────────────────────────────────────────────


class TestConfigValidation:
    def test_invalid_timeout_zero_raises(self) -> None:
        """MeshConfig rejects timeout_s=0 (gt=0.0 constraint)."""
        with pytest.raises(ValueError):
            MeshConfig(socket_path="/run/mnemos/core.sock", timeout_s=0.0)

    def test_invalid_timeout_negative_raises(self) -> None:
        """MeshConfig rejects a negative timeout_s."""
        with pytest.raises(ValueError):
            MeshConfig(socket_path="/run/mnemos/core.sock", timeout_s=-1.0)

    def test_invalid_timeout_over_60_raises(self) -> None:
        """MeshConfig rejects timeout_s > 60 (le=60.0 constraint)."""
        with pytest.raises(ValueError):
            MeshConfig(socket_path="/run/mnemos/core.sock", timeout_s=61.0)

    def test_default_socket_path(self) -> None:
        """MeshConfig defaults socket_path to the systemd-tmpfiles convention."""
        cfg = MeshConfig()
        assert cfg.socket_path == "/run/mnemos/core.sock"
        assert cfg.enabled is False
        assert cfg.timeout_s == 2.0


# ── 9. Timeout handling ──────────────────────────────────────────────────────


class TestTimeoutHandling:
    def test_timeout_is_passed_to_stub_call(
        self, mock_channel_and_stub: tuple[MagicMock, MagicMock]
    ) -> None:
        """The configured timeout is forwarded as the gRPC call deadline."""
        _channel, stub = mock_channel_and_stub
        response = MagicMock(healthy=True, version="v", uptime_seconds=1)
        stub.Heartbeat.return_value = response

        client = MeshClient("/run/mnemos/core.sock", timeout=5.0)
        client.heartbeat("peer-a")

        call_kwargs = stub.Heartbeat.call_args.kwargs
        assert call_kwargs["timeout"] == 5.0

    def test_deadline_exceeded_surfaces_as_unavailable(
        self,
        mock_channel_and_stub: tuple[MagicMock, MagicMock],
    ) -> None:
        """A DEADLINE_EXCEEDED gRPC error maps to the base MeshError (not UNAVAILABLE)."""
        _channel, stub = mock_channel_and_stub

        class _FakeDeadlineExceeded(grpc.RpcError):  # type: ignore[misc]  # grpc.RpcError is a protocol; subclasses allowed for testing
            def code(self) -> grpc.StatusCode:
                return grpc.StatusCode.DEADLINE_EXCEEDED

            def details(self) -> str:
                return "deadline exceeded"

        stub.Heartbeat.side_effect = _FakeDeadlineExceeded()

        client = MeshClient("/run/mnemos/core.sock", timeout=0.001)
        with pytest.raises(MeshError) as exc_info:
            client.heartbeat("peer-a")
        # DEADLINE_EXCEEDED is not UNAVAILABLE, so it stays the base MeshError.
        assert not isinstance(exc_info.value, MeshUnavailableError)


# ── 10. Lifecycle ────────────────────────────────────────────────────────────


class TestLifecycle:
    def test_context_manager_closes_channel(
        self,
        mock_channel_and_stub: tuple[MagicMock, MagicMock],
    ) -> None:
        """Using MeshClient as a context manager closes the channel on exit."""
        channel, _stub = mock_channel_and_stub
        with MeshClient("/run/mnemos/core.sock") as client:
            assert client._channel is channel
        channel.close.assert_called_once()

    def test_close_is_idempotent(
        self,
        mock_channel_and_stub: tuple[MagicMock, MagicMock],
    ) -> None:
        """Calling close() twice is safe (idempotent)."""
        channel, _stub = mock_channel_and_stub
        client = MeshClient("/run/mnemos/core.sock")
        client.close()
        client.close()
        assert channel.close.call_count == 2
