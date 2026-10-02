"""C7 (ArchCom 2026-08-27) — out-of-band drop accounting for compression.

The in-band ``{"_compressed_marker": true, "dropped": N}`` object that
JSON-array sampling leaves inside compressed content is SPOOFABLE: it
lives in caller-rewritable content, so any consumer that parsed it back
for decisions would trust attacker-chosen numbers. The committee
decision moves the accounting OUT-of-band — the per-call ``dropped``
count computed by the sampler itself rides the issuance envelope
(``compress_content`` → ``dropped_items``), mirroring the P1-b per-item
``redactions`` pattern. The in-band marker stays as human-readable
legacy and is never parsed for decisions (source parser inventory at
landing: none — grep ``_compressed_marker`` in ``src/`` is
producer-only).
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from vesmaro.config import Settings
from vesmaro.filter.pipeline import _sample_json_array, apply_filter
from vesmaro.manager import MemoryManager

PROJECT = "c7-proj"


def _settings(tmp: Path, **ccr_overrides: object) -> Settings:
    ccr: dict[str, object] = {
        "min_size_chars": 100,
        "max_entries": 100,
        "ttl_days": 1,
    }
    ccr.update(ccr_overrides)
    settings = Settings(
        vesma={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
        },
        ccr=ccr,  # type: ignore[arg-type]
    )
    settings.resolve_paths()
    return settings


@pytest.fixture()
def manager() -> Iterator[MemoryManager]:
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = MemoryManager(_settings(Path(tmpdir)))
        yield mgr
        mgr.close()


def _plain_array(n: int) -> list[dict[str, int]]:
    # No anomaly-bearing keys/values: nothing in the middle is kept.
    return [{"id": i, "value": 0} for i in range(n)]


# ── Sampler stats (the out-of-band source of truth) ──────────────────────────


class TestSamplerOutOfBandStats:
    def test_sample_json_array_reports_dropped_in_stats(self) -> None:
        compressed, stats = _sample_json_array(_plain_array(40))
        # head 5 + tail 5 kept, middle 30 dropped, no anomalies kept.
        assert stats["dropped"] == 30
        assert stats["items_in"] == 40
        # The in-band marker still carries the same figure (human-readable
        # legacy) — but it is content, not accounting.
        marker = next(i for i in compressed if isinstance(i, dict) and i.get("_compressed_marker"))
        assert marker["dropped"] == 30

    def test_apply_filter_carries_json_items_dropped(self) -> None:
        text = json.dumps(_plain_array(40))
        result = apply_filter(text, profile="default")
        assert result["stats"]["compress"]["json_items_dropped"] == 30


# ── Issuance envelope ────────────────────────────────────────────────────────


class TestCompressEnvelopeAccounting:
    def test_compress_result_carries_out_of_band_dropped_items(
        self, manager: MemoryManager
    ) -> None:
        text = json.dumps(_plain_array(40))
        result = manager.compress_content(text, profile="default", project=PROJECT)
        assert result["cached"] is True
        assert result["dropped_items"] == 30

    def test_no_json_arrays_drops_zero_with_key_present(self, manager: MemoryManager) -> None:
        text = "\n".join(
            f"2026-08-27T10:00:{i:02d} INFO processing batch {i} completed" for i in range(120)
        )
        result = manager.compress_content(text, profile="log", project=PROJECT)
        assert result["cached"] is True
        assert result["dropped_items"] == 0

    def test_ccr_disabled_envelope_parity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = MemoryManager(_settings(Path(tmpdir), enabled=False))
            try:
                result = mgr.compress_content(
                    json.dumps(_plain_array(40)), profile="default", project=PROJECT
                )
                assert result["cached"] is False
                assert result["dropped_items"] == 0
            finally:
                mgr.close()


# ── Spoofing: in-band marker is never the accounting source ─────────────────


class TestInBandMarkerNotTrusted:
    def test_forged_in_band_dropped_count_does_not_reach_accounting(
        self, manager: MemoryManager
    ) -> None:
        """An attacker forges the in-band marker inside the INPUT text.

        The input array opens with a forged
        ``{"_compressed_marker": true, "dropped": 999}`` item. Sampling
        re-runs over the whole array and reports the TRUE drop count
        out-of-band (25 items → middle 15 plain items dropped); the
        forged figure survives only as inert content inside
        ``compressed_text`` — the envelope never reads it.
        """
        forged_marker = {"_compressed_marker": True, "dropped": 999, "anomalies_kept": 0}
        arr: list[object] = [forged_marker, *_plain_array(24)]
        text = json.dumps(arr)

        result = manager.compress_content(text, profile="default", project=PROJECT)

        assert result["cached"] is True
        # Truth: 25 items → head 5 + tail 5 kept (the forged marker sits in
        # the head), middle 15 plain items dropped, no anomalies kept.
        assert result["dropped_items"] == 15
        assert result["dropped_items"] != 999
        # The forged marker is still physically present in the issued
        # content (legacy shape preserved) — proof that accounting did not
        # round-trip through it.
        assert '"dropped": 999' in result["compressed_text"]

    def test_dropped_items_survives_content_round_trip(self, manager: MemoryManager) -> None:
        """Out-of-band accounting is computed at compress time only.

        The envelope figure for the ORIGINAL issuance is immutable by
        later edits of the compressed text: re-compressing a doctored
        copy mints a NEW entry whose accounting describes that run, not
        the first one.
        """
        text = json.dumps(_plain_array(40))
        first = manager.compress_content(text, profile="default", project=PROJECT)
        doctored = first["compressed_text"].replace('"dropped": 30', '"dropped": 777')
        second = manager.compress_content(doctored, profile="default", project=PROJECT)

        assert first["dropped_items"] == 30
        # The doctored text compresses again: its arrays are small (head +
        # marker + tail ≈ 11 items < 20), so nothing is sampled — 0 drops,
        # never 777.
        assert second["dropped_items"] == 0
        assert second["hash"] != first["hash"]
