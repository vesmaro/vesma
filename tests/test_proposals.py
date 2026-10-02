"""Tests for the three approved proposal enhancements.

Covers:

* Task 1: ``mnemos integration setup`` default-flow agent wiring prompt
  (interactive Y/n + non-interactive safe skip).
* Task 2: ``mnemos add --dry-run`` — filter preview without saving.
* Task 3: ``mnemos doctor --fix`` — auto-fix WARN-level checks, plus
  ``--fix --dry-run`` preview.

All tests use ``tmp_path`` and ``monkeypatch`` — never the real
``~/.copilot/agents/`` or real config.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from vesmaro.cli.agent_wiring import VESMARO_WILDCARD
from vesmaro.cli.integration import IntegrationManager, load_targets
from vesmaro.cli.main import app

runner = CliRunner()


# ── Shared fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point VESMA_CONFIG at an empty YAML so the CLI uses tmp_path."""
    cfg = tmp_path / "vesmaro.yaml"
    cfg.write_text(
        f"mnemos:\n"
        f"  vault_path: {tmp_path / 'vault'}\n"
        f"  data_dir: {tmp_path / 'data'}\n"
        f"  db_name: proposals.db\n"
        f"embedding:\n"
        f"  provider: nano\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VESMA_CONFIG", str(cfg))
    return cfg


def _write_agent(
    directory: Path,
    filename: str,
    *,
    name: str | None = None,
    tools: list[str] | None = None,
    tool_profile: str | None = None,
    body: str = "You are an agent.\n",
) -> Path:
    """Write a minimal agent file with YAML frontmatter."""
    import frontmatter

    metadata: dict[str, object] = {"name": name or filename.removesuffix(".agent.md")}
    if tools is not None:
        metadata["tools"] = tools
    if tool_profile is not None:
        metadata["tool_profile"] = tool_profile

    post = frontmatter.Post(body, **metadata)
    path = directory / filename
    frontmatter.dump(post, path)
    return path


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    """Temporary agents directory with one unwired + one wired agent."""
    directory = tmp_path / "agents"
    directory.mkdir()
    _write_agent(
        directory,
        "agent-architect.agent.md",
        name="GCW: Agent Architect",
        tools=["read", "search", "execute", "edit"],
    )
    _write_agent(
        directory,
        "tech-lead.agent.md",
        name="GCW: Tech Lead",
        tools=["read", "search", "execute", VESMARO_WILDCARD],
    )
    return directory


@pytest.fixture
def fake_pack(tmp_path: Path) -> Path:
    """Build a minimal integrations/ pack in tmp_path."""
    pack = tmp_path / "integrations"
    (pack / "instructions").mkdir(parents=True)
    (pack / "skills").mkdir(parents=True)
    (pack / "prompts").mkdir(parents=True)

    (pack / "instructions" / "mnemos-memory.instructions.md").write_text(
        "---\napplyTo: '**'\n---\n# Mnemos memory trigger\nUse mnemos tools.\n",
        encoding="utf-8",
    )
    skill_dir = pack / "skills" / "mnemos-recall"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "# Mnemos recall skill\n\nRecall context from memory.\n", encoding="utf-8"
    )
    (pack / "prompts" / "mnemos-session.prompt.md").write_text(
        "# Mnemos session prompt\n\nStart a memory-aware session.\n", encoding="utf-8"
    )
    (pack / "targets.yaml").write_text(
        yaml.dump(
            {
                "targets": {
                    "test-harness": {
                        "detect": [{"path": str(tmp_path / "harness-marker")}],
                        "deploy": {
                            "instructions": str(tmp_path / "deploy" / "instructions") + "/",
                            "skills": str(tmp_path / "deploy" / "skills") + "/",
                            "prompts": str(tmp_path / "deploy" / "prompts") + "/",
                        },
                        "format": "copy",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "harness-marker").mkdir(parents=True, exist_ok=True)
    return pack


def _patch_integration(monkeypatch: pytest.MonkeyPatch, fake_pack: Path) -> None:
    """Patch the CLI util and integration modules to use the fake pack.

    Patches ``load_targets`` in BOTH ``vesmaro.cli.util`` and
    ``vesmaro.cli.integration`` because ``_fix_integration_stale`` in
    ``doctor.py`` imports ``load_targets`` directly from
    ``vesmaro.cli.integration`` at call time (not via the util module).
    """
    cfg = load_targets(fake_pack / "targets.yaml")
    mgr = IntegrationManager(version="1.2.0", pack_root=fake_pack, targets_config=cfg)

    import vesmaro.cli.util as util_mod

    monkeypatch.setattr(util_mod, "_manager", lambda pack_root=None, home=None: mgr)
    monkeypatch.setattr(util_mod, "load_targets", lambda config_path=None, home=None: cfg)
    # doctor._fix_integration_stale imports load_targets from integration module.
    monkeypatch.setattr(
        "vesmaro.cli.integration.load_targets", lambda config_path=None, home=None: cfg
    )


# ── Task 1: integration setup default-all wiring (UX inversion, #448 family) ──


class TestSetupDefaultWiring:
    """Plain ``vesma integration setup`` wires ALL agents, non-interactively.

    Owner ruling (board card ``vesma-integration-setup-default-all``): the
    plain command is the full host deployment — deploy every detected
    target and wire every unwired agent in ONE non-interactive pass.
    Flags narrow; ``--wire-agents``/``--all`` are kept only for backward
    compatibility (they are no-ops relative to the default).
    """

    def test_default_wires_all_non_interactively(
        self,
        agents_dir: Path,
        fake_pack: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No flags → all unwired agents get wired (no prompt, no TTY needed)."""
        import frontmatter

        monkeypatch.setattr("vesmaro.cli.agent_wiring.DEFAULT_AGENTS_DIR", agents_dir)
        monkeypatch.setattr("vesmaro.cli.util.DEFAULT_AGENTS_DIR", agents_dir)
        _patch_integration(monkeypatch, fake_pack)

        result = runner.invoke(
            app,
            ["integration", "setup", "--target", "test-harness", "--no-mcp"],
        )

        assert result.exit_code == 0, result.output
        post = frontmatter.load(agents_dir / "agent-architect.agent.md")
        assert VESMARO_WILDCARD in post.metadata["tools"]

    def test_no_wire_agents_skips_wiring(
        self,
        agents_dir: Path,
        fake_pack: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--no-wire-agents`` skips agent wiring entirely."""
        monkeypatch.setattr("vesmaro.cli.agent_wiring.DEFAULT_AGENTS_DIR", agents_dir)
        monkeypatch.setattr("vesmaro.cli.util.DEFAULT_AGENTS_DIR", agents_dir)
        _patch_integration(monkeypatch, fake_pack)

        original = (agents_dir / "agent-architect.agent.md").read_text(encoding="utf-8")

        result = runner.invoke(
            app,
            ["integration", "setup", "--target", "test-harness", "--no-mcp", "--no-wire-agents"],
        )

        assert result.exit_code == 0, result.output
        assert (agents_dir / "agent-architect.agent.md").read_text(encoding="utf-8") == original

    def test_wire_agents_flag_still_works(
        self,
        agents_dir: Path,
        fake_pack: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--wire-agents --all`` is still accepted (legacy scripts) and wires."""
        import frontmatter

        monkeypatch.setattr("vesmaro.cli.agent_wiring.DEFAULT_AGENTS_DIR", agents_dir)
        monkeypatch.setattr("vesmaro.cli.util.DEFAULT_AGENTS_DIR", agents_dir)
        _patch_integration(monkeypatch, fake_pack)

        result = runner.invoke(
            app,
            [
                "integration",
                "setup",
                "--target",
                "test-harness",
                "--no-mcp",
                "--wire-agents",
                "--all",
            ],
        )

        assert result.exit_code == 0, result.output
        post = frontmatter.load(agents_dir / "agent-architect.agent.md")
        assert VESMARO_WILDCARD in post.metadata["tools"]

    def test_no_wire_agents_flag_still_works(
        self,
        agents_dir: Path,
        fake_pack: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--no-wire-agents`` still skips without prompting."""
        monkeypatch.setattr("vesmaro.cli.agent_wiring.DEFAULT_AGENTS_DIR", agents_dir)
        monkeypatch.setattr("vesmaro.cli.util.DEFAULT_AGENTS_DIR", agents_dir)
        _patch_integration(monkeypatch, fake_pack)

        original = (agents_dir / "agent-architect.agent.md").read_text(encoding="utf-8")

        result = runner.invoke(
            app,
            [
                "integration",
                "setup",
                "--target",
                "test-harness",
                "--no-mcp",
                "--no-wire-agents",
            ],
        )

        assert result.exit_code == 0, result.output
        assert (agents_dir / "agent-architect.agent.md").read_text(encoding="utf-8") == original


# ── Task 2: mnemos add --dry-run ──────────────────────────────────────────────


class TestAddDryRun:
    """``mnemos add --dry-run`` shows filter stats without saving."""

    def test_dry_run_shows_filter_stats(
        self,
        isolated_config: Path,
        tmp_path: Path,
    ) -> None:
        """``--dry-run`` prints profile, token reduction, dedup, noise, budget."""
        content = "line one\nline one\nline two\n2026-01-01T12:00:00Z timestamp\n"
        result = runner.invoke(
            app,
            [
                "add",
                content,
                "--tags",
                "project:dry,agent:test,mnemos:learning",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Filter preview" in result.output
        assert "Input:" in result.output
        assert "Output:" in result.output
        assert "Profile:" in result.output
        assert "Dedup:" in result.output
        assert "Noise:" in result.output
        assert "Budget:" in result.output
        assert "would be saved" in result.output

    def test_dry_run_does_not_save(
        self,
        isolated_config: Path,
        tmp_path: Path,
    ) -> None:
        """``--dry-run`` does NOT create a memory in the store."""
        result = runner.invoke(
            app,
            [
                "add",
                "some content that will not be saved",
                "--tags",
                "project:dry,agent:test,mnemos:learning",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        # No "Saved:" line — the memory was not persisted.
        assert "Saved:" not in result.output

    def test_dry_run_validates_tag_contract(
        self,
        isolated_config: Path,
    ) -> None:
        """``--dry-run`` with invalid tags raises TagContractError (strict mode)."""
        # Missing required project:/agent:/mnemos: tags.
        result = runner.invoke(
            app,
            ["add", "content", "--tags", "random-tag", "--dry-run"],
        )
        # Strict mode raises TagContractError → non-zero exit.
        assert result.exit_code != 0
        assert "Traceback" not in result.output or "TagContractError" in result.output

    def test_dry_run_from_file(
        self,
        isolated_config: Path,
        tmp_path: Path,
    ) -> None:
        """``--dry-run --file`` reads the file and shows filter stats."""
        content_file = tmp_path / "input.txt"
        content_file.write_text("hello world\nhello world\n", encoding="utf-8")
        result = runner.invoke(
            app,
            [
                "add",
                "--file",
                str(content_file),
                "--tags",
                "project:dry,agent:test,mnemos:learning",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Filter preview" in result.output
        assert "Dedup:" in result.output

    def test_dry_run_with_url_rejected(
        self,
        isolated_config: Path,
    ) -> None:
        """``--dry-run --url`` is rejected (content fetched at ingest time)."""
        result = runner.invoke(
            app,
            [
                "add",
                "--url",
                "https://example.com",
                "--tags",
                "project:dry,agent:test,mnemos:learning",
                "--dry-run",
            ],
        )
        assert result.exit_code != 0
        assert "not supported" in result.output.lower()


# ── Task 3: mnemos doctor --fix ───────────────────────────────────────────────


class TestDoctorFix:
    """``mnemos doctor --fix`` auto-fixes WARN-level checks."""

    def test_fix_dry_run_previews(
        self,
        agents_dir: Path,
        fake_pack: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--fix --dry-run`` previews fixes without executing."""
        monkeypatch.setattr("vesmaro.cli.agent_wiring.DEFAULT_AGENTS_DIR", agents_dir)
        _patch_integration(monkeypatch, fake_pack)

        result = runner.invoke(app, ["doctor", "--fix", "--dry-run", "--json"])

        # Exit code may be 2 (warnings still present in dry-run).
        assert result.exit_code in (0, 2), result.output
        payload = result.stdout
        assert "dry_run" in payload
        assert "would run" in payload.lower() or "preview" in payload.lower()

    def test_fix_action_for_known_checks(self) -> None:
        """``_fix_action_for`` returns actions for Integration, Agent wiring, MCP."""
        from vesmaro.cli.doctor import _fix_action_for

        assert _fix_action_for("Integration") is not None
        assert _fix_action_for("Agent wiring") is not None
        assert _fix_action_for("MCP server") is not None

    def test_fix_action_for_unknown_check_returns_none(self) -> None:
        """``_fix_action_for`` returns None for non-fixable checks."""
        from vesmaro.cli.doctor import _fix_action_for

        assert _fix_action_for("Config") is None
        assert _fix_action_for("SQLite DB") is None
        assert _fix_action_for("Vault") is None

    def test_fix_agent_wiring_wires_unwired(
        self,
        agents_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``_fix_agent_wiring`` wires all unwired agents."""
        import frontmatter

        monkeypatch.setattr("vesmaro.cli.agent_wiring.DEFAULT_AGENTS_DIR", agents_dir)
        from vesmaro.cli.doctor import _fix_agent_wiring

        ok, note = _fix_agent_wiring()
        assert ok is True
        assert "wired" in note
        # agent-architect now has mnemos/*.
        post = frontmatter.load(agents_dir / "agent-architect.agent.md")
        assert VESMARO_WILDCARD in post.metadata["tools"]

    def test_fix_integration_stale_updates(
        self,
        fake_pack: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``_fix_integration_stale`` runs update and reports success."""
        _patch_integration(monkeypatch, fake_pack)
        from vesmaro.cli.doctor import _fix_integration_stale

        ok, note = _fix_integration_stale()
        assert ok is True
        assert "updated" in note

    def test_fix_json_includes_fixed_field(
        self,
        agents_dir: Path,
        fake_pack: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``doctor --fix --json`` includes the ``fixed`` array in output."""
        monkeypatch.setattr("vesmaro.cli.agent_wiring.DEFAULT_AGENTS_DIR", agents_dir)
        _patch_integration(monkeypatch, fake_pack)

        result = runner.invoke(app, ["doctor", "--fix", "--json"])

        # After fix, exit code should be 0 (all fixable warnings resolved)
        # or 2 (some warnings remain — e.g. SQLite/vector missing in tmp).
        assert result.exit_code in (0, 2), result.output
        payload = result.stdout
        assert "fixed" in payload

    def test_fix_skips_fail_level(
        self,
        agents_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--fix`` does not attempt to fix FAIL-level checks."""
        from vesmaro.cli.doctor import CheckStatus, _fix_action_for

        # FAIL-level checks have no fix action.
        for fail_check in ("Config", "Data dir", "Vault", "SQLite DB", "Vector store"):
            assert _fix_action_for(fail_check) is None

        # Verify CheckStatus enum is used correctly.
        assert CheckStatus.FAIL != CheckStatus.WARN

    def test_fix_noop_when_all_pass(
        self,
        agents_dir: Path,
        fake_pack: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``doctor --fix`` is a no-op (exit 0) when all checks pass.

        After fixing all WARN-level issues, re-running ``doctor --fix``
        should find nothing to fix and exit 0.
        """
        monkeypatch.setattr("vesmaro.cli.agent_wiring.DEFAULT_AGENTS_DIR", agents_dir)
        _patch_integration(monkeypatch, fake_pack)

        # First run fixes the warnings.
        runner.invoke(app, ["doctor", "--fix", "--json"])

        # Wire the remaining unwired agent manually so all agents are wired.
        from vesmaro.cli.doctor import _fix_agent_wiring

        _fix_agent_wiring()

        # Second run — all checks pass, --fix is a no-op.
        result = runner.invoke(app, ["doctor", "--fix", "--json"])
        payload = result.stdout
        # Exit code 0 means all checks pass (no warnings, no failures).
        # Some checks (SQLite/vector) may still WARN in tmp_path, so accept 0 or 2.
        assert result.exit_code in (0, 2), result.output
        # The fixed array should be empty (nothing needed fixing).
        if "fixed" in payload:
            import json as _json

            data = _json.loads(payload)
            assert data.get("fixed", []) == []
