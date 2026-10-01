#!/bin/sh
# vesma-harness-pack installer — deploys the Vesma memory harness layer
# (16 vesma-* skills + the vesma-memory-ops instruction) into harness
# skill directories.
#
# Ownership contract: the memory-harness layer is owned by the Vesma
# project (integrations/harness), NOT by any agent framework. This script
# is the canonical installer; it is idempotent, records a per-target
# manifest with sha256 checksums, refuses to clobber files it did not
# install, and removes exactly what it installed on --uninstall.
#
# POSIX sh. No network, no secrets, no privileged operations.
#
# Usage:
#   ./install.sh                  install into ~/.zcode/skills and ~/.agents/skills
#   ./install.sh --target DIR     install into DIR instead of the defaults
#                                 (repeatable; --target disables the defaults)
#   ./install.sh --uninstall      remove exactly what was installed
#   ./install.sh --check          verify installed files against the manifest
#   ./install.sh --force          allow overwriting a foreign file at a pack path
#   ./install.sh --help
#
# See README.md in this directory for the layer contract.

set -eu

PACK_VERSION="1.0.0"
MANIFEST_NAME=".vesma-harness-pack.manifest"
PACK_DIR=$(cd "$(dirname "$0")" && pwd)
SKILLS_SRC="$PACK_DIR/skills"
INSTRUCTION_SRC="$PACK_DIR/instructions/memory-ops.md"
INSTRUCTION_DST_NAME="vesma-memory-ops.instructions.md"

MODE="install"
FORCE=0
DEFAULT_TARGETS="$HOME/.zcode/skills $HOME/.agents/skills"
TARGETS=""

die() { printf 'install.sh: error: %s\n' "$1" >&2; exit 1; }
info() { printf 'install.sh: %s\n' "$1"; }

need_checksum() {
    if command -v sha256sum >/dev/null 2>&1; then
        CHECKSUM="sha256sum"
    elif command -v shasum >/dev/null 2>&1; then
        CHECKSUM="shasum -a 256"
    else
        die "need sha256sum or shasum to track the install manifest"
    fi
}

checksum() {
    # checksum FILE -> prints hex digest
    if [ "$CHECKSUM" = "sha256sum" ]; then
        sha256sum "$1" | cut -d' ' -f1
    else
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

usage() {
    sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
}

[ -f "$INSTRUCTION_SRC" ] || die "pack incomplete: $INSTRUCTION_SRC not found"
[ -d "$SKILLS_SRC" ] || die "pack incomplete: $SKILLS_SRC not found"
need_checksum

while [ $# -gt 0 ]; do
    case "$1" in
        --uninstall) MODE="uninstall" ;;
        --check) MODE="check" ;;
        --force) FORCE=1 ;;
        --target)
            [ $# -ge 2 ] || die "--target requires a directory argument"
            TARGETS="$TARGETS $2"
            shift
            ;;
        --help|-h) usage; exit 0 ;;
        *) die "unknown option: $1 (see --help)" ;;
    esac
    shift
done

if [ -z "$TARGETS" ]; then
    TARGETS="$DEFAULT_TARGETS"
fi

pack_files() {
    # List TARGET-RELATIVE paths: every skill SKILL.md + the flat instruction.
    for d in "$SKILLS_SRC"/*/; do
        [ -f "${d}SKILL.md" ] && printf '%s\n' "$(basename "$d")/SKILL.md"
    done
    printf '%s\n' "$INSTRUCTION_DST_NAME"
}

src_of() {
    # src_of <target-relative-dest> -> absolute source path in the pack
    case "$1" in
        */SKILL.md) printf '%s\n' "$SKILLS_SRC/$1" ;;
        "$INSTRUCTION_DST_NAME") printf '%s\n' "$INSTRUCTION_SRC" ;;
        *) die "unknown pack file layout: $1" ;;
    esac
}

dst_of() {
    # dst_of <target> <target-relative-dest> -> absolute installed path
    printf '%s\n' "$1/$2"
}

install_into() {
    target=$1
    [ -d "$target" ] || mkdir -p "$target" || die "cannot create target dir: $target"
    manifest="$target/$MANIFEST_NAME"

    # Refuse to install into a target that holds a manifest from a DIFFERENT
    # pack generation without --force (stale manifest = uncertain ownership).
    if [ -f "$manifest" ] && [ "$FORCE" -eq 0 ]; then
        if ! grep -q "^# pack-version: $PACK_VERSION\$" "$manifest" 2>/dev/null; then
            die "$manifest exists from another pack version; re-run with --force to re-own this target"
        fi
    fi

    tmp="$manifest.tmp"
    {
        printf '# vesma-harness-pack manifest\n'
        printf '# pack-version: %s\n' "$PACK_VERSION"
        printf '# installed: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        printf '# target: %s\n' "$target"
    } >"$tmp"

    installed_count=0
    for rel in $(pack_files); do
        src=$(src_of "$rel")
        dst=$(dst_of "$target" "$rel")
        mkdir -p "$(dirname "$dst")"
        if [ -e "$dst" ]; then
            owned=0
            if [ -f "$manifest" ] && grep -q "  ${rel}\$" "$manifest" 2>/dev/null; then
                owned=1
            fi
            if [ "$owned" -eq 0 ] && [ "$FORCE" -eq 0 ]; then
                die "refusing to clobber non-pack file: $dst (remove it or pass --force)"
            fi
        fi
        cp "$src" "$dst"
        printf '%s  %s\n' "$(checksum "$dst")" "$rel" >>"$tmp"
        installed_count=$((installed_count + 1))
    done

    mv "$tmp" "$manifest"
    info "installed $installed_count files into $target (manifest: $MANIFEST_NAME)"
}

uninstall_from() {
    target=$1
    manifest="$target/$MANIFEST_NAME"
    if [ ! -f "$manifest" ]; then
        info "no manifest in $target — nothing to uninstall there"
        return 0
    fi
    removed=0
    while read -r sha rel; do
        case "$sha" in ''|\#*) continue ;; esac
        dst="$target/$rel"
        if [ -f "$dst" ]; then
            # Safety: only remove if content still matches what we installed,
            # or --force is set (content may have been legitimately edited).
            if [ "$(checksum "$dst")" = "$sha" ] || [ "$FORCE" -eq 1 ]; then
                rm -f "$dst"
                removed=$((removed + 1))
            else
                info "kept $dst (modified since install; use --force to remove anyway)"
            fi
        fi
        d=$(dirname "$dst")
        rmdir "$d" 2>/dev/null || true
    done <"$manifest"
    rm -f "$manifest"
    info "removed $removed files from $target"
}

check_target() {
    target=$1
    manifest="$target/$MANIFEST_NAME"
    if [ ! -f "$manifest" ]; then
        info "MISSING manifest in $target (pack not installed there?)"
        return 1
    fi
    rc=0
    while read -r sha rel; do
        case "$sha" in ''|\#*) continue ;; esac
        dst="$target/$rel"
        if [ ! -f "$dst" ]; then
            info "MISSING $dst"
            rc=1
        elif [ "$(checksum "$dst")" != "$sha" ]; then
            info "MODIFIED $dst"
            rc=1
        fi
    done <"$manifest"
    [ "$rc" -eq 0 ] && info "OK $target (all files match the manifest)"
    return "$rc"
}

rc=0
for t in $TARGETS; do
    case "$MODE" in
        install) install_into "$t" ;;
        uninstall) uninstall_from "$t" || rc=$? ;;
        check) check_target "$t" || rc=$? ;;
    esac
done
exit "$rc"
