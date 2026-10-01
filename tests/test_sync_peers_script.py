"""Tests for the auto-cron federation bridge (#104).

Covers ``scripts/sync-peers.sh`` (the ExecStart of
``contrib/systemd/vesma-sync.service``) and the two systemd unit files. The
script reads its config from ``VESMARO_SYNC_*`` env vars; these tests exercise
the env-var validation, the dry-run command logging, and the unit file
shape. They do NOT run a real mnemos CLI, rsync, or ssh — dry-run mode
(``VESMARO_SYNC_DRY_RUN=1``) logs the commands and exits before any network
or filesystem side effect.

All secret/PII fixtures use RFC-reserved values (per
``sensitive-data.instructions.md``): 192.0.2.0/24 (RFC 5737),
example.invalid (RFC 6761). The placeholder SSH key files are EMPTY — the
script only checks ``-r`` readability, never parses the key material. No
real ed25519 key is generated.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "sync-peers.sh"
SERVICE = REPO_ROOT / "contrib" / "systemd" / "vesma-sync.service"
TIMER = REPO_ROOT / "contrib" / "systemd" / "vesma-sync.timer"
RSYNC_WRAPPER = REPO_ROOT / "contrib" / "systemd" / "rsync-wrapper.sh"
IMPORT_WRAPPER = REPO_ROOT / "contrib" / "systemd" / "vesma-import-wrapper.sh"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def placeholder_keys(tmp_path: Path) -> tuple[Path, Path]:
    """Create two EMPTY placeholder key files (script only checks -r)."""
    push_key = tmp_path / "sync-push-key"
    trigger_key = tmp_path / "sync-trigger-key"
    push_key.write_text("")
    trigger_key.write_text("")
    push_key.chmod(0o600)
    trigger_key.chmod(0o600)
    return push_key, trigger_key


def _full_env(push_key: Path, trigger_key: Path, export_dir: Path) -> dict[str, str]:
    """A complete, valid env-var set using RFC-reserved dummies.

    Aligns to the script's actual contract (two keys: push + trigger).
    """
    env = {
        "VESMARO_SYNC_PEER_HOST": "192.0.2.10",
        "VESMARO_SYNC_PEER_USER": "mnemos-sync",
        "VESMARO_SYNC_PEER_SSH_KEY": str(push_key),
        "VESMARO_SYNC_PEER_IMPORT_SSH_KEY": str(trigger_key),
        "VESMARO_SYNC_LOCAL_EXPORT_DIR": str(export_dir),
        "VESMARO_SYNC_REMOTE_IMPORT_DIR": "/var/lib/mnemos-sync/incoming",
        "VESMARO_SYNC_SHARED_PROJECTS": "project-test",
        "VESMARO_SYNC_ENCRYPT": "true",
        "VESMARO_SYNC_PASSPHRASE_ENV": "VESMARO_EXPORT_PASSPHRASE",
        "VESMARO_EXPORT_PASSPHRASE": "dummy-passphrase-not-a-secret",
        "VESMARO_SYNC_DRY_RUN": "1",
        # Force a mnemos binary path so the script does not fail auto-discovery
        # in the test environment (where `mnemos` may not be on PATH).
        "VESMARO_SYNC_VESMARO_BIN": "/usr/bin/true",
    }
    # Inherit PATH so bash/date/rsync resolve.
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    return env


def _run_script(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run sync-peers.sh with the given env, capturing stdout+stderr."""
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _run_wrapper(
    wrapper: Path, ssh_original_command: str, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a contrib/systemd wrapper with SSH_ORIGINAL_COMMAND set.

    Wrappers are pinned via ``command=""`` in authorized_keys; SSH places the
    real command in ``$SSH_ORIGINAL_COMMAND``. These tests exercise the
    hardening (B2 rsync option whitelist, B3 import flag whitelist) by driving
    that variable directly — no real sshd, rsync, or mnemos CLI runs.
    """
    full_env: dict[str, str] = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        # Avoid leaking the host SSH_CLIENT into the wrapper under set -u;
        # the wrapper falls back to "unknown" when unset.
    }
    if env:
        full_env.update(env)
    full_env["SSH_ORIGINAL_COMMAND"] = ssh_original_command
    return subprocess.run(
        ["bash", str(wrapper)],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _wrapper_env(tmp_path: Path) -> dict[str, str]:
    """Common env for wrapper tests: temp INCOMING_DIR + temp audit log.

    Uses ``VESMARO_SYNC_*`` names (the wrappers' documented override prefix).
    RSYNC_BIN/VESMARO_BIN point at ``true`` so no real rsync/mnemos runs.
    """
    incoming = tmp_path / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    return {
        "VESMARO_SYNC_INCOMING_DIR": str(incoming),
        "VESMARO_SYNC_AUDIT_LOG": str(tmp_path / "audit.log"),
        "VESMARO_SYNC_PASSPHRASE_ENV": "VESMARO_EXPORT_PASSPHRASE",
        "VESMARO_EXPORT_PASSPHRASE": "dummy-passphrase-not-a-secret",
        "RSYNC_BIN": shutil.which("true") or "/usr/bin/true",
        "VESMARO_SYNC_REMOTE_VESMARO_BIN": shutil.which("true") or "/usr/bin/true",
    }


# ── 1. refuses without required env vars ──────────────────────────────────────


def test_refuses_without_required_env_vars() -> None:
    """Empty env → exit 2 with a 'missing required env var(s)' message."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    result = _run_script(env)
    assert result.returncode != 0
    assert result.returncode == 2, (
        f"expected exit 2 for missing env, got {result.returncode}; stderr:\n{result.stderr}"
    )
    assert "missing required env var" in result.stderr, (
        f"stderr should name the missing var; got:\n{result.stderr}"
    )


# ── 2. refuses with partial env vars ──────────────────────────────────────────


def test_refuses_with_partial_env_vars(placeholder_keys: tuple[Path, Path]) -> None:
    """Only some required vars set → exit 2 naming the missing ones."""
    push_key, _trigger_key = placeholder_keys
    env = {
        "VESMARO_SYNC_PEER_HOST": "192.0.2.10",
        "VESMARO_SYNC_PEER_SSH_KEY": str(push_key),
        # Intentionally omit the other 6 required vars.
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    result = _run_script(env)
    assert result.returncode == 2
    assert "missing required env var" in result.stderr
    # The error should list at least one of the omitted vars.
    omitted = [
        "VESMARO_SYNC_PEER_IMPORT_SSH_KEY",
        "VESMARO_SYNC_LOCAL_EXPORT_DIR",
        "VESMARO_SYNC_REMOTE_IMPORT_DIR",
        "VESMARO_SYNC_SHARED_PROJECTS",
        "VESMARO_SYNC_ENCRYPT",
        "VESMARO_SYNC_PASSPHRASE_ENV",
    ]
    assert any(v in result.stderr for v in omitted), (
        f"stderr should name an omitted var; got:\n{result.stderr}"
    )


# ── 3. dry-run logs expected commands ─────────────────────────────────────────


def test_dry_run_logs_expected_commands(
    placeholder_keys: tuple[Path, Path], tmp_path: Path
) -> None:
    """Full env + DRY_RUN=1 → exit 0, logs mnemos sync export, rsync, ssh."""
    push_key, trigger_key = placeholder_keys
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    env = _full_env(push_key, trigger_key, export_dir)
    result = _run_script(env)
    assert result.returncode == 0, (
        f"expected exit 0 in dry-run, got {result.returncode}; stderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    # The script logs the constructed export command including "sync export".
    assert "mnemos sync export" in combined or "sync export" in combined, (
        f"dry-run should log the export command; got:\n{combined}"
    )
    # The script logs the rsync command.
    assert "rsync" in combined, f"dry-run should log the rsync command; got:\n{combined}"
    # The script logs the ssh import-trigger command.
    assert "ssh" in combined, f"dry-run should log the ssh import trigger; got:\n{combined}"
    # Dry-run must NOT actually call rsync/ssh/mnemos — it logs and skips.
    # The script's dry-run path prints "dry-run: skipping ..." for each step.
    assert "dry-run" in combined, f"dry-run should announce skipping; got:\n{combined}"


# ── 4. systemd units valid ────────────────────────────────────────────────────
# ── 5. rsync-wrapper rejects --delete (B2, CWE-78) ────────────────────────────


def test_rsync_wrapper_rejects_delete_option(tmp_path: Path) -> None:
    """``--delete`` in SSH_ORIGINAL_COMMAND → exit 2 + REJECT audit line.

    A compromised A could inject ``--delete`` to wipe files in INCOMING_DIR
    not in the current transfer. The wrapper must refuse the option before
    exec'ing rsync, log a REJECT line naming the option, and exit non-zero.
    """
    env = _wrapper_env(tmp_path)
    incoming = env["VESMARO_SYNC_INCOMING_DIR"]
    dest = f"{incoming}/export.json"
    result = _run_wrapper(
        RSYNC_WRAPPER,
        f"rsync --server -v -l -r -t -p -S --delete . {dest}",
        env=env,
    )
    assert result.returncode == 2, (
        f"expected exit 2 for --delete injection, got {result.returncode}; stderr:\n{result.stderr}"
    )
    assert "rejected dangerous rsync option: --delete" in result.stderr, (
        f"stderr should name the rejected option; got:\n{result.stderr}"
    )
    audit = Path(env["VESMARO_SYNC_AUDIT_LOG"]).read_text()
    assert "REJECT" in audit and "--delete" in audit, (
        f"audit log should record the REJECT with the option; got:\n{audit}"
    )


# ── 6. rsync-wrapper rejects unknown options (B2, CWE-78) ─────────────────────


def test_rsync_wrapper_rejects_unknown_option(tmp_path: Path) -> None:
    """An unknown rsync option → exit 2 + REJECT audit line.

    Defence-in-depth: future rsync options are NOT silently forwarded. Any
    option outside the whitelist (``-v -l -r -t -p -S --server --sender``) is
    refused, so a new destructive flag added by a future rsync release cannot
    reach INCOMING_DIR through this wrapper.
    """
    env = _wrapper_env(tmp_path)
    incoming = env["VESMARO_SYNC_INCOMING_DIR"]
    dest = f"{incoming}/export.json"
    result = _run_wrapper(
        RSYNC_WRAPPER,
        f"rsync --server --unknown-future-opt . {dest}",
        env=env,
    )
    assert result.returncode == 2, (
        f"expected exit 2 for unknown option, got {result.returncode}; stderr:\n{result.stderr}"
    )
    assert "rejected unknown rsync option: --unknown-future-opt" in result.stderr, (
        f"stderr should name the rejected option; got:\n{result.stderr}"
    )
    audit = Path(env["VESMARO_SYNC_AUDIT_LOG"]).read_text()
    assert "REJECT" in audit and "--unknown-future-opt" in audit, (
        f"audit log should record the REJECT with the option; got:\n{audit}"
    )


# ── 7. import-wrapper rejects unknown --* flags (B3, CWE-78) ──────────────────


def test_import_wrapper_rejects_unknown_flag(tmp_path: Path) -> None:
    """An unknown ``--*`` flag on ``vesma sync import`` → exit 2 + REJECT.

    Only ``--dry-run`` is forwarded; ``--passphrase-env`` is pinned by the
    wrapper itself. A compromised A could otherwise redirect the import
    source via ``--config`` or other file-reading flags. The wrapper must
    refuse the flag, log a REJECT line naming it, and exit non-zero.
    """
    env = _wrapper_env(tmp_path)
    incoming = Path(env["VESMARO_SYNC_INCOMING_DIR"])
    src = incoming / "export.json"
    src.write_text('{"x":1}')
    result = _run_wrapper(
        IMPORT_WRAPPER,
        f"vesma sync import {src} --config /etc/shadow",
        env=env,
    )
    assert result.returncode == 2, (
        f"expected exit 2 for unknown import flag, got {result.returncode};"
        f" stderr:\n{result.stderr}"
    )
    assert "rejected unknown import flag: --config" in result.stderr, (
        f"stderr should name the rejected flag; got:\n{result.stderr}"
    )
    audit = Path(env["VESMARO_SYNC_AUDIT_LOG"]).read_text()
    assert "REJECT" in audit and "--config" in audit, (
        f"audit log should record the REJECT with the flag; got:\n{audit}"
    )


def test_systemd_units_valid() -> None:
    """mnemos-sync.service + .timer are well-formed systemd units."""
    assert SERVICE.is_file(), f"missing service unit: {SERVICE}"
    assert TIMER.is_file(), f"missing timer unit: {TIMER}"
    service_text = SERVICE.read_text()
    timer_text = TIMER.read_text()

    # Text assertions (always run — systemd-analyze may be absent on non-Linux CI).
    assert "[Unit]" in service_text
    assert "[Service]" in service_text
    assert "ExecStart=" in service_text
    assert "Type=oneshot" in service_text
    assert "[Unit]" in timer_text
    assert "[Timer]" in timer_text
    assert "OnCalendar=" in timer_text

    # If systemd-analyze is available, run the formal verifier.
    analyzer = shutil.which("systemd-analyze")
    if analyzer is None:
        # systemd not installed in this environment — text assertions suffice.
        return
    verify = subprocess.run(
        [analyzer, "verify", str(SERVICE), str(TIMER)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    # systemd-analyze verify exits 0 on a valid unit; non-zero with a message
    # on a problem. Treat a clean exit as pass.
    if verify.returncode != 0:
        # Some systemd versions warn about the missing EnvironmentFile at
        # verify time (the file is not present in CI). Distinguish that from a
        # real structural error: if the only complaint is the EnvironmentFile
        # or a missing binary, the unit structure is still valid.
        msg = verify.stderr + verify.stdout
        structural_errors = [
            line
            for line in msg.splitlines()
            if line and "EnvironmentFile" not in line and "Command" not in line
        ]
        assert not structural_errors, f"systemd-analyze verify reported structural errors:\n{msg}"


if __name__ == "__main__":
    # Allow running this file directly:  python tests/test_sync_peers_script.py
    sys.exit(pytest.main([__file__, "-v"]))
