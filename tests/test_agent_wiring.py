"""Tests for agent MCP wiring — ``mnemos/cli/agent_wiring.py``.

Covers:

* ``detect_agents`` — finds all ``*.agent.md`` files, parses frontmatter.
* ``wire_agent`` wildcard mode — adds ``mnemos/*`` to ``tools`` array.
* ``wire_agent`` precise mode — adds individual ``mnemos/mnemos_*`` tokens.
* ``wire_agent`` idempotency — re-running does not duplicate.
* ``wire_agent`` already-wired — skips with correct status.
* ``wire_agent`` tool_profile — skips with reason.
* ``wire_agent`` no ``tools`` field — creates it.
* ``wire_agent`` dry-run — does not modify the file.
* ``verify_agents`` — aggregate wiring summary.
* CLI ``integration setup --wire-agents --all`` — wires all unwired agents.
* CLI ``integration setup --wire-agents --select`` — wires only specified.
* CLI ``integration setup --no-wire-agents`` — skips wiring.
* CLI ``integration verify`` — shows agents section.
* ``doctor`` — agent wiring check (PASS/WARN/SKIP).

All tests use ``tmp_path`` — never the real ``~/.copilot/agents/``.
"""

from __future__ import annotations

import json
from pathlib import Path

import frontmatter
import pytest
from typer.testing import CliRunner

from vesma.cli.agent_wiring import (
    VESMARO_TOOLS,
    VESMARO_WILDCARD,
    WireStatus,
    detect_agents,
    verify_agents,
    wire_agent,
)
from vesma.cli.integration import Target, TargetsConfig
from vesma.cli.main import app

runner = CliRunner()


# ── Fixtures ───────────────────────────────────────────────────────────────────


def _write_agent(
    directory: Path,
    filename: str,
    *,
    name: str | None = None,
    tools: list[str] | None = None,
    tool_profile: str | None = None,
    body: str = "You are an agent.\n",
) -> Path:
    """Write a minimal agent file with YAML frontmatter.

    Args:
        directory: Where to write the file.
        filename: Filename (must end with ``.agent.md``).
        name: Agent ``name`` field (defaults to filename stem).
        tools: ``tools`` list. ``None`` omits the field entirely.
        tool_profile: ``tool_profile`` string. ``None`` omits the field.
        body: Markdown body after the frontmatter.

    Returns:
        Path to the written file.
    """
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
    """Create a temporary agents directory with sample agent files."""
    directory = tmp_path / "agents"
    directory.mkdir()

    # Agent with tools, no memory tools — needs wiring.
    _write_agent(
        directory,
        "agent-architect.agent.md",
        name="GCW: Agent Architect",
        tools=["read", "search", "execute", "edit"],
    )

    # Agent already wired with a wildcard tool token.
    _write_agent(
        directory,
        "tech-lead.agent.md",
        name="GCW: Tech Lead",
        tools=["read", "search", "execute", VESMARO_WILDCARD],
    )

    # Agent with tool_profile — should be skipped.
    _write_agent(
        directory,
        "cr-critic.agent.md",
        name="GCW: CR Critic",
        tool_profile="worker-readonly",
    )

    # Agent with no tools field at all — wiring should create it.
    _write_agent(
        directory,
        "concierge.agent.md",
        name="GCW: Chief of Staff",
        tools=None,
    )

    # Agent already wired with precise mnemos tools.
    _write_agent(
        directory,
        "mnemos-curator.agent.md",
        name="GCW: Mnemos Curator",
        tools=["read", "search", VESMARO_TOOLS[0], VESMARO_TOOLS[1]],
    )

    return directory


@pytest.fixture
def _isolate_copilot_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Monkeypatch load_targets so the copilot target is detected under tmp_path.

    Without this, ``_resolve_targets("copilot")`` checks the real
    ``~/.copilot/instructions`` and ``~/.copilot/skills`` paths, which do
    not exist on CI runners — causing the CLI to exit early before reaching
    agent wiring. This fixture creates the detect/deploy dirs under
    ``tmp_path`` and patches ``load_targets`` in both ``vesma.cli.util``
    and ``vesma.cli.integration`` so the copilot target is always "detected".
    """
    instructions_dir = tmp_path / "copilot" / "instructions"
    skills_dir = tmp_path / "copilot" / "skills"
    instructions_dir.mkdir(parents=True)
    skills_dir.mkdir(parents=True)

    copilot_target = Target(
        name="copilot",
        detect_paths=(instructions_dir, skills_dir),
        deploy_map={"instructions": instructions_dir, "skills": skills_dir},
        format="copy",
    )
    config = TargetsConfig(targets=(copilot_target,))
    monkeypatch.setattr("vesma.cli.util.load_targets", lambda config_path=None, home=None: config)
    monkeypatch.setattr(
        "vesma.cli.integration.load_targets", lambda config_path=None, home=None: config
    )


# ── detect_agents ─────────────────────────────────────────────────────────────


class TestDetectAgents:
    """Tests for ``detect_agents``."""

    def test_finds_all_agent_files(self, agents_dir: Path) -> None:
        """All ``*.agent.md`` files are detected."""
        infos = detect_agents(agents_dir)
        assert len(infos) == 5

    def test_parses_names(self, agents_dir: Path) -> None:
        """Agent names are parsed from frontmatter."""
        infos = detect_agents(agents_dir)
        names = {info.name for info in infos}
        assert "GCW: Agent Architect" in names
        assert "GCW: Tech Lead" in names
        assert "GCW: CR Critic" in names

    def test_detects_mnemos_presence(self, agents_dir: Path) -> None:
        """``has_mnemos`` is True for agents with mnemos tools."""
        infos = detect_agents(agents_dir)
        by_name = {info.name: info for info in infos}

        assert by_name["GCW: Tech Lead"].has_mnemos is True
        assert by_name["GCW: Mnemos Curator"].has_mnemos is True
        assert by_name["GCW: Agent Architect"].has_mnemos is False

    def test_detects_tool_profile(self, agents_dir: Path) -> None:
        """``uses_tool_profile`` is True for tool_profile agents."""
        infos = detect_agents(agents_dir)
        by_name = {info.name: info for info in infos}

        assert by_name["GCW: CR Critic"].uses_tool_profile is True
        assert by_name["GCW: Agent Architect"].uses_tool_profile is False

    def test_tools_count(self, agents_dir: Path) -> None:
        """``tools_count`` reflects the number of tools."""
        infos = detect_agents(agents_dir)
        by_name = {info.name: info for info in infos}

        assert by_name["GCW: Agent Architect"].tools_count == 4
        assert by_name["GCW: Chief of Staff"].tools_count == 0
        assert by_name["GCW: Chief of Staff"].has_tools is False

    def test_empty_directory(self, tmp_path: Path) -> None:
        """An empty directory returns an empty list."""
        empty = tmp_path / "empty"
        empty.mkdir()
        assert detect_agents(empty) == []

    def test_nonexistent_directory(self, tmp_path: Path) -> None:
        """A non-existent directory returns an empty list (no crash)."""
        assert detect_agents(tmp_path / "does-not-exist") == []

    def test_malformed_frontmatter_does_not_crash(self, tmp_path: Path) -> None:
        """A file with broken frontmatter is reported, not crashed on.

        The YAML preprocessing + regex fallback extract ``name`` even from
        malformed flow sequences, so the agent is reported with the parsed
        name (not the filename fallback). The key invariant: no crash, and
        the agent appears in the report with safe defaults (no tools).
        """
        directory = tmp_path / "agents"
        directory.mkdir()
        bad = directory / "broken.agent.md"
        bad.write_text("---\nname: [invalid\n  yaml: {{{\n---\nbody\n", encoding="utf-8")

        infos = detect_agents(directory)
        assert len(infos) == 1
        # Regex fallback extracts the scalar ``name`` value. The important
        # assertion is that we did not crash and the agent has no tools.
        assert infos[0].name == "[invalid"
        assert infos[0].has_tools is False

    def test_unquoted_colon_in_description_does_not_crash(self, tmp_path: Path) -> None:
        """Regression: agent files with unquoted ``:`` in description values.

        Real-world agent files like ``mnemos-curator.agent.md`` have:
            description: (Copilot) Curator ... STUB mode: operates on ...

        The ``:`` in "STUB mode:" confuses the YAML parser (it looks like a
        nested mapping). The frontmatter library raises ScannerError. We
        must catch this and skip the agent gracefully — not crash the whole
        wiring batch.
        """
        directory = tmp_path / "agents"
        directory.mkdir()
        path = directory / "curator.agent.md"
        # Mimic the real mnemos-curator.agent.md frontmatter structure.
        path.write_text(
            "---\n"
            'name: "GCW: Mnemos Curator"\n'
            "description: (Copilot) Curator commands for memory hygiene — "
            "curate/rollup/audit. STUB mode: operates on file-based memory.\n"
            "---\n"
            "You are a curator.\n",
            encoding="utf-8",
        )

        # detect_agents must not crash. The preprocessing fixes the
        # unquoted colon, so the parser succeeds and returns the real name.
        infos = detect_agents(directory)
        assert len(infos) == 1
        assert infos[0].name == "GCW: Mnemos Curator"
        assert infos[0].has_tools is False

        # wire_agent must not crash. The frontmatter parses successfully
        # (preprocessing quotes the description), so wiring proceeds and
        # adds the memory-tool wildcard token.
        result = wire_agent(path, mode="wildcard")
        assert result.status == WireStatus.WIRED
        assert result.tools_added == ["mnemos/*"]


# ── wire_agent ────────────────────────────────────────────────────────────────


class TestWireAgent:
    """Tests for ``wire_agent``."""

    def test_wildcard_adds_mnemos_slash_star(self, agents_dir: Path) -> None:
        """Wildcard mode adds ``mnemos/*`` to the tools array."""
        path = agents_dir / "agent-architect.agent.md"
        result = wire_agent(path, mode="wildcard")

        assert result.status == WireStatus.WIRED
        assert VESMARO_WILDCARD in result.tools_added

        post = frontmatter.load(path)
        tools = post.metadata["tools"]
        assert VESMARO_WILDCARD in tools
        # Original tools preserved.
        assert "read" in tools
        assert "search" in tools

    def test_precise_adds_individual_tools(self, agents_dir: Path) -> None:
        """Precise mode adds individual ``mnemos/mnemos_*`` tokens."""
        path = agents_dir / "agent-architect.agent.md"
        result = wire_agent(path, mode="precise")

        assert result.status == WireStatus.WIRED
        assert len(result.tools_added) == len(VESMARO_TOOLS)

        post = frontmatter.load(path)
        tools = post.metadata["tools"]
        for tool in VESMARO_TOOLS:
            assert tool in tools

    def test_idempotent_wildcard(self, agents_dir: Path) -> None:
        """Re-running wildcard mode does not duplicate the entry."""
        path = agents_dir / "agent-architect.agent.md"
        wire_agent(path, mode="wildcard")
        result = wire_agent(path, mode="wildcard")

        assert result.status == WireStatus.ALREADY_WIRED
        assert result.tools_added == []

        post = frontmatter.load(path)
        tools = post.metadata["tools"]
        assert tools.count(VESMARO_WILDCARD) == 1

    def test_idempotent_precise(self, agents_dir: Path) -> None:
        """Re-running precise mode does not duplicate tokens."""
        path = agents_dir / "agent-architect.agent.md"
        wire_agent(path, mode="precise")
        result = wire_agent(path, mode="precise")

        assert result.status == WireStatus.ALREADY_WIRED
        assert result.tools_added == []

        post = frontmatter.load(path)
        tools = post.metadata["tools"]
        for tool in VESMARO_TOOLS:
            assert tools.count(tool) == 1

    def test_already_wired_wildcard(self, agents_dir: Path) -> None:
        """An agent already wired with wildcard is skipped."""
        path = agents_dir / "tech-lead.agent.md"
        result = wire_agent(path, mode="wildcard")

        assert result.status == WireStatus.ALREADY_WIRED
        assert result.tools_added == []

    def test_already_wired_precise_when_wildcard_present(self, agents_dir: Path) -> None:
        """Precise mode on a wildcard-wired agent adds the individual tokens.

        The wildcard ``mnemos/*`` is not the same string as ``mnemos/vesma_add``,
        so precise mode adds the individual tokens. This is correct behaviour —
        the user explicitly asked for precise mode.
        """
        path = agents_dir / "tech-lead.agent.md"
        result = wire_agent(path, mode="precise")

        # The individual tokens were missing (only wildcard was present).
        assert result.status == WireStatus.WIRED
        assert len(result.tools_added) == len(VESMARO_TOOLS)

    def test_skips_tool_profile(self, agents_dir: Path) -> None:
        """Agents with ``tool_profile`` are skipped, not modified."""
        path = agents_dir / "cr-critic.agent.md"
        result = wire_agent(path, mode="wildcard")

        assert result.status == WireStatus.SKIPPED_TOOL_PROFILE
        assert "tool_profile" in result.note

        # File unchanged — no tools field added.
        post = frontmatter.load(path)
        assert "tools" not in post.metadata

    def test_creates_tools_field_when_missing(self, agents_dir: Path) -> None:
        """When ``tools`` is absent, wiring creates it."""
        path = agents_dir / "concierge.agent.md"
        result = wire_agent(path, mode="wildcard")

        assert result.status == WireStatus.WIRED

        post = frontmatter.load(path)
        tools = post.metadata["tools"]
        assert isinstance(tools, list)
        assert VESMARO_WILDCARD in tools

    def test_dry_run_does_not_modify(self, agents_dir: Path) -> None:
        """``--dry-run`` reports the change without writing."""
        path = agents_dir / "agent-architect.agent.md"
        original = path.read_text(encoding="utf-8")

        result = wire_agent(path, mode="wildcard", dry_run=True)

        assert result.status == WireStatus.DRY_RUN
        assert VESMARO_WILDCARD in result.tools_added
        # File untouched.
        assert path.read_text(encoding="utf-8") == original

    def test_preserves_other_frontmatter_fields(self, agents_dir: Path) -> None:
        """Wiring does not mangle other frontmatter keys."""
        path = agents_dir / "agent-architect.agent.md"
        wire_agent(path, mode="wildcard")

        post = frontmatter.load(path)
        assert post.metadata["name"] == "GCW: Agent Architect"
        # Body preserved.
        assert "You are an agent" in post.content

    def test_invalid_mode_raises(self, agents_dir: Path) -> None:
        """An unknown mode raises ``ValueError``."""
        path = agents_dir / "agent-architect.agent.md"
        with pytest.raises(ValueError, match="Unknown wiring mode"):
            wire_agent(path, mode="bogus")  # type: ignore[arg-type]

    def test_error_on_unparseable_file(self, tmp_path: Path) -> None:
        """A file with no frontmatter delimiters is skipped gracefully.

        Content without ``---`` delimiters cannot have frontmatter, so both
        the YAML parser and the regex fallback return an empty dict. The
        agent is reported as SKIPPED_NO_FRONTMATTER so the wiring batch
        continues with other agents.
        """
        path = tmp_path / "broken.agent.md"
        # No ``---`` frontmatter delimiters at all — truly unparseable.
        path.write_text(
            "This file has no frontmatter delimiters.\nJust plain markdown body text.\n",
            encoding="utf-8",
        )

        result = wire_agent(path, mode="wildcard")
        assert result.status == WireStatus.SKIPPED_NO_FRONTMATTER
        assert "frontmatter parse failed" in result.note


# ── verify_agents ─────────────────────────────────────────────────────────────


class TestVerifyAgents:
    """Tests for ``verify_agents`` aggregate summary."""

    def test_summary_counts(self, agents_dir: Path) -> None:
        """The summary correctly categorises agents."""
        summary = verify_agents(agents_dir)

        assert summary.total == 5
        # tech-lead (wildcard) + curator (precise) = 2 wired.
        assert summary.wired == 2
        # cr-critic uses tool_profile.
        assert summary.skipped_tool_profile == 1
        # agent-architect + concierge = 2 unwired.
        assert summary.unwired == 2
        assert "GCW: Agent Architect" in summary.unwired_names
        assert "GCW: Chief of Staff" in summary.unwired_names
        assert summary.errors == 0

    def test_all_wired_false_when_unwired(self, agents_dir: Path) -> None:
        """``all_wired`` is False when there are unwired agents."""
        summary = verify_agents(agents_dir)
        assert summary.all_wired is False

    def test_all_wired_true_when_complete(self, tmp_path: Path) -> None:
        """``all_wired`` is True when every agent is wired or skipped."""
        directory = tmp_path / "agents"
        directory.mkdir()
        _write_agent(
            directory,
            "a.agent.md",
            tools=["read", VESMARO_WILDCARD],
        )
        _write_agent(
            directory,
            "b.agent.md",
            tool_profile="worker-readonly",
        )

        summary = verify_agents(directory)
        assert summary.total == 2
        assert summary.unwired == 0
        assert summary.all_wired is True

    def test_empty_directory(self, tmp_path: Path) -> None:
        """An empty agents directory yields a zero-total summary."""
        empty = tmp_path / "empty"
        empty.mkdir()
        summary = verify_agents(empty)
        assert summary.total == 0
        assert summary.all_wired is False


# ── CLI: integration setup --wire-agents ──────────────────────────────────────


class TestCliSetupWireAgents:
    """CLI tests for ``mnemos integration setup --wire-agents``.

    These tests use ``monkeypatch`` to redirect ``DEFAULT_AGENTS_DIR`` to a
    ``tmp_path`` directory so the real ``~/.copilot/agents/`` is never touched.
    """

    def test_wire_all_wires_unwired_agents(
        self,
        agents_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        _isolate_copilot_target: None,
    ) -> None:
        """``--wire-agents --all`` wires all unwired agents."""
        monkeypatch.setattr("vesma.cli.agent_wiring.DEFAULT_AGENTS_DIR", agents_dir)
        monkeypatch.setattr("vesma.cli.util.DEFAULT_AGENTS_DIR", agents_dir)

        result = runner.invoke(
            app,
            [
                "integration",
                "setup",
                "--target",
                "copilot",
                "--no-mcp",
                "--wire-agents",
                "--all",
            ],
        )

        assert result.exit_code == 0, result.output

        # agent-architect should now have mnemos/*.
        post = frontmatter.load(agents_dir / "agent-architect.agent.md")
        assert VESMARO_WILDCARD in post.metadata["tools"]

        # concierge (no tools) should now have tools with mnemos/*.
        post = frontmatter.load(agents_dir / "concierge.agent.md")
        assert VESMARO_WILDCARD in post.metadata["tools"]

        # cr-critic (tool_profile) should NOT have tools added.
        post = frontmatter.load(agents_dir / "cr-critic.agent.md")
        assert "tools" not in post.metadata

    def test_wire_select_wires_only_specified(
        self,
        agents_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        _isolate_copilot_target: None,
    ) -> None:
        """``--wire-agents --select name`` wires only the specified agent."""
        monkeypatch.setattr("vesma.cli.agent_wiring.DEFAULT_AGENTS_DIR", agents_dir)
        monkeypatch.setattr("vesma.cli.util.DEFAULT_AGENTS_DIR", agents_dir)

        result = runner.invoke(
            app,
            [
                "integration",
                "setup",
                "--target",
                "copilot",
                "--no-mcp",
                "--wire-agents",
                "--select",
                "agent-architect",
            ],
        )

        assert result.exit_code == 0, result.output

        # agent-architect wired.
        post = frontmatter.load(agents_dir / "agent-architect.agent.md")
        assert VESMARO_WILDCARD in post.metadata["tools"]

        # concierge NOT wired (was not selected).
        post = frontmatter.load(agents_dir / "concierge.agent.md")
        assert "tools" not in post.metadata

    def test_no_wire_agents_skips_wiring(
        self,
        agents_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        _isolate_copilot_target: None,
    ) -> None:
        """``--no-wire-agents`` skips agent wiring entirely."""
        monkeypatch.setattr("vesma.cli.agent_wiring.DEFAULT_AGENTS_DIR", agents_dir)
        monkeypatch.setattr("vesma.cli.util.DEFAULT_AGENTS_DIR", agents_dir)

        original_architect = (agents_dir / "agent-architect.agent.md").read_text(encoding="utf-8")

        result = runner.invoke(
            app,
            [
                "integration",
                "setup",
                "--target",
                "copilot",
                "--no-mcp",
                "--no-wire-agents",
            ],
        )

        assert result.exit_code == 0, result.output
        # No agent files modified.
        assert (agents_dir / "agent-architect.agent.md").read_text(
            encoding="utf-8"
        ) == original_architect

    def test_wire_agents_precise_mode(
        self,
        agents_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        _isolate_copilot_target: None,
    ) -> None:
        """``--wire-agents --all --precise`` uses individual tool names."""
        monkeypatch.setattr("vesma.cli.agent_wiring.DEFAULT_AGENTS_DIR", agents_dir)
        monkeypatch.setattr("vesma.cli.util.DEFAULT_AGENTS_DIR", agents_dir)

        result = runner.invoke(
            app,
            [
                "integration",
                "setup",
                "--target",
                "copilot",
                "--no-mcp",
                "--wire-agents",
                "--all",
                "--precise",
            ],
        )

        assert result.exit_code == 0, result.output

        post = frontmatter.load(agents_dir / "agent-architect.agent.md")
        tools = post.metadata["tools"]
        for tool in VESMARO_TOOLS:
            assert tool in tools
        # Wildcard should NOT be present in precise mode.
        assert VESMARO_WILDCARD not in tools

    def test_wire_agents_dry_run(
        self,
        agents_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        _isolate_copilot_target: None,
    ) -> None:
        """``--wire-agents --all --dry-run`` does not modify files."""
        monkeypatch.setattr("vesma.cli.agent_wiring.DEFAULT_AGENTS_DIR", agents_dir)
        monkeypatch.setattr("vesma.cli.util.DEFAULT_AGENTS_DIR", agents_dir)

        original = (agents_dir / "agent-architect.agent.md").read_text(encoding="utf-8")

        result = runner.invoke(
            app,
            [
                "integration",
                "setup",
                "--target",
                "copilot",
                "--no-mcp",
                "--wire-agents",
                "--all",
                "--dry-run",
            ],
        )

        assert result.exit_code == 0, result.output
        assert (agents_dir / "agent-architect.agent.md").read_text(encoding="utf-8") == original

    def test_mutually_exclusive_flags(
        self,
        agents_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        _isolate_copilot_target: None,
    ) -> None:
        """``--wire-agents`` and ``--no-wire-agents`` are mutually exclusive."""
        monkeypatch.setattr("vesma.cli.agent_wiring.DEFAULT_AGENTS_DIR", agents_dir)
        monkeypatch.setattr("vesma.cli.util.DEFAULT_AGENTS_DIR", agents_dir)

        result = runner.invoke(
            app,
            [
                "integration",
                "setup",
                "--target",
                "copilot",
                "--no-mcp",
                "--wire-agents",
                "--no-wire-agents",
            ],
        )

        assert result.exit_code == 1
        assert "mutually exclusive" in result.output


# ── CLI: integration verify — agents section ──────────────────────────────────


class TestCliVerifyAgentsSection:
    """CLI tests for the agents section in ``integration verify``."""

    def test_verify_shows_agents_section(
        self,
        agents_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        _isolate_copilot_target: None,
    ) -> None:
        """``integration verify`` prints an agents wiring summary."""
        monkeypatch.setattr("vesma.cli.agent_wiring.DEFAULT_AGENTS_DIR", agents_dir)
        monkeypatch.setattr("vesma.cli.util.DEFAULT_AGENTS_DIR", agents_dir)

        result = runner.invoke(
            app,
            ["integration", "verify", "--target", "copilot"],
        )

        # Verify may exit 1 if integration files are stale, but the agents
        # section should still appear in the output.
        assert "Agents:" in result.output
        assert "wired" in result.output

    def test_verify_lists_unwired_names(
        self,
        agents_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        _isolate_copilot_target: None,
    ) -> None:
        """Unwired agent names appear in the verify output."""
        monkeypatch.setattr("vesma.cli.agent_wiring.DEFAULT_AGENTS_DIR", agents_dir)
        monkeypatch.setattr("vesma.cli.util.DEFAULT_AGENTS_DIR", agents_dir)

        result = runner.invoke(
            app,
            ["integration", "verify", "--target", "copilot"],
        )

        assert "Unwired:" in result.output
        assert "Agent Architect" in result.output


# ── doctor: agent wiring check ────────────────────────────────────────────────


class TestDoctorAgentWiring:
    """Tests for the agent wiring check in ``mnemos doctor``."""

    def test_doctor_includes_agent_wiring_check(
        self,
        agents_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        _isolate_copilot_target: None,
    ) -> None:
        """``doctor`` runs the agent wiring check and reports status."""
        monkeypatch.setattr("vesma.cli.agent_wiring.DEFAULT_AGENTS_DIR", agents_dir)

        result = runner.invoke(app, ["doctor", "--json"])

        # Doctor may exit 0, 1, or 2 depending on the environment — we only
        # care that the agent wiring check is present in the output.
        payload = result.stdout
        assert "Agent wiring" in payload

    def test_doctor_warns_when_unwired(
        self,
        agents_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        _isolate_copilot_target: None,
    ) -> None:
        """Doctor reports WARN when agents are unwired and Copilot IS detected."""
        monkeypatch.setattr("vesma.cli.agent_wiring.DEFAULT_AGENTS_DIR", agents_dir)

        result = runner.invoke(app, ["doctor", "--json"])
        # The agent wiring check should mention "unwired".
        assert "unwired" in result.stdout

    def test_doctor_skip_when_no_agents_dir(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Doctor reports SKIP (not WARN) when no agents directory exists."""
        monkeypatch.setattr("vesma.cli.agent_wiring.DEFAULT_AGENTS_DIR", tmp_path / "no-agents")

        result = runner.invoke(app, ["doctor", "--json"])
        assert "Agent wiring" in result.stdout
        # Board card vesma-doctor-fixes-467: a machine without the Copilot
        # agents directory is "not applicable" (SKIP), never a warning.
        assert "not applicable" in result.stdout
        checks = json.loads(result.stdout)["checks"]
        wiring = next(c for c in checks if c["name"] == "Agent wiring")
        assert wiring["status"] == "skip"

    def test_doctor_skip_when_no_copilot_harness_detected(
        self,
        agents_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Agents dir present but the registry sees no Copilot harness → SKIP.

        The owner's-machine regression: a leftover ``~/.copilot/agents``
        full of unwired GCW agents on a machine that does not run the
        Copilot harness produced a permanent "0/41 wired" WARN.
        """
        monkeypatch.setattr("vesma.cli.agent_wiring.DEFAULT_AGENTS_DIR", agents_dir)

        # No _isolate_copilot_target here — but the REAL registry may see a
        # Copilot harness on a developer box (~/.copilot/instructions exists),
        # so force the not-detected verdict deterministically.
        config = TargetsConfig(targets=())
        monkeypatch.setattr(
            "vesma.cli.integration.load_targets", lambda config_path=None, home=None: config
        )

        result = runner.invoke(app, ["doctor", "--json"])
        checks = json.loads(result.stdout)["checks"]
        wiring = next(c for c in checks if c["name"] == "Agent wiring")
        assert wiring["status"] == "skip"
        assert "no Copilot harness detected" in wiring["detail"]
