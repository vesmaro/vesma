"""Canonical user-profile layout (contract layout v1, specs/layout/v1 §3).

Resolution helpers for the §3.2 MUST-table paths (XDG base dirs, empty
var = default) plus the two file-mode primitives the whole wave builds on:

- :func:`ensure_dir` — mkdir -p followed by an EXPLICIT chmod on the leaf
  AND on every intermediate vesma-owned segment: resulting modes are
  umask-independent (layout §3.1, LY-01);
- :func:`verify_dir` — mode check for doctor-style verification.

``resolve_component_paths`` produces the §3.4 placeholder expansion table
(``{config_path}`` / ``{data_dir}`` / ``{runtime_dir}`` / ``{venv_bin}``).
Runtime resolution verifies the candidate is WRITABLE with a probe (a
stale-socket dir, an EROFS mount or ``ProtectHome=read-only`` can leave
``$XDG_RUNTIME_DIR`` set but unwritable — ``os.access`` cannot see mount
flags); an unwritable or empty ``$XDG_RUNTIME_DIR`` falls back to
``~/.local/state/vesma/run/`` (§3.6) with a WARN. The system profile is
deferred (spec §3.3, v2) and deliberately absent here.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger("vesma.service.layout")

# ── Canonical modes (layout §3.2 table) ───────────────────────────────

MODE_DIR_DEFAULT = 0o700  # every vesma dir except the cache root
MODE_DIR_CACHE = 0o750  # ~/.cache/vesma/<name>/
MODE_FILE_ENV = 0o600  # ~/.config/vesma/env/<name>.env


def _xdg_root(var: str, default: Path) -> Path:
    """One XDG base dir; unset AND empty variable both mean the default."""
    value = os.environ.get(var, "")
    if value.strip():
        return Path(value)
    return default


def home() -> Path:
    return Path.home()


# ── §3.2 config plane ─────────────────────────────────────────────────


def config_root() -> Path:
    """``~/.config/vesma/`` (0700)."""
    return _xdg_root("XDG_CONFIG_HOME", home() / ".config") / "vesma"


def canonical_config_path() -> Path:
    """``~/.config/vesma/vesma.yaml`` — the shared config (``{config_path}``)."""
    return config_root() / "vesma.yaml"


def components_dir() -> Path:
    """``~/.config/vesma/components.d/`` — manifests drop-in dir (0700)."""
    return config_root() / "components.d"


def env_dir() -> Path:
    """``~/.config/vesma/env/`` — env files of secrets (0700, files 0600)."""
    return config_root() / "env"


def env_file_path(name: str) -> Path:
    """``~/.config/vesma/env/<name>.env`` — canonical env-file place."""
    return env_dir() / f"{name}.env"


# ── §3.2 data plane ───────────────────────────────────────────────────


def data_root() -> Path:
    """``~/.local/share/vesma/`` (parent of per-component data dirs)."""
    return _xdg_root("XDG_DATA_HOME", home() / ".local" / "share") / "vesma"


def data_dir(name: str) -> Path:
    """``~/.local/share/vesma/<name>/`` — component data, default cwd (0700)."""
    return data_root() / name


def engine_venv_dir() -> Path:
    """``~/.local/share/vesma/venv/`` — the engine (supervisor) venv (0700)."""
    return data_root() / "venv"


def component_venv_dir(name: str) -> Path:
    """``~/.local/share/vesma/venvs/<name>/`` — one venv per python child (0700)."""
    return data_root() / "venvs" / name


def component_venv_bin(name: str) -> Path:
    """``~/.local/share/vesma/venvs/<name>/bin`` — the ``{venv_bin}`` target."""
    return component_venv_dir(name) / "bin"


# ── §3.2/§3.6/§3.7 state plane ────────────────────────────────────────


def state_root() -> Path:
    """``~/.local/state/vesma/``."""
    return _xdg_root("XDG_STATE_HOME", home() / ".local" / "state") / "vesma"


def logs_dir(name: str) -> Path:
    """``~/.local/state/vesma/logs/<name>/`` — files-under-state mode only."""
    return state_root() / "logs" / name


def history_dir() -> Path:
    """``~/.local/state/vesma/history/`` — append-only supervisor journal."""
    return state_root() / "history"


def run_fallback_dir() -> Path:
    """``~/.local/state/vesma/`` — runtime fallback when ``$XDG_RUNTIME_DIR``
    is empty OR unwritable (layout §3.6)."""
    return state_root() / "run"


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
    """``${XDG_RUNTIME_DIR}/vesma/``, or the state/run fallback + WARN.

    An empty ``XDG_RUNTIME_DIR`` takes the fallback branch (LY-11). A SET
    but unwritable ``XDG_RUNTIME_DIR`` also takes the fallback (issue
    #509: ``ProtectHome=read-only`` makes ``/run/user`` read-only inside
    the unit while the variable is still set — binding the control socket
    there is a fatal EROFS). Both take ONE structured warning line; the
    fallback restores tmpfs-less but WORKABLE semantics instead of a
    crash loop.
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
                "(tmpfs semantics lost; specs/layout/v1 §3.6, issue #509)",
                candidate,
                "probe failed",
                fallback,
            )
            return RuntimeResolution(fallback, used_fallback=True)
        return RuntimeResolution(candidate, used_fallback=False)
    fallback = run_fallback_dir()
    logger.warning(
        "layout: XDG_RUNTIME_DIR is empty — runtime falls back to %s "
        "(tmpfs semantics lost; specs/layout/v1 §3.6)",
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


# ── §3.9 cache ────────────────────────────────────────────────────────


def cache_base() -> Path:
    """``~/.cache/vesma/`` — parent of per-component cache dirs."""
    return _xdg_root("XDG_CACHE_HOME", home() / ".cache") / "vesma"


def cache_dir(name: str) -> Path:
    """``~/.cache/vesma/<name>/`` — regenerable only, never secrets (0750)."""
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

    ``venv_bin`` is resolved unconditionally here (the layout path);
    whether a component HAS a venv is a manifest-level fact — expanding
    ``{venv_bin}`` for a venv-less component is refused by
    :mod:`vesma.service.placeholders` (fail-closed, §3.4).
    """
    return ComponentPaths(
        config_path=canonical_config_path(),
        data_dir=data_dir(name),
        runtime_dir=resolve_runtime_dir().path,
        venv_bin=component_venv_bin(name),
    )


# ── Mode primitives (explicit chmod — never umask-inherited) ──────────


def _canonical_roots() -> tuple[Path, ...]:
    """Every vesma-owned root the flow creates directories under.

    Resolved at call time so XDG overrides and ``$XDG_RUNTIME_DIR`` are
    honored (an import-time snapshot would go stale across monkeypatched
    environments and per-session runtime dirs).
    """
    roots = [config_root(), data_root(), state_root(), cache_base(), run_fallback_dir()]
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if runtime.strip():
        roots.append(Path(runtime) / "vesma")
    return tuple(roots)


def _chmod_intermediate_segments(target: Path) -> None:
    """Explicitly chmod the vesma-owned span of ``target`` to 0700.

    ``mkdir -p`` creates intermediate segments under the process umask;
    layout §3.1 forbids umask-inherited rights for anything the flow
    creates, so every segment from the canonical vesma root (inclusive)
    down to ``target``'s parent is re-chmodded explicitly — the leaf gets
    its own mode from :func:`ensure_dir`. Segments ABOVE the vesma root
    (the XDG base dir itself, ``$HOME``) are not ours and are never
    touched; paths outside the canonical roots are a no-op here.
    """
    for root in _canonical_roots():
        try:
            relative = target.relative_to(root)
        except ValueError:
            continue
        os.chmod(root, MODE_DIR_DEFAULT)
        segment = root
        for part in relative.parts[:-1]:  # every created intermediate segment
            segment = segment / part
            os.chmod(segment, MODE_DIR_DEFAULT)
        return


def ensure_dir(path: Path, mode: int) -> Path:
    """mkdir -p + explicit chmod: leaf gets ``mode``, intermediates get 0700.

    Layout §3.1: rights of EVERY directory the flow creates are set
    explicitly at creation, never inherited from umask — so intermediate
    ``mkdir -p`` segments (e.g. ``~/.local/state/vesma/``) are chmod'ed
    too, not just the leaf.
    """
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
