"""Service install/uninstall flow (service-lifecycle v1 §3.6 + layout v1 §3.5/§3.8).

The single writer of the installation (layout §3.5): bundled pack
manifests into ``components.d/<name>.yaml`` (atomic tmp + rename),
per-component data dirs, one venv per python child component, and the
generated systemd user unit. Idempotent: re-running install regenerates
every artifact (the threat model's answer to hand-edited units).

venv discipline (layout §3.8): EXACT ``==`` pins against an
install-generated lock, PyPI-only sources, drift check after install.
For the bundled ``metrics`` component the requirement is the engine
package itself (``vesma==<engine version>``) — its argv runs
``vesmaro.metrics.exposer`` from the component venv. ``board`` is
in-process and has NO component venv by design (it lives on the engine
venv, layout §3.8b).

``pip --require-hashes`` is a SHOULD (layout §3.8) — deliberately skipped
for v1, see ``_install_component_venv`` for the TODO.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import shutil
import subprocess  # nosec B404 - subprocess needed for venv/pip/systemctl with fixed argv
import sys
from pathlib import Path

from vesmaro.service import layout, unitgen
from vesmaro.service.manifest import ComponentManifest, bundled_manifest_path, load_installation

logger = logging.getLogger("vesmaro.service.install")

#: Components shipped inside the engine package (the W1 pack).
BUNDLED_COMPONENTS: tuple[str, ...] = ("board", "metrics")


def _engine_version() -> str:
    from vesmaro import __version__

    return __version__


def _bundled_requirements(name: str) -> tuple[str, ...]:
    return (f"vesma=={_engine_version()}",) if name == "metrics" else ()


#: LY-08: a lock/input line MUST be an exact ``name==version`` pin; URL /
#: ``file:`` / range / wildcard specs are rejected (a wildcard pin is NOT
#: an exact pin).
_PIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9][A-Za-z0-9.+!-]*$")


class InstallError(Exception):
    """Install/uninstall refused — the message is operator-facing."""


@dataclasses.dataclass(frozen=True)
class InstallResult:
    """Outcome of one install run (for the CLI report and tests)."""

    lines: tuple[str, ...]
    manifests: tuple[Path, ...]
    venvs: tuple[Path, ...]
    unit_path: Path
    downgraded: tuple[str, ...]
    daemon_reload: str  # "ok" or "skipped: <reason>"


@dataclasses.dataclass(frozen=True)
class UninstallResult:
    lines: tuple[str, ...]


# ── Atomic file primitives (LY-02 drop-in discipline) ─────────────────


def _atomic_write(path: Path, data: str, mode: int) -> None:
    """Write via a same-directory tmp file + ``os.replace`` (atomic)."""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(data, encoding="utf-8")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _unit_path() -> Path:
    """``$XDG_CONFIG_HOME/systemd/user/vesma.service`` (user profile)."""
    config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(config_home) if config_home else layout.home() / ".config"
    return base / "systemd" / "user" / "vesma.service"


# ── Install ───────────────────────────────────────────────────────────


def _ensure_canonical_dirs() -> None:
    """Every §3.2 root the install flow owns, explicit modes (LY-01)."""
    for path in (
        layout.config_root(),
        layout.components_dir(),
        layout.env_dir(),
        layout.data_root(),
        layout.data_root() / "venvs",
        layout.state_root(),
        layout.history_dir(),
        layout.run_fallback_dir(),
    ):
        layout.ensure_dir(path, layout.MODE_DIR_DEFAULT)
    layout.ensure_dir(layout.cache_base(), layout.MODE_DIR_CACHE)


def _write_bundled_manifests(components_dir: Path) -> list[Path]:
    """Copy the pack manifests into components.d (atomic, LY-02)."""
    written: list[Path] = []
    for name in BUNDLED_COMPONENTS:
        source = bundled_manifest_path(name)
        target = components_dir / f"{name}.yaml"
        _atomic_write(target, source.read_text(encoding="utf-8"), 0o644)
        written.append(target)
    return written


def _write_component_schemas(manifest: ComponentManifest) -> Path | None:
    """Materialize ``config.schema.json`` in the component data dir.

    ``schema_inline`` is written out verbatim; ``schema_file`` is copied
    from its manifest-relative location — so the future ``schema_file``
    resolver can work from the data dir (layout §3.2).
    """
    config = manifest.config
    if config is None:
        return None
    schema_text: str | None = None
    if config.schema_inline is not None:
        schema_text = json.dumps(config.schema_inline, indent=2, ensure_ascii=False) + "\n"
    elif config.schema_file is not None:
        raw = Path(os.path.expanduser(config.schema_file))
        source = raw if raw.is_absolute() else manifest.path.parent / raw
        schema_text = source.read_text(encoding="utf-8")
    if schema_text is None:
        return None
    data_dir = layout.ensure_dir(layout.data_dir(manifest.name), layout.MODE_DIR_DEFAULT)
    schema_path = data_dir / "config.schema.json"
    _atomic_write(schema_path, schema_text, 0o644)
    return schema_path


def _has_venv(manifest: ComponentManifest) -> bool:
    """A python child: its launch argv references ``{venv_bin}``."""
    return manifest.launch is not None and any("{venv_bin}" in arg for arg in manifest.launch.argv)


def _validate_pin(line: str) -> None:
    if _PIN_RE.match(line) is None:
        raise InstallError(
            f"requirement {line!r} is not an exact 'name==version' pin — "
            "layout §3.8: dependencies are pinned with '==' and sourced from "
            "PyPI only (URLs, file: and range specs are rejected)"
        )


def _create_component_venv(name: str) -> Path:
    """``python -m venv`` for one python child (0700)."""
    venv_dir = layout.component_venv_dir(name)
    if (venv_dir / "bin" / "python").exists():
        return venv_dir  # idempotent re-install: keep the existing venv
    result = subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, "-m", "venv", str(venv_dir)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        raise InstallError(
            f"venv creation failed for {name!r} ({venv_dir}): {result.stderr.strip()}"
        )
    os.chmod(venv_dir, layout.MODE_DIR_DEFAULT)
    return venv_dir


def _run_pip(venv_dir: Path, args: list[str]) -> str:
    python = venv_dir / "bin" / "python"
    result = subprocess.run(  # nosec B603 - fixed argv, no shell
        [str(python), "-m", "pip", *args],
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    if result.returncode != 0:
        raise InstallError(
            f"pip {' '.join(args[:2])} failed in {venv_dir}: {result.stderr.strip()[-500:]}"
        )
    return result.stdout


def _parse_pin_line(line: str) -> tuple[str, str] | None:
    """``name==version`` from one freeze/lock line; None for non-pin lines.

    Partition on ``==`` (not ``=``): PEP 503 names cannot contain ``=``,
    so the FIRST ``==`` separator splits name and version exactly.
    """
    line = line.strip()
    if "==" not in line:
        return None
    dist, _, version = line.partition("==")
    dist = dist.strip()
    if not dist:
        return None
    return re.sub(r"[-_.]+", "-", dist).lower(), version.strip()


def _pip_freeze(venv_dir: Path) -> dict[str, str]:
    """Normalized ``name -> version`` map of one venv."""
    out = _run_pip(venv_dir, ["freeze", "--disable-pip-version-check"])
    frozen: dict[str, str] = {}
    for line in out.splitlines():
        parsed = _parse_pin_line(line)
        if parsed is not None:
            frozen[parsed[0]] = parsed[1]
    return frozen


def _install_component_venv(manifest: ComponentManifest, report: list[str]) -> Path:
    """Create the venv, install exact pins, write + verify the lock.

    TODO(v2, layout §3.8 SHOULD): hash-pinning with ``pip --require-hashes``
    — needs a lock tooling pass (hashes for the full transitive tree);
    skipped deliberately in v1, do not re-add silently as "done".
    """
    name = manifest.name
    requirements = _bundled_requirements(name)
    if not requirements:
        raise InstallError(
            f"component {name!r} references {{venv_bin}} but the v1 install "
            "flow has no requirement set for it — the v1 pack installs only "
            f"{sorted(BUNDLED_COMPONENTS)}"
        )
    for line in requirements:
        _validate_pin(line)
    venv_dir = _create_component_venv(name)
    if requirements:
        _run_pip(
            venv_dir,
            ["install", "--disable-pip-version-check", "--no-input", *requirements],
        )
    # The lock is the FULL freeze of the freshly installed venv (every
    # line an exact '==' pin) — drift detection in doctor DR-02 compares
    # freeze against this file exactly.
    frozen = _pip_freeze(venv_dir)
    for line in requirements:
        dist, _, expected = line.partition("==")
        actual = frozen.get(re.sub(r"[-_.]+", "-", dist).lower())
        if actual != expected:
            raise InstallError(
                f"venv drift right after install for {name!r}: "
                f"{dist}=={expected} expected, freeze reports {actual!r}"
            )
    lock_text = "".join(f"{dist}=={version}\n" for dist, version in sorted(frozen.items()))
    lock_path = layout.data_dir(name) / "requirements-lock.txt"
    _atomic_write(lock_path, lock_text, 0o644)
    report.append(f"venv: {name} -> {venv_dir} (lock: {lock_path})")
    return venv_dir


def _daemon_reload() -> str:
    """Best-effort ``systemctl --user daemon-reload`` (never fatal)."""
    try:
        result = subprocess.run(  # nosec B603 - fixed argv, no shell
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except FileNotFoundError:
        return "skipped: systemctl not found (no systemd on this machine)"
    except subprocess.TimeoutExpired:
        return "skipped: systemctl timed out"
    if result.returncode != 0:
        return f"skipped: systemctl --user daemon-reload failed: {result.stderr.strip()[:200]}"
    return "ok"


def install() -> InstallResult:
    """Install the service: manifests, data dirs, venvs, unit. Idempotent."""
    report: list[str] = []

    downgraded: tuple[str, ...] = ()
    if unitgen.container_detect():
        downgraded = tuple(sorted(unitgen.DOWNGRADE_ALLOWED))
        report.append(
            "CONTAINER DOWNGRADE (loud, documented — specs/service-lifecycle/v1 "
            "§3.6): filesystem hardening directives commented out in the unit: "
            + ", ".join(downgraded)
        )

    # 1. Canonical dirs, explicit modes.
    _ensure_canonical_dirs()

    # 2. Bundled manifests -> components.d (atomic, LY-02: only OUR files).
    components_dir = layout.components_dir()
    manifests = _write_bundled_manifests(components_dir)
    for path in manifests:
        report.append(f"manifest: {path}")

    # 3. Fail-closed validation of the WHOLE installation dir (every file
    # must be a valid manifest — a stray file surfaces here, LY-02).
    installation: dict[str, ComponentManifest] = load_installation(components_dir)
    report.append(
        f"validation: components.d loaded fail-closed — "
        f"{len(installation)} component(s): {', '.join(sorted(installation))}"
    )

    # 4. Per-component data dirs + config schemas.
    for manifest in sorted(installation.values(), key=lambda m: m.name):
        layout.ensure_dir(layout.data_dir(manifest.name), layout.MODE_DIR_DEFAULT)
        schema_path = _write_component_schemas(manifest)
        if schema_path is not None:
            report.append(f"schema: {schema_path}")

    # 5. One venv per python child (LY-05); in-process board has none.
    venvs: list[Path] = []
    for manifest in sorted(installation.values(), key=lambda m: m.name):
        if not _has_venv(manifest):
            report.append(
                f"venv: {manifest.name} — no component venv "
                f"(in-process or non-python child lives on the engine venv, "
                f"layout §3.8b)"
            )
            continue
        venvs.append(_install_component_venv(manifest, report))

    # 6. Generate + write the unit (SL-17/SL-18), then daemon-reload.
    unit_text = unitgen.generate(
        home=layout.home(),
        engine_venv=engine_venv(),
        read_write_paths=(layout.state_root(), layout.cache_base(), layout.data_root()),
        read_only_paths=(layout.engine_venv_dir(), layout.data_root() / "venvs"),
        downgraded=downgraded,
    )
    unit_path = _unit_path()
    layout.ensure_dir(unit_path.parent, layout.MODE_DIR_DEFAULT)
    _atomic_write(unit_path, unit_text, 0o644)
    report.append(f"unit: {unit_path} (0644, generated — do not edit)")
    reload_status = _daemon_reload()
    report.append(f"systemctl --user daemon-reload: {reload_status}")

    logger.info(
        "service installed: components=%s venvs=%s unit=%s downgraded=%s",
        sorted(installation),
        [v.name for v in venvs],
        unit_path,
        list(downgraded),
    )
    return InstallResult(
        lines=tuple(report),
        manifests=tuple(manifests),
        venvs=tuple(venvs),
        unit_path=unit_path,
        downgraded=downgraded,
        daemon_reload=reload_status,
    )


def engine_venv() -> Path:
    """The venv the running engine lives in (``ExecStart`` target)."""
    return Path(sys.executable).absolute().parent.parent


# ── Uninstall ─────────────────────────────────────────────────────────


def _systemctl_user(args: list[str]) -> str:
    """Best-effort ``systemctl --user <args>``; returns a status note."""
    try:
        result = subprocess.run(  # nosec B603 - fixed argv, no shell
            ["systemctl", "--user", *args],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except FileNotFoundError:
        return "skipped: systemctl not found"
    except subprocess.TimeoutExpired:
        return "skipped: systemctl timed out"
    if result.returncode != 0:
        return f"skipped: {result.stderr.strip()[:200]}"
    return "ok"


def _remove_venv(name: str) -> str:
    venv_dir = layout.component_venv_dir(name)
    if venv_dir.is_symlink():
        venv_dir.unlink()
        return f"venv symlink removed: {venv_dir}"
    if venv_dir.is_dir():
        shutil.rmtree(venv_dir)
        return f"venv removed: {venv_dir}"
    return f"venv absent (nothing to remove): {venv_dir}"


def uninstall(name: str | None = None, *, remove_all: bool = False) -> UninstallResult:
    """Uninstall one component, or the whole installation with ``--all``.

    Drop-in discipline (LY-02): only files the install flow owns are
    removed — the component manifest, its env file, its venv. Component
    data dirs are operator data and are ALWAYS preserved.
    """
    if remove_all == (name is not None):
        raise InstallError("specify either a component name or --all, not both/neither")

    components_dir = layout.components_dir()
    if not components_dir.is_dir():
        raise InstallError(
            f"no installation found ({components_dir} does not exist) — nothing to uninstall"
        )
    installation = load_installation(components_dir)  # fail-closed on a broken dir

    lines: list[str] = []

    if name is not None:
        if name not in installation:
            raise InstallError(
                f"component {name!r} is not installed (installed: "
                f"{sorted(installation)}) — refusing to uninstall"
            )
        dependents = sorted(m.name for m in installation.values() if name in m.depends_on)
        if dependents:
            raise InstallError(
                f"component {name!r} is required by {dependents} — uninstall "
                "the dependent component(s) first"
            )
        manifest_path = components_dir / f"{name}.yaml"
        if manifest_path.exists():
            manifest_path.unlink()
            lines.append(f"manifest removed: {manifest_path}")
        env_path = layout.env_file_path(name)
        if env_path.exists():
            env_path.unlink()
            lines.append(f"env file removed: {env_path}")
        lines.append(_remove_venv(name))
        lines.append(f"data dir preserved (operator data): {layout.data_dir(name)}")
    else:
        # --all: stop/disable + remove the unit, then every component file.
        disable_status = _systemctl_user(["disable", "--now", "vesma.service"])
        lines.append(f"systemctl --user disable --now vesma.service: {disable_status}")
        unit_path = _unit_path()
        if unit_path.exists():
            unit_path.unlink()
            lines.append(f"unit removed: {unit_path}")
        else:
            lines.append(f"unit absent (nothing to remove): {unit_path}")
        lines.append(f"systemctl --user daemon-reload: {_daemon_reload()}")
        for comp in sorted(installation):
            manifest_path = components_dir / f"{comp}.yaml"
            if manifest_path.exists():
                manifest_path.unlink()
                lines.append(f"manifest removed: {manifest_path}")
            env_path = layout.env_file_path(comp)
            if env_path.exists():
                env_path.unlink()
                lines.append(f"env file removed: {env_path}")
            lines.append(_remove_venv(comp))
            lines.append(f"data dir preserved (operator data): {layout.data_dir(comp)}")

    logger.info("service uninstalled: name=%s all=%s", name, remove_all)
    return UninstallResult(lines=tuple(lines))


__all__ = [
    "BUNDLED_COMPONENTS",
    "InstallError",
    "InstallResult",
    "UninstallResult",
    "install",
    "uninstall",
]
