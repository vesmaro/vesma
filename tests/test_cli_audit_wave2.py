"""cli-audit 2026-10-08 wave-2 fix family (P2 clusters).

Covers the P2 catalog clusters this wave closes:

* peer mTLS warnings — once per peer per process, ``--quiet`` suppresses,
  ``--help`` loads no config (the vitals recorder skips help/version runs);
* ``stats`` — nested dict values render as readable blocks (no raw dict
  reprs), ``--json`` for scripting;
* ``update timer status`` — box legend for state vs host_state (text + json);
* doctor Tag contract — example ids + listing hint, singular/plural;
* ``agent-token list --json`` — full jtis (the table truncates them);
* ``graph delete`` ghost-gate refusal — CLI flags, not MCP kwargs.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from vesma.cli import update_cmd as update_cmd_mod
from vesma.cli._manager import reset_manager
from vesma.cli.graph_cmd import _to_cli_gate_syntax
from vesma.cli.main import _metrics_command_token, app
from vesma.config import _MTLS_PINNING_WARNED, _warn_federation_mtls_pinning_off

runner = CliRunner()


# ── peer mTLS warnings (once per process) ─────────────────────────────────────


class _PeerStub:
    def __init__(self, fingerprint: str | None) -> None:
        self.mtls_cert_fingerprint = fingerprint


class _FedStub:
    def __init__(self, peers: dict[str, Any]) -> None:
        self.peers = peers


class _SettingsStub:
    def __init__(self, peers: dict[str, Any]) -> None:
        self.federation = _FedStub(peers)


@pytest.fixture(autouse=True)
def _reset_warned_peers() -> Any:
    """The warned-peer set is process-global — isolate per test."""
    saved = set(_MTLS_PINNING_WARNED)
    _MTLS_PINNING_WARNED.clear()
    yield
    _MTLS_PINNING_WARNED.clear()
    _MTLS_PINNING_WARNED.update(saved)


def test_peer_mtls_warning_emitted_once_per_peer(caplog: pytest.LogCaptureFixture) -> None:
    """Two config loads in one process (command body + vitals recorder) used
    to print the SAME peer warning twice (cli-audit 2026-10-08 P2)."""
    settings = _SettingsStub({"peer-a": _PeerStub(None)})
    with caplog.at_level(logging.WARNING, logger="vesma.config"):
        _warn_federation_mtls_pinning_off(settings)
        _warn_federation_mtls_pinning_off(settings)  # second load_settings
    records = [r for r in caplog.records if "peer-a" in r.getMessage()]
    assert len(records) == 1
    assert "mtls_cert_fingerprint" in records[0].getMessage()


def test_peer_mtls_warning_per_peer_independent(caplog: pytest.LogCaptureFixture) -> None:
    """A peer without a fingerprint and a pinned peer are tracked separately:
    the pinned one never warns, the unpinned one warns once."""
    settings = _SettingsStub({"pinned": _PeerStub("ab" * 32), "open": _PeerStub(None)})
    with caplog.at_level(logging.WARNING, logger="vesma.config"):
        _warn_federation_mtls_pinning_off(settings)
        _warn_federation_mtls_pinning_off(settings)
    assert [r for r in caplog.records if "pinned" in r.getMessage()] == []
    assert len([r for r in caplog.records if "'open'" in r.getMessage()]) == 1


def test_quiet_flag_silences_vesma_warnings() -> None:
    """``vesma --quiet <cmd>`` raises the vesma loggers to ERROR so the
    config-load-time mTLS hints stay off the terminal (audit P2)."""
    result = runner.invoke(app, ["--quiet", "completion", "--show-instructions"])
    assert result.exit_code == 0, result.output
    vesma_logger = logging.getLogger("vesma")
    try:
        assert not vesma_logger.isEnabledFor(logging.WARNING)
        assert vesma_logger.isEnabledFor(logging.ERROR)
    finally:
        vesma_logger.setLevel(logging.NOTSET)


def test_metrics_command_token_skips_help_and_version_runs() -> None:
    """The vitals recorder must not treat --help/--version runs as commands:
    they used to load the operator's config just to record a verb
    (cli-audit 2026-10-08 P2: warnings on bare --help)."""
    assert _metrics_command_token(["vesma"]) is None
    assert _metrics_command_token(["vesma", "--help"]) is None
    assert _metrics_command_token(["vesma", "--version"]) is None
    assert _metrics_command_token(["vesma", "-h", "--json"]) is None
    # value-taking global option: its VALUE is not a command
    assert _metrics_command_token(["vesma", "--config", "/tmp/x.yaml", "stats"]) == "stats"
    assert _metrics_command_token(["vesma", "-c", "cfg.yaml", "doctor"]) == "doctor"
    assert _metrics_command_token(["vesma", "stats"]) == "stats"


def test_bare_invocation_shows_help_and_exits_zero() -> None:
    """cli-audit 2026-10-08 (P3): bare `vesma` = help on stdout + exit 0
    (click's no_args_is_help path used to exit 2 with help on stderr)."""
    result = runner.invoke(app, [])
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


# ── stats snapshot rendering ──────────────────────────────────────────────────


def test_stats_no_raw_dict_reprs() -> None:
    """cli-audit 2026-10-08 (P2): nested snapshot dicts printed as raw Python
    reprs (newlines mid-value). Now: header + indented subkeys."""
    reset_manager()
    result = runner.invoke(app, ["stats"])
    assert result.exit_code == 0, result.output
    assert "{'" not in result.output and '"}' not in result.output
    # the always-present nested block renders as header + indented subkeys
    assert "filter:" in result.output
    assert any(line.startswith("    auto_filter:") for line in result.output.splitlines())


def test_stats_json_flag() -> None:
    reset_manager()
    result = runner.invoke(app, ["stats", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert isinstance(payload["filter"], dict)  # nested block survives as an object


# ── update timer status legend ────────────────────────────────────────────────


def _spy_timer_env(monkeypatch: pytest.MonkeyPatch, *, in_box: bool) -> None:
    """Stub the systemctl/distrobox seams of update_cmd: local manager reports
    'not installed'; in-box runs also see HOST units installed-disabled."""

    def _systemctl(*_args: Any, **_kw: Any) -> int | None:
        return 1  # is-enabled → non-zero: not enabled

    def _host(*_args: Any, **_kw: Any) -> int | None:
        return 1

    def _property(_unit: str, _prop: str) -> str | None:
        return "Mon 2026-10-05" if _prop == "LastTriggerUSec" else None

    def _distrobox() -> tuple[bool, str | None, Path | None]:
        if in_box:
            return True, "ubuntu", Path("/var/home/abyss")
        return False, None, None

    monkeypatch.setattr(update_cmd_mod, "_systemctl", _systemctl)
    monkeypatch.setattr(update_cmd_mod, "_systemctl_host", _host)
    monkeypatch.setattr(update_cmd_mod, "_systemctl_property", _property)
    monkeypatch.setattr(update_cmd_mod, "_distrobox_context", _distrobox)
    monkeypatch.setattr(update_cmd_mod, "_unit_dir", lambda: Path("/tmp/no-such-units"))


def test_timer_status_box_legend_text(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _spy_timer_env(monkeypatch, in_box=True)
    result = runner.invoke(update_cmd_mod.timer_app, ["status"])
    assert result.exit_code == 0, result.output
    assert "host_state" in result.output
    assert "legend" in result.output
    # wrap-safe: rich may break the line anywhere, assert on short tokens
    assert "self-update timer" in result.output
    assert "cannot fire from the box" in result.output


def test_timer_status_box_legend_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _spy_timer_env(monkeypatch, in_box=True)
    result = runner.invoke(update_cmd_mod.timer_app, ["status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["host_state"] is not None
    assert payload["legend"]["state"].startswith("this box")
    assert "fires_when" in payload["legend"]


def test_timer_status_plain_host_has_no_legend(monkeypatch: pytest.MonkeyPatch) -> None:
    _spy_timer_env(monkeypatch, in_box=False)
    result = runner.invoke(update_cmd_mod.timer_app, ["status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "host_state" not in payload
    assert "legend" not in payload


# ── doctor Tag contract detail ────────────────────────────────────────────────


class _SettingsPathsStub:
    def __init__(self, db_path: Path, *, strict: bool = True) -> None:
        self.strict_tag_contract = strict
        self.db_path = db_path


class _DoctorSettingsStub:
    def __init__(self, db_path: Path) -> None:
        self.vesma = _SettingsPathsStub(db_path)
        self.db_path = db_path


def _make_db(tmp_path: Path, rows: list[tuple[str, str]]) -> Path:
    db = tmp_path / "doctor-tags.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, tags TEXT NOT NULL)")
    conn.executemany("INSERT INTO memories (id, tags) VALUES (?, ?)", rows)
    conn.commit()
    conn.close()
    return db


def test_doctor_tag_contract_singular_entry_with_example(tmp_path: Path) -> None:
    from vesma.cli.doctor import CheckStatus, _check_tag_contract

    db = _make_db(tmp_path, [("a" * 32, json.dumps(["vesma:checkpoint"]))])
    result = _check_tag_contract(_DoctorSettingsStub(db))
    assert result.status == CheckStatus.WARN
    assert "1 non-conformant entry " in result.detail  # singular — was "1 entries"
    assert "aaaaaaaa" in result.detail  # example id prefix
    assert "vesma tags validate" in result.detail  # listing hint


def test_doctor_tag_contract_plural_and_count(tmp_path: Path) -> None:
    from vesma.cli.doctor import _check_tag_contract

    rows = [(f"{i:032x}", json.dumps(["vesma:learning"])) for i in range(7)]
    db = _make_db(tmp_path, rows)
    result = _check_tag_contract(_DoctorSettingsStub(db))
    assert "7 non-conformant entries" in result.detail
    assert "(and 2 more)" in result.detail  # first 5 shown


def test_doctor_tag_contract_clean_pass(tmp_path: Path) -> None:
    from vesma.cli.doctor import CheckStatus, _check_tag_contract

    db = _make_db(
        tmp_path,
        [
            (
                "b" * 32,
                json.dumps(["project:vesma", "agent:gcw-senior-system-engineer", "vesma:learning"]),
            )
        ],
    )
    result = _check_tag_contract(_DoctorSettingsStub(db))
    assert result.status == CheckStatus.PASS
    assert "0 non-conformant entries" in result.detail


# ── agent-token list --json ───────────────────────────────────────────────────


def test_agent_token_list_json_full_jti(tmp_path: Path) -> None:
    from vesma.agent_tokens import issue_agent_token, load_or_create_signing_key
    from vesma.config import load_settings

    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"vesma:\n  data_dir: {tmp_path / 'data'}\n", encoding="utf-8")
    settings = load_settings(cfg)
    settings.resolve_paths()
    key = load_or_create_signing_key(settings.vesma.data_dir / "agent-token-signing.key")

    from vesma.agent_tokens import AgentTokenStore

    store = AgentTokenStore(settings.db_path)
    try:
        _, claims = issue_agent_token(
            agent_id="audit-agent",
            node_id="node-x",
            scope_spec="read",
            ttl_hours=1.0,
            store=store,
            key=key,
            issuer_id="test-issuer",
        )
    finally:
        store.close()

    result = runner.invoke(app, ["agent-token", "list", "--json", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    payload = [json.loads(line) for line in lines]
    assert len(payload) == 1
    assert payload[0]["jti"] == claims.jti  # FULL jti — the table truncates it
    assert payload[0]["agent"] == "audit-agent"
    assert payload[0]["status"] == "live"


def test_agent_token_list_table_hints_at_json(tmp_path: Path) -> None:
    """The table truncates jtis to fit; it must SAY where the full ids live."""
    from vesma.agent_tokens import AgentTokenStore, issue_agent_token, load_or_create_signing_key
    from vesma.config import load_settings

    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"vesma:\n  data_dir: {tmp_path / 'data'}\n", encoding="utf-8")
    settings = load_settings(cfg)
    settings.resolve_paths()
    key = load_or_create_signing_key(settings.vesma.data_dir / "agent-token-signing.key")
    store = AgentTokenStore(settings.db_path)
    try:
        issue_agent_token(
            agent_id="audit-agent",
            node_id="node-x",
            scope_spec="read",
            ttl_hours=1.0,
            store=store,
            key=key,
            issuer_id="test-issuer",
        )
    finally:
        store.close()
    result = runner.invoke(app, ["agent-token", "list", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "--json" in result.output


# ── graph delete gate refusal wording ─────────────────────────────────────────


def test_graph_gate_refusal_translated_to_cli_flags() -> None:
    """The service's ghost-gate refusal names REST/MCP kwargs; the CLI surface
    must spell the SAME gate as --force / --confirm-name (audit P2)."""
    from vesma.codegraph.service import GraphConfinementError

    original = (
        "project 'audit-fakeproj' is a GHOST (registered root missing on disk); deleting "
        "the registration requires the evidence gate — pass confirm=true and "
        "confirm_name='audit-fakeproj' (echo of the project name)"
    )
    exc = GraphConfinementError(original)
    rewritten = str(_to_cli_gate_syntax(exc))
    assert "confirm=true" not in rewritten
    assert "--force" in rewritten
    assert "--confirm-name 'audit-fakeproj'" in rewritten
    assert "(echo of the project name)" not in rewritten


def test_graph_non_gate_refusal_untouched() -> None:
    from vesma.codegraph.service import GraphConfinementError

    original = "project 'x' is not registered in the projects table (PG2)"
    assert str(_to_cli_gate_syntax(GraphConfinementError(original))) == original
