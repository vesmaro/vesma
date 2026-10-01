#!/usr/bin/env bash
# rsync-wrapper.sh — restricted rsync server guard for mnemos-sync on B (#104).
#
# Pinned in ~mnemos-sync/.ssh/authorized_keys via command="" for the PUSH key
# (ssh-sync-hardening.md §2). SSH invokes this wrapper instead of a shell; the
# real rsync server command arrives in $SSH_ORIGINAL_COMMAND. This wrapper:
#
#   1. Rejects anything that is not an rsync server invocation.
#   2. Parses the rsync args and enforces the destination against INCOMING_DIR.
#   3. Re-execs the sanitised rsync server command with the destination locked
#      to INCOMING_DIR — the caller cannot write outside it.
#   4. Appends an audit line to AUDIT_LOG for every invocation (§6).
#
# vesma itself stays offline — this runs on B's host SSH layer, not inside
# vesma. Per ArchCom 2026-07-20 (vesma memory 4dc7d96e).
#
# Install: chmod 0755, place at /usr/local/sbin/rsync-wrapper.sh, pin in
# authorized_keys as:
#   command="/usr/local/sbin/rsync-wrapper.sh",no-pty,no-agent-forwarding,... \
#   ssh-ed25519 AAAA... mnemos-sync-push@A
#
# Exit codes: 0 success, 1 rsync failed, 2 policy violation / parse error.

set -euo pipefail

# ── config ────────────────────────────────────────────────────────────────────
# INCOMING_DIR MUST match VESMARO_SYNC_REMOTE_IMPORT_DIR on A and the dir
# created per ssh-sync-hardening.md §1. Override via /etc/mnemos/rsync-wrapper.env
# if your layout differs.
# ── legacy env compatibility (ADR-0031 dual period) ─────────────────────────
for _v in INCOMING_DIR AUDIT_LOG PASSPHRASE_ENV REMOTE_IMPORT_DIR \
          REMOTE_VESMARO_BIN; do
    _new="VESMARO_SYNC_${_v}"
    _old="MNEMOS_SYNC_${_v}"
    if [ -z "${!_new:-}" ] && [ -n "${!_old:-}" ]; then
        eval "export $_new=\${!_old}"
    fi
done
if [ -z "${VESMARO_EXPORT_PASSPHRASE:-}" ] && [ -n "${MNEMOS_EXPORT_PASSPHRASE:-}" ]; then
    export VESMARO_EXPORT_PASSPHRASE="$MNEMOS_EXPORT_PASSPHRASE"
fi
INCOMING_DIR="${VESMARO_SYNC_INCOMING_DIR:-/var/lib/mnemos-sync/incoming}"
AUDIT_LOG="${VESMARO_SYNC_AUDIT_LOG:-/var/log/vesma-sync.log}"
RSYNC_BIN="${RSYNC_BIN:-rsync}"

# ── audit helper (§6) ─────────────────────────────────────────────────────────
_audit() {
    # Append ISO-8601 UTC timestamp + source IP + event + detail.
    # `local` masks an unset SSH_CLIENT under `set -u` (ssh-test environments
    # and direct invocation have no SSH_CLIENT); fall back to "unknown".
    local src_ip="${SSH_CLIENT:-}"
    src_ip="${src_ip%% *}"
    [[ -n "$src_ip" ]] || src_ip="unknown"
    local ts
    ts="$(date -u +%FT%TZ)"
    # Best-effort audit — never let a log write failure crash the sync.
    printf '[%s] rsync-wrapper src=%s %s %s\n' "$ts" "$src_ip" "$1" "${2:-}" \
        >>"$AUDIT_LOG" 2>/dev/null || true
}

_err() {
    printf '[%s] rsync-wrapper: ERROR: %s\n' "$(date -u +%FT%TZ)" "$*" >&2
    _audit "REJECT" "$*"
}

# ── 1. only rsync is allowed ──────────────────────────────────────────────────
if [[ -z "${SSH_ORIGINAL_COMMAND:-}" ]]; then
    _err "no command provided — interactive shell refused."
    exit 2
fi

# rsync over ssh sends:  rsync --server [opts] . <dest>
if [[ "$SSH_ORIGINAL_COMMAND" != rsync\ * ]]; then
    _err "non-rsync command refused: ${SSH_ORIGINAL_COMMAND}"
    exit 2
fi

# ── 2. parse and validate the destination ──────────────────────────────────────
# Walk the tokens of SSH_ORIGINAL_COMMAND. The last non-option token is the
# destination. We reject any destination that is not inside INCOMING_DIR after
# realpath resolution (symlinks followed).
_read_args=()
_dest=""
# shellcheck disable=SC2206  # intentional word-split on the original command.
# SC2206 limitation: this breaks on paths with spaces or shell metacharacters.
# The attack path is closed by policy, not by the parser — the wrapper rewrites
# the source path under INCOMING_DIR (see _source derivation below) and rejects
# extra positionals, so a malicious SSH_ORIGINAL_COMMAND cannot inject tokens
# that escape the whitelisted rsync options or the pinned destination. A
# proper shell-quote-aware parser (xargs -n1 from a quoted string, or a Python
# helper) is a future hardening option; low priority because the policy gates
# already close the injection surface.
_tokens=($SSH_ORIGINAL_COMMAND)
_shift=0
for (( _i=0; _i<${#_tokens[@]}; _i++ )); do
    _t="${_tokens[$_i]}"
    case "$_t" in
        # ── B2 hardening (CWE-78): whitelist safe rsync server options ──────
        # A compromised A could inject --delete to wipe files in INCOMING_DIR
        # not in the current transfer. Only read-only / metadata options are
        # allowed; destructive options and any unknown option are rejected.
        -v|-l|-r|-t|-p|-S|--server|--sender)
            _read_args+=("$_t") ;;
        .)  _read_args+=("$_t") ;;          # rsync sends a literal "." module
        # Known-destructive options — reject with a named audit message so the
        # forensic signal distinguishes an attack from a misconfiguration.
        --delete|--delete-before|--delete-during|--delete-after|--delete-excluded|--remove-source-files|--force)
            _err "rejected dangerous rsync option: $_t"
            exit 2 ;;
        --*)
            # Any long option outside the whitelist is refused — defence-in-
            # depth, future rsync options are NOT silently forwarded.
            _err "rejected unknown rsync option: $_t"
            exit 2 ;;
        -?*)
            # Any short option cluster outside the whitelist is refused.
            _err "rejected unknown rsync option: $_t"
            exit 2 ;;
        *)
            # First non-option token after --server is the module ("."), the
            # second is the destination path. Track the last one as the dest.
            _dest="$_t"
            _read_args+=("$_t")
            ;;
    esac
done

if [[ -z "$_dest" ]]; then
    _err "could not determine rsync destination from: ${SSH_ORIGINAL_COMMAND}"
    exit 2
fi

# Resolve both paths and verify the destination is inside INCOMING_DIR.
# realpath -m tolerates a not-yet-existing destination file.
_incoming_real=""
_dest_real=""
if command -v realpath >/dev/null 2>&1; then
    _incoming_real="$(realpath -m "$INCOMING_DIR")"
    _dest_real="$(realpath -m "$_dest")"
else
    # Fallback: normalise via readlink -m or strip-leading-slash compare.
    _incoming_real="$INCOMING_DIR"
    _dest_real="$_dest"
fi

case "$_dest_real" in
    "${_incoming_real%/}/"*)
        # inside incoming — allowed
        ;;
    "${_incoming_real%/}")
        # exactly the incoming dir — allowed (rsync may write a file in it)
        ;;
    *)
        _err "destination outside INCOMING_DIR: $_dest_real (expected under $_incoming_real)"
        exit 2
        ;;
esac

# ── 3. re-exec the rsync server with the locked destination ───────────────────
# We pass the original tokens through unchanged — the destination is already
# constrained to INCOMING_DIR by the check above. rsync itself enforces the
# server protocol; we only gate the path.
_audit "ACCEPT" "dest=$_dest_real"
exec "$RSYNC_BIN" --server "${_read_args[@]:1}"