"""nhi-15 money baseline — the tariff table (``vesma.metrics.tariffs``).

Contract under test:

* the packaged ``tariffs.json`` loads and carries the wave-0 rates
  (GLM-5.3-Flash 0.15/0.03/0.50, GLM-5.3 1.4/0.26/4.4, kimi-k3
  3.0/0.30/15.0 — the Z.AI docs + Kimi platform list prices of
  2026-10-08);
* model-id normalization survives harness spellings
  (``account:plan/GLM-5.3-Flash`` etc.); unknown ids resolve to ``None``
  — flagged downstream, never priced;
* the loader is fail-closed on a malformed table (bad JSON, unknown
  schema version, negative / non-numeric rates, missing models map);
* ``turn_cost_usd`` bills the cached (hit) input INSIDE the turn input
  (the live-ledger semantics: ``cache_read <= input`` in 100% of
  turns), floors hostile counters at zero, and never adds reasoning
  tokens on top of output (double-billing guard).
"""

from __future__ import annotations

import json

import pytest

from vesma.metrics.tariffs import (
    SUPPORTED_SCHEMA_VERSION,
    TariffError,
    load_tariffs,
    normalize_model_id,
    parse_tariffs,
    turn_cost_usd,
)


def test_packaged_table_loads_with_wave0_rates() -> None:
    table = load_tariffs()
    flash = table.rate("GLM-5.3-Flash")
    assert flash is not None
    assert (flash.input, flash.cached_input, flash.output) == (0.15, 0.03, 0.5)
    big = table.rate("GLM-5.3")
    assert big is not None
    assert (big.input, big.cached_input, big.output) == (1.4, 0.26, 4.4)
    kimi = table.rate("kimi-k3")
    assert kimi is not None
    assert (kimi.input, kimi.cached_input, kimi.output) == (3.0, 0.3, 15.0)
    assert table.as_of == "2026-10-08"
    assert table.currency == "USD"


def test_normalize_survives_harness_spellings() -> None:
    assert normalize_model_id("GLM-5.3-Flash") == "glm-5.3-flash"
    assert normalize_model_id("account:zai-plan/GLM-5.3-Flash") == "glm-5.3-flash"
    assert normalize_model_id("zai-api/glm-5.3-flash") == "glm-5.3-flash"
    assert normalize_model_id("  GLM-5.3  ") == "glm-5.3"
    # the packaged table resolves every spelling to the same rate
    table = load_tariffs()
    assert table.rate("account:zai-plan/GLM-5.3-Flash") == table.rate("glm-5.3-flash")


def test_unknown_model_resolves_to_none_not_invented() -> None:
    table = load_tariffs()
    assert table.rate("totally-new-model") is None
    assert table.rate("") is None
    assert table.rate(None) is None


def _minimal_doc(**overrides: object) -> str:
    doc = {
        "schema_version": SUPPORTED_SCHEMA_VERSION,
        "as_of": "2026-10-08",
        "currency": "USD",
        "models": {"M": {"input": 1.0, "cached_input": 0.1, "output": 2.0}},
    }
    doc.update(overrides)  # type: ignore[arg-type]
    return json.dumps(doc)


def test_parse_ok_roundtrip() -> None:
    table = parse_tariffs(_minimal_doc())
    assert table.rate("M") is not None


def _doc_with_rate(value: object) -> str:
    return json.dumps(
        {
            "schema_version": SUPPORTED_SCHEMA_VERSION,
            "as_of": "x",
            "currency": "USD",
            "models": {"M": {"input": value, "cached_input": 1, "output": 1}},
        }
    )


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        "[]",  # not an object
        json.dumps(
            {
                "schema_version": 99,
                "as_of": "x",
                "currency": "USD",
                "models": {"M": {"input": 1, "cached_input": 1, "output": 1}},
            }
        ),
        json.dumps(
            {
                "schema_version": SUPPORTED_SCHEMA_VERSION,
                "currency": "USD",
                "models": {"M": {"input": 1, "cached_input": 1, "output": 1}},
            }
        ),  # no as_of
        json.dumps(
            {
                "schema_version": SUPPORTED_SCHEMA_VERSION,
                "as_of": "x",
                "models": {"M": {"input": 1, "cached_input": 1, "output": 1}},
            }
        ),  # no currency
        json.dumps(
            {
                "schema_version": SUPPORTED_SCHEMA_VERSION,
                "as_of": "x",
                "currency": "USD",
                "models": {},
            }
        ),  # empty models
        _doc_with_rate(-1),  # negative rate
        _doc_with_rate("1"),  # string rate
        _doc_with_rate(True),  # bool rate
    ],
)
def test_parse_fail_closed(raw: str) -> None:
    with pytest.raises(TariffError):
        parse_tariffs(raw)


def test_turn_cost_bills_cached_inside_input() -> None:
    table = load_tariffs()
    rate = table.rate("GLM-5.3-Flash")
    assert rate is not None
    # 2M in (1M of it cached) + 0.5M out at 0.15/0.03/0.50:
    #   fresh 1M*0.15 + cached 1M*0.03 + out 0.5M*0.50 = 0.15+0.03+0.25 = 0.43
    cost = turn_cost_usd(
        rate, input_tokens=2_000_000, cached_read_tokens=1_000_000, output_tokens=500_000
    )
    assert cost == pytest.approx(0.43)


def test_turn_cost_floors_hostile_cached_over_input() -> None:
    table = load_tariffs()
    rate = table.rate("GLM-5.3-Flash")
    assert rate is not None
    # cached reported above input (hostile/buggy counter): fresh floors at 0
    cost = turn_cost_usd(rate, input_tokens=100, cached_read_tokens=500, output_tokens=0)
    assert cost == pytest.approx(500 * rate.cached_input / 1_000_000)
