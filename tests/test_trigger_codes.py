"""Tests for the federation trigger codes enum (Phase 1 prerequisite).

Covers contract §9 — exactly five codes, ``is_terminal``, and
``should_fallback_to_local`` for every code.
"""

from __future__ import annotations

import pytest

from vesma.trigger_codes import TriggerCode, is_terminal, should_fallback_to_local

# ── Enum membership ──────────────────────────────────────────────────────────


def test_trigger_code_has_exactly_five_values() -> None:
    """Contract §9 lists exactly five codes — no extras, no missing."""
    members = {c.value for c in TriggerCode}
    assert members == {
        "EXHAUSTIVE",
        "ALREADY_EXHAUSTED",
        "PARTIAL",
        "REFUSED",
        "OFFLINE_LITE",
    }


@pytest.mark.parametrize(
    "code",
    list(TriggerCode),
    ids=[c.value for c in TriggerCode],
)
def test_trigger_code_is_str_enum(code: TriggerCode) -> None:
    """TriggerCode is a StrEnum — each member is also a plain string."""
    assert isinstance(code, str)
    assert code == code.value


# ── is_terminal ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "code, expected",
    [
        (TriggerCode.EXHAUSTIVE, True),
        (TriggerCode.ALREADY_EXHAUSTED, True),
        (TriggerCode.REFUSED, True),
        (TriggerCode.PARTIAL, False),
        (TriggerCode.OFFLINE_LITE, False),
    ],
    ids=["EXHAUSTIVE", "ALREADY_EXHAUSTED", "REFUSED", "PARTIAL", "OFFLINE_LITE"],
)
def test_is_terminal(code: TriggerCode, expected: bool) -> None:
    """Terminal codes: A should not repeat the request."""
    assert is_terminal(code) is expected


# ── should_fallback_to_local ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "code, expected",
    [
        (TriggerCode.EXHAUSTIVE, False),
        (TriggerCode.ALREADY_EXHAUSTED, False),
        (TriggerCode.PARTIAL, False),
        (TriggerCode.REFUSED, True),
        (TriggerCode.OFFLINE_LITE, True),
    ],
    ids=["EXHAUSTIVE", "ALREADY_EXHAUSTED", "PARTIAL", "REFUSED", "OFFLINE_LITE"],
)
def test_should_fallback_to_local(code: TriggerCode, expected: bool) -> None:
    """REFUSED and OFFLINE_LITE → A falls back to local vesma_search (КП-2)."""
    assert should_fallback_to_local(code) is expected


# ── Docstrings present (contract §9 requires per-code explanation) ──────────


def test_every_code_has_a_docstring() -> None:
    """Each code's docstring explains when B returns it and what A does."""
    for code in TriggerCode:
        assert code.__doc__ is not None
        assert len(code.__doc__.strip()) > 20, f"{code.value} docstring too short"
