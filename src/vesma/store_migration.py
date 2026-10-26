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
2. dry-run by default: planning + counters through READ-ONLY opens —
   the source's records stay unchanged (no WAL sidecar effects, no
   write-lock probe). Writes happen only under an explicit ``--apply``.
3. snapshot-gate: before any write a full consistent copy of the source
   (SQLite backup API per database file) lands in
   ``<to>/../migrate-snapshot-<ts>/`` and is verified (open + counters)
   BEFORE the migration continues. The target is materialized FROM the
   verified snapshot, never from the (possibly racy) live source.
4. quiesce gate (--apply only): live connections on the source databases
   (write-lock held, WAL growth between samples) refuse the run with a
   "stop the vesma service first" message. A frozen-name unix socket
   merely warns (stale sockets are common); the database probes decide.
   A read-only source FILESYSTEM is reported as an honest warning (the
   write probe cannot lock there) — never misdiagnosed as "locked".
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

── System target (``--target=system``, ADR-0047; ratified ArchCom 2026-10-22) ──

The same mover relocates a store INTO the installed system profile (the
FHS layout of ADR-0045). The system target contract (C1-C15 of ADR-0047)
is normative here; the load-bearing points:

- C1: the target resolves ONLY through the system install-state + the FHS
  manifest resolvers (:mod:`vesma.service.fhs`) — zero hardcoded system
  paths in this module (a pin test scans this file's source). Every
  precondition (install-state present + ``profile: system``, profile
  isolation DR-14, DR-17 clean, target data root fresh of store content,
  quiesce) fails fast BEFORE the first write. A missing profile is a typed
  refusal with remediation — there is NO auto-install.
- C2: the cargo is the STORE ONLY (memories db + data siblings + vault +
  config). env secrets, component manifests/data, venvs and unit links are
  not carried — the report lists what remains, with owner commands.
- C3: an existing non-empty system conffile is NEVER overwritten or
  silently merged — the incoming config lands beside it as
  ``config.yaml.migrated-<ts>`` with a loud operator callout.
- C4: ownership is applied per FHS manifest ROLES (never a blanket
  recursive chown): data tree to the service account, dirs/files at the
  manifest modes; wider than the manifest — never.
- C5: store home == data root, so a single rename is impossible — publish
  is PIECEWISE, with a fresh quiesce gate right before it, and the
  finally-path rolls published entries back on ANY interruption.
- C6: the source is NEVER written: when the effective uid differs from the
  source owner every source probe goes through read-only URIs; a stale
  sidecar requiring recovery is a typed refusal (repair is the owner's
  job). No checkpoint is ever truncated on the source. The guarantee is
  VERIFIED, not assumed: every sibling sqlite under the data root
  (component scratch databases included, sidecars excluded) is digested
  before and after the snapshot copy window — drift is a typed snapshot
  refusal naming the file (F8: a component writer touching its scratch
  db mid-copy must stop the run, never ride along undetected).
- C7: target quiesce is a triple gate (system unit state via systemctl,
  the system control socket as a warning, file probes decisive) run twice:
  in the plan and immediately before publish.
- C8: verification extends the B3 set with per-file sha256 parity of every
  non-sqlite file, per-table count parity of every sibling database, and a
  post-publish ownership/mode stat audit. Byte-equality of the main db is
  NOT a gate (the backup API legally reshapes page layout).
- C9: the snapshot defaults NEXT TO THE SOURCE (operator-owned, 0700);
  explicit ``--snapshot-dir``; inside the source or target trees — refused.
- C10: the memory vault and agent-token signing key travel with the store
  (verified); TOTP blobs survive only with the env-only master key
  provisioned in the system env dir — a printed precondition, never an
  assumption; out-of-home secret-path config overrides are reported as
  "not carried" (key NAMES only, never values).
- C11: record-count/id-set equality and the byte-stable no-federate trust
  marker are REQUIRED for the system target too.
- C12: the mover does NOT write the store into the system install-state
  and creates no installer artifacts (an uninstall must never silently
  delete a migrated store).
- C13: ``--apply`` for the system target was HARD-GATED until the
  round-trip soak closed green AND the owner gave the word (ADR-0045
  Decision 9); ACTIVE since 6.5.0 — ratified by the owner 2026-10-23
  after the green soak (ca534ff). The typed refusal (exit 15) stays as
  belt-and-braces.
- C14: zero rewrite counters ⇒ the UPDATE sweep is skipped (pure backup-API
  copy); bulk reads always fetch in batches; phased progress banners +
  streaming counters; stepped backup for large databases; preflight
  free-space >= 2x the source volume; no wall-clock timeout anywhere near a
  healthy run; the source WAL size is an advisory number.
- C15: every new refusal is a :class:`StoreMigrationError` subclass with a
  stable, documented exit code; the taxonomy grows additively (10+).
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
import subprocess  # nosec B404 - fixed-argv systemctl probes, no shell
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args
from urllib.parse import quote

from vesma.models import NO_FEDERATE_TAG
from vesma.service import fhs

__all__ = [
    "CANONICAL_DB_NAME",
    "CANONICAL_PROJECT_SLUG",
    "LEGACY_DB_NAME",
    "LEGACY_PROJECT_SLUG",
    "MIGRATE_CONFFILE_HEADER",
    "MIGRATION_MARKER_NAME",
    "NEW_TAG_PREFIX",
    "OLD_TAG_PREFIX",
    "SNAPSHOT_DIR_PREFIX",
    "SOURCE_SOCKET",
    "SYSTEM_TARGET",
    "TRUST_MARKER_TAG",
    "USER_TARGET",
    "ActivationGateError",
    "AlreadyMigratedError",
    "ConfigMigrationError",
    "DiscoveryRefusedError",
    "InstallStateDriftError",
    "InsufficientSpaceError",
    "InvalidSourceError",
    "MigrationPlan",
    "MigrationReport",
    "ProfileIsolationError",
    "QuiesceViolationError",
    "SourceRecoveryRequiredError",
    "StoreMigrationError",
    "SystemProfileNotInstalledError",
    "TargetStoreContentError",
    "UsageError",
    "VerificationError",
    "activation_gate_open",
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
#: Store-home targets (ADR-0047): the classic explicit-path mover and the
#: system-profile relocation resolved through the install-state + manifest.
USER_TARGET: str = "user"
SYSTEM_TARGET: str = "system"
_MARKER_FILE_NAME = ".migrate-store-applied.json"
#: Public names for the cross-module contract with the system
#: uninstaller (F9): the uninstall adopt evidence reads the marker and
#: recognizes the provenance header — ONE constant each, never a second
#: literal drifting elsewhere.
MIGRATION_MARKER_NAME = _MARKER_FILE_NAME
MIGRATE_CONFFILE_HEADER = "# written by vesma migrate-store"
_SQLITE_HEADER = b"SQLite format 3\x00"
_QUIESCE_WAL_SAMPLE_SECONDS = 0.7
_SIDECAR_SUFFIXES = ("-wal", "-shm")
#: Everything the mover writes is store data (vault content, memories,
#: config) — the project norm for secret-bearing surfaces is 0600/0700
#: (cf. metrics/sink.py, agent_tokens.py), NEVER umask defaults (CWE-732).
#: These hardening modes are the USER-target floor ONLY (C4): for the
#: system target every mode comes from the FHS manifest — wider than the
#: manifest is never.
_PRIVATE_FILE_MODE = 0o600
_PRIVATE_DIR_MODE = 0o700
# ── System-target contract constants (ADR-0047) ──────────────────────────────
#: C13 activation gate: OPEN — ``--apply --target=system`` is active since
#: 6.5.0, ratified by the owner 2026-10-23 after the green round-trip soak
#: (round 1 + round 2 on ca534ff, evidence /tmp/vesma-soak-stand), per
#: ADR-0047 C13 / ADR-0045 Decision 9. The typed refusal (exit 15) stays as
#: belt-and-braces; flipping this constant is a ratification act — never a
#: wave-side convenience.
_ACTIVATION_GATE_OPEN: bool = True
#: C14: bulk reads always fetch in batches (a full-table materialization
#: used to buffer gigabytes of RAM); batch rows, not spinner frames.
_FETCH_BATCH_ROWS = 512
#: C14: stepped (paged) backup with progress for databases above this size.
_STEPPED_BACKUP_THRESHOLD_BYTES = 256 * 1024 * 1024
_STEPPED_BACKUP_PAGES = 1024
#: C14: preflight free space >= 2x the source volume (refusal with numbers).
_FREE_SPACE_FACTOR = 2
#: C14: streaming counters emit at most every N units (paths+numbers only).
_PROGRESS_EVERY_ROWS = 50_000
_PROGRESS_EVERY_FILES = 500
#: C8: seeded-sample size grows on very large stores.
_SAMPLE_SIZE = 10
_SAMPLE_SIZE_LARGE = 100
_LARGE_STORE_ROWS = 1_000_000
_SAMPLE_SEED = 20261007
#: C10: config keys whose value is a secret PATH override (not re-rootable
#: store data). Values are never printed — only the key name surfaces.
_SECRET_PATH_KEYS: frozenset[str] = frozenset({"agent_token_key_path"})
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


# ── System-target refusals (ADR-0047 C15: additive taxonomy, 10+) ─────────────


class SystemProfileNotInstalledError(StoreMigrationError):
    """No system profile (or not ``profile: system``): typed refusal with
    remediation — there is NO auto-install (C1, committee unanimous)."""

    exit_code = 10


class TargetStoreContentError(StoreMigrationError):
    """The system data root already carries store content (C1: the generic
    "target must not exist" rule NARROWS to this for the system target —
    installer scaffolding is allowed, store content is not)."""

    exit_code = 11


class ProfileIsolationError(StoreMigrationError):
    """Source home and the system data root overlap (DR-14, C1)."""

    exit_code = 12


class InstallStateDriftError(StoreMigrationError):
    """System install-state ↔ filesystem drift (DR-17 must be clean, C1)."""

    exit_code = 13


class SourceRecoveryRequiredError(StoreMigrationError):
    """A read-only source probe failed in a way that requires database
    recovery — repair is the OWNER's job; the mover never recovers a
    foreign source (C6)."""

    exit_code = 14


class ActivationGateError(StoreMigrationError):
    """``--apply --target=system`` before the soak closed + the owner's
    word (C13; ADR-0045 Decision 9)."""

    exit_code = 15


class InsufficientSpaceError(StoreMigrationError):
    """Preflight free space below 2x the source volume (C14) — the message
    carries the numbers."""

    exit_code = 16


def activation_gate_open() -> bool:
    """C13 gate state (module-global so tests can exercise both sides: the
    product surface opened by the 2026-10-23 ratification; tests flip the
    constant to False to pin the typed refusal)."""
    return _ACTIVATION_GATE_OPEN


# ── Connection helpers ────────────────────────────────────────────────────────


def _ro_uri(db_path: Path) -> str:
    """Read-only SQLite URI with the path percent-encoded (review P3b).

    A literal ``?``/``%``/``#`` in a store path would otherwise be parsed as
    URI syntax (query/fragment) and break the open or silently retarget it.
    """
    return f"file:{quote(str(db_path))}?mode=ro"


def _open_conn(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a store database with Row access.

    Prefers a read-write open so SQLite can recover a stale ``-shm`` after a
    killed writer (the quiesce gate guarantees no live writers); falls back
    to a read-only URI open when the filesystem refuses writes.
    ``read_only=True`` skips the RW attempt entirely — dry-run planning must
    never create WAL sidecars or trigger recovery on the source (review P3d).
    """
    if read_only:
        ro_conn = sqlite3.connect(_ro_uri(db_path), uri=True, timeout=5.0)
        ro_conn.row_factory = sqlite3.Row
        return ro_conn
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        conn.execute("SELECT 1").fetchone()
    except sqlite3.OperationalError:
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        conn = sqlite3.connect(_ro_uri(db_path), uri=True, timeout=5.0)
    if conn is None:  # both connects either succeed or raise — fail closed
        raise sqlite3.OperationalError("store database could not be opened")
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


def read_store_stats(
    db_path: Path,
    *,
    read_only: bool = False,
    on_rows: Callable[[int], None] | None = None,
) -> StoreStats:
    """One full pass over ``memories``: counters, digests, rewrite counts.

    ``read_only=True`` (dry-run planning, and EVERY source probe under the
    system target when the effective uid is not the source owner, C6)
    opens the database through the read-only URI so the source gains no
    WAL sidecars from the read. Bulk reads fetch in batches (C14) — a
    full-table fetch used to buffer the whole table in RAM.
    """
    conn = _open_conn(db_path, read_only=read_only)
    status_counter: Counter[str] = Counter()
    project_counter: Counter[str] = Counter()
    subtype_swaps = 0
    project_swaps = 0
    duplicates_removed = 0
    trust_markers_kept = 0
    unparseable = 0
    ids: list[str] = []
    try:
        cursor = conn.execute("SELECT id, tags, project, status FROM memories")
        while True:
            rows = cursor.fetchmany(_FETCH_BATCH_ROWS)
            if not rows:
                break
            for row in rows:
                memory_id = str(row["id"])
                ids.append(memory_id)
                status_counter[str(row["status"])] += 1
                # The breakdown reports the slugs AS STORED in this database
                # (the source keeps mnemos, the migrated target keeps vesma);
                # the re-slag counter tracks how many would change.
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
            if on_rows is not None:
                on_rows(len(ids))
    finally:
        conn.close()
    ids.sort()
    digest = hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()
    return StoreStats(
        total=len(ids),
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


def is_already_migrated(layout: _StoreLayout) -> bool:
    """True when the source carries POSITIVE evidence of a past migration.

    Evidence: the idempotence marker file, or a canonical ``vesma.db``
    (data/ or the home root) beside the resolved database. The mere ABSENCE
    of ``mnemos:*`` tags is NOT evidence — a legacy store that never used
    the namespace must stay migratable (review P3a: the old "zero
    re-slaggable tags" heuristic false-positived on exactly that).
    """
    if _marker_file(layout.data_dir).is_file():
        return True
    return (layout.data_dir / CANONICAL_DB_NAME).is_file() or (
        layout.home / CANONICAL_DB_NAME
    ).is_file()


# ── Quiesce gate ──────────────────────────────────────────────────────────────


def quiesce_problems(
    db_paths: list[Path], *, with_wal_sampling: bool
) -> tuple[list[str], list[str]]:
    """Probe the source databases for live connections.

    Returns ``(fatal_problems, warnings)``. Fatal: a held write lock or
    WAL growth between samples — the operator must stop the vesma service.
    Warning-only: the frozen-name unix socket exists (stale sockets survive
    a stopped service; the database probes decide); a read-only source
    FILESYSTEM fails the write probe without any live writer and is
    reported honestly instead of as "database is locked" (review P3e).
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
            message = str(exc)
            if "readonly" in message or "read-only" in message:
                warnings.append(
                    f"{db_path}: source is on a read-only filesystem — the "
                    "write-lock probe was skipped (nothing can write there)"
                )
                continue
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


def _copy_sqlite(
    src: Path, dst: Path, *, progress: Callable[[int, int], None] | None = None
) -> None:
    """Consistent copy via the SQLite backup API (WAL content included).

    ``progress(remaining_pages, total_pages)`` fires per step when the
    database is large enough for a STEPPED backup (C14) — never a
    wall-clock timeout, the page loop runs as long as the copy needs.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    source = sqlite3.connect(_ro_uri(src), uri=True)
    try:
        target = sqlite3.connect(str(dst))
        try:
            if (
                progress is not None
                and src.is_file()
                and src.stat().st_size > _STEPPED_BACKUP_THRESHOLD_BYTES
            ):
                source.backup(
                    target,
                    pages=_STEPPED_BACKUP_PAGES,
                    progress=lambda status, remaining, total: progress(int(remaining), int(total)),
                )
            else:
                source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    _harden_file(dst)  # the backup-API copy must not inherit umask defaults


def _copy_tree(
    src: Path,
    dst: Path,
    warnings: list[str] | None = None,
    progress_cb: Callable[[], None] | None = None,
) -> tuple[int, int]:
    """Byte-for-byte recursive copy; sqlite files go through the backup API.

    SQLite ``-wal``/``-shm`` sidecars are SKIPPED: every sqlite main file
    is merged through the backup API, and a stale sidecar copied next to a
    merged copy would be replayed on open (corruption risk).
    Non-regular entries (fifos, sockets, devices, broken symlinks) are
    SKIPPED too — ``copy2`` would hang on a fifo — and reported as warning
    lines (paths only, review P3-fifo).

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
            if not src_file.is_file():
                if warnings is not None:
                    warnings.append(f"{src_file}: skipped non-regular file (fifo/socket)")
                continue
            dst_file = dst_dir / name
            if _is_sqlite(src_file):
                _copy_sqlite(src_file, dst_file)
                via_backup += 1
            else:
                shutil.copy2(src_file, dst_file)
                _harden_file(dst_file)
            copied += 1
            if progress_cb is not None:
                progress_cb()
    return (copied, via_backup)


@dataclass(frozen=True)
class _Snapshot:
    layout: _StoreLayout
    path: Path
    stats: StoreStats  # stats of the SNAPSHOT copy (authoritative for the run)
    warnings: tuple[str, ...] = ()  # skipped non-regular entries (paths only)


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


def _sibling_sqlite_digests(source: _StoreLayout) -> dict[str, str]:
    """(relpath → sha256) of every sqlite main file under the data root
    besides the main db — component scratch databases included (F8).

    Sidecars are excluded (transient by design; their content folds into
    the main file at the writer's checkpoint), and — flat layout only —
    the home-root non-data entries stay out: they ride their own contract
    branches (vault/config) or are not store data (logs/cache), the SAME
    scoping as :func:`_snapshot_data_files`.
    """
    digests: dict[str, str] = {}
    flat_layout = source.data_dir == source.home
    for entry in sorted(source.data_dir.iterdir()):
        if entry.name == source.db_path.name or _is_sidecar(entry.name):
            continue
        if _is_non_data_entry(entry, flat_layout=flat_layout):
            continue
        if entry.is_file():
            if _is_sqlite(entry):
                digests[entry.name] = _sha256_file(entry)
        elif entry.is_dir():
            for root, _dirs, files in os.walk(entry):
                for name in files:
                    if _is_sidecar(name):
                        continue
                    path = Path(root) / name
                    if path.is_file() and _is_sqlite(path):
                        digests[str(path.relative_to(source.data_dir))] = _sha256_file(path)
    return digests


def _assert_sibling_sqlite_untouched(before: dict[str, str], after: dict[str, str]) -> None:
    """C6/F8: refuse when any sibling sqlite drifted across the copy window.

    A byte change, a NEW sqlite born mid-copy or a vanished one are all a
    live component writer — the run stops instead of migrating a source
    that was moving (names only in the refusal; content never echoed).
    """
    changed = sorted(name for name, digest in after.items() if before.get(name) != digest)
    gone = sorted(set(before) - set(after))
    drifted = changed + [f"{name} (vanished)" for name in gone]
    if drifted:
        names = ", ".join(drifted[:5]) + ("…" if len(drifted) > 5 else "")
        raise SnapshotError(
            "sibling sqlite changed while the snapshot was taken (live "
            f"component writer?): {names} — stop the vesma service "
            "components and retry"
        )


def make_snapshot(
    source: _StoreLayout,
    snapshot_dir: Path,
    *,
    source_read_only: bool = False,
    chown_to: tuple[int, int] | None = None,
) -> _Snapshot:
    """Snapshot-gate (brief item 3): consistent copy + verification.

    ``source_read_only`` (C6, system target): the liveness comparison
    reads the live source through the read-only URI when the effective
    uid is not the source owner. ``chown_to`` (C9, system target): the
    rollback artifact is handed to the SOURCE owner (operator inspects
    and deletes it without sudo); a failed chown is a loud warning, not
    a silent root-owned artifact."""

    copy_warnings: list[str] = []
    sibling_before = _sibling_sqlite_digests(source)
    try:
        (snapshot_dir / "data").mkdir(parents=True)
        _harden_dir(snapshot_dir)
        _harden_dir(snapshot_dir / "data")
        _copy_sqlite(source.db_path, snapshot_dir / "data" / source.db_path.name)
        # Non-regular entries at the data-dir top level never enter the
        # _snapshot_data_files listing — report them instead of dropping
        # them silently (review P3-fifo; the vault case is handled inside
        # _copy_tree).
        for entry in sorted(source.data_dir.iterdir()):
            if entry.is_file() or entry.is_dir() or _is_sidecar(entry.name):
                continue
            copy_warnings.append(f"{entry}: skipped non-regular file (fifo/socket)")
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
                _copy_tree(item, target, copy_warnings)
        if source.vault_dir.is_dir():
            _copy_tree(source.vault_dir, snapshot_dir / "vault", copy_warnings)
        if source.config_path.is_file():
            shutil.copy2(source.config_path, snapshot_dir / "config.yaml")
            _harden_file(snapshot_dir / "config.yaml")
    except (OSError, sqlite3.Error) as exc:
        # sqlite3.Error joins OSError here (P2-cleanup class): a corrupt or
        # unreadable source database failing the backup API is a SNAPSHOT
        # failure (exit 5), not a raw traceback (exit 1).
        raise SnapshotError(f"snapshot copy failed: {exc}") from exc

    if chown_to is not None:
        uid, gid = chown_to
        chown_failed = False
        for owned in (snapshot_dir, *snapshot_dir.rglob("*")):
            try:
                os.chown(owned, uid, gid)
            except OSError:
                chown_failed = True
        if chown_failed:
            copy_warnings.append(
                f"{snapshot_dir}: could not hand the snapshot to the source "
                "owner — the rollback artifact stays root-owned (C9 wants "
                "operator-inspectable; adjust manually)"
            )

    # Verify the snapshot BEFORE continuing (open + counters), and confirm
    # the source has not moved under us since the snapshot was taken.
    snapshot_layout = _resolve_layout(snapshot_dir)
    snap_stats = read_store_stats(snapshot_layout.db_path)
    try:
        live_stats = read_store_stats(source.db_path, read_only=source_read_only)
    except (OSError, sqlite3.Error) as exc:
        # P3-1: the apply-phase source re-read goes through the SAME typed
        # classifier as the plan phase — a source that turned
        # recovery-needing between plan and apply is exit 14, never a raw
        # traceback.
        raise _classify_source_ro_failure(source.db_path, exc) from exc
    if snap_stats.total != live_stats.total or snap_stats.ids_digest != live_stats.ids_digest:
        raise SnapshotError(
            "the source store changed while the snapshot was taken "
            "(live writer detected) — stop the vesma service and retry"
        )
    # C6/F8: the same "not moved under us" verdict for EVERY sibling
    # sqlite under the data root — the copy window must leave them
    # byte-identical (a component writer stops the run by name).
    _assert_sibling_sqlite_untouched(sibling_before, _sibling_sqlite_digests(source))
    return _Snapshot(
        layout=snapshot_layout,
        path=snapshot_dir,
        stats=snap_stats,
        warnings=tuple(copy_warnings),
    )


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
        if key == "db_name":
            # ALWAYS pin the canonical name (review P3c): a misconfigured
            # legacy value (anything but vesma.db) must not survive — the
            # materialized database IS vesma.db, and a carried-over custom
            # name would leave the produced config pointing at nothing.
            rerooted[key] = CANONICAL_DB_NAME
            db_name_pinned = value != CANONICAL_DB_NAME
            continue
        new_value, moved = _reroot(value, source.home, new_home)
        if moved:
            paths_rerooted += 1
        rerooted[key] = new_value
    vesma_section = rerooted
    # ALWAYS append the pin, even when the source config carried NO db_name
    # key at all (owner directive 2026-10-08; the 08.10 production incident:
    # the pre-fix mover left the key absent, the service then fell back to
    # its default db name and silently read an empty auto-created database).
    # The materialized target database IS vesma.db unconditionally, so the
    # migrated config must name it unconditionally too.
    if "db_name" not in vesma_section:
        vesma_section["db_name"] = CANONICAL_DB_NAME
        db_name_pinned = True

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


def _materialize_target(
    snapshot: _Snapshot,
    staging: Path,
    *,
    progress: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Build the 6.0 target from the verified snapshot; re-slag in place.

    C14 fast path: when the plan carries ZERO rewrite counters (a 6.0
    source — the EXPECTED case for the system target) the UPDATE sweep is
    skipped entirely and the run is a pure backup-API copy; the FTS index
    rides inside the copied pages and needs no rebuild.
    """
    staging.mkdir(parents=True)
    _harden_dir(staging)
    src_data = snapshot.layout.data_dir
    dst_data = staging / "data"
    dst_data.mkdir()
    _harden_dir(dst_data)
    # The snapshot keeps the SOURCE db name (legacy mnemos.db for a 5.x
    # store, vesma.db for an already-migrated 6.0 source on the system
    # target) — the main copy is name-agnostic.
    main_db_name = snapshot.layout.db_path.name
    subtype_swaps = 0
    project_swaps = 0
    duplicates_removed = 0
    trust_markers_kept = 0
    files_copied = 0

    def _emit(message: str) -> None:
        if progress is not None:
            progress(message)

    def _copied() -> None:
        nonlocal files_copied
        files_copied += 1
        if files_copied % _PROGRESS_EVERY_FILES == 0:
            _emit(f"  staging: {files_copied} files copied")

    for item in _snapshot_data_files(
        src_data, main_db_name, flat_layout=src_data == snapshot.layout.home
    ):
        target = dst_data / item.name
        if item.is_file():
            if _is_sqlite(item):
                _copy_sqlite(item, target)
            else:
                shutil.copy2(item, target)
                # Harden HERE too (residual P3), not only at snapshot time:
                # the 0600 must be enforced at every write, never relied on
                # transitively from the snapshot copy's preserved mode.
                _harden_file(target)
            _copied()
        else:
            _copy_tree(item, target, progress_cb=_copied)

    # Vault rides from the snapshot byte-for-byte (config.yaml is NOT copied
    # from the snapshot — migrate_config writes the canonical one).
    snapshot_vault = snapshot.layout.vault_dir
    if snapshot_vault.is_dir():
        _copy_tree(snapshot_vault, staging / "vault", progress_cb=_copied)

    target_db = dst_data / CANONICAL_DB_NAME
    row_count = 0
    stats = snapshot.stats
    fast_path = (
        stats.subtype_swaps == 0 and stats.project_swaps == 0 and stats.duplicates_removed == 0
    )
    if fast_path:
        _emit("  fast path: zero rewrites — pure backup-API copy (no UPDATE sweep)")
        _copy_sqlite(src_data / main_db_name, target_db)
        conn = _open_conn(target_db)
        try:
            row_count = int(conn.execute("SELECT count(*) FROM memories").fetchone()[0])
        finally:
            conn.close()
    else:
        _emit("  re-slag sweep: rewriting the tag namespace in the staging copy")
        _copy_sqlite(src_data / main_db_name, target_db)
        conn = sqlite3.connect(str(target_db), timeout=30.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute("SELECT id, tags, project FROM memories")
            while True:
                rows = cursor.fetchmany(_FETCH_BATCH_ROWS)
                if not rows:
                    break
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
                row_count += len(rows)
                if row_count % _PROGRESS_EVERY_ROWS == 0:
                    _emit(f"  re-slag: {row_count} rows rewritten")
            # FTS rebuild (draft requirement; belt-and-braces beyond the
            # AFTER UPDATE triggers) — then commit and checkpoint. The
            # checkpoint targets the STAGING copy only — never the source
            # (C6: the source is not written, its WAL is never truncated).
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
        "fast_path": fast_path,
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
        "fast_path": int(fast_path),
    }


def _canonical_tags(raw: str) -> str:
    """Serialization-independent canonical form of a tags value.

    The UPDATE sweep re-serializes tags through ``json.dumps``; the fast
    path (C14) preserves the stored bytes. Comparing the EXPECTED rewrite
    against the target must therefore compare the tag LIST, not the byte
    formatting — both sides canonicalize before hashing."""
    try:
        parsed = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        return raw
    return json.dumps(parsed, ensure_ascii=False)


def _field_digest(row: sqlite3.Row, *, normalized: bool) -> str:
    values: list[str] = []
    for name in _SAMPLE_FIELDS:
        value = row[name]
        if name == "tags":
            value = reslag_tags_json(str(value))[0] if normalized else _canonical_tags(str(value))
        elif normalized and name == "project":
            value = reslag_project_slug(str(value))[0]
        values.append("" if value is None else str(value))
    return hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()


def _sample_size_for(total_rows: int) -> int:
    """C8: 100 seeded samples above a million rows, 10 otherwise."""
    return _SAMPLE_SIZE_LARGE if total_rows > _LARGE_STORE_ROWS else _SAMPLE_SIZE


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

    # Seeded sample records (10; 100 above a million rows, C8): per-field
    # checksums equal under the EXPECTED rewrite (source normalized,
    # target raw).
    rng = random.Random(_SAMPLE_SEED)  # nosec B311 - seeded verification sampling, not security
    ids = list(snap_stats.ids)
    sample_size = _sample_size_for(snap_stats.total)
    sample_ids = rng.sample(ids, min(sample_size, len(ids)))
    sample_mismatches = 0
    snap_conn = _open_conn(snapshot.layout.db_path)
    tgt_conn = _open_conn(target_db)
    try:
        columns = ", ".join(_SAMPLE_FIELDS)
        for memory_id in sample_ids:
            snap_row = snap_conn.execute(
                f"SELECT {columns} FROM memories WHERE id = ?",  # nosec B608 - static column list
                (memory_id,),
            ).fetchone()
            tgt_row = tgt_conn.execute(
                f"SELECT {columns} FROM memories WHERE id = ?",  # nosec B608 - static column list
                (memory_id,),
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


# ── System target (ADR-0047: install-state + FHS manifest resolved) ──────────


_MIGRATED_CONFIG_INFIX = ".migrated-"
#: Store-content entry names the target data root must NOT carry before a
#: run (C1: the generic "--to must not exist" rule NARROWS to this for the
#: system target — installer scaffolding is allowed, store content is not).
_STORE_CONTENT_ENTRY_NAMES: tuple[str, ...] = (
    CANONICAL_DB_NAME,
    LEGACY_DB_NAME,
    "data",
    "vault",
)


class SystemQuiesce:
    """Result rows of one C7 gate pass (fatal problems + warnings)."""

    def __init__(self, fatal: list[str], warnings: list[str]) -> None:
        self.fatal = fatal
        self.warnings = warnings


def _system_control_socket(prefix: Path | None = None) -> Path:
    """The SYSTEM-scope control socket, resolved through the FHS manifest.

    A NEW constant of the system-target contract (C7) — deliberately NOT
    the user-scope socket resolver and not the frozen 6.0 source socket.
    """
    return fhs.system_ipc_dir(prefix) / "control.sock"


def _run_systemctl(args: list[str]) -> str:
    """Seam: ``systemctl <args>`` stdout (system scope). Tests monkeypatch
    this — the real probe is a fixed-argv subprocess, best-effort: any
    failure yields an empty string (the file probes decide, C7)."""
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return ""
    try:
        result = subprocess.run(  # nosec B603 - fixed argv, no shell
            [systemctl, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip()


def _probe_system_unit() -> tuple[str, str]:
    """(active-state, enabled-state) of the system-scope vesma unit."""
    return (
        _run_systemctl(["is-active", "vesma.service"]),
        _run_systemctl(["is-enabled", "vesma.service"]),
    )


def _sqlites_under(root: Path) -> list[Path]:
    """Every sqlite main file under a tree (WAL/SHM never match)."""
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file() and _is_sqlite(p))


def _target_file_problems(data_root: Path, *, with_wal_sampling: bool) -> list[str]:
    """The DECISIVE C7 leg: write-lock + WAL probes over every sqlite
    already under the system data root (component data included)."""
    fatal: list[str] = []
    for db_path in _sqlites_under(data_root):
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
                    f"{wal_path}: WAL is growing — a live process is writing "
                    "under the system data root"
                )
    return fatal


def system_quiesce_problems(
    data_root: Path,
    *,
    with_wal_sampling: bool,
    prefix: Path | None = None,
) -> SystemQuiesce:
    """C7 target quiesce, triple gate. (1) the system-scope unit state:
    active/activating is FATAL (exit 4), a merely enabled unit is a warning
    (timer/socket activation); (2) the system control socket exists —
    warning only (stale sockets are common); (3) the file probes decide."""
    fatal: list[str] = []
    warnings: list[str] = []
    active, enabled = _probe_system_unit()
    if active in ("active", "activating"):
        fatal.append(
            f"vesma.service (system scope) is {active} — stop it first: "
            "sudo systemctl stop vesma.service"
        )
    elif active == "":
        warnings.append(
            "systemctl probe unavailable — the file probes under the data root decide (C7)"
        )
    elif enabled == "enabled":
        warnings.append(
            "vesma.service (system scope) is enabled — timer/socket "
            "activation could start it mid-run; consider disabling it for "
            "the migration window"
        )
    sock = _system_control_socket(prefix)
    if sock.exists():
        warnings.append(
            f"{sock} exists — if the supervisor is running, stop it (a "
            "stale socket is harmless; the file probes decide)"
        )
    fatal.extend(_target_file_problems(data_root, with_wal_sampling=with_wal_sampling))
    return SystemQuiesce(fatal=fatal, warnings=warnings)


def _source_probe_read_only(layout: _StoreLayout) -> bool:
    """C6: true when the effective uid differs from the source owner —
    then EVERY source probe goes through read-only URIs (the mover may run
    as root against a user-owned store; root COULD write the source, and
    must not)."""
    return os.geteuid() != layout.db_path.stat().st_uid


def _source_problems_system(
    layout: _StoreLayout, *, read_only_probes: bool, with_wal_sampling: bool
) -> tuple[list[str], list[str]]:
    """Source-side quiesce under the system target (C6).

    The system run probes the source READ-ONLY: the write-lock probe is
    skipped (a write handle — even root's — is a C6 violation), liveness
    stays gated by WAL sampling plus the snapshot digest comparison. The
    caller owns the honest warning line."""
    if not read_only_probes:
        return quiesce_problems(
            [layout.db_path, *_sibling_sqlite_dbs(layout)],
            with_wal_sampling=with_wal_sampling,
        )
    warnings: list[str] = []
    fatal: list[str] = []
    for db_path in [layout.db_path, *_sibling_sqlite_dbs(layout)]:
        wal_path = db_path.parent / f"{db_path.name}-wal"
        if with_wal_sampling and wal_path.is_file():
            size_before = wal_path.stat().st_size
            time.sleep(_QUIESCE_WAL_SAMPLE_SECONDS)
            if wal_path.stat().st_size != size_before:
                fatal.append(
                    f"{wal_path}: WAL is growing — a live process is writing to the source store"
                )
    return (fatal, warnings)


def _classify_source_ro_failure(db_path: Path, exc: Exception) -> StoreMigrationError:
    """C6 typed mapping: a read-only probe that fails because the database
    needs RECOVERY is the owner's repair job — never the mover's."""
    text = str(exc).lower()
    if (
        "readonly" in text
        or "read-only" in text
        or "recover" in text
        or "unable to open database file" in text
    ):
        return SourceRecoveryRequiredError(
            f"{db_path}: a read-only probe failed in a way that requires "
            "database recovery (a stale WAL sidecar from a killed writer) — "
            "repair is the source OWNER's job; the mover never recovers a "
            "source it does not own (fix: open and close the store once as "
            "the owning account, or stop its writer cleanly)"
        )
    return UsageError(f"source database unreadable while planning: {db_path}: {exc}")


def _roots_disjoint(a: Path, b: Path) -> bool:
    """False when one root IS the other or contains it (DR-14, C1)."""
    return not (a == b or a in b.parents or b in a.parents)


def _store_content_present(data_root: Path) -> list[str]:
    """Store-content entries found in the target data root (C1)."""
    present = [name for name in _STORE_CONTENT_ENTRY_NAMES if (data_root / name).exists()]
    if (data_root / _MARKER_FILE_NAME).exists():
        present.append(_MARKER_FILE_NAME)
    return present


def _system_preconditions(prefix: Path | None) -> Path:
    """C1 preconditions that do not involve the source; returns the
    resolved data root. Order is fail-fast: install-state → isolation is
    checked by the caller (needs the source) → drift → freshness."""
    from vesma.service import system_install

    try:
        state = system_install.read_system_state(prefix)
    except system_install.SystemInstallError as exc:
        raise SystemProfileNotInstalledError(
            f"the system install-state cannot be used: {exc} — remediation: "
            "`sudo vesma system install`, stop the service "
            "(`sudo systemctl stop vesma.service`), re-run this command"
        ) from exc
    if state is None:
        raise SystemProfileNotInstalledError(
            "no system profile is installed on this machine (no install-"
            "state) — remediation: `sudo vesma system install`, "
            "stop the service, re-run this command. The mover NEVER "
            "installs the profile itself (no auto-install, C1)"
        )
    profile = state.get("profile")
    if profile != SYSTEM_TARGET:
        raise SystemProfileNotInstalledError(
            f"the system install-state says profile={profile!r}, expected "
            f"'{SYSTEM_TARGET}' — refusing (C1; a cross-contaminated state "
            "is a trust-anchor problem: run `vesma doctor`)"
        )
    data_root = fhs.system_data_root(prefix)
    drift = system_install.system_state_drift(prefix)
    if drift:
        head = "; ".join(drift[:3])
        more = f" (+{len(drift) - 3} more)" if len(drift) > 3 else ""
        raise InstallStateDriftError(
            "system install-state ↔ filesystem drift must be clean before "
            f"a migration (DR-17 / C1): {head}{more} — inventory with "
            "`vesma doctor`, resolve the drift, then re-run"
        )
    if not data_root.is_dir():
        raise InstallStateDriftError(
            f"the system data root {data_root} is missing while the "
            "install-state records it (DR-17 / C1)"
        )
    present = _store_content_present(data_root)
    if present:
        raise TargetStoreContentError(
            "the system data root already carries store content: "
            + ", ".join(present)
            + " — remove or adopt it explicitly first (installer "
            "scaffolding is allowed; store content is not, C1)"
        )
    return data_root


def _remaining_cargo_advisory(source_home: Path, prefix: Path | None) -> list[str]:
    """C2 honesty: what the mover does NOT carry, with owner commands."""
    env_dir = fhs.system_env_dir(prefix)
    components_dir = fhs.system_components_dir(prefix)
    checks: tuple[tuple[str, str], ...] = (
        (
            "env.d",
            f"env secrets are NOT carried (never the mover's cargo): copy "
            f"them manually into {env_dir} as root, mode 0600 (operator)",
        ),
        (
            "components.d",
            f"component manifests are NOT carried: re-provision them under "
            f"{components_dir} (operator)",
        ),
        (
            "components",
            "component data dirs are NOT carried: move them manually if "
            "the components return on the system profile (operator)",
        ),
        (
            "venv",
            "the engine venv is NOT carried: materialize it with "
            "`sudo vesma system install` (installer-owned)",
        ),
        (
            "venvs",
            "component venvs are NOT carried: materialize them with "
            "`sudo vesma system install` (installer-owned)",
        ),
        (
            "systemd",
            "unit artifacts are NOT carried: `sudo vesma service install "
            "--system` installs the system unit",
        ),
        ("logs", "file logs are not store data (journald is primary)"),
    )
    return [f"{name}/: {text}" for name, text in checks if (source_home / name).exists()]


def _is_under(path: Path, root: Path) -> bool:
    """True when ``path`` IS ``root`` or lives under it."""
    return path == root or root in path.parents


def _secret_path_warnings(layout: _StoreLayout) -> list[str]:
    """C10: out-of-home secret-path overrides in the source config. Key
    NAMES only — values are never printed (output privacy)."""
    import yaml
    from pydantic import BaseModel

    from vesma.config import Settings

    if not layout.config_path.is_file():
        return []
    try:
        doc = yaml.safe_load(layout.config_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return []  # migrate_config refuses on the same file with its own error
    if not isinstance(doc, dict):
        return []
    settings_fields = Settings.model_fields
    warnings: list[str] = []
    for section_name, section in doc.items():
        if not isinstance(section, dict):
            continue
        model_field = settings_fields.get(str(section_name))
        if model_field is None:
            continue
        annotation = model_field.annotation
        if not (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
            continue
        for key, sub_field in annotation.model_fields.items():
            value = section.get(key)
            if value is None:
                continue
            named_secret = key in _SECRET_PATH_KEYS
            path_typed = _annotation_is_path(sub_field.annotation)
            if not (named_secret or path_typed):
                continue
            raw = Path(str(value)).expanduser()
            try:
                resolved = raw.resolve()
            except OSError:
                continue
            if not _is_under(resolved, layout.home):
                warnings.append(
                    f"config key {section_name}.{key} points outside the "
                    "source home — NOT carried by the mover (re-provision "
                    "it on the target); the value is not printed"
                )
    return warnings


def _totp_precondition_lines(layout: _StoreLayout, prefix: Path | None) -> list[str]:
    """C10: the TOTP master key is ENV-ONLY and never travels with the
    store — the printed precondition, plus an env-dir existence advisory."""
    lines: list[str] = []
    totp_enabled = False
    if layout.config_path.is_file():
        import yaml

        try:
            doc = yaml.safe_load(layout.config_path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            doc = None
        if isinstance(doc, dict) and isinstance(doc.get("api"), dict):
            totp_enabled = bool(doc["api"].get("totp_enabled", False))
    env_dir = fhs.system_env_dir(prefix)
    if totp_enabled:
        lines.append(
            "TOTP is enabled: the TOTP blobs stay decryptable ONLY with "
            "VESMARO_API__TOTP_MASTER_KEY provisioned for the system "
            "service (env-only — it never lives in the store home). "
            f"Precondition: provide it in {env_dir} (mode 0600) BEFORE the "
            "service starts on the migrated store"
        )
    if not env_dir.is_dir() or not any(env_dir.iterdir()):
        lines.append(
            f"the system env dir {env_dir} is empty or absent — env-only "
            "secrets (e.g. the TOTP master key, if used) must be provisioned "
            "there before service start (operator action)"
        )
    return lines


def _validate_snapshot_dir(
    snapshot_dir: Path, source_home: Path, data_root: Path, config_root: Path
) -> None:
    """C9 placement: never inside the source or the target trees, never
    under the system roots (and never their parents — no garbage there)."""
    forbidden: tuple[Path, ...] = (
        source_home,
        data_root,
        data_root.parent,
        config_root,
        config_root.parent,
    )
    for root in forbidden:
        if _is_under(snapshot_dir, root):
            raise UsageError(
                f"--snapshot-dir {snapshot_dir} sits inside {root} — the "
                "snapshot never lands inside the source or the system "
                "target trees (C9); choose a location outside both"
            )
    if snapshot_dir.exists():
        raise UsageError(
            f"--snapshot-dir {snapshot_dir} already exists — choose a fresh snapshot location"
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_parity_problems(snapshot_root: Path, staging_root: Path) -> list[str]:
    """C8: per-file sha256 parity of every NON-sqlite regular file (vault
    and other plain files).

    sqlite ``-wal``/``-shm`` sidecars are excluded on BOTH sides: the
    mover never copies them (their content folds in through the backup
    API), and a READ-ONLY open of a WAL-mode database materializes
    transient sidecars beside the copy — the staging build reads the
    snapshot's databases, so the snapshot tree legitimately carries
    them mid-run. They are sqlite read machinery, never cargo."""
    snap_files = {
        p.relative_to(snapshot_root): p
        for p in snapshot_root.rglob("*")
        if p.is_file()
        and not _is_sqlite(p)
        and not _is_sidecar(p.name)
        and p.name != _MARKER_FILE_NAME
    }
    stag_files = {
        p.relative_to(staging_root): p
        for p in staging_root.rglob("*")
        if p.is_file()
        and not _is_sqlite(p)
        and not _is_sidecar(p.name)
        and p.name != _MARKER_FILE_NAME
    }
    problems: list[str] = []
    for rel in sorted(set(snap_files) | set(stag_files)):
        src = snap_files.get(rel)
        dst = stag_files.get(rel)
        if src is None:
            problems.append(f"{rel}: in staging but not in the snapshot")
            continue
        if dst is None:
            problems.append(f"{rel}: in the snapshot but missing in staging")
            continue
        if _sha256_file(src) != _sha256_file(dst):
            problems.append(f"{rel}: sha256 mismatch between snapshot and staging")
    return problems


def _table_counts(db_path: Path) -> dict[str, int]:
    conn = _open_conn(db_path)
    try:
        names = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
            )
        ]
        counts: dict[str, int] = {}
        for name in names:
            if '"' in name:
                continue  # never interpolate a hostile identifier
            counts[name] = int(
                conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]  # nosec B608 - quoted identifier from our own copy's sqlite_master
            )
        return counts
    finally:
        conn.close()


def _sibling_table_parity_problems(
    snapshot_data: Path, staging_data: Path, main_db_names: tuple[str, ...]
) -> list[str]:
    """C8: per-table count(*) parity for every sibling database."""
    problems: list[str] = []
    snap_dbs = {
        p.relative_to(snapshot_data): p
        for p in snapshot_data.rglob("*")
        if p.is_file() and _is_sqlite(p) and p.name not in main_db_names
    }
    for rel, snap_db in sorted(snap_dbs.items()):
        stag_db = staging_data / rel
        if not stag_db.is_file():
            problems.append(f"{rel}: sibling database missing in staging")
            continue
        try:
            snap_counts = _table_counts(snap_db)
            stag_counts = _table_counts(stag_db)
        except sqlite3.Error as exc:
            problems.append(f"{rel}: table count parity failed: {exc}")
            continue
        for table in sorted(set(snap_counts) | set(stag_counts)):
            if table not in stag_counts:
                problems.append(f"{rel}: table {table} missing in staging")
            elif table not in snap_counts:
                problems.append(f"{rel}: table {table} absent in the snapshot")
            elif snap_counts[table] != stag_counts[table]:
                problems.append(
                    f"{rel}: table {table} count {stag_counts[table]} != "
                    f"snapshot {snap_counts[table]}"
                )
    return problems


def _key_invariant_problems(snapshot: _Snapshot, data_root: Path) -> list[str]:
    """C10 post-copy invariants: the memory vault is readable (plaintext
    markdown, trivially) and the agent-token signing key traveled with the
    data siblings (tokens must verify without a re-mint)."""
    problems: list[str] = []
    vault = data_root / "vault"
    if vault.is_dir():
        sample = next((p for p in sorted(vault.rglob("*.md")) if p.is_file()), None)
        if sample is not None:
            try:
                sample.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                problems.append(f"vault sample unreadable after the copy: {sample}: {exc}")
    for key_name in ("agent-token-signing.key",):
        src_key = snapshot.layout.data_dir / key_name
        if src_key.is_file() and not (data_root / key_name).is_file():
            problems.append(
                f"the {key_name} existed in the source data dir but is "
                "missing in the target — agent tokens would not verify "
                "without a re-mint (C10 invariant)"
            )
    return problems


def _account_uid(name: str | None) -> int | None:
    if name is None:
        return None
    import pwd

    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError as exc:
        raise SystemProfileNotInstalledError(
            f"system account {name!r} does not exist — the sysusers.d "
            "fragment creates it; remediation: `sudo vesma service install "
            "--system` (applies the fragment), then re-run"
        ) from exc


def _account_gid(name: str | None) -> int | None:
    if name is None:
        return None
    import grp

    try:
        return grp.getgrnam(name).gr_gid
    except KeyError as exc:
        raise SystemProfileNotInstalledError(
            f"system group {name!r} does not exist — the sysusers.d "
            "fragment creates it; remediation: `sudo vesma service install "
            "--system` (applies the fragment), then re-run"
        ) from exc


def _uid_to_name(uid: int) -> str:
    import pwd

    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _gid_to_name(gid: int) -> str:
    import grp

    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


@dataclass(frozen=True)
class _OwnershipExpectation:
    """One C4 row: a staged path with its manifest-derived mode/owner.

    ``staged`` is where the row sits during the pre-publish pass;
    ``path`` is where the same bytes land after the publish rename (the
    post-publish audit stats THAT path)."""

    staged: Path
    path: Path
    role: str
    mode: int
    owner: str | None
    group: str | None


def _publish_ownership_expectations(
    entries: list[_PublishEntry], prefix: Path | None
) -> list[_OwnershipExpectation]:
    """C4: the pass is DERIVED FROM THE MANIFEST ROLES — data-tree dirs
    take the data-root role, files the store-db role, the config the
    shared-config role. Wider than the manifest — never."""
    manifest = fhs.load_fhs_manifest()
    data_entry = manifest.entry("data_root")
    db_entry = manifest.entry("store_db")
    cfg_entry = manifest.entry("shared_config")
    expectations: list[_OwnershipExpectation] = []
    for entry in entries:
        if entry.cross_fs:
            expectations.append(
                _OwnershipExpectation(
                    entry.staged,
                    entry.final,
                    "shared_config",
                    cfg_entry.mode,
                    cfg_entry.owner,
                    cfg_entry.group,
                )
            )
            continue
        # The entry ROOT row first (rglob never yields the root itself):
        # a whole-directory rename still needs its manifest mode.
        if entry.staged.is_dir():
            expectations.append(
                _OwnershipExpectation(
                    entry.staged,
                    entry.final,
                    "data_root",
                    data_entry.mode,
                    data_entry.owner,
                    data_entry.group,
                )
            )
        for node in sorted(entry.staged.rglob("*")):
            final_node = entry.final / node.relative_to(entry.staged)
            if node.is_dir():
                expectations.append(
                    _OwnershipExpectation(
                        node,
                        final_node,
                        "data_root",
                        data_entry.mode,
                        data_entry.owner,
                        data_entry.group,
                    )
                )
            elif node.is_file():
                expectations.append(
                    _OwnershipExpectation(
                        node,
                        final_node,
                        "store_db",
                        db_entry.mode,
                        db_entry.owner,
                        db_entry.group,
                    )
                )
        if entry.staged.is_file():
            expectations.append(
                _OwnershipExpectation(
                    entry.staged,
                    entry.final,
                    "store_db",
                    db_entry.mode,
                    db_entry.owner,
                    db_entry.group,
                )
            )
    return expectations


def _apply_ownership(expectations: list[_OwnershipExpectation], *, apply: bool) -> list[str]:
    """C4 pass over staging: chmod + chown per manifest row. With
    ``apply=False`` (non-root) the pass is a recorded simulation — the
    post-publish audit then checks MODES only, and the report says so."""
    lines = ["ownership pass: per FHS manifest roles (data tree, files, config)"]
    for exp in expectations:
        os.chmod(exp.staged, exp.mode)
        if not apply:
            continue
        uid = _account_uid(exp.owner)
        gid = _account_gid(exp.group)
        if uid is None and gid is None:
            continue
        os.chown(
            exp.staged,
            uid if uid is not None else -1,
            gid if gid is not None else -1,
        )
    if not apply:
        lines.append(
            "ownership: non-root run — owner/group RECORDED as intent but "
            "not applied (the post-publish audit checks modes only)"
        )
    return lines


def _stat_audit_problems(
    expectations: list[_OwnershipExpectation], *, ownership_applied: bool
) -> list[str]:
    """C4/C8 post-publish audit: owner + mode of every published path
    against the manifest-derived expectations. Part of verification."""
    problems: list[str] = []
    for exp in expectations:
        try:
            st = exp.path.stat()
        except OSError as exc:
            problems.append(f"{exp.path}: post-publish stat failed ({exc})")
            continue
        actual_mode = stat.S_IMODE(st.st_mode)
        if actual_mode != exp.mode:
            problems.append(
                f"{exp.path}: mode {format(actual_mode, '04o')} != manifest "
                f"{format(exp.mode, '04o')} (role {exp.role})"
            )
        if ownership_applied:
            if exp.owner is not None and _uid_to_name(st.st_uid) != exp.owner:
                problems.append(
                    f"{exp.path}: owner {_uid_to_name(st.st_uid)} != manifest "
                    f"{exp.owner} (role {exp.role})"
                )
            if exp.group is not None and _gid_to_name(st.st_gid) != exp.group:
                problems.append(
                    f"{exp.path}: group {_gid_to_name(st.st_gid)} != manifest "
                    f"{exp.group} (role {exp.role})"
                )
    return problems


def _assert_no_sidecars(tree: Path) -> None:
    """C14 pin: no ``-wal``/``-shm`` may exist anywhere in staging before
    publish (a stale sidecar beside a merged copy would replay on open)."""
    for path in tree.rglob("*"):
        if path.is_file() and _is_sidecar(path.name):
            raise VerificationError(
                f"staging carries a sqlite sidecar before publish: {path} — "
                "refusing (a stale sidecar would be replayed over the "
                "merged copy)"
            )


def _check_free_space(planned_bytes: int, snapshot_dir: Path, data_root: Path) -> None:
    """C14 preflight: snapshot fs >= 1x source, target fs >= 2x source —
    the refusal carries the numbers. The snapshot fs is measured at its
    parent (the snapshot dir itself is created only on --apply)."""
    requirements = (
        ("snapshot", snapshot_dir.parent, planned_bytes),
        ("target", data_root, planned_bytes * _FREE_SPACE_FACTOR),
    )
    for label, path, needed in requirements:
        try:
            usage = shutil.disk_usage(path)
        except OSError as exc:
            raise InsufficientSpaceError(
                f"cannot measure free space on the {label} filesystem ({path}): {exc}"
            ) from exc
        if usage.free < needed:
            raise InsufficientSpaceError(
                f"insufficient free space on the {label} filesystem "
                f"({path}): {usage.free} bytes free, {needed} bytes needed "
                f"(source volume {planned_bytes} bytes, factor "
                f"{_FREE_SPACE_FACTOR if label == 'target' else 1}) — free "
                "space and retry"
            )


def _migrate_config_system(
    layout: _StoreLayout,
    data_root: Path,
    staging: Path,
) -> tuple[Path, ConfigPlan]:
    """C3/C10 config leg for the system target: the classic rewrite
    (section map, schema validation) re-rooted onto the data root, then
    the FLAT system shape is pinned unconditionally — the store home IS
    the data root, so data_dir/vault_path/db_name must name it. The
    out-of-home secret-path warnings (C10) ride the PLAN (dry-run
    included), not this leg."""
    import yaml

    staged_config, cplan = migrate_config(layout, data_root, staging)
    doc = yaml.safe_load(staged_config.read_text(encoding="utf-8")) or {}
    section = doc.get("vesma")
    if not isinstance(section, dict):  # migrate_config guarantees the section
        section = {}
        doc["vesma"] = section
    pins: list[str] = []
    for key, value in (
        ("data_dir", str(data_root)),
        ("vault_path", str(data_root / "vault")),
        ("db_name", CANONICAL_DB_NAME),
    ):
        if section.get(key) != value:
            section[key] = value
            pins.append(key)
    if pins:
        staged_config.write_text(
            yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        _harden_file(staged_config)
    return staged_config, cplan


@dataclass(frozen=True)
class _PublishEntry:
    """One piecewise-publish move (C5): staging path → final path."""

    staged: Path
    final: Path
    cross_fs: bool
    overwrite_ok: bool = False  # the C3 empty-conffile replacement case


def _plan_publish_entries(
    staging: Path, data_root: Path, config_staged: Path, config_final: Path
) -> list[_PublishEntry]:
    """The publish map: staging ``data/*`` flattens ONTO the data root
    (store home == data root), ``vault/`` keeps its name, the config goes
    to its own plane (cross-filesystem by design). ``overwrite_ok`` marks
    the C3 disposition where the target conffile exists but is EMPTY (the
    ratified "empty file gets the write" case)."""
    entries: list[_PublishEntry] = []
    staged_data = staging / "data"
    for child in sorted(staged_data.iterdir()):
        entries.append(_PublishEntry(child, data_root / child.name, cross_fs=False))
    if (staging / "vault").is_dir():
        entries.append(_PublishEntry(staging / "vault", data_root / "vault", cross_fs=False))
    overwrite_config = (
        config_final.is_file() and not config_final.read_text(encoding="utf-8").strip()
    )
    entries.append(
        _PublishEntry(
            config_staged,
            config_final,
            cross_fs=True,
            overwrite_ok=overwrite_config,
        )
    )
    return entries


def _publish_entry(entry: _PublishEntry) -> None:
    if not entry.cross_fs:
        os.rename(entry.staged, entry.final)
        return
    # Cross-filesystem (config → its plane): copy to a tmp name in the
    # destination dir, fsync, atomic replace. copy2 carries the mode the
    # ownership pass set on the staged file — but NOT the owner/group
    # (shutil never chowns): the tmp file would land root:root on a root
    # run and fail the C4 post-publish stat audit. The staged file is the
    # ownership-pass truth (C4 ran over it pre-publish), so its uid/gid
    # ride onto the tmp fd before the replace. Non-root runs keep the
    # recorded-intent contract: the audit checks modes only there.
    tmp = entry.final.parent / f".{entry.final.name}.publish-tmp"
    shutil.copy2(entry.staged, tmp)
    fd = os.open(tmp, os.O_RDONLY)
    try:
        if os.geteuid() == 0:
            staged_stat = entry.staged.stat()
            os.fchown(fd, staged_stat.st_uid, staged_stat.st_gid)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, entry.final)
    _fsync_dir(entry.final.parent)


def _rollback_published(published: list[_PublishEntry]) -> list[str]:
    """C5 finally-path: undo published entries in reverse (the snapshot is
    the rollback artifact; staging is removed by the caller)."""
    notes: list[str] = []
    for entry in reversed(published):
        try:
            if entry.final.is_dir() and not entry.final.is_symlink():
                shutil.rmtree(entry.final)
            elif entry.final.exists() or entry.final.is_symlink():
                entry.final.unlink()
            notes.append(f"rolled back: {entry.final}")
        except OSError as exc:
            notes.append(f"rollback FAILED for {entry.final}: {exc}")
    return notes


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
    # ── system-target extensions (ADR-0047; defaults keep the B3 shape) ──
    target: str = USER_TARGET
    data_root: Path | None = None
    system_config_path: Path | None = None
    source_owner_uid: int | None = None
    wal_bytes: int = 0
    fast_path: bool = False
    remaining_cargo: tuple[str, ...] = ()
    precondition_lines: tuple[str, ...] = ()
    secret_path_warnings: tuple[str, ...] = ()


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


def build_plan(
    source_home: Path,
    target_home: Path | None,
    *,
    apply: bool,
    target: str = USER_TARGET,
    snapshot_dir: Path | None = None,
    allow_migrated_source: bool = False,
) -> MigrationPlan:
    """Plan the run: reads only (read-only opens), writes nothing.

    Dry-run planning never touches the source for write: stats go through
    the read-only URI and the quiesce write-lock probe runs on ``--apply``
    only (review P3d) — a dry-run on a busy source still produces its plan.

    The system target (``target="system"``, ADR-0047) ignores ``--to``:
    the target resolves through the install-state + the FHS manifest, the
    C1 preconditions run HERE (fail-fast, before any write — the dry-run
    refuses on the same blockers the apply would hit), and the snapshot
    defaults NEXT TO THE SOURCE (C9).

    ``allow_migrated_source`` is an internal seam for the round-trip/soak
    machinery (relocating an already-migrated store BACK to a fresh user
    home); the CLI never passes it — the public user-target contract still
    refuses an already-migrated source.
    """
    if target not in (USER_TARGET, SYSTEM_TARGET):
        raise UsageError(f"unknown target {target!r} (use '{USER_TARGET}' or '{SYSTEM_TARGET}')")
    source_home = source_home.expanduser().resolve()
    if not source_home.is_dir():
        raise UsageError(f"--from does not exist or is not a directory: {source_home}")

    data_root: Path | None = None
    system_config: Path | None = None
    if target == SYSTEM_TARGET:
        if target_home is not None:
            raise UsageError(
                "--to is not used with the system target — the target is "
                "resolved from the system install-state + the FHS manifest (C1)"
            )
        data_root = _system_preconditions(None)
        target_home = data_root
        system_config = fhs.system_config_path(None)
        if not _roots_disjoint(source_home, data_root):
            raise ProfileIsolationError(
                f"the source {source_home} overlaps the system data root "
                f"{data_root} — the profiles must keep distinct data roots "
                "(DR-14 / C1)"
            )
        if snapshot_dir is not None:
            snapshot_dir = snapshot_dir.expanduser().resolve()
            _validate_snapshot_dir(snapshot_dir, source_home, data_root, system_config)
    else:
        if target_home is None:
            raise UsageError("--to is required (explicit target path)")
        target_home = target_home.expanduser().resolve()
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
    # C14 advisory: the source WAL size is sampled BEFORE any database open
    # (an owner-equivalent RW open legally checkpoints-and-removes a WAL on
    # close; the advisory must report the pre-run size).
    wal_path = layout.db_path.parent / f"{layout.db_path.name}-wal"
    wal_bytes = wal_path.stat().st_size if wal_path.is_file() else 0
    # Dry-run planning opens the source READ-ONLY unconditionally (review
    # P3d: no WAL sidecars, no recovery, nothing absorbed). The system
    # target keeps the source read-only on --apply too (C6: the source is
    # never written — an owner-equivalent RW open would legally checkpoint
    # and remove a WAL, which is still a source mutation). Only the classic
    # apply may open read-write (the quiesce gate already ran and an
    # owner-equivalent recovery is its designed semantics).
    stats_read_only = bool(not apply or target == SYSTEM_TARGET)
    try:
        stats = read_store_stats(layout.db_path, read_only=stats_read_only)
    except (OSError, sqlite3.Error) as exc:
        if target == SYSTEM_TARGET:
            raise _classify_source_ro_failure(layout.db_path, exc) from exc
        # A source that vanishes or fails between layout resolution and the
        # stats read must be a typed usage error (residual P3), not a raw
        # traceback.
        raise UsageError(
            f"source database unreadable while planning: {layout.db_path}: {exc}"
        ) from exc
    if target == USER_TARGET and is_already_migrated(layout) and not allow_migrated_source:
        # The system target EXEMPTS this refusal: relocation of an already
        # migrated (6.0) store is its EXPECTED payload — zero rewrite
        # counters take the fast path (C14).
        raise AlreadyMigratedError(
            f"source {source_home} already carries the vesma:* namespace "
            f"({stats.total} records, 0 tags to rewrite) — nothing to migrate"
        )
    if stats.unparseable_rows:
        raise InvalidSourceError(
            f"source has {stats.unparseable_rows} row(s) with unparseable tags "
            "JSON — repair the source before migrating (nothing was touched)"
        )

    quiesce_warnings: tuple[str, ...] = ()
    if apply:
        if target == SYSTEM_TARGET:
            fatal, probe_warnings = _source_problems_system(
                layout, read_only_probes=True, with_wal_sampling=False
            )
        else:
            fatal, probe_warnings = quiesce_problems(
                [layout.db_path, *_sibling_sqlite_dbs(layout)], with_wal_sampling=False
            )
        if fatal:
            raise QuiesceViolationError(
                "source store is not quiescent (stop the vesma service first): " + "; ".join(fatal)
            )
        quiesce_warnings = tuple(probe_warnings)

    try:
        planned = _planned_files_for(layout, target_home, target)
    except OSError as exc:
        # A bare OSError here used to surface as a traceback (exit 1); a
        # vanished/unreadable source file is an operator-facing usage
        # problem (review P3h).
        raise UsageError(f"source file unreadable while planning: {exc}") from exc

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    if target == SYSTEM_TARGET:
        if data_root is None or system_config is None:  # pragma: no cover - invariant
            raise VerificationError("system target resolved without its paths")
        # C7 plan-phase gate (dry-run included — the ratified double run).
        gate = system_quiesce_problems(data_root, with_wal_sampling=False)
        quiesce_warnings = quiesce_warnings + tuple(gate.warnings)
        if gate.fatal:
            raise QuiesceViolationError(
                "the system target is not quiescent (C7): " + "; ".join(gate.fatal)
            )
        resolved_snapshot = (
            snapshot_dir
            if snapshot_dir is not None
            else source_home.parent / f"{SNAPSHOT_DIR_PREFIX}{timestamp}"
        )
        _check_free_space(sum(f.size_bytes for f in planned), resolved_snapshot, data_root)
    else:
        resolved_snapshot = (
            snapshot_dir.expanduser().resolve()
            if snapshot_dir is not None
            else target_home.parent / f"{SNAPSHOT_DIR_PREFIX}{timestamp}"
        )
    staging_dir = target_home.parent / f"{target_home.name}.staging-{timestamp}"
    fast_path = (
        stats.subtype_swaps == 0 and stats.project_swaps == 0 and stats.duplicates_removed == 0
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
        quiesce_warnings=quiesce_warnings,
        planned_files=tuple(planned),
        snapshot_dir=resolved_snapshot,
        staging_dir=staging_dir,
        target=target,
        data_root=data_root,
        system_config_path=system_config,
        source_owner_uid=layout.db_path.stat().st_uid if target == SYSTEM_TARGET else None,
        wal_bytes=wal_bytes,
        fast_path=fast_path,
        remaining_cargo=(
            tuple(_remaining_cargo_advisory(source_home, None)) if target == SYSTEM_TARGET else ()
        ),
        precondition_lines=(
            tuple(_totp_precondition_lines(layout, None)) if target == SYSTEM_TARGET else ()
        ),
        secret_path_warnings=(
            tuple(_secret_path_warnings(layout)) if target == SYSTEM_TARGET else ()
        ),
    )


def _planned_files_for(layout: _StoreLayout, target_home: Path, target: str) -> list[PlannedFile]:
    """The carry map (plan listing == publish map, cascade P2-2).

    User target: the 6.0 nested shape (``<to>/data/vesma.db``). System
    target: the FLAT shape — the store home IS the data root, so the db
    and its siblings land directly in it (C5)."""
    data_base = target_home / "data" if target == USER_TARGET else target_home
    planned: list[PlannedFile] = [
        PlannedFile(
            source=layout.db_path,
            target=data_base / CANONICAL_DB_NAME,
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
                    target=data_base / path.name,
                    size_bytes=path.stat().st_size,
                    via_backup_api=_is_sqlite(path),
                )
            )
        else:
            total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            planned.append(
                PlannedFile(
                    source=path,
                    target=data_base / path.name,
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
    return planned


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
    # ── system-target extensions (ADR-0047) ──────────────────────────────
    target: str = USER_TARGET
    fast_path: bool = False
    wal_bytes: int = 0
    config_disposition: str | None = None  # "written" | "migrated-<ts>"
    config_final_path: Path | None = None
    ownership_applied: bool = False
    remaining_cargo: tuple[str, ...] = ()
    rollback_notes: tuple[str, ...] = ()


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


def _moved_sqlite_dbs(staging_data: Path) -> list[Path]:
    """Every sqlite main file the run produced, INCLUDING nested trees.

    Review P3g: ``quick_check`` must cover databases inside copied
    directories (``_copy_tree``), not only the data-dir top level; the
    ``_is_sqlite`` header check keeps ``-wal``/``-shm`` sidecars out.
    """
    return [path for path in sorted(staging_data.rglob("*")) if path.is_file() and _is_sqlite(path)]


def run_migration(
    plan: MigrationPlan, progress: Callable[[str], None] | None = None
) -> MigrationReport:
    """Execute a plan built with ``apply=True`` (the only writable mode).

    ``progress`` receives phased banner + streaming counter lines (C14) —
    plain text, paths and numbers only (no spinner: sudo+ssh+grep safe).
    """
    if plan.mode != "apply":
        raise UsageError("run_migration requires a plan built with apply=True")
    if plan.target == SYSTEM_TARGET:
        # C13 belt-and-braces: the CLI gates already refuse; the machinery
        # itself re-checks so nothing can bypass the gate by construction.
        if not activation_gate_open():
            raise ActivationGateError(
                "--apply for the system target is gated (C13 / ADR-0045 "
                "Decision 9): it refuses until the round-trip soak has "
                "closed green AND the owner has given the word. The dry-run "
                "is fully functional and is the soak's own tool"
            )
        try:
            return _run_system_migration(plan, progress=progress)
        finally:
            # The SINGLE cleanup path: on ANY failure — typed, unexpected,
            # or a BaseException like KeyboardInterrupt — staging is
            # removed; published entries were rolled back by the publish
            # guard; the snapshot stays (the rollback artifact).
            _cleanup_staging(plan.staging_dir)
    return _run_user_migration(plan)


def _run_user_migration(plan: MigrationPlan) -> MigrationReport:
    """The B3 5.x → 6.0 namespace move (explicit ``--to`` path)."""
    warnings = list(plan.quiesce_warnings)
    layout = _resolve_layout(plan.source_home)

    # Re-run the quiesce gate WITH WAL sampling right before the copy.
    fatal, extra_warnings = quiesce_problems(
        [layout.db_path, *_sibling_sqlite_dbs(layout)], with_wal_sampling=True
    )
    warnings.extend(extra_warnings)
    if fatal:
        raise QuiesceViolationError(
            "source store is not quiescent (stop the vesma service first): " + "; ".join(fatal)
        )

    pre_stats = read_store_stats(layout.db_path)
    snapshot = make_snapshot(layout, plan.snapshot_dir)
    warnings.extend(snapshot.warnings)  # skipped non-regular entries (P3-fifo)
    if snapshot.stats.ids_digest != pre_stats.ids_digest:
        raise SnapshotError("source changed between plan and snapshot — retry")

    config_plan: ConfigPlan | None = None
    try:
        applied = _materialize_target(snapshot, plan.staging_dir)
        target_db = plan.staging_dir / "data" / CANONICAL_DB_NAME
        moved_dbs = _moved_sqlite_dbs(plan.staging_dir / "data")
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
        fast_path=bool(applied.get("fast_path", False)),
    )


def _run_system_migration(
    plan: MigrationPlan, progress: Callable[[str], None] | None
) -> MigrationReport:
    """The system-target relocation (ADR-0047 C1-C14 apply path).

    Reachable only behind the C13 activation gate. Phases: source gates →
    snapshot (next to the source, C9) → staging build (fast path aware,
    C14) → config leg (C3/C10) → verification (B3 set + C8 parity, strict
    pre-publish) → ownership pass (C4, manifest roles) → publish (C5,
    piecewise with rollback) → post-publish stat audit (C4/C8)."""
    if plan.data_root is None or plan.system_config_path is None:  # pragma: no cover
        raise VerificationError("run_migration: a system plan must carry the resolved target paths")

    def emit(message: str) -> None:
        if progress is not None:
            progress(message)

    warnings = list(plan.quiesce_warnings)
    layout = _resolve_layout(plan.source_home)
    # C6, unconditional for the system run: every source open is read-only
    # (even owner-equivalent — a RW open would checkpoint-and-remove a WAL,
    # and the source must stay byte-identical). The foreign-source check
    # drives the honest warning and the snapshot ownership handover only.
    # Every source open on a system run is read-only (see C6 note above).
    foreign_source = _source_probe_read_only(layout)
    if foreign_source:
        warnings.append(
            "the source is owned by another account — every source probe "
            "runs read-only (C6): the write-lock probe is skipped; liveness "
            "is gated by WAL sampling and the snapshot digest comparison"
        )
    data_root = plan.data_root
    config_target = plan.system_config_path

    # Phase 1 — source gates re-run (symmetric to the B3 two-phase source
    # quiesce; the target-side C7 gate re-runs right before publish).
    emit("[system migrate] 1/7 source quiesce + snapshot")
    source_fatal, _ = _source_problems_system(layout, read_only_probes=True, with_wal_sampling=True)
    if source_fatal:
        raise QuiesceViolationError(
            "source store is not quiescent (stop the vesma service first): "
            + "; ".join(source_fatal)
        )

    try:
        pre_stats = read_store_stats(layout.db_path, read_only=True)
    except (OSError, sqlite3.Error) as exc:
        # P3-1: same typed classifier as the plan phase — the source may
        # turn recovery-needing BETWEEN plan and apply; that is exit 14,
        # never a raw traceback.
        raise _classify_source_ro_failure(layout.db_path, exc) from exc
    snapshot = make_snapshot(
        layout,
        plan.snapshot_dir,
        source_read_only=True,
        chown_to=(
            (layout.db_path.stat().st_uid, layout.db_path.stat().st_gid) if foreign_source else None
        ),
    )
    warnings.extend(snapshot.warnings)
    if snapshot.stats.ids_digest != pre_stats.ids_digest:
        raise SnapshotError("source changed between plan and snapshot — retry")

    # Phase 2 — staging build from the verified snapshot.
    emit("[system migrate] 2/7 staging build")
    applied = _materialize_target(snapshot, plan.staging_dir, progress=progress)
    emit(
        f"[system migrate] staging: {applied['records']} records, "
        f"fast_path={bool(applied['fast_path'])}"
    )

    # Phase 3 — config leg (C3 disposition decided at publish, C10 pins).
    emit("[system migrate] 3/7 config leg")
    config_staged, _config_plan = _migrate_config_system(layout, data_root, plan.staging_dir)
    # F9 adopt evidence: the published config gets the provenance header
    # (stamped BEFORE hashing), and the marker gains the conffile record
    # (canonical path + sha as published) plus the recoverability
    # pointers — pre-publish, into staging, so rollback/parity semantics
    # are untouched.
    conffile_bytes = _stamp_conffile_provenance(config_staged)
    _record_conffile_evidence(
        plan.staging_dir / "data",
        config_target,
        hashlib.sha256(conffile_bytes).hexdigest(),
        source_home=plan.source_home,
        snapshot_dir=plan.snapshot_dir,
    )
    warnings.extend(plan.precondition_lines)
    warnings.extend(plan.secret_path_warnings)

    # Phase 4 — verification, strictly PRE-publish (C8): the B3 set plus
    # sha256 parity, sibling table counts, and the key invariants.
    emit("[system migrate] 4/7 verification (pre-publish)")
    target_db = plan.staging_dir / "data" / CANONICAL_DB_NAME
    moved_dbs = _moved_sqlite_dbs(plan.staging_dir / "data")
    verification = verify_target(target_db, snapshot, moved_dbs)
    parity_problems = _sha256_parity_problems(plan.snapshot_dir / "data", plan.staging_dir / "data")
    if snapshot.layout.vault_dir.is_dir():
        parity_problems += _sha256_parity_problems(
            plan.snapshot_dir / "vault", plan.staging_dir / "vault"
        )
    parity_problems += _sibling_table_parity_problems(
        plan.snapshot_dir / "data",
        plan.staging_dir / "data",
        main_db_names=(snapshot.layout.db_path.name, LEGACY_DB_NAME, CANONICAL_DB_NAME),
    )
    parity_problems += _key_invariant_problems(snapshot, plan.staging_dir / "data")
    if parity_problems:
        raise VerificationError("; ".join(parity_problems))
    verification["sha256_parity"] = "ok"
    verification["sibling_table_parity"] = "ok"

    # Phase 5 — publish map + ownership pass (C4): the pass runs on the
    # STAGED paths, per manifest role, BEFORE fsync + publish.
    emit("[system migrate] 5/7 ownership pass (FHS manifest roles)")
    ownership_applied = os.geteuid() == 0
    config_final = _config_publish_target(config_target)
    entries = _plan_publish_entries(plan.staging_dir, data_root, config_staged, config_final)
    expectations = _publish_ownership_expectations(entries, None)
    warnings.extend(_apply_ownership(expectations, apply=ownership_applied))

    # Phase 6 — publish (C5): fresh target-side C7 gate, then piecewise
    # moves with rollback on ANY interruption.
    emit("[system migrate] 6/7 publish (piecewise)")
    gate = system_quiesce_problems(data_root, with_wal_sampling=True)
    warnings.extend(gate.warnings)
    if gate.fatal:
        raise QuiesceViolationError(
            "the system target is not quiescent before publish (C7): " + "; ".join(gate.fatal)
        )
    _assert_no_sidecars(plan.staging_dir)
    present = _store_content_present(data_root)
    if present:
        raise TargetStoreContentError(
            "the system data root acquired store content mid-run: "
            + ", ".join(present)
            + " — refusing to publish over it (C1/C5)"
        )
    published: list[_PublishEntry] = []
    try:
        _fsync_tree(plan.staging_dir)
        for index, entry in enumerate(entries, start=1):
            if entry.cross_fs:
                # P3-2 (C3 TOCTOU): the empty-conffile disposition was
                # decided in phase 5, but the conffile may change under the
                # run. Re-evaluate it on the FRESH stat immediately before
                # the overwrite_ok write — through the SAME typed decision
                # path as phase 5 (_config_publish_target raises the
                # phase-5 ConfigMigrationError when the conffile cannot be
                # read). Only a CLASS change redirects the write (and the
                # audit row): a still-non-empty conffile keeps its phase-5
                # sidecar stamp, never a silent fallback.
                fresh_final = _config_publish_target(config_target)
                if (fresh_final == config_target) != entry.overwrite_ok:
                    entry = replace(
                        entry, final=fresh_final, overwrite_ok=fresh_final == config_target
                    )
                    config_final = fresh_final
                    expectations = [
                        replace(exp, path=fresh_final) if exp.staged == entry.staged else exp
                        for exp in expectations
                    ]
            if not entry.overwrite_ok and (entry.final.exists() or entry.final.is_symlink()):
                raise TargetStoreContentError(
                    f"publish target appeared mid-run: {entry.final} — refusing (a racing actor?)"
                )
            _publish_entry(entry)
            published.append(entry)
            emit(f"[system migrate] publish {index}/{len(entries)}: {entry.final.name}")
    except BaseException as exc:
        notes = _rollback_published(published)
        # Typed refusals keep their exit codes; a KeyboardInterrupt keeps
        # its type (masking Ctrl-C as a "verification failure" would be
        # hostile) — both AFTER the rollback above.
        if isinstance(exc, (StoreMigrationError, KeyboardInterrupt)):
            raise
        raise VerificationError(
            f"publish interrupted ({type(exc).__name__}); published entries "
            "rolled back: " + "; ".join(notes)
        ) from exc

    # Phase 7 — post-publish stat audit (C4/C8): part of verification.
    emit("[system migrate] 7/7 post-publish audit")
    audit_problems = _stat_audit_problems(expectations, ownership_applied=ownership_applied)
    if audit_problems:
        # P3-3: after publish there is NO retry — the store IS published;
        # a re-run is refused with exit 11 (the data root already carries
        # the migrated store). One message, no new exit code.
        raise VerificationError(
            "post-publish stat audit failed (C4) — the store IS PUBLISHED "
            "and stays published; a re-run will be refused with exit 11 "
            "(the migrated store is already in the data root): " + "; ".join(audit_problems)
        )
    verification["stat_audit"] = "ok"
    verification["ownership_applied"] = ownership_applied

    disposition = "migrated-<ts>" if config_final.name != config_target.name else "written"
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
        config=_config_plan,
        verification=verification,
        warnings=tuple(warnings),
        target=SYSTEM_TARGET,
        fast_path=bool(applied["fast_path"]),
        wal_bytes=plan.wal_bytes,
        config_disposition=disposition,
        config_final_path=config_final,
        ownership_applied=ownership_applied,
        remaining_cargo=plan.remaining_cargo,
    )


def _config_publish_target(config_target: Path) -> Path:
    """C3 disposition (decided BEFORE the pass so the ownership rows can
    name the final path): an existing NON-EMPTY conffile is never touched
    — the incoming config lands beside it as ``config.yaml.migrated-<ts>``;
    an empty or absent one gets the canonical name."""
    if not config_target.is_file():
        return config_target
    try:
        content = config_target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigMigrationError(
            f"the existing system conffile {config_target} cannot be read "
            f"({exc}) — the mover never overwrites a conffile it cannot "
            "inspect (C3)"
        ) from exc
    if content.strip():
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        return config_target.with_name(f"{config_target.name}{_MIGRATED_CONFIG_INFIX}{stamp}")
    return config_target


def _stamp_conffile_provenance(config_staged: Path) -> bytes:
    """Prepend the mover provenance header to the staged system conffile
    (F9) and return the final bytes. A YAML comment — ``yaml.safe_load``
    sees the same document with or without it; the stamp happens BEFORE
    hashing, so the marker's sha256 covers exactly the bytes that land on
    disk (the cross-fs publish copies them verbatim)."""
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    body = config_staged.read_bytes()
    final = f"{MIGRATE_CONFFILE_HEADER} {stamp}\n".encode() + body
    config_staged.write_bytes(final)
    _harden_file(config_staged)
    return final


def _record_conffile_evidence(
    staged_data: Path,
    config_target: Path,
    conffile_sha256: str,
    *,
    source_home: Path,
    snapshot_dir: Path,
) -> None:
    """Give the preservation marker its conffile record (F9): the
    CANONICAL shared_config path + the sha of the bytes AS PUBLISHED,
    plus the recoverability pointers (source home + snapshot) the
    uninstaller reports on adopt. Written INTO STAGING pre-publish — the
    C5 rollback semantics and the C8 parity (which excludes the marker)
    stay intact; the marker rides the ordinary publish into the data
    root. The install-state is NEVER touched here (C12 verbatim)."""
    marker = _marker_file(staged_data)
    try:
        doc = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(
            f"the migration marker at {marker} cannot be read back for the "
            f"conffile record ({exc}) — refusing to publish without the "
            "uninstall adopt evidence (F9)"
        ) from exc
    doc["conffile"] = {"path": str(config_target), "sha256": conffile_sha256}
    doc["source_home"] = str(source_home)
    doc["snapshot_dir"] = str(snapshot_dir)
    marker.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
    _harden_file(marker)
