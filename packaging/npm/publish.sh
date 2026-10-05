#!/usr/bin/env bash
# packaging/npm/publish.sh — npm publish pipeline for @vesmaro/vesma
# (codified from the manual 5.5.0-train procedure). The npm manifest
# LIVES in the repo at packaging/npm/package.json — the legacy root
# package.json (pi-mnemos, frozen) is NOT touched by this pipeline.
#
# The package is a metadata/marker dist (README + manifest; no JS code):
# it feeds the npm version badge and the `vesma update` npm leg. The
# published 5.5.0 composition is package.json + README.md — this script
# reproduces exactly that from the checkout (the published tarball also
# carried a stray pack-preview.tgz from the manual flow; that accident
# is NOT reproduced — the files allowlist in package.json pins the
# composition deterministically).
#
# Usage:
#   packaging/npm/publish.sh                 # check mode: manifest gate + staging + npm pack + composition check (NO upload)
#   packaging/npm/publish.sh --compare-registry  # + compare local tarball composition vs the published version on the registry (WARN)
#   packaging/npm/publish.sh --publish       # run all checks, then npm publish (credentials + version-not-on-registry required)
#   packaging/npm/publish.sh --dry-run       # print steps, no mutations, no npm, no upload
#   packaging/npm/publish.sh --help
#
# Gates:
#   V1  packaging/npm/package.json version == pyproject.toml version
#       (hard in --publish; one version across all channels)
#   C1  packed tarball composition == { package/package.json,
#       package/README.md } exactly (deterministic artifact)
#   R0  registry: version already published = ALWAYS an error in
#       --publish (npm versions are immutable — bump and repack);
#       WARN in check mode
#   R1  --compare-registry: local file count / unpacked size vs the
#       published dist metadata (WARN on drift)
#
# Credentials (--publish):
#   Preferred: NPM_TOKEN env var — served to npm through a temporary
#   userconfig inside the throwaway staging dir (0600, trap-cleaned,
#   never written inside the repo). Fallback: ~/.npmrc.
#
# npm is required for check mode (pack) but NOT for --dry-run.
# Publishing is an OWNER-executed step (npm versions are immutable).
# Procedure + composition proof vs the published 5.5.0:
#   docs/en/admin/runbooks/pypi-publish.md (npm section)
#

set -euo pipefail

PUBLISH=false; DRY_RUN=false; COMPARE_REGISTRY=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --publish)          PUBLISH=true ;;
    --dry-run)          DRY_RUN=true ;;
    --compare-registry) COMPARE_REGISTRY=true ;;
    --help|-h) awk 'NR>1 && /^set -/{exit} NR>1 {sub(/^#( |$)/,""); print}' "$0"; exit 0 ;;
    *) echo "ERROR: unknown arg: $1" >&2; exit 1 ;;
  esac
  shift
done

PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$PKG_DIR/../.." && pwd)"

STAGING="$(mktemp -d /tmp/vesmaro-npm-pack.XXXXXX)"
trap 'rm -rf "$STAGING"' EXIT

# --- V1: manifest version == pyproject version --------------------------------

echo "=== npm publish pipeline — @vesmaro/vesma ==="
NPMV=$(grep -m1 '"version"' "$PKG_DIR/package.json" | cut -d'"' -f4)
PYV=$(grep -m1 '^version' "$ROOT_DIR/pyproject.toml" | cut -d'"' -f2)
echo "npm-manifest:$NPMV  pyproject:$PYV"
if [[ "$NPMV" != "$PYV" ]]; then
  if $PUBLISH; then
    echo "ERROR [V1]: packaging/npm/package.json version $NPMV != pyproject.toml $PYV — one version per release across ALL channels." >&2
    exit 2
  fi
  echo "⚠ [V1 warning]: npm manifest $NPMV != pyproject $PYV (check mode continues; --publish would refuse)"
else
  echo "✓ [V1] versions agree"
fi

# --- staging: manifest + README from the checkout ------------------------------

if $DRY_RUN; then
  echo "→ DRY-RUN: stage packaging/npm/package.json + README.md into a throwaway dir"
else
  cp "$PKG_DIR/package.json" "$STAGING/package.json"
  cp "$ROOT_DIR/README.md" "$STAGING/README.md"
fi

# --- pack ----------------------------------------------------------------------

TGZ="$STAGING/vesmaro-vesma-$NPMV.tgz"
if $DRY_RUN; then
  echo "→ DRY-RUN: (cd staging && npm pack) → vesmaro-vesma-$NPMV.tgz"
elif ! command -v npm >/dev/null 2>&1; then
  echo "ERROR: npm not found — install Node.js 20+ (check mode needs npm pack)" >&2
  exit 2
else
  (cd "$STAGING" && npm pack --silent >/dev/null 2>&1)
  [[ -f "$TGZ" ]] || { echo "ERROR: npm pack produced no $TGZ" >&2; exit 2; }
  echo "→ packed $(basename "$TGZ") ($(wc -c <"$TGZ" | tr -d ' ') bytes)"
fi

# --- C1: deterministic composition check ---------------------------------------

echo ""
echo "=== [C1] tarball composition gate ==="
if $DRY_RUN; then
  echo "→ DRY-RUN: tar -tzf must be exactly: package/package.json + package/README.md"
else
  tar -tzf "$TGZ" | sort >"$STAGING/composition.actual"
  printf 'package/README.md\npackage/package.json\n' | sort >"$STAGING/composition.expected"
  if diff -u "$STAGING/composition.expected" "$STAGING/composition.actual"; then
    echo "✓ [C1] composition is exactly { package/package.json, package/README.md }"
  else
    echo "ERROR [C1]: tarball composition drifted — the files allowlist in packaging/npm/package.json and the staging set must stay minimal." >&2
    exit 2
  fi
fi

# --- R0: version already on the registry? --------------------------------------

REG_JSON=""
reg_fetch() {
  curl -s -m 20 -H 'Accept: application/json' 'https://registry.npmjs.org/@vesmaro%2Fvesma' 2>/dev/null || true
}
echo ""
echo "=== [R0] registry version gate ==="
if $DRY_RUN; then
  echo "→ DRY-RUN: curl https://registry.npmjs.org/@vesmaro%2Fvesma (version $NPMV must NOT exist for --publish)"
elif command -v curl >/dev/null 2>&1; then
  REG_JSON=$(reg_fetch)
  if [[ -n "$REG_JSON" ]] && printf '%s' "$REG_JSON" | python3 -c "
import json, sys
d = json.load(sys.stdin)
sys.exit(0 if '$NPMV' in d.get('versions', {}) else 1)
" 2>/dev/null; then
    if $PUBLISH; then
      echo "ERROR [R0]: @vesmaro/vesma $NPMV is ALREADY published — npm versions are immutable. Bump and repack." >&2
      exit 2
    fi
    echo "⚠ [R0 warning]: $NPMV already published (expected — this is the routine verification of a shipped version)"
  else
    echo "✓ [R0] $NPMV not on the registry (or registry unreachable) — publish would register it"
  fi
else
  echo "⚠ [R0 warning]: curl not found — registry gate skipped"
fi

# --- R1: compare-registry (composition vs the published dist) -------------------

if $COMPARE_REGISTRY; then
  echo ""
  echo "=== [R1] compare-registry (local tarball vs published dist metadata) ==="
  if $DRY_RUN; then
    echo "→ DRY-RUN: compare local file count / unpacked size with registry dist metadata for $NPMV"
  elif [[ -z "$REG_JSON" ]]; then
    REG_JSON=$(reg_fetch)
  fi
  if [[ -z "$REG_JSON" ]]; then
    echo "⚠ [R1 warning]: registry unreachable — comparison skipped"
  else
    LOCAL_COUNT=$(tar -tzf "$TGZ" | grep -vc '/$' || true)
    printf '%s' "$REG_JSON" | REG_VERSION="$NPMV" LOCAL_FILES="$LOCAL_COUNT" python3 -c "
import json, os, sys
d = json.load(sys.stdin)
v = d['versions'].get(os.environ['REG_VERSION'])
if not v:
    print(f\"⚠ [R1 warning]: {os.environ['REG_VERSION']} not on registry yet — nothing to compare\")
    sys.exit(0)
dist = v.get('dist', {})
remote_fc = dist.get('fileCount')
local_fc = int(os.environ['LOCAL_FILES'])
print(f\"registry fileCount={remote_fc}, local fileCount={local_fc}\")
if remote_fc != local_fc:
    print('⚠ [R1 warning]: file-count drift vs published artifact — inspect the published tarball manually')
else:
    print('✓ [R1] file counts match')
"
  fi
fi

# --- publish (owner decision — explicit flag + creds required) ------------------

if $PUBLISH; then
  echo ""
  echo "=== npm publish @vesmaro/vesma@$NPMV ==="
  if $DRY_RUN; then
    echo "→ DRY-RUN: npm publish vesmaro-vesma-$NPMV.tgz --access public"
  else
    if [[ -n "${NPM_TOKEN:-}" ]]; then
      # temporary userconfig INSIDE the trap-cleaned staging dir; never in the repo
      NPMRC="$STAGING/.npmrc"
      printf '//registry.npmjs.org/:_authToken=%s\n' "$NPM_TOKEN" >"$NPMRC"
      chmod 600 "$NPMRC"
      export NPM_CONFIG_USERCONFIG="$NPMRC"
    elif [[ ! -f "$HOME/.npmrc" ]]; then
      echo "ERROR: no credentials — set NPM_TOKEN (recommended) or configure ~/.npmrc" >&2
      exit 2
    fi
    npm publish "$TGZ" --access public
    echo ""
    echo "Published. Verify:  npm view @vesmaro/vesma@$NPMV dist.fileCount"
  fi
fi

# --- default exit ----------------------------------------------------------------

if ! $PUBLISH; then
  echo ""
  echo "════════════════════════════════════════════════════════════════"
  echo " HARD STOP — artifact verified, NOTHING uploaded to npm."
  echo " Publishing is an OWNER-executed step (npm versions are immutable):"
  echo "   export NPM_TOKEN=<automation-token>"
  echo "   packaging/npm/publish.sh --publish"
  echo "════════════════════════════════════════════════════════════════"
fi

echo "✅ npm pipeline complete (publish: $PUBLISH, dry-run: $DRY_RUN)"
