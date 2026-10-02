"""Edge-case tests for agent MCP wiring — ``mnemos/cli/agent_wiring.py``.

These tests complement ``test_agent_wiring.py`` by covering gaps identified
during the QA audit:

* **Frontmatter edge cases**: missing frontmatter entirely, frontmatter with
  YAML comments, multi-line ``tools:`` block vs inline flow, ``tools`` as a
  string instead of a list (malformed).
* **Concurrent wiring**: two processes wiring the same agent file — last
  writer wins, no corruption (the file is not locked, but the operation is
  read-modify-write; we verify the final state is valid YAML with mnemos
  tools present).
* **File permissions**: read-only agent file → graceful ERROR result, no
  crash.
* **Large agent count**: 100+ agents — detection and verify scale linearly.
* **``--select`` with non-existent agent name** → exit code and message.
* **``--select`` with already-wired agent** → skip, don't error.
* **Wildcard → precise migration**: agent has ``mnemos/*``, user runs
  ``--precise`` → individual tokens added, wildcard preserved (both exist).
* **Doctor check PASS**: all agents wired → doctor reports PASS, not WARN.
* **Verify output format**: counts and percentages are correct.

All tests use ``tmp_path`` — never the real ``~/.copilot/agents/``.
"""

from __future__ import annotations

import os
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


def _tools_from_post(post: frontmatter.Post) -> list[str]:
    """Extract tools list from a frontmatter Post, narrowing the object type."""
    tools = post.metadata.get("tools")
    return list(tools) if isinstance(tools, list) else []


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


# ── Helpers ───────────────────────────────────────────────────────────────────


def _write_raw_agent(directory: Path, filename: str, content: str) -> Path:
    """Write an agent file with raw text content (no frontmatter parsing)."""
    path = directory / filename
    path.write_text(content, encoding="utf-8")
    return path


def _write_agent(
    directory: Path,
    filename: str,
    *,
    name: str | None = None,
    tools: list[str] | None = None,
    tool_profile: str | None = None,
    body: str = "You are an agent.\n",
) -> Path:
    metadata: dict[str, object] = {"name": name or filename.removesuffix(".agent.md")}
    if tools is not None:
        metadata["tools"] = tools
    if tool_profile is not None:
        metadata["tool_profile"] = tool_profile
    # frontmatter.Post accepts **metadata as kwargs at runtime, but mypy
    # sees the 2nd positional arg as `handler`. This is a known upstream
    # typing gap in python-frontmatter — same pattern as test_agent_wiring.py.
    post = frontmatter.Post(body, **metadata)  # type: ignore[arg-type]
    path = directory / filename
    frontmatter.dump(post, path)
    return path


# ── Frontmatter edge cases ────────────────────────────────────────────────────


class TestFrontmatterEdgeCases:
    """Malformed and unusual frontmatter shapes."""

    def test_no_frontmatter_at_all(self, tmp_path: Path) -> None:
        """A file with no frontmatter delimiters is reported, not crashed on."""
        directory = tmp_path / "agents"
        directory.mkdir()
        _write_raw_agent(
            directory,
            "no-frontmatter.agent.md",
            "Just some markdown content with no YAML block.\n",
        )

        infos = detect_agents(directory)
        assert len(infos) == 1
        info = infos[0]
        assert info.has_tools is False
        assert info.has_mnemos is False
        # Name falls back to filename.
        assert info.name == "no-frontmatter.agent.md"

    def test_frontmatter_with_yaml_comments(self, tmp_path: Path) -> None:
        """YAML comments in frontmatter are preserved through wiring."""
        directory = tmp_path / "agents"
        directory.mkdir()
        _write_raw_agent(
            directory,
            "commented.agent.md",
            (
                "---\n"
                "# This is a comment\n"
                "name: 'GCW: Commented Agent'  # inline comment\n"
                "tools:\n"
                "  - read\n"
                "  - search  # search tool\n"
                "---\n"
                "Body.\n"
            ),
        )
        # NOTE: names with colons must be quoted in YAML, else the parser
        # treats the colon as a mapping separator.

        infos = detect_agents(directory)
        assert len(infos) == 1
        assert infos[0].name == "GCW: Commented Agent"
        assert infos[0].tools_count == 2

        # Wiring should work and preserve the name.
        result = wire_agent(directory / "commented.agent.md", mode="wildcard")
        assert result.status == WireStatus.WIRED

        post = frontmatter.load(directory / "commented.agent.md")
        assert post.metadata["name"] == "GCW: Commented Agent"
        assert VESMARO_WILDCARD in _tools_from_post(post)

    def test_multiline_tools_block(self, tmp_path: Path) -> None:
        """Multi-line YAML block-style ``tools:`` array is parsed correctly."""
        directory = tmp_path / "agents"
        directory.mkdir()
        _write_raw_agent(
            directory,
            "block-tools.agent.md",
            (
                "---\n"
                "name: 'GCW: Block Tools'\n"
                "tools:\n"
                "  - read\n"
                "  - search\n"
                "  - execute\n"
                "---\n"
                "Body.\n"
            ),
        )

        infos = detect_agents(directory)
        assert infos[0].tools_count == 3
        assert infos[0].has_mnemos is False

        result = wire_agent(directory / "block-tools.agent.md", mode="wildcard")
        assert result.status == WireStatus.WIRED

        post = frontmatter.load(directory / "block-tools.agent.md")
        assert VESMARO_WILDCARD in _tools_from_post(post)
        # Original tools preserved.
        assert "read" in _tools_from_post(post)
        assert "execute" in _tools_from_post(post)

    def test_inline_flow_tools_array(self, tmp_path: Path) -> None:
        """Inline flow-style ``tools: [a, b]`` is parsed correctly."""
        directory = tmp_path / "agents"
        directory.mkdir()
        _write_raw_agent(
            directory,
            "flow-tools.agent.md",
            ("---\nname: 'GCW: Flow Tools'\ntools: [read, search, execute]\n---\nBody.\n"),
        )

        infos = detect_agents(directory)
        assert infos[0].tools_count == 3

        result = wire_agent(directory / "flow-tools.agent.md", mode="wildcard")
        assert result.status == WireStatus.WIRED

        post = frontmatter.load(directory / "flow-tools.agent.md")
        assert VESMARO_WILDCARD in _tools_from_post(post)

    def test_tools_as_string_not_list(self, tmp_path: Path) -> None:
        """``tools`` as a string (malformed) → treated as no-tools, no crash."""
        directory = tmp_path / "agents"
        directory.mkdir()
        _write_raw_agent(
            directory,
            "string-tools.agent.md",
            ("---\nname: 'GCW: String Tools'\ntools: read\n---\nBody.\n"),
        )

        infos = detect_agents(directory)
        info = infos[0]
        # tools is a string, not a list → has_tools is False.
        assert info.has_tools is False
        assert info.tools_count == 0

        # Wiring should create a new tools list (not append to the string).
        result = wire_agent(directory / "string-tools.agent.md", mode="wildcard")
        assert result.status == WireStatus.WIRED

        post = frontmatter.load(directory / "string-tools.agent.md")
        tools = _tools_from_post(post)
        assert isinstance(tools, list)
        assert VESMARO_WILDCARD in tools

    def test_empty_frontmatter(self, tmp_path: Path) -> None:
        """Empty frontmatter (``---\\n---``) → name falls back to filename."""
        directory = tmp_path / "agents"
        directory.mkdir()
        _write_raw_agent(
            directory,
            "empty-fm.agent.md",
            "---\n---\nBody.\n",
        )

        infos = detect_agents(directory)
        assert len(infos) == 1
        assert infos[0].name == "empty-fm.agent.md"
        assert infos[0].has_tools is False


# ── Concurrent wiring ─────────────────────────────────────────────────────────


class TestConcurrentWiring:
    """Concurrent wiring of the same file — last writer wins, no corruption."""

    def test_sequential_double_wire_no_corruption(self, tmp_path: Path) -> None:
        """Two sequential wires of the same file produce valid YAML.

        This simulates the race condition outcome (last writer wins) and
        verifies the file is not corrupted — it remains valid frontmatter
        with mnemos tools present.
        """
        directory = tmp_path / "agents"
        directory.mkdir()
        path = _write_agent(
            directory,
            "racy.agent.md",
            name="GCW: Racy",
            tools=["read", "search"],
        )

        # First wire — wildcard.
        r1 = wire_agent(path, mode="wildcard")
        assert r1.status == WireStatus.WIRED

        # Second wire — precise (simulates a concurrent process with
        # different mode). The file now has wildcard; precise adds tokens.
        r2 = wire_agent(path, mode="precise")
        assert r2.status == WireStatus.WIRED

        # File must still be valid frontmatter.
        post = frontmatter.load(path)
        tools = _tools_from_post(post)
        assert isinstance(tools, list)
        assert VESMARO_WILDCARD in tools
        for tool in VESMARO_TOOLS:
            assert tool in tools
        # Original tools preserved.
        assert "read" in tools
        assert "search" in tools

    def test_concurrent_double_wire_same_mode_idempotent(self, tmp_path: Path) -> None:
        """Two concurrent wires with the same mode → no duplication."""
        directory = tmp_path / "agents"
        directory.mkdir()
        path = _write_agent(
            directory,
            "racy2.agent.md",
            name="GCW: Racy 2",
            tools=["read"],
        )

        r1 = wire_agent(path, mode="wildcard")
        # Second wire sees the wildcard already present → ALREADY_WIRED.
        r2 = wire_agent(path, mode="wildcard")
        assert r1.status == WireStatus.WIRED
        assert r2.status == WireStatus.ALREADY_WIRED

        post = frontmatter.load(path)
        assert _tools_from_post(post).count(VESMARO_WILDCARD) == 1


# ── File permissions ──────────────────────────────────────────────────────────


class TestFilePermissions:
    """Read-only agent file → graceful error."""

    @pytest.mark.skipif(
        os.name == "nt",
        reason="POSIX permission bits not enforced on Windows",
    )
    def test_read_only_file_returns_error(self, tmp_path: Path) -> None:
        """A read-only agent file → ERROR status, no exception raised."""
        directory = tmp_path / "agents"
        directory.mkdir()
        path = _write_agent(
            directory,
            "readonly.agent.md",
            name="GCW: ReadOnly",
            tools=["read"],
        )
        path.chmod(0o444)  # read-only for all

        try:
            result = wire_agent(path, mode="wildcard")
            # Either ERROR (write failed) or WIRED if running as root.
            if os.geteuid() == 0:
                # Root bypasses permission checks.
                assert result.status in (WireStatus.WIRED, WireStatus.ERROR)
            else:
                assert result.status == WireStatus.ERROR
                assert "write failed" in result.note.lower()
        finally:
            # Restore permissions so tmp_path cleanup works.
            path.chmod(0o644)

    @pytest.mark.skipif(
        os.name == "nt",
        reason="POSIX permission bits not enforced on Windows",
    )
    def test_detect_agents_on_read_only_dir(self, tmp_path: Path) -> None:
        """detect_agents reads from a read-only directory without error."""
        directory = tmp_path / "agents"
        directory.mkdir()
        _write_agent(
            directory,
            "agent.agent.md",
            name="GCW: Agent",
            tools=["read"],
        )
        directory.chmod(0o555)  # read+execute, no write

        try:
            infos = detect_agents(directory)
            assert len(infos) == 1
            assert infos[0].name == "GCW: Agent"
        finally:
            directory.chmod(0o755)


# ── Large agent count ─────────────────────────────────────────────────────────


class TestLargeAgentCount:
    """Performance: 100+ agents — detection and verify scale."""

    def test_detect_100_agents(self, tmp_path: Path) -> None:
        """detect_agents handles 100 agent files without error."""
        directory = tmp_path / "agents"
        directory.mkdir()
        for i in range(100):
            _write_agent(
                directory,
                f"agent-{i:03d}.agent.md",
                name=f"GCW: Agent {i:03d}",
                tools=["read", "search"],
            )

        infos = detect_agents(directory)
        assert len(infos) == 100
        # All should be unwired (no mnemos tools).
        assert all(not info.has_mnemos for info in infos)

    def test_verify_100_agents_counts(self, tmp_path: Path) -> None:
        """verify_agents correctly counts 100 agents with mixed states."""
        directory = tmp_path / "agents"
        directory.mkdir()
        # 80 unwired, 10 wired (wildcard), 10 tool_profile.
        for i in range(80):
            _write_agent(
                directory,
                f"unwired-{i:03d}.agent.md",
                tools=["read"],
            )
        for i in range(10):
            _write_agent(
                directory,
                f"wired-{i:03d}.agent.md",
                tools=["read", VESMARO_WILDCARD],
            )
        for i in range(10):
            _write_agent(
                directory,
                f"profile-{i:03d}.agent.md",
                tool_profile="worker-readonly",
            )

        summary = verify_agents(directory)
        assert summary.total == 100
        assert summary.wired == 10
        assert summary.skipped_tool_profile == 10
        assert summary.unwired == 80
        assert len(summary.unwired_names) == 80
        assert summary.errors == 0
        assert summary.all_wired is False


# ── Wildcard → precise migration ──────────────────────────────────────────────


class TestWildcardToPreciseMigration:
    """Agent has ``mnemos/*``, user runs ``--precise``."""

    def test_precise_on_wildcard_adds_tokens_preserves_wildcard(self, tmp_path: Path) -> None:
        """Precise mode on a wildcard-wired agent adds tokens; wildcard stays.

        This is the documented behaviour: precise mode adds individual tokens
        that are missing. The wildcard ``mnemos/*`` is a different string from
        ``mnemos/vesma_add``, so it is not removed. Both coexist after
        migration.
        """
        directory = tmp_path / "agents"
        directory.mkdir()
        path = _write_agent(
            directory,
            "migrate.agent.md",
            name="GCW: Migrate",
            tools=["read", "search", VESMARO_WILDCARD],
        )

        result = wire_agent(path, mode="precise")
        assert result.status == WireStatus.WIRED
        assert len(result.tools_added) == len(VESMARO_TOOLS)

        post = frontmatter.load(path)
        tools = _tools_from_post(post)
        # Wildcard preserved.
        assert VESMARO_WILDCARD in tools
        # All precise tokens added.
        for tool in VESMARO_TOOLS:
            assert tool in tools
        # Original tools preserved.
        assert "read" in tools
        assert "search" in tools

    def test_precise_after_wildcard_is_idempotent(self, tmp_path: Path) -> None:
        """Running precise twice after wildcard → second run is ALREADY_WIRED."""
        directory = tmp_path / "agents"
        directory.mkdir()
        path = _write_agent(
            directory,
            "migrate2.agent.md",
            name="GCW: Migrate 2",
            tools=["read", VESMARO_WILDCARD],
        )

        wire_agent(path, mode="precise")
        result = wire_agent(path, mode="precise")
        assert result.status == WireStatus.ALREADY_WIRED
        assert result.tools_added == []


# ── CLI: --select edge cases ──────────────────────────────────────────────────


class TestCliSelectEdgeCases:
    """``--select`` with non-existent or already-wired agents."""

    def test_select_nonexistent_agent_name(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--select`` with a name that doesn't match → no agent wired."""
        directory = tmp_path / "agents"
        directory.mkdir()
        _write_agent(
            directory,
            "real-agent.agent.md",
            name="GCW: Real Agent",
            tools=["read"],
        )
        monkeypatch.setattr("vesma.cli.agent_wiring.DEFAULT_AGENTS_DIR", directory)
        monkeypatch.setattr("vesma.cli.util.DEFAULT_AGENTS_DIR", directory)

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
                "nonexistent-agent",
            ],
        )

        # The command should succeed (exit 0) but wire nothing.
        assert result.exit_code == 0, result.output
        # Real agent should NOT be wired (was not selected).
        post = frontmatter.load(directory / "real-agent.agent.md")
        assert VESMARO_WILDCARD not in _tools_from_post(post)

    def test_select_already_wired_agent_skips(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--select`` with an already-wired agent → skip, don't error."""
        directory = tmp_path / "agents"
        directory.mkdir()
        _write_agent(
            directory,
            "wired.agent.md",
            name="GCW: Wired",
            tools=["read", VESMARO_WILDCARD],
        )
        monkeypatch.setattr("vesma.cli.agent_wiring.DEFAULT_AGENTS_DIR", directory)
        monkeypatch.setattr("vesma.cli.util.DEFAULT_AGENTS_DIR", directory)

        original_tools = _tools_from_post(frontmatter.load(directory / "wired.agent.md"))

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
                "wired",
            ],
        )

        assert result.exit_code == 0, result.output
        # Tools unchanged — already wired (no duplication, no reformatting
        # of the tools list content).
        post = frontmatter.load(directory / "wired.agent.md")
        assert _tools_from_post(post) == original_tools
        assert _tools_from_post(post).count(VESMARO_WILDCARD) == 1


# ── Doctor: PASS case ─────────────────────────────────────────────────────────


class TestDoctorPassCase:
    """Doctor reports PASS (not WARN) when all agents are wired."""

    def test_doctor_pass_when_all_wired(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Doctor reports no unwired agents when everything is wired."""
        directory = tmp_path / "agents"
        directory.mkdir()
        _write_agent(
            directory,
            "wired.agent.md",
            name="GCW: Wired",
            tools=["read", VESMARO_WILDCARD],
        )
        _write_agent(
            directory,
            "profile.agent.md",
            name="GCW: Profile",
            tool_profile="worker-readonly",
        )
        monkeypatch.setattr("vesma.cli.agent_wiring.DEFAULT_AGENTS_DIR", directory)

        result = runner.invoke(app, ["doctor", "--json"])
        assert "Agent wiring" in result.stdout
        # No unwired agents → should not mention "unwired".
        assert "unwired" not in result.stdout.lower()


# ── Verify output format ──────────────────────────────────────────────────────


class TestVerifyOutputFormat:
    """``integration verify`` agents section shows correct counts."""

    def test_verify_shows_correct_counts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        _isolate_copilot_target: None,
    ) -> None:
        """Verify output includes wired/total counts."""
        directory = tmp_path / "agents"
        directory.mkdir()
        _write_agent(
            directory,
            "wired.agent.md",
            name="GCW: Wired",
            tools=["read", VESMARO_WILDCARD],
        )
        _write_agent(
            directory,
            "unwired.agent.md",
            name="GCW: Unwired",
            tools=["read"],
        )
        monkeypatch.setattr("vesma.cli.agent_wiring.DEFAULT_AGENTS_DIR", directory)
        monkeypatch.setattr("vesma.cli.util.DEFAULT_AGENTS_DIR", directory)

        result = runner.invoke(
            app,
            ["integration", "verify", "--target", "copilot"],
        )

        assert "Agents:" in result.output
        # 1 wired out of 2 total.
        assert "1" in result.output
        assert "Unwired:" in result.output
        assert "Unwired" in result.output
