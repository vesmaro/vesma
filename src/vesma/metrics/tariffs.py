"""Model tariff table — the money axis of the metrics baseline (nhi-15).

A versioned DATA file (``tariffs.json``, shipped inside the wheel next
to this module) maps model ids to USD rates per 1M tokens: fresh input,
cached (hit) input and output. The loader is fail-closed on a malformed
table (wrong version, non-numeric or negative price) — a broken tariff
file must never yield silently-wrong money numbers. A model id that
resolves to no rate is not an error: the caller (the baseline report)
FLAGS it as unknown and prices nothing — numbers are never invented
(the honest-ledger rule of the vitals plane).

Model-id normalization: harnesses spell the same model many ways
(``account:plan/GLM-5.3-Flash``, ``zai-api/glm-5.3-flash``,
``GLM-5.3-Flash``); the resolver takes the last ``/``-or-``:`` segment
and matches case-insensitively against the table's keys. The canonical
spelling lives in the JSON file.
"""

# ── PROVENANCE ────────────────────────────────────────────────────────
# Vesma-wave addition (nhi-15 money baseline, 2026-10-08) — not part of
# the mnemos-vitals vendored surface. Rates: providers' official list
# prices at the ``as_of`` date (see the JSON ``sources``).

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files as _resource_files
from pathlib import Path
from typing import Any

TARIFFS_FILENAME = "tariffs.json"

#: Bump when the JSON contract changes shape; the loader refuses files
#: it does not understand (fail-closed, never guess).
SUPPORTED_SCHEMA_VERSION = 1

_TOKENS_PER_UNIT = 1_000_000


class TariffError(ValueError):
    """The tariff table is malformed — refuse to price anything."""


@dataclass(frozen=True)
class ModelRate:
    """USD per 1M tokens (list price)."""

    input: float
    cached_input: float
    output: float


@dataclass(frozen=True)
class TariffTable:
    """Immutable, validated view over ``tariffs.json``."""

    schema_version: int
    as_of: str
    currency: str
    rates: dict[str, ModelRate]  # keys: normalized (casefolded) model ids

    def rate(self, model_id: str | None) -> ModelRate | None:
        """Resolve a raw harness model id, or ``None`` when unknown.

        Unknown is a REPORTABLE state (the baseline flags it), never an
        exception — a new model appearing in the ledger must not break
        the report; it must show up as ``unknown``.
        """
        if not model_id:
            return None
        return self.rates.get(normalize_model_id(model_id))


def normalize_model_id(model_id: str) -> str:
    """Canonical lookup key: last ``/``/``:`` segment, casefolded.

    ``account:zai-plan/GLM-5.3-Flash`` → ``glm-5.3-flash``; bare
    ``GLM-5.3-Flash`` → ``glm-5.3-flash``. Provider prefixes are
    ownership slugs, not part of the model identity.
    """
    tail = model_id.replace(":", "/").rsplit("/", 1)[-1].strip()
    return tail.casefold()


def parse_tariffs(raw: str | bytes) -> TariffTable:
    """Validate and build a :class:`TariffTable` from JSON text.

    Fail-closed: unknown ``schema_version``, a non-object ``models``
    map, or any non-numeric / negative rate raises :class:`TariffError`.
    """
    try:
        doc: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TariffError(f"tariffs: not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise TariffError("tariffs: top level must be an object")
    version = doc.get("schema_version")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise TariffError(
            f"tariffs: unsupported schema_version {version!r}"
            f" (loader supports {SUPPORTED_SCHEMA_VERSION})"
        )
    as_of = doc.get("as_of")
    if not isinstance(as_of, str) or not as_of:
        raise TariffError("tariffs: 'as_of' must be a non-empty string")
    currency = doc.get("currency")
    if not isinstance(currency, str) or not currency:
        raise TariffError("tariffs: 'currency' must be a non-empty string")
    models = doc.get("models")
    if not isinstance(models, dict) or not models:
        raise TariffError("tariffs: 'models' must be a non-empty object")
    rates: dict[str, ModelRate] = {}
    for model, spec in models.items():
        if not isinstance(model, str) or not isinstance(spec, dict):
            raise TariffError(f"tariffs: bad model entry {model!r}")
        fields = {}
        for key in ("input", "cached_input", "output"):
            value = spec.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TariffError(f"tariffs: {model}.{key} must be a number")
            if value < 0:
                raise TariffError(f"tariffs: {model}.{key} must be >= 0")
            fields[key] = float(value)
        rates[normalize_model_id(model)] = ModelRate(**fields)
    return TariffTable(
        schema_version=SUPPORTED_SCHEMA_VERSION, as_of=as_of, currency=currency, rates=rates
    )


def load_tariffs(path: Path | None = None) -> TariffTable:
    """Load the packaged tariff table (or an explicit override file)."""
    if path is None:
        raw: str = _resource_files("vesma.metrics").joinpath(TARIFFS_FILENAME).read_text("utf-8")
    else:
        raw = path.read_text("utf-8")
    return parse_tariffs(raw)


def turn_cost_usd(
    rate: ModelRate, *, input_tokens: int, cached_read_tokens: int, output_tokens: int
) -> float:
    """USD cost of one billed turn under ``rate``.

    Providers report the cached (hit) input INSIDE the turn's total
    input (verified on the live ledger: ``cache_read <= input`` in
    100% of turns), so the fresh-input volume is
    ``input_tokens - cached_read_tokens`` (floored at 0 against hostile
    counters). Output is billed flat; ``reasoning_tokens`` are assumed
    to already ride inside the provider's output count and are NOT
    added (double-billing guard). Cached WRITE tokens are not billed
    in v1 (Z.AI storage is limited-time free).
    """
    fresh = max(0, input_tokens - cached_read_tokens)
    cost = (
        fresh * rate.input + cached_read_tokens * rate.cached_input + output_tokens * rate.output
    ) / _TOKENS_PER_UNIT
    return cost


__all__ = [
    "SUPPORTED_SCHEMA_VERSION",
    "TARIFFS_FILENAME",
    "ModelRate",
    "TariffError",
    "TariffTable",
    "load_tariffs",
    "normalize_model_id",
    "parse_tariffs",
    "turn_cost_usd",
]
