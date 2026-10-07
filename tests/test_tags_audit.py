"""``vesma tags audit`` — tag-contract conformance scan + additive heal.

Board card vesma-doctor-fixes-467 (wave W-B). Covers:

* ``_audit_heal_tags`` — the pure heal policy (additive-only, project
  column slug, ``project:unsorted`` fallback, corrupt JSON).
* The CLI subcommand: dry-run lists without writing; ``--apply`` adds
  exactly the missing prefixes; idempotent second run; ``--limit`` caps
  the LISTING (the scan still covers the store); ``--json`` for both
  modes.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from vesma.cli.main import _audit_heal_tags, app
from vesma.config import Settings
from vesma.models import Memory
from vesma.storage.sqlite_store import SQLiteStore

runner = CliRunner()

# ── _audit_heal_tags — the pure heal policy ───────────────────────────────────


def test_heal_conformant_row_is_untouched() -> None:
    """A row with all three prefixes is conformant → nothing missing."""
    healed, missing, unparseable = _audit_heal_tags(
        '["project:vesma", "agent:tech-writer", "mnemos:decision"]', "vesma"
    )
    assert missing == []
    assert healed == ["project:vesma", "agent:tech-writer", "mnemos:decision"]
    assert unparseable is False


def test_heal_adds_exactly_the_missing_prefixes() -> None:
    """Only the missing prefixes are appended; existing tags survive."""
    healed, missing, _ = _audit_heal_tags('["project:p1", "agent:coder"]', "p1")
    assert missing == ["vesma:*"]
    assert healed == ["project:p1", "agent:coder", "vesma:legacy"]


def test_heal_project_from_column_slug_normalized() -> None:
    """Missing project:* → the row's project column, slug-normalized."""
    healed, missing, _ = _audit_heal_tags('["agent:coder", "mnemos:rule"]', "My Project")
    assert missing == ["project:*"]
    assert "project:my-project" in healed


def test_heal_project_empty_column_falls_back_to_unsorted() -> None:
    """Empty project column → project:unsorted."""
    healed, missing, _ = _audit_heal_tags('["agent:coder", "mnemos:rule"]', "")
    assert missing == ["project:*"]
    assert "project:unsorted" in healed


def test_heal_corrupt_tags_json_is_flagged_and_fully_healed() -> None:
    """Unparseable tags JSON → non-conformant; heal = the three prefixes."""
    healed, missing, unparseable = _audit_heal_tags("{not json", "col")
    assert unparseable is True
    assert missing == ["project:*", "agent:*", "vesma:*"]
    assert healed == ["project:col", "agent:user", "vesma:legacy"]


def test_heal_non_string_json_list_is_corrupt() -> None:
    """A JSON value that is not a list of strings counts as corrupt too."""
    _, missing, unparseable = _audit_heal_tags("[1, 2]", "")
    assert unparseable is True
    assert len(missing) == 3


# ── CLI fixtures ──────────────────────────────────────────────────────────────


def _seed_memory(store: SQLiteStore, content: str, tags: list[str], project: str = "") -> Memory:
    mem = Memory(content=content, tags=tags, project=project, agent="")
    store.save(mem)
    return mem


def _raw_tags(db_path: Path, memory_id: str) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT tags FROM memories WHERE id=?", (memory_id,)).fetchone()
    finally:
        conn.close()
    return str(row[0]) if row else ""


@pytest.fixture
def audit_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Isolated config + a seeded store; the CLI reads the same DB."""
    from vesma.cli._manager import reset_manager

    reset_manager()
    cfg = tmp_path / "vesma.yaml"
    cfg.write_text(
        f"vesma:\n"
        f"  vault_path: {tmp_path / 'vault'}\n"
        f"  data_dir: {tmp_path / 'data'}\n"
        f"  db_name: audit-test.db\n"
        f"embedding:\n"
        f"  provider: nano\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VESMA_CONFIG", str(cfg))

    settings = Settings(
        vesma={
            "vault_path": str(tmp_path / "vault"),
            "data_dir": str(tmp_path / "data"),
            "db_name": "audit-test.db",
        },
        embedding={"provider": "nano"},
    )
    settings.resolve_paths()
    store = SQLiteStore(settings.db_path)

    ids = {
        "conformant": _seed_memory(
            store, "conformant entry", ["project:vesma", "agent:tech-writer", "mnemos:decision"]
        ).id,
        "missing-all": _seed_memory(store, "missing every prefix", [], project="My Project").id,
        "missing-mnemos": _seed_memory(
            store, "missing only mnemos", ["project:p1", "agent:coder"]
        ).id,
        "missing-project": _seed_memory(
            store, "missing only project", ["agent:user", "mnemos:rule"]
        ).id,
    }
    # A row with a corrupt tags JSON column (raw SQL — the model layer
    # refuses to build it). project column stays readable ("c1").
    corrupt = _seed_memory(
        store, "corrupt tags row", ["project:c1", "agent:bot", "mnemos:rule"], project="c1"
    )
    conn = sqlite3.connect(str(settings.db_path))
    try:
        conn.execute("UPDATE memories SET tags='{not json' WHERE id=?", (corrupt.id,))
        conn.commit()
    finally:
        conn.close()
    ids["corrupt"] = corrupt.id

    yield {"cfg": cfg, "db_path": settings.db_path, "ids": ids}
    reset_manager()


# ── CLI: dry-run ──────────────────────────────────────────────────────────────


def test_audit_dry_run_lists_without_writing(audit_store: dict[str, Any]) -> None:
    """Default mode lists non-conformant entries and writes NOTHING."""
    result = runner.invoke(app, ["tags", "audit"])
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output
    for key in ("missing-all", "missing-mnemos", "missing-project", "corrupt"):
        assert audit_store["ids"][key][:8] in result.output, key
    # The conformant entry is not listed.
    assert audit_store["ids"]["conformant"][:8] not in result.output
    # Nothing was written — tags byte-identical.
    assert _raw_tags(audit_store["db_path"], audit_store["ids"]["missing-all"]) == "[]"
    assert (
        _raw_tags(audit_store["db_path"], audit_store["ids"]["missing-mnemos"])
        == '["project:p1", "agent:coder"]'
    )


def test_audit_dry_run_json(audit_store: dict[str, Any]) -> None:
    """``--json`` dry-run: counts + rows, no heal bookkeeping."""
    result = runner.invoke(app, ["tags", "audit", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["mode"] == "dry-run"
    assert payload["scanned"] == 5
    assert payload["non_conformant"] == 4
    assert "healed" not in payload
    by_snippet = {r["snippet"]: r for r in payload["rows"]}
    corrupt = by_snippet["corrupt tags row"]
    assert corrupt["missing"] == ["project:*", "agent:*", "vesma:*"]
    assert corrupt["tags"] == "(unparseable)"


def test_audit_limit_caps_listing_but_not_scan(audit_store: dict[str, Any]) -> None:
    """``--limit 2`` shows 2 rows + a tail; the scan still covers all 5."""
    result = runner.invoke(app, ["tags", "audit", "--limit", "2"])
    assert result.exit_code == 0
    assert "…and 2 more" in result.output
    payload = json.loads(runner.invoke(app, ["tags", "audit", "--limit", "2", "--json"]).output)
    assert len(payload["rows"]) == 2
    assert payload["non_conformant"] == 4


# ── CLI: apply ────────────────────────────────────────────────────────────────


def test_audit_apply_adds_exactly_missing_prefixes(audit_store: dict[str, Any]) -> None:
    """``--apply`` heals: project column slug, agent:user, mnemos:legacy."""
    result = runner.invoke(app, ["tags", "audit", "--apply"])
    assert result.exit_code == 0, result.output
    assert "Healed:" in result.output
    db = audit_store["db_path"]
    ids = audit_store["ids"]

    # missing-all: all three prefixes, project from the column slug.
    tags_all = json.loads(_raw_tags(db, ids["missing-all"]))
    assert tags_all == ["project:my-project", "agent:user", "vesma:legacy"]

    # missing-mnemos: ONLY mnemos:legacy added, existing tags preserved.
    tags_mnemos = json.loads(_raw_tags(db, ids["missing-mnemos"]))
    assert tags_mnemos == ["project:p1", "agent:coder", "vesma:legacy"]

    # missing-project: empty project column → project:unsorted.
    tags_project = json.loads(_raw_tags(db, ids["missing-project"]))
    assert tags_project == ["agent:user", "mnemos:rule", "project:unsorted"]

    # corrupt: unparseable → nothing salvageable → the three prefixes,
    # project from the (readable) column.
    tags_corrupt = json.loads(_raw_tags(db, ids["corrupt"]))
    assert tags_corrupt == ["project:c1", "agent:user", "vesma:legacy"]


def test_audit_apply_is_idempotent(audit_store: dict[str, Any]) -> None:
    """A second ``--apply`` run heals nothing (idempotent by construction)."""
    first = runner.invoke(app, ["tags", "audit", "--apply", "--json"])
    assert first.exit_code == 0
    payload = json.loads(first.output)
    assert payload["healed"] == 4

    second = runner.invoke(app, ["tags", "audit", "--apply", "--json"])
    assert second.exit_code == 0
    payload2 = json.loads(second.output)
    assert payload2["non_conformant"] == 0
    assert payload2["healed"] == 0


def test_audit_apply_json_summary(audit_store: dict[str, Any]) -> None:
    """``--apply --json`` carries per-prefix add counts."""
    result = runner.invoke(app, ["tags", "audit", "--apply", "--json"])
    payload = json.loads(result.output)
    assert payload["mode"] == "applied"
    assert payload["healed"] == 4
    assert payload["tags_added"] == {
        "project:*": 3,
        "agent:*": 2,
        "vesma:*": 3,
    }
