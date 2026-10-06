"""Board card ``vesma-ops-mode-ux`` — the awareness ops CLI surface.

Covers the two surfaces the wave lands over the existing engine (no
awareness semantics touched — the gates themselves are out of scope):

* ``vesma awareness get`` — raw config-file value + effective value (env
  overrides included), zero-config refusal;
* ``vesma awareness set`` — end-to-end mode switch: validation against the
  born-final ladder (unknown value refused with the allowed list, NOTHING
  written), atomic YAML write preserving sibling mappings, the restart
  note always printed (a running server reads the config at startup —
  never silently assume a reload), and the effective value observable on
  a fresh settings load;
* ``vesma awareness stats`` — the wave-0 funnel over ``awareness_events``
  read through the sink's own connection (no operator SQL): kind counts,
  suppressions by reason, calm|delta split, tail token cost, --project
  scoping, --window-hours bounding (old events excluded), absent-sidecar
  degradation to a hint line (exit 0 — a broken metrics plane must not
  make console reads fatal), unreadable-meta degradation (skip, not
  crash), and zero peer content in the output (identity slugs never
  printed).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from vesmaro.cli._manager import reset_manager
from vesmaro.cli.awareness_cmd import AWARENESS_MODES, _awareness_stats
from vesmaro.cli.main import app
from vesmaro.metrics.sink import MetricsStore

runner = CliRunner()


@pytest.fixture
def ops_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated config + data dir; the manager singleton reset around it."""
    reset_manager()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"mnemos:\n"
        f"  vault_path: {tmp_path / 'vault'}\n"
        f"  data_dir: {tmp_path / 'data'}\n"
        f"  db_name: t.db\n"
    )
    monkeypatch.setenv("VESMA_CONFIG", str(cfg))
    monkeypatch.setenv("VESMARO_CONFIG", str(cfg))
    yield cfg
    reset_manager()


PROJ = "proj"
PROJ2 = "other"


def load_settings_for(cfg: Path) -> Any:
    """Settings from the isolated config path (same precedence the CLI uses)."""
    import os

    from vesmaro.config import load_settings

    prev = os.environ.get("VESMA_CONFIG")
    os.environ["VESMA_CONFIG"] = str(cfg)
    os.environ.pop("VESMARO_AWARENESS__NATIVE_HEARTBEAT_MODE", None)
    os.environ.pop("VESMA_AWARENESS__NATIVE_HEARTBEAT_MODE", None)
    try:
        return load_settings(str(cfg))
    finally:
        if prev is not None:
            os.environ["VESMA_CONFIG"] = prev
        else:
            os.environ.pop("VESMA_CONFIG", None)


def _seed_sidecar(data_dir: Path) -> MetricsStore:
    """Real sink writes into the exact sidecar path ``stats`` reads."""
    store = MetricsStore(data_dir / "metrics.sqlite")
    store.record_awareness_event(
        kind="tool_call", project=PROJ, agent="a1", session="s1", meta={"tool": "mnemos_search"}
    )
    store.record_awareness_event(
        kind="tool_call", project=PROJ, agent="a2", session="s2", meta={"tool": "mnemos_add"}
    )
    store.record_awareness_event(
        kind="peer_write", project=PROJ, agent="a2", session="s2", meta={"tool": "mnemos_add"}
    )
    store.record_awareness_event(kind="delta_available", project=PROJ, agent="a1", meta={})
    store.record_awareness_event(
        kind="heartbeat_delivery",
        project=PROJ,
        agent="a1",
        meta={
            "state": "delta",
            "lines": 6,
            "tokens_est": 118,
            "cursor_before": None,
            "cursor_after": "2026-10-01T12:00:00+00:00",
            "tool": "mnemos_search",
        },
    )
    store.record_awareness_event(
        kind="heartbeat_delivery",
        project=PROJ,
        agent="a1",
        meta={"state": "calm", "tokens_est": 10},
    )
    store.record_awareness_event(
        kind="heartbeat_suppressed", project=PROJ2, agent="a3", meta={"reason": "rate_cap"}
    )
    store.record_awareness_event(
        kind="conflict_hint_emitted", project=PROJ, agent="a1", meta={"tool": "mnemos_search"}
    )
    return store


# ── get ───────────────────────────────────────────────────────────────────────


class TestModeGet:
    def test_raw_not_set_shows_default(self, ops_config: Path) -> None:
        result = runner.invoke(app, ["awareness", "get"])
        assert result.exit_code == 0
        assert "not set" in result.output
        assert "effective: off" in result.output

    def test_raw_and_effective_after_set(
        self, ops_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("vesmaro.cli._manager._manager", None)
        assert runner.invoke(app, ["awareness", "set", "shadow"]).exit_code == 0
        reset_manager()
        result = runner.invoke(app, ["awareness", "get"])
        assert result.exit_code == 0
        assert "native_heartbeat_mode: shadow" in result.output
        assert "effective: shadow" in result.output

    def test_env_override_flagged_in_output(
        self, ops_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("vesmaro.cli._manager._manager", None)
        monkeypatch.setenv("VESMA_AWARENESS__NATIVE_HEARTBEAT_MODE", "canary")
        result = runner.invoke(app, ["awareness", "get"])
        # effective must reflect the file value here (config > env per the
        # dual-source precedence), but the ACTIVE env twin is always named
        assert "env override active" in result.output
        assert "VESMA_AWARENESS__NATIVE_HEARTBEAT_MODE=canary" in result.output

    def test_zero_config_refuses(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        reset_manager()
        monkeypatch.delenv("VESMA_CONFIG", raising=False)
        monkeypatch.delenv("VESMARO_CONFIG", raising=False)
        monkeypatch.chdir(tmp_path)
        # the home fallback (~/.mnemos/config.yaml) must also point away
        monkeypatch.setenv("HOME", str(tmp_path))
        result = runner.invoke(app, ["awareness", "get"])
        assert result.exit_code == 1
        assert "zero-config" in result.output


# ── set ───────────────────────────────────────────────────────────────────────


class TestModeSet:
    def test_set_writes_config_and_echoes(
        self, ops_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reset_manager()
        result = runner.invoke(app, ["awareness", "set", "canary"])
        assert result.exit_code == 0, result.output
        assert "native_heartbeat_mode = canary" in result.output
        assert str(ops_config) in result.output
        # the atomic write preserved the sibling mapping
        text = ops_config.read_text()
        assert "data_dir:" in text
        assert "vault_path:" in text
        assert "native_heartbeat_mode: canary" in text

    def test_set_then_load_settings_sees_value(
        self, ops_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reset_manager()
        monkeypatch.delenv("VESMARO_AWARENESS__NATIVE_HEARTBEAT_MODE", raising=False)
        monkeypatch.delenv("VESMA_AWARENESS__NATIVE_HEARTBEAT_MODE", raising=False)
        assert runner.invoke(app, ["awareness", "set", "shadow"]).exit_code == 0
        reset_manager()
        from vesmaro.config import load_settings

        settings = load_settings(str(ops_config))
        assert settings.awareness.native_heartbeat_mode == "shadow"

    def test_unknown_value_refused_and_nothing_written(self, ops_config: Path) -> None:
        before = ops_config.read_text()
        result = runner.invoke(app, ["awareness", "set", "turbo"])
        assert result.exit_code == 1
        assert "unknown mode 'turbo'" in result.output
        assert "off, shadow, canary, on" in result.output
        assert ops_config.read_text() == before

    def test_restart_note_always_printed(self, ops_config: Path) -> None:
        result = runner.invoke(app, ["awareness", "set", "on"])
        assert result.exit_code == 0
        # the note is unconditional — a silent no-restart assumption is banned
        assert "takes effect on the NEXT server start" in result.output

    def test_set_preserves_other_awareness_keys(
        self, ops_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reset_manager()
        ops_config.write_text(
            ops_config.read_text() + "awareness:\n" + "  heartbeat_rate_limit_per_minute: 5\n"
        )
        result = runner.invoke(app, ["awareness", "set", "shadow"])
        assert result.exit_code == 0
        text = ops_config.read_text()
        assert "heartbeat_rate_limit_per_minute: 5" in text
        assert "native_heartbeat_mode: shadow" in text

    def test_set_creates_awareness_section_when_missing(self, ops_config: Path) -> None:
        result = runner.invoke(app, ["awareness", "set", "off"])
        assert result.exit_code == 0
        import yaml

        data = yaml.safe_load(ops_config.read_text())
        assert isinstance(data, dict)
        assert data["awareness"]["native_heartbeat_mode"] == "off"

    def test_modes_ladder_matches_born_final(self) -> None:
        from vesmaro.config import AwarenessConfig

        field_mode = AwarenessConfig.model_fields["native_heartbeat_mode"]
        allowed = set(field_mode.annotation.__args__)
        assert set(AWARENESS_MODES) == allowed


# ── stats ─────────────────────────────────────────────────────────────────────


class TestStats:
    def test_funnel_counts_roundtrip(self, ops_config: Path) -> None:
        reset_manager()
        store = _seed_sidecar(ops_config.parent / "data")
        store.close()
        result = runner.invoke(app, ["awareness", "stats"])
        assert result.exit_code == 0, result.output
        assert "tool_call (denominator): 2" in result.output
        assert "peer_write: 1" in result.output
        assert "delta_available: 1" in result.output
        assert "heartbeat_delivery: 2" in result.output
        assert "calm 1 / delta 1" in result.output
        assert "heartbeat_suppressed: 1" in result.output
        assert "rate_cap=1" in result.output
        assert "conflict_hint_emitted: 1" in result.output
        assert "sum 128 over 2 deliveries" in result.output

    def test_project_scoping(self, ops_config: Path) -> None:
        reset_manager()
        store = _seed_sidecar(ops_config.parent / "data")
        store.close()
        result = runner.invoke(app, ["awareness", "stats", "--project", "other"])
        assert result.exit_code == 0
        assert "heartbeat_suppressed: 1" in result.output
        assert "tool_call (denominator): 0" in result.output

    def test_window_hours_excludes_old_events(self, ops_config: Path) -> None:
        reset_manager()
        store = _seed_sidecar(ops_config.parent / "data")
        store.close()
        import sqlite3
        from datetime import UTC, datetime, timedelta

        very_old = (datetime.now(UTC) - timedelta(days=30)).timestamp()
        conn = sqlite3.connect(ops_config.parent / "data" / "metrics.sqlite")
        try:
            conn.execute(
                "UPDATE awareness_events SET ts = ?"
                " WHERE ts = (SELECT MIN(ts) FROM awareness_events)",
                (very_old,),
            )
            conn.commit()
        finally:
            conn.close()
        result = runner.invoke(app, ["awareness", "stats", "--window-hours", "24"])
        assert result.exit_code == 0
        assert "tool_call (denominator): 1" in result.output

    def test_absent_sidecar_is_zeroes_not_a_crash(self, ops_config: Path) -> None:
        reset_manager()
        result = runner.invoke(app, ["awareness", "stats"])
        assert result.exit_code == 0
        # The CLI reopens the plane via create_vitals_store, which CREATES
        # the sidecar (same bootstrap the server uses) — an absent one
        # therefore reads as an all-zero funnel, exit 0, never a crash.
        assert "tool_call (denominator): 0" in result.output

    def test_disabled_plane_degrades_to_hint(self, ops_config: Path) -> None:
        reset_manager()
        ops_config.write_text(ops_config.read_text() + "vitals:\n  enabled: false\n")
        result = runner.invoke(app, ["awareness", "stats"])
        assert result.exit_code == 0
        assert "sidecar unavailable" in result.output
        assert "vitals.enabled=false" in result.output

    def test_unreadable_meta_skipped_not_crash(self, ops_config: Path) -> None:
        reset_manager()
        store = MetricsStore(ops_config.parent / "data" / "metrics.sqlite")
        store.record_awareness_event(
            kind="heartbeat_delivery",
            project=PROJ,
            agent="a1",
            meta={"state": "delta", "tokens_est": 5},
        )
        store.close()
        import sqlite3

        conn = sqlite3.connect(ops_config.parent / "data" / "metrics.sqlite")
        try:
            conn.execute("UPDATE awareness_events SET meta_json = 'not-json{'")
            conn.commit()
        finally:
            conn.close()
        result = runner.invoke(app, ["awareness", "stats"])
        assert result.exit_code == 0
        # the delivery row was skipped by the meta-readability degradation,
        # but the kind-level COUNT still reports it (read through the row)
        assert "heartbeat_delivery: 1" in result.output

    def test_output_carries_no_peer_content(self, ops_config: Path) -> None:
        reset_manager()
        store = _seed_sidecar(ops_config.parent / "data")
        store.close()
        result = runner.invoke(app, ["awareness", "stats"])
        assert result.exit_code == 0
        # identity slugs are aggregates-only: none of the seeded agent slugs
        # appears in the output (CWE-359 discipline even in aggregates)
        assert "a1" not in result.output
        assert "a2" not in result.output
        assert "a3" not in result.output
        assert "s1" not in result.output

    def test_stats_helper_direct_shape(self, ops_config: Path) -> None:
        store = _seed_sidecar(ops_config.parent / "data")
        store.close()
        # Re-open through the CLI's exact construction (the settings-derived
        # path — the sidecar filename is never spelled in the CLI module).
        from vesmaro.metrics.boundary import create_vitals_store

        settings = load_settings_for(ops_config)
        fresh = create_vitals_store(settings)
        assert fresh is not None
        try:
            data = _awareness_stats(fresh, window_hours=24, project=None)
        finally:
            fresh.close()
        assert data["available"] is True
        assert data["events"]["tool_call"] == 2
        assert data["suppressions_by_reason"]["rate_cap"] == 1
        assert data["deliveries_state"] == {"calm": 1, "delta": 1}
        assert data["tokens_sum"] == 128
        assert data["tokens_n"] == 2


# ── help-tree parity (the UX canon: -h works at every level) ─────────────────


class TestHelpSurface:
    def test_group_help(self) -> None:
        result = runner.invoke(app, ["awareness", "--help"])
        assert result.exit_code == 0
        for verb in ("get", "set", "stats"):
            assert verb in result.output

    def test_minus_h_group(self) -> None:
        result = runner.invoke(app, ["awareness", "-h"])
        assert result.exit_code == 0

    @pytest.mark.parametrize("verb", ["get", "set", "stats"])
    def test_verb_help_bodies(self, verb: str) -> None:
        result = runner.invoke(app, ["awareness", verb, "-h"])
        assert result.exit_code == 0
        assert "Usage:" in result.output
