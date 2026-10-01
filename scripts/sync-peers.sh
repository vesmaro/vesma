#!/usr/bin/env bash
# sync-peers.sh — auto-cron federation bridge (#104) for vesma Phase 0.
#
# Automates the operator step that Phase 0 batch sync (#85 part 2b) left
# manual: it runs `vesma sync export` on A, pushes the encrypted payload
# to B over rsync+ssh, then triggers `vesma sync import` on B over ssh.
# vesma ITSELF stays offline — there is no inbound endpoint on vesma.
# All automation is at the host/SSH layer, per ArchCom 2026-07-20 decision
# (vesma memory 4dc7d96e, protocol .archcom/sessions/2026-07-20-automated-channel.md).
#
# This script is the ExecStart of contrib/systemd/vesma-sync.service. It is
# also runnable by hand for testing. It reads its config from env vars —
# the systemd unit loads /etc/vesma/sync.env via EnvironmentFile=.
#
# Required env (refuse to run if any is missing — exit 2):
#   VESMARO_SYNC_PEER_HOST          — peer (TARGET) host B
#   VESMARO_SYNC_PEER_SSH_KEY       — ed25519 private key on A for rsync push
#   VESMARO_SYNC_PEER_IMPORT_SSH_KEY— ed25519 private key on A for import trigger
#   VESMARO_SYNC_LOCAL_EXPORT_DIR   — local dir where export writes the payload
#   VESMARO_SYNC_REMOTE_IMPORT_DIR  — dir on B where rsync delivers the payload
#   VESMARO_SYNC_SHARED_PROJECTS    — comma-separated project slugs to sync
#   VESMARO_SYNC_ENCRYPT            — "true"|"false"
#   VESMARO_SYNC_PASSPHRASE_ENV     — NAME of env var holding the passphrase
#
# Optional env:
#   VESMARO_SYNC_PEER_USER           — ssh user on B (default: mnemos-sync)
#   VESMARO_SYNC_DRY_RUN             — "1" logs commands only, no writes/ssh
#   VESMARO_SYNC_SOURCE_CONFIG       — path to A's vesma config.yaml
#   VESMARO_SYNC_REMOTE_FILE         — basename on B (default: vesma-sync-<ts>.json)
#   VESMARO_SYNC_MNEMOS_BIN          — vesma CLI on A (default: auto-discover)
#
# The path to the vesma CLI on B (VESMARO_SYNC_REMOTE_MNEMOS_BIN) is set on B
# in /etc/vesma/sync.env — A does not need it because the vesma-import-wrapper
# on B resolves the binary.
#
# Security: the passphrase is NEVER passed on the command line. On A it is read
# by `vesma sync export` from $VESMARO_SYNC_PASSPHRASE_ENV (which must be set in
# the service environment). On B it is read by `vesma sync import` from the
# env var NAME passed via --passphrase-env — that name is pinned on B by the
# vesma-import-wrapper guard (contrib/systemd/vesma-import-wrapper.sh) and
# the value must be provisioned on B's systemd environment independently.
#
# Exit codes:
#   0 — export + transfer + import all green
#   1 — a sync step failed (mnemos/rsync/ssh returned non-zero)
#   2 — env-var validation failed (script not configured)

set -euo pipefail

# ── logging ──────────────────────────────────────────────────────────────────
# All output goes to stderr with ISO-8601 UTC timestamps. The systemd journal
# captures it; for cron/manual runs the operator redirects 2>>/var/log/vesma-sync.log.
_log() {
    printf '[%s] sync-peers: %s\n' "$(date -u +%FT%TZ)" "$*" >&2
}
_err() {
    printf '[%s] sync-peers: ERROR: %s\n' "$(date -u +%FT%TZ)" "$*" >&2
}

# ── legacy env-name compatibility (ADR-0031 dual period) ─────────────────────
# Pre-5.0 deployments (and old /etc/mnemos/sync.env files) used MNEMOS_SYNC_*.
# Map any unset VESMARO_SYNC_* from its MNEMOS_SYNC_* counterpart so old
# EnvironmentFiles keep working. New names win on conflict.
for _v in PEER_HOST PEER_USER PEER_SSH_KEY IMPORT_SSH_KEY LOCAL_EXPORT_DIR \
          REMOTE_IMPORT_DIR SHARED_PROJECTS ENCRYPT PASSPHRASE_ENV \
          MNEMOS_BIN DELETE_REMOTE KEEP_REMOTE_DAYS DRY_RUN; do
    _new="VESMARO_SYNC_${_v}"
    _old="MNEMOS_SYNC_${_v}"
    if [ -z "${!_new:-}" ] && [ -n "${!_old:-}" ]; then
        eval "export $_new=\${!_old}"
    fi
done
if [ -z "${VESMARO_SYNC_DRY_RUN:-}" ] && [ "${MNEMOS_SYNC_DRY_RUN:-}" = "1" ]; then
    export VESMARO_SYNC_DRY_RUN=1
fi

# ── env-var validation ───────────────────────────────────────────────────────
# Refuse to run if any required var is missing. Print a clear error pointing
# the operator at /etc/vesma/sync.env (the EnvironmentFile the service loads).

_required_vars=(
    VESMARO_SYNC_PEER_HOST
    VESMARO_SYNC_PEER_SSH_KEY
    VESMARO_SYNC_PEER_IMPORT_SSH_KEY
    VESMARO_SYNC_LOCAL_EXPORT_DIR
    VESMARO_SYNC_REMOTE_IMPORT_DIR
    VESMARO_SYNC_SHARED_PROJECTS
    VESMARO_SYNC_ENCRYPT
    VESMARO_SYNC_PASSPHRASE_ENV
)

_missing=()
for v in "${_required_vars[@]}"; do
    if [[ -z "${!v:-}" ]]; then
        _missing+=("$v")
    fi
done

if [[ ${#_missing[@]} -gt 0 ]]; then
    _err "missing required env var(s): ${_missing[*]}"
    _err "configure them in /etc/vesma/sync.env (see contrib/systemd/sync.env.example)."
    exit 2
fi

# ── optional env with defaults ───────────────────────────────────────────────
PEER_USER="${VESMARO_SYNC_PEER_USER:-mnemos-sync}"
DRY_RUN="${VESMARO_SYNC_DRY_RUN:-0}"
SOURCE_CONFIG="${VESMARO_SYNC_SOURCE_CONFIG:-}"
REMOTE_FILE="${VESMARO_SYNC_REMOTE_FILE:-mnemos-sync-$(date -u +%Y%m%dT%H%M%SZ).json}"
MNEMOS_BIN="${VESMARO_SYNC_MNEMOS_BIN:-}"

# Normalize VESMARO_SYNC_ENCRYPT to a boolean string.
case "${VESMARO_SYNC_ENCRYPT}" in
    true|True|TRUE|1|yes|Yes) ENCRYPT=true ;;
    false|False|FALSE|0|no|No) ENCRYPT=false ;;
    *)
        _err "VESMARO_SYNC_ENCRYPT must be 'true' or 'false' (got '${VESMARO_SYNC_ENCRYPT}')."
        exit 2
        ;;
esac

if [[ "$ENCRYPT" == "true" ]]; then
    # The passphrase must be available in the env var NAME we advertise. If the
    # named env var is not set on A, refuse — vesma sync export would fail.
    if [[ -z "${!VESMARO_SYNC_PASSPHRASE_ENV:-}" ]]; then
        _err "ENCRYPT=true but \${${VESMARO_SYNC_PASSPHRASE_ENV}} is not set on A."
        _err "provision the passphrase in the service environment (systemd LoadCredential or drop-in)."
        exit 2
    fi
fi

# Discover the vesma CLI on A if not set.
if [[ -z "$MNEMOS_BIN" ]]; then
    if command -v vesma >/dev/null 2>&1; then
        MNEMOS_BIN="$(command -v vesma)"
    elif [[ -x "$(dirname "$0")/../.venv/bin/mnemos" ]]; then
        MNEMOS_BIN="$(cd "$(dirname "$0")/.." && pwd)/.venv/bin/mnemos"
    else
        _err "vesma CLI not found on PATH and no .venv next to the script."
        _err "set VESMARO_SYNC_MNEMOS_BIN in /etc/vesma/sync.env."
        exit 2
    fi
fi

# Sanity: the ssh keys must exist and be readable.
if [[ ! -r "$VESMARO_SYNC_PEER_SSH_KEY" ]]; then
    _err "VESMARO_SYNC_PEER_SSH_KEY not readable: $VESMARO_SYNC_PEER_SSH_KEY"
    exit 2
fi
if [[ ! -r "$VESMARO_SYNC_PEER_IMPORT_SSH_KEY" ]]; then
    _err "VESMARO_SYNC_PEER_IMPORT_SSH_KEY not readable: $VESMARO_SYNC_PEER_IMPORT_SSH_KEY"
    exit 2
fi

# Ensure the local export dir exists.
if [[ "$DRY_RUN" != "1" ]]; then
    mkdir -p "$VESMARO_SYNC_LOCAL_EXPORT_DIR"
fi

# ── helpers ──────────────────────────────────────────────────────────────────
# Build the ssh base options for BatchMode (no password prompt — fail loudly).
_ssh_opts=(-o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o PasswordAuthentication=no)

# Build the export args for `vesma sync export` on A.
_export_args=(sync export --output "${VESMARO_SYNC_LOCAL_EXPORT_DIR}/${REMOTE_FILE}" --shared-projects "$VESMARO_SYNC_SHARED_PROJECTS")
if [[ -n "$SOURCE_CONFIG" ]]; then
    _export_args+=(--config "$SOURCE_CONFIG")
fi
if [[ "$ENCRYPT" == "true" ]]; then
    _export_args+=(--encrypt)
fi
if [[ "$DRY_RUN" == "1" ]]; then
    _export_args+=(--dry-run)
fi

# ── 1. SOURCE: export on A ───────────────────────────────────────────────────
_log "step 1/3 — export on A: ${MNEMOS_BIN} ${_export_args[*]}"
if [[ "$DRY_RUN" == "1" ]]; then
    _log "dry-run: skipping actual export."
else
    # vesma sync export reads the passphrase from $VESMARO_SYNC_PASSPHRASE_ENV
    # (the NAME), which must be set in this process's environment.
    set +e
    "$MNEMOS_BIN" "${_export_args[@]}"
    rc=$?
    set -e
    if [[ $rc -ne 0 ]]; then
        _err "vesma sync export failed (exit $rc)."
        exit 1
    fi
fi

if [[ "$DRY_RUN" == "1" ]]; then
    _log "dry-run: would verify ${VESMARO_SYNC_LOCAL_EXPORT_DIR}/${REMOTE_FILE} exists."
else
    if [[ ! -f "${VESMARO_SYNC_LOCAL_EXPORT_DIR}/${REMOTE_FILE}" ]]; then
        _err "export produced no file at ${VESMARO_SYNC_LOCAL_EXPORT_DIR}/${REMOTE_FILE}."
        exit 1
    fi
fi

# ── 2. TRANSFER: rsync over ssh to B ─────────────────────────────────────────
# The rsync runs as ${PEER_USER}@${VESMARO_SYNC_PEER_HOST} and is restricted on
# B by rsync-wrapper.sh (contrib/systemd/rsync-wrapper.sh) pinned in authorized_keys.
# We use -e "ssh ..." so the wrapper receives the rsync server command via
# SSH_ORIGINAL_COMMAND and validates the destination against INCOMING_DIR.
_rsync_log_cmd=(rsync -az -e "ssh -i ${VESMARO_SYNC_PEER_SSH_KEY} ${_ssh_opts[*]}" "${VESMARO_SYNC_LOCAL_EXPORT_DIR}/${REMOTE_FILE}" "${PEER_USER}@${VESMARO_SYNC_PEER_HOST}:${VESMARO_SYNC_REMOTE_IMPORT_DIR}/${REMOTE_FILE}")
_log "step 2/3 — transfer: ${_rsync_log_cmd[*]}"
if [[ "$DRY_RUN" == "1" ]]; then
    _log "dry-run: skipping actual rsync."
else
    set +e
    rsync -az -e "ssh -i ${VESMARO_SYNC_PEER_SSH_KEY} ${_ssh_opts[*]}" \
        "${VESMARO_SYNC_LOCAL_EXPORT_DIR}/${REMOTE_FILE}" \
        "${PEER_USER}@${VESMARO_SYNC_PEER_HOST}:${VESMARO_SYNC_REMOTE_IMPORT_DIR}/${REMOTE_FILE}"
    rc=$?
    set -e
    if [[ $rc -ne 0 ]]; then
        _err "rsync transfer to ${PEER_USER}@${VESMARO_SYNC_PEER_HOST} failed (exit $rc)."
        exit 1
    fi
fi

# ── 3. IMPORT: trigger `vesma sync import` on B via ssh ──────────────────────
# The remote command is `vesma sync import <path> --passphrase-env <NAME>` —
# `source` is a POSITIONAL argument in the vesma CLI (see `vesma sync import
# --help`). On B, vesma-import-wrapper.sh (pinned in authorized_keys for the
# import key) rewrites the positional path to the absolute incoming path, pins
# --passphrase-env to the configured name, and rejects any other command. The
# passphrase value itself lives on B's environment (provisioned independently —
# never crosses the wire).
_remote_import_path="${VESMARO_SYNC_REMOTE_IMPORT_DIR%/}/${REMOTE_FILE}"
_import_remote_cmd=(vesma sync import "$_remote_import_path" --passphrase-env "$VESMARO_SYNC_PASSPHRASE_ENV")
if [[ "$DRY_RUN" == "1" ]]; then
    _import_remote_cmd+=(--dry-run)
fi

_log "step 3/3 — import on B: ssh -i ${VESMARO_SYNC_PEER_IMPORT_SSH_KEY} ... ${_import_remote_cmd[*]}"
if [[ "$DRY_RUN" == "1" ]]; then
    _log "dry-run: skipping actual ssh import trigger."
else
    set +e
    ssh -i "${VESMARO_SYNC_PEER_IMPORT_SSH_KEY}" "${_ssh_opts[@]}" \
        "${PEER_USER}@${VESMARO_SYNC_PEER_HOST}" \
        "${_import_remote_cmd[*]}"
    rc=$?
    set -e
    if [[ $rc -ne 0 ]]; then
        _err "remote vesma sync import failed (exit $rc)."
        exit 1
    fi
fi

_log "done — sync file: ${VESMARO_SYNC_LOCAL_EXPORT_DIR}/${REMOTE_FILE}"
exit 0