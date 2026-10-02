"""Tests for M8 — Path-scoped rules ingest."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.models import MemorySource, MemoryStatus


@pytest.fixture
def manager():
    with tempfile.TemporaryDirectory() as tmpdir:
        settings = Settings(
            vesma={"vault_path": tmpdir, "data_dir": tmpdir, "db_name": "test.db"},
            embedding={"provider": "nano"},
        )
        mgr = MemoryManager(settings)
        yield mgr
        mgr.close()


@pytest.fixture
def sample_rule_file():
    content = """---
applyTo: '**'
description: 'Test rule description'
---

# Test Rule Title

This is the body of the rule.
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".instructions.md", delete=False) as fh:
        fh.write(content)
        fh.flush()
        yield Path(fh.name)


class TestParseRuleFile:
    def test_parses_frontmatter_and_body(self, sample_rule_file):
        from vesma.watchers.path_scoped import parse_rule_file

        result = parse_rule_file(sample_rule_file)

        assert result["title"] == "Test Rule Title"
        assert result["description"] == "Test rule description"
        assert result["apply_to"] == ["**"]
        assert "This is the body of the rule." in result["body"]
        assert result["source_url"] == str(sample_rule_file.resolve())

    def test_parses_multiple_apply_to(self):
        content = """---
applyTo:
  - 'src/**'
  - 'tests/**'
---

# Multi Scope Rule

Body here.
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".instructions.md", delete=False) as fh:
            fh.write(content)
            path = Path(fh.name)

        from vesma.watchers.path_scoped import parse_rule_file

        result = parse_rule_file(path)
        assert result["apply_to"] == ["src/**", "tests/**"]
        path.unlink()

    def test_no_frontmatter(self):
        content = "# No Frontmatter\n\nJust body."
        with tempfile.NamedTemporaryFile(mode="w", suffix=".instructions.md", delete=False) as fh:
            fh.write(content)
            path = Path(fh.name)

        from vesma.watchers.path_scoped import parse_rule_file

        result = parse_rule_file(path)
        assert result["title"] == "No Frontmatter"
        assert result["apply_to"] == ["**"]
        assert result["body"] == "Just body."
        path.unlink()


class TestIngestRule:
    def test_creates_published_memory(self, manager, sample_rule_file):
        from vesma.watchers.path_scoped import ingest_rule

        result = ingest_rule(manager, sample_rule_file, project="test-proj", agent="test-agent")

        assert result["action"] == "created"
        assert result["memory_id"] is not None

        memory = manager.get(result["memory_id"])
        assert memory is not None
        assert memory.source == MemorySource.RULE
        assert memory.status == MemoryStatus.PUBLISHED
        assert "mnemos:rule" in memory.tags
        assert "project:test-proj" in memory.tags
        assert "agent:test-agent" in memory.tags
        assert "applyTo:**" in memory.tags
        assert "source:path-scoped-rule" in memory.tags
        assert memory.title == "Test Rule Title"

    def test_updates_existing_memory(self, manager, sample_rule_file):
        from vesma.watchers.path_scoped import ingest_rule

        # First ingest
        result1 = ingest_rule(manager, sample_rule_file, project="test-proj")
        mem_id = result1["memory_id"]

        # Modify file
        new_content = """---
applyTo: 'src/**'
description: 'Updated description'
---

# Updated Title

Updated body.
"""
        sample_rule_file.write_text(new_content, encoding="utf-8")

        # Re-ingest
        result2 = ingest_rule(manager, sample_rule_file, project="test-proj")

        assert result2["action"] == "updated"
        assert result2["memory_id"] == mem_id

        memory = manager.get(mem_id)
        assert memory.title == "Updated Title"
        assert "applyTo:src/**" in memory.tags
        assert "Updated body." in memory.content


class TestRemoveRule:
    def test_removes_existing_memory(self, manager, sample_rule_file):
        from vesma.watchers.path_scoped import ingest_rule, remove_rule

        result = ingest_rule(manager, sample_rule_file, project="test-proj")
        mem_id = result["memory_id"]

        remove_result = remove_rule(manager, sample_rule_file)
        assert remove_result["removed"] is True
        assert remove_result["memory_id"] == mem_id

        assert manager.get(mem_id) is None

    def test_noop_for_missing_memory(self, manager, sample_rule_file):
        from vesma.watchers.path_scoped import remove_rule

        result = remove_rule(manager, sample_rule_file)
        assert result["removed"] is False
        assert result["memory_id"] is None


class TestBatchIngest:
    def test_ingests_multiple_files(self, manager):
        with tempfile.TemporaryDirectory() as tmpdir:
            rules_dir = Path(tmpdir) / ".github" / "instructions"
            rules_dir.mkdir(parents=True)

            (rules_dir / "rule1.instructions.md").write_text(
                "---\napplyTo: '**'\n---\n# Rule 1\nBody 1."
            )
            (rules_dir / "rule2.instructions.md").write_text(
                "---\napplyTo: 'src/**'\n---\n# Rule 2\nBody 2."
            )

            from vesma.watchers.path_scoped import ingest_path_scoped_rules

            results = ingest_path_scoped_rules(
                manager, rules_dir, project="batch-proj", agent="batch-agent"
            )

            assert len(results) == 2
            assert all(r["action"] == "created" for r in results)

            # Verify both memories exist
            memories = manager.list_recent(project="batch-proj")
            assert len(memories) == 2
            titles = {m.title for m in memories}
            assert titles == {"Rule 1", "Rule 2"}

    def test_skips_missing_directory(self, manager):
        from vesma.watchers.path_scoped import ingest_path_scoped_rules

        results = ingest_path_scoped_rules(manager, Path("/nonexistent/path"), project="test")
        assert results == []


class TestManagerMethods:
    def test_manager_ingest_path_scoped_rules(self, manager):
        with tempfile.TemporaryDirectory() as tmpdir:
            rules_dir = Path(tmpdir) / "instructions"
            rules_dir.mkdir()
            (rules_dir / "test.instructions.md").write_text(
                "---\napplyTo: '**'\n---\n# Test\nBody."
            )

            results = manager.ingest_path_scoped_rules(
                rules_dir, project="mgr-proj", agent="mgr-agent"
            )

            assert len(results) == 1
            assert results[0]["action"] == "created"

    def test_manager_remove_path_scoped_rule(self, manager, sample_rule_file):
        from vesma.watchers.path_scoped import ingest_rule

        result = ingest_rule(manager, sample_rule_file, project="test-proj")
        mem_id = result["memory_id"]

        remove_result = manager.remove_path_scoped_rule(sample_rule_file)
        assert remove_result["removed"] is True
        assert manager.get(mem_id) is None
