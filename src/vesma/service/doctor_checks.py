"""Doctor service-installation checks (layout v1 §3.10, DR-01…DR-13).

Read-only by contract: the doctor reports findings with severity
``OK/WARN/FAIL`` and a ready fix command (LY-14) but NEVER executes a
fix. Every check degrades honestly when the installation (or a piece of
it) is absent — an absent unit/socket is a status line, not an error by
itself (DR-08 semantics). The CLI surface (``vesma doctor service``)
renders the table; exit code 1 on any FAIL.

The control-socket probe here is deliberately minimal (connect + ``hello``
per specs/control-socket/v1 §4.2/4.4): the full client belongs to wave
W3 — replace the inline probe when that lands.
"""

from __future__ import annotations

import contextlib
import dataclasses
import getpass
import grp
import json
import os
import pwd
import re
import shlex
import socket
import stat
import subprocess  # nosec B404 - fixed-argv read-only probes (pip freeze, python -c)
import sys
import urllib.parse
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

from vesma.service import fhs, layout, unitgen
from vesma.service.install import engine_venv, parse_pin_line, read_lock
from vesma.service.manifest import CONFFILE_NEW_SUFFIX, ComponentManifest, load_installation
from vesma.service.system_install import (
    REVISION_ROLES,
    SystemInstallError,
    read_system_state,
    regenerate_system_unit,
    system_state_drift,
)

#: Free-space thresholds (DR-10; engine-owned numbers, documented).
FREE_SPACE_WARN_BYTES = 500 * 1024 * 1024
FREE_SPACE_FAIL_BYTES = 100 * 1024 * 1024

_JOURNALD_CONF = Path("/etc/systemd/journald.conf")
_SYSTEMD_RUN_DIR = Path("/run/systemd/system")
_VENV_REF_RE = re.compile(r"venvs/([a-z][a-z0-9-]*)/")


class Severity(StrEnum):
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclasses.dataclass(frozen=True)
class Finding:
    """One DR-check outcome: severity + detail + ready fix command."""

    check_id: str
    title: str
    severity: Severity
    detail: str
    fix_command: str | None = None


def _mode(path: Path) -> int | None:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


def _owner(path: Path) -> int | None:
    try:
        return path.stat().st_uid
    except OSError:
        return None


def _weakened(mode: int, expected: int) -> bool:
    """Group/other bits present beyond the expected mode (fail-closed class)."""
    return (mode & 0o077) & ~expected != 0


def _group_or_world_writable(mode: int) -> bool:
    """Group/world WRITE bits present (the layout §3.8 site-packages bar:
    «не миро- и не группо-записываемы» — readability is not a violation)."""
    return mode & 0o022 != 0


# ── DR-01: canonical dir/env-file rights ──────────────────────────────


def _dr01() -> Finding:
    dir_specs: list[tuple[Path, int]] = list(layout.canonical_dir_specs())
    problems: list[str] = []
    fixes: list[str] = []
    checked = 0
    hard = False  # weakened rights / wrong owner / non-0600 env file = FAIL
    for path, expected in dir_specs:
        mode = _mode(path)
        if mode is None:
            continue  # not created yet — install has not run; not a finding
        checked += 1
        if _weakened(mode, expected) or _owner(path) != os.geteuid():
            hard = True
            problems.append(f"{path}: mode {mode:04o} owner {_owner(path)}")
            fixes.append(f"chmod {expected:04o} {path} && chown {getpass.getuser()} {path}")
        elif mode != expected:
            problems.append(f"{path}: mode {mode:04o} (stricter than {expected:04o} — review)")
            fixes.append(f"chmod {expected:04o} {path}")
    env_dir = layout.env_dir()
    if env_dir.is_dir():
        for env_file in sorted(env_dir.glob("*.env")):
            mode = _mode(env_file)
            checked += 1
            if mode != 0o600 or _owner(env_file) != os.geteuid():
                hard = True  # fail-closed loader refuses to start on this (layout §3.5)
                problems.append(f"{env_file}: mode {mode:04o} (env files MUST be 0600)")
                fixes.append(f"chmod 600 {env_file}")
    if problems:
        severity = Severity.FAIL if hard else Severity.WARN
        return Finding(
            "DR-01",
            "rights/ownership of config/state/env roots",
            severity,
            f"{checked} path(s) checked; violation(s): " + "; ".join(problems),
            " ; ".join(fixes) if fixes else None,
        )
    return Finding(
        "DR-01",
        "rights/ownership of config/state/env roots",
        Severity.OK,
        f"{checked} path(s) checked — modes and ownership canonical",
    )


# ── DR-02: venv integrity (rights, owner, freeze vs lock) ─────────────
# Lock/freeze lines are parsed by install.parse_pin_line / install.read_lock
# (single shared parser — doctor and installer must parse locks identically).


def _pip_freeze(venv_dir: Path) -> dict[str, str] | None:
    # AWARENESS (DR-02 audit note): this probe EXECUTES <venv>/bin/python
    # for ANY directory under venvs/ — same-uid code, trusted by design
    # (the whole tree is install-owned, 0700, DR-01/DR-12 enforce it).
    python = venv_dir / "bin" / "python"
    if not python.exists():
        return None
    try:
        result = subprocess.run(  # nosec B603 - fixed argv, no shell
            [str(python), "-m", "pip", "freeze", "--disable-pip-version-check"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    frozen: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parsed = parse_pin_line(line)
        if parsed is not None:
            frozen[parsed[0]] = parsed[1]
    return frozen


def _dr02() -> Finding:
    venvs_root = layout.component_venvs_root()
    if not venvs_root.is_dir():
        return Finding("DR-02", "venv integrity", Severity.OK, "no component venvs (n/a)")
    problems: list[str] = []
    fixes: list[str] = []
    checked = 0
    for venv_dir in sorted(p for p in venvs_root.iterdir() if p.is_dir()):
        checked += 1
        mode = _mode(venv_dir)
        if mode is None or _weakened(mode, 0o700) or _owner(venv_dir) != os.geteuid():
            problems.append(f"{venv_dir}: mode/owner violation ({mode:04o})")
            fixes.append(f"chmod 700 {venv_dir}")
        site_packages = _site_packages(venv_dir)
        if site_packages is not None:
            sp_mode = _mode(site_packages)
            if sp_mode is not None and _group_or_world_writable(sp_mode):
                problems.append(
                    f"{site_packages}: group/world-WRITABLE site-packages "
                    "(supply-chain boundary, layout §3.8)"
                )
                fixes.append(f"chmod -R go-w {venv_dir}")
        lock = read_lock(layout.data_dir(venv_dir.name) / "requirements-lock.txt")
        frozen = _pip_freeze(venv_dir)
        if lock is None:
            problems.append(f"{venv_dir}: lock file missing (requirements-lock.txt)")
            fixes.append("vesma service install")
        elif frozen is None:
            problems.append(f"{venv_dir}: pip freeze failed (broken venv?)")
            fixes.append("vesma service install")
        elif frozen != lock:
            drifted = sorted(set(frozen.items()) ^ set(lock.items()))
            problems.append(f"{venv_dir}: freeze drift vs lock — {drifted[:5]}")
            fixes.append("vesma service install")
    if problems:
        return Finding(
            "DR-02",
            "venv integrity (rights, owner, freeze vs lock)",
            Severity.FAIL,
            "; ".join(problems),
            " ; ".join(dict.fromkeys(fixes)),
        )
    return Finding(
        "DR-02",
        "venv integrity (rights, owner, freeze vs lock)",
        Severity.OK,
        f"{checked} venv(s) checked — rights, owner and lock match",
    )


def _site_packages(venv_dir: Path) -> Path | None:
    lib = venv_dir / "lib"
    if not lib.is_dir():
        return None
    for version_dir in sorted(lib.iterdir()):
        candidate = version_dir / "site-packages"
        if candidate.is_dir():
            return candidate
    return None


# ── DR-03: user-site leak test in a clean environment ─────────────────


def _dr03() -> Finding:
    """Spawn a clean-env python WITH ``PYTHONNOUSERSITE=1`` (the exact
    mechanism the supervisor injects unconditionally, layout §3.8) and
    require the user site dir to stay out of ``sys.path``."""
    probe = (
        "import json,site,sys;"
        "print(json.dumps({'user': site.getusersitepackages(), 'path': sys.path}))"
    )
    env = {"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~"), "PYTHONNOUSERSITE": "1"}
    try:
        result = subprocess.run(  # nosec B603 - fixed argv, no shell
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Finding(
            "DR-03",
            "user-site leak (clean-env import test)",
            Severity.WARN,
            f"probe could not run: {exc}",
        )
    if result.returncode != 0:
        return Finding(
            "DR-03",
            "user-site leak (clean-env import test)",
            Severity.WARN,
            f"probe failed: {result.stderr.strip()[:200]}",
        )
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    user_site = payload["user"]
    if any(Path(p) == Path(user_site) for p in payload["path"] if p):
        return Finding(
            "DR-03",
            "user-site leak (clean-env import test)",
            Severity.FAIL,
            f"user site {user_site} leaked into sys.path under PYTHONNOUSERSITE=1",
            f"check the python build at {sys.executable} (user-site must be "
            f"excluded when PYTHONNOUSERSITE=1)",
        )
    return Finding(
        "DR-03",
        "user-site leak (clean-env import test)",
        Severity.OK,
        f"clean-env import test passed (user site {user_site} absent from sys.path)",
    )


# ── DR-04: two manifests on one venv ──────────────────────────────────


def _dr04(installation: dict[str, ComponentManifest] | None) -> Finding:
    if not installation:
        return Finding(
            "DR-04", "venv uniqueness across manifests", Severity.OK, "n/a — no installation"
        )
    problems: list[str] = []
    for manifest in installation.values():
        if manifest.launch is None:
            continue
        for position, arg in enumerate(manifest.launch.argv):
            for match in _VENV_REF_RE.finditer(arg):
                referenced = match.group(1)
                if referenced != manifest.name:
                    problems.append(
                        f"{manifest.name}: launch.argv[{position}] references "
                        f"venvs/{referenced}/ — a venv is owned by exactly one "
                        "python unit (layout §3.8)"
                    )
    if problems:
        return Finding(
            "DR-04",
            "venv uniqueness across manifests",
            Severity.FAIL,
            "; ".join(problems),
            "give each python child its own venvs/<name> (install flow does "
            "this; hand-written manifests must not cross-reference)",
        )
    return Finding(
        "DR-04",
        "venv uniqueness across manifests",
        Severity.OK,
        f"{len(installation)} manifest(s) — no cross-referenced venvs",
    )


# ── DR-05: python version constraint vs interpreter ───────────────────

_PYVER_RE = re.compile(r"^(>=|<=|==|>|<|\^|~)?(\d+)\.(\d+)(?:\.(\d+))?$")


def _constraint_ok(constraint: str, actual: tuple[int, int, int]) -> bool:
    """Evaluate one CM §3.6 python.version constraint (single clause)."""
    match = _PYVER_RE.match(constraint.strip())
    if match is None:
        return False
    op, major, minor, patch = (
        match.group(1) or ">=",
        int(match.group(2)),
        int(match.group(3)),
        int(match.group(4) or 0),
    )
    if op == "^":  # compatible: same major
        return actual[0] == major and actual >= (major, minor, patch)
    if op == "~":  # compatible: same major.minor
        return actual[:2] == (major, minor) and actual >= (major, minor, patch)
    return {
        ">=": actual >= (major, minor, patch),
        "<=": actual <= (major, minor, patch),
        "==": actual[:2] == (major, minor),
        ">": actual > (major, minor, patch),
        "<": actual < (major, minor, patch),
    }[op]


def _dr05(installation: dict[str, ComponentManifest] | None) -> Finding:
    if not installation:
        return Finding("DR-05", "python version constraints", Severity.OK, "n/a — no installation")
    problems: list[str] = []
    actual = sys.version_info[:3]
    for manifest in installation.values():
        if manifest.in_process is None or manifest.in_process.python_version is None:
            continue
        constraint = manifest.in_process.python_version
        if not _constraint_ok(constraint, actual):
            engine = ".".join(map(str, actual))
            problems.append(
                f"{manifest.name}: python.version {constraint!r} not satisfied "
                f"by the engine interpreter {engine}"
            )
    if problems:
        return Finding(
            "DR-05",
            "python version constraints",
            Severity.FAIL,
            "; ".join(problems),
            "rebuild the component venv with a matching interpreter or relax "
            "the manifest constraint (CM §3.6)",
        )
    return Finding(
        "DR-05",
        "python version constraints",
        Severity.OK,
        "all declared python.version constraints satisfied "
        "(CM v1 carries the constraint on in_process only; child venvs have "
        "no constraint field — n/a by schema)",
    )


# ── DR-06: venv ≠ engine venv; reserved names ─────────────────────────


def _dr06(installation: dict[str, ComponentManifest] | None) -> Finding:
    """A component venv must never BE (nor symlink to, nor share
    site-packages with) the installation's ENGINE venv (layout §3.2
    ``~/.local/share/vesma/venv``; §3.8 supply-chain boundary).

    Identity is judged by venv ROOTS and resolved site-packages — NOT by
    interpreter binary resolution (#501): every venv of the same base
    CPython resolves ``bin/python`` to the same base binary, so comparing
    against the doctor's own interpreter false-FAILed healthy installs.
    When the engine venv is absent from the machine (dev/self-hosted
    reality) the identity leg is n/a; the reserved-names part stays
    enforced regardless. A symlinked ``venvs/<name>`` is a FAIL on its
    own, engine present or not.
    """
    engine_venv = layout.engine_venv_dir()
    engine_present = engine_venv.exists()
    engine_real = Path(os.path.realpath(engine_venv))
    engine_site = _site_packages(engine_venv) if engine_present else None
    engine_site_real = Path(os.path.realpath(engine_site)) if engine_site is not None else None
    venvs_root = layout.component_venvs_root()
    problems: list[str] = []
    fixes: list[str] = []
    checked = 0
    if venvs_root.is_dir():
        for venv_dir in sorted(venvs_root.iterdir()):
            checked += 1
            real = Path(os.path.realpath(venv_dir))
            if venv_dir.is_symlink():
                problems.append(f"{venv_dir} is a symlink -> {real}")
                fixes.append("remove the symlink and reinstall: vesma service install")
            elif engine_present and (
                real == engine_real or engine_real in real.parents or real in engine_real.parents
            ):
                problems.append(
                    f"{venv_dir} resolves inside the engine venv {engine_venv} "
                    "(DR-06: a component venv must never be the engine venv)"
                )
                fixes.append(
                    f"remove the colliding tree {venv_dir} and reinstall: vesma service install"
                )
            elif engine_site_real is not None:
                site = _site_packages(venv_dir)
                if site is not None and Path(os.path.realpath(site)) == engine_site_real:
                    problems.append(
                        f"{venv_dir} shares the engine venv site-packages ({site}) — "
                        "supply-chain boundary (layout §3.8)"
                    )
                    fixes.append("rebuild the component venv from scratch: vesma service install")
    components_dir = layout.components_dir()
    if components_dir.is_dir():
        for reserved in sorted(layout.RESERVED_TREE_NAMES):
            if (components_dir / f"{reserved}.yaml").exists():
                problems.append(f"reserved component name installed: {reserved}")
                fixes.append(f"remove {components_dir}/{reserved}.yaml")
    if problems:
        return Finding(
            "DR-06",
            "venv != engine venv; reserved names",
            Severity.FAIL,
            "; ".join(problems),
            " ; ".join(dict.fromkeys(fixes)),
        )
    detail = (
        f"{checked} venv(s) disjoint from the engine venv; no reserved names"
        if engine_present
        else f"{checked} venv(s) checked — engine venv not present (identity leg n/a); "
        "no reserved names"
    )
    return Finding("DR-06", "venv != engine venv; reserved names", Severity.OK, detail)


# ── DR-07: installed unit vs regeneration ─────────────────────────────


def _read_unit() -> tuple[Path, str] | None:
    unit_path = _installed_unit_path()
    try:
        return unit_path, unit_path.read_text(encoding="utf-8")
    except OSError:
        return None


def _installed_unit_path() -> Path:
    return layout.installed_unit_path()


def _regenerate_unit(downgraded: list[str]) -> str:
    return unitgen.generate(
        home=layout.home(),
        engine_venv=engine_venv(),
        read_write_paths=layout.unit_rw_paths(),
        read_only_paths=layout.unit_ro_paths(),
        downgraded=downgraded,
    )


def _dr07() -> Finding:
    unit = _read_unit()
    if unit is None:
        return Finding(
            "DR-07",
            "unit drift (installed vs regenerated)",
            Severity.OK,
            f"no unit installed at {_installed_unit_path()} (n/a — run `vesma service install`)",
        )
    unit_path, text = unit
    downgraded = unitgen.parse_downgrade_marker(text)
    regenerated = _regenerate_unit(downgraded)
    if regenerated != text:
        return Finding(
            "DR-07",
            "unit drift (installed vs regenerated)",
            Severity.FAIL,
            f"{unit_path} differs from the regenerated unit (hand edit or "
            "generator upgrade) — note: run `vesma doctor` from the engine "
            "venv, a dev-checkout interpreter venv will not match",
            "vesma service install",
        )
    return Finding(
        "DR-07",
        "unit drift (installed vs regenerated)",
        Severity.OK,
        f"{unit_path} matches the regeneration byte-for-byte",
    )


# ── DR-08: control-socket liveness (connect-probe + hello) ────────────


def _dr08() -> Finding:
    runtime = layout.resolve_runtime_dir()
    sock_path = runtime.path / "control.sock"
    if not sock_path.exists():
        return Finding(
            "DR-08",
            "control socket liveness",
            Severity.OK,
            f"no control socket at {sock_path} — supervisor not running (n/a, "
            "not an error by itself)",
        )
    request = json.dumps({"id": 1, "method": "hello", "params": {"protocol_version": 1}})
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2.0)
            sock.connect(str(sock_path))
            sock.sendall((request + "\n").encode("utf-8"))
            reply = sock.recv(4096).decode("utf-8", errors="replace").strip()
    except OSError as exc:
        return Finding(
            "DR-08",
            "control socket liveness",
            Severity.OK,
            f"socket present at {sock_path} but the probe failed ({exc}) — "
            "stale socket or stopped supervisor (status, not an error)",
            f"stale socket: remove {sock_path} after confirming the supervisor is stopped",
        )
    try:
        payload = json.loads(reply.splitlines()[0])
        version_ok = isinstance(payload, dict) and (
            "result" in payload or payload.get("error") is None
        )
    except (json.JSONDecodeError, IndexError):
        # A foreign process owning the socket is NOT fine — it is a live
        # warning (stale/absent stays a status per contract; a talkative
        # foreign listener is something the operator must look at).
        return Finding(
            "DR-08",
            "control socket liveness",
            Severity.WARN,
            f"socket at {sock_path} answered non-JSON — not a vesma "
            f"supervisor? reply: {reply[:120]!r}",
            f"confirm what owns {sock_path}, then remove the foreign "
            "socket and start the supervisor: vesma service run",
        )
    if version_ok:
        return Finding(
            "DR-08",
            "control socket liveness",
            Severity.OK,
            f"live supervisor answered hello at {sock_path}",
        )
    return Finding(
        "DR-08",
        "control socket liveness",
        Severity.WARN,
        f"socket at {sock_path} answered an error — protocol mismatch "
        f"(payload: {str(payload)[:120]})",
        "restart the supervisor so the socket speaks the installed "
        "protocol: systemctl --user restart vesma.service",
    )


# ── DR-09: health port collisions across the installation ────────────


def _dr09(installation: dict[str, ComponentManifest] | None) -> Finding:
    if not installation:
        return Finding("DR-09", "health port collisions", Severity.OK, "n/a — no installation")
    claims: dict[int, set[str]] = {}
    for manifest in installation.values():
        health = manifest.health
        if health is None:
            continue
        ports: list[int] = []
        if health.http is not None:
            parsed = urllib.parse.urlsplit(health.http.url)
            if parsed.port is not None:
                ports.append(parsed.port)
        if health.tcp is not None:
            ports.append(health.tcp.port)
        for port in ports:
            claims.setdefault(port, set()).add(manifest.name)
    collisions = {port: names for port, names in claims.items() if len(names) > 1}
    if collisions:
        detail = "; ".join(
            f"port {port}: {sorted(names)}" for port, names in sorted(collisions.items())
        )
        return Finding(
            "DR-09",
            "health port collisions",
            Severity.FAIL,
            detail,
            "change the health port of one of the components (manifest health.http/health.tcp)",
        )
    return Finding(
        "DR-09",
        "health port collisions",
        Severity.OK,
        f"{len(claims)} health port(s) claimed, no cross-manifest collisions",
    )


# ── DR-10: free space on canonical roots ──────────────────────────────


def _dr10() -> Finding:
    roots = list(layout.free_space_roots())
    runtime = layout.resolve_runtime_dir()
    roots.append(("runtime", runtime.path))
    problems: list[str] = []
    fixes: list[str] = []
    seen_devices: set[int] = set()
    for label, root in roots:
        probe = root
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent  # nearest existing ancestor owns the volume
        try:
            info = os.statvfs(probe)
        except OSError:
            continue
        device = os.stat(probe).st_dev
        if device in seen_devices:
            continue
        seen_devices.add(device)
        free_gb = info.f_bavail * info.f_frsize / (1024**3)
        if free_gb * (1024**3) < FREE_SPACE_FAIL_BYTES:
            problems.append(f"{label} volume ({probe}): {free_gb:.1f} GB free (< 100 MB)")
            fixes.append(f"free space on the volume of {probe}")
        elif free_gb * (1024**3) < FREE_SPACE_WARN_BYTES:
            problems.append(f"{label} volume ({probe}): {free_gb:.1f} GB free (< 500 MB)")
    if problems:
        cache = layout.cache_base()
        return Finding(
            "DR-10",
            "free space on canonical roots",
            Severity.FAIL if any("< 100 MB" in p for p in problems) else Severity.WARN,
            "; ".join(problems) + " (thresholds: WARN < 500 MB, FAIL < 100 MB)",
            f"the cache is regenerable — deleting {cache}/* is safe (layout §3.9); "
            "otherwise expand the volume",
        )
    return Finding(
        "DR-10",
        "free space on canonical roots",
        Severity.OK,
        "all canonical-root volumes above the WARN threshold (500 MB)",
    )


# ── DR-11: journald Storage=persistent ────────────────────────────────


def _dr11() -> Finding:
    if sys.platform != "linux":
        # Portability F7: on macOS/BSD there is no systemd and no journald —
        # the old path either warned about a journald that cannot exist or
        # reported a n/a systemd probe. Skip loudly instead of warning.
        return Finding(
            "DR-11",
            "journald Storage=persistent",
            Severity.OK,
            "systemd not detected on this platform — check skipped",
        )
    if not _SYSTEMD_RUN_DIR.exists():
        return Finding(
            "DR-11",
            "journald Storage=persistent",
            Severity.OK,
            "no systemd running on this machine (n/a)",
        )
    storage = "auto"
    try:
        for line in _JOURNALD_CONF.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^\s*Storage\s*=\s*(\S+)", line)
            if match:
                storage = match.group(1)
    except OSError:
        pass  # file absent → default 'auto'
    if storage == "persistent":
        return Finding(
            "DR-11",
            "journald Storage=persistent",
            Severity.OK,
            "journald keeps logs across reboots",
        )
    if storage == "auto" and Path("/var/log/journal").exists():
        return Finding(
            "DR-11",
            "journald Storage=persistent",
            Severity.OK,
            "Storage=auto with /var/log/journal present — persistent",
        )
    return Finding(
        "DR-11",
        "journald Storage=persistent",
        Severity.WARN,
        f"journald is volatile (Storage={storage}) — service logs are lost on reboot",
        "set Storage=persistent in /etc/systemd/journald.conf (or create "
        "/var/log/journal) and restart systemd-journald",
    )


# ── DR-12: venv/venvs read-only at runtime (unit coverage + fs perms) ─


def _dr12() -> Finding:
    unit = _read_unit()
    venv, venvs = layout.unit_ro_paths()
    problems: list[str] = []
    fixes: list[str] = []
    hard = False  # undocumented loss of coverage / fs-level weakening = FAIL
    for path in (venv, venvs):
        mode = _mode(path)
        if mode is not None and _weakened(mode, 0o700):
            hard = True  # writable-by-others venv trees = the DR-12 tamper vector
            problems.append(f"{path}: group/world-writable on disk ({mode:04o})")
            fixes.append(f"chmod 700 {path}")
    if unit is None:
        detail = "no unit installed (n/a)" + ("; " + "; ".join(problems) if problems else "")
        if problems:
            return Finding(
                "DR-12", "venv read-only at runtime", Severity.FAIL, detail, " ; ".join(fixes)
            )
        return Finding("DR-12", "venv read-only at runtime", Severity.OK, detail)
    _, text = unit
    downgraded = unitgen.parse_downgrade_marker(text)
    active, commented = unitgen.parse_unit(text)
    expected = {unitgen.render_path(venv, layout.home()), unitgen.render_path(venvs, layout.home())}
    if "ReadOnlyPaths" in downgraded:
        # The sanctioned loud container downgrade (SL §3.6): the protection
        # is REALLY absent here — WARN (documented downgrade), not FAIL, so
        # a legitimate container install is not permanently red. Any OTHER
        # loss of coverage stays FAIL.
        problems.append(
            "ReadOnlyPaths is container-downgraded — venvs are NOT runtime-"
            "protected in this environment (loud documented downgrade, "
            "specs/service-lifecycle/v1 §3.6)"
        )
        fixes.append(
            "vesma service install (on a systemd host this restores "
            "ReadOnlyPaths; inside a container the downgrade is documented)"
        )
    elif "ReadOnlyPaths" in commented:
        hard = True
        problems.append("ReadOnlyPaths is commented out WITHOUT a downgrade marker (tampering?)")
        fixes.append("vesma service install")
    else:
        # shlex, not str.split: the generator double-quotes rendered paths
        # that contain spaces (systemd parses the value with shell-like
        # quoting) — the coverage comparison must undo exactly that.
        covered = set(shlex.split(active.get("ReadOnlyPaths") or ""))
        missing = expected - covered
        if missing:
            hard = True
            problems.append("unit ReadOnlyPaths does not cover: " + ", ".join(sorted(missing)))
            fixes.append("vesma service install")
    if problems:
        return Finding(
            "DR-12",
            "venv read-only at runtime",
            Severity.FAIL if hard else Severity.WARN,
            "; ".join(problems),
            " ; ".join(dict.fromkeys(fixes)) if fixes else None,
        )
    return Finding(
        "DR-12",
        "venv read-only at runtime",
        Severity.OK,
        "unit ReadOnlyPaths covers the venv trees; on-disk modes clean",
    )


# ── DR-13: container downgrades stayed inside the allowlist ──────────

_HARDENING_DIRECTIVES = (
    "NoNewPrivileges",
    "ProtectSystem",
    "ReadWritePaths",
    "ReadOnlyPaths",
    "ProtectHome",
    "PrivateTmp",
    "PrivateDevices",
    "ProtectKernelTunables",
    "ProtectKernelModules",
    "ProtectKernelLogs",
    "ProtectControlGroups",
    "RestrictSUIDSGID",
    "LockPersonality",
    "RestrictRealtime",
    "CapabilityBoundingSet",
    "RestrictAddressFamilies",
    "SystemCallFilter",
)


def _dr13() -> Finding:
    unit = _read_unit()
    if unit is None:
        return Finding(
            "DR-13", "container downgrade allowlist", Severity.OK, "no unit installed (n/a)"
        )
    _, text = unit
    downgraded = unitgen.parse_downgrade_marker(text)
    illegal = [n for n in downgraded if n not in unitgen.DOWNGRADE_ALLOWED]
    _, commented = unitgen.parse_unit(text)
    silent = [
        n
        for n in commented
        if n in _HARDENING_DIRECTIVES
        and n not in downgraded
        and n != "SystemCallFilter"  # Tier B: always commented in v1, by design
    ]
    problems: list[str] = []
    if illegal:
        problems.append(f"downgrade marker names outside the filesystem allowlist: {illegal}")
    if silent:
        problems.append(
            f"hardening directive(s) commented out without a downgrade marker: {silent}"
        )
    if problems:
        return Finding(
            "DR-13",
            "container downgrade allowlist",
            Severity.FAIL,
            "; ".join(problems) + f" (allowlist: {sorted(unitgen.DOWNGRADE_ALLOWED)})",
            "vesma service install",
        )
    if downgraded:
        return Finding(
            "DR-13",
            "container downgrade allowlist",
            Severity.OK,
            f"container downgrades inside the allowlist: {downgraded}",
        )
    return Finding(
        "DR-13",
        "container downgrade allowlist",
        Severity.OK,
        "no downgrades; full hardening block active",
    )


# ── DR-17: install-state ↔ filesystem — user generation mixing + the
# system-profile inversion leg (specs/layout/v2 §3.3/§3.14; the system
# leg shares the updater preflight's predicate verbatim —
# system_install.system_state_drift — so diagnosis and refusal can never
# disagree) ────────────────────────────────────────────────────────────────

_SEVERITY_RANK: dict[Severity, int] = {Severity.FAIL: 0, Severity.WARN: 1, Severity.OK: 2}


def _dr17() -> Finding:
    """Two legs, worst severity wins:

    - user plane: a tree mixing v1.5 and v2 service artifacts is a FAIL
      (§3.3 MUST NOT — the engine never writes the other generation's
      paths; resolved by the consolidation mover, not by hand);
    - system plane: recorded-but-missing artifacts, type drift and
      unrecorded content in strict dirs (conffile zone exempt) — the
      exact predicate the updater refuses on, rendered for diagnosis.
    """
    title = "install-state ↔ filesystem (generations + system inversion)"
    # User plane (generation mixing).
    legacy_roots = list(layout.legacy_service_artifacts())
    v2_dirs = [p for p in layout.v2_only_service_dirs() if p.exists()]
    if legacy_roots and v2_dirs:
        user_finding = Finding(
            "DR-17",
            title,
            Severity.FAIL,
            "MIXED generations: v1.5 plane "
            f"{[str(p) for p in legacy_roots]} AND v2 plane "
            f"{[str(p) for p in v2_dirs]} — the engine never writes both; "
            "resolve before anything else touches the stack",
            "consolidation (ADR-0045 rollout wave 4 mover); until then keep "
            "the planes untouched — do NOT hand-delete either side",
        )
    elif legacy_roots or v2_dirs:
        generation = "v1.5 (legacy XDG spread)" if legacy_roots else "v2 (self-contained ~/.vesma)"
        user_finding = Finding("DR-17", title, Severity.OK, f"single user plane: {generation}")
    else:
        user_finding = Finding("DR-17", title, Severity.OK, "no user service stack (n/a)")

    # System plane (state ↔ filesystem, the updater's preflight predicate).
    try:
        state = read_system_state()
    except SystemInstallError as exc:
        system_finding = Finding(
            "DR-17",
            title,
            Severity.WARN,
            f"the system install-state is unreadable: {exc}",
            "vesma doctor  (inventory the broken install-state)",
        )
    else:
        if state is None:
            system_finding = Finding(
                "DR-17", title, Severity.OK, "no system-profile installation on this machine"
            )
        else:
            drift = system_state_drift()
            if drift:
                system_finding = Finding(
                    "DR-17",
                    title,
                    Severity.FAIL,
                    f"system profile state ↔ filesystem drift ({len(drift)}"
                    f" problem(s)); first: {drift[0]} — the updater and the"
                    " exact-inverse uninstall both refuse on this",
                    "inventory with `vesma doctor`, adopt or fix the drift, then"
                    " re-run; do NOT hand-edit the install-state",
                )
            else:
                system_finding = Finding(
                    "DR-17", title, Severity.OK, "system profile: state ↔ filesystem consistent"
                )

    worst = min((user_finding, system_finding), key=lambda f: _SEVERITY_RANK[f.severity])
    if worst.severity is Severity.OK:
        return Finding(
            "DR-17",
            title,
            Severity.OK,
            f"{user_finding.detail}; {system_finding.detail}",
        )
    details = [f.detail for f in (user_finding, system_finding) if f.severity == worst.severity]
    fix = worst.fix_command or next(
        (f.fix_command for f in (user_finding, system_finding) if f.fix_command), None
    )
    return Finding("DR-17", title, worst.severity, "; ".join(details), fix)


# ── DR-19: layout generation advisories (specs/layout/v2 §3.3/§3.14) ──


def _dr19() -> Finding:
    """Generation advisory, both profiles (§3.3 SHOULD; non-fatal by
    contract — WARN, never FAIL):

    - user plane: v1.5 → WARN with the consolidation pointer; an
      UNSTAMPED v2 tree → WARN (§3.1 MUST: fixed at install time); v2 → OK;
    - system plane: the install-state's ``generation:`` key must say
      ``v2`` — missing/unknown → WARN (re-stamp with a re-install).
    """
    title = "layout generation advisories"
    stamped = layout.read_generation_stamp()
    generation = layout.detect_generation()
    if generation is layout.LayoutGeneration.V1_5:
        user_sev, user_detail, user_fix = (
            Severity.WARN,
            "generation v1.5 (legacy XDG spread) — keeps resolving per "
            "layout v1; consolidation to the self-contained ~/.vesma form "
            "is an explicit operator step (specs/layout/v2 §9.2)",
            "consolidation mover lands with ADR-0045 rollout wave 4 — "
            "re-run `vesma doctor` after it ships; until then no action",
        )
    else:
        v2_present = any(p.exists() for p in layout.v2_only_service_dirs())
        if not v2_present and stamped is None:
            user_sev, user_detail, user_fix = (
                Severity.OK,
                "no user service stack installed — first install creates generation v2",
                None,
            )
        elif stamped is None:
            user_sev, user_detail, user_fix = (
                Severity.WARN,
                f"v2 tree without a generation stamp ({layout.install_state_path()} "
                "absent or unparsable) — §3.1: the generation MUST be fixed at "
                "install time",
                "vesma service install",
            )
        else:
            user_sev, user_detail, user_fix = (
                Severity.OK,
                "generation v2 (self-contained ~/.vesma), stamped at "
                f"{layout.install_state_path()}",
                None,
            )

    try:
        state = read_system_state()
    except SystemInstallError:
        state = None
    system_notes: list[str] = []
    system_warn = False
    if state is not None:
        system_generation = state.get("generation")
        if system_generation == "v2":
            system_notes.append("system profile: generation v2")
        else:
            system_warn = True
            system_notes.append(
                f"system profile: generation stamp missing/unknown"
                f" (got {system_generation!r}) — §3.1: stamped at install"
            )

    severity = Severity.WARN if (user_sev is Severity.WARN or system_warn) else Severity.OK
    detail = user_detail + ("; " + "; ".join(system_notes) if system_notes else "")
    fix = user_fix or (
        "sudo vesma system install (re-stamps the system profile)" if system_warn else None
    )
    return Finding("DR-19", title, severity, detail, fix)


# ── DR-20: second writer on the store home (ADR-0049 Lock 1, wave 0) ─────────


def _supervisor_alive() -> bool:
    """True when the user-profile supervisor answers a control hello (the
    DR-08 liveness predicate, extracted for the writer-lock cross-check;
    a stale socket, a probe failure and an absent socket all read NOT
    alive — DR-08 carries the finer statuses)."""
    sock_path = layout.resolve_runtime_dir().path / "control.sock"
    if not sock_path.exists():
        return False
    request = json.dumps({"id": 1, "method": "hello", "params": {"protocol_version": 1}})
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2.0)
            sock.connect(str(sock_path))
            sock.sendall((request + "\n").encode("utf-8"))
            sock.recv(4096)
    except OSError:
        return False
    return True


def _user_store_home() -> Path:
    """The USER profile's store home, resolved leniently (env + defaults,
    no config-file demand) — the same resolution the instance registry's
    wave-0 writer uses, so the probed home and the recorded row agree."""
    from vesma.config import Settings

    settings = Settings()
    settings.resolve_paths()
    return settings.vesma.data_dir


def _dr20() -> Finding:
    """Second writer on the store home (ADR-0049 wave 0).

    The per-home writer lock's LIVE verdict against the supervisor's
    liveness — the flock is the authority, the holder file is the
    attribution:

    - lock held + supervisor alive → the DESIGNED state (the supervisor
      IS the exclusive holder) → OK, holder named;
    - lock held + NO supervisor → the DR-20 hazard class: a writer-of-
      record nobody supervises (a mover transit in flight, or a foreign /
      second supervisor) → FAIL with the holder's attribution;
    - supervisor alive + lock FREE → the single-writer invariant is not
      enforced on this machine (a pre-wave-0 build?) → WARN;
    - nothing holds, nothing runs → OK.

    Advisory context: the instance registry's row for this home joins the
    detail (registry problems never flip the severity — advisory by
    contract).
    """
    title = "second writer on the store home"
    try:
        store_home = _user_store_home()
    except Exception as exc:  # doctor reports, never crashes
        return Finding("DR-20", title, Severity.WARN, f"store home unresolved: {exc}")
    from vesma.service.home_lock import probe_writer_lock

    held, holder = probe_writer_lock(store_home)
    alive = _supervisor_alive()

    registry_note = ""
    # advisory context only — a missing/corrupt registry never fails DR-20
    with contextlib.suppress(Exception):
        from vesma.service.instance_registry import load_registry

        state = load_registry()
        rows = [row for row in state.entries if str(store_home) == row.home]
        if rows:
            row = rows[0]
            registry_note = (
                f"; registry: profile={row.profile} endpoint={row.endpoint!r} "
                f"unit={row.unit!r} instance_id={row.instance_id!r}"
            )
        elif state.problem is not None:
            registry_note = f"; {state.problem}"

    if held:
        holder_desc = holder.describe() if holder is not None else "payload unreadable"
        if alive:
            return Finding(
                "DR-20",
                title,
                Severity.OK,
                f"the supervisor holds the home writer lock "
                f"({store_home}): {holder_desc}{registry_note}",
            )
        return Finding(
            "DR-20",
            title,
            Severity.FAIL,
            f"a writer-of-record holds {store_home} while NO supervisor is "
            f"running — a mover transit in flight, or a foreign/second "
            f"supervisor (the two-writers hazard): {holder_desc}{registry_note}",
            "identify the holder from the attribution above; if it is a "
            "mover, let it finish; otherwise stop it, then re-run `vesma "
            "doctor`",
        )
    if alive:
        return Finding(
            "DR-20",
            title,
            Severity.WARN,
            f"the supervisor runs WITHOUT the home writer lock ({store_home}) "
            "— the single-writer invariant is not enforced on this machine "
            f"(a pre-wave-0 build?){registry_note}",
            "restart under the current build: vesma service stop && vesma service start",
        )
    return Finding(
        "DR-20",
        title,
        Severity.OK,
        f"no writer-of-record holds the home ({store_home}) and no "
        f"supervisor is running{registry_note}",
    )


# ── DR-14: profile isolation — user ↔ system roots + stamp cross-checks
# (specs/layout/v2 §3.1; the data-root minimal leg landed with rollout 2,
# the config-root overlap and the stamp cross-checks complete the matrix
# here — the doctor wave, ADR-0045 rollout 3) ─────────────────────────────


def _roots_overlap(a: Path, b: Path) -> bool:
    """True when one root IS the other or contains it (either direction)."""
    return a == b or a in b.parents or b in a.parents


_STAMP_PROFILE_RE = re.compile(r"^\s*profile:\s*(\S+)\s*$", re.MULTILINE)


def _dr14() -> Finding:
    """Profile isolation, full matrix (§3.1; «ERROR» verdict class):

    - user- and system-profile data roots collide → FAIL;
    - the user profile root(s) resolve inside the system CONFIG root →
      FAIL (the config plane is the other trust boundary);
    - stamp cross-checks: the system install-state must say
      ``profile: system``; the user stamp must never claim the system
      profile (a cross-contaminated stamp corrupts every resolver that
      trusts it) → FAIL.

    Read-only; the system side is detected by its install-state.
    """
    title = "profile isolation (user ↔ system roots + stamps)"
    try:
        state = read_system_state()
    except SystemInstallError as exc:
        return Finding(
            "DR-14",
            title,
            Severity.WARN,
            f"the system install-state is unreadable: {exc}",
            "vesma doctor  (inventory the broken install-state)",
        )
    if state is None:
        return Finding(
            "DR-14",
            title,
            Severity.OK,
            "no system-profile installation on this machine",
        )
    system_data_root = fhs.system_data_root()
    system_config_root = fhs.system_config_root()
    user_data_roots = (layout.vesma_home(), layout.data_root())
    # USER-plane roots, deliberately NOT the profile-dispatched resolvers
    # (components_dir/env_dir): on a system-stamped machine those resolve
    # the FHS slots — comparing them against the system config root would
    # self-collide. config_root() is user-plane by contract (layout v2 §3.2).
    user_config_roots = (
        layout.config_root(),
        layout.vesma_home(),
    )
    problems: list[str] = []
    data_collisions = [str(r) for r in user_data_roots if _roots_overlap(r, system_data_root)]
    if data_collisions:
        problems.append(
            f"user profile data root(s) {data_collisions} collide with the"
            f" system data root {system_data_root} — the profiles MUST have"
            " distinct data roots (§3.1, the «ERROR» class)"
        )
    config_collisions = [str(r) for r in user_config_roots if _roots_overlap(r, system_config_root)]
    if config_collisions:
        problems.append(
            f"user profile root(s) {config_collisions} collide with the"
            f" system config root {system_config_root} (§3.1 isolation)"
        )
    profile = state.get("profile")
    if profile != "system":
        problems.append(
            f"the system install-state says profile={profile!r}, expected"
            " 'system' — the state is the trust anchor for every system"
            " surface"
        )
    try:
        user_stamp_text = layout.install_state_path().read_text(encoding="utf-8")
    except OSError:
        user_stamp_text = ""
    claimed = _STAMP_PROFILE_RE.search(user_stamp_text)
    if claimed is not None and claimed.group(1) == "system":
        problems.append(
            "the USER install-state claims profile: system — a"
            " cross-contaminated stamp (every user-surface resolver that"
            " trusts it is corrupted)"
        )
    if problems:
        return Finding(
            "DR-14",
            title,
            Severity.FAIL,
            "; ".join(problems),
            "move the user profile off the system roots and restore the"
            " stamps (fix the state files by hand — they are trust"
            " anchors —, then re-run `vesma doctor`)",
        )
    return Finding(
        "DR-14",
        title,
        Severity.OK,
        f"profiles coexist with disjoint roots (system data root:"
        f" {system_data_root}; stamps consistent)",
    )


# ── DR-15: system-profile rights vs the FHS manifest (the fixed-table
# leg landed with rollout 2; the doctor wave completes it: per-component
# template slots, the revision trees, the env.d 0600 FAIL gate and the
# conffile zone — any env.d mode drift is a FAIL, ADR-0045 Decision 4
# footnote 1) ──────────────────────────────────────────────────────────────


def _dr15() -> Finding:
    """Walk the system tree against the manifest (modes + owners).

    Degrades honestly when unreadable (the doctor typically runs as a
    plain user; ``/etc/vesma`` is ``0750 root:vesma``): an unreadable
    entry is a WARN naming the privilege needed — never a silent OK.

    Two problem classes: HARD (weakened modes, ownership drift, any
    env.d drift — trust boundaries) → FAIL; SOFT (a stricter-than-manifest
    mode on operator conffile-zone content — safe, review-worthy) → WARN
    when nothing hard was found. env.d secrets are HARD on ANY drift:
    the normative invariant is exactly ``0600`` (fail-closed loader,
    supervisor-only readable). File CONTENTS are never read or logged
    here — modes/owners only.
    """
    title = "system-profile rights vs FHS manifest"
    try:
        state = read_system_state()
    except SystemInstallError as exc:
        return Finding(
            "DR-15",
            title,
            Severity.WARN,
            f"the system install-state is unreadable: {exc}",
            "vesma doctor  (inventory the broken install-state)",
        )
    if state is None:
        return Finding(
            "DR-15",
            title,
            Severity.OK,
            "no system-profile installation on this machine",
        )
    manifest = fhs.load_fhs_manifest()
    hard: list[str] = []
    soft: list[str] = []
    unreadable: list[str] = []
    checked = 0

    def audit(
        path: Path,
        entry: fhs.FhsEntry,
        *,
        exact_mode: bool,
        label: str,
    ) -> None:
        """Mode+owner audit of one concrete path against one entry.

        ``exact_mode`` (machine-owned content): any mode drift is hard.
        Otherwise (operator conffile zone): weakened mode is hard, a
        stricter mode is soft (safe — review). Ownership drift is always
        hard.
        """
        nonlocal checked
        try:
            info = path.stat()
        except FileNotFoundError:
            return  # optional slot (opt-in log root, unseeded conffile)
        except PermissionError:
            unreadable.append(str(path))
            return
        except OSError as exc:
            unreadable.append(f"{path} ({exc})")
            return
        checked += 1
        mode = stat.S_IMODE(info.st_mode)
        if mode != entry.mode:
            weakened = (mode & 0o077) & ~entry.mode != 0
            complaint = (
                f"{path} ({label}): mode {format(mode, '04o')}, manifest"
                f" {format(entry.mode, '04o')}"
            )
            if exact_mode or weakened:
                hard.append(complaint)
            else:
                soft.append(complaint + " (stricter than the manifest — review)")
        owner_name = _uid_to_name(info.st_uid)
        group_name = _gid_to_name(info.st_gid)
        if (
            entry.owner is not None
            and entry.group is not None
            and (owner_name != entry.owner or group_name != entry.group)
        ):
            hard.append(
                f"{path} ({label}): owner {owner_name}:{group_name}, manifest"
                f" {entry.owner}:{entry.group}"
            )

    for entry in manifest.entries:
        if "<name>" in entry.system_path or "<rev>" in entry.system_path:
            continue  # per-component slots are walked below
        if entry.owner is None or entry.group is None:
            continue  # /run/vesma — runtime ownership by the unit
        audit(
            fhs.resolve_system_path(entry),
            entry,
            exact_mode=True,
            label=entry.role,
        )

    # Concrete per-component slots: names come from the install-state's
    # component_data rows plus whatever manifests sit in the drop-in dir.
    raw_artifacts = state.get("artifacts")
    state_rows: list[Any] = list(raw_artifacts) if isinstance(raw_artifacts, list) else []
    names: set[str] = set()
    revision_dirs: list[Path] = []
    for raw in state_rows:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role"))
        if role == "component_data":
            names.add(Path(str(raw["path"])).name)
        elif role in REVISION_ROLES:  # one source with the updater (cascade fix)
            revision_dirs.append(Path(str(raw["path"])))
    components_dir = fhs.system_components_dir()
    if components_dir.is_dir():
        names.update(p.stem for p in components_dir.glob("*.yaml"))
    manifest_entry = manifest.entry("component_manifest")
    for name in sorted(names):
        manifest_file = components_dir / f"{name}.yaml"
        if manifest_file.is_file():
            audit(manifest_file, manifest_entry, exact_mode=False, label="component conffile")
        try:
            data_dir = fhs.system_component_data_dir(name)
            venv_link = fhs.system_venv_component(name)
        except ValueError:
            continue  # operator filename outside the component-name grammar
        audit(data_dir, manifest.entry("component_data"), exact_mode=True, label="component_data")
        audit(venv_link, manifest.entry("venv_component"), exact_mode=True, label="venv_component")
    venvs_root = fhs.system_venvs_root()
    for rev_dir in sorted(revision_dirs):
        role = "venv_component_revision" if rev_dir.parent == venvs_root else "venv_engine_revision"
        audit(rev_dir, manifest.entry(role), exact_mode=True, label=role)

    # env.d secrets: the 0600 FAIL gate (ADR-0045 Decision 4¹) — operator
    # files, walked from the directory, never through the state.
    env_entry = manifest.entry("env_secrets")
    env_dir_path = fhs.system_env_dir()
    if env_dir_path.is_dir():
        for env_file in sorted(env_dir_path.glob("*.env")):
            try:
                info = env_file.stat()
            except PermissionError:
                unreadable.append(str(env_file))
                continue
            except OSError as exc:
                unreadable.append(f"{env_file} ({exc})")
                continue
            checked += 1
            mode = stat.S_IMODE(info.st_mode)
            if mode != 0o600:
                hard.append(
                    f"{env_file}: mode {format(mode, '04o')} (env.d files MUST"
                    " be exactly 0600 — ANY drift is the FAIL gate)"
                )
            owner_name = _uid_to_name(info.st_uid)
            group_name = _gid_to_name(info.st_gid)
            if owner_name != env_entry.owner or group_name != env_entry.group:
                hard.append(
                    f"{env_file}: owner {owner_name}:{group_name}, manifest"
                    f" {env_entry.owner}:{env_entry.group}"
                )

    # Pending conffile defaults (SY-05 .rpmnew) audit like their manifests.
    if components_dir.is_dir():
        for pending in sorted(components_dir.glob(f"*{CONFFILE_NEW_SUFFIX}")):
            audit(pending, manifest_entry, exact_mode=False, label="conffile .rpmnew")

    problems = hard + soft
    if problems:
        severity = Severity.FAIL if hard else Severity.WARN
        # The fix command promises "vesma doctor lists each path" — the
        # detail carries the FULL list (capped only against a wildly
        # drifted tree producing an endless cell).
        cap = 30
        listed = "; ".join(problems[:cap])
        if len(problems) > cap:
            listed += f"; … {len(problems) - cap} further drift(s) suppressed"
        return Finding(
            "DR-15",
            title,
            severity,
            f"{len(problems)} drift(s) vs the FHS manifest: {listed}"
            + (f"; also unreadable: {unreadable}" if unreadable else ""),
            "sudo chmod/chown per the FHS manifest"
            " (`vesma doctor` lists each path; the manifest is the source"
            " of truth)",
        )
    if unreadable:
        return Finding(
            "DR-15",
            title,
            Severity.WARN,
            f"{checked} entr(ies) verified OK; {len(unreadable)} unreadable"
            f" without privileges: {unreadable[:3]} — re-run as root for a"
            " full walk",
            "sudo vesma doctor service",
        )
    return Finding(
        "DR-15",
        title,
        Severity.OK,
        f"{checked} entr(ies) match the FHS manifest (modes + owners)",
    )


# ── DR-16: symlink-revision integrity under the manifest code plane
# (layout v2 §3.10; ADR-0045 Decision 6 — the doctor wave, rollout 3;
# ADR-0048: the title renders the manifest-resolved code root —
# per-lane shipped manifests may name different /usr forms) ───────────────


def _dr16() -> Finding:
    """Every stable venv slot must be a symlink whose target exists and
    resolves INSIDE its manifest root (a link that escapes the revision
    tree is a path-traversal vector, not just drift); revision dirs no
    symlink points at are leftover updates (post-rollback diagnosis
    state) → WARN, never silent.
    """
    try:
        state = read_system_state()
    except SystemInstallError as exc:
        return Finding(
            "DR-16",
            "symlink-revision integrity",
            Severity.WARN,
            f"the system install-state is unreadable: {exc}",
            "vesma doctor  (inventory the broken install-state)",
        )
    if state is None:
        return Finding(
            "DR-16",
            "symlink-revision integrity",
            Severity.OK,
            "no system-profile installation on this machine",
        )
    manifest = fhs.load_fhs_manifest()
    code_root = fhs.resolve_system_path(manifest.entry("code_root"))
    title = f"symlink-revision integrity ({code_root})"
    venvs_root = fhs.system_venvs_root()
    hard: list[str] = []
    leftovers: list[str] = []

    def check_link(link_path: Path, boundary: Path, label: str) -> str | None:
        """Returns the revision-dir name when the link is healthy."""
        if not link_path.is_symlink():
            hard.append(
                f"{link_path}: the {label} slot must be a symlink to a"
                " revision (layout v2 §3.10), found a non-symlink"
            )
            return None
        target_name = os.readlink(link_path)
        rev_dir = link_path.parent / target_name
        if not rev_dir.is_dir():
            hard.append(f"{link_path}: dangling symlink -> {target_name}")
            return None
        real_target = Path(os.path.realpath(rev_dir))
        real_boundary = Path(os.path.realpath(boundary))
        if real_target != real_boundary and real_boundary not in real_target.parents:
            hard.append(
                f"{link_path}: revision target {target_name} resolves OUTSIDE"
                f" {boundary} — refusing to treat an escaping revision slot"
                " as healthy"
            )
            return None
        return target_name

    engine_target = check_link(fhs.system_venv_engine(), code_root, "engine venv")
    if engine_target is not None:
        # Engine-side leftovers: venv-<rev> dirs no symlink points at.
        for child in sorted(code_root.iterdir()) if code_root.is_dir() else []:
            if (
                child.is_dir()
                and not child.is_symlink()
                and child.name.startswith("venv-")
                and child.name != engine_target
            ):
                leftovers.append(str(child))

    linked_targets: set[str] = set()
    if venvs_root.is_dir():
        for child in sorted(venvs_root.iterdir()):
            if child.is_symlink():
                target = check_link(child, venvs_root, f"component venv {child.name}")
                if target is not None:
                    linked_targets.add(target)
            elif child.is_dir():
                if child.name not in linked_targets:
                    leftovers.append(str(child))
            else:
                hard.append(f"{child}: non-dir, non-symlink content in the venvs root")

    if hard:
        return Finding(
            "DR-16",
            title,
            Severity.FAIL,
            "; ".join(hard) + (f"; leftover revision(s): {leftovers[:3]}" if leftovers else ""),
            "re-materialize and re-point the revisions:"
            " `sudo vesma system install` (an escaping slot must"
            " be removed by hand first)",
        )
    if leftovers:
        return Finding(
            "DR-16",
            title,
            Severity.WARN,
            "leftover revision dir(s) no symlink points at (an update"
            " rollback left them for diagnosis?): " + ", ".join(leftovers[:5]),
            "after diagnosis remove the leftover revision(s), or re-run `sudo vesma system update`",
        )
    return Finding(
        "DR-16",
        title,
        Severity.OK,
        "revision symlinks resolve inside their roots; no leftovers",
    )


# ── DR-18: system unit presence + drift vs the canon regeneration ──────


def _dr18() -> Finding:
    """The system unit must exist and match
    :func:`vesma.service.system_install.regenerate_system_unit`
    byte-for-byte (the DR-07 leg for the system profile: a hand edit or
    a generator upgrade without reinstall is tampering/drift class).
    """
    title = "system unit vs regeneration"
    try:
        state = read_system_state()
    except SystemInstallError as exc:
        return Finding(
            "DR-18",
            title,
            Severity.WARN,
            f"the system install-state is unreadable: {exc}",
            "vesma doctor  (inventory the broken install-state)",
        )
    if state is None:
        return Finding(
            "DR-18",
            title,
            Severity.OK,
            "no system-profile installation on this machine (n/a)",
        )
    unit_path = fhs.system_unit_path()
    try:
        text = unit_path.read_text(encoding="utf-8")
    except OSError:
        return Finding(
            "DR-18",
            title,
            Severity.FAIL,
            f"the system unit is MISSING at {unit_path} (a recorded"
            " artifact) — the supervisor has no unit to run from",
            "sudo vesma system install",
        )
    expected = regenerate_system_unit()
    if text != expected:
        return Finding(
            "DR-18",
            title,
            Severity.FAIL,
            f"{unit_path} differs from the regenerated unit (hand edit or"
            " generator upgrade) — note: run `vesma doctor` from the engine"
            " venv, a dev-checkout interpreter will not match",
            "sudo vesma system install",
        )
    return Finding(
        "DR-18",
        title,
        Severity.OK,
        f"{unit_path} matches the regeneration byte-for-byte",
    )


# ── DR-21: the system daemon serves the FHS data root (card 4c8a0cbe) ──


def _unit_env_value(unit_text: str, name: str) -> str | None:
    """The last ``Environment="NAME=…"`` value in a unit (quoted or bare);
    ``None`` when the unit never wires the variable — the 6.6.0-era shape
    that left the system engine zero-config (card 4c8a0cbe)."""
    prefix = f"{name}="
    for line in unit_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("Environment="):
            continue
        value = stripped[len("Environment=") :]
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        if value.startswith(prefix):
            return value[len(prefix) :]
    return None


def _expand_against(raw: str, account_home: Path) -> Path:
    """``expanduser`` mirrored against the SERVICE account's home — the
    doctor operator's own home must never leak into the resolution."""
    if raw == "~":
        return account_home
    if raw.startswith("~/"):
        return account_home / raw[2:]
    return Path(raw)


def _resolved_system_store_home(
    unit_text: str, *, getpwnam: Callable[[str], Any] | None = None
) -> tuple[Path | None, str] | None:
    """The store home the SYSTEM daemon would resolve RIGHT NOW, mirrored
    through the engine's OWN config chain (read-only; no protocol call):

    1. ``VESMA_CONFIG`` from the unit's ``Environment=`` (the wiring the
       generator emits since card 4c8a0cbe);
    2. ``<WorkingDirectory>/config.yaml`` (systemd's default CWD for a
       system unit without the directive is ``/``);
    3. ``<account home>/.vesma/config.yaml`` — the zero-config home leg.

    The first candidate that READS wins: its ``vesma.data_dir`` is the
    resolved home (a ``~``-value expands against the service account's
    home, never the doctor operator's); a config without ``data_dir``
    falls through to the zero-config default ``<home>/.vesma/data``.

    Returns ``(None, reason)`` when a config candidate exists but cannot
    be mirrored (unreadable/unparsable — the engine refuses at boot on
    it, never a silent fall-through), and ``None`` when the service
    account itself is unresolvable (the daemon could not run at all —
    DR-15 owns that class).

    ``getpwnam`` is injectable for tests.
    """
    """The store home the SYSTEM daemon would resolve RIGHT NOW, mirrored
    through the engine's OWN config chain (read-only; no protocol call):

    1. ``VESMA_CONFIG`` from the unit's ``Environment=`` (the wiring the
       generator emits since card 4c8a0cbe);
    2. ``<WorkingDirectory>/config.yaml`` (systemd's default CWD for a
       system unit without the directive is ``/``);
    3. ``<account home>/.vesma/config.yaml`` — the zero-config home leg.

    The first candidate that READS wins: its ``vesma.data_dir`` is the
    resolved home (a ``~``-value expands against the service account's
    home, never the doctor operator's); a config without ``data_dir``
    falls through to the zero-config default ``<home>/.vesma/data``.
    ``None`` when the service account itself is unresolvable (the daemon
    could not run at all — DR-15 owns that class).

    ``getpwnam`` is injectable for tests.
    """
    lookup = getpwnam if getpwnam is not None else pwd.getpwnam
    directives, _commented = unitgen.parse_unit(unit_text)
    user = directives.get("User") or unitgen.SYSTEM_USER
    try:
        account_home = Path(str(lookup(user).pw_dir))
    except KeyError:
        return None

    import yaml

    candidates: list[tuple[Path, str]] = []
    env_config = _unit_env_value(unit_text, "VESMA_CONFIG")
    if env_config:
        candidates.append((Path(env_config), f"VESMA_CONFIG={env_config} (unit Environment)"))
    working_dir = directives.get("WorkingDirectory") or os.sep
    candidates.append(
        (Path(working_dir) / "config.yaml", f"config.yaml under WorkingDirectory={working_dir}")
    )
    home_candidate = account_home / ".vesma" / "config.yaml"
    candidates.append((home_candidate, f"{home_candidate} (the account-home zero-config leg)"))

    default_home = account_home / ".vesma" / "data"
    for candidate, how in candidates:
        try:
            if not candidate.is_file():
                continue
            doc = yaml.safe_load(candidate.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError, UnicodeDecodeError):
            # The engine would refuse at boot on this candidate (an
            # unreadable/unparsable config is loud there, never a silent
            # fall-through) — the false-green mirror stops honestly.
            return (
                None,
                f"config candidate {candidate} ({how}) exists but cannot be"
                " mirrored: unreadable or unparsable",
            )
        if not isinstance(doc, dict):
            continue
        section = doc.get("vesma")
        data_dir = section.get("data_dir") if isinstance(section, dict) else None
        if isinstance(data_dir, str) and data_dir.strip():
            resolved = _expand_against(data_dir, account_home).resolve()
            return (resolved, f"vesma.data_dir from {candidate} ({how})")
        return (
            default_home,
            f"zero-config default ({candidate} ({how}) carries no vesma.data_dir)",
        )
    return (
        default_home,
        "zero-config default (~/.vesma/data of the account home; no config candidate exists)",
    )


def _dr21() -> Finding:
    """The system daemon serves the FHS data root, not a user home (card
    ``4c8a0cbe`` — the 2026-10-28 false-green class).

    With the unit missing the config wiring, the engine resolved
    ZERO-CONFIG and silently auto-created an EMPTY store under the
    service account's ``$HOME/.vesma`` — while ``health`` read ok and
    every pre-existing gate stayed green: nothing compared the daemon's
    EFFECTIVE store home with the FHS data root. This check mirrors the
    engine's own resolution (unit Environment → WorkingDirectory →
    account-home config chain) and compares it with the install-state's
    recorded data root; a live mismatch is corroborated by the store
    database actually present under the wrongly-resolved home.
    """
    title = "system daemon serves the FHS data root"
    try:
        state = read_system_state()
    except SystemInstallError as exc:
        return Finding(
            "DR-21",
            title,
            Severity.WARN,
            f"the system install-state is unreadable: {exc}",
            "vesma doctor  (inventory the broken install-state)",
        )
    if state is None:
        return Finding(
            "DR-21",
            title,
            Severity.OK,
            "no system-profile installation on this machine (n/a)",
        )
    expected = fhs.system_data_root()
    artifacts = state.get("artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if isinstance(artifact, dict) and artifact.get("role") == "data_root":
                recorded = artifact.get("path")
                if isinstance(recorded, str) and recorded:
                    expected = Path(recorded)
                break
    unit_path = fhs.system_unit_path()
    try:
        unit_text = unit_path.read_text(encoding="utf-8")
    except OSError:
        return Finding(
            "DR-21",
            title,
            Severity.FAIL,
            f"the system unit is MISSING at {unit_path} — the daemon has no"
            " unit to resolve anything from (DR-18 carries the unit gate)",
            "sudo vesma system install",
        )
    resolution = _resolved_system_store_home(unit_text)
    if resolution is None:
        return Finding(
            "DR-21",
            title,
            Severity.WARN,
            f"the {unitgen.SYSTEM_USER} service account is not resolvable —"
            " the daemon's store-home resolution cannot be mirrored (a"
            " missing account means the daemon cannot run at all; DR-15"
            " flags the ownership drift)",
            "sudo vesma system install  (applies the sysusers fragment)",
        )
    home, how = resolution
    if home is None:
        return Finding(
            "DR-21",
            title,
            Severity.WARN,
            f"the daemon's store-home resolution cannot be mirrored: {how}"
            " (the engine refuses at boot on such a config — health would"
            " NOT be ok; fix the config, then re-run `vesma doctor`)",
        )
    if home == expected:
        return Finding(
            "DR-21",
            title,
            Severity.OK,
            f"the daemon resolves its store to the FHS data root {expected} (via {how})",
        )
    from vesma.store_migration import CANONICAL_DB_NAME

    evidence = ""
    if (home / CANONICAL_DB_NAME).exists():
        evidence = (
            f" — the auto-created store IS materialized there"
            f" ({home / CANONICAL_DB_NAME} exists): the health-ok but"
            " EMPTY-auto-store false-green is live"
        )
    return Finding(
        "DR-21",
        title,
        Severity.FAIL,
        f"the daemon resolves its store to {home} (via {how}) — NOT the"
        f" FHS data root {expected}{evidence}. This is the 2026-10-28"
        " false-green class: health reads ok while the daemon serves an"
        " auto-created store outside the installed data root",
        "sudo vesma system install  (regenerates the unit with the"
        " VESMA_CONFIG + WorkingDirectory wiring) — then verify"
        " /etc/vesma/config.yaml names the data root as vesma.data_dir",
    )


def _uid_to_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid-{uid}"


def _gid_to_name(gid: int) -> str:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return f"gid-{gid}"


# ── DR-22: loaded component tree vs canonical manifests dir ───────────


def _dr22() -> Finding:
    """Loaded tree == canonical dir (card 0f75698d, ArchCom 2026-10-29).

    The live source of truth is the RUNNING supervisor's own component set
    (control-socket ``status``), never a re-resolution through the same
    layout resolver — the bug class this check guards is precisely
    "resolver says X, supervisor runs Y". While the supervisor is down the
    check degrades to a directory summary (status, not an error), and a
    failed probe degrades the same way per the DR-08 contract.
    """
    title = "loaded components vs canonical manifests dir"
    canonical = layout.components_dir()
    disk_names = sorted(p.stem for p in canonical.glob("*.yaml")) if canonical.is_dir() else []

    system_unit_present = fhs.system_unit_path().exists()
    if not layout.system_profile_active() and system_unit_present:
        return Finding(
            "DR-22",
            title,
            Severity.OK,
            "system installation detected, but this doctor session resolves "
            "the user plane (unprivileged) — re-run privileged to audit the "
            "system manifests plane",
        )
    if not canonical.is_dir():
        return Finding(
            "DR-22",
            title,
            Severity.WARN,
            f"canonical manifests dir {canonical} does not exist"
            + (
                " — the system manifest step never ran or was removed"
                if layout.system_profile_active()
                else ""
            ),
            f"install components so manifests land in {canonical}: vesma install board",
        )
    if not os.access(canonical, os.R_OK):
        return Finding(
            "DR-22",
            title,
            Severity.FAIL,
            f"canonical manifests dir {canonical} is not readable by this "
            "identity — the service user may be blind to its own manifests",
            "restore the FHS rights (dir 0750 root:vesma, manifests 0640 "
            "root:vesma) — see the layout v2 §3.4 table",
        )

    # Manifests parked in a service-home plane dir: the pre-fix workaround
    # shape (card 0f75698d). Once the resolver dispatches, these copies are
    # dead weight the operator must remove — flagged as hygiene.
    parked: list[Path] = []
    if layout.system_profile_active():
        data_root = fhs.system_data_root()
        parked = [
            p
            for p in (
                data_root / ".config" / "vesma" / "components.d",
                data_root / ".vesma" / "components.d",
            )
            if p.is_dir() and any(p.glob("*.yaml"))
        ]

    loaded_names: list[str] | None = None
    probe_note = ""
    runtime = layout.resolve_runtime_dir()
    sock_path = runtime.path / "control.sock"
    if sock_path.exists():
        try:
            hello = json.dumps({"id": 1, "method": "hello", "params": {"protocol_version": 1}})
            status_req = json.dumps({"id": 2, "method": "status", "params": {}})
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(2.0)
                sock.connect(str(sock_path))
                sock.sendall((hello + "\n").encode("utf-8"))
                sock.recv(65536).decode("utf-8", errors="replace")
                sock.sendall((status_req + "\n").encode("utf-8"))
                status_reply = sock.recv(65536).decode("utf-8", errors="replace").strip()
            payload = json.loads(status_reply.splitlines()[0])
            loaded_names = sorted(payload.get("result", {}).get("components", {}))
        except (OSError, json.JSONDecodeError, IndexError, AttributeError) as exc:
            probe_note = f"live probe failed ({type(exc).__name__}) — comparison degraded"

    if loaded_names is None:
        finding = Finding(
            "DR-22",
            title,
            Severity.OK,
            f"supervisor not running or probe unavailable — canonical dir "
            f"{canonical} holds {len(disk_names)} manifests"
            + (f" ({', '.join(disk_names)})" if disk_names else "")
            + (f"; {probe_note}" if probe_note else ""),
        )
    else:
        loaded_set, disk_set = set(loaded_names), set(disk_names)
        bundle_only = bool(loaded_set) and loaded_set <= {"board", "metrics"}
        if bundle_only and (disk_set - loaded_set):
            finding = Finding(
                "DR-22",
                title,
                Severity.FAIL,
                f"supervisor runs the BUNDLE fallback ({', '.join(loaded_names)}) while "
                f"the canonical manifests dir {canonical} holds operator manifests the "
                f"service did NOT load ({', '.join(sorted(disk_set - loaded_set))}) — "
                "loss-class",
                "restart the service so it loads the canonical manifests: "
                "sudo systemctl restart vesma.service",
            )
        elif bundle_only and not disk_set:
            finding = Finding(
                "DR-22",
                title,
                Severity.OK,
                "supervisor runs the bundle fallback; the canonical dir "
                f"{canonical} holds no operator manifests (nothing lost)",
            )
        elif loaded_set != disk_set:
            finding = Finding(
                "DR-22",
                title,
                Severity.WARN,
                f"loaded {sorted(loaded_set)} != canonical {sorted(disk_set)} at {canonical}",
                "restart the service after any manifest change: "
                "sudo systemctl restart vesma.service",
            )
        else:
            finding = Finding(
                "DR-22",
                title,
                Severity.OK,
                f"loaded tree matches the canonical dir ({len(loaded_set)} components "
                f"at {canonical})",
            )

    if parked and finding.severity != Severity.FAIL:
        return Finding(
            "DR-22",
            title,
            Severity.WARN,
            f"manifests parked in a non-canonical service-home dir "
            f"({', '.join(str(p) for p in parked)}) — pre-fix workaround shape; the "
            f"canonical plane is {canonical}",
            "confirm the service loads from the canonical dir (this check), then "
            "remove the parked copies",
        )
    return finding


# ── DR-23: artifacts outside the platform (ArchCom 2026-10-29,
# verdict 1c2b8d64: "an artifact outside the slot table is a doctor
# finding, not a lifestyle"; the audit classes live in handoff
# vesma-component-platform-file-layout-20261029.md §3: DE-2 the root unit
# exec'ing into a user home, DE-3 the legacy mnemos-* binary names, DE-3/5
# live component-named processes without a supervising unit) ──────────────


#: A path under SOMEONE's user home (``/home/<user>/…`` or
#: ``/var/home/<user>/…`` — the immutable-host spellings included).
_DR23_FOREIGN_HOME_RE = re.compile(r"^/(?:var/)?home/[^/]+(?:/|$)")

#: Active (non-comment) path-bearing directives of a service unit.
_DR23_UNIT_PATH_DIRECTIVE_RE = re.compile(r"^(Exec[A-Za-z]+|EnvironmentFile)\s*=\s*(.+)$")

#: Component-name families that must exist ONLY inside the platform
#: (``vesma-``/``vesmaro-`` current names, ``mnemos-`` the pre-2026-10-01
#: rebrand legacy); a BARE ``vesma`` (no dash) is the supervisor itself
#: and never matches.
_DR23_COMPONENT_NAME_RE = re.compile(r"vesma-|vesmaro-|mnemos-")

#: The units allowed to own component processes (the supervisor's own).
_DR23_SUPERVISED_UNIT_EXACT = "vesma.service"
_DR23_SUPERVISED_UNIT_PREFIX = "vesma@"

#: /proc walking caps: a read-only scan must stay cheap and bounded.
_DR23_PID_SCAN_CAP = 4096
_DR23_DETAIL_ITEMS_CAP = 6

#: Injectable for tests (monkeypatch to a fake proc tree).
_PROC_PATH = Path("/proc")


def _dr23_unit_dirs(prefix: Path) -> tuple[Path, Path]:
    """The SYSTEM unit directories of the scan universe: the admin dir
    (every root-owned unit) and the vendor dir (vesma-named units only —
    a vendor unit is not platform-installed unless it IS the manifest's
    system unit)."""
    return prefix / "etc/systemd/system", prefix / "usr/lib/systemd/system"


def _dr23_unit_files(prefix: Path) -> list[Path]:
    """The unit files to audit: ``*.service`` from the admin dir plus
    ``vesma*.service`` from the vendor dir, the ratified system unit
    (``fhs.system_unit_path()``) excluded — the platform's own unit is
    DR-18's subject and must never be slandered here. Deduplicated by
    resolved path."""
    admin_dir, vendor_dir = _dr23_unit_dirs(prefix)
    canonical = fhs.system_unit_path()
    candidates: list[Path] = []
    if admin_dir.is_dir():
        candidates.extend(sorted(admin_dir.glob("*.service")))
    if vendor_dir.is_dir():
        candidates.extend(sorted(vendor_dir.glob("vesma*.service")))
    files: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate if not candidate.is_symlink() else Path(os.path.realpath(candidate))
        if resolved == Path(os.path.realpath(canonical)) or resolved in seen:
            continue
        seen.add(resolved)
        files.append(candidate)
    return files


def _dr23_directive_paths(value: str) -> list[str]:
    """The path-like tokens of one directive value (shlex, not str.split:
    systemd parses the value with shell-like quoting — the DR-12
    precedent). ``-``-prefixed EnvironmentFile absence markers and
    ``--flag=path`` spellings are undone so the path itself is judged."""
    try:
        tokens = shlex.split(value)
    except ValueError:
        tokens = [value]
    paths: list[str] = []
    for token in tokens:
        candidate = token[1:] if token.startswith("-") else token
        # systemd command prefixes: ``!!`` (privilege requirement) rides
        # the SAME argv token as the binary path — strip before judging.
        candidate = candidate.lstrip("!")
        candidate = candidate.split("=", 1)[-1] if "=" in candidate else candidate
        if candidate.startswith("/"):
            paths.append(candidate)
    return paths


def _dr23_scan_units(
    prefix: Path,
) -> tuple[list[str], list[str], int, int, list[str]]:
    """Legs (a)+(b) over the unit corpus — pure text parsing, nothing is
    executed. Returns ``(fail_items, warn_items, units_checked,
    units_unreadable, na_notes)``:

    - leg (a) FAIL: a root-owned unit whose Exec*/EnvironmentFile path
      lands under a user home (``/home|/var/home/<user>/``) instead of
      the ratified service home (paths under ``fhs.system_data_root()``
      are legal by contract) — root executing a user-writable file is
      the LPE class;
    - leg (b) WARN: an argv path whose basename carries the legacy
      ``mnemos-*`` name (pre-rebrand 2026-10-01) — the rename contract
      keeps artifacts under the platform's name table.
    """
    fail_items: list[str] = []
    warn_items: list[str] = []
    na_notes: list[str] = []
    files = _dr23_unit_files(prefix)
    data_root = fhs.system_data_root()
    checked = 0
    unreadable = 0
    for unit_path in files:
        try:
            text = unit_path.read_text(encoding="utf-8")
        except OSError:
            unreadable += 1
            continue
        checked += 1
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(("#", ";", "[")):
                continue
            match = _DR23_UNIT_PATH_DIRECTIVE_RE.match(line)
            if match is None:
                continue
            directive, value = match.group(1), match.group(2)
            for candidate in _dr23_directive_paths(value):
                # The data-root exemption covers leg (a) ONLY — a legacy
                # NAME is a finding wherever it lives (the legs are
                # independent; no short-circuit between them).
                candidate_path = Path(candidate)
                if (
                    data_root != candidate_path
                    and data_root not in candidate_path.parents
                    and _DR23_FOREIGN_HOME_RE.match(candidate)
                ):
                    fail_items.append(
                        f"{unit_path}: {directive} -> {candidate} (a root-owned"
                        " unit executing/reading a user-writable home path —"
                        " the LPE class; move the artifact into the platform"
                        " slot)"
                    )
                basename = candidate.rsplit("/", 1)[-1].split("=", 1)[-1]
                if basename.startswith("mnemos-"):
                    warn_items.append(
                        f"{unit_path}: {directive} -> {candidate} (a legacy"
                        " mnemos-* binary name from before the 2026-10-01"
                        " rebrand — the platform rename contract keeps"
                        " artifacts under vesma-* names)"
                    )
    if not files and unreadable == 0:
        na_notes.append("no system units found (n/a)")
    return fail_items, warn_items, checked, unreadable, na_notes


def _dr23_process_unit(proc_root: Path, pid: str) -> str | None:
    """The systemd unit owning ``pid`` via its cgroup path (the last
    ``.service`` basename across the cgroup lines; ``None`` when the
    cgroup file is unreadable or names no service)."""
    try:
        lines = (proc_root / pid / "cgroup").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    unit = None
    for line in lines:
        parts = line.split(":", 2)
        cgroup_path = parts[2] if len(parts) == 3 else ""
        basename = cgroup_path.rsplit("/", 1)[-1]
        if basename.endswith(".service"):
            unit = basename
    return unit


def _dr23_scan_processes(proc_root: Path) -> tuple[list[str], int, bool]:
    """Leg (c), best-effort: live processes whose name carries a platform
    component-name family but whose owning unit is NOT the supervisor's
    (``vesma.service`` / ``vesma@*.service``) — a component running
    outside the platform's lifecycle. Read-only, bounded; a partially
    unreadable /proc degrades to the ``checked`` count speaking for
    itself. Returns ``(flagged_items, processes_checked, proc_available)``.
    """
    if not proc_root.is_dir():
        return [], 0, False
    flagged: list[str] = []
    checked = 0
    pids = sorted((p for p in proc_root.iterdir() if p.name.isdigit()), key=lambda p: int(p.name))
    for pid_dir in pids[:_DR23_PID_SCAN_CAP]:
        try:
            name = (pid_dir / "comm").read_text(encoding="utf-8").strip()
        except OSError:
            continue  # unreadable entry — the honest degrade is the count
        checked += 1
        if not name or _DR23_COMPONENT_NAME_RE.search(name) is None:
            continue
        unit = _dr23_process_unit(proc_root, pid_dir.name)
        supervised = unit is not None and (
            unit == _DR23_SUPERVISED_UNIT_EXACT or unit.startswith(_DR23_SUPERVISED_UNIT_PREFIX)
        )
        if not supervised:
            flagged.append(
                f"{name} (pid {pid_dir.name}, unit {unit or 'unknown'}) is not"
                " owned by the supervisor — a live component outside the"
                " platform's lifecycle"
            )
    return flagged, checked, True


def _dr23(proc_root: Path | None = None) -> Finding:
    """Artifacts outside the platform (ArchCom 2026-10-29, 1c2b8d64):
    three independent legs, one finding, worst severity wins — every leg
    stays readable in the detail:

    (a) system units exec/reading user homes — FAIL (security class);
    (b) legacy ``mnemos-*`` binary names in units — WARN (rename
        contract);
    (c) live component-named processes without a supervising unit —
        WARN, best-effort over ``/proc`` with an honest degrade.

    All legs read-only; the doctor never kills, moves or rewrites — the
    findings name the platform slot and the fix belongs to the operator.
    Known coverage edge (honest, by the cheap-text-parse design): direct
    argv paths are judged; a path buried inside a shell-wrapped
    argument (``bash -lc 'exec /home/x/…'``) is indirection this parse
    deliberately does not chase.
    """
    title = "artifacts outside the platform"
    prefix = fhs.default_prefix()
    fail_items, warn_items, units_checked, units_unreadable, na_notes = _dr23_scan_units(prefix)
    root = proc_root if proc_root is not None else _PROC_PATH
    process_items, processes_checked, proc_available = _dr23_scan_processes(root)

    segments: list[str] = []
    units_summary = f"system units: {units_checked} checked"
    if units_unreadable:
        units_summary += f", {units_unreadable} unreadable without privileges"
    segments.append(units_summary + (f" ({'; '.join(na_notes)})" if na_notes else ""))
    if proc_available:
        segments.append(f"live processes: {processes_checked} scanned")
    else:
        segments.append("live processes: scan unavailable (no /proc)")

    problems = fail_items + warn_items + process_items
    if problems:
        listed = "; ".join(segments + problems[:_DR23_DETAIL_ITEMS_CAP])
        if len(problems) > _DR23_DETAIL_ITEMS_CAP:
            listed += f"; … {len(problems) - _DR23_DETAIL_ITEMS_CAP} further finding(s) suppressed"
        if fail_items:
            return Finding(
                "DR-23",
                title,
                Severity.FAIL,
                listed,
                "move the artifact into its platform slot and regenerate the"
                " unit from the component manifest (`vesma service install`;"
                " hand-written units are a doctor finding, not a lifestyle)",
            )
        return Finding(
            "DR-23",
            title,
            Severity.WARN,
            listed,
            "re-point the legacy artifact at the platform slot (component"
            " manifest + `vesma install board`) and stop the out-of-platform"
            " process; components run under the supervisor's units",
        )
    return Finding("DR-23", title, Severity.OK, "; ".join(segments))


# ── Runner ────────────────────────────────────────────────────────────


def run_service_checks() -> list[Finding]:
    """Run the doctor registry against the local installation. READ-ONLY.

    DR-01…DR-13 (layout/service-lifecycle v1) + the layout v2 §3.14
    system-profile gates (ADR-0045 rollout 3): DR-14 (profile isolation,
    full matrix), DR-15 (system rights vs the FHS manifest incl. the
    env.d 0600 FAIL gate), DR-16 (symlink-revision integrity),
    DR-17 (generation mixing + system state↔fs inversion), DR-18 (system
    unit vs regeneration), DR-19 (generation advisories, both profiles),
    DR-20 (second writer on the store home — ADR-0049 wave 0), DR-21
    (the system daemon serves the FHS data root — card 4c8a0cbe), DR-23
    (artifacts outside the platform — ArchCom 2026-10-29, 1c2b8d64).
    """
    installation: dict[str, ComponentManifest] | None = None
    try:
        components_dir = layout.components_dir()
        if components_dir.is_dir():
            installation = load_installation(components_dir)
    except Exception as exc:  # doctor reports, never crashes
        return [
            Finding(
                "DR-00",
                "installation load",
                Severity.FAIL,
                f"components.d failed to load (fail-closed): {exc}",
                f"fix or remove the offending file in {layout.components_dir()}",
            )
        ]

    checks: list[tuple[str, str, Callable[[], Finding]]] = [
        ("DR-01", "rights/ownership of config/state/env roots", _dr01),
        ("DR-02", "venv integrity", _dr02),
        ("DR-03", "user-site leak", _dr03),
        ("DR-04", "venv uniqueness across manifests", lambda: _dr04(installation)),
        ("DR-05", "python version constraints", lambda: _dr05(installation)),
        ("DR-06", "venv != engine venv; reserved names", lambda: _dr06(installation)),
        ("DR-07", "unit drift (installed vs regenerated)", _dr07),
        ("DR-08", "control socket liveness", _dr08),
        ("DR-09", "health port collisions", lambda: _dr09(installation)),
        ("DR-10", "free space on canonical roots", _dr10),
        ("DR-11", "journald Storage=persistent", _dr11),
        ("DR-12", "venv read-only at runtime", _dr12),
        ("DR-13", "container downgrade allowlist", _dr13),
        ("DR-14", "profile isolation (user ↔ system roots + stamps)", _dr14),
        ("DR-15", "system-profile rights vs FHS manifest", _dr15),
        ("DR-16", "symlink-revision integrity", _dr16),
        ("DR-17", "install-state ↔ filesystem (generations + system inversion)", _dr17),
        ("DR-18", "system unit vs regeneration", _dr18),
        ("DR-19", "layout generation advisories", _dr19),
        ("DR-20", "second writer on the store home", _dr20),
        ("DR-21", "system daemon serves the FHS data root", _dr21),
        ("DR-22", "loaded components vs canonical manifests dir", _dr22),
        ("DR-23", "artifacts outside the platform", _dr23),
    ]
    findings: list[Finding] = []
    for check_id, title, check in checks:
        try:
            findings.append(check())
        except Exception as exc:  # doctor must never crash
            findings.append(
                Finding(
                    check_id,
                    title,
                    Severity.FAIL,
                    f"check crashed: {type(exc).__name__}: {exc}",
                )
            )
    return findings


def exit_code(findings: list[Finding]) -> int:
    """1 on any FAIL, 2 on warnings-only, else 0 (doctor conventions)."""
    if any(f.severity == Severity.FAIL for f in findings):
        return 1
    if any(f.severity == Severity.WARN for f in findings):
        return 2
    return 0


def system_target_forecast() -> list[Finding]:
    """The system-profile legs the ``migrate-store --target=system``
    dry-run renders as its DR forecast (ADR-0047 C13).

    ONE implementation rule: the forecast calls the very same doctor
    check functions the ``vesma doctor`` registry runs — the mover never
    re-derives a DR predicate of its own, so a diagnosis and the
    migration plan cannot disagree. The caller renders the results
    marked as a FORECAST (a dry-run prediction about the environment the
    migration would land in, not a post-migration verdict). Read-only.
    """
    return [_dr14(), _dr15(), _dr17()]


__all__ = ["Finding", "Severity", "exit_code", "run_service_checks", "system_target_forecast"]
