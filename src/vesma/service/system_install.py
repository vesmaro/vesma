"""``vesma system install`` — the canonical system-profile installer
(specs/layout v2 §3.13; ADR-0045 Decisions 4-6, rollout wave 2). The former
``vesma service install --system`` spelling is a hidden deprecated alias
(CLI topology v2; removal not before 9.0).

Owns conformance SY-01…SY-04, SY-08, SY-09:

- **SY-01** — the install stamps the system profile
  (``/var/lib/vesma/install-state.yaml``, ``profile: system``); the user
  profile (`~/.vesma/install-state.yaml`) is never touched — both
  profiles coexist on one machine with disjoint data roots (doctor
  DR-14 refuses a collision);
- **SY-02** — every path comes from the ONE FHS manifest
  (:mod:`vesma.service.fhs`); this module contains no path literals;
- **SY-03** — rights/owners are applied explicitly per the manifest
  (never umask-inherited); ``/etc/vesma`` ``0750 root:vesma`` with
  ``0640`` files, ``env.d`` ``0600`` class enforced at creation for
  files the flow writes;
- **SY-04** — the service account comes from the sysusers.d fragment
  (static content — a re-install writes byte-identical bytes and never
  duplicates the account; systemd-sysusers is idempotent by design);
- **SY-05** (completed by rollout 3) — the conffile discipline: a
  bundled manifest that diverges from the operator's on-disk edit NEVER
  overwrites it; the new default lands next to it as
  ``<name>.yaml.rpmnew`` (the ``.rpmnew`` convention: diff, merge,
  remove), pending defaults are never clobbered, ``config.yaml`` is
  never seeded or touched;
- **SY-08** — venvs are materialized by the installer: ``uv venv`` +
  ``uv pip install`` of the SAME-BUILD wheel pin (``vesma==<running
  version>``). ``pip`` as root is never invoked — the venv has no pip
  at all (``uv venv`` default); foreign venvs are never written (every
  path is manifest-derived);
- **SY-09** — every artifact is recorded in the install-state AT/BEFORE
  creation (the full plan is written before the first mutation);
  ``vesma system uninstall`` is the EXACT INVERSE: recorded artifacts only,
  reverse creation order, foreign content = loud refusal (never a
  silent skip), state ↔ filesystem drift = loud refusal with a doctor
  pointer. Operator data (the store home plane) is preserved and
  reported, mirroring the user-profile uninstall discipline.

The dest-dir technique: the whole flow runs against a deployment
``prefix`` (default ``/`` — canonical FHS). Root-less conformance tests
monkeypatch :func:`vesma.service.fhs.default_prefix` to a temp dir; the
real-prefix run refuses to start without euid 0 (sudo), and ownership
application is skipped (recorded, not applied) in a simulation.

Rollout 3 (ADR-0045) added HERE: the SY-05 conffile discipline (this
module), the shared drift predicate :func:`system_state_drift` (doctor
DR-17's system leg AND the updater's preflight — one predicate, so
diagnosis and refusal can never disagree) and the canon unit text
:func:`regenerate_system_unit` (install + doctor DR-18 read ONE
source). The symlink-swap UPDATER itself lives in
:mod:`vesma.service.system_update`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import errno
import grp
import hashlib
import json
import logging
import os
import pwd
import re
import shutil
import subprocess  # nosec B404 - fixed-argv installer subprocesses
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import yaml

from vesma.config import load_settings
from vesma.service import fhs, unitgen
from vesma.service import install as user_install
from vesma.service.manifest import CONFFILE_NEW_SUFFIX, bundled_manifest_path, load_installation

logger = logging.getLogger("vesma.service.system_install")

#: Install-state schema version (additive growth only).
STATE_SCHEMA: Final[int] = 1

#: Roles whose state rows name one materialized venv revision (the
#: updater's retirement set and the doctor's revision walkers).
REVISION_ROLES: Final[frozenset[str]] = frozenset(
    {"venv_engine_revision", "venv_component_revision"}
)

#: The sysusers.d fragment content (§3.13: the service account comes from
#: HERE — static text, so a re-install can never duplicate the account;
#: systemd-sysusers creates users with a nologin shell by design).
_SYSUSERS_TEMPLATE = (
    "# vesma service account (specs/layout/v2 §3.4/§3.13; written by"
    " `vesma system install`).\n"
    "# systemd-sysusers(8): system user, nologin shell, home = the data root.\n"
    "#Type Name   ID GECOS              Home directory\n"
    "g     vesma  -  -\n"
    'u     vesma  -  "VESMA supervisor" {home}\n'
)


class SystemInstallError(Exception):
    """System install/uninstall refused — the message is operator-facing."""


#: The stable exit code of the read-only-target refusal (ADR-0048
#: Decision 5; the ADR-0047 C15 taxonomy discipline — additive window:
#: the mover's refusals occupy 1-16, this one takes 17). Declared on the
#: class; surfacing it through ``typer.Exit`` in ``cli/system_cmd.py``
#: is reserved for the CLI wave (additive, non-breaking).
READ_ONLY_TARGET_EXIT_CODE: Final[int] = 17


class SystemInstallReadOnlyTargetError(SystemInstallError):
    """A code-plane install leg hit ``EROFS`` (Read-only file system) —
    the typed refusal with the atomic-distro remediation (ADR-0048
    Decision 5, card ``151a55ff``).

    Raised ONLY on the EROFS fact (``errno.EROFS`` from the failing
    write); the atomic/immutable-distro detection (os-release hints,
    ``/run/ostree-booted``) informs ONLY the remediation wording — never
    the refusal decision itself.
    """

    exit_code: int = READ_ONLY_TARGET_EXIT_CODE


#: os-release ``VARIANT_ID`` tokens that hint an atomic/immutable distro
#: (remediation WORDING only — the refusal stays EROFS-fact-based; the
#: universal ostree signal is the ``/run/ostree-booted`` marker, which
#: covers the rest of the ostree family beyond these names).
_ATOMIC_VARIANT_TOKENS: Final[tuple[str, ...]] = ("silverblue", "coreos", "bluefin")


def _read_os_release(path: Path) -> dict[str, str]:
    """Best-effort ``KEY=VALUE`` parse of one os-release file."""
    values: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in raw.splitlines():
        key, sep, value = line.partition("=")
        if not sep or not key.strip().isupper():
            continue
        values[key.strip()] = value.strip().strip('"')
    return values


def _atomic_system_hints(root: Path | None = None) -> list[str]:
    """«This machine looks atomic/immutable» hints — used ONLY to word
    the EROFS remediation (ADR-0048 Decision 5): the refusal is always
    by the EROFS fact, never by a distro guess. Signals: the
    ``/run/ostree-booted`` marker (the universal ostree signal) and an
    os-release ``VARIANT_ID`` naming a known atomic variant
    (``root`` is injectable for tests).
    """
    base = Path(os.sep) if root is None else root
    hints: list[str] = []
    marker = base / "run" / "ostree-booted"
    try:
        ostree = marker.exists() or "IS_OSTREE=1" in marker.read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        ostree = False
    if ostree:
        hints.append("detected: /run/ostree-booted (ostree-based system)")
    for rel in ("etc/os-release", "usr/lib/os-release"):
        variant = _read_os_release(base / rel).get("VARIANT_ID", "").lower()
        if any(token in variant for token in _ATOMIC_VARIANT_TOKENS):
            hints.append(f"detected: os-release VARIANT_ID={variant}")
            break
    return hints


def _readonly_remediation(target: Path, hints: list[str]) -> str:
    """The operator-facing EROFS refusal text (ADR-0048): what failed,
    the atomic/immutable guidance (rpm-ostree usroverlay for an
    experiment, the rpm package for a permanent install) and the lane
    split that keeps /usr writable for packages only."""
    atomic = (
        f"atomic/immutable system {'; '.join(hints)}"
        if hints
        else "atomic/immutable system (Silverblue/CoreOS): if this is one"
    )
    return (
        f"cannot write {target}: read-only filesystem (EROFS) — the"
        " installer cannot lay the code plane there (ADR-0048:"
        " /usr/local; on ostree distros it resolves into persistent"
        " var/usrlocal, so this failure means an admin-readonly mount)."
        f" {atomic} — for a one-off experiment:"
        " `sudo rpm-ostree usroverlay` (lives until reboot); for a"
        " permanent install use the rpm package (the rpm lane ships its"
        " own manifest with /usr/lib/vesma). Nothing else can be done by"
        " this installer."
    )


@contextlib.contextmanager
def _rofs_guard(target: Path) -> Iterator[None]:
    """Convert an ``EROFS`` ``OSError`` raised by a code-plane leg into
    the typed ADR-0048 refusal; ANY other ``OSError`` keeps its original
    classification (EACCES, ENOSPC, … are not the read-only case)."""
    try:
        yield
    except OSError as exc:
        if exc.errno == errno.EROFS:
            raise SystemInstallReadOnlyTargetError(
                _readonly_remediation(target, _atomic_system_hints())
            ) from exc
        raise


@dataclasses.dataclass(frozen=True)
class SystemInstallResult:
    """Outcome of one system install run (CLI report + tests)."""

    lines: tuple[str, ...]
    state_path: Path
    unit_path: Path
    engine_venv: Path
    revisions: tuple[Path, ...]


@dataclasses.dataclass(frozen=True)
class SystemUninstallResult:
    """Outcome of one system uninstall run."""

    lines: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class SystemConfigSetResult:
    """Outcome of one ``system config set --file`` run (CLI report + tests).

    ``backup`` is ``None`` when no previous conffile existed (the first
    config lands with nothing to preserve). ``first_conffile`` lets the
    CLI phrase the report honestly instead of guessing from ``backup``.
    """

    target: Path
    backup: Path | None
    mode: int
    owner: str | None
    group: str | None
    first_conffile: bool


@dataclasses.dataclass
class StateArtifact:
    """One install-state row (§3.13: every created artifact, at/before
    creation). ``remove_mode`` is decided by the INSTALLER and trusted by
    the uninstaller — the uninstall never invents policy."""

    role: str
    kind: str  # dir | file | symlink
    path: str
    mode: int
    owner: str | None
    group: str | None
    status: str = "planned"  # planned | created | kept
    preserve: bool = False  # operator data — uninstall reports + keeps
    target: str | None = None  # symlink target (relative to the link dir)
    remove_mode: str = "file"  # strict-dir | rmtree | skip | file | symlink

    def to_dict(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "role": self.role,
            "kind": self.kind,
            "path": self.path,
            "mode": format(self.mode, "04o"),
            "owner": self.owner,
            "group": self.group,
            "status": self.status,
            "preserve": self.preserve,
            "remove_mode": self.remove_mode,
        }
        if self.target is not None:
            doc["target"] = self.target
        return doc

    @classmethod
    def from_dict(cls, doc: dict[str, Any]) -> StateArtifact:
        return cls(
            role=str(doc["role"]),
            kind=str(doc["kind"]),
            path=str(doc["path"]),
            mode=int(str(doc["mode"]), 8),
            owner=doc.get("owner"),
            group=doc.get("group"),
            status=str(doc.get("status", "planned")),
            preserve=bool(doc.get("preserve", False)),
            target=doc.get("target"),
            remove_mode=str(doc.get("remove_mode", "file")),
        )


# ── Ownership (root applies it; a simulation records it only) ─────────


class _Ownership:
    """uid/gid resolution for the manifest owner/group names.

    ``apply=False`` (non-root simulation): the intended owner/group are
    recorded in the install-state but NOT applied — the report says so,
    and the doctor DR-15 rights walk is expected to flag the drift on a
    simulated tree (honest, by design).
    """

    def __init__(self, apply_ownership: bool) -> None:
        self.apply = apply_ownership
        self._uids: dict[str, int] = {}
        self._gids: dict[str, int] = {}
        # F2: the installer reports HERE what step 0 (the sysusers leg)
        # already did, so a resolution failure can never hand out a
        # remediation that points at a step that has not run.
        self.sysusers_path: Path | None = None
        self.sysusers_status: str | None = None

    def _account_hint(self) -> str:
        if self.sysusers_path is None:
            return (
                "the sysusers.d fragment must be applied first"
                " (fix: re-run `sudo vesma system install`, it"
                " applies the fragment via systemd-sysusers)"
            )
        return (
            "the sysusers.d fragment was written and applied at the START"
            f" of this run (systemd-sysusers: {self.sysusers_status}) —"
            " fix the reported systemd-sysusers failure (or reboot: the"
            f" fragment at {self.sysusers_path} applies on boot), then"
            " re-run `sudo vesma system install`"
        )

    def uid(self, name: str | None) -> int | None:
        if name is None:
            return None
        if name not in self._uids:
            try:
                self._uids[name] = pwd.getpwnam(name).pw_uid
            except KeyError as exc:
                raise SystemInstallError(
                    f"system account {name!r} does not exist — {self._account_hint()}"
                ) from exc
        return self._uids[name]

    def gid(self, name: str | None) -> int | None:
        if name is None:
            return None
        if name not in self._gids:
            try:
                self._gids[name] = grp.getgrnam(name).gr_gid
            except KeyError as exc:
                raise SystemInstallError(
                    f"system group {name!r} does not exist — {self._account_hint()}"
                ) from exc
        return self._gids[name]

    def apply_to(self, path: Path, owner: str | None, group: str | None) -> None:
        if not self.apply:
            return
        uid = self.uid(owner)
        gid = self.gid(group)
        if uid is None and gid is None:
            return
        os.chown(path, uid if uid is not None else -1, gid if gid is not None else -1)


# ── Filesystem primitives (atomic, explicit modes — never umask) ──────


def _atomic_write(path: Path, data: str, mode: int) -> None:
    """Same-directory tmp file + ``os.replace`` (the LY-02 drop-in
    discipline; O_EXCL semantics mean a pre-placed symlink at ``path``
    can never be followed)."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.tmp-")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _atomic_symlink(target: Path, link_path: Path) -> None:
    """Atomically point ``link_path`` at ``target`` (tmp link + rename).

    The revision-swap primitive (§3.10): a partial or missing venv is
    never observable through the symlink."""
    tmp = link_path.parent / f".{link_path.name}.tmp-link-{os.getpid()}"
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    os.symlink(target, tmp)
    os.replace(tmp, link_path)


def _ensure_dir(path: Path, mode: int) -> bool:
    """mkdir -p + explicit chmod (SY-03: never umask-inherited).

    Only the manifest-listed leaf is chmodded — parent segments above
    the vesma namespace (/etc, /var, /usr/lib, …) are not ours. Returns
    True when the directory was CREATED by this call, False when it
    already existed."""
    if path.is_dir():
        os.chmod(path, mode)
        return False
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)
    return True


# ── Subprocess seams (monkeypatched in tests) ─────────────────────────


def _uv_path() -> str | None:
    return shutil.which("uv")


def _run_uv(args: list[str], *, timeout: int, env: dict[str, str] | None = None) -> str:
    uv = _uv_path()
    if uv is None:
        raise SystemInstallError(
            "uv is not on PATH — the system installer materializes venvs with"
            " `uv venv` (pip-as-root is forbidden, specs/layout/v2 §3.10)"
            " — fix: install uv (https://docs.astral.sh/uv/), then re-run"
            " `sudo vesma system install`"
        )
    try:
        result = subprocess.run(  # nosec B603 - fixed argv, no shell
            [uv, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise SystemInstallError(f"uv {' '.join(args[:2])} timed out") from exc
    if result.returncode != 0:
        raise SystemInstallError(f"uv {' '.join(args[:2])} failed: {result.stderr.strip()[-500:]}")
    return result.stdout


def _apply_sysusers(fragment: Path) -> str:
    """Best-effort ``systemd-sysusers <fragment>`` (never fatal — a
    non-systemd machine gets the fragment on disk for the next boot)."""
    binary = shutil.which("systemd-sysusers")
    if binary is None:
        return "skipped: systemd-sysusers not found (fragment applied on next boot)"
    try:
        result = subprocess.run(  # nosec B603 - fixed argv, no shell
            [binary, str(fragment)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "skipped: systemd-sysusers timed out"
    if result.returncode != 0:
        return f"skipped: systemd-sysusers failed: {result.stderr.strip()[:200]}"
    return "ok"


def _systemctl_system(args: list[str]) -> str:
    """Best-effort ``systemctl <args>`` (system scope, never fatal)."""
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return "skipped: systemctl not found"
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
    except subprocess.TimeoutExpired:
        return "skipped: systemctl timed out"
    if result.returncode != 0:
        return f"skipped: {result.stderr.strip()[:200]}"
    return "ok"


def _materialize_venv(
    venv_dir: Path,
    requirements: tuple[str, ...],
    mode: int,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, str]:
    """``uv venv`` + exact pins via ``uv pip`` — the SY-08 leg.

    ``mode`` comes from the FHS manifest entry (0755 root:root for the
    system venv trees, specs/layout/v2 §3.4) and is enforced BEFORE any
    wheel content lands — never a hardcoded literal, never group- or
    world-writable. ``env`` carries UV_PYTHON_INSTALL_DIR for the run
    (the canonical toolchain-pythons role — a managed python downloaded
    under sudo's $HOME would be 0700 root and unexecutable by the
    service account). The created venv has NO pip (uv default):
    pip-as-root is not merely avoided, it does not exist in the tree.
    Returns the normalized freeze map of the fresh venv. Broken/incomplete
    revision dirs are rebuilt from scratch by the caller.
    """
    if venv_dir.exists():
        shutil.rmtree(venv_dir)
    _run_uv(["venv", str(venv_dir)], timeout=300, env=env)
    os.chmod(venv_dir, mode)
    if requirements:
        _run_uv(
            [
                "pip",
                "install",
                "--python",
                str(venv_dir / "bin" / "python"),
                *requirements,
            ],
            timeout=1800,
            env=env,
        )
    freeze_out = _run_uv(
        ["pip", "freeze", "--python", str(venv_dir / "bin" / "python")],
        timeout=300,
        env=env,
    )
    frozen: dict[str, str] = {}
    for line in freeze_out.splitlines():
        parsed = user_install.parse_pin_line(line)
        if parsed is not None:
            frozen[parsed[0]] = parsed[1]
    return frozen


# ── Install-state (§3.13 — the exact-inverse uninstall source) ────────


def _state_path(prefix: Path) -> Path:
    return fhs.system_install_state_path(prefix)


def _write_state(prefix: Path, doc: dict[str, Any], ownership: _Ownership) -> None:
    path = _state_path(prefix)
    doc["updated"] = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    _atomic_write(path, text, 0o640)
    entry = fhs.load_fhs_manifest().entry("install_state")
    ownership.apply_to(path, entry.owner, entry.group)


def read_system_state(prefix: Path | None = None) -> dict[str, Any] | None:
    """The parsed install-state, or ``None`` when absent.

    An UNPARSABLE state is NOT "absent" — the caller refuses loudly
    (silent tolerance here would let an uninstall run half-blind)."""
    path = _state_path(fhs.default_prefix() if prefix is None else prefix)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SystemInstallError(
            f"cannot read the system install-state at {path}: {exc} —"
            " refusing to guess (fix: run `vesma doctor`)"
        ) from exc
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise SystemInstallError(
            f"the system install-state at {path} is unparsable ({exc}) —"
            " refusing to uninstall from a broken state (fix: run"
            " `vesma doctor`)"
        ) from exc
    if not isinstance(doc, dict) or "artifacts" not in doc:
        raise SystemInstallError(
            f"the system install-state at {path} has no artifacts list —"
            " refusing to uninstall from an unknown shape"
        )
    return doc


def read_system_profile(prefix: Path | None = None) -> str | None:
    """``profile:`` from the system install-state (None = not installed)."""
    doc = read_system_state(prefix)
    if doc is None:
        return None
    profile = doc.get("profile")
    return str(profile) if profile is not None else None


#: How many drift problems system_state_drift reports before it stops
#: walking (a wildly drifted tree must not produce an endless list; the
#: verdict is the same either way — refuse).
_DRIFT_REPORT_CAP: Final[int] = 50


def system_state_drift(prefix: Path | None = None) -> list[str]:
    """Install-state ↔ filesystem drift of the system profile — the ONE
    predicate behind doctor DR-17's system leg AND the updater's
    preflight (specs/layout/v2 §3.14; ADR-0045 Decision 6).

    Both directions are drift, both loud:

    - a recorded artifact (status ≠ ``planned``) missing on disk;
    - a recorded artifact that changed type (symlink → dir, file → dir, …);
    - unrecorded content inside a recorded strict-dir — EXCEPT the
      operator conffile zone (everything under ``/etc/vesma``:
      ``config.yaml``, ``components.d`` drop-ins and ``env.d`` secrets
      are operator-provisioned by the manifest, so their presence is
      never drift).

    ``planned`` rows (interrupted install) are tolerated, mirroring the
    uninstall discipline. Read-only; the updater refuses on ANY returned
    problem BEFORE its first mutation, and the doctor renders the same
    list — one predicate, so diagnosis and refusal cannot disagree.
    """
    resolved_prefix = fhs.default_prefix() if prefix is None else prefix
    doc = read_system_state(resolved_prefix)
    if doc is None:
        return []
    try:
        artifacts = [StateArtifact.from_dict(a) for a in doc["artifacts"]]
    except (KeyError, TypeError, ValueError) as exc:
        return [f"install-state artifact records malformed: {exc}"]
    config_root = str(fhs.system_config_root(resolved_prefix))
    recorded = {a.path for a in artifacts}
    drift: list[str] = []
    for artifact in artifacts:
        if artifact.status == "planned":
            continue
        path = Path(artifact.path)
        if not path.exists() and not path.is_symlink():
            drift.append(f"recorded {artifact.kind} {path} is MISSING")
        else:
            try:
                _check_type(artifact, path)
            except SystemInstallError as exc:
                drift.append(str(exc))
            else:
                if artifact.remove_mode == "strict-dir" and not _in_conffile_zone(
                    path, config_root
                ):
                    try:
                        children = sorted(path.iterdir())
                    except OSError as exc:
                        drift.append(f"recorded dir {path} is unreadable: {exc}")
                        children = []
                    for child in children:
                        if str(child) in recorded:
                            continue
                        drift.append(
                            f"unrecorded content: {child} (inside the recorded tree {path})"
                        )
                        if len(drift) >= _DRIFT_REPORT_CAP:
                            drift.append("… further drift suppressed (cap reached)")
                            return drift
    return drift


def _in_conffile_zone(path: Path, config_root: str) -> bool:
    """True when ``path`` IS the conffile zone root or lives under it."""
    return str(path) == config_root or str(path).startswith(config_root + os.sep)


# ── Install ───────────────────────────────────────────────────────────


def _engine_version() -> str:
    from vesma import __version__

    return __version__


def _require_root_for_real_prefix(prefix: Path) -> None:
    if prefix == Path(os.sep) and os.geteuid() != 0:
        raise SystemInstallError(
            "the system profile installs into the real FHS tree — run it as"
            " root: `sudo vesma system install`"
        )


def regenerate_system_unit(prefix: Path | None = None) -> str:
    """The canon system unit text — the ONE source for the installer and
    the doctor's DR-18 leg (layout v2 §3.4; the DR-07 precedent: a
    doctor diff against a separately-maintained recipe is a drift bug
    waiting to happen, so both read this function).

    Pure path math from the FHS manifest + the deployment prefix; no
    filesystem I/O.
    """
    manifest = fhs.load_fhs_manifest()
    return unitgen.generate_system_unit(
        engine_venv=fhs.system_venv_engine(prefix),
        read_write_paths=(fhs.system_data_root(prefix), fhs.system_cache_root(prefix)),
        read_only_paths=(
            fhs.resolve_system_path(manifest.entry("code_root"), prefix),
            fhs.system_static_root(prefix),
        ),
        optional_read_write_paths=(fhs.resolve_system_path(manifest.entry("log_root"), prefix),),
        # Config wiring (card 4c8a0cbe): the FHS conffile + the data root
        # as the working directory — every path manifest/prefix-resolved
        # (SY-02: no path literals here).
        config_path=fhs.system_config_path(prefix),
        working_directory=fhs.system_data_root(prefix),
    )


def _validate_conffile_candidate(source: Path, data: str) -> None:
    """Validate EXACTLY the bytes that will be written — the ONE machinery.

    The candidate text is staged to a 0600 ``mkstemp`` copy and loaded
    through :func:`vesma.config.load_settings` (the same validation
    ``vesma doctor`` and the system daemon's own startup run), so the
    validated bytes and the installed bytes are the SAME bytes — a naive
    ``load_settings(source)`` followed by a second read would leave a
    window where a swapped file lands unvalidated in the root-owned FHS
    tree. The refusal names the OPERATOR's path, never the staging temp.
    """
    try:
        fd, tmp_name = tempfile.mkstemp(suffix=".yaml", prefix="vesma-config-candidate-")
    except OSError as exc:
        raise SystemInstallError(
            f"cannot stage candidate config {source} for validation ({exc}) — nothing was changed"
        ) from exc
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        try:
            load_settings(tmp)
        except Exception as exc:  # candidate bytes are operator data — refuse loud
            raise SystemInstallError(
                f"candidate config {source} is NOT valid ({exc}) — refused BEFORE"
                " any mutation: nothing was backed up, nothing was replaced"
            ) from exc
    except OSError as exc:
        raise SystemInstallError(
            f"cannot stage candidate config {source} for validation ({exc}) — nothing was changed"
        ) from exc
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_backup(target: Path, backup: Path, entry: Any, ownership: _Ownership) -> None:
    """Land the conffile backup through the same atomic discipline as the
    install itself (security spot-check P3-2): mkstemp same-dir → the
    CURRENT bytes (read once) → the previous mode → ``os.replace`` — a
    torn ``.bak`` on a mid-write crash is impossible, and the manifest
    ownership keeps the backup readable by the vesma group."""
    old_mode = target.stat().st_mode & 0o777
    data = target.read_bytes()
    fd, tmp_name = tempfile.mkstemp(
        dir=str(backup.parent), prefix=f".{backup.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, old_mode)
        os.replace(tmp, backup)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    ownership.apply_to(backup, entry.owner, entry.group)


def install_system_conffile(source: Path, prefix: Path | None = None) -> SystemConfigSetResult:
    """Install an operator-provided config as the system conffile (CLI
    topology v2 wave C: the ONLY write of the ``vesma system config``
    domain — ``set --file``).

    Discipline (ratified contract, ArchCom 2026-10-23):

    * root for the real prefix (the :func:`_require_root_for_real_prefix`
      IDIOM — prefix-aware gate, this verb's own canonical spelling in
      the hint; the dest-dir simulation runs unprivileged);
    * VALIDATE FIRST through the ONE settings machinery
      (:func:`vesma.config.load_settings` — the same validation ``vesma
      doctor`` and the system daemon's startup run) on EXACTLY the bytes
      that will be written (staged to a 0600 temp copy, so a candidate
      swapped mid-command can never land unvalidated): an invalid
      candidate is refused BEFORE any mutation — no backup, no replace,
      the tree stays byte-identical;
    * the previous conffile is backed up SAME-DIR as
      ``config.yaml.bak-<ts>`` (append-only: a same-second collision
      grows a counter, an earlier backup is never clobbered);
    * the install is ATOMIC (:func:`_atomic_write` — same-dir tmp +
      ``os.replace``; a pre-placed symlink at the target can never be
      followed) with the manifest role's mode (0640);
    * ownership per the manifest row (root:vesma), resolved BEFORE any
      mutation (a missing account refuses with the tree untouched) and
      applied only under real root (the :class:`_Ownership` simulation
      discipline);
    * the written bytes are EXACTLY the candidate's bytes — NO mover
      provenance header is added, so the uninstall adopt predicate
      (F9) keeps refusing a set-installed conffile (operator content).

    Refuses (typed, tree untouched) when the system config root does
    not exist — ``set`` configures an INSTALLED system profile, it
    never lays down the FHS tree itself.
    """
    resolved_prefix = fhs.default_prefix() if prefix is None else prefix
    # The _require_root_for_real_prefix IDIOM (prefix-aware gate), with THIS
    # verb's own canonical spelling in the hint — the install gate's message
    # names `vesma system install`, wrong for a config verb.
    if resolved_prefix == Path(os.sep) and os.geteuid() != 0:
        raise SystemInstallError(
            f"the system conffile lives in the real FHS tree — run it as"
            f" root: `sudo vesma system config set --file {source}`"
        )
    if not source.is_file():
        raise SystemInstallError(
            f"candidate config {source} not found (or not a regular file) — nothing was changed"
        )
    try:
        data = source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SystemInstallError(
            f"cannot read candidate config {source} ({exc}) — nothing was changed"
        ) from exc
    _validate_conffile_candidate(source, data)

    entry = fhs.load_fhs_manifest().entry("shared_config")
    target = fhs.system_config_path(resolved_prefix)
    config_root = fhs.system_config_root(resolved_prefix)
    # Security spot-check P3-1: the gate is the INSTALL-STATE, not the
    # bare directory — a hand-created /etc/vesma without `system install`
    # must not receive a root-owned conffile (fail-closed on anything but
    # an installed system profile; subsumes the absent-config-root case).
    if read_system_profile(resolved_prefix) != "system" or not config_root.is_dir():
        raise SystemInstallError(
            "the system profile is not installed (no install-state with"
            " `profile: system`) — `set` configures an INSTALLED system"
            " profile; install it first: `sudo vesma system install`"
        )

    # Resolve the manifest owner/group BEFORE any mutation (the F2 lesson):
    # a machine without the vesma account/group (sysusers never ran) must
    # refuse with the tree untouched — not after the conffile was already
    # replaced. The resolution is cached; apply_to below re-uses it.
    ownership = _Ownership(apply_ownership=os.geteuid() == 0)
    ownership.uid(entry.owner)
    ownership.gid(entry.group)

    backup: Path | None = None
    first_conffile = not (target.is_file() and not target.is_symlink())
    if not first_conffile:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = target.with_name(f"{target.name}.bak-{stamp}")
        counter = 1
        while backup.exists():  # same-second collision: never clobber a backup
            backup = target.with_name(f"{target.name}.bak-{stamp}-{counter}")
            counter += 1
        _atomic_backup(target, backup, entry, ownership)

    _atomic_write(target, data, entry.mode)
    ownership.apply_to(target, entry.owner, entry.group)
    return SystemConfigSetResult(
        target=target,
        backup=backup,
        mode=entry.mode,
        owner=entry.owner,
        group=entry.group,
        first_conffile=first_conffile,
    )


def install_system(prefix: Path | None = None) -> SystemInstallResult:
    """Install the system profile. Idempotent (§3.13: a re-run is a
    refresh — byte-identical artifacts are kept, nothing duplicates).

    Venv materialization is ONLINE by design: ``uv pip install`` pulls
    the same-build wheel pin from PyPI at install time; offline bundling
    of wheels is deferred to the packaging wave (deb/rpm, ADR-0045
    rollout, last)."""
    resolved_prefix = fhs.default_prefix() if prefix is None else prefix
    _require_root_for_real_prefix(resolved_prefix)
    manifest = fhs.load_fhs_manifest()
    ownership = _Ownership(apply_ownership=os.geteuid() == 0)
    report: list[str] = []
    if not ownership.apply:
        report.append(
            "note: non-root simulation — ownership is RECORDED in the"
            " install-state but not applied (dest-dir technique)"
        )

    version = _engine_version()
    state_doc: dict[str, Any] = {
        "schema": STATE_SCHEMA,
        "profile": "system",
        "generation": "v2",
        "engine_version": version,
        "prefix": str(resolved_prefix),
        "artifacts": [],
    }
    artifacts: list[StateArtifact] = []
    revisions: list[Path] = []

    def plan(
        role: str,
        path: Path,
        *,
        remove_mode: str,
        preserve: bool = False,
        target: Path | None = None,
    ) -> StateArtifact:
        entry = manifest.entry(role)
        artifact = StateArtifact(
            role=role,
            kind=entry.kind,
            path=str(path),
            mode=entry.mode,
            owner=entry.owner,
            group=entry.group,
            preserve=preserve,
            target=str(target) if target is not None else None,
            remove_mode=remove_mode,
        )
        artifacts.append(artifact)
        return artifact

    def make_dir(
        role: str,
        path: Path,
        *,
        preserve: bool = False,
        remove_mode: str | None = None,
    ) -> Path:
        entry = manifest.entry(role)
        created = _ensure_dir(path, entry.mode)
        ownership.apply_to(path, entry.owner, entry.group)
        artifact = plan(
            role,
            path,
            remove_mode="skip" if preserve else (remove_mode or "strict-dir"),
            preserve=preserve,
        )
        artifact.status = "created" if created else "kept"
        if created:
            report.append(f"dir: {path} ({format(entry.mode, '04o')} {entry.owner}:{entry.group})")
        return path

    def write_file(role: str, path: Path, data: str) -> Path:
        entry = manifest.entry(role)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, data, entry.mode)
        ownership.apply_to(path, entry.owner, entry.group)
        plan(role, path, remove_mode="file").status = "created"
        return path

    # 0. Service account FIRST (SY-04; installer wave 2026-10-23, F2):
    #    the sysusers fragment is written and applied BEFORE the first
    #    ownership application that resolves the vesma account — on a
    #    fresh machine the old order (fragment at step 3, vesma chown at
    #    step 1) was unsolvable: the remediation text pointed at a step
    #    that had not run yet. The fragment write itself needs only
    #    root:root, never the vesma account.
    data_root = fhs.system_data_root(resolved_prefix)
    sysusers_path = fhs.system_sysusers_path(resolved_prefix)
    fragment = _SYSUSERS_TEMPLATE.format(home=str(data_root))
    existed = sysusers_path.exists()
    with _rofs_guard(sysusers_path):
        write_file("sysusers_fragment", sysusers_path, fragment)
    report.append(
        f"sysusers fragment: {sysusers_path}"
        + (" (unchanged — the account is not duplicated on re-install)" if existed else "")
    )
    sysusers_status = _apply_sysusers(sysusers_path)
    report.append(f"systemd-sysusers: {sysusers_status}")
    ownership.sysusers_path = sysusers_path
    ownership.sysusers_status = sysusers_status

    # 1. Data plane next — the install-state lives inside it (§3.13);
    #    every vesma chown from here on resolves against an account the
    #    step above just made sure exists.
    make_dir("data_root", data_root, preserve=True)
    history = fhs.resolve_system_path(manifest.entry("supervisor_history"), resolved_prefix)
    make_dir("supervisor_history", history, preserve=True)
    cache_root = fhs.system_cache_root(resolved_prefix)
    make_dir("cache_root", cache_root, remove_mode="rmtree")  # regenerable, §3.11

    # 2. The PLAN is written before any further mutation (§3.13: every
    #    artifact recorded at/before creation — a crash leaves a
    #    complete plan, and uninstall can reason about it). The two
    #    mutations that precede it are the crash-tolerant ones: the
    #    sysusers fragment (step 0 — it must precede the first vesma
    #    resolution, F2) and the data-plane dirs (the state file lives
    #    inside the data root). The state file's own record joins the
    #    plan.
    state_entry = manifest.entry("install_state")
    state_artifact = StateArtifact(
        role="install_state",
        kind="file",
        path=str(_state_path(resolved_prefix)),
        mode=state_entry.mode,
        owner=state_entry.owner,
        group=state_entry.group,
        status="created",
        remove_mode="file",
    )
    artifacts.append(state_artifact)
    state_doc["artifacts"] = [a.to_dict() for a in artifacts]
    _write_state(resolved_prefix, state_doc, ownership)

    # 3. Config plane (root-owned, group vesma reads — SY-03).
    config_root = fhs.system_config_root(resolved_prefix)
    make_dir("config_root", config_root)
    make_dir("manifest_dir", fhs.system_components_dir(resolved_prefix))
    make_dir("env_dir", fhs.system_env_dir(resolved_prefix))
    report.append(
        f"config: {config_root} is the conffile zone — config.yaml is"
        " operator-provisioned and is NOT seeded by the installer"
    )

    # 4. Code plane + toolchain + engine venv revision (SY-07
    #    groundwork, SY-08; F3 toolchain slot). Every /usr-style leg
    #    runs under the EROFS guard (ADR-0048 Decision 5): a read-only
    #    target is the typed refusal, anything else keeps its class.
    code_root_path = fhs.resolve_system_path(manifest.entry("code_root"), resolved_prefix)
    toolchain_dir = fhs.system_toolchain_pythons(resolved_prefix)
    venvs_root_path = fhs.system_venvs_root(resolved_prefix)
    with _rofs_guard(code_root_path):
        make_dir("code_root", code_root_path)
    with _rofs_guard(toolchain_dir):
        make_dir("toolchain_pythons", toolchain_dir, remove_mode="rmtree")
    # The materialize leg resolves/downloads interpreters into the
    # canonical toolchain slot — never sudo's $HOME/.local/share/uv
    # (0700 root: the service account could not execute the engine) and
    # never a path inside the revision trees (C1 freshness).
    uv_env = {**os.environ, "UV_PYTHON_INSTALL_DIR": str(toolchain_dir)}
    with _rofs_guard(venvs_root_path):
        make_dir("venv_component_root", venvs_root_path)
    # Revision id = the engine version for THIS wave (chairman
    # ratification 2026-10-20): same build == same content-id, which is
    # what keeps a re-install idempotent. A freeze-hash content-id is
    # the updater wave's (ADR-0045 rollout 3) candidate — do not change
    # it here without that wave's health-gate semantics.
    engine_rev_dir = fhs.system_venv_engine_revision(version, resolved_prefix)
    engine_symlink = fhs.system_venv_engine(resolved_prefix)
    # The engine venv's lock IS its wheel pin (vesma==version) — no lock
    # file; the component venvs below carry freeze locks.
    with _rofs_guard(engine_rev_dir):
        _ensure_venv_revision(
            "venv_engine_revision",
            engine_rev_dir,
            (f"vesma=={version}",),
            plan,
            revisions,
            report,
            ownership,
            done_marker="bin/vesma",
            uv_env=uv_env,
        )
    with _rofs_guard(engine_symlink):
        _ensure_venv_symlink("venv_engine", engine_symlink, engine_rev_dir, plan, report)

    # 5. Bundled components: manifests (SY-05 conffile discipline), data
    #    dirs, python children's venvs.
    components_dir = fhs.system_components_dir(resolved_prefix)
    for name in user_install.BUNDLED_COMPONENTS:
        bundle_text = bundled_manifest_path(name).read_text(encoding="utf-8")
        target = components_dir / f"{name}.yaml"
        entry = manifest.entry("component_manifest")
        if not target.exists():
            _atomic_write(target, bundle_text, entry.mode)
            ownership.apply_to(target, entry.owner, entry.group)
            plan("component_manifest", target, remove_mode="file").status = "created"
            report.append(f"manifest: {target}")
        elif target.read_text(encoding="utf-8") == bundle_text:
            # Byte-identical: keep the file untouched (an operator file is
            # never rewritten needlessly — mtime discipline).
            plan("component_manifest", target, remove_mode="file").status = "kept"
            report.append(f"manifest (unchanged): {target}")
        else:
            # SY-05: the operator's edit is the file of record — NEVER
            # overwritten. The new default lands NEXT TO it as
            # `<name>.yaml.rpmnew` (the .rpmnew convention: diff, merge,
            # remove), and a pending .rpmnew is never clobbered either —
            # an unreviewed default must not silently disappear.
            new_default = components_dir / f"{target.name}{CONFFILE_NEW_SUFFIX}"
            if new_default.exists():
                report.append(
                    f"conffile kept (operator edit): {target}; a new default"
                    f" already waits at {new_default} (SY-05: diff, merge,"
                    " remove the .rpmnew)"
                )
            else:
                _atomic_write(new_default, bundle_text, entry.mode)
                ownership.apply_to(new_default, entry.owner, entry.group)
                report.append(
                    f"conffile kept (operator edit): {target}; new default"
                    f" written to {new_default} (SY-05: diff, merge, remove"
                    " the .rpmnew)"
                )
            plan("component_manifest", target, remove_mode="file").status = "kept"
        # Every pending .rpmnew in the drop-in zone is recorded (status
        # kept) — installer-created content the exact-inverse uninstall
        # owns, never invisible to the state.
        for pending in sorted(components_dir.glob(f"*{CONFFILE_NEW_SUFFIX}")):
            if any(a.path == str(pending) for a in artifacts):
                continue
            pending_entry = manifest.entry("component_manifest")
            artifacts.append(
                StateArtifact(
                    role="component_manifest_rpmnew",
                    kind="file",
                    path=str(pending),
                    mode=pending_entry.mode,
                    owner=pending_entry.owner,
                    group=pending_entry.group,
                    status="kept",
                    remove_mode="file",
                )
            )

    installation = load_installation(components_dir)  # fail-closed (SY-02/LY-02)
    report.append(
        f"validation: components.d loaded fail-closed — "
        f"{len(installation)} component(s): {', '.join(sorted(installation))}"
    )
    for comp in sorted(installation.values(), key=lambda m: m.name):
        data_dir = fhs.system_component_data_dir(comp.name, resolved_prefix)
        make_dir("component_data", data_dir, preserve=True)
        if not user_install._has_venv(comp):
            report.append(
                f"venv: {comp.name} — no component venv (in-process or"
                " non-python child lives on the engine venv)"
            )
            continue
        requirements = user_install._effective_requirements(comp)
        for line in requirements:
            user_install._validate_pin(line)
        rev_dir = fhs.system_venv_component_revision(comp.name, version, resolved_prefix)
        with _rofs_guard(rev_dir):
            frozen = _ensure_venv_revision(
                "venv_component_revision",
                rev_dir,
                requirements,
                plan,
                revisions,
                report,
                ownership,
                done_marker="bin/python",
                uv_env=uv_env,
            )
        symlink = fhs.system_venv_component(comp.name, resolved_prefix)
        with _rofs_guard(symlink):
            _ensure_venv_symlink("venv_component", symlink, rev_dir, plan, report)
        # The lock is the full freeze of the materialized venv (the
        # freeze==lock discipline, layout v2 §3.10); the updater of
        # rollout 3 verifies against it before swapping.
        lock_text = "".join(f"{dist}=={ver}\n" for dist, ver in sorted(frozen.items()))
        lock_path = data_dir / "requirements-lock.txt"
        _atomic_write(lock_path, lock_text, 0o640)
        ownership.apply_to(lock_path, "vesma", "vesma")
        report.append(f"lock: {lock_path}")

    # 6. Distro static (read-only at runtime; content ships later waves).
    static_root = fhs.system_static_root(resolved_prefix)
    with _rofs_guard(static_root):
        make_dir("distro_static", static_root)

    # 7. The system unit (§3.4; coexists with the user unit by scope).
    #    The text comes from regenerate_system_unit — the ONE source the
    #    doctor's DR-18 leg re-reads, so install and doctor can never
    #    disagree on the canon.
    unit_path = fhs.system_unit_path(resolved_prefix)
    unit_text = regenerate_system_unit(resolved_prefix)
    write_file("system_unit", unit_path, unit_text)
    report.append(f"unit: {unit_path} (0644, generated — do not edit)")
    report.append(f"systemctl daemon-reload: {_systemctl_system(['daemon-reload'])}")

    # 8. Final state reconcile (statuses final; the exact-inverse source).
    state_doc["artifacts"] = [a.to_dict() for a in artifacts]
    _write_state(resolved_prefix, state_doc, ownership)
    report.append(f"install-state: {_state_path(resolved_prefix)} (profile: system)")

    logger.info(
        "system profile installed: prefix=%s components=%s revisions=%s unit=%s",
        resolved_prefix,
        sorted(installation),
        [r.name for r in revisions],
        unit_path,
    )
    return SystemInstallResult(
        lines=tuple(report),
        state_path=_state_path(resolved_prefix),
        unit_path=unit_path,
        engine_venv=engine_symlink,
        revisions=tuple(revisions),
    )


def _ensure_venv_revision(
    role: str,
    rev_dir: Path,
    requirements: tuple[str, ...],
    plan: Any,
    revisions: list[Path],
    report: list[str],
    ownership: _Ownership,
    *,
    done_marker: str,
    uv_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Materialize one venv revision dir (SY-08) and record it; returns
    the normalized freeze map of the (fresh or reused) revision.

    Idempotent: the current build's revision dir is reused when its
    done-marker exists; an incomplete one (interrupted install) is
    rebuilt from scratch — the installer owns its own revision trees.
    ``uv_env`` (UV_PYTHON_INSTALL_DIR) rides only the materialize leg —
    a reused healthy revision resolves no interpreters."""
    entry = fhs.load_fhs_manifest().entry(role)
    healthy = (rev_dir / done_marker).exists()
    frozen: dict[str, str] = {}
    if healthy:
        os.chmod(rev_dir, entry.mode)
        report.append(f"venv revision (current): {rev_dir}")
    else:
        frozen = _materialize_venv(rev_dir, requirements, entry.mode, env=uv_env)
        os.chmod(rev_dir, entry.mode)
        for line in requirements:
            dist, _, expected = line.partition("==")
            normalized = re.sub(r"[-_.]+", "-", dist).lower()
            actual = frozen.get(normalized)
            if actual != expected:
                raise SystemInstallError(
                    f"venv drift right after materialization ({rev_dir}): "
                    f"{dist}=={expected} expected, freeze reports {actual!r} — "
                    "fix: re-run `sudo vesma system install`"
                )
        report.append(
            f"venv revision materialized: {rev_dir} (uv venv, no pip;"
            f" pins: {', '.join(requirements)})"
        )
    ownership.apply_to(rev_dir, entry.owner, entry.group)
    artifact = plan(role, rev_dir, remove_mode="rmtree")
    artifact.status = "kept" if healthy else "created"
    if rev_dir not in revisions:
        revisions.append(rev_dir)
    return frozen


def _ensure_venv_symlink(
    role: str,
    link_path: Path,
    rev_dir: Path,
    plan: Any,
    report: list[str],
) -> None:
    """Point the stable name at the revision (atomic tmp+rename swap)."""
    target_name = rev_dir.name
    if link_path.is_symlink() and os.readlink(link_path) == target_name:
        plan(role, link_path, remove_mode="symlink", target=Path(target_name)).status = "kept"
        report.append(f"venv symlink (current): {link_path} -> {target_name}")
        return
    _atomic_symlink(Path(target_name), link_path)
    plan(role, link_path, remove_mode="symlink", target=Path(target_name)).status = "created"
    report.append(f"venv symlink: {link_path} -> {target_name} (atomic swap)")


# ── Uninstall (SY-09 — exact inverse) ─────────────────────────────────


@dataclasses.dataclass(frozen=True)
class _AdoptEvidence:
    """The migration marker's conffile record + recoverability pointers
    (ADR-0047 F9: the ONLY evidence that lets uninstall delete a conffile
    it did not record)."""

    marker_path: str
    sha256: str
    source_home: str | None
    snapshot_dir: str | None


def _read_adopt_evidence(prefix: Path) -> _AdoptEvidence | None:
    """Parse the mover's preservation marker in the data root.

    ``None`` when the marker is absent, unreadable, malformed, or predates
    the conffile record (old markers adopt NOTHING — no guessing, F9 §4).
    The store_migration import is lazy: that module lazily imports THIS
    one (C1 preconditions), so a module-level edge would be circular.
    """
    from vesma.store_migration import MIGRATION_MARKER_NAME

    marker = fhs.system_data_root(prefix) / MIGRATION_MARKER_NAME
    try:
        doc = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    record = doc.get("conffile")
    if not isinstance(record, dict) or not isinstance(record.get("path"), str):
        return None
    if not isinstance(record.get("sha256"), str):
        return None
    return _AdoptEvidence(
        marker_path=str(record["path"]),
        sha256=str(record["sha256"]),
        source_home=str(doc["source_home"]) if isinstance(doc.get("source_home"), str) else None,
        snapshot_dir=str(doc["snapshot_dir"]) if isinstance(doc.get("snapshot_dir"), str) else None,
    )


def _adopted_migrated_conffile(path: Path, state: dict[str, Any] | None, *, prefix: Path) -> bool:
    """TRUE iff uninstall may adopt ``path`` as mover-published bytes.

    ALL five evidence checks must hold (F9; ArchCom-ratified 2026-10-23):
    (a) the path IS the fhs-resolved shared_config role — zero literals,
    the adopt scope never widens by so much as a byte;
    (b) the install-state exists and says ``profile: system``;
    (c) the migration marker in the data root carries a conffile record
    naming exactly this path (old markers without the record → False →
    the current loud refusal);
    (d) the file carries the mover's provenance header (operator-written
    bytes never carry it);
    (e) sha256 of the on-disk bytes equals the marker's recorded sha.
    Small, single-purpose, AND-composed — the security shape: no check
    can be skipped by an earlier one succeeding.
    """
    from vesma.store_migration import MIGRATE_CONFFILE_HEADER

    if state is None or state.get("profile") != "system":  # (b)
        return False
    if path != fhs.system_config_path(prefix):  # (a)
        return False
    evidence = _read_adopt_evidence(prefix)  # (c)
    if evidence is None or evidence.marker_path != str(path):
        return False
    if path.is_symlink() or not path.is_file():
        return False
    try:
        with path.open("r", encoding="utf-8") as handle:
            first_line = handle.readline()
    except (OSError, UnicodeDecodeError):
        return False
    if not first_line.startswith(MIGRATE_CONFFILE_HEADER):  # (d)
        return False
    return _sha256_of(path) == evidence.sha256  # (e)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def uninstall_system(prefix: Path | None = None, *, dry_run: bool = False) -> SystemUninstallResult:
    """Exact-inverse uninstall: recorded artifacts only, reverse creation
    order, loud refusal on drift or foreign content (§3.13).

    Operator data under ``/var/lib/vesma`` is preserved across uninstall
    BY DESIGN (chairman ratification 2026-10-20): exact-inverse covers
    only installer-created artifacts, mirroring the user-profile
    uninstall; explicit data removal is the operator's action.

    Two phases: VALIDATE everything first (presence, type, foreign
    content) — a refusal leaves the tree untouched; then MUTATE in
    reverse creation order. A half-removed tree that would poison every
    re-run with drift is exactly the failure class this split prevents.

    ``dry_run=True`` (G4, CLI topology v2 wave B) stops AFTER phase 1:
    the plan prints as ``dry-run:`` lines (including the preserved /
    not-materialized rows) and NOTHING is touched — no systemctl calls,
    no unlinks. Drift refuses identically in both modes: a plan over a
    drifted tree is a lie, never shown.
    """
    resolved_prefix = fhs.default_prefix() if prefix is None else prefix
    _require_root_for_real_prefix(resolved_prefix)
    doc = read_system_state(resolved_prefix)
    if doc is None:
        state = _state_path(resolved_prefix)
        raise SystemInstallError(
            f"no system install-state at {state} — nothing was recorded, so"
            " nothing is uninstalled (never hand-guess; inventory with"
            " `vesma doctor`)"
        )
    try:
        artifacts = [StateArtifact.from_dict(a) for a in doc["artifacts"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemInstallError(
            f"the system install-state at {_state_path(resolved_prefix)} has"
            f" malformed artifact records ({exc}) — refusing to uninstall"
        ) from exc

    recorded_paths = {a.path for a in artifacts}

    # The F9 adopt decision is made ONCE, before validation: the shared
    # config is adoptable ONLY when every evidence check holds. The
    # adopted row stays IN-MEMORY for this run — the install-state is
    # never written by uninstall (consumer, not writer; C12 verbatim).
    shared_config = fhs.system_config_path(resolved_prefix)
    adopt_evidence = _read_adopt_evidence(resolved_prefix)
    adoptable: set[str] = set()
    if shared_config.is_file() and _adopted_migrated_conffile(
        shared_config, doc, prefix=resolved_prefix
    ):
        adoptable.add(str(shared_config))

    # Phase 1 — full validation, zero mutation.
    drift: list[str] = []
    for artifact in reversed(artifacts):
        if artifact.preserve or artifact.remove_mode == "skip":
            continue
        path = Path(artifact.path)
        if not path.exists() and not path.is_symlink():
            if artifact.status == "planned":
                continue  # interrupted install — nothing was materialized
            drift.append(f"recorded {artifact.kind} {path} is MISSING")
            continue
        try:
            _check_type(artifact, path)
            _refuse_foreign(artifact, path, recorded_paths, adoptable)
        except SystemInstallError as exc:
            drift.append(str(exc))
    if adoptable:
        # The adopted conffile follows the recorded-artifact discipline
        # (kind=file): type drift on it refuses exactly like a recorded
        # file's would.
        config_entry = fhs.load_fhs_manifest().entry("shared_config")
        adopt_artifact = StateArtifact(
            role="shared_config_adopted",
            kind="file",
            path=str(shared_config),
            mode=config_entry.mode,
            owner=config_entry.owner,
            group=config_entry.group,
            status="kept",
            remove_mode="file",
        )
        try:
            _check_type(adopt_artifact, shared_config)
        except SystemInstallError as exc:
            drift.append(str(exc))
    if drift:
        raise SystemInstallError(
            "install-state ↔ filesystem drift — refusing the exact inverse"
            f" ({len(drift)} problem(s)); first: {drift[0]} — inventory with"
            " `vesma doctor`, fix or adopt the drift, then re-run"
            " `sudo vesma system uninstall`"
        )

    # Phase 2 — mutate, reverse creation order. The adopted conffile is
    # the NEWEST artifact (the mover published it after the install), so
    # its unlink leads the mutations — and re-hashes the evidence
    # against the FRESH bytes IMMEDIATELY before the unlink, BEFORE any
    # phase-2 side effect (the two-phase discipline, symmetric to the
    # mover's C7 double quiesce): evidence that went stale between
    # validate and mutate refuses the run with NOTHING touched.
    if (
        adoptable
        and adopt_evidence is not None
        and _sha256_of(shared_config) != adopt_evidence.sha256
    ):
        raise SystemInstallError(
            f"the adopted conffile {shared_config} changed between"
            " validation and removal (sha256 mismatch) — refusing"
            " (post-migration edits are operator data, never silently"
            " deleted)"
        )
    lines: list[str] = []
    removed = 0
    if dry_run:
        lines.append("dry-run: would run: systemctl disable --now vesma.service")
        if adoptable and adopt_evidence is not None:
            lines.append(f"dry-run: would unlink (adopted conffile): {shared_config}")
        for artifact in reversed(artifacts):
            path = Path(artifact.path)
            if artifact.preserve or artifact.remove_mode == "skip":
                lines.append(f"preserved (operator data): {path}")
                continue
            if not path.exists() and not path.is_symlink():
                lines.append(f"not materialized (interrupted install): {path}")
                continue
            lines.append(f"dry-run: would remove ({artifact.remove_mode}): {path}")
        lines.append("dry-run: would run: systemctl daemon-reload")
        lines.append(
            "dry-run: data plane preserved: "
            + str(fhs.system_data_root(resolved_prefix))
            + " (store home — never touched by uninstall)"
        )
        logger.info("system profile uninstall dry-run: prefix=%s", resolved_prefix)
        return SystemUninstallResult(lines=tuple(lines))
    lines.append(
        "systemctl disable --now vesma.service: "
        + _systemctl_system(["disable", "--now", "vesma.service"])
    )
    if adoptable and adopt_evidence is not None:
        shared_config.unlink()
        removed += 1
        lines.append(
            f"adopted-from-migrate-store: {shared_config} (unlinked —"
            " mover-published bytes, provenance header + sha256 verified;"
            " content recoverable from the migration snapshot: "
            f"{adopt_evidence.snapshot_dir or 'not recorded'}, source"
            f" home: {adopt_evidence.source_home or 'not recorded'})"
        )
    for artifact in reversed(artifacts):
        path = Path(artifact.path)
        if artifact.preserve or artifact.remove_mode == "skip":
            lines.append(f"preserved (operator data): {path}")
            continue
        if not path.exists() and not path.is_symlink():
            lines.append(f"not materialized (interrupted install): {path}")
            continue
        if artifact.remove_mode == "rmtree":
            shutil.rmtree(path)
        elif artifact.remove_mode == "strict-dir":
            path.rmdir()
        else:
            path.unlink()
        removed += 1
        lines.append(f"removed: {path}")

    lines.append("systemctl daemon-reload: " + _systemctl_system(["daemon-reload"]))
    lines.append(
        "data plane preserved: "
        + str(fhs.system_data_root(resolved_prefix))
        + " (store home — the user-profile uninstall keeps operator data too)"
    )
    logger.info("system profile uninstalled: prefix=%s removed=%d", resolved_prefix, removed)
    return SystemUninstallResult(lines=tuple(lines))


def _check_type(artifact: StateArtifact, path: Path) -> None:
    """Type drift (recorded file exists as dir, symlink as file, …) is a
    loud refusal — a blind unlink/rmdir could destroy foreign data."""
    if artifact.kind == "symlink":
        if not path.is_symlink():
            raise SystemInstallError(
                f"install-state ↔ filesystem drift: {path} is recorded as a"
                " symlink but is no longer one — refusing (fix: run"
                " `vesma doctor`)"
            )
        return
    if artifact.kind == "dir" and not path.is_dir():
        raise SystemInstallError(
            f"install-state ↔ filesystem drift: {path} is recorded as a dir"
            " but is not one — refusing (fix: run `vesma doctor`)"
        )
    if artifact.kind == "file" and (path.is_dir() or path.is_symlink()):
        raise SystemInstallError(
            f"install-state ↔ filesystem drift: {path} is recorded as a file"
            " but is a directory/symlink — refusing (fix: run `vesma doctor`)"
        )


def _refuse_foreign(
    artifact: StateArtifact, path: Path, recorded: set[str], adopted: set[str]
) -> None:
    """A strict dir must contain ONLY recorded artifacts (or the ONE
    evidence-checked adopted conffile, F9) before its rmdir/rmtree —
    foreign content = loud refusal (SY-09), never a silent skip."""
    if artifact.remove_mode != "strict-dir":
        return
    for child in sorted(path.iterdir()):
        if str(child) in recorded or str(child) in adopted:
            continue
        raise SystemInstallError(
            f"foreign content in the recorded tree: {child} (inside"
            f" {path}) is NOT in the install-state — refusing to remove"
            " the directory (fix: inventory with `vesma doctor`, remove"
            " or adopt the foreign file, then re-run"
            " `sudo vesma system uninstall`)"
        )


__all__ = [
    "CONFFILE_NEW_SUFFIX",
    "READ_ONLY_TARGET_EXIT_CODE",
    "REVISION_ROLES",
    "STATE_SCHEMA",
    "StateArtifact",
    "SystemInstallError",
    "SystemInstallReadOnlyTargetError",
    "SystemInstallResult",
    "SystemUninstallResult",
    "install_system",
    "read_system_profile",
    "read_system_state",
    "regenerate_system_unit",
    "system_state_drift",
    "uninstall_system",
]
