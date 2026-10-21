"""``vesma service install --system`` — the canonical system-profile
installer (specs/layout v2 §3.13; ADR-0045 Decisions 4-6, rollout wave 2).

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
- **SY-08** — venvs are materialized by the installer: ``uv venv`` +
  ``uv pip install`` of the SAME-BUILD wheel pin (``vesma==<running
  version>``). ``pip`` as root is never invoked — the venv has no pip
  at all (``uv venv`` default); foreign venvs are never written (every
  path is manifest-derived);
- **SY-09** — every artifact is recorded in the install-state AT/BEFORE
  creation (the full plan is written before the first mutation);
  ``uninstall --system`` is the EXACT INVERSE: recorded artifacts only,
  reverse creation order, foreign content = loud refusal (never a
  silent skip), state ↔ filesystem drift = loud refusal with a doctor
  pointer. Operator data (the store home plane) is preserved and
  reported, mirroring the user-profile uninstall discipline.

The dest-dir technique: the whole flow runs against a deployment
``prefix`` (default ``/`` — canonical FHS). Root-less conformance tests
monkeypatch :func:`vesma.service.fhs.default_prefix` to a temp dir; the
real-prefix run refuses to start without euid 0 (sudo), and ownership
application is skipped (recorded, not applied) in a simulation.

Deliberately NOT this wave (ADR-0045 rollout 3): the conffile update
policy beyond the one-time backup, the symlink-swap UPDATER with its
health gate, doctor DR-16/DR-17(system)/DR-18.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import grp
import logging
import os
import pwd
import re
import shutil
import subprocess  # nosec B404 - fixed-argv installer subprocesses
import tempfile
from pathlib import Path
from typing import Any, Final

import yaml

from vesma.service import fhs, unitgen
from vesma.service import install as user_install
from vesma.service.manifest import bundled_manifest_path, load_installation

logger = logging.getLogger("vesma.service.system_install")

#: Install-state schema version (additive growth only).
STATE_SCHEMA: Final[int] = 1

#: The sysusers.d fragment content (§3.13: the service account comes from
#: HERE — static text, so a re-install can never duplicate the account;
#: systemd-sysusers creates users with a nologin shell by design).
_SYSUSERS_TEMPLATE = (
    "# vesma service account (specs/layout/v2 §3.4/§3.13; written by"
    " `vesma service install --system`).\n"
    "# systemd-sysusers(8): system user, nologin shell, home = the data root.\n"
    "#Type Name   ID GECOS              Home directory\n"
    "g     vesma  -  -\n"
    'u     vesma  -  "VESMA supervisor" {home}\n'
)


class SystemInstallError(Exception):
    """System install/uninstall refused — the message is operator-facing."""


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

    def uid(self, name: str | None) -> int | None:
        if name is None:
            return None
        if name not in self._uids:
            try:
                self._uids[name] = pwd.getpwnam(name).pw_uid
            except KeyError as exc:
                raise SystemInstallError(
                    f"system account {name!r} does not exist — the sysusers.d"
                    " fragment must be applied first"
                    " (fix: re-run `sudo vesma service install --system`, it"
                    " applies the fragment via systemd-sysusers)"
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
                    f"system group {name!r} does not exist — the sysusers.d"
                    " fragment must be applied first"
                    " (fix: re-run `sudo vesma service install --system`)"
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


def _run_uv(args: list[str], *, timeout: int) -> str:
    uv = _uv_path()
    if uv is None:
        raise SystemInstallError(
            "uv is not on PATH — the system installer materializes venvs with"
            " `uv venv` (pip-as-root is forbidden, specs/layout/v2 §3.10)"
            " — fix: install uv (https://docs.astral.sh/uv/), then re-run"
            " `sudo vesma service install --system`"
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


def _materialize_venv(venv_dir: Path, requirements: tuple[str, ...], mode: int) -> dict[str, str]:
    """``uv venv`` + exact pins via ``uv pip`` — the SY-08 leg.

    ``mode`` comes from the FHS manifest entry (0755 root:root for the
    system venv trees, specs/layout/v2 §3.4) and is enforced BEFORE any
    wheel content lands — never a hardcoded literal, never group- or
    world-writable. The created venv has NO pip (uv default): pip-as-root
    is not merely avoided, it does not exist in the tree. Returns the
    normalized freeze map of the fresh venv. Broken/incomplete revision
    dirs are rebuilt from scratch by the caller.
    """
    if venv_dir.exists():
        shutil.rmtree(venv_dir)
    _run_uv(["venv", str(venv_dir)], timeout=300)
    os.chmod(venv_dir, mode)
    if requirements:
        _run_uv(
            [
                "pip",
                "install",
                "--python",
                str(venv_dir / "bin" / "python"),
                "--no-input",
                *requirements,
            ],
            timeout=1800,
        )
    freeze_out = _run_uv(
        ["pip", "freeze", "--python", str(venv_dir / "bin" / "python")],
        timeout=300,
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


# ── Install ───────────────────────────────────────────────────────────


def _engine_version() -> str:
    from vesma import __version__

    return __version__


def _require_root_for_real_prefix(prefix: Path) -> None:
    if prefix == Path(os.sep) and os.geteuid() != 0:
        raise SystemInstallError(
            "the system profile installs into the real FHS tree — run it as"
            " root: `sudo vesma service install --system`"
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

    # 1. Data plane first — the install-state lives inside it (§3.13).
    data_root = fhs.system_data_root(resolved_prefix)
    make_dir("data_root", data_root, preserve=True)
    history = fhs.resolve_system_path(manifest.entry("supervisor_history"), resolved_prefix)
    make_dir("supervisor_history", history, preserve=True)
    cache_root = fhs.system_cache_root(resolved_prefix)
    make_dir("cache_root", cache_root, remove_mode="rmtree")  # regenerable, §3.11

    # 2. The PLAN is written before any further mutation (§3.13: every
    #    artifact recorded at/before creation — a crash leaves a
    #    complete plan, and uninstall can reason about it). The state
    #    file's own record joins the plan.
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

    # 3. Service account via sysusers.d (§3.13; SY-04) — static content,
    #    idempotent by construction.
    sysusers_path = fhs.system_sysusers_path(resolved_prefix)
    fragment = _SYSUSERS_TEMPLATE.format(home=str(data_root))
    existed = sysusers_path.exists()
    write_file("sysusers_fragment", sysusers_path, fragment)
    report.append(
        f"sysusers fragment: {sysusers_path}"
        + (" (unchanged — the account is not duplicated on re-install)" if existed else "")
    )
    report.append(f"systemd-sysusers: {_apply_sysusers(sysusers_path)}")

    # 4. Config plane (root-owned, group vesma reads — SY-03).
    config_root = fhs.system_config_root(resolved_prefix)
    make_dir("config_root", config_root)
    make_dir("manifest_dir", fhs.system_components_dir(resolved_prefix))
    make_dir("env_dir", fhs.system_env_dir(resolved_prefix))
    report.append(
        f"config: {config_root} is the conffile zone — config.yaml is"
        " operator-provisioned and is NOT seeded by the installer"
    )

    # 5. Code plane + engine venv revision (SY-07 groundwork, SY-08).
    make_dir("code_root", fhs.resolve_system_path(manifest.entry("code_root"), resolved_prefix))
    make_dir("venv_component_root", fhs.system_venvs_root(resolved_prefix))
    # Revision id = the engine version for THIS wave (chairman
    # ratification 2026-10-20): same build == same content-id, which is
    # what keeps a re-install idempotent. A freeze-hash content-id is
    # the updater wave's (ADR-0045 rollout 3) candidate — do not change
    # it here without that wave's health-gate semantics.
    engine_rev_dir = fhs.system_venv_engine_revision(version, resolved_prefix)
    engine_symlink = fhs.system_venv_engine(resolved_prefix)
    # The engine venv's lock IS its wheel pin (vesma==version) — no lock
    # file; the component venvs below carry freeze locks.
    _ensure_venv_revision(
        "venv_engine_revision",
        engine_rev_dir,
        (f"vesma=={version}",),
        plan,
        revisions,
        report,
        ownership,
        done_marker="bin/vesma",
    )
    _ensure_venv_symlink("venv_engine", engine_symlink, engine_rev_dir, plan, report)

    # 6. Bundled components: manifests (conffile backup discipline),
    #    data dirs, python children's venvs.
    components_dir = fhs.system_components_dir(resolved_prefix)
    for name in user_install.BUNDLED_COMPONENTS:
        bundle_text = bundled_manifest_path(name).read_text(encoding="utf-8")
        target = components_dir / f"{name}.yaml"
        entry = manifest.entry("component_manifest")
        if target.exists() and target.read_text(encoding="utf-8") != bundle_text:
            backup = components_dir.parent / f"{target.name}.pre-regen.bak"
            if not backup.exists():
                _atomic_write(backup, target.read_text(encoding="utf-8"), 0o600)
                ownership.apply_to(backup, entry.owner, entry.group)
                backup_artifact = StateArtifact(
                    role="component_manifest_backup",
                    kind="file",
                    path=str(backup),
                    mode=0o600,
                    owner=entry.owner,
                    group=entry.group,
                    status="created",
                    remove_mode="file",
                )
                artifacts.append(backup_artifact)
                report.append(f"backup: {backup} (previous manifest preserved)")
        _atomic_write(target, bundle_text, entry.mode)
        ownership.apply_to(target, entry.owner, entry.group)
        plan("component_manifest", target, remove_mode="file").status = "created"
        report.append(f"manifest: {target}")

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
        frozen = _ensure_venv_revision(
            "venv_component_revision",
            rev_dir,
            requirements,
            plan,
            revisions,
            report,
            ownership,
            done_marker="bin/python",
        )
        symlink = fhs.system_venv_component(comp.name, resolved_prefix)
        _ensure_venv_symlink("venv_component", symlink, rev_dir, plan, report)
        # The lock is the full freeze of the materialized venv (the
        # freeze==lock discipline, layout v2 §3.10); the updater of
        # rollout 3 verifies against it before swapping.
        lock_text = "".join(f"{dist}=={ver}\n" for dist, ver in sorted(frozen.items()))
        lock_path = data_dir / "requirements-lock.txt"
        _atomic_write(lock_path, lock_text, 0o640)
        ownership.apply_to(lock_path, "vesma", "vesma")
        report.append(f"lock: {lock_path}")

    # 7. Distro static (read-only at runtime; content ships later waves).
    make_dir("distro_static", fhs.system_static_root(resolved_prefix))

    # 8. The system unit (§3.4; coexists with the user unit by scope).
    unit_path = fhs.system_unit_path(resolved_prefix)
    unit_text = unitgen.generate_system_unit(
        engine_venv=engine_symlink,
        read_write_paths=(data_root, cache_root),
        read_only_paths=(
            fhs.resolve_system_path(manifest.entry("code_root"), resolved_prefix),
            fhs.system_static_root(resolved_prefix),
        ),
        optional_read_write_paths=(
            fhs.resolve_system_path(manifest.entry("log_root"), resolved_prefix),
        ),
    )
    write_file("system_unit", unit_path, unit_text)
    report.append(f"unit: {unit_path} (0644, generated — do not edit)")
    report.append(f"systemctl daemon-reload: {_systemctl_system(['daemon-reload'])}")

    # 9. Final state reconcile (statuses final; the exact-inverse source).
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
) -> dict[str, str]:
    """Materialize one venv revision dir (SY-08) and record it; returns
    the normalized freeze map of the (fresh or reused) revision.

    Idempotent: the current build's revision dir is reused when its
    done-marker exists; an incomplete one (interrupted install) is
    rebuilt from scratch — the installer owns its own revision trees."""
    entry = fhs.load_fhs_manifest().entry(role)
    healthy = (rev_dir / done_marker).exists()
    frozen: dict[str, str] = {}
    if healthy:
        os.chmod(rev_dir, entry.mode)
        report.append(f"venv revision (current): {rev_dir}")
    else:
        frozen = _materialize_venv(rev_dir, requirements, entry.mode)
        os.chmod(rev_dir, entry.mode)
        for line in requirements:
            dist, _, expected = line.partition("==")
            normalized = re.sub(r"[-_.]+", "-", dist).lower()
            actual = frozen.get(normalized)
            if actual != expected:
                raise SystemInstallError(
                    f"venv drift right after materialization ({rev_dir}): "
                    f"{dist}=={expected} expected, freeze reports {actual!r} — "
                    "fix: re-run `sudo vesma service install --system`"
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


def uninstall_system(prefix: Path | None = None) -> SystemUninstallResult:
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
            _refuse_foreign(artifact, path, recorded_paths)
        except SystemInstallError as exc:
            drift.append(str(exc))
    if drift:
        raise SystemInstallError(
            "install-state ↔ filesystem drift — refusing the exact inverse"
            f" ({len(drift)} problem(s)); first: {drift[0]} — inventory with"
            " `vesma doctor`, fix or adopt the drift, then re-run"
            " `sudo vesma service uninstall --system`"
        )

    # Phase 2 — mutate, reverse creation order.
    lines: list[str] = [
        "systemctl disable --now vesma.service: "
        + _systemctl_system(["disable", "--now", "vesma.service"])
    ]
    removed = 0
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


def _refuse_foreign(artifact: StateArtifact, path: Path, recorded: set[str]) -> None:
    """A strict dir must contain ONLY recorded artifacts before its
    rmdir/rmtree — foreign content = loud refusal (SY-09), never a
    silent skip."""
    if artifact.remove_mode != "strict-dir":
        return
    for child in sorted(path.iterdir()):
        if str(child) not in recorded:
            raise SystemInstallError(
                f"foreign content in the recorded tree: {child} (inside"
                f" {path}) is NOT in the install-state — refusing to remove"
                " the directory (fix: inventory with `vesma doctor`, remove"
                " or adopt the foreign file, then re-run"
                " `sudo vesma service uninstall --system`)"
            )


__all__ = [
    "STATE_SCHEMA",
    "StateArtifact",
    "SystemInstallError",
    "SystemInstallResult",
    "SystemUninstallResult",
    "install_system",
    "read_system_profile",
    "read_system_state",
    "uninstall_system",
]
