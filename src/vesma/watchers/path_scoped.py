"""M8 — Path-scoped rules ingest.

Watches `.github/instructions/*.instructions.md` (or any `*.instructions.md`)
and creates Memory entries with:
  - source = MemorySource.RULE
  - status = MemoryStatus.PUBLISHED
  - tags   = vesma:rule, project:<repo>, applyTo:<glob>, source:path-scoped-rule

On file change → update memory. On delete → remove memory + vector entry.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any

import yaml

from vesma.manager import MemoryManager
from vesma.models import MemoryCreate, MemorySource, MemoryStatus

logger = logging.getLogger(__name__)

# ── Frontmatter parsing ──────────────────────────────────────────────────────


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter and body from markdown text.

    Returns (frontmatter_dict, body). If no frontmatter found,
    returns ({}, text).
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    # Find closing ---
    end_idx = -1
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = i
            break

    if end_idx == -1:
        return {}, text

    yaml_text = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1 :])

    try:
        frontmatter = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError:
        frontmatter = {}

    return frontmatter, body


def parse_rule_file(path: Path) -> dict[str, Any]:
    """Parse a `.instructions.md` file.

    Returns a dict with:
      - title: str | None
      - description: str | None
      - apply_to: list[str]
      - body: str
      - source_url: str (the file path)
    """
    text = path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(text)

    # Extract title from first H1 if present
    title: str | None = None
    body_lines = body.splitlines()
    h1_idx = -1
    for i, line in enumerate(body_lines):
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            h1_idx = i
            break

    # Remove H1 line from body so it doesn't duplicate the title
    if h1_idx >= 0:
        body_lines.pop(h1_idx)
        # Also remove any immediately following blank lines
        while body_lines and not body_lines[0].strip():
            body_lines.pop(0)
        body = "\n".join(body_lines)

    # applyTo can be a string or list of strings
    apply_to_raw = frontmatter.get("applyTo", "**")
    if isinstance(apply_to_raw, str):
        apply_to = [apply_to_raw]
    elif isinstance(apply_to_raw, list):
        apply_to = [str(g) for g in apply_to_raw]
    else:
        apply_to = ["**"]

    description = frontmatter.get("description", "")

    # Fallback title: filename without .instructions.md suffix
    fallback_title = path.name
    if fallback_title.endswith(".instructions.md"):
        fallback_title = fallback_title[: -len(".instructions.md")]
    elif fallback_title.endswith(".md"):
        fallback_title = fallback_title[:-3]

    return {
        "title": title or fallback_title,
        "description": description,
        "apply_to": apply_to,
        "body": body.strip(),
        "source_url": str(path.resolve()),
    }


# ── Ingest / remove ────────────────────────────────────────────────────────


def ingest_rule(
    manager: MemoryManager,
    file_path: Path,
    *,
    project: str = "",
    agent: str = "",
) -> dict[str, Any]:
    """Ingest a single `.instructions.md` file as a published Memory.

    If a memory already exists for this file_path, it is updated.
    """
    parsed = parse_rule_file(file_path)

    # Build tags
    tags: list[str] = ["vesma:rule", "source:path-scoped-rule"]
    if project:
        tags.append(f"project:{project}")
    if agent:
        tags.append(f"agent:{agent}")
    for glob in parsed["apply_to"]:
        tags.append(f"applyTo:{glob}")

    # Check if already exists by source_url
    existing = manager.sqlite.get_by_source_url(parsed["source_url"])

    content = parsed["body"]
    if parsed["description"]:
        content = f"{parsed['description']}\n\n{content}"

    data = MemoryCreate(
        content=content,
        title=parsed["title"],
        tags=tags,
        source=MemorySource.RULE,
        source_url=parsed["source_url"],
        status=MemoryStatus.PUBLISHED,
        metadata={
            "apply_to": parsed["apply_to"],
            "description": parsed["description"],
        },
    )

    if existing:
        # Update existing memory
        from vesma.models import MemoryUpdate

        update_data = MemoryUpdate(
            content=data.content,
            title=data.title,
            tags=data.tags,
            status=MemoryStatus.PUBLISHED,
            metadata=data.metadata,
        )
        memory = manager.update(existing.id, update_data)
        action = "updated"
    else:
        memory = manager.add(data, project=project, agent=agent)
        action = "created"

    return {
        "action": action,
        "memory_id": memory.id if memory else None,
        "title": parsed["title"],
        "apply_to": parsed["apply_to"],
    }


def remove_rule(manager: MemoryManager, file_path: Path) -> dict[str, Any]:
    """Remove the Memory associated with a rule file.

    Returns {"removed": bool, "memory_id": str | None}.
    """
    source_url = str(file_path.resolve())
    existing = manager.sqlite.get_by_source_url(source_url)

    if existing:
        manager.delete(existing.id)
        return {"removed": True, "memory_id": existing.id}

    return {"removed": False, "memory_id": None}


# ── Batch operations ─────────────────────────────────────────────────────────


def ingest_path_scoped_rules(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    """Vitals boundary #9 (A2): time the rules ingest."""
    import time as _time

    t0 = _time.monotonic()
    try:
        result = _ingest_path_scoped_rules_impl(*args, **kwargs)
    except Exception as exc:
        manager = args[0] if args else kwargs.get("manager")
        if manager is not None:
            with contextlib.suppress(Exception):
                # Vitals boundary #9 (A2) — error leg.
                manager.record_verb_vitals(
                    surface="background",
                    verb="watcher.ingest",
                    status="error",
                    latency_ms=(_time.monotonic() - t0) * 1000,
                    meta={"error_type": type(exc).__name__},
                )
        raise
    with contextlib.suppress(Exception):
        manager = args[0] if args else kwargs.get("manager")
        if manager is not None:
            manager.record_verb_vitals(
                surface="background",
                verb="watcher.ingest",
                status="ok",
                latency_ms=(_time.monotonic() - t0) * 1000,
                meta={"counters": {"rules": len(result)}},
            )
    return result


def _ingest_path_scoped_rules_impl(
    manager: MemoryManager,
    rules_dir: Path,
    *,
    project: str = "",
    agent: str = "",
    pattern: str = "*.instructions.md",
) -> list[dict[str, Any]]:
    """Scan a directory for `*.instructions.md` files and ingest them all.

    Returns a list of per-file result dicts.
    """
    results: list[dict[str, Any]] = []
    if not rules_dir.exists():
        logger.warning("Rules directory does not exist: %s", rules_dir)
        return results

    for path in sorted(rules_dir.rglob(pattern)):
        try:
            result = ingest_rule(manager, path, project=project, agent=agent)
            results.append(result)
        except Exception as exc:
            logger.error("Failed to ingest rule %s: %s", path, exc)
            results.append({"action": "error", "path": str(path), "error": str(exc)})

    logger.info("ingest_path_scoped_rules: %d files processed", len(results))
    return results


def remove_path_scoped_rule(
    manager: MemoryManager,
    file_path: Path,
) -> dict[str, Any]:
    """Remove a single rule by file path."""
    return remove_rule(manager, file_path)
