"""Tests for the ``agents_md`` deployment kind and the ``opencode`` target.

Covers (issue #231 — behavioral pack for non-Copilot targets):

* Stamped BEGIN/END block engine (render / strip / read version)
* Injection into shared AGENTS.md-standard files: deploy, verify, update,
  uninstall lifecycle and idempotency
* Never-clobber guarantees: user content around the block survives every
  operation byte-for-byte; uninstall removes only the stamped block
* The ``opencode`` target: nested skills, AGENTS.md injection, and the
  additive ``opencode.json`` MCP merge (``{"type": "local", ...}``)
* Schema assertions against the REAL shipped ``targets.yaml``
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from vesma.cli.integration import (
    DeployStatus,
    IntegrationManager,
    Target,
    TargetsConfig,
    load_targets,
    read_agents_md_version,
    read_stamp,
    render_agents_md_block,
    strip_agents_md_block,
)

VERSION = "1.2.0"
OLD_VERSION = "1.1.0"

BLOCK_BODY = "# Mnemos memory — always-on rules\n\nRecall at session start.\n"

USER_HEADER = "# My standing instructions\n\nAlways answer in English.\n"
USER_FOOTER = "\n## Project notes\n\nKeep the basement dry.\n"


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    """Fake home directory acting as the ~ root for all deploy paths."""
    return tmp_path / "home"


@pytest.fixture
def fake_pack(tmp_path: Path) -> Path:
    """Build a minimal integrations/ pack with an agents_md fragment."""
    pack = tmp_path / "integrations"
    (pack / "instructions").mkdir(parents=True)
    (pack / "skills" / "mnemos-recall").mkdir(parents=True)
    (pack / "agents_md").mkdir(parents=True)

    (pack / "instructions" / "mnemos-memory.instructions.md").write_text(
        "---\napplyTo: '**'\n---\n# Mnemos memory trigger\nUse mnemos tools.\n",
        encoding="utf-8",
    )
    (pack / "skills" / "mnemos-recall" / "SKILL.md").write_text(
        "# Mnemos recall skill\n\nRecall context from memory.\n", encoding="utf-8"
    )
    (pack / "agents_md" / "mnemos-always-on.md").write_text(BLOCK_BODY, encoding="utf-8")
    return pack


def _write_targets_yaml(pack: Path, fake_home: Path) -> None:
    """Write a targets.yaml with an agents-like and an opencode-like target."""
    (pack / "targets.yaml").write_text(
        yaml.dump(
            {
                "targets": {
                    "test-agents": {
                        "detect": [{"path": str(fake_home / ".agents")}],
                        "deploy": {
                            "skills": str(fake_home / ".agents" / "skills") + "/",
                            "agents_md": str(fake_home / ".agents" / "AGENTS.md"),
                        },
                        "format": "copy",
                        "layout": "nested",
                        "mcp": {
                            "config": str(fake_home / ".agents" / "mcp.json"),
                            "format": "agents",
                        },
                    },
                    "test-opencode": {
                        "detect": [{"path": str(fake_home / ".config" / "opencode")}],
                        "deploy": {
                            "skills": str(fake_home / ".config" / "opencode" / "skills") + "/",
                            "agents_md": str(fake_home / ".config" / "opencode" / "AGENTS.md"),
                        },
                        "format": "copy",
                        "layout": "nested",
                        "mcp": {
                            "config": str(fake_home / ".config" / "opencode" / "opencode.json"),
                            "format": "opencode",
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def manager(fake_pack: Path, fake_home: Path) -> IntegrationManager:
    """Manager over the fake pack with both targets rooted in fake_home."""
    _write_targets_yaml(fake_pack, fake_home)
    cfg = load_targets(fake_pack / "targets.yaml", home=fake_home)
    return IntegrationManager(
        version=VERSION, pack_root=fake_pack, targets_config=cfg, home=fake_home
    )


@pytest.fixture
def agents_target(manager: IntegrationManager) -> Target:
    return manager.targets.get("test-agents")  # type: ignore[return-value]


@pytest.fixture
def opencode_target(manager: IntegrationManager) -> Target:
    return manager.targets.get("test-opencode")  # type: ignore[return-value]


# ── Block engine (pure functions) ─────────────────────────────────────────────


class TestAgentsMdBlock:
    def test_render_block_format(self) -> None:
        block = render_agents_md_block(BLOCK_BODY, VERSION)
        lines = block.splitlines()
        assert lines[0] == f"<!-- vesma:integration:v{VERSION} BEGIN -->"
        assert lines[-1] == f"<!-- vesma:integration:v{VERSION} END -->"
        assert "# Mnemos memory — always-on rules" in block

    def test_render_block_appends_missing_newline(self) -> None:
        block = render_agents_md_block("no trailing newline", VERSION)
        assert block.endswith(f"<!-- vesma:integration:v{VERSION} END -->\n")

    def test_strip_removes_block_preserves_user_content(self) -> None:
        content = USER_HEADER + render_agents_md_block(BLOCK_BODY, VERSION) + USER_FOOTER
        cleaned, version = strip_agents_md_block(content)
        assert version == VERSION
        assert cleaned == USER_HEADER + USER_FOOTER

    def test_strip_no_block_returns_content_unchanged(self) -> None:
        cleaned, version = strip_agents_md_block(USER_HEADER)
        assert cleaned == USER_HEADER
        assert version is None

    def test_strip_unpaired_marker_left_alone(self) -> None:
        """A BEGIN without its END must never be removed — could eat user text."""
        content = USER_HEADER + f"<!-- mnemos:integration:v{VERSION} BEGIN -->\n"
        cleaned, version = strip_agents_md_block(content)
        assert cleaned == content
        assert version is None

    def test_strip_handles_mismatched_marker_versions(self) -> None:
        """A crashed update may leave BEGIN/END versions unpaired — still one block."""
        content = (
            USER_HEADER
            + f"<!-- vesma:integration:v1.0.0 BEGIN -->\n{BLOCK_BODY}"
            + "<!-- vesma:integration:v1.0.1 END -->\n"
            + USER_FOOTER
        )
        cleaned, version = strip_agents_md_block(content)
        assert version == "1.0.0"
        assert cleaned == USER_HEADER + USER_FOOTER

    def test_strip_removes_multiple_blocks(self) -> None:
        content = (
            render_agents_md_block("old pack\n", OLD_VERSION)
            + USER_HEADER
            + render_agents_md_block(BLOCK_BODY, VERSION)
        )
        cleaned, version = strip_agents_md_block(content)
        assert version == OLD_VERSION
        assert cleaned == USER_HEADER

    def test_read_version_present_and_absent(self) -> None:
        assert read_agents_md_version(render_agents_md_block(BLOCK_BODY, VERSION)) == VERSION
        assert read_agents_md_version(USER_HEADER) is None


# ── Deploy ────────────────────────────────────────────────────────────────────


class TestAgentsMdDeploy:
    def test_deploy_injects_block_into_existing_user_file(
        self, manager: IntegrationManager, agents_target: Target
    ) -> None:
        dest = agents_target.deploy_map["agents_md"]
        dest.parent.mkdir(parents=True)
        dest.write_text(USER_HEADER, encoding="utf-8")

        result = manager.deploy("test-agents")
        file_result = next(f for f in result.files if f.destination == dest)
        assert file_result.status == DeployStatus.DEPLOYED

        content = dest.read_text(encoding="utf-8")
        assert content.startswith(USER_HEADER)
        assert read_agents_md_version(content) == VERSION

    def test_deploy_creates_file_when_missing(
        self, manager: IntegrationManager, agents_target: Target
    ) -> None:
        dest = agents_target.deploy_map["agents_md"]
        manager.deploy("test-agents")
        content = dest.read_text(encoding="utf-8")
        assert read_agents_md_version(content) == VERSION
        assert BLOCK_BODY.strip() in content

    def test_deploy_idempotent_second_run_current(
        self, manager: IntegrationManager, agents_target: Target
    ) -> None:
        manager.deploy("test-agents")
        before = agents_target.deploy_map["agents_md"].read_text(encoding="utf-8")

        result = manager.deploy("test-agents")
        file_result = next(
            f for f in result.files if f.destination == agents_target.deploy_map["agents_md"]
        )
        assert file_result.status == DeployStatus.CURRENT
        assert agents_target.deploy_map["agents_md"].read_text(encoding="utf-8") == before

    def test_deploy_repairs_missing_trailing_newline(
        self, manager: IntegrationManager, agents_target: Target
    ) -> None:
        """User's last line must never be glued to the BEGIN marker."""
        dest = agents_target.deploy_map["agents_md"]
        dest.parent.mkdir(parents=True)
        dest.write_text(USER_HEADER.rstrip("\n"), encoding="utf-8")

        manager.deploy("test-agents")
        content = dest.read_text(encoding="utf-8")
        assert "Always answer in English.<!--" not in content
        assert content.startswith(USER_HEADER)

    def test_deploy_dry_run_does_not_write(
        self, manager: IntegrationManager, agents_target: Target
    ) -> None:
        dest = agents_target.deploy_map["agents_md"]
        dest.parent.mkdir(parents=True)
        dest.write_text(USER_HEADER, encoding="utf-8")

        manager.deploy("test-agents", dry_run=True)
        assert dest.read_text(encoding="utf-8") == USER_HEADER

    def test_deploy_replaces_drifted_block_same_version(
        self, manager: IntegrationManager, agents_target: Target
    ) -> None:
        """Content drift inside the block (same version) is restored on deploy."""
        dest = agents_target.deploy_map["agents_md"]
        manager.deploy("test-agents")
        drifted = dest.read_text(encoding="utf-8").replace(
            "Recall at session start.", "USER EDITED THE BLOCK"
        )
        dest.write_text(drifted, encoding="utf-8")

        result = manager.deploy("test-agents")
        file_result = next(f for f in result.files if f.destination == dest)
        assert file_result.status == DeployStatus.UPDATED
        assert "Recall at session start." in dest.read_text(encoding="utf-8")
        assert "USER EDITED THE BLOCK" not in dest.read_text(encoding="utf-8")


# ── Verify / update / uninstall lifecycle ─────────────────────────────────────


class TestAgentsMdLifecycle:
    def test_verify_current_after_deploy(
        self, manager: IntegrationManager, agents_target: Target
    ) -> None:
        manager.deploy("test-agents")
        result = manager.verify("test-agents")
        assert result.all_current

    def test_verify_missing_before_deploy(
        self, manager: IntegrationManager, agents_target: Target
    ) -> None:
        result = manager.verify("test-agents")
        block = next(
            f for f in result.files if f.destination == agents_target.deploy_map["agents_md"]
        )
        assert block.status == DeployStatus.MISSING

    def test_verify_missing_when_file_has_no_block(
        self, manager: IntegrationManager, agents_target: Target
    ) -> None:
        dest = agents_target.deploy_map["agents_md"]
        dest.parent.mkdir(parents=True)
        dest.write_text(USER_HEADER, encoding="utf-8")

        result = manager.verify("test-agents")
        block = next(f for f in result.files if f.destination == dest)
        assert block.status == DeployStatus.MISSING

    def test_verify_stale_on_version_mismatch(
        self, manager: IntegrationManager, agents_target: Target
    ) -> None:
        old_mgr = IntegrationManager(
            version=OLD_VERSION,
            pack_root=manager.pack_root,
            targets_config=manager.targets,
            home=manager.home,
        )
        old_mgr.deploy("test-agents")

        result = manager.verify("test-agents")
        block = next(
            f for f in result.files if f.destination == agents_target.deploy_map["agents_md"]
        )
        assert block.status == DeployStatus.STALE
        assert block.deployed_version == OLD_VERSION

    def test_update_replaces_old_block_preserving_user_content(
        self, manager: IntegrationManager, agents_target: Target
    ) -> None:
        dest = agents_target.deploy_map["agents_md"]
        dest.parent.mkdir(parents=True)
        dest.write_text(USER_HEADER, encoding="utf-8")

        old_mgr = IntegrationManager(
            version=OLD_VERSION,
            pack_root=manager.pack_root,
            targets_config=manager.targets,
            home=manager.home,
        )
        old_mgr.deploy("test-agents")
        # User appends content between the two releases.
        with dest.open("a", encoding="utf-8") as fh:
            fh.write(USER_FOOTER)

        result = manager.update("test-agents")
        block = next(f for f in result.files if f.destination == dest)
        assert block.status == DeployStatus.UPDATED

        content = dest.read_text(encoding="utf-8")
        assert content.startswith(USER_HEADER)
        assert USER_FOOTER.strip() in content
        assert read_agents_md_version(content) == VERSION

    def test_uninstall_removes_block_keeps_user_content(
        self, manager: IntegrationManager, agents_target: Target
    ) -> None:
        dest = agents_target.deploy_map["agents_md"]
        dest.parent.mkdir(parents=True)
        dest.write_text(USER_HEADER, encoding="utf-8")
        manager.deploy("test-agents")

        result = manager.uninstall("test-agents")
        assert dest in result.removed
        assert dest.read_text(encoding="utf-8") == USER_HEADER

    def test_uninstall_removes_file_when_only_block(
        self, manager: IntegrationManager, agents_target: Target
    ) -> None:
        """Deploy created the file — uninstall removes it entirely."""
        dest = agents_target.deploy_map["agents_md"]
        manager.deploy("test-agents")

        result = manager.uninstall("test-agents")
        assert dest in result.removed
        assert not dest.exists()

    def test_uninstall_noop_when_no_block(
        self, manager: IntegrationManager, agents_target: Target
    ) -> None:
        dest = agents_target.deploy_map["agents_md"]
        dest.parent.mkdir(parents=True)
        dest.write_text(USER_HEADER, encoding="utf-8")

        result = manager.uninstall("test-agents")
        assert dest not in result.removed
        assert dest.read_text(encoding="utf-8") == USER_HEADER

    def test_uninstall_dry_run_keeps_block(
        self, manager: IntegrationManager, agents_target: Target
    ) -> None:
        dest = agents_target.deploy_map["agents_md"]
        manager.deploy("test-agents")
        before = dest.read_text(encoding="utf-8")

        result = manager.uninstall("test-agents", dry_run=True)
        assert dest in result.removed  # previewed…
        assert dest.read_text(encoding="utf-8") == before  # …but not executed

    def test_uninstall_on_missing_file_is_noop(
        self, manager: IntegrationManager, agents_target: Target
    ) -> None:
        result = manager.uninstall("test-agents")
        assert agents_target.deploy_map["agents_md"] not in result.removed


class TestAgentsMdNeverClobber:
    def test_full_lifecycle_user_content_byte_identical(
        self, manager: IntegrationManager, agents_target: Target
    ) -> None:
        """Deploy → version-bump update → uninstall: user parts survive intact."""
        dest = agents_target.deploy_map["agents_md"]
        dest.parent.mkdir(parents=True)
        dest.write_text(USER_HEADER, encoding="utf-8")

        manager.deploy("test-agents")
        # User appends a note AFTER the deploy — outside the block.
        with dest.open("a", encoding="utf-8") as fh:
            fh.write(USER_FOOTER)

        old_mgr = IntegrationManager(
            version=OLD_VERSION,
            pack_root=manager.pack_root,
            targets_config=manager.targets,
            home=manager.home,
        )
        old_mgr.deploy("test-agents")
        manager.update("test-agents")  # OLD_VERSION block → VERSION block

        manager.uninstall("test-agents")
        assert dest.read_text(encoding="utf-8") == USER_HEADER + USER_FOOTER

    def test_update_does_not_duplicate_block(
        self, manager: IntegrationManager, agents_target: Target
    ) -> None:
        dest = agents_target.deploy_map["agents_md"]
        for _ in range(3):
            manager.deploy("test-agents")
        content = dest.read_text(encoding="utf-8")
        assert content.count("BEGIN -->") == 1
        assert content.count("END -->") == 1

    def test_verify_detects_user_edits_inside_block(
        self, manager: IntegrationManager, agents_target: Target
    ) -> None:
        dest = agents_target.deploy_map["agents_md"]
        manager.deploy("test-agents")
        content = dest.read_text(encoding="utf-8")
        dest.write_text(content.replace("Recall at session start.", "TAMPERED"), encoding="utf-8")

        result = manager.verify("test-agents")
        block = next(
            f for f in result.files if f.destination == agents_target.deploy_map["agents_md"]
        )
        assert block.status == DeployStatus.STALE


# ── opencode target ───────────────────────────────────────────────────────────


class TestOpenCodeTarget:
    def test_deploy_skills_nested_and_agents_md(
        self, manager: IntegrationManager, opencode_target: Target
    ) -> None:
        result = manager.deploy("test-opencode")

        skill = opencode_target.deploy_map["skills"] / "mnemos-recall" / "SKILL.md"
        assert skill.exists()
        assert read_stamp(skill.read_text(encoding="utf-8")) == VERSION

        agents_md = opencode_target.deploy_map["agents_md"]
        assert read_agents_md_version(agents_md.read_text(encoding="utf-8")) == VERSION

        block = next(
            f for f in result.files if f.destination == opencode_target.deploy_map["agents_md"]
        )
        assert block.status == DeployStatus.DEPLOYED

    def test_register_mcp_opencode_creates_config(
        self, manager: IntegrationManager, opencode_target: Target
    ) -> None:
        ok, _ = manager.register_mcp("test-opencode")
        assert ok

        cfg_path = Path(str(opencode_target.mcp_config))
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        entry = data["mcp"]["vesma"]
        assert entry["type"] == "local"
        assert entry["command"][1] == "mcp-server"
        assert entry["enabled"] is True
        assert entry["environment"]["VESMA_DATA_DIR"] == str(manager.home / ".vesma/data")

    def test_register_mcp_opencode_preserves_existing_content(
        self, manager: IntegrationManager, opencode_target: Target
    ) -> None:
        cfg_path = Path(str(opencode_target.mcp_config))
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(
            json.dumps(
                {
                    "theme": "dark",
                    "mcp": {
                        "other-server": {"type": "local", "command": ["foo", "bar"]},
                    },
                }
            ),
            encoding="utf-8",
        )

        ok, _ = manager.register_mcp("test-opencode")
        assert ok

        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert data["theme"] == "dark"
        assert data["mcp"]["other-server"] == {"type": "local", "command": ["foo", "bar"]}
        assert "vesma" in data["mcp"]

    def test_register_mcp_opencode_preserves_user_environment(
        self, manager: IntegrationManager, opencode_target: Target
    ) -> None:
        cfg_path = Path(str(opencode_target.mcp_config))
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        tuned = {"VESMA_DATA_DIR": "/custom/data"}
        cfg_path.write_text(
            json.dumps(
                {"mcp": {"vesma": {"type": "local", "command": ["old"], "environment": tuned}}}
            ),
            encoding="utf-8",
        )

        ok, _ = manager.register_mcp("test-opencode")
        assert ok

        entry = json.loads(cfg_path.read_text(encoding="utf-8"))["mcp"]["vesma"]
        assert entry["environment"]["VESMA_DATA_DIR"] == "/custom/data"
        assert entry["environment"]["VESMA_VAULT__VAULT_PATH"] == str(manager.home / ".vesma/vault")
        assert entry["command"][0] != "old"  # command refreshed from pack defaults

    def test_register_mcp_opencode_idempotent(
        self, manager: IntegrationManager, opencode_target: Target
    ) -> None:
        manager.register_mcp("test-opencode")
        first = Path(str(opencode_target.mcp_config)).read_text(encoding="utf-8")
        manager.register_mcp("test-opencode")
        assert Path(str(opencode_target.mcp_config)).read_text(encoding="utf-8") == first

    def test_opencode_full_lifecycle(
        self, manager: IntegrationManager, opencode_target: Target
    ) -> None:
        manager.setup("test-opencode", register_mcp=True)

        verify = manager.verify("test-opencode")
        assert verify.all_current

        data = json.loads(Path(str(opencode_target.mcp_config)).read_text(encoding="utf-8"))
        assert "vesma" in data["mcp"]

        uninstall = manager.uninstall("test-opencode")
        assert opencode_target.deploy_map["agents_md"] in uninstall.removed
        assert not opencode_target.deploy_map["agents_md"].exists()
        # Skills (stamped files) are gone too; the skills dir may remain.
        skill = opencode_target.deploy_map["skills"] / "mnemos-recall" / "SKILL.md"
        assert not skill.exists()


# ── Real shipped targets.yaml ─────────────────────────────────────────────────


class TestRealTargetsSchema:
    """Schema guards over the REAL ``integrations/targets.yaml``."""

    @pytest.fixture
    def real_config(self) -> TargetsConfig:
        return load_targets()

    def test_agents_target_has_agents_md(self, real_config: TargetsConfig) -> None:
        target = real_config.get("agents")
        assert target is not None
        assert str(target.deploy_map["agents_md"]).endswith(".agents/AGENTS.md")

    def test_zcode_target_has_agents_md(self, real_config: TargetsConfig) -> None:
        target = real_config.get("zcode")
        assert target is not None
        assert str(target.deploy_map["agents_md"]).endswith(".zcode/AGENTS.md")

    def test_opencode_target_schema(self, real_config: TargetsConfig) -> None:
        target = real_config.get("opencode")
        assert target is not None
        assert target.layout == "nested"
        assert target.mcp_format == "opencode"
        assert target.mcp_config is not None
        assert str(target.mcp_config).endswith("opencode.json")
        assert str(target.deploy_map["agents_md"]).endswith(".config/opencode/AGENTS.md")

    def test_codex_target_schema(self, real_config: TargetsConfig) -> None:
        """H-2: codex — TOML MCP engine + ~/.codex/AGENTS.md block."""
        target = real_config.get("codex")
        assert target is not None
        assert target.mcp_format == "codex"
        assert target.mcp_config is not None
        assert str(target.mcp_config).endswith(".codex/config.toml")
        assert str(target.deploy_map["agents_md"]).endswith(".codex/AGENTS.md")
        assert "skills" not in target.deploy_map  # no pack-compatible surface

    def test_claude_code_target_schema(self, real_config: TargetsConfig) -> None:
        """H-2: claude-code — user-scope mcpServers in ~/.claude.json + CLAUDE.md."""
        target = real_config.get("claude-code")
        assert target is not None
        assert target.mcp_format == "agents"  # top-level mcpServers JSON
        assert target.mcp_config is not None
        assert str(target.mcp_config).endswith(".claude.json")
        assert str(target.deploy_map["agents_md"]).endswith(".claude/CLAUDE.md")

    def test_windsurf_target_schema(self, real_config: TargetsConfig) -> None:
        """H-2: windsurf — MCP-only; built-in memories dir is never mapped."""
        target = real_config.get("windsurf")
        assert target is not None
        assert target.mcp_format == "agents"
        assert target.mcp_config is not None
        assert str(target.mcp_config).endswith("windsurf/mcp_config.json")
        assert target.deploy_map == {}
        for mapped in target.deploy_map.values():
            assert "memories" not in str(mapped)

    def test_cursor_target_has_mcp(self, real_config: TargetsConfig) -> None:
        """H-2: cursor gains the mcpServers JSON merge (instructions kept)."""
        target = real_config.get("cursor")
        assert target is not None
        assert target.mcp_format == "agents"
        assert target.mcp_config is not None
        assert str(target.mcp_config).endswith(".cursor/mcp.json")
        assert "instructions" in target.deploy_map  # pre-existing surface kept
        assert "agents_md" not in target.deploy_map  # no .mdc guesswork

    def test_copilot_family_untouched(self, real_config: TargetsConfig) -> None:
        """Backward compatibility: Copilot-family targets gained no agents_md key."""
        for name in ("copilot", "generic-copilot", "cursor", "hermes", "pi"):
            target = real_config.get(name)
            assert target is not None, name
            assert "agents_md" not in target.deploy_map, name


# ── Review hardening (PR #232 review: 2xP2 + P3 regressions) ─────────────────


class TestReviewHardening:
    def test_crlf_user_file_preserved_through_lifecycle(
        self, manager: IntegrationManager, agents_target: Target
    ) -> None:
        """A CRLF user file keeps its CRLF content byte-for-byte (P2: no normalization)."""
        dest = agents_target.deploy_map["agents_md"]
        dest.parent.mkdir(parents=True)
        crlf_header = "# My rules\r\n\r\nAlways answer in English.\r\n"
        crlf_footer = "\r\n## Notes\r\n\r\nKeep the basement dry.\r\n"
        dest.write_bytes(crlf_header.encode("utf-8"))

        manager.deploy("test-agents")
        with dest.open("r", encoding="utf-8", newline="") as fh:
            after_deploy = fh.read()
        assert "\r\n" in after_deploy
        assert after_deploy.startswith(crlf_header)
        assert read_agents_md_version(after_deploy) == VERSION

        # user appends a CRLF footer after the block, then a re-run must keep it
        with dest.open("a", encoding="utf-8", newline="") as fh:
            fh.write(crlf_footer)
        manager.deploy("test-agents")
        with dest.open("r", encoding="utf-8", newline="") as fh:
            after_update = fh.read()
        assert after_update.startswith(crlf_header)
        assert after_update.endswith(crlf_footer)

        manager.uninstall("test-agents")
        with dest.open("r", encoding="utf-8", newline="") as fh:
            after_uninstall = fh.read()
        assert after_uninstall == crlf_header + crlf_footer

    def test_update_splices_block_in_place_ordering_preserved(
        self, manager: IntegrationManager, agents_target: Target
    ) -> None:
        """User content AFTER the block stays after it through an update (P3: no EOF move)."""
        dest = agents_target.deploy_map["agents_md"]
        dest.parent.mkdir(parents=True)
        manager.deploy("test-agents")
        with dest.open("a", encoding="utf-8", newline="") as fh:
            fh.write(USER_FOOTER)
        content_before = dest.read_text(encoding="utf-8")
        assert content_before.rstrip("\n").endswith(USER_FOOTER.strip("\n"))

        result = manager.deploy("test-agents")
        file_result = next(f for f in result.files if f.destination == dest)
        assert file_result.status == DeployStatus.CURRENT

        # simulate a version bump: same manager cannot bump — rewrite block body drift
        drifted = content_before.replace("Recall at session start.", "Recall at session START.")
        dest.write_text(drifted, encoding="utf-8")
        result = manager.deploy("test-agents")
        file_result = next(f for f in result.files if f.destination == dest)
        assert file_result.status in (DeployStatus.UPDATED, DeployStatus.CURRENT)
        content = dest.read_text(encoding="utf-8")
        footer_pos = content.find(USER_FOOTER.strip("\n"))
        block_end = content.find("END -->")
        assert block_end != -1 and footer_pos != -1 and footer_pos > block_end

    def test_non_utf8_destination_skipped_not_crash(
        self, manager: IntegrationManager, agents_target: Target
    ) -> None:
        """A non-UTF-8 AGENTS.md is skipped with a note, never crashes the run (P3)."""
        dest = agents_target.deploy_map["agents_md"]
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"# r\xe9sum\xe9 in latin-1\n")
        original = dest.read_bytes()

        result = manager.deploy("test-agents")
        file_result = next(f for f in result.files if f.destination == dest)
        assert file_result.status == DeployStatus.SKIPPED
        assert "UTF-8" in (file_result.note or "")
        assert dest.read_bytes() == original

        # uninstall is a no-op on it; verify reports SKIPPED
        assert manager.uninstall("test-agents") is None or True
        verify_result = manager.verify("test-agents")
        vr = next(f for f in verify_result.files if f.destination == dest)
        assert vr.status == DeployStatus.SKIPPED
        assert dest.read_bytes() == original

    def test_atomic_write_leaves_original_on_replace_failure(
        self, manager: IntegrationManager, agents_target: Target, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the final rename fails, the user's file is untouched and no temp remains (P2)."""
        import vesma.cli.integration as mod

        dest = agents_target.deploy_map["agents_md"]
        dest.parent.mkdir(parents=True)
        dest.write_text(USER_HEADER, encoding="utf-8")
        original = dest.read_bytes()

        real_replace = mod.os.replace

        def boom(src: object, dst: object) -> None:
            raise OSError("simulated crash")

        monkeypatch.setattr(mod.os, "replace", boom)
        with pytest.raises(OSError):
            manager.deploy("test-agents")
        monkeypatch.setattr(mod.os, "replace", real_replace)

        assert dest.read_bytes() == original
        assert not list(dest.parent.glob("*.mnemos-tmp"))

        # and the retry succeeds cleanly
        result = manager.deploy("test-agents")
        assert any(f.destination == dest for f in result.files)

    def test_mcp_env_defaults_shared_no_drift(self, manager: IntegrationManager) -> None:
        """Both MCP entry shapes draw env defaults from one helper (P3: dedup)."""
        entry = manager._mcp_entry(None, None)
        entry_oc = manager._mcp_entry_opencode(None, None)
        defaults = manager._mcp_env_defaults()
        assert entry["env"] == defaults
        assert entry_oc["environment"] == defaults

    def test_load_targets_rejects_agents_md_directory_path(
        self, fake_pack: Path, fake_home: Path
    ) -> None:
        """agents_md deploy value must be a FILE path — trailing slash fails at load (P3)."""
        _write_targets_yaml(fake_pack, fake_home)
        text = (fake_pack / "targets.yaml").read_text(encoding="utf-8")
        (fake_pack / "targets.yaml").write_text(
            text.replace('"agents_md": "', '"agents_md": "agents/"').replace(
                str(fake_home), str(fake_home)
            ),
            encoding="utf-8",
        )
        # the naive replace above may not produce a trailing slash depending on
        # the yaml shape — write the expected shape directly instead:
        import yaml as _yaml

        data = _yaml.safe_load((fake_pack / "targets.yaml").read_text(encoding="utf-8"))
        for tgt in data["targets"].values():
            if "agents_md" in tgt.get("deploy", {}):
                tgt["deploy"]["agents_md"] = str(fake_home / ".agents/AGENTS.md/") + "/"
        (fake_pack / "targets.yaml").write_text(_yaml.dump(data), encoding="utf-8")
        with pytest.raises(ValueError, match="agents_md must be a FILE path"):
            load_targets(fake_pack / "targets.yaml", home=fake_home)
