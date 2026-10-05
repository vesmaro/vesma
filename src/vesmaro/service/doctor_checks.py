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

import dataclasses
import getpass
import json
import os
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

from vesmaro.service import layout, unitgen
from vesmaro.service.install import engine_venv, parse_pin_line, read_lock
from vesmaro.service.manifest import ComponentManifest, load_installation

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
    dir_specs: list[tuple[Path, int]] = [
        (layout.config_root(), 0o700),
        (layout.components_dir(), 0o700),
        (layout.env_dir(), 0o700),
        (layout.data_root(), 0o700),
        (layout.data_root() / "venvs", 0o700),
        (layout.state_root(), 0o700),
        (layout.history_dir(), 0o700),
        (layout.run_fallback_dir(), 0o700),
        (layout.cache_base(), 0o750),
    ]
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
    venvs_root = layout.data_root() / "venvs"
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
    venvs_root = layout.data_root() / "venvs"
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
        for reserved in ("venv", "venvs"):
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
    config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(config_home) if config_home else layout.home() / ".config"
    return base / "systemd" / "user" / "vesma.service"


def _regenerate_unit(downgraded: list[str]) -> str:
    return unitgen.generate(
        home=layout.home(),
        engine_venv=engine_venv(),
        read_write_paths=(layout.state_root(), layout.cache_base(), layout.data_root()),
        read_only_paths=(layout.engine_venv_dir(), layout.data_root() / "venvs"),
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
    roots = [
        ("config", layout.config_root()),
        ("data", layout.data_root()),
        ("state", layout.state_root()),
        ("cache", layout.cache_base()),
    ]
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
    venv = layout.engine_venv_dir()
    venvs = layout.data_root() / "venvs"
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


# ── Runner ────────────────────────────────────────────────────────────


def run_service_checks() -> list[Finding]:
    """Run DR-01…DR-13 against the local installation. READ-ONLY."""
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


__all__ = ["Finding", "Severity", "exit_code", "run_service_checks"]
