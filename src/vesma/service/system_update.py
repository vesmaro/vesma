"""``vesma system update`` — the mode-aware system-profile updater
(specs/layout v2 §3.13; ADR-0045 Decision 6, rollout wave 3; conformance
SY-07/SY-10/SY-11). The former ``vesma service update --system`` flag
form is REMOVED (CLI topology FINAL, 7.0.0) — this is the one spelling.

The update contract, in execution order:

1. **Refuse-before-mutate**: the update needs an existing system
   install-state and a drift-free tree — :func:`vesma.service.
   system_install.system_state_drift` (the SAME predicate doctor DR-17
   renders) is evaluated BEFORE any action; any drift is a loud refusal.
2. **New revision nearby**: the running build's venv revisions are
   materialized by the canonical installer (:func:`install_system` —
   reuse, not re-implementation: ``uv venv`` + the SAME-BUILD wheel pin,
   ``pip`` as root is forbidden and does not exist in the tree, foreign
   venvs are never written — the owner's environment-isolation rule 8).
   The revision id stays the engine version (chairman ratification
   2026-10-20: same build == same content-id keeps the flow idempotent;
   a freeze-hash content-id remains a candidate and is NOT adopted
   without this wave's health-gate semantics being re-ratified).
3. **Atomic swap**: the stable symlinks (``/usr/lib/vesma/venv``,
   ``venvs/<name>``) are re-pointed by tmp-link + rename — a partial or
   missing revision is never observable through the symlink.
4. **Health gate**: EVERY new revision must prove itself before the
   previous revision is retired (§3.13: removal only after a successful
   swap AND a health check). Health, this wave: the revision's
   entrypoint executes and self-reports the expected identity (engine:
   ``bin/vesma --version`` carries the running build's version;
   component: its interpreter starts). Service-level health (unit
   restart + control-socket hello) lands with the units wave and is
   deliberately out of scope here.
5. **Rollback on gate failure**: the symlinks are atomically swapped
   BACK to the recorded targets, the install-state text and the
   component locks are restored — the previous revision is active and
   fully recorded again; the failed new revision stays on disk for
   diagnosis (DR-16 reports it as a leftover). The raised error carries
   the rollback log with an honest verdict — ROLLED BACK vs ROLLBACK
   INCOMPLETE when the restore itself partly failed (the CLI prints
   report lines only on success, so the log must ride the error).
6. **Retirement**: only after the gate passes, the previous revision
   dirs are removed (``rmtree``) — they are venv code trees, not
   operator data and not memory records (the wave's P0 record-deletion
   ban is untouched).
7. **Idempotency**: a re-run at the same version materializes nothing
   (the installer reuses healthy revisions), re-probes health and
   retires nothing — a refresh, not a second update.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import os
import re
import shutil
import subprocess  # nosec B404 - fixed-argv health probe, no shell
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from vesma.service import fhs
from vesma.service import system_install as si
from vesma.service.system_install import StateArtifact, SystemInstallError

logger = logging.getLogger("vesma.service.system_update")

#: Health-probe budget per revision (a cold interpreter start is
#: seconds; a hung probe must not hang the update).
_HEALTH_TIMEOUT: Final[int] = 120


@dataclasses.dataclass(frozen=True)
class SystemUpdateResult:
    """Outcome of one system update run (CLI report + tests)."""

    lines: tuple[str, ...]
    engine_venv: Path
    revisions: tuple[Path, ...]
    rolled_back: bool


def update_system(prefix: Path | None = None) -> SystemUpdateResult:
    """Update the system profile to the RUNNING build (§3.13).

    Refuses loudly when no system profile is installed or the tree
    drifts from the install-state; otherwise materializes the running
    version's venv revisions beside the current ones, swaps the symlinks
    atomically, health-gates, and retires the previous revisions only
    after the gate passes. Re-runs are idempotent.
    """
    resolved_prefix = fhs.default_prefix() if prefix is None else prefix
    si._require_root_for_real_prefix(resolved_prefix)
    old_doc = si.read_system_state(resolved_prefix)
    if old_doc is None:
        raise SystemInstallError(
            f"no system profile is installed ({si._state_path(resolved_prefix)}"
            " absent) — update needs an existing installation"
            " (fix: `sudo vesma system install`)"
        )
    # Preflight (step 1): DR-17's exact predicate, BEFORE any action.
    drift = si.system_state_drift(resolved_prefix)
    if drift:
        raise SystemInstallError(
            "install-state ↔ filesystem drift — refusing the update BEFORE"
            f" any action ({len(drift)} problem(s)); first: {drift[0]} —"
            " inventory with `vesma doctor`, resolve the drift, then re-run"
            " `sudo vesma system update`"
        )

    version = si._engine_version()
    lines: list[str] = []
    already = str(old_doc.get("engine_version")) == version
    if already:
        lines.append(
            f"engine already at {version} — refresh run (idempotent:"
            " healthy revisions are reused, nothing is re-materialized)"
        )

    # Snapshot everything the rollback path restores (step 5): the state
    # text, the recorded symlink targets, the component locks.
    state_path = si._state_path(resolved_prefix)
    old_state_text = state_path.read_text(encoding="utf-8")
    try:
        old_rows = [StateArtifact.from_dict(a) for a in old_doc["artifacts"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemInstallError(
            f"the system install-state at {state_path} has malformed artifact"
            f" records ({exc}) — refusing to update"
        ) from exc
    old_links = {
        Path(a.path): a.target
        for a in old_rows
        if a.kind == "symlink" and a.status != "planned" and a.target is not None
    }
    old_rev_dirs = {
        Path(a.path) for a in old_rows if a.role in si.REVISION_ROLES and a.status != "planned"
    }
    # Security containment (cascade fix, P1): every path the retirement
    # step may rmtree MUST resolve inside the manifest code plane — the
    # install-state is installer-written but the retirement acts on the
    # SNAPSHOT taken here, before the installer rewrites the state, so a
    # tampered row must be caught BEFORE any mutation. Validate ALL
    # paths first, delete later (no partial removals). Boundary
    # semantics mirror doctor DR-16's check_link (realpath containment).
    boundary_roots = (
        Path(
            os.path.realpath(
                fhs.resolve_system_path(fhs.load_fhs_manifest().entry("code_root"), resolved_prefix)
            )
        ),
        Path(os.path.realpath(fhs.system_venvs_root(resolved_prefix))),
    )
    for rev_dir in old_rev_dirs:
        if not rev_dir.exists():
            continue  # absent rows are skipped by the retirement loop too
        real = Path(os.path.realpath(rev_dir))
        if not any(real == root or root in real.parents for root in boundary_roots):
            raise SystemInstallError(
                f"install-state names a revision path OUTSIDE the code"
                f" plane: {rev_dir} — refusing the update BEFORE any action"
                " (tampered install-state?); inventory with `vesma doctor`,"
                " repair the state, then re-run"
                " `sudo vesma system update`"
            )
    ownership = si._Ownership(apply_ownership=os.geteuid() == 0)
    lock_paths = {
        Path(a.path) / "requirements-lock.txt" for a in old_rows if a.role == "component_data"
    }
    old_lock_text = {
        lock: lock.read_text(encoding="utf-8") for lock in sorted(lock_paths) if lock.exists()
    }

    # Steps 2-3: materialize + swap through the canonical installer. ANY
    # failure past the preflight rolls the visible world back (best
    # effort, never masking the original error) — the operator keeps a
    # working, fully recorded install, and the raised error CARRIES the
    # rollback log (the CLI prints report lines only on success — a
    # failure text claiming a clean rollback while the restore itself
    # partly failed would be a lie; cascade fix P2).
    rollback_log: list[str] = []
    try:
        result = si.install_system(resolved_prefix)
    except SystemInstallError as exc:
        with contextlib.suppress(Exception):
            _rollback(
                resolved_prefix, old_links, old_state_text, old_lock_text, ownership, rollback_log
            )
        raise SystemInstallError(_failure_text(str(exc), rollback_log)) from exc
    except Exception as exc:
        with contextlib.suppress(Exception):
            _rollback(
                resolved_prefix, old_links, old_state_text, old_lock_text, ownership, rollback_log
            )
        raise SystemInstallError(
            _failure_text(f"{type(exc).__name__}: {exc}", rollback_log)
        ) from exc
    failures = _health_gate(resolved_prefix, result.revisions, version, lines)
    if failures:
        with contextlib.suppress(Exception):
            _rollback(
                resolved_prefix, old_links, old_state_text, old_lock_text, ownership, rollback_log
            )
        raise SystemInstallError(
            _failure_text(
                "health gate FAILED after the swap"
                f" ({len(failures)} failure(s); first: {failures[0]})."
                " The failed new revision(s) stay on disk for diagnosis"
                " (doctor DR-16 reports them as leftovers)",
                rollback_log,
            )
        )

    # Step 6: retirement — only the gate's survivors reach this line.
    new_revs = set(result.revisions)
    retired = 0
    for rev_dir in sorted(old_rev_dirs):
        if rev_dir in new_revs or not rev_dir.exists():
            continue
        shutil.rmtree(rev_dir)
        retired += 1
        lines.append(f"retired previous revision: {rev_dir}")
    lines.append(f"system profile at {version} ({result.engine_venv})")
    logger.info(
        "system profile updated: prefix=%s version=%s retired=%d revisions=%s",
        resolved_prefix,
        version,
        retired,
        [r.name for r in result.revisions],
    )
    return SystemUpdateResult(
        lines=tuple(lines),
        engine_venv=result.engine_venv,
        revisions=result.revisions,
        rolled_back=False,
    )


def _failure_text(problem: str, rollback_log: list[str]) -> str:
    """The operator-facing failure text: the problem, the rollback
    verdict (clean vs INCOMPLETE — a partial rollback must never claim a
    clean one) and the rollback log lines. The log rides the ERROR
    because the CLI prints report lines only on success — without this,
    "could not restore" lines were lost exactly when they mattered
    (cascade fix P2)."""
    partial = any(line.startswith("rollback: could not") for line in rollback_log)
    verdict = (
        "ROLLBACK INCOMPLETE — the restore itself partly failed"
        if partial
        else "ROLLED BACK to the previous revision(s)"
    )
    log = "; ".join(rollback_log) if rollback_log else "no rollback steps were needed"
    return (
        f"{problem} — {verdict}. Rollback log: {log}. The install-state"
        " describes the previous, ACTIVE revision — inventory with"
        " `vesma doctor`, fix, then re-run"
        " `sudo vesma system update`"
    )


def _health_gate(
    prefix: Path, revisions: tuple[Path, ...], version: str, lines: list[str]
) -> list[str]:
    """Probe every new revision (step 4); returns the failure list.

    The engine revision must self-report the running build's version;
    component revisions must at least start their interpreter (their
    content identity was already pinned+verified at materialization).
    """
    venvs_root = fhs.system_venvs_root(prefix)
    failures: list[str] = []
    for rev_dir in revisions:
        if rev_dir.parent == venvs_root:
            ok, detail = _probe_revision(rev_dir, "bin/python", expected=None)
        else:
            ok, detail = _probe_revision(rev_dir, "bin/vesma", expected=version)
        if ok:
            lines.append(f"health: {rev_dir.name}: ok ({detail})")
        else:
            failures.append(f"{rev_dir}: {detail}")
    return failures


def _probe_revision(rev_dir: Path, done_marker: str, *, expected: str | None) -> tuple[bool, str]:
    """One revision's health leg: the entrypoint exists AND executes,
    self-reporting the expected identity when one is given.

    Subprocess seam (monkeypatched in tests); explicit argv, no shell —
    the probe executes ONLY installer-materialized trees (root-owned,
    manifest modes, DR-15/DR-16 enforce the boundary).
    """
    marker = rev_dir / done_marker
    if not marker.exists():
        return False, f"done marker {marker} missing"
    argv = (
        [str(marker), "--version"]
        if done_marker == "bin/vesma"
        else [str(marker), "-c", "import sys; print(sys.version.split()[0])"]
    )
    try:
        result = subprocess.run(  # nosec B603 - fixed argv, no shell
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_HEALTH_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"probe failed to run: {exc}"
    if result.returncode != 0:
        return False, f"exit {result.returncode}: {result.stderr.strip()[-300:]}"
    output = result.stdout.strip()[:200]
    if expected is not None and not _version_token_present(expected, output):
        return False, f"entrypoint reports {output!r}, expected {expected!r}"
    return True, output or "exit 0"


def _version_token_present(expected: str, output: str) -> bool:
    """Token-boundary version match (cascade fix, P2): ``6.3.0`` must
    NOT match ``6.3.01`` / ``6.3.0rc1`` / ``16.3.0`` — the expected
    version must appear as a standalone token on non-identifier
    boundaries (a plain substring check accepted all of those)."""
    pattern = rf"(?<![0-9A-Za-z_.-]){re.escape(expected)}(?![0-9A-Za-z_.-])"
    return re.search(pattern, output) is not None


def _rollback(
    prefix: Path,
    old_links: Mapping[Path, str | None],
    old_state_text: str,
    old_lock_text: Mapping[Path, str],
    ownership: si._Ownership,
    lines: list[str],
) -> None:
    """Best-effort atomic rollback (step 5): symlinks back to the
    recorded targets, the pre-update install-state text and component
    locks restored. Never raises past the caller's suppress."""
    for link_path, target_name in sorted(old_links.items()):
        desired = str(target_name)
        try:
            current = os.readlink(link_path) if link_path.is_symlink() else None
            if current == desired:
                continue
            si._atomic_symlink(Path(desired), link_path)
        except OSError as exc:
            lines.append(f"rollback: could not restore {link_path}: {exc}")
            continue
        lines.append(f"rollback: {link_path} -> {desired}")
    try:
        si._atomic_write(state_path := si._state_path(prefix), old_state_text, 0o640)
        state_entry = fhs.load_fhs_manifest().entry("install_state")
        ownership.apply_to(state_path, state_entry.owner, state_entry.group)
        lines.append("rollback: install-state restored to the previous revision set")
    except OSError as exc:
        lines.append(f"rollback: could not restore the install-state: {exc}")
    for lock, text in sorted(old_lock_text.items()):
        try:
            si._atomic_write(lock, text, 0o640)
            ownership.apply_to(lock, "vesma", "vesma")
            lines.append(f"rollback: lock restored: {lock}")
        except OSError as exc:
            lines.append(f"rollback: could not restore {lock}: {exc}")


__all__ = [
    "SystemUpdateResult",
    "update_system",
]
