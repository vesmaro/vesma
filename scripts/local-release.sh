#!/usr/bin/env bash
# local-release.sh — full local release pipeline for mnemos.
#
# DEPRECATED (owner directive ecc205fc): superseded by Korrnals/release-pipeline
# (local symlink mode, connected 2026-08-30). Use instead:
#   ./scripts/run-release-local.sh --dry-run    # pipeline dry-run
#   ./scripts/run-release-local.sh              # pipeline release (next version)
# The pipeline covers everything this script did (verify gates, wheel/sdist,
# SHA256 + cosign + SBOM layers, Docker image, GitHub Release) plus signing
# layers this script never had. Kept as fallback until the first pipeline
# release (2.16.0) ships; Makefile targets local-release* still point here.
#
# WHY: GitHub Actions billing-locked (memory ef56d3b5). Tag-triggered
# release.yml does NOT fire. This script replicates it locally:
# verify → build wheel/sdist → build container image → push to ghcr.io
# → create GitHub Release with artifacts.
#
# Usage:
#   scripts/local-release.sh                  # full release
#   scripts/local-release.sh --skip-verify    # skip verify (use after local-ci.sh)
#   scripts/local-release.sh --dry-run        # print steps, no mutations
#   scripts/local-release.sh --no-image       # wheel/sdist only, no container
#   scripts/local-release.sh --no-release     # no GitHub Release creation
#   scripts/local-release.sh --help
#
# Prereqs: on release tag, clean tree, venv with dev extras, gh CLI auth,
# docker or buildah for image, python -m build available.
#
# See: memory ef56d3b5 (CI billing), b9f022f8 (mnemos local-CI workaround)

set -euo pipefail

# args
SKIP_VERIFY=false; DRY_RUN=false; NO_IMAGE=false; NO_RELEASE=false
for arg in "$@"; do
  case "$arg" in
    --skip-verify) SKIP_VERIFY=true ;;
    --dry-run)     DRY_RUN=true ;;
    --no-image)    NO_IMAGE=true ;;
    --no-release)  NO_RELEASE=true ;;
    --help|-h) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "ERROR: unknown arg: $arg" >&2; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

declare -a STEP_NAMES=() STEP_RESULTS=()
record() { STEP_NAMES+=("$1"); STEP_RESULTS+=("$2"); }

run_step() {
  local idx="$1"; shift; local total="$1"; shift; local name="$1"; shift
  echo ""; echo "=== [$idx/$total] $name ==="
  if $DRY_RUN; then echo "→ DRY-RUN: $*"; record "$name" "SKIP"; return 0; fi
  set +e; "$@"; local rc=$?; set -e
  if [[ $rc -eq 0 ]]; then record "$name" "PASS"; echo "→ $name: PASS"
  else record "$name" "FAIL"; echo "→ $name: FAIL ($rc)" >&2; fi
  return $rc
}

skip_step() {
  local idx="$1"; shift; local total="$1"; shift; local name="$1"; shift; local reason="$1"
  echo ""; echo "=== [$idx/$total] $name ==="; echo "→ SKIP: $reason"; record "$name" "SKIP"
}

print_summary() {
  echo ""; echo "=== Summary ==="
  local failed=0 skipped=0 i=0
  for name in "${STEP_NAMES[@]}"; do
    local r="${STEP_RESULTS[$i]}"
    local m; case "$r" in PASS) m="✅";; FAIL) m="❌"; failed=$((failed+1));; SKIP) m="⏭️"; skipped=$((skipped+1));; *) m="?";; esac
    printf "  %s  %s — %s\n" "$m" "$r" "$name"; i=$((i+1))
  done
  echo ""
  [[ $failed -gt 0 ]] && { echo "❌ FAILED — $failed failed, $skipped skipped."; exit 1; }
  echo "✅ Complete — $skipped skipped."; exit 0
}

# pre-flight
echo "=== Local release — mnemos (replica of release.yml) ==="
echo "Reason: GitHub Actions billing-locked. See memory ef56d3b5."
echo ""

if ! git describe --tags --exact-match HEAD >/dev/null 2>&1; then
  echo "ERROR: HEAD not on a tag. Checkout release tag (e.g. git checkout v2.12.1)" >&2; exit 2
fi
TAG="$(git describe --tags --exact-match HEAD)"
VERSION="${TAG#v}"
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "ERROR: dirty working tree" >&2; git status --short; exit 2
fi
PYV=$(grep -m1 '^version' pyproject.toml | cut -d'"' -f2)
[[ "$VERSION" == "$PYV" ]] || { echo "ERROR: tag $TAG != pyproject $PYV" >&2; exit 2; }
echo "Tag:$TAG  Version:$VERSION  Sanity:✓"
echo ""

TOTAL=0
$SKIP_VERIFY || TOTAL=$((TOTAL+1))
TOTAL=$((TOTAL+1))
$NO_IMAGE || TOTAL=$((TOTAL+2))
$NO_RELEASE || TOTAL=$((TOTAL+1))
IDX=0

# venv
[[ -f "$ROOT_DIR/.venv/bin/activate" ]] && source "$ROOT_DIR/.venv/bin/activate" || {
  echo "ERROR: .venv missing — run scripts/local-ci.sh first" >&2; exit 2; }

# 1. verify
if ! $SKIP_VERIFY; then
  IDX=$((IDX+1))
  [[ -x "$SCRIPT_DIR/local-ci.sh" ]] && { run_step $IDX $TOTAL "Verify" "$SCRIPT_DIR/local-ci.sh"; [[ $? -ne 0 ]] && print_summary; } || skip_step $IDX $TOTAL "Verify" "local-ci.sh not found"
fi

# 2. build wheel + sdist
IDX=$((IDX+1))
python -c "import build" 2>/dev/null || pip install build
echo ""; echo "=== [$IDX/$TOTAL] Build wheel + sdist ==="
if $DRY_RUN; then echo "→ DRY-RUN: python -m build"; record "Build wheel+sdist" "SKIP"
else
  rm -rf dist/; set +e; python -m build; rc=$?; set -e
  if [[ $rc -eq 0 ]]; then record "Build wheel+sdist" "PASS"; ls -lh dist/ | awk 'NR>1 {print "  "$NF"  "$5}'
  else record "Build wheel+sdist" "FAIL"; print_summary; fi
fi

# 3-4. container image
if ! $NO_IMAGE; then
  IMAGE="ghcr.io/vesmaro/vesma"
  BT=""; command -v buildah >/dev/null 2>&1 && BT="buildah" || { command -v docker >/dev/null 2>&1 && BT="docker"; }
  IDX=$((IDX+1))
  if [[ -z "$BT" ]]; then skip_step $IDX $TOTAL "Build image" "no buildah/docker"
  else
    echo ""; echo "=== [$IDX/$TOTAL] Build image ($BT) ==="
    if $DRY_RUN; then echo "→ DRY-RUN: $BT build -t $IMAGE:$VERSION -t $IMAGE:latest -f Containerfile ."; record "Build image" "SKIP"
    else
      set +e
      [[ "$BT" == "buildah" ]] && buildah bud -t "$IMAGE:$VERSION" -t "$IMAGE:latest" -f Containerfile . || docker build -t "$IMAGE:$VERSION" -t "$IMAGE:latest" -f Containerfile .
      rc=$?; set -e
      [[ $rc -eq 0 ]] && { record "Build image" "PASS"; echo "→ $IMAGE:$VERSION + :latest"; } || { record "Build image" "FAIL"; print_summary; }
    fi
  fi
  IDX=$((IDX+1))
  if [[ -z "$BT" ]]; then skip_step $IDX $TOTAL "Push image" "no build tool"
  else
    echo ""; echo "=== [$IDX/$TOTAL] Push image to ghcr.io ==="
    if $DRY_RUN; then echo "→ DRY-RUN: push $IMAGE:$VERSION + :latest"; record "Push image" "SKIP"
    else
      GH_TOKEN=$(gh auth token 2>/dev/null || true)
      set +e
      if [[ "$BT" == "buildah" ]]; then
        [[ -n "$GH_TOKEN" ]] && echo "$GH_TOKEN" | buildah login -u Korrnals --password-stdin ghcr.io 2>/dev/null || true
        buildah push "$IMAGE:$VERSION"; p1=$?; buildah push "$IMAGE:latest"; p2=$?
      else
        [[ -n "$GH_TOKEN" ]] && echo "$GH_TOKEN" | docker login ghcr.io -u Korrnals --password-stdin 2>/dev/null || true
        docker push "$IMAGE:$VERSION"; p1=$?; docker push "$IMAGE:latest"; p2=$?
      fi
      set -e
      [[ $p1 -eq 0 && $p2 -eq 0 ]] && { record "Push image" "PASS"; } || { record "Push image" "FAIL"; echo "  Auth: gh auth token | docker login ghcr.io -u Korrnals --password-stdin" >&2; print_summary; }
    fi
  fi
fi

# 5. GitHub Release
if ! $NO_RELEASE; then
  IDX=$((IDX+1))
  command -v gh >/dev/null 2>&1 || { skip_step $IDX $TOTAL "GitHub Release" "gh CLI not installed"; print_summary; }
  echo ""; echo "=== [$IDX/$TOTAL] GitHub Release (attach wheel+sdist) ==="
  if $DRY_RUN; then echo "→ DRY-RUN: gh release create $TAG dist/* --generate-notes"; record "GitHub Release" "SKIP"
  else
    set +e
    if gh release view "$TAG" >/dev/null 2>&1; then
      gh release upload "$TAG" dist/* --clobber; rc=$?
    else
      gh release create "$TAG" dist/* --generate-notes --title "v$VERSION"; rc=$?
    fi
    set -e
    [[ $rc -eq 0 ]] && { record "GitHub Release" "PASS"; echo "  https://github.com/vesmaro/vesmaro/releases/tag/$TAG"; } || { record "GitHub Release" "FAIL"; echo "  Check: gh auth status" >&2; }
  fi
fi

print_summary
