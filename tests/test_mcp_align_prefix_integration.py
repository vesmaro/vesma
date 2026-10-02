"""Integration tests for the ``vesma_align_prefix`` MCP tool (P1-5).

These exercise the full ``_dispatch("vesma_align_prefix", ...)`` path
against a REAL ``MemoryManager`` + REAL ``align()`` — not a mocked
``align_prefix`` return value. They cover the QA gaps left by the
unit-level routing test in ``test_mcp_server.py``:

- HIGH #2 — the returned dict carries the four contract keys
  (``aligned_text``, ``extracted``, ``prefix_stabilized``, ``moved_chars``)
  and the timestamp is actually relocated to the Dynamic context block.
- HIGH #3 — the ``profile`` arg is forwarded through the dispatcher to
  ``align()`` and the ``"code"`` profile skips token extraction.
- LOW  #13 — ``call_tool`` wraps the dict as a ``TextContent`` with valid
  JSON containing the four keys.
- LOW  #14 — a missing required ``text`` arg returns an error string
  (graceful, not a crash).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.mcp_server import _dispatch, call_tool

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def real_manager(tmp_path: Path) -> MemoryManager:
    """A real MemoryManager with cache_aligner enabled, isolated under tmp."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        settings = Settings(
            vesma={
                "vault_path": str(tmp / "vault"),
                "data_dir": str(tmp / "data"),
                "db_name": "test.db",
            },
        )
        settings.resolve_paths()
        # Ensure CacheAligner is enabled (default) for these integration tests.
        settings.cache_aligner.enabled = True
        mgr = MemoryManager(settings)
        yield mgr
        mgr.close()


# ── HIGH fix #2 — end-to-end shape against real align() ───────────────────────


class TestAlignPrefixEndToEnd:
    async def test_mcp_align_prefix_dispatches_to_real_align(
        self, real_manager: MemoryManager
    ) -> None:
        """_dispatch against a real MemoryManager must return the four
        contract keys and actually relocate a timestamp to the Dynamic
        context block."""
        text = "System prompt. Logged at 2026-07-17T10:30:00Z end."
        with patch("vesma.mcp_server.get_manager", return_value=real_manager):
            result = await _dispatch("vesma_align_prefix", {"text": text})

        assert isinstance(result, dict), f"expected dict, got {type(result)}"
        # The four contract keys MUST be present.
        assert "aligned_text" in result
        assert "extracted" in result
        assert "prefix_stabilized" in result
        assert "moved_chars" in result

        # The timestamp was actually extracted and relocated.
        assert result["prefix_stabilized"] is True
        assert result["moved_chars"] > 0
        assert any(
            s["kind"] == "timestamp" and s["value"] == "2026-07-17T10:30:00Z"
            for s in result["extracted"]
        ), result["extracted"]
        # The timestamp is NOT in the aligned body (before the dynamic block).
        body = result["aligned_text"].split("--- Dynamic context ---")[0]
        assert "2026-07-17T10:30:00Z" not in body
        # It IS in the dynamic block at the tail.
        assert "2026-07-17T10:30:00Z" in result["aligned_text"]


# ── HIGH fix #3 — profile param forwarding ────────────────────────────────────


class TestProfileForwarding:
    async def test_mcp_align_prefix_forwards_profile_code(
        self, real_manager: MemoryManager
    ) -> None:
        """profile='code' must reach align() and skip token extraction.
        A long bare token that would be extracted by default must NOT be
        extracted under the 'code' profile."""
        long_token = "aBcDeFgHiJkLmNoPqRsTuVwXy"  # 25 chars, passes token regex
        text = f"Built at 2026-07-17T10:00:00Z commit {long_token};"
        with patch("vesma.mcp_server.get_manager", return_value=real_manager):
            result = await _dispatch(
                "vesma_align_prefix",
                {"text": text, "profile": "code"},
            )
        assert isinstance(result, dict)
        kinds = {s["kind"] for s in result["extracted"]}
        assert "token" not in kinds, f"profile='code' must skip token kind; got kinds={kinds}"
        # Timestamps are still extracted under 'code' (only tokens skipped).
        assert "timestamp" in kinds
        # The token stays in the aligned body (not relocated).
        body = result["aligned_text"].split("--- Dynamic context ---")[0]
        assert long_token in body

    async def test_mcp_align_prefix_forwards_profile_docs(
        self, real_manager: MemoryManager
    ) -> None:
        """Sanity: profile='docs' also skips tokens (same skip set as 'code')."""
        long_token = "aBcDeFgHiJkLmNoPqRsTuVwXy"
        text = f"Updated 2026-07-17T10:00:00Z ref {long_token}."
        with patch("vesma.mcp_server.get_manager", return_value=real_manager):
            result = await _dispatch(
                "vesma_align_prefix",
                {"text": text, "profile": "docs"},
            )
        assert isinstance(result, dict)
        kinds = {s["kind"] for s in result["extracted"]}
        assert "token" not in kinds
        assert "timestamp" in kinds

    async def test_mcp_align_prefix_default_profile_extracts_token(
        self, real_manager: MemoryManager
    ) -> None:
        """Without a profile, tokens ARE extracted (contrast with the
        'code'/'docs' profile tests above)."""
        long_token = "aBcDeFgHiJkLmNoPqRsTuVwXy"
        text = f"Token: {long_token} at 2026-07-17T10:00:00Z."
        with patch("vesma.mcp_server.get_manager", return_value=real_manager):
            result = await _dispatch("vesma_align_prefix", {"text": text})
        assert isinstance(result, dict)
        kinds = {s["kind"] for s in result["extracted"]}
        assert "token" in kinds


# ── LOW fix #13 — call_tool TextContent wrapping ──────────────────────────────


class TestCallToolTextContentWrapping:
    async def test_call_tool_align_prefix_wraps_dict_as_json(
        self, real_manager: MemoryManager
    ) -> None:
        """call_tool('vesma_align_prefix', ...) must return a list with
        one TextContent whose .text is valid JSON containing the four
        contract keys."""
        text = "System prompt. At 2026-07-17T10:30:00Z done."
        with patch("vesma.mcp_server.get_manager", return_value=real_manager):
            contents = await call_tool("vesma_align_prefix", {"text": text})

        assert len(contents) == 1
        payload = json.loads(contents[0].text)
        assert isinstance(payload, dict)
        assert "aligned_text" in payload
        assert "extracted" in payload
        assert "prefix_stabilized" in payload
        assert "moved_chars" in payload
        # The real align() relocated the timestamp.
        assert payload["moved_chars"] > 0
        assert any(s["kind"] == "timestamp" for s in payload["extracted"])


# ── LOW fix #14 — missing required 'text' arg ─────────────────────────────────


class TestMissingTextArg:
    async def test_align_prefix_missing_text_arg_returns_error(
        self, real_manager: MemoryManager
    ) -> None:
        """Calling _dispatch('vesma_align_prefix', {}) without the required
        'text' arg must return an error string (graceful), not raise."""
        with patch("vesma.mcp_server.get_manager", return_value=real_manager):
            # _dispatch does args["text"] → KeyError. The call_tool wrapper
            # catches Exception and returns a TextContent with the error.
            # We test via call_tool so the full graceful path is exercised.
            contents = await call_tool("vesma_align_prefix", {})
        assert len(contents) == 1
        # The error is surfaced to the caller (not swallowed).
        assert "Error" in contents[0].text or "text" in contents[0].text

    async def test_dispatch_missing_text_arg_raises_keyerror_caught_by_call_tool(
        self, real_manager: MemoryManager
    ) -> None:
        """At the _dispatch layer, the missing key raises KeyError — this
        is expected; call_tool is the public surface that catches it. This
        test documents the layering so a future refactor that moves
        validation into _dispatch is caught."""
        with (
            patch("vesma.mcp_server.get_manager", return_value=real_manager),
            pytest.raises(KeyError),
        ):
            await _dispatch("vesma_align_prefix", {})
