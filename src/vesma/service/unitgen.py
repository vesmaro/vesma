"""systemd user-unit generation (contract service-lifecycle v1 §3.6).

Pure generator: paths in → unit text out, NO filesystem I/O (path math
only). The install flow (:mod:`vesma.service.install`) resolves the
canonical layout roots and writes the result; doctor DR-07/DR-12/DR-13
re-generate and diff against the installed unit.

Normative content, fixed by the spec (do not "improve" here):

- SL-17 MUST table — exactly one unit over the supervisor process;
  ``ExecStart`` is ONE static line ``<venv>/bin/vesma service run`` and
  NEVER contains component argv or ``sh -c``; ``ExecStop`` is NEVER
  generated (the default SIGTERM IS the contract stop);
- SL-18 hardening block — full set, with ``SystemCallFilter`` emitted as a
  COMMENTED Tier-B line (activated only after the bare/distrobox/podman
  smoke matrix) and ``MemoryDenyWriteExecute`` deliberately ABSENT (it
  breaks CPython native extensions — the spec forbids its silent
  re-addition);
- container downgrade — ONLY the filesystem allowlist
  ``{ProtectSystem, ProtectHome, ReadOnlyPaths, ReadWritePaths,
  PrivateTmp}`` may be downgraded, only loudly (marker comment + install
  report); any other directive name is a :class:`UnitGenerationError`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Collection, Mapping
from pathlib import Path

#: Version marker written into the header; the normative source is the
#: specs repo (service-lifecycle ``1.0.0-draft.2``).
GENERATOR_VERSION = "service-lifecycle v1 (1.0.0-draft.2)"

# ── SL-17 contract constants (single source of truth) ─────────────────
# The supervisor wave (W3) imports THESE values — the unit template and
# the supervisor's internal stop/restart budgets must never drift apart.
#: ``TimeoutStopSec=`` — inner child-stop budget + headroom (SL §3.5).
TIMEOUT_STOP_SEC = 90
#: ``RestartSec=`` — pause between unit restarts.
RESTART_SEC = "5s"
#: ``StartLimitIntervalSec=`` — the unit crash-loop detection window.
START_LIMIT_INTERVAL_SEC = 300
#: ``StartLimitBurst=`` — failures within the window before the unit stops.
START_LIMIT_BURST = 5
#: ``KillSignal=`` — the contract stop signal (SL §3.5).
KILL_SIGNAL = "SIGTERM"
#: ``KillMode=`` — SIGTERM to the main, cgroup SIGKILL as the backstop.
KILL_MODE = "mixed"
#: ``Restart=`` — supervisor death (incl. in-process core) restarts the unit.
RESTART_POLICY = "on-failure"
#: ``Type=`` — start succeeded only after a successful exec().
UNIT_TYPE = "exec"

#: SL §3.6 — the EXACT set of hardening directives a container
#: environment may downgrade. Anything else raises.
DOWNGRADE_ALLOWED: frozenset[str] = frozenset(
    {"ProtectSystem", "ProtectHome", "ReadOnlyPaths", "ReadWritePaths", "PrivateTmp"}
)

#: Machine-readable marker prefix in the generated unit (DR-13 parses it).
DOWNGRADE_MARKER = "# vesma:downgraded="

_DESCRIPTION = (
    "VESMA supervisor (single unit over the vesma process; systemd never knows about children)"
)
_DOCUMENTATION_URL = "https://github.com/vesmaro/vesma-specs/tree/main/specs/service-lifecycle/v1"


class UnitGenerationError(Exception):
    """Unit generation refused — e.g. a downgrade outside the allowlist."""


# ── Container detection ───────────────────────────────────────────────


def container_detect(
    env: Mapping[str, str] | None = None,
    root: Path = Path(os.sep),
) -> bool:
    """True when the process appears to run inside a container/box.

    Signals (any hit): ``DISTROBOX_ENTER_ENV`` / ``CONTAINER_ID`` env
    variables (non-empty) or one of the well-known container markers
    ``/.dockerenv`` and ``/run/.containerenv``. ``env`` defaults to
    ``os.environ``; ``root`` is injectable for tests.
    """
    environment = os.environ if env is None else env
    for var in ("DISTROBOX_ENTER_ENV", "CONTAINER_ID"):
        if environment.get(var, "").strip():
            return True
    return (root / ".dockerenv").exists() or (root / "run" / ".containerenv").exists()


# ── Path rendering ────────────────────────────────────────────────────


def render_path(path: Path, home: Path) -> str:
    """Render ``path`` for the unit: ``%h/…`` under home, absolute otherwise.

    Pure string math — no ``resolve()`` (no I/O, no symlink following).
    """
    try:
        relative = path.relative_to(home)
    except ValueError:
        return str(path)
    if str(relative) == ".":
        return "%h"
    return "%h/" + relative.as_posix()


def _quote_unit_value(value: str) -> str:
    """Quote one rendered path for a whitespace-separated unit value.

    systemd splits directive values on whitespace, so a path containing a
    space is emitted double-quoted, with ``"`` and ``\\`` escaped (the
    shell-like quoting systemd applies to unit directive values). Paths
    without spaces stay bare — the generated unit must not change for the
    common case.
    """
    if " " not in value:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _render_path_list(paths: Collection[Path], home: Path) -> str:
    return " ".join(_quote_unit_value(render_path(p, home)) for p in paths)


# ── Generator ─────────────────────────────────────────────────────────


def _validate_downgrades(downgraded: Collection[str]) -> list[str]:
    """Reject any downgrade outside the filesystem allowlist (SL §3.6)."""
    requested = sorted(set(downgraded))
    illegal = [name for name in requested if name not in DOWNGRADE_ALLOWED]
    if illegal:
        raise UnitGenerationError(
            f"container downgrade requested for non-filesystem directive(s) "
            f"{illegal} — only {sorted(DOWNGRADE_ALLOWED)} may be downgraded "
            "(loud documented downgrades only, specs/service-lifecycle/v1 §3.6)"
        )
    return requested


def _directive_line(name: str, value: str, downgraded: Collection[str]) -> str:
    """One hardening line; a downgraded directive stays visible, commented."""
    if name in downgraded:
        return (
            f"# {name}={value}\n"
            f"# ^ vesma:downgraded — container environment (no real systemd "
            f"host); loud documented downgrade, specs/service-lifecycle/v1 §3.6"
        )
    return f"{name}={value}"


def generate(
    home: Path,
    engine_venv: Path,
    read_write_paths: Collection[Path],
    read_only_paths: Collection[Path],
    downgraded: Collection[str] = (),
) -> str:
    """Generate the user-profile unit text (SL-17 + SL-18).

    ``engine_venv`` is the venv the engine runs from (``ExecStart`` points
    at its ``bin/vesma``); ``read_write_paths`` / ``read_only_paths`` are
    the layout state/cache/data roots and the venv trees respectively.
    ``downgraded`` may name ONLY filesystem-allowlist directives.
    """
    names = _validate_downgrades(downgraded)

    exec_start = f"{render_path(engine_venv, home)}/bin/vesma service run"
    header = [
        "# Generated by `vesma service install` — service-lifecycle v1 "
        "(specs/service-lifecycle/v1).",
        "# Generated, do NOT edit by hand: regeneration overwrites this file.",
        "# Regenerate with: vesma service install",
        f"# vesma:generator={GENERATOR_VERSION}",
    ]
    if names:
        header.append(f"{DOWNGRADE_MARKER}{','.join(names)}")

    hardening: list[tuple[str, str]] = [
        ("NoNewPrivileges", "true"),
        ("ProtectSystem", "strict"),
        ("ReadWritePaths", _render_path_list(read_write_paths, home)),
        ("ReadOnlyPaths", _render_path_list(read_only_paths, home)),
        ("ProtectHome", "read-only"),
        ("PrivateTmp", "yes"),
        ("PrivateDevices", "yes"),
        ("ProtectKernelTunables", "yes"),
        ("ProtectKernelModules", "yes"),
        ("ProtectKernelLogs", "yes"),
        ("ProtectControlGroups", "yes"),
        ("RestrictSUIDSGID", "yes"),
        ("LockPersonality", "yes"),
        ("RestrictRealtime", "yes"),
        ("CapabilityBoundingSet", ""),
        ("RestrictAddressFamilies", "AF_UNIX AF_NETLINK AF_INET AF_INET6"),
    ]

    lines = [*header, "", "[Unit]"]
    lines.append(f"Description={_DESCRIPTION}")
    lines.append(f"Documentation={_DOCUMENTATION_URL}")
    lines.append(f"StartLimitIntervalSec={START_LIMIT_INTERVAL_SEC}")
    lines.append(f"StartLimitBurst={START_LIMIT_BURST}")
    lines += ["", "[Service]"]
    lines.append(f"Type={UNIT_TYPE}")
    # Issue #509: ProtectHome=read-only makes /run/user RO inside the unit
    # namespace (systemd.exec(5)) — the canonical control-socket dir
    # ${XDG_RUNTIME_DIR}/vesma must be systemd-CREATED writable:
    # RuntimeDirectory= works even under ProtectSystem=strict +
    # ProtectHome=read-only (it whitelists the dir it creates).
    lines.append("RuntimeDirectory=vesma")
    lines.append("RuntimeDirectoryMode=0700")
    lines.append(f"ExecStart={exec_start}")
    lines += [
        "# ExecStop is intentionally NOT generated: the default SIGTERM to the main",
        "# process IS the contract stop (reverse-topological graceful stop of children",
        "# performed inside the supervisor; children are invisible to systemd).",
    ]
    lines.append(f"KillSignal={KILL_SIGNAL}")
    lines.append(f"KillMode={KILL_MODE}")
    lines.append(f"TimeoutStopSec={TIMEOUT_STOP_SEC}")
    lines.append(f"Restart={RESTART_POLICY}")
    lines.append(f"RestartSec={RESTART_SEC}")
    lines.append("StandardOutput=journal")
    lines.append("StandardError=journal")
    lines += ["", "# --- hardening block (service-lifecycle v1, MUST) ---"]
    for name, value in hardening:
        lines.append(_directive_line(name, value, names))
    lines += [
        "# MemoryDenyWriteExecute is deliberately ABSENT: it breaks CPython native",
        '# extensions (see spec section 3.6, "MemoryDenyWriteExecute"); the spec',
        "# forbids re-adding it silently.",
        "# SystemCallFilter=@system-service",
        "# ^ Tier B: uncommented by the generator only after the smoke matrix",
        "#   (bare / distrobox / podman); until then it stays commented with this",
        "#   loud marker — no silent downgrades.",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


# ── System-profile unit (layout v2 §3.4/§3.8; ADR-0045 rollout wave 2) ─


#: The system-profile service account (created by the sysusers.d
#: fragment; layout v2 §3.4).
SYSTEM_USER = "vesma"
SYSTEM_GROUP = "vesma"


def _render_absolute_path_list(paths: Collection[Path]) -> str:
    """Whitespace-joined absolute path list for a system unit directive.

    Unlike :func:`_render_path_list` there is NO ``%h`` mapping — a
    system unit has no user home; every path renders as-is (per-path
    quoted, systemd directive-value quoting).
    """
    return " ".join(_quote_unit_value(str(p)) for p in paths)


def generate_system_unit(
    engine_venv: Path,
    read_write_paths: Collection[Path],
    read_only_paths: Collection[Path],
    optional_read_write_paths: Collection[Path] = (),
    *,
    config_path: Path,
    working_directory: Path,
    user: str = SYSTEM_USER,
    group: str = SYSTEM_GROUP,
) -> str:
    """Generate the SYSTEM supervisor unit (layout v2 §3.4/§3.8).

    Same SL-17/SL-18 skeleton as the user unit, profile-adjusted:

    - ``User=``/``Group=`` — the vesma service account (sysusers.d);
    - ``RuntimeDirectoryMode=0750`` — the system IPC dir ``/run/vesma``
      (§3.8; the user unit keeps 0700);
    - ``WantedBy=multi-user.target`` — a system unit, not a user session;
    - ``Environment="PYTHONNOUSERSITE=1"`` — the venv discipline MUST
      (layout v2 §3.10), spelled on the unit for the system profile;
    - ``Environment="VESMA_CONFIG=<config_path>"`` + ``WorkingDirectory=
      <working_directory>`` — the config WIRING (card ``4c8a0cbe``): a
      system unit runs WITHOUT a user session's environment, so without
      these the engine resolved zero-config and silently served an empty
      auto-store under the account's ``$HOME/.vesma`` while health read
      ok (the 2026-10-28 field incident). ``config_path`` is the FHS
      conffile (``/etc/vesma/config.yaml``), ``working_directory`` the
      FHS data root (``/var/lib/vesma`` — in ``ReadWritePaths``, so the
      chdir succeeds under ``ProtectSystem=strict``);
    - ``optional_read_write_paths`` render with the systemd ``-`` prefix
      (a missing path is not an error) — the opt-in ``/var/log/vesma``
      tree (§3.9) must not brick unit start when the operator never
      enabled file logs;
    - paths render ABSOLUTE (no ``%h`` — a system unit has no user
      home; the service account's home is the data root).

    Pure generator: no filesystem I/O. DR-18 (installed-unit ↔ canon
    drift) belongs to a later rollout wave and is deliberately absent.
    """
    exec_start = f"{engine_venv}/bin/vesma service run"
    if not exec_start.startswith("/"):
        raise UnitGenerationError(f"system unit ExecStart must be absolute, got {exec_start!r}")

    header = [
        "# Generated by `vesma service install --system` — service-lifecycle v1",
        "# (specs/service-lifecycle/v1) + specs/layout/v2 §3.4 (system profile).",
        "# Generated, do NOT edit by hand: regeneration overwrites this file.",
        f"# vesma:generator={GENERATOR_VERSION}",
        "# vesma:profile=system",
    ]

    lines = [*header, "", "[Unit]"]
    lines.append(f"Description={_DESCRIPTION}")
    lines.append(f"Documentation={_DOCUMENTATION_URL}")
    lines.append(f"StartLimitIntervalSec={START_LIMIT_INTERVAL_SEC}")
    lines.append(f"StartLimitBurst={START_LIMIT_BURST}")
    lines += ["", "[Service]"]
    lines.append(f"Type={UNIT_TYPE}")
    lines.append(f"User={user}")
    lines.append(f"Group={group}")
    lines.append('Environment="PYTHONNOUSERSITE=1"')
    # Config wiring (card 4c8a0cbe): the engine's config chain reads
    # VESMA_CONFIG first — without it a system unit starts zero-config
    # and silently serves an empty auto-store under the account home.
    lines.append(f'Environment="VESMA_CONFIG={config_path}"')
    lines.append("RuntimeDirectory=vesma")
    # layout v2 §3.8: system IPC dir 0750 (the user unit keeps 0700).
    lines.append("RuntimeDirectoryMode=0750")
    # Working directory = the data root (card 4c8a0cbe): pins the
    # engine's ./config.yaml + relative-path resolution onto the FHS
    # store home instead of systemd's default CWD (/). The data root is
    # in ReadWritePaths, so the chdir succeeds under ProtectSystem=strict.
    lines.append(f"WorkingDirectory={working_directory}")
    lines.append(f"ExecStart={exec_start}")
    lines += [
        "# ExecStop is intentionally NOT generated: the default SIGTERM to the main",
        "# process IS the contract stop (reverse-topological graceful stop of children",
        "# performed inside the supervisor; children are invisible to systemd).",
    ]
    lines.append(f"KillSignal={KILL_SIGNAL}")
    lines.append(f"KillMode={KILL_MODE}")
    lines.append(f"TimeoutStopSec={TIMEOUT_STOP_SEC}")
    lines.append(f"Restart={RESTART_POLICY}")
    lines.append(f"RestartSec={RESTART_SEC}")
    lines.append("StandardOutput=journal")
    lines.append("StandardError=journal")
    lines += ["", "# --- hardening block (service-lifecycle v1, MUST) ---"]
    optional_list = " ".join(f"-{p}" for p in optional_read_write_paths)
    read_write = " ".join(
        part for part in (_render_absolute_path_list(read_write_paths), optional_list) if part
    )
    for name, value in (
        ("NoNewPrivileges", "true"),
        ("ProtectSystem", "strict"),
        # Rendered from the FHS manifest paths by the installer —
        # absolute, never %h.
        ("ReadWritePaths", read_write),
        ("ReadOnlyPaths", _render_absolute_path_list(read_only_paths)),
        ("ProtectHome", "read-only"),
        ("PrivateTmp", "yes"),
        ("PrivateDevices", "yes"),
        ("ProtectKernelTunables", "yes"),
        ("ProtectKernelModules", "yes"),
        ("ProtectKernelLogs", "yes"),
        ("ProtectControlGroups", "yes"),
        ("RestrictSUIDSGID", "yes"),
        ("LockPersonality", "yes"),
        ("RestrictRealtime", "yes"),
        ("CapabilityBoundingSet", ""),
        ("RestrictAddressFamilies", "AF_UNIX AF_NETLINK AF_INET AF_INET6"),
    ):
        lines.append(f"{name}={value}")
    lines += [
        "# MemoryDenyWriteExecute is deliberately ABSENT: it breaks CPython native",
        '# extensions (see spec section 3.6, "MemoryDenyWriteExecute"); the spec',
        "# forbids re-adding it silently.",
        "# SystemCallFilter=@system-service",
        "# ^ Tier B: uncommented by the generator only after the smoke matrix",
        "#   (bare / distrobox / podman); until then it stays commented with this",
        "#   loud marker — no silent downgrades.",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ]
    return "\n".join(lines)


#: The user-profile instance TEMPLATE unit name (ADR-0049 wave 1):
#: ``systemctl start vesma@<id>`` instantiates it; the DEFAULT instance
#: stays ``vesma.service`` byte-for-byte.
TEMPLATE_UNIT_NAME = "vesma@.service"

_TEMPLATE_DESCRIPTION = "VESMA supervisor (instance %i; systemd template unit)"

_TEMPLATE_OPERATORS_NOTE = (
    "# Per-instance config is OPERATOR-owned (ADR-0049 §3: orchestration of N\n"
    "# instances belongs to the launch layer): give the instance its own store\n"
    "# home + api port via a drop-in, e.g.\n"
    "#   systemctl --user edit vesma@<id>.service\n"
    "#     [Service]\n"
    '#     Environment="VESMA_CONFIG=%h/.vesma/instances/<id>/config.yaml"\n'
    "# The template itself carries only the instance identity (the engine's\n"
    "# config chain reads VESMA_INSTANCE_ID for endpoint namespacing)."
)


def generate_template_unit(
    home: Path,
    engine_venv: Path,
    read_write_paths: Collection[Path],
    read_only_paths: Collection[Path],
    downgraded: Collection[str] = (),
) -> str:
    """Generate the ``vesma@.service`` TEMPLATE unit (ADR-0049 wave 1).

    Mirrors :func:`generate` (SL-17/SL-18) with the instance deltas — the
    default unit's text is deliberately NOT touched (an existing install
    must not show doctor drift until it re-installs; backward
    compatibility is contract §3):

    - ``Environment="VESMA_INSTANCE_ID=%i"`` — the systemd template
      specifier becomes the runtime identity (the engine validates it,
      derives ``instances/<id>/control.sock`` and reports it in the
      hello frame / API header / registry);
    - ``RuntimeDirectory=vesma/instances/%i`` — the per-instance runtime
      dir (systemd creates the ``vesma`` parents; nested entries are
      supported since systemd 240), mode 0700 as the user unit;
    - ``SyslogIdentifier=vesma@%i`` — journald namespacing:
      ``journalctl --user -t 'vesma@<id>'`` (the default unit keeps its
      argv0-derived ``vesma`` identifier — unchanged behavior).

    The DEFAULT id (``vesma@default``) is NOT special-cased here: the
    engine treats ``VESMA_INSTANCE_ID=default`` exactly like an unset
    variable (canonical paths), so the template degrades safely.
    """
    names = _validate_downgrades(downgraded)

    exec_start = f"{render_path(engine_venv, home)}/bin/vesma service run"
    header = [
        "# Generated by `vesma service install` — service-lifecycle v1 "
        "(specs/service-lifecycle/v1).",
        "# Instance TEMPLATE unit (ADR-0049 wave 1): instantiate with "
        "`systemctl --user start vesma@<id>`.",
        "# Generated, do NOT edit by hand: regeneration overwrites this file.",
        "# Regenerate with: vesma service install",
        f"# vesma:generator={GENERATOR_VERSION}",
        "# vesma:template=user-instance",
        *_TEMPLATE_OPERATORS_NOTE.splitlines(),
    ]

    lines = [*header, "", "[Unit]"]
    lines.append(f"Description={_TEMPLATE_DESCRIPTION}")
    lines.append(f"Documentation={_DOCUMENTATION_URL}")
    lines.append(f"StartLimitIntervalSec={START_LIMIT_INTERVAL_SEC}")
    lines.append(f"StartLimitBurst={START_LIMIT_BURST}")
    lines += ["", "[Service]"]
    lines.append(f"Type={UNIT_TYPE}")
    # Issue #509 rationale carries over: ProtectHome=read-only makes
    # /run/user RO inside the unit namespace — RuntimeDirectory= is what
    # systemd-CREATEs the writable per-instance socket dir (parents
    # included, nested entries since systemd 240).
    lines.append("RuntimeDirectory=vesma/instances/%i")
    lines.append("RuntimeDirectoryMode=0700")
    lines.append('Environment="VESMA_INSTANCE_ID=%i"')
    lines.append("SyslogIdentifier=vesma@%i")
    lines.append(f"ExecStart={exec_start}")
    lines += [
        "# ExecStop is intentionally NOT generated: the default SIGTERM to the main",
        "# process IS the contract stop (reverse-topological graceful stop of children",
        "# performed inside the supervisor; children are invisible to systemd).",
    ]
    lines.append(f"KillSignal={KILL_SIGNAL}")
    lines.append(f"KillMode={KILL_MODE}")
    lines.append(f"TimeoutStopSec={TIMEOUT_STOP_SEC}")
    lines.append(f"Restart={RESTART_POLICY}")
    lines.append(f"RestartSec={RESTART_SEC}")
    lines.append("StandardOutput=journal")
    lines.append("StandardError=journal")
    lines += ["", "# --- hardening block (service-lifecycle v1, MUST) ---"]
    for name, value in (
        ("NoNewPrivileges", "true"),
        ("ProtectSystem", "strict"),
        ("ReadWritePaths", _render_path_list(read_write_paths, home)),
        ("ReadOnlyPaths", _render_path_list(read_only_paths, home)),
        ("ProtectHome", "read-only"),
        ("PrivateTmp", "yes"),
        ("PrivateDevices", "yes"),
        ("ProtectKernelTunables", "yes"),
        ("ProtectKernelModules", "yes"),
        ("ProtectKernelLogs", "yes"),
        ("ProtectControlGroups", "yes"),
        ("RestrictSUIDSGID", "yes"),
        ("LockPersonality", "yes"),
        ("RestrictRealtime", "yes"),
        ("CapabilityBoundingSet", ""),
        ("RestrictAddressFamilies", "AF_UNIX AF_NETLINK AF_INET AF_INET6"),
    ):
        lines.append(_directive_line(name, value, names))
    lines += [
        "# MemoryDenyWriteExecute is deliberately ABSENT: it breaks CPython native",
        '# extensions (see spec section 3.6, "MemoryDenyWriteExecute"); the spec',
        "# forbids re-adding it silently.",
        "# SystemCallFilter=@system-service",
        "# ^ Tier B: uncommented by the generator only after the smoke matrix",
        "#   (bare / distrobox / podman); until then it stays commented with this",
        "#   loud marker — no silent downgrades.",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


# ── Unit parsing (doctor DR-07/DR-12/DR-13 helpers) ───────────────────

_ACTIVE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*)=(.*)$")
_COMMENTED_RE = re.compile(r"^#\s*([A-Za-z][A-Za-z0-9]*)=(.*)$")


def parse_downgrade_marker(text: str) -> list[str]:
    """Names from the ``# vesma:downgraded=`` marker; empty when absent."""
    for line in text.splitlines():
        if line.startswith(DOWNGRADE_MARKER):
            names = line[len(DOWNGRADE_MARKER) :].strip()
            return [n for n in names.split(",") if n]
    return []


def parse_unit(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """Split a unit into (active directives, commented-out directives).

    Later occurrences of the same directive win (systemd semantics for
    non-list directives); comment lines like ``# ProtectSystem=strict``
    land in the commented map so DR-13 can detect out-of-band tampering.
    """
    active: dict[str, str] = {}
    commented: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        active_match = _ACTIVE_RE.match(line)
        if active_match:
            active[active_match.group(1)] = active_match.group(2).strip()
            continue
        commented_match = _COMMENTED_RE.match(line)
        if commented_match:
            commented[commented_match.group(1)] = commented_match.group(2).strip()
    return active, commented
