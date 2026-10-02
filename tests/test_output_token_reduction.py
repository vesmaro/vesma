"""Tests for P1-7 — Output token reduction (verbosity steering + effort routing).

Inspired by headroom's output token reduction work. Original implementation.
These tests verify that the verbosity/effort parameters inject guidance into
tool results, that defaults preserve the exact pre-P1-7 behaviour, and that
calls without the new params work identically (backward compatibility).
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from vesmaro.manager import IssuanceScan
from vesmaro.mcp_server import _EFFORT_GUIDANCE, _VERBOSITY_GUIDANCE, _dispatch

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_mock_manager() -> MagicMock:
    """Mock manager with stub return values for the steered tools."""
    mock_memory = MagicMock()
    mock_memory.id = "smoke-id-1"
    mock_memory.auto_title.return_value = "Smoke Memory"
    mock_memory.status = "published"
    mock_memory.filter_profile = None

    mgr = MagicMock()
    mgr.settings.vesma.strict_tag_contract = False
    mgr.settings.vesma.auto_filter = False
    mgr.settings.output_style.enabled = True
    mgr.settings.output_style.default_verbosity = "default"
    mgr.settings.output_style.default_effort = "medium"
    mgr.add.return_value = mock_memory
    mgr.search.return_value = []
    mgr.recall_context.return_value = []
    # ADR-0018 P1-b: content-echoing handlers now route the issued string
    # through MemoryManager.scan_issuance — stub it as a clean pass-through
    # so mocked managers keep issuing their fixture content unchanged.
    mgr.scan_issuance.side_effect = lambda text, *, context: IssuanceScan(
        text=text, refused=False, reason=None, redactions=0, redacted_patterns={}
    )
    return mgr


# ── Verbosity steering ───────────────────────────────────────────────────────


class TestVerbositySteering:
    async def test_terse_injects_guidance_on_add(self) -> None:
        mock_mgr = _make_mock_manager()
        with (
            patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr),
            patch(
                "vesmaro.mcp_server.validate_tag_contract",
                side_effect=lambda tags, **_kw: tags,
            ),
        ):
            result = await _dispatch(
                "vesma_add",
                {
                    "content": "smoke content",
                    "tags": ["project:smoke", "agent:qa", "mnemos:decision"],
                    "verbosity": "terse",
                },
            )
        assert isinstance(result, dict)
        assert "_output_style_hint" in result
        assert (
            "terse" in result["_output_style_hint"].lower()
            or "brief" in result["_output_style_hint"].lower()
        )

    async def test_minimal_injects_guidance_on_add(self) -> None:
        mock_mgr = _make_mock_manager()
        with (
            patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr),
            patch(
                "vesmaro.mcp_server.validate_tag_contract",
                side_effect=lambda tags, **_kw: tags,
            ),
        ):
            result = await _dispatch(
                "vesma_add",
                {
                    "content": "smoke content",
                    "tags": ["project:smoke", "agent:qa", "mnemos:decision"],
                    "verbosity": "minimal",
                },
            )
        assert isinstance(result, dict)
        assert "_output_style_hint" in result
        assert (
            "minimal" in result["_output_style_hint"].lower()
            or "facts" in result["_output_style_hint"].lower()
        )

    async def test_terse_injects_guidance_on_search(self) -> None:
        mock_mgr = _make_mock_manager()
        with patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr):
            result = await _dispatch(
                "vesma_search",
                {"query": "smoke", "verbosity": "terse"},
            )
        # When steering is active, search returns a dict with results + hint.
        assert isinstance(result, dict)
        assert "_output_style_hint" in result
        assert "results" in result

    async def test_terse_injects_guidance_on_recall_context(self) -> None:
        mock_mgr = _make_mock_manager()
        with patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr):
            result = await _dispatch(
                "vesma_recall_context",
                {"project": "smoke", "verbosity": "terse"},
            )
        # recall_context returns a string; the suffix is appended.
        assert isinstance(result, str)
        assert "terse" in result.lower() or "brief" in result.lower()


# ── Default mode unchanged ───────────────────────────────────────────────────


class TestDefaultModeUnchanged:
    async def test_default_verbosity_no_hint_on_add(self) -> None:
        mock_mgr = _make_mock_manager()
        with (
            patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr),
            patch(
                "vesmaro.mcp_server.validate_tag_contract",
                side_effect=lambda tags, **_kw: tags,
            ),
        ):
            result = await _dispatch(
                "vesma_add",
                {
                    "content": "smoke content",
                    "tags": ["project:smoke", "agent:qa", "mnemos:decision"],
                    "verbosity": "default",
                },
            )
        assert isinstance(result, dict)
        assert "_output_style_hint" not in result

    async def test_default_verbosity_no_hint_on_search(self) -> None:
        mock_mgr = _make_mock_manager()
        with patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr):
            result = await _dispatch(
                "vesma_search",
                {"query": "smoke", "verbosity": "default"},
            )
        # No steering → bare list (backward compat).
        assert isinstance(result, list)

    async def test_disabled_output_style_no_hint(self) -> None:
        mock_mgr = _make_mock_manager()
        mock_mgr.settings.output_style.enabled = False
        with (
            patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr),
            patch(
                "vesmaro.mcp_server.validate_tag_contract",
                side_effect=lambda tags, **_kw: tags,
            ),
        ):
            result = await _dispatch(
                "vesma_add",
                {
                    "content": "smoke content",
                    "tags": ["project:smoke", "agent:qa", "mnemos:decision"],
                    "verbosity": "terse",
                },
            )
        assert isinstance(result, dict)
        assert "_output_style_hint" not in result


# ── Effort routing ──────────────────────────────────────────────────────────


class TestEffortRouting:
    async def test_low_effort_injects_hint_on_add(self) -> None:
        mock_mgr = _make_mock_manager()
        with (
            patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr),
            patch(
                "vesmaro.mcp_server.validate_tag_contract",
                side_effect=lambda tags, **_kw: tags,
            ),
        ):
            result = await _dispatch(
                "vesma_add",
                {
                    "content": "smoke content",
                    "tags": ["project:smoke", "agent:qa", "mnemos:decision"],
                    "effort": "low",
                },
            )
        assert isinstance(result, dict)
        assert "_output_style_hint" in result
        assert "low" in result["_output_style_hint"].lower()

    async def test_high_effort_injects_hint_on_add(self) -> None:
        mock_mgr = _make_mock_manager()
        with (
            patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr),
            patch(
                "vesmaro.mcp_server.validate_tag_contract",
                side_effect=lambda tags, **_kw: tags,
            ),
        ):
            result = await _dispatch(
                "vesma_add",
                {
                    "content": "smoke content",
                    "tags": ["project:smoke", "agent:qa", "mnemos:decision"],
                    "effort": "high",
                },
            )
        assert isinstance(result, dict)
        assert "_output_style_hint" in result
        assert "high" in result["_output_style_hint"].lower()

    async def test_medium_effort_no_hint(self) -> None:
        mock_mgr = _make_mock_manager()
        with (
            patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr),
            patch(
                "vesmaro.mcp_server.validate_tag_contract",
                side_effect=lambda tags, **_kw: tags,
            ),
        ):
            result = await _dispatch(
                "vesma_add",
                {
                    "content": "smoke content",
                    "tags": ["project:smoke", "agent:qa", "mnemos:decision"],
                    "effort": "medium",
                },
            )
        assert isinstance(result, dict)
        assert "_output_style_hint" not in result


# ── Backward compatibility ──────────────────────────────────────────────────


class TestBackwardCompatibility:
    async def test_add_without_new_params_works(self) -> None:
        mock_mgr = _make_mock_manager()
        with (
            patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr),
            patch(
                "vesmaro.mcp_server.validate_tag_contract",
                side_effect=lambda tags, **_kw: tags,
            ),
        ):
            result = await _dispatch(
                "vesma_add",
                {
                    "content": "smoke content",
                    "tags": ["project:smoke", "agent:qa", "mnemos:decision"],
                },
            )
        assert isinstance(result, dict)
        assert "id" in result
        assert "_output_style_hint" not in result

    async def test_search_without_new_params_returns_list(self) -> None:
        mock_mgr = _make_mock_manager()
        with patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr):
            result = await _dispatch("vesma_search", {"query": "smoke"})
        assert isinstance(result, list)

    async def test_recall_context_without_new_params_works(self) -> None:
        mock_mgr = _make_mock_manager()
        with patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr):
            result = await _dispatch("vesma_recall_context", {"project": "smoke"})
        assert isinstance(result, str)


# ── Guidance table sanity ───────────────────────────────────────────────────


class TestGuidanceTables:
    def test_default_verbosity_is_empty_string(self) -> None:
        assert _VERBOSITY_GUIDANCE["default"] == ""

    def test_medium_effort_is_empty_string(self) -> None:
        assert _EFFORT_GUIDANCE["medium"] == ""

    def test_terse_and_minimal_non_empty(self) -> None:
        assert _VERBOSITY_GUIDANCE["terse"]
        assert _VERBOSITY_GUIDANCE["minimal"]

    def test_low_and_high_effort_non_empty(self) -> None:
        assert _EFFORT_GUIDANCE["low"]
        assert _EFFORT_GUIDANCE["high"]


# ── QA fix #4 — config default_verbosity drives hint when arg absent ────────


class TestConfigDefaultVerbosity:
    async def test_config_default_verbosity_drives_hint_when_arg_absent(self) -> None:
        """When the caller omits `verbosity`, the config default_verbosity
        must drive the hint — not the hard-coded "default" fallback."""
        mock_mgr = _make_mock_manager()
        mock_mgr.settings.output_style.default_verbosity = "terse"
        with (
            patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr),
            patch(
                "vesmaro.mcp_server.validate_tag_contract",
                side_effect=lambda tags, **_kw: tags,
            ),
        ):
            # NOTE: no "verbosity" key in args — config default must apply.
            result = await _dispatch(
                "vesma_add",
                {
                    "content": "smoke content",
                    "tags": ["project:smoke", "agent:qa", "mnemos:decision"],
                },
            )
        assert isinstance(result, dict)
        assert "_output_style_hint" in result, (
            "config default_verbosity='terse' must inject a hint when arg is absent"
        )
        assert (
            "terse" in result["_output_style_hint"].lower()
            or "brief" in result["_output_style_hint"].lower()
        )

    async def test_config_default_verbosity_minimal_when_arg_absent(self) -> None:
        """Sanity: the same path works for the 'minimal' default."""
        mock_mgr = _make_mock_manager()
        mock_mgr.settings.output_style.default_verbosity = "minimal"
        with (
            patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr),
            patch(
                "vesmaro.mcp_server.validate_tag_contract",
                side_effect=lambda tags, **_kw: tags,
            ),
        ):
            result = await _dispatch(
                "vesma_add",
                {
                    "content": "smoke content",
                    "tags": ["project:smoke", "agent:qa", "mnemos:decision"],
                },
            )
        assert isinstance(result, dict)
        assert "_output_style_hint" in result
        assert (
            "minimal" in result["_output_style_hint"].lower()
            or "facts" in result["_output_style_hint"].lower()
        )

    async def test_config_default_effort_drives_hint_when_arg_absent(self) -> None:
        """The same wiring applies to default_effort."""
        mock_mgr = _make_mock_manager()
        mock_mgr.settings.output_style.default_effort = "high"
        with (
            patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr),
            patch(
                "vesmaro.mcp_server.validate_tag_contract",
                side_effect=lambda tags, **_kw: tags,
            ),
        ):
            result = await _dispatch(
                "vesma_add",
                {
                    "content": "smoke content",
                    "tags": ["project:smoke", "agent:qa", "mnemos:decision"],
                },
            )
        assert isinstance(result, dict)
        assert "_output_style_hint" in result
        assert "high" in result["_output_style_hint"].lower()


# ── QA fix #5 — invalid verbosity/effort value falls back with warning ──────


class TestInvalidVerbosityEffortFallback:
    async def test_invalid_verbosity_value_falls_back_to_default_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An invalid verbosity (e.g. 'verbose') must NOT inject a hint
        (falls back to 'default' → empty) and must log a WARNING."""
        mock_mgr = _make_mock_manager()
        # default_verbosity is "default" → fallback produces no hint.
        with (
            patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr),
            patch(
                "vesmaro.mcp_server.validate_tag_contract",
                side_effect=lambda tags, **_kw: tags,
            ),
            caplog.at_level(logging.WARNING, logger="vesmaro.mcp_server"),
        ):
            result = await _dispatch(
                "vesma_add",
                {
                    "content": "smoke content",
                    "tags": ["project:smoke", "agent:qa", "mnemos:decision"],
                    "verbosity": "verbose",  # invalid — typo for "terse"?
                },
            )
        assert isinstance(result, dict)
        # Fallback to "default" → no hint injected.
        assert "_output_style_hint" not in result
        # A warning was logged naming the invalid value.
        assert any(
            "Invalid verbosity" in r.message and "verbose" in r.message for r in caplog.records
        ), [r.message for r in caplog.records]

    async def test_invalid_effort_value_falls_back_to_medium_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An invalid effort (e.g. 'turbo') must fall back to 'medium'
        (no hint) and log a WARNING."""
        mock_mgr = _make_mock_manager()
        with (
            patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr),
            patch(
                "vesmaro.mcp_server.validate_tag_contract",
                side_effect=lambda tags, **_kw: tags,
            ),
            caplog.at_level(logging.WARNING, logger="vesmaro.mcp_server"),
        ):
            result = await _dispatch(
                "vesma_add",
                {
                    "content": "smoke content",
                    "tags": ["project:smoke", "agent:qa", "mnemos:decision"],
                    "effort": "turbo",  # invalid
                },
            )
        assert isinstance(result, dict)
        # Fallback to "medium" → no effort hint. And verbosity defaults to
        # "default" → no verbosity hint either. So no _output_style_hint.
        assert "_output_style_hint" not in result
        assert any(
            "Invalid effort" in r.message and "turbo" in r.message for r in caplog.records
        ), [r.message for r in caplog.records]

    async def test_invalid_verbosity_with_terse_default_falls_back_to_terse(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When the config default is non-default (e.g. 'terse'), an invalid
        arg falls back to that configured default, NOT to the hard-coded
        'default'. This proves the fallback reads the config."""
        mock_mgr = _make_mock_manager()
        mock_mgr.settings.output_style.default_verbosity = "terse"
        with (
            patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr),
            patch(
                "vesmaro.mcp_server.validate_tag_contract",
                side_effect=lambda tags, **_kw: tags,
            ),
            caplog.at_level(logging.WARNING, logger="vesmaro.mcp_server"),
        ):
            result = await _dispatch(
                "vesma_add",
                {
                    "content": "smoke content",
                    "tags": ["project:smoke", "agent:qa", "mnemos:decision"],
                    "verbosity": "verbose",  # invalid → falls back to config default "terse"
                },
            )
        assert isinstance(result, dict)
        assert "_output_style_hint" in result, (
            "invalid verbosity must fall back to config default 'terse', injecting a hint"
        )
        assert any("Invalid verbosity" in r.message for r in caplog.records)


# ── QA fix #6 — recall_context non-empty branch with verbosity ─────────────


class TestRecallContextNonEmptyWithVerbosity:
    async def test_recall_context_non_empty_appends_verbosity_suffix(self) -> None:
        """The non-empty branch of recall_context (joins memory contents +
        instructions + _steering_suffix) must append the verbosity suffix
        when a verbosity arg is passed."""
        mock_memory = MagicMock()
        mock_memory.effective_content.return_value = "## Goals\nsmoke goals"
        mock_mgr = _make_mock_manager()
        mock_mgr.recall_context.return_value = [mock_memory]
        with patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr):
            result = await _dispatch(
                "vesma_recall_context",
                {"project": "smoke", "verbosity": "terse"},
            )
        assert isinstance(result, str)
        # The memory content is present in the joined output.
        assert "smoke goals" in result
        # The terse suffix is appended at the end.
        assert "terse" in result.lower() or "brief" in result.lower()

    async def test_recall_context_non_empty_appends_minimal_suffix(self) -> None:
        """Sanity: minimal verbosity also reaches the non-empty branch."""
        mock_memory = MagicMock()
        mock_memory.effective_content.return_value = "## Goals\nsmoke goals"
        mock_mgr = _make_mock_manager()
        mock_mgr.recall_context.return_value = [mock_memory]
        with patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr):
            result = await _dispatch(
                "vesma_recall_context",
                {"project": "smoke", "verbosity": "minimal"},
            )
        assert isinstance(result, str)
        assert "smoke goals" in result
        assert "minimal" in result.lower() or "facts" in result.lower()
