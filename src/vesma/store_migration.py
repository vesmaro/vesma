"""Store mover 5.x → 6.0 — the ``vesma migrate-store`` command core (B3).

Moves a 5.x-era store home (``mnemos.db`` under ``data/``, vault, legacy
``mnemos:`` tag namespace, ``project:mnemos`` silo slug) to the 6.0 canonical
layout (``vesma.db`` under ``data/``, re-slagged ``vesma:*`` tags, the
``project:vesma`` silo) and rewrites the config file onto the canonical
``vesma:`` settings section.

Safety contract (normative — docs/release/6.0.0-store-migration-draft.md +
owner directive 2026-10-07; deviations resolved toward the SAFER variant and
flagged in the delivery report):

1. explicit-only: both ``--from`` and ``--to`` are REQUIRED paths; the mover
   NEVER guesses the live store. An omitted ``--from`` triggers a READ-ONLY
   discovery that can only SUGGEST candidates — it never proceeds.
2. dry-run by default: planning + counters, zero writes anywhere. Writes
   happen only under an explicit ``--apply``.
3. snapshot-gate: before any write a full consistent copy of the source
   (SQLite backup API per database file) lands in
   ``<to>/../migrate-snapshot-<ts>/`` and is verified (open + counters)
   BEFORE the migration continues. The target is materialized FROM the
   verified snapshot, never from the (possibly racy) live source.
4. quiesce gate: live connections on the source databases (write-lock held,
   WAL growth between samples) refuse the run with a "stop the vesma
   service" message. A frozen-name unix socket merely warns (stale sockets
   are common); the database probes decide.
5. zero-config fork refusal: without an explicit ``--from`` and without ONE
   unambiguous candidate the mover refuses with an explanation — it never
   picks a "similar" store.
6. discovery-fallback is read-only: an old home found on disk is offered as
   a suggested ``--from``, nothing is touched.
7. data re-slag (owner "clean sheet" ruling): exact-prefix rewrites in the
   tags field (and its denormalizations: the ``project`` column, the FTS
   mirror) — ``mnemos:<subtype>`` → ``vesma:<subtype>`` and
   ``project:mnemos`` → ``project:vesma``. Everything else stays
   byte-for-byte. SAFER-variant deviation from the owner brief: the
   ``mnemos:no-federate`` trust marker is NEVER rewritten here — the 6.0.0
   code at the mover's base commit reads it byte-stable
   (``NO_FEDERATE_TAG``) and rewriting it would convert the trust boundary
   into an exfiltration amplifier (draft Security condition). The kept
   marker count is reported; a marker rewrite can only ride the atomic
   code+data wave that flips the canonical prefix.
8. config migration: the legacy ``mnemos:`` YAML section maps onto the
   canonical ``vesma:`` Settings section; path values under the old home
   re-root under the new home; ``db_name`` pins ``vesma.db``; legacy env
   names are never created. The produced config is validated against the
   Settings models BEFORE anything is written; unmapped key NAMES abort
   (values are never printed).
9. output privacy: the report prints paths and numbers only — never record
   contents or values.
10. verification: pre/post equality of record counts, status breakdown,
    project-slug breakdown (mapped), id-set digest, and per-field checksums
    of 10 seeded sample records (source fields normalized by the EXPECTED
    rewrite before hashing); FTS rebuild + integrity check; sqlite
    ``quick_check`` on every moved database. An already-migrated source is
    refused politely; exit codes are documented and stable.
11. the legacy ``vesma migrate`` command (ai-brain M13) is untouched — the
    new top-level name ``migrate-store`` does not collide with it.

The target is built in a staging directory and appears at ``--to`` only
after full verification (same-FS atomic rename); a failed run leaves the
snapshot in place and removes staging. ``--to`` must not exist when the
run starts — even an empty directory is refused (choose a fresh target);
a path that appears MID-run is refused by the race guard before the rename.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import random
import shutil
import sqlite3
import stat
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

from vesma.models import NO_FEDERATE_TAG

__all__ = [
    "CANONICAL_DB_NAME",
    "CANONICAL_PROJECT_SLUG",
    "LEGACY_DB_NAME",
    "LEGACY_PROJECT_SLUG",
    "NEW_TAG_PREFIX",
    "OLD_TAG_PREFIX",
    "SNAPSHOT_DIR_PREFIX",
    "SOURCE_SOCKET",
    "TRUST_MARKER_TAG",
    "AlreadyMigratedError",
    "ConfigMigrationError",
    "DiscoveryRefusedError",
    "InvalidSourceError",
    "MigrationPlan",
    "MigrationReport",
    "QuiesceViolationError",
    "StoreMigrationError",
    "UsageError",
    "VerificationError",
    "build_plan",
    "discover_source_candidates",
    "reslag_project_slug",
    "reslag_tags_json",
    "run_migration",
]

# ── Namespace constants (single authority for the mover) ─────────────────────

OLD_TAG_PREFIX: str = "mnemos:"
NEW_TAG_PREFIX: str = "vesma:"
#: The trust marker stays byte-stable in storage (draft Security condition;
#: models.NO_FEDERATE_TAG is the single authority — re-exported for reports).
TRUST_MARKER_TAG: str = NO_FEDERATE_TAG
LEGACY_PROJECT_SLUG: str = "mnemos"
CANONICAL_PROJECT_SLUG: str = "vesma"
LEGACY_DB_NAME: str = "mnemos.db"
CANONICAL_DB_NAME: str = "vesma.db"
SNAPSHOT_DIR_PREFIX: str = "migrate-snapshot-"
#: Frozen 6.0 socket name (verdict: /run/mnemos stays for 6.0) — warning only.
SOURCE_SOCKET: Path = Path("/run/mnemos/core.sock")
_MARKER_FILE_NAME = ".migrate-store-applied.json"
_SQLITE_HEADER = b"SQLite format 3\x00"
_QUIESCE_WAL_SAMPLE_SECONDS = 0.7
_SIDECAR_SUFFIXES = ("-wal", "-shm")
#: Everything the mover writes is store data (vault content, memories,
#: config) — the project norm for secret-bearing surfaces is 0600/0700
#: (cf. metrics/sink.py, agent_tokens.py), NEVER umask defaults (CWE-732).
_PRIVATE_FILE_MODE = 0o600
_PRIVATE_DIR_MODE = 0o700
#: Home-root entries never carried as data siblings (flat pre-2.1 layout,
#: where data_dir == home): vault/ migrates through its own branch,
#: config.yaml is rewritten by migrate_config, logs/ and cache/ are not
#: store data at all.
_ROOT_NON_DATA_DIRS = frozenset({"vault", "logs", "cache"})
#: Fields checksummed for the 10-sample identity check (privacy: only the
#: digests are compared; values never leave the process).
_SAMPLE_FIELDS: tuple[str, ...] = (
    "id",
    "content",
    "title",
    "tags",
    "project",
    "agent",
    "source",
    "source_url",
    "memory_type",
    "created_at",
    "updated_at",
    "metadata",
    "file_path",
    "category",
    "status",
)
_SAMPLE_SIZE = 10
_SAMPLE_SEED = 20261007


class StoreMigrationError(Exception):
    """Base class: a migrate-store failure with a stable exit code."""

    exit_code: int = 1


class UsageError(StoreMigrationError):
    exit_code = 2


class DiscoveryRefusedError(StoreMigrationError):
    exit_code = 3


class QuiesceViolationError(StoreMigrationError):
    exit_code = 4


class SnapshotError(StoreMigrationError):
    exit_code = 5


class VerificationError(StoreMigrationError):
    exit_code = 6


class AlreadyMigratedError(StoreMigrationError):
    exit_code = 7


class InvalidSourceError(StoreMigrationError):
    exit_code = 8


class ConfigMigrationError(StoreMigrationError):
    exit_code = 9


# ── Connection helpers ────────────────────────────────────────────────────────


def _open_conn(db_path: Path) -> sqlite3.Connection:
    """Open a store database with Row access.

    Prefers a read-write open so SQLite can recover a stale ``-shm`` after a
    killed writer (the quiesce gate guarantees no live writers); falls back
    to a read-only URI open when the filesystem refuses writes.
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        conn.execute("SELECT 1").fetchone()
    except sqlite3.OperationalError:
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    assert conn is not None
    conn.row_factory = sqlite3.Row
    return conn


def _is_sqlite(path: Path) -> bool:
    """True when the file carries the SQLite magic header (WAL/SHM do not)."""
    try:
        with path.open("rb") as fh:
            return fh.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER
    except OSError:
        return False


def _is_sidecar(name: str) -> bool:
    return name.endswith(_SIDECAR_SUFFIXES)


def _harden_dir(path: Path) -> None:
    """Force 0700 on a directory the mover created (umask never widens it)."""
    path.chmod(_PRIVATE_DIR_MODE)


def _harden_file(path: Path) -> None:
    """Force 0600 on a file the mover created (umask never widens it)."""
    path.chmod(_PRIVATE_FILE_MODE)


# ── Layout resolution ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _StoreLayout:
    """Resolved paths inside one store home."""

    home: Path
    data_dir: Path
    db_path: Path  # the memories database (legacy name mnemos.db)
    vault_dir: Path
    config_path: Path  # may not exist


def _resolve_layout(home: Path) -> _StoreLayout:
    """Resolve the memories db inside a store home (5.x or migrated 6.0).

    Recognizes the legacy name (``mnemos.db``) and the canonical 6.0 name
    (``vesma.db``) so an already-migrated home is diagnosed as such
    (polite refusal) instead of "not a store home".
    """
    data_dir = home / "data"
    candidates = (
        data_dir / LEGACY_DB_NAME,
        home / LEGACY_DB_NAME,
        data_dir / CANONICAL_DB_NAME,
        home / CANONICAL_DB_NAME,
    )
    for candidate_db in candidates:
        if candidate_db.is_file():
            return _StoreLayout(
                home=home,
                data_dir=data_dir if candidate_db.parent == data_dir else home,
                db_path=candidate_db,
                vault_dir=home / "vault",
                config_path=home / "config.yaml",
            )
    raise InvalidSourceError(
        f"source home {home} does not contain a {LEGACY_DB_NAME} "
        f"or {CANONICAL_DB_NAME} (looked in data/ and the home root) — "
        "not a 5.x store home"
    )


def discover_source_candidates(home: Path | None = None) -> list[Path]:
    """Read-only discovery of plausible 5.x store homes (brief items 5-6).

    Returns every known-path candidate that actually holds a memories
    database. NEVER used to proceed automatically — the CLI only SUGGESTS
    them; an explicit ``--from`` is always required.
    """
    base = home if home is not None else Path.home()
    candidates = [
        base / ".mnemos",
        base / ".local" / "share" / "vesma" / "core",
        base / ".vesma",
    ]
    found: list[Path] = []
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        with contextlib.suppress(InvalidSourceError):
            _resolve_layout(candidate)
            found.append(candidate)
    return found


# ── Re-slag primitives ────────────────────────────────────────────────────────


class _UnparseableTagsError(Exception):
    """Internal: the tags column of one row is not a JSON list of strings."""


def reslag_tags_json(raw: str) -> tuple[str, int, int, int, int]:
    """Re-slag one ``tags`` column value (JSON list of strings).

    Exact-prefix rewrites only: ``mnemos:<subtype>`` → ``vesma:<subtype>``
    and the ``project:mnemos`` slug; the ``mnemos:no-federate`` trust
    marker is kept byte-stable; anything else passes through untouched.
    Duplicates created by the rewrite are removed preserving order.

    Returns ``(new_json, subtype_swaps, project_swaps, duplicates_removed,
    trust_markers_kept)``.
    """
    try:
        parsed = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError) as exc:
        raise _UnparseableTagsError(str(exc)) from exc
    if not isinstance(parsed, list) or not all(isinstance(t, str) for t in parsed):
        raise _UnparseableTagsError("tags column is not a JSON list of strings")

    subtype_swaps = 0
    project_swaps = 0
    trust_markers_kept = 0
    rewritten: list[str] = []
    for tag in parsed:
        if tag == TRUST_MARKER_TAG:
            trust_markers_kept += 1
            rewritten.append(tag)
        elif tag.startswith(OLD_TAG_PREFIX):
            rewritten.append(NEW_TAG_PREFIX + tag[len(OLD_TAG_PREFIX) :])
            subtype_swaps += 1
        elif tag == f"project:{LEGACY_PROJECT_SLUG}":
            rewritten.append(f"project:{CANONICAL_PROJECT_SLUG}")
            project_swaps += 1
        else:
            rewritten.append(tag)

    deduped: list[str] = []
    seen: set[str] = set()
    duplicates_removed = 0
    for tag in rewritten:
        if tag in seen:
            duplicates_removed += 1
            continue
        seen.add(tag)
        deduped.append(tag)
    return (
        json.dumps(deduped, ensure_ascii=False),
        subtype_swaps,
        project_swaps,
        duplicates_removed,
        trust_markers_kept,
    )


def reslag_project_slug(value: str) -> tuple[str, bool]:
    """Re-slag the denormalized ``project`` column (exact match only)."""
    if value == LEGACY_PROJECT_SLUG:
        return (CANONICAL_PROJECT_SLUG, True)
    return (value, False)


# ── Stats and idempotency ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class StoreStats:
    total: int
    status_breakdown: dict[str, int]
    project_breakdown: dict[str, int]
    ids_digest: str
    ids: tuple[str, ...]
    subtype_swaps: int
    project_swaps: int
    duplicates_removed: int
    trust_markers_kept: int
    unparseable_rows: int


def read_store_stats(db_path: Path) -> StoreStats:
    """One full pass over ``memories``: counters, digests, rewrite counts."""
    conn = _open_conn(db_path)
    try:
        rows = conn.execute("SELECT id, tags, project, status FROM memories").fetchall()
    finally:
        conn.close()
    status_counter: Counter[str] = Counter()
    project_counter: Counter[str] = Counter()
    subtype_swaps = 0
    project_swaps = 0
    duplicates_removed = 0
    trust_markers_kept = 0
    unparseable = 0
    ids: list[str] = []
    for row in rows:
        memory_id = str(row["id"])
        ids.append(memory_id)
        status_counter[str(row["status"])] += 1
        # The breakdown reports the slugs AS STORED in this database (the
        # source keeps mnemos, the migrated target keeps vesma); the
        # re-slag counter tracks how many would change.
        project_counter[str(row["project"])] += 1
        _, swapped = reslag_project_slug(str(row["project"]))
        if swapped:
            project_swaps += 1
        try:
            _, sub, proj, dup, marker = reslag_tags_json(str(row["tags"]))
        except _UnparseableTagsError:
            unparseable += 1
            continue
        subtype_swaps += sub
        project_swaps += proj
        duplicates_removed += dup
        trust_markers_kept += marker
    ids.sort()
    digest = hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()
    return StoreStats(
        total=len(rows),
        status_breakdown=dict(status_counter),
        project_breakdown=dict(project_counter),
        ids_digest=digest,
        ids=tuple(ids),
        subtype_swaps=subtype_swaps,
        project_swaps=project_swaps,
        duplicates_removed=duplicates_removed,
        trust_markers_kept=trust_markers_kept,
        unparseable_rows=unparseable,
    )


def _marker_file(data_dir: Path) -> Path:
    return data_dir / _MARKER_FILE_NAME


def is_already_migrated(layout: _StoreLayout, stats: StoreStats) -> bool:
    """True when the source store carries the post-migration namespace.

    The trust marker is byte-stable by design, so ``mnemos:no-federate``
    tags do NOT count against migration. An empty store is never
    "already migrated" (a copy-only move is still legitimate).
    """
    if _marker_file(layout.data_dir).is_file():
        return True
    return stats.total > 0 and stats.subtype_swaps == 0 and stats.project_swaps == 0


# ── Quiesce gate ──────────────────────────────────────────────────────────────


def quiesce_problems(
    db_paths: list[Path], *, with_wal_sampling: bool
) -> tuple[list[str], list[str]]:
    """Probe the source databases for live connections.

    Returns ``(fatal_problems, warnings)``. Fatal: a held write lock or
    WAL growth between samples — the operator must stop the vesma service.
    Warning-only: the frozen-name unix socket exists (stale sockets survive
    a stopped service; the database probes decide).
    """
    fatal: list[str] = []
    warnings: list[str] = []
    for db_path in db_paths:
        try:
            conn = sqlite3.connect(str(db_path), timeout=0.25)
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("ROLLBACK")
            finally:
                conn.close()
        except sqlite3.OperationalError as exc:
            fatal.append(f"{db_path}: database is locked ({exc})")
            continue
        wal_path = db_path.parent / f"{db_path.name}-wal"
        if with_wal_sampling and wal_path.is_file():
            size_before = wal_path.stat().st_size
            time.sleep(_QUIESCE_WAL_SAMPLE_SECONDS)
            if wal_path.stat().st_size != size_before:
                fatal.append(
                    f"{wal_path}: WAL is growing — a live process is writing to the source store"
                )
    if SOURCE_SOCKET.exists():
        warnings.append(
            f"{SOURCE_SOCKET} exists — if the vesma service is running, stop it "
            "(a stale socket is harmless; the database probes decide)"
        )
    return (fatal, warnings)


# ── Copy machinery (SQLite backup API + byte-for-byte files) ──────────────────


def _copy_sqlite(src: Path, dst: Path) -> None:
    """Consistent copy via the SQLite backup API (WAL content included)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(str(dst))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    _harden_file(dst)  # the backup-API copy must not inherit umask defaults


def _copy_tree(src: Path, dst: Path) -> tuple[int, int]:
    """Byte-for-byte recursive copy; sqlite files go through the backup API.

    SQLite ``-wal``/``-shm`` sidecars are SKIPPED: every sqlite main file
    is merged through the backup API, and a stale sidecar copied next to a
    merged copy would be replayed on open (corruption risk).

    Returns ``(files_copied, sqlite_via_backup_api)``.
    """
    copied = 0
    via_backup = 0
    for root, _dirs, files in os.walk(src):
        root_path = Path(root)
        rel = root_path.relative_to(src)
        dst_dir = dst / rel
        dst_dir.mkdir(parents=True, exist_ok=True)
        _harden_dir(dst_dir)
        for name in files:
            if _is_sidecar(name):
                continue
            src_file = root_path / name
            dst_file = dst_dir / name
            if _is_sqlite(src_file):
                _copy_sqlite(src_file, dst_file)
                via_backup += 1
            else:
                shutil.copy2(src_file, dst_file)
                _harden_file(dst_file)
            copied += 1
    return (copied, via_backup)


@dataclass(frozen=True)
class _Snapshot:
    layout: _StoreLayout
    path: Path
    stats: StoreStats  # stats of the SNAPSHOT copy (authoritative for the run)


def _is_non_data_entry(path: Path, *, flat_layout: bool) -> bool:
    """True when a home-root entry never rides along as a data sibling.

    Only the FLAT pre-2.1 layout (data_dir == home) puts ``vault/``,
    ``logs/``, ``cache/`` and ``config.yaml`` next to the database; there
    they are NOT data siblings — vault/ migrates through its own
    ``vault_dir`` branch, config.yaml is rewritten by ``migrate_config``
    (a stale legacy copy inside data/ would be a fork seed), logs/ and
    cache/ are not store data. In a nested layout a ``data/vault`` stays
    ordinary data. ONE filter for BOTH the plan listing and the
    snapshot/materialize copies (cascade P2-2: plan must equal fact).
    """
    if not flat_layout:
        return False
    return path.name in _ROOT_NON_DATA_DIRS or path.name == "config.yaml"


def _snapshot_data_files(data_dir: Path, main_db_name: str, *, flat_layout: bool) -> list[Path]:
    """Regular files in a data dir that ride along with the migration.

    Excludes the main db (copied explicitly), sqlite ``-wal``/``-shm``
    sidecars (their content is folded in by the backup API), the
    idempotence marker, and — flat layout only — the home-root non-data
    entries (vault/logs/cache/config.yaml, see :func:`_is_non_data_entry`).
    """
    return [
        path
        for path in sorted(data_dir.iterdir())
        if path.name != main_db_name
        and path.name != _MARKER_FILE_NAME
        and not _is_sidecar(path.name)
        and not _is_non_data_entry(path, flat_layout=flat_layout)
        and (path.is_file() or path.is_dir())
    ]


def make_snapshot(source: _StoreLayout, snapshot_dir: Path) -> _Snapshot:
    """Snapshot-gate (brief item 3): consistent copy + verification."""
    try:
        (snapshot_dir / "data").mkdir(parents=True)
        _harden_dir(snapshot_dir)
        _harden_dir(snapshot_dir / "data")
        _copy_sqlite(source.db_path, snapshot_dir / "data" / source.db_path.name)
        for item in _snapshot_data_files(
            source.data_dir, source.db_path.name, flat_layout=source.data_dir == source.home
        ):
            target = snapshot_dir / "data" / item.name
            if item.is_file():
                if _is_sqlite(item):
                    _copy_sqlite(item, target)
                else:
                    shutil.copy2(item, target)
                    _harden_file(target)
            else:
                _copy_tree(item, target)
        if source.vault_dir.is_dir():
            _copy_tree(source.vault_dir, snapshot_dir / "vault")
        if source.config_path.is_file():
            shutil.copy2(source.config_path, snapshot_dir / "config.yaml")
            _harden_file(snapshot_dir / "config.yaml")
    except OSError as exc:
        raise SnapshotError(f"snapshot copy failed: {exc}") from exc

    # Verify the snapshot BEFORE continuing (open + counters), and confirm
    # the source has not moved under us since the snapshot was taken.
    snapshot_layout = _resolve_layout(snapshot_dir)
    snap_stats = read_store_stats(snapshot_layout.db_path)
    live_stats = read_store_stats(source.db_path)
    if snap_stats.total != live_stats.total or snap_stats.ids_digest != live_stats.ids_digest:
        raise SnapshotError(
            "the source store changed while the snapshot was taken "
            "(live writer detected) — stop the vesma service and retry"
        )
    return _Snapshot(layout=snapshot_layout, path=snapshot_dir, stats=snap_stats)


# ── Config migration ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ConfigPlan:
    source_config: Path | None
    synthesized: bool
    section_renamed: bool  # mnemos: → vesma:
    paths_rerooted: int
    db_name_pinned: bool
    kept_untouched_values: int


def _reroot(value: Any, old_home: Path, new_home: Path) -> tuple[Any, bool]:
    """Re-root a path value from the old home onto the new home."""
    if not isinstance(value, (str, Path)):
        return (value, False)
    raw = Path(str(value))
    try:
        resolved = raw.expanduser().resolve()
    except OSError:
        return (value, False)
    try:
        rel = resolved.relative_to(old_home.resolve())
    except ValueError:
        return (value, False)
    return (str(new_home / rel), True)


def _annotation_is_path(annotation: Any) -> bool:
    if annotation is Path:
        return True
    return Path in get_args(annotation)


def migrate_config(source: _StoreLayout, new_home: Path, staging: Path) -> tuple[Path, ConfigPlan]:
    """Map the legacy config onto the canonical 6.0 config (brief item 8).

    The ``mnemos:`` section becomes ``vesma:``; Path-typed values under the
    old home re-root under the new home; ``db_name`` pins ``vesma.db``; the
    result is validated against the Settings section models BEFORE any
    write. Unmapped key NAMES — at the top level AND inside every mapped
    section — abort with exit code 9; values are never printed.
    """
    import yaml
    from pydantic import BaseModel, ValidationError

    from vesma.config import Settings

    old_path = source.config_path
    data: dict[str, Any] = {}
    if old_path.is_file():
        loaded = yaml.safe_load(old_path.read_text(encoding="utf-8"))
        if loaded is not None:
            if not isinstance(loaded, dict):
                raise ConfigMigrationError(f"{old_path}: top level is not a mapping")
            data = dict(loaded)

    legacy_section = data.pop("mnemos", None)
    canonical_section = data.pop("vesma", None)
    if legacy_section is not None and canonical_section is not None:
        raise ConfigMigrationError(
            "config carries BOTH a mnemos: and a vesma: section — ambiguous "
            "source, resolve manually and retry"
        )
    section_renamed = legacy_section is not None
    if section_renamed and not isinstance(legacy_section, dict):
        raise ConfigMigrationError("config section mnemos: is not a mapping")
    if canonical_section is not None and not isinstance(canonical_section, dict):
        raise ConfigMigrationError("config section vesma: is not a mapping")
    vesma_section: dict[str, Any] = dict(legacy_section or canonical_section or {})

    # Path re-rooting + db_name pinning inside the vesma: section.
    paths_rerooted = 0
    db_name_pinned = False
    rerooted: dict[str, Any] = {}
    for key, value in vesma_section.items():
        if key == "db_name" and value == LEGACY_DB_NAME:
            rerooted[key] = CANONICAL_DB_NAME
            db_name_pinned = True
            continue
        new_value, moved = _reroot(value, source.home, new_home)
        if moved:
            paths_rerooted += 1
        rerooted[key] = new_value
    vesma_section = rerooted

    # Remaining sections: re-root Path-typed values, keep everything else.
    settings_fields = Settings.model_fields
    unmapped: list[str] = []
    kept_untouched = 0
    for section_name, section_value in data.items():
        model_field = settings_fields.get(section_name)
        if model_field is None:
            unmapped.append(section_name)
            continue
        annotation = model_field.annotation
        if section_name == "policies":
            continue  # free-form mapping, carried as-is
        if not (
            isinstance(annotation, type)
            and issubclass(annotation, BaseModel)
            and isinstance(section_value, dict)
        ):
            kept_untouched += 1
            continue
        rerooted_section: dict[str, Any] = {}
        for key, value in section_value.items():
            sub_field = annotation.model_fields.get(key)
            is_path = sub_field is not None and _annotation_is_path(sub_field.annotation)
            if is_path:
                new_value, moved = _reroot(value, source.home, new_home)
                if moved:
                    paths_rerooted += 1
                rerooted_section[key] = new_value
            else:
                rerooted_section[key] = value
        data[section_name] = rerooted_section

    if unmapped:
        raise ConfigMigrationError(
            "config keys unknown to the 6.0 Settings schema cannot be carried "
            "over (map them manually): " + ", ".join(sorted(unmapped))
        )

    synthesized = not old_path.is_file()
    if synthesized:
        # Zero-config (or env-configured) source: synthesize a self-describing
        # config pinning the new canonical layout.
        vesma_section.setdefault("data_dir", str(new_home / "data"))
        vesma_section.setdefault("vault_path", str(new_home / "vault"))
        vesma_section.setdefault("db_name", CANONICAL_DB_NAME)
        db_name_pinned = vesma_section.get("db_name") == CANONICAL_DB_NAME

    new_data = dict(data)
    new_data["vesma"] = vesma_section

    # Validate EVERY section against the real 6.0 models before writing.
    for section_name, section_value in new_data.items():
        if section_name == "policies":
            continue
        model_field = settings_fields.get(section_name)
        annotation = model_field.annotation if model_field else None
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            if isinstance(section_value, dict):
                # Unknown keys INSIDE a section must abort: model_validate
                # with extra=ignore would silently DROP a typo'd key (e.g.
                # vual_path) and the produced config would quietly fork the
                # store onto a default path. Key NAMES only — never values.
                unknown_keys = sorted(
                    str(key) for key in set(section_value) - set(annotation.model_fields)
                )
                if unknown_keys:
                    raise ConfigMigrationError(
                        f"config section {section_name}: keys unknown to the 6.0 schema "
                        "cannot be carried over (map them manually): "
                        f"{', '.join(unknown_keys)}"
                    )
            try:
                annotation.model_validate(section_value)
            except ValidationError as exc:
                names = sorted({str(err["loc"][0]) for err in exc.errors()})
                raise ConfigMigrationError(
                    f"config section {section_name}: keys rejected by the 6.0 "
                    f"schema (map them manually): {', '.join(names)}"
                ) from exc

    staging.mkdir(parents=True, exist_ok=True)
    new_config = staging / "config.yaml"
    new_config.write_text(
        yaml.safe_dump(new_data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    _harden_file(new_config)
    plan = ConfigPlan(
        source_config=old_path if old_path.is_file() else None,
        synthesized=synthesized,
        section_renamed=section_renamed,
        paths_rerooted=paths_rerooted,
        db_name_pinned=db_name_pinned,
        kept_untouched_values=kept_untouched,
    )
    return (new_config, plan)


# ── Apply ─────────────────────────────────────────────────────────────────────


def _materialize_target(snapshot: _Snapshot, staging: Path) -> dict[str, int]:
    """Build the 6.0 target from the verified snapshot; re-slag in place."""
    staging.mkdir(parents=True)
    _harden_dir(staging)
    src_data = snapshot.layout.data_dir
    dst_data = staging / "data"
    dst_data.mkdir()
    _harden_dir(dst_data)
    subtype_swaps = 0
    project_swaps = 0
    duplicates_removed = 0
    trust_markers_kept = 0

    for item in _snapshot_data_files(
        src_data, LEGACY_DB_NAME, flat_layout=src_data == snapshot.layout.home
    ):
        target = dst_data / item.name
        if item.is_file():
            if _is_sqlite(item):
                _copy_sqlite(item, target)
            else:
                shutil.copy2(item, target)
        else:
            _copy_tree(item, target)

    # Vault rides from the snapshot byte-for-byte (config.yaml is NOT copied
    # from the snapshot — migrate_config writes the canonical one).
    snapshot_vault = snapshot.layout.vault_dir
    if snapshot_vault.is_dir():
        _copy_tree(snapshot_vault, staging / "vault")

    target_db = dst_data / CANONICAL_DB_NAME
    _copy_sqlite(src_data / LEGACY_DB_NAME, target_db)

    conn = sqlite3.connect(str(target_db), timeout=30.0)
    row_count = 0
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("SELECT id, tags, project FROM memories").fetchall()
        row_count = len(rows)
        for row in rows:
            new_tags, sub, proj, dup, marker = reslag_tags_json(str(row["tags"]))
            new_project, swapped = reslag_project_slug(str(row["project"]))
            subtype_swaps += sub
            project_swaps += proj + (1 if swapped else 0)
            duplicates_removed += dup
            trust_markers_kept += marker
            conn.execute(
                "UPDATE memories SET tags = ?, project = ? WHERE id = ?",
                (new_tags, new_project, str(row["id"])),
            )
        # FTS rebuild (draft requirement; belt-and-braces beyond the
        # AFTER UPDATE triggers) — then commit and checkpoint.
        conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()

    # Write the idempotence marker (paths + numbers only).
    marker_doc = {
        "migrated_at": datetime.now(UTC).isoformat(),
        "records": row_count,
        "tag_subtypes_rewritten": subtype_swaps,
        "project_slug_rewritten": project_swaps,
        "trust_markers_kept": trust_markers_kept,
        "tool": "vesma migrate-store",
    }
    _marker_file(dst_data).write_text(
        json.dumps(marker_doc, indent=2, sort_keys=True), encoding="utf-8"
    )
    _harden_file(_marker_file(dst_data))
    return {
        "records": row_count,
        "subtype_swaps": subtype_swaps,
        "project_swaps": project_swaps,
        "duplicates_removed": duplicates_removed,
        "trust_markers_kept": trust_markers_kept,
    }


def _field_digest(row: sqlite3.Row, *, normalized: bool) -> str:
    values: list[str] = []
    for name in _SAMPLE_FIELDS:
        value = row[name]
        if normalized and name == "tags":
            value = reslag_tags_json(str(value))[0]
        elif normalized and name == "project":
            value = reslag_project_slug(str(value))[0]
        values.append("" if value is None else str(value))
    return hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()


def verify_target(target_db: Path, snapshot: _Snapshot, all_db_paths: list[Path]) -> dict[str, Any]:
    """Post-apply verification (brief item 10); raises VerificationError."""
    problems: list[str] = []

    snap_stats = snapshot.stats
    target_stats = read_store_stats(target_db)
    if target_stats.total != snap_stats.total:
        problems.append(
            f"record count mismatch: source {snap_stats.total} vs target {target_stats.total}"
        )
    if target_stats.ids_digest != snap_stats.ids_digest:
        problems.append("id set mismatch between source and target")
    for slug, count in snap_stats.status_breakdown.items():
        if target_stats.status_breakdown.get(slug, 0) != count:
            problems.append(f"status breakdown mismatch for status={slug}")
    # Map the snapshot's project breakdown onto the 6.0 slugs FIRST (both
    # mnemos and vesma land on vesma), then compare per silo.
    mapped_project: dict[str, int] = {}
    for slug, count in snap_stats.project_breakdown.items():
        mapped = CANONICAL_PROJECT_SLUG if slug == LEGACY_PROJECT_SLUG else slug
        mapped_project[mapped] = mapped_project.get(mapped, 0) + count
    for slug, count in mapped_project.items():
        if target_stats.project_breakdown.get(slug, 0) != count:
            problems.append(f"project breakdown mismatch for silo {slug}")

    # 10 seeded sample records: per-field checksums equal under the EXPECTED
    # rewrite (source normalized, target raw).
    rng = random.Random(_SAMPLE_SEED)
    ids = list(snap_stats.ids)
    sample_ids = rng.sample(ids, min(_SAMPLE_SIZE, len(ids)))
    sample_mismatches = 0
    snap_conn = _open_conn(snapshot.layout.db_path)
    tgt_conn = _open_conn(target_db)
    try:
        columns = ", ".join(_SAMPLE_FIELDS)
        for memory_id in sample_ids:
            snap_row = snap_conn.execute(
                f"SELECT {columns} FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            tgt_row = tgt_conn.execute(
                f"SELECT {columns} FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            if snap_row is None or tgt_row is None:
                sample_mismatches += 1
                continue
            if _field_digest(snap_row, normalized=True) != _field_digest(tgt_row, normalized=False):
                sample_mismatches += 1
    finally:
        snap_conn.close()
        tgt_conn.close()
    if sample_mismatches:
        problems.append(
            f"sample field checksums mismatched on {sample_mismatches} of {len(sample_ids)} rows"
        )

    # FTS: row parity + index integrity after the rebuild.
    conn = _open_conn(target_db)
    try:
        fts_count = int(conn.execute("SELECT count(*) FROM memories_fts").fetchone()[0])
        if fts_count != target_stats.total:
            problems.append(f"FTS row count {fts_count} != memories {target_stats.total}")
        try:
            conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('integrity-check')")
        except sqlite3.DatabaseError as exc:
            problems.append(f"FTS integrity check failed: {exc}")
    finally:
        conn.close()

    # sqlite quick_check on every moved database.
    for db_path in all_db_paths:
        conn = _open_conn(db_path)
        try:
            result = conn.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            conn.close()
        if result != "ok":
            problems.append(f"{db_path.name}: quick_check returned {result}")

    if problems:
        raise VerificationError("; ".join(problems))
    return {
        "total": target_stats.total,
        "sample_size": len(sample_ids),
        "sample_mismatches": 0,
        "status_breakdown": dict(target_stats.status_breakdown),
        "project_breakdown": dict(target_stats.project_breakdown),
    }


# ── Plan / orchestration ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class PlannedFile:
    source: Path
    target: Path
    size_bytes: int
    via_backup_api: bool


@dataclass(frozen=True)
class MigrationPlan:
    source_home: Path
    target_home: Path
    mode: str  # "dry-run" | "apply"
    layout_data_dir: Path
    source_db: Path
    vault_present: bool
    config_present: bool
    stats: StoreStats
    quiesce_warnings: tuple[str, ...]
    planned_files: tuple[PlannedFile, ...]
    snapshot_dir: Path
    staging_dir: Path


def _sibling_sqlite_dbs(layout: _StoreLayout) -> list[Path]:
    """Other sqlite databases in the data dir (vectors, code_graph, ...)."""
    return [
        path
        for path in sorted(layout.data_dir.iterdir())
        if path.is_file()
        and path != layout.db_path
        and path.name != _MARKER_FILE_NAME
        and not _is_sidecar(path.name)
        and _is_sqlite(path)
    ]


def build_plan(source_home: Path, target_home: Path, *, apply: bool) -> MigrationPlan:
    """Dry-run-safe planning: reads only, writes nothing."""
    source_home = source_home.expanduser().resolve()
    target_home = target_home.expanduser().resolve()
    if not source_home.is_dir():
        raise UsageError(f"--from does not exist or is not a directory: {source_home}")
    if source_home == target_home:
        raise UsageError("--from and --to must be different paths")
    if source_home in target_home.parents or target_home in source_home.parents:
        raise UsageError("--from and --to must not be nested inside each other")
    if target_home.exists():
        # ANY pre-existing --to is refused (even an empty directory): the
        # mover must never mix its materialized layout with foreign content,
        # and the empty case used to slip through to a mid-run rename
        # failure with a misleading "appeared during migration" message.
        raise UsageError(f"--to exists: {target_home} — choose a fresh target")

    layout = _resolve_layout(source_home)
    stats = read_store_stats(layout.db_path)
    if is_already_migrated(layout, stats):
        raise AlreadyMigratedError(
            f"source {source_home} already carries the vesma:* namespace "
            f"({stats.total} records, 0 re-slaggable) — nothing to migrate"
        )
    if stats.unparseable_rows:
        raise InvalidSourceError(
            f"source has {stats.unparseable_rows} row(s) with unparseable tags "
            "JSON — repair the source before migrating (nothing was touched)"
        )

    fatal, warnings = quiesce_problems(
        [layout.db_path, *_sibling_sqlite_dbs(layout)], with_wal_sampling=False
    )
    if fatal:
        raise QuiesceViolationError(
            "source store is not quiescent (остановите vesma service): " + "; ".join(fatal)
        )

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    snapshot_dir = target_home.parent / f"{SNAPSHOT_DIR_PREFIX}{timestamp}"
    staging_dir = target_home.parent / f"{target_home.name}.staging-{timestamp}"

    planned: list[PlannedFile] = [
        PlannedFile(
            source=layout.db_path,
            target=target_home / "data" / CANONICAL_DB_NAME,
            size_bytes=layout.db_path.stat().st_size,
            via_backup_api=True,
        )
    ]
    for path in _snapshot_data_files(
        layout.data_dir, layout.db_path.name, flat_layout=layout.data_dir == layout.home
    ):
        if path.is_file():
            planned.append(
                PlannedFile(
                    source=path,
                    target=target_home / "data" / path.name,
                    size_bytes=path.stat().st_size,
                    via_backup_api=_is_sqlite(path),
                )
            )
        else:
            total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            planned.append(
                PlannedFile(
                    source=path,
                    target=target_home / "data" / path.name,
                    size_bytes=total,
                    via_backup_api=False,
                )
            )
    if layout.vault_dir.is_dir():
        vault_size = sum(f.stat().st_size for f in layout.vault_dir.rglob("*") if f.is_file())
        planned.append(
            PlannedFile(
                source=layout.vault_dir,
                target=target_home / "vault",
                size_bytes=vault_size,
                via_backup_api=False,
            )
        )
    return MigrationPlan(
        source_home=source_home,
        target_home=target_home,
        mode="apply" if apply else "dry-run",
        layout_data_dir=layout.data_dir,
        source_db=layout.db_path,
        vault_present=layout.vault_dir.is_dir(),
        config_present=layout.config_path.is_file(),
        stats=stats,
        quiesce_warnings=tuple(warnings),
        planned_files=tuple(planned),
        snapshot_dir=snapshot_dir,
        staging_dir=staging_dir,
    )


@dataclass(frozen=True)
class MigrationReport:
    """Final report: paths and numbers only (privacy contract)."""

    mode: str
    source_home: Path
    target_home: Path
    snapshot_dir: Path
    records: int
    tag_subtypes_rewritten: int
    project_slug_rewritten: int
    duplicates_removed: int
    trust_markers_kept: int
    status_breakdown: dict[str, int]
    project_breakdown: dict[str, int]
    files_copied: int
    sqlite_via_backup_api: int
    config: ConfigPlan | None
    verification: dict[str, Any] | None
    warnings: tuple[str, ...] = field(default=tuple())


def _cleanup_staging(staging: Path) -> None:
    shutil.rmtree(staging, ignore_errors=True)


def _fsync_dir(path: Path) -> None:
    """fsync a directory entry so a rename/creation inside it is durable."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_tree(root: Path) -> None:
    """fsync every staged file and directory BEFORE the atomic rename.

    Without this, a power-loss right after the rename could leave ``--to``
    present with EMPTY file contents (data survived only in page cache).
    Only regular files are synced (fifos/sockets would block); the sqlite
    files were already committed by the backup API, this flushes the rest.
    """
    for dirpath, _dirnames, filenames in os.walk(root):
        dir_path = Path(dirpath)
        for name in filenames:
            file_path = dir_path / name
            if not stat.S_ISREG(file_path.stat().st_mode):
                continue
            fd = os.open(file_path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        _fsync_dir(dir_path)


def run_migration(plan: MigrationPlan) -> MigrationReport:
    """Execute a plan built with ``apply=True`` (the only writable mode)."""
    if plan.mode != "apply":
        raise UsageError("run_migration requires a plan built with apply=True")
    warnings = list(plan.quiesce_warnings)
    layout = _resolve_layout(plan.source_home)

    # Re-run the quiesce gate WITH WAL sampling right before the copy.
    fatal, extra_warnings = quiesce_problems(
        [layout.db_path, *_sibling_sqlite_dbs(layout)], with_wal_sampling=True
    )
    warnings.extend(extra_warnings)
    if fatal:
        raise QuiesceViolationError(
            "source store is not quiescent (остановите vesma service): " + "; ".join(fatal)
        )

    pre_stats = read_store_stats(layout.db_path)
    snapshot = make_snapshot(layout, plan.snapshot_dir)
    if snapshot.stats.ids_digest != pre_stats.ids_digest:
        raise SnapshotError("source changed between plan and snapshot — retry")

    config_plan: ConfigPlan | None = None
    try:
        applied = _materialize_target(snapshot, plan.staging_dir)
        target_db = plan.staging_dir / "data" / CANONICAL_DB_NAME
        moved_dbs = [
            path
            for path in sorted((plan.staging_dir / "data").iterdir())
            if path.is_file() and _is_sqlite(path)
        ]
        _, config_plan = migrate_config(layout, plan.target_home, plan.staging_dir)
        verification = verify_target(target_db, snapshot, moved_dbs)
        # Atomic appearance of the fully verified target (same-FS rename).
        # Cascade P2-fsync: flush every staged file + directory fd FIRST —
        # a rename without fsync is not durable (post-power-loss --to could
        # exist with hollow files) — then persist the rename itself via the
        # parent dir fd.
        plan.target_home.parent.mkdir(parents=True, exist_ok=True)
        _fsync_tree(plan.staging_dir)
        if plan.target_home.exists():
            # build_plan already refused ANY pre-existing --to; reaching this
            # guard means the path appeared MID-RUN (a racing actor) — the
            # rename is refused, staging is cleaned up by the caller.
            raise UsageError(f"--to raced during migration: {plan.target_home}")
        plan.staging_dir.rename(plan.target_home)
        _fsync_dir(plan.target_home.parent)
    except StoreMigrationError:
        raise
    except (OSError, sqlite3.Error, json.JSONDecodeError, ValueError) as exc:
        raise VerificationError(f"migration failed: {exc}") from exc
    except Exception as exc:
        # Typed last resort: an unexpected failure must still surface with a
        # stable exit code — the old except-tuple missed internal errors and
        # left staging residue behind.
        raise VerificationError(f"migration failed unexpectedly: {exc!r}") from exc
    finally:
        # The SINGLE cleanup path (cascade P2-cleanup): on success staging no
        # longer exists (renamed onto --to), so this is a no-op; on ANY
        # failure — typed, unexpected, or a BaseException like
        # KeyboardInterrupt — staging is removed. The snapshot stays (the
        # rollback artifact is deliberate).
        _cleanup_staging(plan.staging_dir)

    return MigrationReport(
        mode="apply",
        source_home=plan.source_home,
        target_home=plan.target_home,
        snapshot_dir=plan.snapshot_dir,
        records=applied["records"],
        tag_subtypes_rewritten=applied["subtype_swaps"],
        project_slug_rewritten=applied["project_swaps"],
        duplicates_removed=applied["duplicates_removed"],
        trust_markers_kept=applied["trust_markers_kept"],
        status_breakdown=verification["status_breakdown"],
        project_breakdown=verification["project_breakdown"],
        files_copied=len(plan.planned_files),
        sqlite_via_backup_api=sum(1 for f in plan.planned_files if f.via_backup_api),
        config=config_plan,
        verification=verification,
        warnings=tuple(warnings),
    )
