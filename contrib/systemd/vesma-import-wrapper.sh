#!/usr/bin/env bash
# vesma-import-wrapper.sh — restricted import trigger guard on B (#104).
#
# Pinned in ~vesma-sync/.ssh/authorized_keys via command="" for the TRIGGER
# key (ssh-sync-hardening.md §2). SSH invokes this wrapper instead of a shell;
# the real import command arrives in $SSH_ORIGINAL_COMMAND. This wrapper:
#
#   1. Rejects anything that is not a `vesma sync import` invocation.
#   2. Rewrites the positional source path to an absolute path under INCOMING_DIR
#      (the caller cannot read outside it).
#   3. Pins --passphrase-env to PASSPHRASE_ENV_NAME — the caller cannot inject
#      an arbitrary env var name to read another secret.
#   4. Appends an audit line to AUDIT_LOG for every invocation (§6).
#   5. Resolves the vesma CLI on B via VESMA_SYNC_REMOTE_VESMA_BIN or PATH.
#
# vesma itself stays offline — this runs on B's host SSH layer. Per ArchCom
# 2026-07-20 (vesma memory 4dc7d96e).
#
# Install: chmod 0755, place at /usr/local/sbin/vesma-import-wrapper.sh, pin in
# authorized_keys as:
#   command="/usr/local/sbin/vesma-import-wrapper.sh",no-pty,no-agent-forwarding,... \
#   ssh-ed25519 AAAA... vesma-sync-trigger@A
#
# Exit codes: 0 success, 1 import failed, 2 policy violation / parse error.

set -euo pipefail

# ── config ────────────────────────────────────────────────────────────────────
# INCOMING_DIR MUST match VESMA_SYNC_REMOTE_IMPORT_DIR on A and rsync-wrapper.sh.
# PASSPHRASE_ENV_NAME is the env var name that `vesma sync import` reads the
# passphrase from on B. Provision the VALUE on B's systemd environment — never
# on A and never inline in this file. Override via /etc/vesma/import-wrapper.env.
INCOMING_DIR="${VESMA_SYNC_INCOMING_DIR:-/var/lib/vesma-sync/incoming}"
PASSPHRASE_ENV_NAME="${VESMA_SYNC_PASSPHRASE_ENV:-VESMA_EXPORT_PASSPHRASE}"
AUDIT_LOG="${VESMA_SYNC_AUDIT_LOG:-/var/log/vesma-sync.log}"
VESMA_BIN="${VESMA_SYNC_REMOTE_VESMA_BIN:-}"

# ── audit helper (§6) ─────────────────────────────────────────────────────────
_audit() {
    # `local` masks an unset SSH_CLIENT under `set -u` only with `:-`; the bare
    # `${SSH_CLIENT%% *}` form still errors. Use the safe default-then-strip form.
    local src_ip="${SSH_CLIENT:-}"
    src_ip="${src_ip%% *}"
    [[ -n "$src_ip" ]] || src_ip="unknown"
    local ts
    ts="$(date -u +%FT%TZ)"
    printf '[%s] vesma-import-wrapper src=%s %s %s\n' "$ts" "$src_ip" "$1" "${2:-}" \
        >>"$AUDIT_LOG" 2>/dev/null || true
}

_err() {
    printf '[%s] vesma-import-wrapper: ERROR: %s\n' "$(date -u +%FT%TZ)" "$*" >&2
    _audit "REJECT" "$*"
}

# ── 1. only `vesma sync import` is allowed ───────────────────────────────────
if [[ -z "${SSH_ORIGINAL_COMMAND:-}" ]]; then
    _err "no command provided — interactive shell refused."
    exit 2
fi

# Accept:  vesma sync import <path> [--passphrase-env <name>] [--dry-run]
# Reject anything else (including `vesma sync export`, shells, other CLIs).
if [[ "$SSH_ORIGINAL_COMMAND" != "vesma sync import "* ]]; then
    _err "non-import command refused: ${SSH_ORIGINAL_COMMAND}"
    exit 2
fi

# ── 2. discover the vesma CLI on B ───────────────────────────────────────────
if [[ -z "$VESMA_BIN" ]]; then
    if command -v vesma >/dev/null 2>&1; then
        VESMA_BIN="$(command -v vesma)"
    elif [[ -x /usr/local/bin/vesma ]]; then
        VESMA_BIN=/usr/local/bin/vesma
    elif [[ -x /opt/vesma/.venv/bin/vesma ]]; then
        VESMA_BIN=/opt/vesma/.venv/bin/vesma
    else
        _err "vesma CLI not found on B. Set VESMA_SYNC_REMOTE_VESMA_BIN."
        exit 2
    fi
fi

# ── 3. parse the import command ───────────────────────────────────────────────
# Tokenise and rebuild a sanitised argv. The positional source path is the
# first non-option token; --passphrase-env is FORCED to PASSPHRASE_ENV_NAME
# regardless of what the caller sent (defence-in-depth: even if A is
# compromised and tries to point at another env var, B refuses).
_source=""
_passphrase_env_seen=0
_dry_run_seen=0
# shellcheck disable=SC2206
# SC2206 limitation: breaks on paths with spaces or shell metacharacters.
# The attack path is closed by policy — the wrapper rewrites the source path
# under INCOMING_DIR and rejects extra positionals + unknown --* flags (B3
# hardening), so token injection cannot escape the whitelisted import flags.
# A shell-quote-aware parser is a future hardening option; low priority
# because the policy gates already close the injection surface.
_tokens=($SSH_ORIGINAL_COMMAND)
# Drop the leading "vesma sync import" — already validated above.
_shift=3
for (( _i=$_shift; _i<${#_tokens[@]}; _i++ )); do
    _t="${_tokens[$_i]}"
    case "$_t" in
        --passphrase-env)
            _passphrase_env_seen=1
            ;;  # value follows — consumed by the next iteration
        --dry-run)
            _dry_run_seen=1
            ;;
        --*)
            # ── B3 hardening (CWE-78): whitelist import flags ─────────────
            # Only --dry-run is forwarded to `vesma sync import`. --passphrase-env
            # is pinned by the wrapper itself (above). Any other --* flag is
            # refused — a compromised A could otherwise redirect the import
            # source via --config or other file-reading flags.
            _err "rejected unknown import flag: $_t"
            exit 2 ;;
        *)
            if [[ $_passphrase_env_seen -eq 1 ]]; then
                # Caller's passphrase-env value — IGNORE, we pin our own.
                _passphrase_env_seen=0
            elif [[ -z "$_source" ]]; then
                _source="$_t"
            else
                _err "unexpected extra positional argument: $_t"
                exit 2
            fi
            ;;
    esac
done

if [[ -z "$_source" ]]; then
    _err "missing source path in: ${SSH_ORIGINAL_COMMAND}"
    exit 2
fi

# ── 4. lock the source path under INCOMING_DIR ────────────────────────────────
# The caller may send a relative or absolute path; we force it absolute under
# INCOMING_DIR. realpath -m tolerates a not-yet-existing source file.
_incoming_real=""
_source_real=""
if command -v realpath >/dev/null 2>&1; then
    _incoming_real="$(realpath -m "$INCOMING_DIR")"
    # If the caller sent a path already inside INCOMING_DIR, keep it; else join.
    case "$_source" in
        "$_incoming_real"/*) _source_real="$(realpath -m "$_source")" ;;
        /*) _source_real="$(realpath -m "$_source")" ;;
        *)  _source_real="$(realpath -m "${_incoming_real%/}/${_source}")" ;;
    esac
else
    _incoming_real="$INCOMING_DIR"
    case "$_source" in
        "$_incoming_real"/*) _source_real="$_source" ;;
        /*) _source_real="$_source" ;;
        *)  _source_real="${_incoming_real%/}/${_source}" ;;
    esac
fi

case "$_source_real" in
    "${_incoming_real%/}/"*)
        : ;;  # inside incoming — allowed
    *)
        _err "source path outside INCOMING_DIR: $_source_real (expected under $_incoming_real)"
        exit 2
        ;;
esac

if [[ ! -f "$_source_real" ]]; then
    _err "source file does not exist: $_source_real"
    exit 2
fi

# ── 5. build and exec the sanitised import command ────────────────────────────
# --passphrase-env is ALWAYS pinned to PASSPHRASE_ENV_NAME (defence-in-depth:
# even a compromised A cannot redirect the passphrase read to another var).
# --dry-run is the only caller-controlled flag we forward (whitelisted above).
_cmd=("$VESMA_BIN" sync import "$_source_real" --passphrase-env "$PASSPHRASE_ENV_NAME")
if [[ $_dry_run_seen -eq 1 ]]; then
    _cmd+=(--dry-run)
fi

_audit "ACCEPT" "source=$_source_real passphrase-env=$PASSPHRASE_ENV_NAME dry_run=$_dry_run_seen"
exec "${_cmd[@]}"