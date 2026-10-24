"""Profile layout resolver (contract layout v1 §3 + layout v2 §3.1-3.3).

THE single resolution point for every user-profile path (layout v2 §3.1:
hardcoded paths outside this module violate the contract). The resolver is
generation-dispatched: the layout generation (specs/layout/v2 §3.1/§3.3) is
read from the install stamp and picks the path table —

- **v1.5** (legacy, normative for existing installations): the XDG spread
  ``~/.config/vesma`` / ``~/.local/share/vesma`` / ``~/.local/state/vesma``
  / ``~/.cache/vesma`` (layout v1 §3.2 tables), unchanged byte-for-byte;
- **v2** (self-contained): one ``~/.vesma/**`` tree — engine venv
  ``~/.vesma/venv``, component venvs ``~/.vesma/venvs/<name>``, component
  data ``~/.vesma/components/<name>``, cache ``~/.vesma/cache/<name>``,
  logs/history/run and the systemd canonical storage
  ``~/.vesma/systemd/user/`` inside the same tree. XDG variables other than
  ``XDG_RUNTIME_DIR`` do not participate (§3.2-b).

Generation detection (§3.3, zero-regression-first):

1. an explicit stamp (``~/.vesma/install-state.yaml``, ``generation:`` key)
   wins;
2. no stamp + v1.5 service-stack artifacts on disk → v1.5 (an existing
   installation keeps resolving the v1 tables — a canon change never
   rewrites a working install);
3. otherwise → v2 (new installations are always created in the v2 form).

The engine-venv-only entries of the v1.5 data root (the update flow's
venv, its markers) do NOT count as service-stack artifacts: the engine venv
slot is an installer-flow concern, not a layout-generation signal.

File-mode primitives (both generations): :func:`ensure_dir` — mkdir -p
followed by an EXPLICIT chmod on the leaf AND on every intermediate
vesma-owned segment (umask-independent, LY-01; cache-plane segments keep
their 0750 instead of the 0700 default); :func:`verify_dir` — mode check
for doctor-style verification.

``resolve_component_paths`` produces the placeholder expansion table
(``{config_path}`` / ``{data_dir}`` / ``{runtime_dir}`` / ``{venv_bin}``).
Runtime resolution verifies the candidate is WRITABLE with a probe (issue
#509); an unwritable or empty ``$XDG_RUNTIME_DIR`` falls back to the
generation's run dir with a WARN (§3.6 v1 / §3.2-a.2 v2 — preserved).
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import errno
import logging
import os
import re
import stat
import tempfile
from enum import StrEnum
from pathlib import Path

from vesma.service import fhs

logger = logging.getLogger("vesma.service.layout")

# ── Canonical modes (layout v1 §3.2 / v2 §3.2 tables) ─────────────────

MODE_DIR_DEFAULT = 0o700  # every vesma dir except the cache plane
MODE_DIR_CACHE = 0o750  # <cache-root>/<name>/ (both generations)
MODE_FILE_ENV = 0o600  # env files of secrets


class LayoutGenerationError(Exception):
    """A resolver slot does not exist for the detected layout generation.

    Raised fail-loud (never a silent wrong-plane path): e.g. the v1.5
    state plane has no v2 counterpart — v2 resolves logs/history/run
    individually inside ``~/.vesma``.
    """


# ── Generation model (layout v2 §3.1/§3.3) ────────────────────────────


class LayoutGeneration(StrEnum):
    """The layout generation of the local installation."""

    V1_5 = "v1.5"  # legacy XDG spread — resolves per layout v1 tables
    V2 = "v2"  # self-contained ~/.vesma tree


#: The install-state file at the shared ``~/.vesma`` root — the ONE
#: directory that exists in BOTH generations, so the stamp is readable
#: before any path resolution. The name is from the v2 reserved-name
#: registry (§3.2-e); the system-profile wave reuses the same name in ITS
#: root (``/var/lib/vesma/install-state.yaml``, §3.13). The user-side
#: schema is minimal and additive: ``generation`` (v1.5|v2), ``profile``,
#: ``updated`` — the resolver reads ONLY the ``generation:`` key,
#: defensively (an unparsable stamp means "no stamp", never a crash).
GENERATION_STAMP_NAME = "install-state.yaml"

_GENERATION_RE = re.compile(r"^\s*generation:\s*(v1\.5|v2)\s*$", re.MULTILINE)


def vesma_home() -> Path:
    """``~/.vesma/`` — the profile root (both generations share it: the
    engine store home lives there since the 6.0 home flip; v2 embeds the
    whole service stack into the same tree)."""
    return home() / ".vesma"


def install_state_path() -> Path:
    """``~/.vesma/install-state.yaml`` — the generation stamp location."""
    return vesma_home() / GENERATION_STAMP_NAME


def read_generation_stamp() -> LayoutGeneration | None:
    """The stamped generation, or ``None`` when absent/unparsable.

    Deliberately crash-proof: any read/parse problem degrades to "no
    stamp" and the artifact detection below takes over (conservative —
    legacy artifacts keep legacy resolution).
    """
    try:
        text = install_state_path().read_text(encoding="utf-8")
    except OSError:
        return None
    found = _GENERATION_RE.search(text)
    if found is None:
        return None
    return LayoutGeneration(found.group(1))


def write_generation_stamp(generation: LayoutGeneration) -> Path:
    """Stamp the layout generation (install/consolidation flows only).

    Atomic (same-dir tmp + rename), file ``0600`` under the ``0700``
    profile root. Callers must stamp the DETECTED generation — rewriting
    a stamp of an existing installation is a consolidation-step concern
    (specs/layout/v2 §9.2), never an install side effect.
    """
    ensure_dir(vesma_home(), MODE_DIR_DEFAULT)
    _atomic_write_text(
        install_state_path(),
        (
            "# vesma install-state (specs/layout/v2 §3.1/§3.3; written by"
            " `vesma service install`)\n"
            f"generation: {generation.value}\n"
            "profile: user\n"
            f'updated: "{datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")}"\n'
        ),
        MODE_FILE_ENV,
    )
    return install_state_path()


#: The ``profile:`` line of the SYSTEM install-state — the system-profile
#: dispatch key (layout v2 §3.13; ADR-0045). The state file lives at the
#: FHS slot (``/var/lib/vesma/install-state.yaml`` via
#: :func:`vesma.service.fhs.system_install_state_path`) and is written by
#: the system installer with ``profile: system``; the user stamp above
#: carries ``profile: user`` in ITS root. Only the ``profile:`` value
#: participates in layout dispatch — artifacts/ownership stay installer
#: concerns (:mod:`vesma.service.system_install`).
_SYSTEM_PROFILE_RE = re.compile(r"^\s*profile:\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*$", re.MULTILINE)

_SYSTEM_PROFILE = "system"


def read_system_profile_stamp() -> str | None:
    """``profile:`` from the SYSTEM install-state, or ``None``.

    Deliberately crash-proof and permission-blind (the
    :func:`read_generation_stamp` canon): on a user-profile machine the
    system state is absent, and for a plain user account on a
    system-profile machine it is ``EACCES`` — both mean "resolve the user
    layout" here. The LOUD unparsable-state semantics belong to the
    installer flows (:func:`vesma.service.system_install.read_system_state`),
    where acting half-blind is the hazard, not to a path resolver.
    """
    try:
        text = fhs.system_install_state_path().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # OSError covers absent/EACCES; UnicodeDecodeError (a ValueError)
        # covers a state file that is not text at all — both degrade to
        # "no stamp" for a path resolver.
        return None
    found = _SYSTEM_PROFILE_RE.search(text)
    if found is None:
        return None
    return found.group(1)


def system_profile_active() -> bool:
    """True when the SYSTEM install-state stamps ``profile: system``.

    Call-time evaluation, no caching (the module canon).
    """
    return read_system_profile_stamp() == _SYSTEM_PROFILE


def detect_generation() -> LayoutGeneration:
    """The layout generation of the local installation (see module docs).

    Call-time evaluation (no caching): XDG roots and ``HOME`` are honored
    live, matching every other resolver here — monkeypatched environments
    (tests, per-session runtime dirs) never see a stale generation.
    """
    stamped = read_generation_stamp()
    if stamped is not None:
        return stamped
    if legacy_service_artifacts():
        return LayoutGeneration.V1_5
    return LayoutGeneration.V2


#: v1.5 data-root entries that belong to the ENGINE VENV / update flow and
#: are NOT service-stack artifacts (the install.sh venv + its ownership
#: marker + the write-only user-site audit stamp). Their presence alone
#: must never classify an installation as legacy — a machine with only the
#: engine venv at the old canonical root still gets a v2 service stack.
_V15_ENGINE_ONLY_ENTRIES: frozenset[str] = frozenset({"venv", ".vesma-venv", ".user-site-install"})


def legacy_service_artifacts() -> tuple[Path, ...]:
    """The v1.5 service-stack roots present on disk (empty = none).

    Service-stack signal only: a bare engine-venv data root counts only
    when it holds entries beyond :data:`_V15_ENGINE_ONLY_ENTRIES`. Used by
    generation detection AND by doctor DR-17 (mixed-tree detection) — the
    same predicate, so detection and diagnosis can never disagree.
    """
    found: list[Path] = []
    if _v15_config_root().is_dir():
        found.append(_v15_config_root())
    if _v15_state_root().is_dir():
        found.append(_v15_state_root())
    if _v15_cache_base().is_dir():
        found.append(_v15_cache_base())
    data_root = _v15_data_root()
    if data_root.is_dir():
        try:
            entries = list(data_root.iterdir())
        except OSError:
            entries = []
        if any(entry.name not in _V15_ENGINE_ONLY_ENTRIES for entry in entries):
            found.append(data_root)
    return tuple(found)


def v2_only_service_dirs() -> tuple[Path, ...]:
    """v2 slots that ONLY a v2 service stack creates (DR-17 markers).

    Engine-adjacent ``~/.vesma`` dirs are excluded on purpose: ``run/``
    (mesh core socket), ``logs/`` (engine log files), ``cache/`` (legacy
    store-migration inventory), ``history/`` — they exist on healthy v1.5
    machines too and must never read as v2 drift.
    """
    return (
        vesma_home() / "venv",
        vesma_home() / "venvs",
        vesma_home() / "components",
        vesma_home() / "components.d",
        vesma_home() / "env.d",
        vesma_home() / "systemd" / "user",
    )


# ── Reserved-name registry (layout v2 §3.2-e) ──────────────────────────

#: Names a component may never take — the registry of the tree ROOT.
#: Additive growth only (minor); narrowing is MAJOR. Validated at install
#: time (the v1 §3.8 mechanism, extended additively).
RESERVED_TREE_NAMES: frozenset[str] = frozenset(
    {
        "venv",
        "venvs",
        "components",
        "components.d",
        "env.d",
        "cache",
        "logs",
        "history",
        "run",
        "completion",
        "systemd",
        "data",
        "vault",
        GENERATION_STAMP_NAME,
    }
)


def is_reserved_tree_name(name: str) -> bool:
    """True when ``name`` collides with the tree-root registry (§3.2-e)."""
    return name in RESERVED_TREE_NAMES


# ── Base ──────────────────────────────────────────────────────────────


def _xdg_root(var: str, default: Path) -> Path:
    """One XDG base dir; unset AND empty variable both mean the default."""
    value = os.environ.get(var, "")
    if value.strip():
        return Path(value)
    return default


def home() -> Path:
    return Path.home()


# ── v1.5 path math (private — the legacy plane, layout v1 §3.2) ───────


def _v15_config_root() -> Path:
    return _xdg_root("XDG_CONFIG_HOME", home() / ".config") / "vesma"


def _v15_data_root() -> Path:
    return _xdg_root("XDG_DATA_HOME", home() / ".local" / "share") / "vesma"


def _v15_state_root() -> Path:
    return _xdg_root("XDG_STATE_HOME", home() / ".local" / "state") / "vesma"


def _v15_cache_base() -> Path:
    return _xdg_root("XDG_CACHE_HOME", home() / ".cache") / "vesma"


def legacy_engine_venv_dir() -> Path:
    """``~/.local/share/vesma/venv`` — the v1.5-generation engine venv slot.

    Generation-INDEPENDENT by design: the self-update ownership check must
    recognize the legacy canonical slot even on a v2-resolving machine
    (the venv predates the generation); the consolidation mover will need
    the same name. Not used for resolution — see :func:`engine_venv_dir`.
    """
    return _v15_data_root() / "venv"


# ── §3.2 config plane (generation-dispatched) ─────────────────────────


def config_root() -> Path:
    """``~/.config/vesma/`` (v1.5) or ``~/.vesma/`` (v2) — 0700."""
    if detect_generation() is LayoutGeneration.V2:
        return vesma_home()
    return _v15_config_root()


def canonical_config_path() -> Path:
    """The shared config: ``vesma.yaml`` (v1.5) / ``config.yaml`` (v2 —
    the name aligned with the engine store-home canon, layout v2 §7)."""
    if detect_generation() is LayoutGeneration.V2:
        return vesma_home() / "config.yaml"
    return _v15_config_root() / "vesma.yaml"


def components_dir() -> Path:
    """Manifests drop-in dir (0700): ``components.d`` of the generation."""
    return config_root() / "components.d"


def env_dir() -> Path:
    """Env-secrets dir (0700, files 0600): ``env/`` (v1.5) / ``env.d``
    (v2 — the directory renamed by layout v2 §7)."""
    if detect_generation() is LayoutGeneration.V2:
        return vesma_home() / "env.d"
    return _v15_config_root() / "env"


def env_file_path(name: str) -> Path:
    """``<env_dir>/<name>.env`` — the canonical env-file place."""
    return env_dir() / f"{name}.env"


# ── §3.2 data plane ───────────────────────────────────────────────────


def data_root() -> Path:
    """Parent of per-component data dirs: ``~/.local/share/vesma/`` (v1.5)
    / ``~/.vesma/components/`` (v2)."""
    if detect_generation() is LayoutGeneration.V2:
        return vesma_home() / "components"
    return _v15_data_root()


def data_dir(name: str) -> Path:
    """``<data_root>/<name>/`` — component data, default cwd (0700)."""
    return data_root() / name


def engine_venv_dir() -> Path:
    """The engine venv CONTRACT slot: ``~/.local/share/vesma/venv/`` (v1.5)
    / ``~/.vesma/venv/`` (v2, §3.2) — 0700.

    The contract slot, not the running venv: where the engine ACTUALLY
    runs from is ``vesma.service.install.engine_venv()`` (sys.prefix — the
    unit's ExecStart target). Materializing the engine into the v2 slot
    belongs to the installer wave.
    """
    if detect_generation() is LayoutGeneration.V2:
        return vesma_home() / "venv"
    return _v15_data_root() / "venv"


def component_venvs_root() -> Path:
    """Parent of component venvs: ``<data_root>/venvs`` (v1.5) /
    ``~/.vesma/venvs/`` (v2)."""
    if detect_generation() is LayoutGeneration.V2:
        return vesma_home() / "venvs"
    return _v15_data_root() / "venvs"


def component_venv_dir(name: str) -> Path:
    """``<venvs_root>/<name>/`` — one venv per python child (0700)."""
    return component_venvs_root() / name


def component_venv_bin(name: str) -> Path:
    """``<venvs_root>/<name>/bin`` — the ``{venv_bin}`` target."""
    return component_venv_dir(name) / "bin"


# ── State plane ───────────────────────────────────────────────────────


def state_root() -> Path:
    """``~/.local/state/vesma/`` — the v1.5 state plane ONLY.

    v2 has NO single state root: :func:`logs_root`, :func:`history_dir`
    and :func:`run_fallback_dir` resolve individually inside
    ``~/.vesma``. A v2-resolving call fails loud — there is no honest v2
    value, and a silent legacy path would invite wrong-plane writes (DR-17
    material).
    """
    if detect_generation() is LayoutGeneration.V2:
        raise LayoutGenerationError(
            "state_root() has no v2 slot (layout v2 §3.2: logs/history/run "
            "resolve individually inside ~/.vesma) — use logs_root(), "
            "history_dir() or run_fallback_dir()"
        )
    return _v15_state_root()


def logs_root() -> Path:
    """Parent of per-component file-log dirs: ``<state_root>/logs`` (v1.5)
    / ``~/.vesma/logs/`` (v2 — component subdirs never collide with the
    engine's files in the same dir, §3.2-d)."""
    if detect_generation() is LayoutGeneration.V2:
        return vesma_home() / "logs"
    return _v15_state_root() / "logs"


def logs_dir(name: str) -> Path:
    """``<logs_root>/<name>/`` — files-under-state mode only."""
    return logs_root() / name


def history_dir() -> Path:
    """The append-only supervisor journal: ``<state_root>/history`` (v1.5)
    / ``~/.vesma/history/`` (v2)."""
    if detect_generation() is LayoutGeneration.V2:
        return vesma_home() / "history"
    return _v15_state_root() / "history"


def run_fallback_dir() -> Path:
    """Runtime fallback when ``$XDG_RUNTIME_DIR`` is empty/unwritable:
    ``<state_root>/run`` (v1.5, §3.6) / ``~/.vesma/run/`` (v2, §3.2-a.2)."""
    if detect_generation() is LayoutGeneration.V2:
        return vesma_home() / "run"
    return _v15_state_root() / "run"


# ── systemd unit locations (layout v2 §3.2-a.1) ───────────────────────


def installed_unit_path() -> Path:
    """``$XDG_CONFIG_HOME/systemd/user/vesma.service`` — where systemd
    reads the unit, BOTH generations (the OS mandate; the one closed-list
    exception of §3.2-a.1)."""
    config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(config_home) if config_home else home() / ".config"
    return base / "systemd" / "user" / "vesma.service"


def unit_canonical_path() -> Path:
    """Canonical unit storage: ``~/.vesma/systemd/user/vesma.service`` (v2;
    the install flow links it into :func:`installed_unit_path`) — v1.5 has
    no separate canonical storage, so the installed path IS canonical."""
    if detect_generation() is LayoutGeneration.V2:
        return vesma_home() / "systemd" / "user" / "vesma.service"
    return installed_unit_path()


# ── §3.6 runtime ──────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class RuntimeResolution:
    """Resolved runtime dir plus whether the §3.6 fallback was taken."""

    path: Path
    used_fallback: bool


def _dir_is_writable(path: Path) -> bool:
    """True when ``path`` admits creating + deleting a file NOW.

    Defense-in-depth (issue #509): ``os.access`` tests the DAC bits only —
    a read-only bind mount (``ProtectHome=read-only`` makes ``/run/user``
    RO inside the unit, a stale/foreign-owned directory, a full tmpfs)
    fails the write() itself. The probe is one 0-byte file created and
    unlinked under a unique name; any OSError means "not writable".
    """
    if not path.is_dir():
        return False
    probe = path / f".vesma-write-probe-{os.getpid()}"
    try:
        probe.touch(mode=0o600)
    except OSError:
        return False
    finally:
        with contextlib.suppress(OSError):
            probe.unlink()
    return True


def resolve_runtime_dir() -> RuntimeResolution:
    """``${XDG_RUNTIME_DIR}/vesma/``, or the generation's run dir + WARN.

    The ONE XDG variable the v2 profile keeps (§3.2-a.2/b). An empty
    ``XDG_RUNTIME_DIR`` takes the fallback branch (LY-11); a SET but
    unwritable one also takes it (issue #509: ``ProtectHome=read-only``
    makes ``/run/user`` read-only inside the unit while the variable is
    still set). Both take ONE structured warning line.
    """
    value = os.environ.get("XDG_RUNTIME_DIR", "")
    if value.strip():
        candidate = Path(value) / "vesma"
        try:
            _ensure_runtime_candidate(candidate)
        except OSError:
            fallback = run_fallback_dir()
            logger.warning(
                "layout: %s is not writable (%s) — runtime falls back to %s "
                "(tmpfs semantics lost; specs/layout §3.6/v2 §3.2-a.2, issue #509)",
                candidate,
                "probe failed",
                fallback,
            )
            return RuntimeResolution(fallback, used_fallback=True)
        return RuntimeResolution(candidate, used_fallback=False)
    fallback = run_fallback_dir()
    logger.warning(
        "layout: XDG_RUNTIME_DIR is empty — runtime falls back to %s "
        "(tmpfs semantics lost; specs/layout §3.6/v2 §3.2-a.2)",
        fallback,
    )
    return RuntimeResolution(fallback, used_fallback=True)


def _ensure_runtime_candidate(candidate: Path) -> None:
    """Make the candidate existing + writable or raise ``OSError``.

    Creates the ``vesma`` subdirectory when missing (0700) and runs the
    writability probe against the candidate itself — the probe's OSError
    is the honest EROFS/permission verdict.
    """
    with contextlib.suppress(FileExistsError):
        candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not _dir_is_writable(candidate):
        raise OSError(
            errno.EROFS, "runtime candidate is not writable (writability probe)", str(candidate)
        )


# ── §3.9/§3.11 cache ──────────────────────────────────────────────────


def cache_base() -> Path:
    """Parent of per-component cache dirs: ``~/.cache/vesma/`` (v1.5) /
    ``~/.vesma/cache/`` (v2)."""
    if detect_generation() is LayoutGeneration.V2:
        return vesma_home() / "cache"
    return _v15_cache_base()


def cache_dir(name: str) -> Path:
    """``<cache_base>/<name>/`` — regenerable only, never secrets (0750)."""
    return cache_base() / name


# ── §3.4 placeholder expansion table ──────────────────────────────────


@dataclasses.dataclass(frozen=True)
class ComponentPaths:
    """Per-component §3.4 resolution (the argv placeholder targets)."""

    config_path: Path
    data_dir: Path
    runtime_dir: Path
    venv_bin: Path


def resolve_component_paths(name: str) -> ComponentPaths:
    """Resolve the four §3.4 placeholder targets for one component.

    Profile-dispatched (layout v2 §3.12/§3.13; ADR-0045): when the SYSTEM
    install-state stamps ``profile: system`` every target resolves through
    the FHS manifest — :mod:`vesma.service.fhs` is the single system-path
    resolution point — giving ``/etc/vesma/config.yaml``,
    ``/var/lib/vesma/<name>``, ``/run/vesma`` (the systemd
    ``RuntimeDirectory`` slot, §3.8 — the system profile has no XDG
    fallback chain) and ``/usr/lib/vesma/venvs/<name>/bin``. Otherwise
    (user profile, no system state, or a state unreadable to this
    account) the generation tables of THIS module resolve —
    byte-for-byte as before.

    ``venv_bin`` is resolved unconditionally here (the layout path);
    whether a component HAS a venv is a manifest-level fact — expanding
    ``{venv_bin}`` for a venv-less component is refused by
    :mod:`vesma.service.placeholders` (fail-closed, §3.4).
    """
    if system_profile_active():
        return ComponentPaths(
            config_path=fhs.system_config_path(),
            data_dir=fhs.system_component_data_dir(name),
            runtime_dir=fhs.system_ipc_dir(),
            venv_bin=fhs.system_venv_component(name) / "bin",
        )
    return ComponentPaths(
        config_path=canonical_config_path(),
        data_dir=data_dir(name),
        runtime_dir=resolve_runtime_dir().path,
        venv_bin=component_venv_bin(name),
    )


# ── Unit sandbox path sets (consumers: install flow, doctor DR-07/DR-12) ──


def unit_rw_paths() -> tuple[Path, ...]:
    """ReadWritePaths of the generated unit, per generation.

    v1.5: the state plane (logs+history+run), cache, data. v2: the same
    roles as individual ``~/.vesma`` dirs — the state plane split.
    """
    if detect_generation() is LayoutGeneration.V2:
        return (
            logs_root(),
            history_dir(),
            run_fallback_dir(),
            cache_base(),
            data_root(),
        )
    return (state_root(), cache_base(), data_root())


def unit_ro_paths() -> tuple[Path, ...]:
    """ReadOnlyPaths of the generated unit: the venv trees (supply-chain
    boundary, both generations; slots per generation)."""
    return (engine_venv_dir(), component_venvs_root())


def canonical_dir_specs() -> tuple[tuple[Path, int], ...]:
    """Every canonical dir with its expected mode (doctor DR-01 table)."""
    if detect_generation() is LayoutGeneration.V2:
        return (
            (vesma_home(), MODE_DIR_DEFAULT),
            (components_dir(), MODE_DIR_DEFAULT),
            (env_dir(), MODE_DIR_DEFAULT),
            (data_root(), MODE_DIR_DEFAULT),
            (component_venvs_root(), MODE_DIR_DEFAULT),
            (logs_root(), MODE_DIR_DEFAULT),
            (history_dir(), MODE_DIR_DEFAULT),
            (run_fallback_dir(), MODE_DIR_DEFAULT),
            (unit_canonical_path().parent, MODE_DIR_DEFAULT),
            (cache_base(), MODE_DIR_CACHE),
        )
    return (
        (config_root(), MODE_DIR_DEFAULT),
        (components_dir(), MODE_DIR_DEFAULT),
        (env_dir(), MODE_DIR_DEFAULT),
        (data_root(), MODE_DIR_DEFAULT),
        (component_venvs_root(), MODE_DIR_DEFAULT),
        (state_root(), MODE_DIR_DEFAULT),
        (history_dir(), MODE_DIR_DEFAULT),
        (run_fallback_dir(), MODE_DIR_DEFAULT),
        (cache_base(), MODE_DIR_CACHE),
    )


def free_space_roots() -> tuple[tuple[str, Path], ...]:
    """Labeled volume-probe roots for DR-10 (per generation; v2 folds the
    whole stack into the one profile-root volume)."""
    if detect_generation() is LayoutGeneration.V2:
        return (("profile", vesma_home()), ("cache", cache_base()))
    return (
        ("config", config_root()),
        ("data", data_root()),
        ("state", state_root()),
        ("cache", cache_base()),
    )


# ── Mode primitives (explicit chmod — never umask-inherited) ──────────


def _canonical_roots() -> tuple[Path, ...]:
    """Every vesma-owned root the flow creates directories under.

    Resolved at call time so XDG overrides, the detected generation and
    ``$XDG_RUNTIME_DIR`` are honored (an import-time snapshot would go
    stale across monkeypatched environments and per-session runtime dirs).
    v1.5 spans four XDG planes; v2 folds everything into ``~/.vesma``.
    """
    roots: list[Path]
    if detect_generation() is LayoutGeneration.V2:
        roots = [vesma_home()]
    else:
        roots = [
            _v15_config_root(),
            _v15_data_root(),
            _v15_state_root(),
            _v15_cache_base(),
        ]
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if runtime.strip():
        roots.append(Path(runtime) / "vesma")
    return tuple(roots)


def _segment_mode(segment: Path, cache_root: Path) -> int:
    """0750 for the cache plane (root and below), 0700 elsewhere.

    The cache root is created 0750 (spec table) — a blanket-0700 walk used
    to RE-TIGHTEN it to 0700 on the first component-cache ensure_dir
    (pre-wave bug: DR-01 then WARNed "stricter than 0750" on every such
    install). Segments at-or-under the cache root keep their plane mode.
    """
    try:
        segment.relative_to(cache_root)
    except ValueError:
        return MODE_DIR_DEFAULT
    return MODE_DIR_CACHE


def _chmod_intermediate_segments(target: Path) -> None:
    """Explicitly chmod the vesma-owned span of ``target``.

    ``mkdir -p`` creates intermediate segments under the process umask;
    the layout forbids umask-inherited rights for anything the flow
    creates, so every segment from the canonical vesma root (inclusive)
    down to ``target``'s parent is re-chmodded explicitly — the leaf gets
    its own mode from :func:`ensure_dir`. Segments ABOVE the vesma root
    (the XDG base dir itself, ``$HOME``) are not ours and are never
    touched; paths outside the canonical roots are a no-op here.
    """
    cache_root = cache_base()
    for root in _canonical_roots():
        try:
            relative = target.relative_to(root)
        except ValueError:
            continue
        os.chmod(root, _segment_mode(root, cache_root))
        segment = root
        for part in relative.parts[:-1]:  # every created intermediate segment
            segment = segment / part
            os.chmod(segment, _segment_mode(segment, cache_root))
        return


def ensure_dir(path: Path, mode: int) -> Path:
    """mkdir -p + explicit chmod: leaf gets ``mode``, intermediates get
    their plane mode (0700, 0750 in the cache plane)."""
    path.mkdir(parents=True, exist_ok=True)
    _chmod_intermediate_segments(path)
    os.chmod(path, mode)
    return path


def verify_dir(path: Path, expected_mode: int) -> bool:
    """True iff ``path`` exists, is a directory and has exactly ``expected_mode``."""
    try:
        info = path.stat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == expected_mode


# ── Stamp file primitive ──────────────────────────────────────────────


def _atomic_write_text(path: Path, text: str, mode: int) -> None:
    """Same-directory tmp file + ``os.replace`` (atomic, 0600-class)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.tmp-")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
