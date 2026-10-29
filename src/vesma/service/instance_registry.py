"""Instance registry (ADR-0049 wave 0 skeleton).

The machine-readable reference of vesma instances on this machine. ONE
instance = (store home, endpoint) — the registry records one row per
installed profile so the doctor and the CLI can answer "what instances
exist here" without parsing units (the advisory contract: the registry
is REFERENCE data — the units and configs stay the source of truth; a
drifted registry degrades diagnoses, never behavior).

Format: JSON at ``$XDG_STATE_HOME/vesma/instances.json`` — the decision
the wave-0 contract delegated to the executor (§6.2): stdlib-only, strict
types, no YAML boolean-coercion class, stable ``sort_keys`` serialization.
The location is generation-independent and outside every store home tree
(same reasoning as the writer-lock files: a v2 home root IS a store home;
a control-plane reference must not ride the mover's cargo or the C6
digest).

Advisory status, per contract: ``install``/``update`` WRITE,
``doctor``/CLI READ. A write failure is a reported problem line — the
install itself never fails because the registry could not be updated; a
read failure yields an empty registry plus the problem for the reader to
surface. Since wave 1 (ADR-0049 Lock 3) the writers mint the instance id
and record it (the wave-0 ``None`` placeholder; an unmintable identity
degrades advisorially to ``None`` + the problem line).
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

REGISTRY_VERSION = 1
REGISTRY_FILE_NAME = "instances.json"


@dataclasses.dataclass(frozen=True)
class InstanceEntry:
    """One installed instance row (JSON-shaped, wave-0 field set)."""

    profile: str  # "user" | "system"
    home: str  # absolute store-home path
    endpoint: str | None  # the API base URL, when known
    unit: str | None  # the systemd unit file name, when known
    instance_id: str | None  # wave 1 mints it; None at wave 0
    updated_at: str  # ISO-8601


def to_json_mapping(entry: InstanceEntry) -> dict[str, object]:
    """The JSON object shape of one entry (pinned by tests)."""
    return dataclasses.asdict(entry)


def from_json_mapping(raw: object) -> InstanceEntry | None:
    """Parse one row; ``None`` on any shape drift (a corrupt row must not
    poison the whole registry read)."""
    if not isinstance(raw, dict):
        return None
    profile = raw.get("profile")
    home = raw.get("home")
    if not isinstance(profile, str) or not profile or not isinstance(home, str) or not home:
        return None
    endpoint = raw.get("endpoint")
    unit = raw.get("unit")
    instance_id = raw.get("instance_id")
    updated_at = raw.get("updated_at")
    return InstanceEntry(
        profile=profile,
        home=home,
        endpoint=endpoint if isinstance(endpoint, str) else None,
        unit=unit if isinstance(unit, str) else None,
        instance_id=instance_id if isinstance(instance_id, str) else None,
        updated_at=updated_at if isinstance(updated_at, str) else "",
    )


@dataclasses.dataclass(frozen=True)
class RegistryState:
    """What a reader gets: the rows plus an advisory problem line (a
    corrupt/unreadable registry is never silent — and never fatal)."""

    entries: tuple[InstanceEntry, ...]
    problem: str | None = None


def registry_path() -> Path:
    """``$XDG_STATE_HOME/vesma/instances.json`` (unset AND empty variable
    both mean the XDG default)."""
    xdg = os.environ.get("XDG_STATE_HOME", "")
    root = Path(xdg) if xdg.strip() else Path.home() / ".local" / "state"
    return root / "vesma" / REGISTRY_FILE_NAME


def load_registry() -> RegistryState:
    """Read the registry; missing file = empty state, corrupt file = empty
    state + the problem line (advisory contract — readers surface it)."""
    path = registry_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return RegistryState(entries=())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return RegistryState(
            entries=(),
            problem=f"instance registry unreadable ({path}): {type(exc).__name__}",
        )
    if not isinstance(raw, dict) or not isinstance(raw.get("instances"), list):
        return RegistryState(
            entries=(),
            problem=f"instance registry has an unexpected shape ({path})",
        )
    entries: list[InstanceEntry] = []
    for row in raw["instances"]:
        entry = from_json_mapping(row)
        if entry is None:
            return RegistryState(
                entries=(),
                problem=f"instance registry carries an unparsable row ({path})",
            )
        entries.append(entry)
    return RegistryState(entries=tuple(entries))


def record_instance(entry: InstanceEntry) -> str | None:
    """Upsert one row (keyed by profile+home); ``None`` on success, else
    the advisory problem line. Atomic: tmp + replace, 0600."""
    state = load_registry()
    if state.problem is not None and state.entries:
        return state.problem  # never overwrite a registry we only half-read
    rows = [to_json_mapping(item) for item in state.entries if _key(item) != _key(entry)]
    rows.append(to_json_mapping(entry))
    return _write_rows(rows)


def forget_instance(*, profile: str, home: str) -> str | None:
    """Drop the row for (profile, home); ``None`` when done (including
    "was already absent")."""
    state = load_registry()
    if state.problem is not None and state.entries:
        return state.problem
    rows = [to_json_mapping(item) for item in state.entries if _key(item) != (profile, home)]
    return _write_rows(rows)


def _key(entry: InstanceEntry) -> tuple[str, str]:
    return (entry.profile, entry.home)


def _write_rows(rows: list[dict[str, object]]) -> str | None:
    path = registry_path()
    payload = json.dumps(
        {
            "version": REGISTRY_VERSION,
            "instances": sorted(rows, key=lambda row: (row["profile"], row["home"])),
        },
        indent=2,
        sort_keys=True,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        # mkstemp lands 0600 by itself — the registry never widens.
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".instances-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
    except OSError as exc:
        return f"instance registry update failed ({path}): {exc}"
    return None


def record_user_profile_instance(unit_name: str) -> str | None:
    """Writer for the USER profile installs: resolves the current
    settings leniently (defaults, no config-file demand), mints/loads the
    instance id (ADR-0049 Lock 3, wave 1 — the wave-0 ``None`` placeholder
    is filled from here on) and upserts the row. Advisory — callers print
    the problem, never raise."""
    from vesma.config import Settings
    from vesma.service.instance import (
        InstanceError,
        ensure_instance_id,
        resolve_requested_instance_id,
    )

    settings = Settings()
    settings.resolve_paths()
    mint_problem: str | None = None
    try:
        instance_id = ensure_instance_id(
            settings.vesma.data_dir, env_value=resolve_requested_instance_id()
        )
    except (InstanceError, OSError) as exc:
        # Advisory: an unmintable identity still installs — the row
        # carries instance_id=None and the problem line explains it.
        instance_id, mint_problem = None, f"user instance-id mint failed: {exc}"
    problem = record_instance(
        InstanceEntry(
            profile="user",
            home=str(settings.vesma.data_dir),
            endpoint=f"http://{settings.api.host}:{settings.api.port}",
            unit=unit_name,
            instance_id=instance_id,
            updated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
    )
    if mint_problem is not None:
        problem = f"{problem}; {mint_problem}" if problem else mint_problem
    return problem


def _mint_system_instance_id(system_home: Path) -> tuple[str | None, str | None]:
    """The SYSTEM home's instance id + an advisory note (or ``None``).

    The id file is minted by the ROOT installer but must stay readable —
    and re-writable at a later boot — by the SERVICE account: ``0644`` +
    account ownership when the account resolves (the id is an identifier,
    never a secret). ``InstanceHomeMismatchError`` cannot happen here (no
    env claim at a system install); any other failure degrades
    advisorially to ``(None, problem)``.
    """
    import grp
    import pwd

    from vesma.service.instance import InstanceError, ensure_instance_id, id_file_path
    from vesma.service.unitgen import SYSTEM_GROUP, SYSTEM_USER

    try:
        instance_id = ensure_instance_id(system_home)
    except (InstanceError, OSError) as exc:
        return None, f"system instance-id mint failed ({system_home}): {exc}"
    path = id_file_path(system_home)
    note: str | None = None
    try:
        os.chmod(path, 0o644)
        uid = pwd.getpwnam(SYSTEM_USER).pw_uid
        gid = grp.getgrnam(SYSTEM_GROUP).gr_gid
        os.chown(path, uid, gid)
    except (OSError, KeyError) as exc:
        note = (
            f"system instance-id file left installer-owned ({path}): {exc} — "
            f"the {SYSTEM_USER} account may not be able to rewrite it at boot"
        )
    return instance_id, note


def record_system_profile_instance(unit_name: str) -> str | None:
    """Writer for the SYSTEM profile installs (the FHS data root is the
    home); wave 1 fills ``instance_id`` (ADR-0049 Lock 3). Advisory —
    callers print the problem, never raise."""
    from vesma.service import fhs

    system_home = fhs.system_data_root()
    instance_id, mint_note = _mint_system_instance_id(system_home)
    problem = record_instance(
        InstanceEntry(
            profile="system",
            home=str(system_home),
            endpoint=None,
            unit=unit_name,
            instance_id=instance_id,
            updated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
    )
    if mint_note is not None:
        problem = f"{problem}; {mint_note}" if problem else mint_note
    return problem


def forget_profile_instance(*, profile: str) -> str | None:
    """Wave-0 writer for whole-profile uninstalls: drop the profile's row.
    Removal is BY PROFILE on purpose — the uninstall must not depend on
    the CURRENT settings (the row records the home as it was installed;
    a since-drifted config must not orphan it)."""
    state = load_registry()
    rows = [item for item in state.entries if item.profile != profile]
    if len(rows) == len(state.entries):
        return None  # already absent
    return _write_rows([to_json_mapping(item) for item in rows])


__all__ = [
    "REGISTRY_VERSION",
    "InstanceEntry",
    "RegistryState",
    "forget_instance",
    "forget_profile_instance",
    "from_json_mapping",
    "load_registry",
    "record_instance",
    "record_system_profile_instance",
    "record_user_profile_instance",
    "registry_path",
    "to_json_mapping",
]
