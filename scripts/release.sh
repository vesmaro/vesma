#!/usr/bin/env bash
# release.sh — one-command release: bump version everywhere, commit, tag, push.
#
# Usage:
#   scripts/release.sh 2.7.6            # release version 2.7.6
#   scripts/release.sh 2.7.6 --no-push # bump + commit + tag, but don't push
#
# Updates version in: pyproject.toml, integrations/hermes/plugin.yaml,
# README.md, README.ru.md (inside <!-- version:xxx --> markers).
# Creates a single commit "chore: release vX.Y.Z", tags it, and pushes.
#
# The release workflow (.github/workflows/release.yml) triggers on the tag
# and builds wheel + sdist + Docker image. No CI-side commits needed.

set -euo pipefail

# ── args ──────────────────────────────────────────────────────────────
if [[ $# -lt 1 ]]; then
  echo "Usage: scripts/release.sh <version> [--no-push]" >&2
  echo "  version       e.g. 2.7.6 (no 'v' prefix)" >&2
  echo "  --no-push     create commit + tag, don't push" >&2
  exit 1
fi

VERSION="$1"
PUSH=true
[[ "${2:-}" == "--no-push" ]] && PUSH=false

# Strip 'v' prefix if accidentally included
VERSION="${VERSION#v}"

# Validate semver X.Y.Z
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "ERROR: '$VERSION' is not a valid semver (X.Y.Z)" >&2
  exit 1
fi

# ── locate project root ───────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

echo "=== Releasing v$VERSION ==="

# ── pre-flight checks ─────────────────────────────────────────────────

# Must be on main branch
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$BRANCH" != "main" ]]; then
  echo "ERROR: must be on 'main' branch (currently on '$BRANCH')" >&2
  exit 1
fi

# Working tree must be clean (no uncommitted changes)
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "ERROR: working tree has uncommitted changes. Commit or stash first." >&2
  git status --short
  exit 1
fi

# Tag must not already exist
if git tag -l "v$VERSION" | grep -q .; then
  echo "ERROR: tag v$VERSION already exists" >&2
  exit 1
fi

# Up-to-date with remote
git fetch origin main
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
if [[ "$LOCAL" != "$REMOTE" ]]; then
  echo "ERROR: local main ($LOCAL) != origin/main ($REMOTE). Pull or push first." >&2
  exit 1
fi

echo "  pre-flight checks: OK"

# ── bump versions ─────────────────────────────────────────────────────

# pyproject.toml — update the top-level version = "X.Y.Z"
PYPROJECT="pyproject.toml"
if ! grep -q "^version = \"$VERSION\"" "$PYPROJECT"; then
  OLD_VERSION=$(grep -m1 '^version' "$PYPROJECT" | sed -E 's/.*"([^"]+)".*/\1/')
  # Use perl for reliable in-place replacement
  perl -i -pe "s/^version = \"$OLD_VERSION\"/version = \"$VERSION\"/" "$PYPROJECT"
  echo "  $PYPROJECT: $OLD_VERSION → $VERSION"
else
  echo "  $PYPROJECT: already $VERSION"
fi

# integrations/hermes/plugin.yaml — version: "X.Y.Z"
PLUGIN="integrations/hermes/plugin.yaml"
if [[ -f "$PLUGIN" ]]; then
  if ! grep -q "^version: \"$VERSION\"" "$PLUGIN"; then
    OLD_PV=$(grep -m1 '^version:' "$PLUGIN" | sed -E 's/.*"([^"]+)".*/\1/')
    perl -i -pe "s/^version: \"$OLD_PV\"/version: \"$VERSION\"/" "$PLUGIN"
    echo "  $PLUGIN: $OLD_PV → $VERSION"
  else
    echo "  $PLUGIN: already $VERSION"
  fi
fi

# README.md + README.ru.md — update version markers
bash scripts/sync-readme-version.sh "$VERSION"

# ── verify consistency ────────────────────────────────────────────────

echo "  verifying version consistency..."
PYV=$(grep -m1 '^version' pyproject.toml | cut -d'"' -f2)
PLV=$(grep -m1 '^version:' "$PLUGIN" | cut -d'"' -f2)
if [[ "$PYV" != "$VERSION" || "$PLV" != "$VERSION" ]]; then
  echo "ERROR: version mismatch! pyproject=$PYV plugin=$PLV expected=$VERSION" >&2
  exit 1
fi
echo "  all versions consistent: $VERSION"

# ── commit + tag + push ───────────────────────────────────────────────

git add pyproject.toml "$PLUGIN" README.md README.ru.md

# Check if there's anything to commit
if git diff --cached --quiet; then
  echo "ERROR: no changes to commit — versions were already up to date" >&2
  exit 1
fi

git commit -m "chore: release v$VERSION"
echo "  committed: chore: release v$VERSION"

git tag "v$VERSION"
echo "  tagged: v$VERSION"

if $PUSH; then
  echo "  pushing..."
  git push origin main
  git push origin "v$VERSION"
  echo ""
  echo "✅ Released v$VERSION"
  echo "   Release workflow will build wheel + sdist + Docker image."
  echo "   Check: https://github.com/vesmaro/vesma/actions"
else
  echo ""
  echo "✅ Prepared v$VERSION (--no-push)"
  echo "   To publish: git push origin main && git push origin v$VERSION"
fi