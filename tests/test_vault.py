"""Tests for `storage/vault.py` — Obsidian-compatible markdown vault.

Covers the M18 coverage push for `VaultManager` (66% → ≥ 90%):
  - memory_to_file sanitizes special characters in title (via public surface)
  - memory_to_file with full fields and minimal fields
  - memory_to_file collision (file exists with different file_path → _id suffix)
  - memory_to_file with no `title` falls back to auto_title()
  - file_to_memory happy path (all frontmatter fields)
  - file_to_memory error paths (missing path, non-`.md`, invalid YAML, empty content)
  - file_to_memory with invalid `created`/`updated` dates → falls back to now()
  - file_to_memory with missing `id` → falls back to file path string
  - file_to_memory with unknown `source` / `memory_type` → defaults applied
  - scan mixed .md / .txt / .json → only parses .md
  - scan returns empty list for empty vault
  - scan recurses into nested subdirectories
  - delete_file True on existing, False on missing
  - delete_file on a directory path → False
  - delete_file accepts str path (not only Path)
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

import frontmatter
import pytest

from vesma.models import Memory, MemorySource, MemoryStatus, MemoryType
from vesma.storage.vault import VaultManager

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def vault_dir():
    """Per-test vault root under a fresh tempdir."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / "vault"


@pytest.fixture
def vm(vault_dir: Path) -> VaultManager:
    """A fresh VaultManager rooted at the test vault dir."""
    return VaultManager(vault_dir)


def _make_memory(**overrides) -> Memory:
    """Build a valid Memory with the given overrides; sensible defaults for the rest."""
    defaults: dict = dict(
        content="# Hello world\n\nSome body content.",
        title="Hello world",
        tags=["project:mnemos", "agent:qa", "mnemos:learning"],
        source=MemorySource.MANUAL,
        source_url=None,
        memory_type=MemoryType.NOTE,
        project="mnemos",
        agent="qa",
        status=MemoryStatus.PUBLISHED,
    )
    defaults.update(overrides)
    return Memory(**defaults)


# ── memory_to_file ────────────────────────────────────────────────────────────


class TestMemoryToFile:
    def test_sanitizes_special_characters_in_title(self, vm: VaultManager) -> None:
        """Special characters in title are replaced with underscores on disk."""
        # Title contains `/`, `\\`, `:`, `*`, `?` — all must be replaced.
        mem = _make_memory(title="a/b\\c:d*e?f")
        path = vm.memory_to_file(mem)
        assert path.is_file()
        # Filename uses '_' for every special char
        assert path.name == "a_b_c_d_e_f.md"

    def test_truncates_long_titles(self, vm: VaultManager) -> None:
        """A title longer than 80 chars is truncated to 80."""
        mem = _make_memory(title="x" * 100)
        path = vm.memory_to_file(mem)
        assert path.stem == "x" * 80

    def test_empty_title_falls_back_to_untitled(self, vm: VaultManager) -> None:
        """If auto_title() returns 'Untitled' (the library fallback), filename is 'Untitled.md'."""
        mem = _make_memory(title="", content="   \n   \n")  # only whitespace
        # auto_title() strips & takes first non-empty line → "Untitled"
        path = vm.memory_to_file(mem)
        assert path.name == "Untitled.md"

    def test_writes_minimal_memory(self, vm: VaultManager, vault_dir: Path) -> None:
        """A memory with no source_url, no quality_score, no cluster_id, no
        project/agent is written cleanly."""
        mem = _make_memory(
            title="Minimal",
            source_url=None,
            quality_score=None,
            cluster_id=None,
            project="",
            agent="",
            metadata={},
        )
        path = vm.memory_to_file(mem)
        assert path.is_file()
        assert path.parent == vault_dir / mem.memory_type.value
        # Read back via frontmatter
        post = frontmatter.load(str(path))
        assert post.content.strip().startswith("# Hello world")
        assert post.metadata["title"] == "Minimal"
        assert post.metadata["status"] == mem.status.value
        # project/agent only present when non-empty
        assert "project" not in post.metadata
        assert "agent" not in post.metadata

    def test_writes_full_memory_with_all_fields(self, vm: VaultManager) -> None:
        """A memory with every optional field produces matching frontmatter."""
        mem = _make_memory(
            title="Full",
            source_url="https://example.com/x",
            quality_score=0.91,
            cluster_id="cluster-abc",
            metadata={"foo": "bar", "n": 7},
        )
        path = vm.memory_to_file(mem)
        post = frontmatter.load(str(path))
        m = post.metadata
        assert m["id"] == mem.id
        assert m["source_url"] == "https://example.com/x"
        assert m["quality_score"] == 0.91
        assert m["cluster_id"] == "cluster-abc"
        assert m["extra"] == {"foo": "bar", "n": 7}
        assert m["memory_type"] == "note"
        assert m["source"] == "manual"

    def test_collision_uses_id_suffix(self, vm: VaultManager, vault_dir: Path) -> None:
        """If the target file already exists AND the memory's recorded
        file_path differs, the new file is written with an id-based suffix
        (avoiding overwrite)."""
        mem = _make_memory(
            title="Same Title",
            # Force the collision branch: memory already had a *different*
            # file_path recorded (e.g. from a prior ingest).
            file_path=str(vault_dir / "previously-elsewhere.md"),
        )
        # Pre-create a file at the sanitized path with different content
        target_dir = vault_dir / mem.memory_type.value
        target_dir.mkdir(parents=True, exist_ok=True)
        # _sanitize_filename keeps spaces, so the filename is "Same Title.md"
        blocker = target_dir / "Same Title.md"
        blocker.write_text("unrelated content", encoding="utf-8")

        path = vm.memory_to_file(mem)
        assert path != blocker
        # Filename is `<sanitized_title>_<8hex>.md`
        assert path.name.startswith("Same Title_")
        assert path.name.endswith(".md")
        assert path.stem != "Same Title"
        # The original blocker is untouched
        assert blocker.read_text(encoding="utf-8") == "unrelated content"

    def test_no_title_uses_auto_title(self, vm: VaultManager) -> None:
        """If memory.title is None, filename is derived from auto_title()."""
        mem = _make_memory(title=None, content="First line is the title")
        # auto_title() strips the leading '#' and uses the first non-empty line
        path = vm.memory_to_file(mem)
        assert path.name.startswith("First line is the title")
        assert path.suffix == ".md"

    def test_writes_tags_into_frontmatter(self, vm: VaultManager) -> None:
        """All tags appear in the frontmatter list."""
        mem = _make_memory(tags=["project:mnemos", "agent:qa", "mnemos:learning", "custom-tag"])
        path = vm.memory_to_file(mem)
        post = frontmatter.load(str(path))
        assert list(post.metadata["tags"]) == mem.tags


# ── file_to_memory ────────────────────────────────────────────────────────────


class TestFileToMemory:
    def test_parses_well_formed_file(self, vm: VaultManager) -> None:
        """A complete markdown file round-trips through file_to_memory."""
        original = _make_memory(
            title="Roundtrip",
            source_url="https://example.com/a",
            quality_score=0.7,
            cluster_id="c-1",
        )
        path = vm.memory_to_file(original)

        loaded = vm.file_to_memory(path)
        assert loaded is not None
        assert loaded.id == original.id
        assert loaded.title == "Roundtrip"
        assert loaded.source == MemorySource.MANUAL
        assert loaded.memory_type == MemoryType.NOTE
        assert loaded.source_url == "https://example.com/a"
        # project / agent preserved via frontmatter
        assert loaded.project == original.project
        assert loaded.agent == original.agent
        assert loaded.file_path == str(path)
        # content is the body after the frontmatter
        assert "Some body content" in loaded.content

    def test_non_existent_path_returns_none(self, vm: VaultManager) -> None:
        """Non-existent file → None (no exception)."""
        assert vm.file_to_memory(Path("/no/such/file.md")) is None

    def test_non_md_suffix_returns_none(self, vm: VaultManager, vault_dir: Path) -> None:
        """A file with non-`.md` extension is rejected outright."""
        p = vault_dir / "note.txt"
        p.write_text("---\nid: x\n---\nbody", encoding="utf-8")
        assert vm.file_to_memory(p) is None

    def test_invalid_yaml_returns_none(self, vm: VaultManager, vault_dir: Path) -> None:
        """Frontmatter parser failure → None."""
        p = vault_dir / "bad.md"
        # Unbalanced quotes inside frontmatter triggers a parser error
        p.write_text('---\ntitle: "unterminated\n---\nbody', encoding="utf-8")
        assert vm.file_to_memory(p) is None

    def test_empty_content_returns_none(self, vm: VaultManager, vault_dir: Path) -> None:
        """A file with only frontmatter and no body content → None."""
        p = vault_dir / "empty.md"
        p.write_text("---\nid: x\ntitle: y\n---\n", encoding="utf-8")
        assert vm.file_to_memory(p) is None

    def test_whitespace_only_content_returns_none(self, vm: VaultManager, vault_dir: Path) -> None:
        """A file whose body is only whitespace → None."""
        p = vault_dir / "ws.md"
        p.write_text("---\nid: x\n---\n   \n\n  \n", encoding="utf-8")
        assert vm.file_to_memory(p) is None

    def test_invalid_date_falls_back_to_now(self, vm: VaultManager, vault_dir: Path) -> None:
        """A non-ISO `created`/`updated` value is silently replaced with now()."""
        p = vault_dir / "baddate.md"
        p.write_text(
            "---\nid: x\ncreated: not-a-date\nupdated: also-bad\n---\nbody",
            encoding="utf-8",
        )
        mem = vm.file_to_memory(p)
        assert mem is not None
        # We can't assert exact time, only that we got a recent datetime
        assert isinstance(mem.created_at, datetime)
        assert isinstance(mem.updated_at, datetime)

    def test_missing_id_falls_back_to_file_path(self, vm: VaultManager, vault_dir: Path) -> None:
        """When frontmatter has no `id`, the file path string is used as id."""
        p = vault_dir / "noid.md"
        p.write_text("---\ntitle: No Id\n---\nbody", encoding="utf-8")
        mem = vm.file_to_memory(p)
        assert mem is not None
        assert mem.id == str(p)

    def test_unknown_source_falls_back_to_obsidian(self, vm: VaultManager, vault_dir: Path) -> None:
        """If frontmatter `source` is missing, defaults to 'obsidian'."""
        p = vault_dir / "nosrc.md"
        p.write_text("---\nid: z\n---\nbody", encoding="utf-8")
        mem = vm.file_to_memory(p)
        assert mem is not None
        assert mem.source == MemorySource.OBSIDIAN

    def test_unknown_memory_type_falls_back_to_note(
        self, vm: VaultManager, vault_dir: Path
    ) -> None:
        """If frontmatter `memory_type` is missing, defaults to 'note'."""
        p = vault_dir / "nomtype.md"
        p.write_text("---\nid: z\n---\nbody", encoding="utf-8")
        mem = vm.file_to_memory(p)
        assert mem is not None
        assert mem.memory_type == MemoryType.NOTE


# ── scan ──────────────────────────────────────────────────────────────────────


class TestScan:
    def test_empty_vault_returns_empty(self, vm: VaultManager) -> None:
        """An empty vault root returns an empty list."""
        assert vm.scan() == []

    def test_skips_non_md_files(self, vm: VaultManager, vault_dir: Path) -> None:
        """Only `.md` files are picked up; .txt / .json are skipped."""
        sub = vault_dir / "notes"
        sub.mkdir(parents=True)
        (sub / "a.md").write_text("---\nid: a\n---\nA", encoding="utf-8")
        (sub / "b.md").write_text("---\nid: b\n---\nB", encoding="utf-8")
        (sub / "ignore.txt").write_text("plain text", encoding="utf-8")
        (sub / "data.json").write_text('{"id": "x"}', encoding="utf-8")
        (sub / "config.yaml").write_text("key: value", encoding="utf-8")

        result = vm.scan()
        ids = {m.id for m in result}
        assert ids == {"a", "b"}
        assert len(result) == 2

    def test_skips_md_files_with_no_body(self, vm: VaultManager, vault_dir: Path) -> None:
        """`.md` files with empty body are filtered out by file_to_memory."""
        (vault_dir / "ok.md").write_text("---\nid: ok\n---\nbody", encoding="utf-8")
        (vault_dir / "empty.md").write_text("---\nid: empty\n---\n", encoding="utf-8")
        result = vm.scan()
        assert {m.id for m in result} == {"ok"}

    def test_scan_recurses_into_subdirs(self, vm: VaultManager, vault_dir: Path) -> None:
        """`rglob` semantics — scan descends into subdirectories of the vault."""
        deep = vault_dir / "deep" / "nested" / "deeper"
        deep.mkdir(parents=True)
        (deep / "found.md").write_text("---\nid: deep\n---\ndeep body", encoding="utf-8")
        result = vm.scan()
        assert len(result) == 1
        assert result[0].id == "deep"


# ── delete_file ───────────────────────────────────────────────────────────────


class TestDeleteFile:
    def test_returns_true_for_existing_file(self, vm: VaultManager, vault_dir: Path) -> None:
        """An existing file is removed and the call returns True."""
        p = vault_dir / "del.md"
        p.write_text("body", encoding="utf-8")
        assert vm.delete_file(p) is True
        assert not p.exists()

    def test_returns_false_for_missing_file(self, vm: VaultManager) -> None:
        """A non-existent path returns False (no exception)."""
        assert vm.delete_file(Path("/no/such/file.md")) is False

    def test_returns_false_for_directory(self, vm: VaultManager, vault_dir: Path) -> None:
        """A directory is not a file — delete_file returns False and does not remove it."""
        d = vault_dir / "sub"
        d.mkdir()
        assert vm.delete_file(d) is False
        assert d.exists()

    def test_accepts_string_path(self, vm: VaultManager, vault_dir: Path) -> None:
        """`str` input is coerced to Path internally."""
        p = vault_dir / "str.md"
        p.write_text("body", encoding="utf-8")
        assert vm.delete_file(str(p)) is True
        assert not p.exists()
