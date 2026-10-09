"""Obsidian-compatible vault integration — read/write markdown with YAML frontmatter.

Stores pipeline fields (status, quality_score, cluster_id) and project + agent
(Vesma tag contract) in frontmatter for searchability. Default path: ~/.vesma/vault/.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import frontmatter

from vesma.fs_hardening import ensure_private_dir, harden_file
from vesma.models import Memory, MemorySource, MemoryType

logger = logging.getLogger(__name__)


class VaultManager:
    """Manages the Obsidian-compatible markdown vault."""

    def __init__(self, vault_path: Path) -> None:
        self.vault_path = vault_path
        # Cascade fix 2026-10-09: 0700 on every created level (the plain
        # mkdir(parents=True) left the vault under the process umask).
        ensure_private_dir(self.vault_path)

    # ── helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _sanitize_filename(title: str) -> str:
        safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in title)
        return (safe.strip()[:80]) or "untitled"

    def _memory_dir(self, memory: Memory) -> Path:
        return self.vault_path / memory.memory_type.value

    # ── write ─────────────────────────────────────────────────────────────

    def memory_to_file(self, memory: Memory) -> Path:
        """Write memory as a markdown file with YAML frontmatter. Returns file path."""
        target_dir = self._memory_dir(memory)
        ensure_private_dir(target_dir)

        filename = self._sanitize_filename(memory.auto_title())
        file_path = target_dir / f"{filename}.md"

        # Avoid overwriting unrelated files
        if file_path.exists() and memory.file_path and str(file_path) != memory.file_path:
            file_path = target_dir / f"{filename}_{memory.id[:8]}.md"

        post = frontmatter.Post(memory.content)
        m = post.metadata
        m["id"] = memory.id
        m["title"] = memory.auto_title()
        m["tags"] = memory.tags
        m["source"] = memory.source.value
        if memory.source_url:
            m["source_url"] = memory.source_url
        m["memory_type"] = memory.memory_type.value
        m["created"] = memory.created_at.isoformat()
        m["updated"] = memory.updated_at.isoformat()
        # Vesma contract
        if memory.project:
            m["project"] = memory.project
        if memory.agent:
            m["agent"] = memory.agent
        # Pipeline status
        m["status"] = memory.status.value
        if memory.quality_score is not None:
            m["quality_score"] = memory.quality_score
        if memory.cluster_id:
            m["cluster_id"] = memory.cluster_id
        if memory.metadata:
            m["extra"] = memory.metadata

        file_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        # Cascade fix 2026-10-09: vault markdown carries the same content as
        # the db — keep it 0600 regardless of the writer's umask.
        harden_file(file_path)
        return file_path

    # ── read ──────────────────────────────────────────────────────────────

    def file_to_memory(self, file_path: Path) -> Memory | None:
        """Read a markdown file and parse into Memory. Returns None if invalid."""
        if not file_path.is_file() or file_path.suffix != ".md":
            return None
        try:
            post = frontmatter.load(str(file_path))
        except (ValueError, TypeError, KeyError, OSError):
            return None
        # python-frontmatter can raise yaml.scanner.ScannerError /
        # yaml.parser.ParserError on malformed YAML — these inherit from
        # yaml.YAMLError which is NOT a ValueError, so we catch them
        # explicitly. (M18: regression test for invalid YAML frontmatter.)
        except Exception as exc:
            logger.warning("failed to parse frontmatter in %s: %s", file_path, exc)
            return None

        # `python-frontmatter` exposes `post.metadata` as `Any` (untyped
        # library stub). For our Vesma/Obsidian vault contract the metadata
        # is always a YAML mapping, so the cast is sound — it lets mypy
        # treat the subsequent `.get(...)` calls as dict[str, Any] access.
        meta = cast("dict[str, Any]", post.metadata)
        content = post.content
        if not content.strip():
            return None

        return Memory(
            id=meta.get("id", str(file_path)),
            content=content,
            title=meta.get("title"),
            tags=meta.get("tags", []),
            source=MemorySource(meta.get("source", "obsidian")),
            source_url=meta.get("source_url"),
            memory_type=MemoryType(meta.get("memory_type", "note")),
            created_at=_parse_dt(meta.get("created")),
            updated_at=_parse_dt(meta.get("updated")),
            metadata=meta.get("extra", {}),
            file_path=str(file_path),
            project=meta.get("project", ""),
            agent=meta.get("agent", ""),
        )

    def scan(self) -> list[Memory]:
        """Recursively read all markdown files in the vault."""
        memories: list[Memory] = []
        for md_file in sorted(self.vault_path.rglob("*.md")):
            m = self.file_to_memory(md_file)
            if m is not None:
                memories.append(m)
        return memories

    def delete_file(self, file_path: str | Path) -> bool:
        path = Path(file_path)
        if path.exists() and path.is_file():
            path.unlink()
            return True
        return False


def _parse_dt(value: str | None) -> datetime:
    if value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now(UTC)
