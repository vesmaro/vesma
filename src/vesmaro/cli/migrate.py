"""M13 — Migration CLI: ai-brain → Vesma.

Converts legacy ai-brain SQLite DB + vault into Vesma format.
Key transformations:
  - tags: add project:legacy, agent:unknown, mnemos:legacy
  - status: preserved (raw/processing/processed/published/archived)
  - source: ai-brain TELEGRAM → Vesma MCP (closest match)
  - Memory fields: parent_ids → derived_from, content_ru/content_en → metadata
  - Config: BrainConfig → VesmaConfig (paths updated)
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vesmaro.config import Settings, load_settings
from vesmaro.manager import MemoryManager
from vesmaro.models import MemoryCreate, MemorySource, MemoryStatus, MemoryType

logger = logging.getLogger(__name__)

# ── Legacy ai-brain schema mapping ───────────────────────────────────────────

_LEGACY_TO_VESMARO_SOURCE: dict[str, MemorySource] = {
    "manual": MemorySource.MANUAL,
    "telegram": MemorySource.MCP,  # closest match
    "web": MemorySource.WEB,
    "file": MemorySource.FILE,
    "mcp": MemorySource.MCP,
    "obsidian": MemorySource.OBSIDIAN,
    "cli": MemorySource.CLI,
}

_LEGACY_TO_VESMARO_TYPE: dict[str, MemoryType] = {
    "note": MemoryType.NOTE,
    "fact": MemoryType.FACT,
    "snippet": MemoryType.SNIPPET,
    "bookmark": MemoryType.BOOKMARK,
    "conversation": MemoryType.CONVERSATION,
    "session_context": MemoryType.SESSION_CONTEXT,
}

_LEGACY_TO_VESMARO_STATUS: dict[str, MemoryStatus] = {
    "raw": MemoryStatus.RAW,
    "processing": MemoryStatus.PROCESSING,
    "processed": MemoryStatus.PROCESSED,
    "published": MemoryStatus.PUBLISHED,
    "archived": MemoryStatus.ARCHIVED,
}


def _migrate_tags(old_tags: list[str]) -> list[str]:
    """Add Vesma contract tags to legacy tags and migrate gcw: → mnemos:."""
    tags = []
    for t in old_tags:
        # Migrate legacy gcw: tags → mnemos:
        if t.startswith("gcw:"):
            tags.append(f"mnemos:{t[4:]}")
        else:
            tags.append(t)
    if not any(t.startswith("project:") for t in tags):
        tags.append("project:legacy")
    if not any(t.startswith("agent:") for t in tags):
        tags.append("agent:unknown")
    if not any(t.startswith("mnemos:") for t in tags):
        tags.append("mnemos:legacy")
    return tags


def migrate_gcw_to_mnemos_tags(db_path: Path) -> dict[str, int]:
    """Migrate existing gcw: tags in the Vesma DB to mnemos: tags.

    Converts all tag arrays in the memories table that contain ``gcw:<subtype>``
    entries to ``mnemos:<subtype>``. Idempotent — safe to run multiple times.

    Returns:
        Summary dict with counts: ``{"memories_updated": N, "tags_converted": N}``
    """
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    updated = 0
    converted = 0
    try:
        cur = conn.execute("SELECT id, tags FROM memories")
        for row in cur.fetchall():
            tags = json.loads(row["tags"]) if row["tags"] else []
            new_tags = [f"mnemos:{t[4:]}" if t.startswith("gcw:") else t for t in tags]
            if new_tags != tags:
                conn.execute(
                    "UPDATE memories SET tags = ? WHERE id = ?",
                    (json.dumps(new_tags), row["id"]),
                )
                updated += 1
                converted += sum(1 for t in tags if t.startswith("gcw:"))
        conn.commit()
    finally:
        conn.close()
    logger.info("Migrated %d tags across %d memories (gcw: → mnemos:)", converted, updated)
    return {"memories_updated": updated, "tags_converted": converted}


def _migrate_memory(row: sqlite3.Row) -> MemoryCreate:
    """Convert a legacy ai-brain DB row into Vesma MemoryCreate."""
    raw_tags = json.loads(row["tags"]) if row["tags"] else []
    tags = _migrate_tags(raw_tags)

    # Migrate source
    raw_source = row["source"] or "manual"
    source = _LEGACY_TO_VESMARO_SOURCE.get(raw_source, MemorySource.MANUAL)

    # Migrate type
    raw_type = row["memory_type"] or "note"
    memory_type = _LEGACY_TO_VESMARO_TYPE.get(raw_type, MemoryType.NOTE)

    # Migrate status
    raw_status = row["status"] or "raw"
    status = _LEGACY_TO_VESMARO_STATUS.get(raw_status, MemoryStatus.RAW)

    # Build metadata from legacy fields not present in Vesma
    metadata: dict[str, Any] = {}
    if row["content_ru"]:
        metadata["content_ru"] = row["content_ru"]
    if row["content_en"]:
        metadata["content_en"] = row["content_en"]
    if row["parent_ids"]:
        metadata["parent_ids"] = json.loads(row["parent_ids"])

    # content_ru/content_en as primary content if available
    content = row["content"] or ""
    if not content.strip() and metadata.get("content_ru"):
        content = metadata["content_ru"]
    elif not content.strip() and metadata.get("content_en"):
        content = metadata["content_en"]

    return MemoryCreate(
        content=content,
        title=row["title"] or None,
        tags=tags,
        source=source,
        source_url=row["source_url"] or None,
        memory_type=memory_type,
        metadata=metadata,
        category=row["category"] or None,
        status=status,
    )


# ── Migration runner ─────────────────────────────────────────────────────────


def migrate_from_ai_brain(
    source_db: Path,
    source_vault: Path | None = None,
    *,
    dry_run: bool = False,
    backup: bool = True,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Migrate ai-brain SQLite DB (and optional vault) into Vesma.

    Args:
        source_db: Path to ai-brain SQLite file (e.g. ~/.ai-brain/ai_brain.db).
        source_vault: Optional path to ai-brain vault dir (e.g. ~/brain-vault).
        dry_run: If True, only report what would be migrated without writing.
        backup: If True and not dry_run, backup existing Vesma DB before migration.
        settings: Vesma settings to use. If None, loads default.

    Returns:
        Summary dict with counts and any errors.
    """
    if not source_db.exists():
        raise FileNotFoundError(f"Source DB not found: {source_db}")

    if settings is None:
        settings = load_settings()
        settings.resolve_paths()
        settings.apply_runtime_env()

    # Backup existing Vesma DB if it exists
    mnemos_db = settings.db_path
    if backup and not dry_run and mnemos_db.exists():
        ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        backup_path = mnemos_db.with_suffix(f".db.backup-{ts}")
        shutil.copy2(mnemos_db, backup_path)
        logger.info("Backed up existing Vesma DB to %s", backup_path)

    manager = MemoryManager(settings)

    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "source_db": str(source_db),
        "source_vault": str(source_vault) if source_vault else None,
        "memories_migrated": 0,
        "vault_files_migrated": 0,
        "errors": [],
    }

    # ── Migrate SQLite memories ────────────────────────────────────────────
    conn = sqlite3.connect(str(source_db))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute("SELECT * FROM memories")
        rows = cur.fetchall()
        for row in rows:
            try:
                data = _migrate_memory(row)
                project = next(
                    (t[len("project:") :] for t in data.tags if t.startswith("project:")),
                    "legacy",
                )
                agent = next(
                    (t[len("agent:") :] for t in data.tags if t.startswith("agent:")),
                    "unknown",
                )
                if not dry_run:
                    manager.add(
                        data, project=project, agent=agent, mint_relates_to=False
                    )  # #322 review M2: bulk import is machine traffic, not fuel
                summary["memories_migrated"] += 1
            except Exception as exc:
                logger.warning("Failed to migrate memory %s: %s", row.get("id", "?"), exc)
                summary["errors"].append(str(exc))
    finally:
        conn.close()

    # ── Migrate vault files ──────────────────────────────────────────────
    if source_vault and source_vault.exists():
        for md_file in source_vault.rglob("*.md"):
            try:
                if not dry_run:
                    # Copy file into Vesma vault, preserving relative path
                    rel = md_file.relative_to(source_vault)
                    target = settings.mnemos.vault_path / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(md_file, target)
                summary["vault_files_migrated"] += 1
            except Exception as exc:
                logger.warning("Failed to migrate vault file %s: %s", md_file, exc)
                summary["errors"].append(str(exc))

    if not dry_run:
        manager.close()

    return summary
