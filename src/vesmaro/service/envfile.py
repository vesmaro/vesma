"""Fail-closed env_file loader (contract component-manifest v1 §3.5, code ENV_FILE_UNSAFE).

The env file is the ONLY place secrets may live. Every load condition is
fail-closed — file exists, mode is exactly 0600, owner is the current
user, path lies outside the manifests directory — and ANY violation is a
:class:`ManifestError` with a ready fix command, never a warning
(layout §3.5, LY-03; threat model: symlinked/mis-owned/misplaced secret
files are tamper vectors).

Parsing is dotenv-lite: ``KEY=VALUE`` lines, ``#`` comments, blank lines
skipped; a malformed line is a violation (a secrets file with unparseable
content must not be silently truncated).
"""

from __future__ import annotations

import getpass
import os
import re
import stat
from pathlib import Path

from vesmaro.service.errors import ENV_FILE_UNSAFE, ManifestError
from vesmaro.service.layout import components_dir

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Reserved keys — never settable through an env file (see the parse loop).
_RESERVED_KEYS = frozenset({"PATH"})


def _fix_chmod(path: Path) -> str:
    return f"chmod 600 {path}"


def _fix_chown(path: Path) -> str:
    return f"chown {getpass.getuser()} {path}"


def _fix_move() -> str:
    return (
        "move the env file to ~/.config/vesma/env/<name>.env (canonical place, "
        "layout §3.5) and update the manifest's env.env_file"
    )


def _is_inside(child: Path, parent: Path) -> bool:
    child_real = Path(os.path.realpath(child))
    parent_real = Path(os.path.realpath(parent))
    return child_real == parent_real or parent_real in child_real.parents


def _check_safety(path: Path, manifests_dir: Path | None) -> None:
    # Symlink refusal BEFORE any following stat (layout §8, LY-02: a
    # symlinked "env file" can be re-pointed at an unrelated user-owned
    # 0600 file — the lstat check below never follows the link, so the
    # target's benign mode/owner cannot launder the path through).
    if path.is_symlink():
        raise ManifestError(
            ENV_FILE_UNSAFE,
            "$.launch.env.env_file",
            f"env_file {path} is a symlink — a symlinked secrets file is a "
            "tamper vector (specs/layout/v1 §8)",
            fix_hint=f"remove the symlink and place a regular 0600 file at {path}",
        )
    if not path.exists():
        raise ManifestError(
            ENV_FILE_UNSAFE,
            "$.launch.env.env_file",
            f"env_file {path} is declared but missing (fail-closed start refusal)",
            fix_hint=f"create it ({_fix_chmod(path)}) or drop the env_file declaration",
        )
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ManifestError(
            ENV_FILE_UNSAFE,
            "$.launch.env.env_file",
            f"env_file {path} is not a regular file",
            fix_hint=f"place a regular 0600 file at {path}",
        )
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ManifestError(
            ENV_FILE_UNSAFE,
            "$.launch.env.env_file",
            f"env_file {path} has mode {stat.S_IMODE(info.st_mode):04o} (must be exactly 0600)",
            fix_hint=_fix_chmod(path),
        )
    if info.st_uid != os.geteuid():
        raise ManifestError(
            ENV_FILE_UNSAFE,
            "$.launch.env.env_file",
            f"env_file {path} is owned by uid {info.st_uid}, not by the "
            f"supervisor user (uid {os.geteuid()})",
            fix_hint=f"{_fix_chown(path)} (owner must be {getpass.getuser()})",
        )
    # Placement: outside the manifests directory (CM §3.5) — both the
    # directory of the declaring manifest and the canonical components.d
    # are refused. The canonical dir resolves at call time (XDG-aware,
    # never cached at import).
    for label, base in (
        ("the declaring manifest's directory", manifests_dir),
        ("the canonical manifests directory", components_dir()),
    ):
        if base is not None and _is_inside(path, base):
            raise ManifestError(
                ENV_FILE_UNSAFE,
                "$.launch.env.env_file",
                f"env_file {path} lies inside {label} ({base}) — "
                "the manifests dir must never hold secrets",
                fix_hint=_fix_move(),
            )


def load_env_file(
    path: str | os.PathLike[str],
    manifests_dir: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    """Load a component env file; ANY safety violation refuses with a fix command.

    Returns the parsed ``KEY=VALUE`` mapping. ``manifests_dir`` (the
    directory of the manifest that declares this env file) enables the
    placement check; ``None`` skips it.
    """
    env_path = Path(os.path.expanduser(str(path)))
    check_dir = Path(manifests_dir) if manifests_dir is not None else None
    _check_safety(env_path, check_dir)

    values: dict[str, str] = {}
    for line_no, raw_line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ManifestError(
                ENV_FILE_UNSAFE,
                f"$.launch.env.env_file[{line_no}]",
                f"env_file {env_path} line {line_no} is not KEY=VALUE",
                fix_hint=f"fix or remove line {line_no} in {env_path}",
            )
        key, _, value = line.partition("=")
        key = key.strip()
        if not _ENV_KEY_RE.match(key):
            raise ManifestError(
                ENV_FILE_UNSAFE,
                f"$.launch.env.env_file[{line_no}]",
                f"env_file {env_path} line {line_no}: invalid variable name {key!r}",
                fix_hint=f"fix or remove line {line_no} in {env_path}",
            )
        # Reserved keys (cascade 2026-10-05 P2-D): PATH is constructed
        # canonically by the supervisor (SL-13 / SL §3.2) — a file-supplied
        # PATH would silently override it. This TIGHTENS beyond the spec
        # letter (the spec bans PATH only in launch.env.vars); flagged as a
        # candidate clarification for specs draft.3.
        if key in _RESERVED_KEYS:
            raise ManifestError(
                ENV_FILE_UNSAFE,
                f"$.launch.env.env_file[{line_no}].{key}",
                f"env_file {env_path} line {line_no}: {key} is reserved — the "
                "supervisor constructs the child PATH from the component venv "
                "bin dir + the fixed system string (SL-13); a file-supplied "
                "PATH would override the canonical constructed one",
                fix_hint=(
                    f"move PATH out of {env_path} (the canonical constructed "
                    "PATH is contractual; add path entries to the component "
                    "venv or a launcher wrapper)"
                ),
            )
        values[key] = value.strip()
    return values
