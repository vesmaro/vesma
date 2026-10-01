#!/usr/bin/env bash
# pypi-publish.sh — local PyPI publish pipeline for the mnemos
# distribution (mnemos-memory-server on PyPI; import package: mnemos).
#
# WHY: GitHub Actions is billing-locked (#117) so the release workflow
# does not fire, and a first PyPI publish is an IRREVERSIBLE owner
# decision (project name + version immutability). This script prepares
# everything — name gate, version gates, wheel/sdist build, twine check,
# smoke install — and STOPS before upload unless --publish is passed
# explicitly together with credentials.
#
# Usage:
#   scripts/pypi-publish.sh                 # check mode (default): gates + build + twine check + metadata smoke
#   scripts/pypi-publish.sh --full-smoke    # + install wheel into a throwaway venv WITH deps, run `mnemos --version`
#   scripts/pypi-publish.sh --publish       # run all checks, then twine upload (requires release tag + creds)
#   scripts/pypi-publish.sh --publish --full-smoke    # recommended pre-upload combination
#   scripts/pypi-publish.sh --i-own-name    # allow upload when the PyPI project already exists (updates only)
#   scripts/pypi-publish.sh --reuse-dist    # skip build when dist/ already holds matching artifacts
#   scripts/pypi-publish.sh --dry-run       # print steps, no mutations, no upload
#   scripts/pypi-publish.sh --help
#
# Gates (HARD in --publish mode, warning otherwise):
#   G0  PyPI name: 404 = free (first publish), 200 + --i-own-name = update
#       of our own project; a version already on PyPI is ALWAYS an error
#       (PyPI versions are immutable — bump and rebuild, never re-upload)
#   G1  HEAD is exactly on a tag vX.Y.Z
#   G2  tag version == pyproject.toml version
#   G3  wheel/sdist filename version == pyproject.toml version
#   G4  smoke-installed package version == pyproject.toml version
#
# Credentials (--publish):
#   Preferred: PYPI_TOKEN env var (PyPI API token). Converted to
#   TWINE_USERNAME=__token__ / TWINE_PASSWORD — never written to disk.
#   Fallback: twine reads ~/.pypirc if it exists.
#
# Prereqs: venv with dev extras (.venv/), git tag cut from a release
# branch merged to main, network access to pypi.org for G0 (--publish
# only), twine check and --full-smoke.
#
# First publish is an OWNER-executed step (irreversible on PyPI; the
# distribution name is decided: mnemos-memory-server). Matrix history +
# procedure:
#   docs/en/admin/runbooks/pypi-publish.md
#
# See: issue #122 (ADR-0017 Phase 0), scripts/local-release.sh (sibling
# pipeline: container + GitHub Release)
#

set -euo pipefail

# args
FULL_SMOKE=false; PUBLISH=false; DRY_RUN=false; REUSE_DIST=false; I_OWN_NAME=false
for arg in "$@"; do
  case "$arg" in
    --full-smoke) FULL_SMOKE=true ;;
    --publish)    PUBLISH=true ;;
    --i-own-name) I_OWN_NAME=true ;;
    --reuse-dist) REUSE_DIST=true ;;
    --dry-run)    DRY_RUN=true ;;
    --help|-h) awk 'NR>1 && /^set -/{exit} NR>1 {sub(/^#( |$)/,""); print}' "$0"; exit 0 ;;
    *) echo "ERROR: unknown arg: $arg" >&2; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

declare -a STEP_NAMES=() STEP_RESULTS=()
record() { STEP_NAMES+=("$1"); STEP_RESULTS+=("$2"); }

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

# venv (required for build/twine + smoke venvs are separate)
if [[ -f "$ROOT_DIR/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091  # venv path is created by scripts/local-ci.sh / make bootstrap
  source "$ROOT_DIR/.venv/bin/activate"
else echo "ERROR: .venv missing — run scripts/local-ci.sh first" >&2; exit 2
fi

# --- pre-flight: tree, tag, versions --------------------------------------

echo "=== PyPI publish pipeline — mnemos ==="
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "ERROR: dirty working tree" >&2; git status --short; exit 2
fi

PKG_NAME=$(grep -m1 '^name' pyproject.toml | cut -d'"' -f2)
# PEP 503 / wheel-filename normalization: hyphens and dots become underscores
# ("mnemos-memory-server" -> wheel "mnemos_memory_server-...").
PKG_FS="${PKG_NAME//[-.]/_}"
PYV=$(grep -m1 '^version' pyproject.toml | cut -d'"' -f2)

if git describe --tags --exact-match HEAD >/dev/null 2>&1; then
  TAG="$(git describe --tags --exact-match HEAD)"; ON_TAG=true
else
  TAG="(none)"; ON_TAG=false
fi
TAGV="${TAG#v}"

# G1 — on-tag check (hard for publish, warning for check mode)
if $ON_TAG; then
  echo "Tag:$TAG  pyproject:$PYV  Name:$PKG_NAME"
else
  if $PUBLISH && ! $DRY_RUN; then
    echo "ERROR [G1]: --publish requires HEAD exactly on a release tag (vX.Y.Z)." >&2
    echo "  Cut the release first: release/X.Y.Z → main → git tag vX.Y.Z → checkout." >&2
    exit 2
  fi
  echo "⚠ [G1 warning]: HEAD not on a tag — check mode continues, publish gate NOT satisfied."
  echo "  pyproject:$PYV  Name:$PKG_NAME"
fi

# G2 — tag == pyproject (hard for publish, warning otherwise)
if $ON_TAG && [[ "$TAGV" != "$PYV" ]]; then
  if $PUBLISH; then echo "ERROR [G2]: tag $TAG != pyproject $PYV" >&2; exit 2
  else echo "⚠ [G2 warning]: tag $TAG != pyproject $PYV"; fi
fi

# steps: G0 (always) -> build -> G3 -> twine check -> G4 metadata smoke
#        -> [full smoke] -> [upload]
TOTAL=5
$FULL_SMOKE && TOTAL=$((TOTAL+1)) || true
$PUBLISH && TOTAL=$((TOTAL+1)) || true
IDX=0

# --- G0: PyPI name gate -------------------------------------------------------

# 404 = name free (first publish registers it); 200 = project exists (needs
# --i-own-name to proceed, and an existing version is ALWAYS an error);
# anything else = pypi.org unreachable (hard fail for --publish, warn-skip
# in check mode).
IDX=$((IDX+1))
echo ""
echo "=== [$IDX/$TOTAL] G0 PyPI name gate ($PKG_NAME) ==="
if $DRY_RUN; then
  echo "→ DRY-RUN: curl https://pypi.org/simple/$PKG_NAME/"
  record "G0 name gate" "SKIP"
elif ! command -v curl >/dev/null 2>&1; then
  echo "⚠ [G0 warning]: curl not found — name gate skipped"
  record "G0 name gate" "SKIP"
else
  HTTP=$(curl -s -o /dev/null -w '%{http_code}' -m 15 "https://pypi.org/simple/${PKG_NAME}/" || echo 000)
  if [[ "$HTTP" == "404" ]]; then
    echo "✓ name '$PKG_NAME' is FREE on PyPI — first publish will register it"
    record "G0 name gate" "PASS"
  elif [[ "$HTTP" == "200" ]]; then
    if curl -s -m 15 "https://pypi.org/simple/${PKG_NAME}/" | grep -qE "${PKG_FS}-${PYV}(-|\.)"; then
      echo "ERROR [G0]: $PKG_NAME $PYV is ALREADY on PyPI — versions are immutable. Bump the version and rebuild." >&2
      record "G0 name gate" "FAIL"; print_summary
    elif $I_OWN_NAME; then
      echo "⚠ project exists on PyPI and --i-own-name given — proceeding as an update of OUR project"
      record "G0 name gate" "PASS"
    elif $PUBLISH; then
      echo "ERROR [G0]: name '$PKG_NAME' is TAKEN on PyPI (https://pypi.org/simple/${PKG_NAME}/)." >&2
      echo "  If (and only if) that PyPI project is ours, re-run with --i-own-name." >&2
      echo "  Otherwise pick a free name in pyproject.toml — see the runbook's name matrix." >&2
      record "G0 name gate" "FAIL"; print_summary
    else
      echo "⚠ [G0 warning]: name '$PKG_NAME' is TAKEN on PyPI — check mode continues; --publish would refuse it."
      echo "  Free fallbacks + procedure: docs/en/admin/runbooks/pypi-publish.md"
      record "G0 name gate" "SKIP"
    fi
  elif $PUBLISH; then
    echo "ERROR [G0]: cannot reach pypi.org (HTTP $HTTP) — publishing needs network. Aborting." >&2
    record "G0 name gate" "FAIL"; print_summary
  else
    echo "⚠ [G0 warning]: cannot reach pypi.org (HTTP $HTTP) — name gate skipped in check mode"
    record "G0 name gate" "SKIP"
  fi
fi

echo ""

# --- 1. build wheel + sdist -------------------------------------------------

python -c "import build" 2>/dev/null || pip install -q build
IDX=$((IDX+1))
echo "=== [$IDX/$TOTAL] Build wheel + sdist ==="
if $DRY_RUN; then echo "→ DRY-RUN: python -m build"; record "Build wheel+sdist" "SKIP"
else
  if $REUSE_DIST && compgen -G "dist/${PKG_FS}-${PYV}*" >/dev/null; then
    echo "→ reuse dist/${PKG_FS}-${PYV}* (existing artifacts match version)"; record "Build wheel+sdist" "SKIP"
  else
    rm -rf dist/; set +e; python -m build; rc=$?; set -e
    if [[ $rc -ne 0 ]]; then record "Build wheel+sdist" "FAIL"; print_summary; fi
    record "Build wheel+sdist" "PASS"; find dist -maxdepth 1 -type f -printf '  %f  %k KB\n'
  fi
fi

WHEEL=$(find dist -maxdepth 1 -name "${PKG_FS}-*.whl" | sort | head -1 || true)
SDIST=$(find dist -maxdepth 1 -name "${PKG_FS}-*.tar.gz" | sort | head -1 || true)
[[ -n "$WHEEL" && -n "$SDIST" ]] || { echo "ERROR: wheel or sdist missing in dist/" >&2; exit 2; }

# G3 — artifact version: wheel/sdist filenames embed the version (twine
# check below validates the metadata inside them)
IDX=$((IDX+1))
echo ""
echo "=== [$IDX/$TOTAL] G3 artifact version gate ==="
if $DRY_RUN; then echo "→ DRY-RUN: filename version check"; record "G3 artifact version" "SKIP"
else
  # PEP 427 wheel filename: {name}-{version}-{python}-{abi}-{platform}.whl —
  # versions contain no hyphens, so the version is exactly the first segment.
  WFV=$(basename "$WHEEL" | sed -E "s/^${PKG_FS}-([^-]+)-[^-]+-[^-]+-[^-]+\\.whl$/\\1/")
  SFV=$(basename "$SDIST" | sed -E "s/^${PKG_FS}-([^-]+)\.tar\.gz$/\1/")
  if [[ "$WFV" == "$PYV" && "$SFV" == "$PYV" ]]; then
    echo "✓ wheel=$WFV sdist=$SFV == pyproject=$PYV"; record "G3 artifact version" "PASS"
  else
    record "G3 artifact version" "FAIL"
    if $PUBLISH; then echo "ERROR [G3]: artifact versions (wheel=$WFV, sdist=$SFV) != pyproject $PYV" >&2; print_summary
    else echo "⚠ [G3 warning]: artifact versions (wheel=$WFV, sdist=$SFV) != pyproject $PYV"; fi
  fi
fi

# --- twine check --------------------------------------------------------------

python -c "import twine" 2>/dev/null || pip install -q twine
IDX=$((IDX+1))
echo ""
echo "=== [$IDX/$TOTAL] twine check ==="
if $DRY_RUN; then echo "→ DRY-RUN: twine check dist/*"; record "twine check" "SKIP"
else
  set +e; twine check dist/*; rc=$?; set -e
  if [[ $rc -ne 0 ]]; then record "twine check" "FAIL"; print_summary; fi
  record "twine check" "PASS"
fi

# --- metadata smoke: install wheel (--no-deps) into throwaway venv ------------

IDX=$((IDX+1))
echo ""
echo "=== [$IDX/$TOTAL] G4 metadata smoke (--no-deps venv) ==="
if $DRY_RUN; then echo "→ DRY-RUN: venv install --no-deps + version + integrations/scripts check"; record "G4 metadata smoke" "SKIP"
else
  SMOKE_DIR=$(mktemp -d /tmp/mnemos-pypi-smoke.XXXXXX)
  SMV="$SMOKE_DIR/.venv"
  # Debian/Ubuntu often ships python3 without ensurepip (python3-venv not
  # installed): fall back to a pip-less venv + the OUTER pip targeting the
  # venv interpreter (--python, pip >= 22.3 — the build venv has current pip).
  PIP_INSTALL="$SMV/bin/pip install -q --no-deps"
  if ! python -m venv "$SMV" 2>/dev/null || [[ ! -x "$SMV/bin/pip" ]]; then
    rm -rf "$SMV"
    python -m venv --without-pip "$SMV" && PIP_INSTALL="pip --python $SMV/bin/python install -q --no-deps"
  fi
  set +e
  $PIP_INSTALL "$WHEEL" \
    && EXPECTED="$PYV" NAME="$PKG_NAME" "$SMV/bin/python" - <<'PY'
import os, importlib.resources as r
from importlib.metadata import version
name, expected = os.environ["NAME"], os.environ["EXPECTED"]
v = version(name)
assert v == expected, f"installed {v} != expected {expected}"
p = r.files("vesmaro")
assert (p / "integrations").is_dir(), "integrations/ missing from wheel — integration setup would break on pip installs"
assert (p / "scripts").is_dir(), "scripts/ missing from wheel — mcp-setup.sh would not be found"
print(f"✓ installed {name} {v}; integrations/ + scripts/ shipped")
PY
  rc=$?
  set -e
  rm -rf "$SMOKE_DIR"
  if [[ $rc -ne 0 ]]; then record "G4 metadata smoke" "FAIL"; print_summary; fi
  record "G4 metadata smoke" "PASS"
fi

# --- full smoke: install WITH deps, run the CLI --------------------------------

if $FULL_SMOKE; then
  IDX=$((IDX+1))
  echo ""; echo "=== [$IDX/$TOTAL] Full smoke (throwaway venv, full deps, CLI) ==="
  if $DRY_RUN; then echo "→ DRY-RUN: venv install + mnemos --version"; record "Full smoke" "SKIP"
  else
    SMOKE_DIR=$(mktemp -d /tmp/mnemos-pypi-fullsmoke.XXXXXX)
    SMV="$SMOKE_DIR/.venv"
    FULL_PIP="$SMV/bin/pip install -q"
    if ! python -m venv "$SMV" 2>/dev/null || [[ ! -x "$SMV/bin/pip" ]]; then
      rm -rf "$SMV"
      python -m venv --without-pip "$SMV" && FULL_PIP="pip --python $SMV/bin/python install -q"
    fi
    set +e
    $FULL_PIP "$WHEEL" \
      && OUT="$("$SMV/bin/mnemos" --version 2>&1)"; rc=$?
    set -e
    rm -rf "$SMOKE_DIR"
    if [[ $rc -eq 0 ]]; then echo "→ $OUT"; record "Full smoke" "PASS"
    else record "Full smoke" "FAIL"; print_summary; fi
  fi
fi

# --- publish (owner decision — explicit flag + creds required) -----------------

if $PUBLISH; then
  IDX=$((IDX+1))
  echo ""; echo "=== [$IDX/$TOTAL] twine upload → PyPI ==="
  if $DRY_RUN; then echo "→ DRY-RUN: twine upload dist/*"; record "twine upload" "SKIP"
  else
    # credential resolution: PYPI_TOKEN → TWINE_* env; else ~/.pypirc; else abort
    if [[ -n "${PYPI_TOKEN:-}" ]]; then
      export TWINE_USERNAME="__token__"; export TWINE_PASSWORD="$PYPI_TOKEN"
    elif [[ ! -f "$HOME/.pypirc" ]] && [[ -z "${TWINE_USERNAME:-}" || -z "${TWINE_PASSWORD:-}" ]]; then
      echo "ERROR: no credentials — set PYPI_TOKEN (recommended) or configure ~/.pypirc" >&2
      record "twine upload" "FAIL"; print_summary
    fi
    set +e
    twine upload --non-interactive dist/*
    rc=$?; set -e
    if [[ $rc -eq 0 ]]; then
      record "twine upload" "PASS"
      echo ""
      echo "Published. Verify:  pip index versions ${PKG_NAME}   (or: curl -s -o /dev/null -w '%{http_code}' https://pypi.org/simple/${PKG_NAME}/)"
    else
      record "twine upload" "FAIL"; print_summary
    fi
  fi
fi

# --- default exit: prepared, not published --------------------------------------

if ! $PUBLISH; then
  echo ""
  echo "════════════════════════════════════════════════════════════════"
  echo " HARD STOP — everything prepared, NOTHING uploaded to PyPI."
  echo " First publish is an OWNER-executed step (name decided: see pyproject)."
  echo " (PyPI names/versions are immutable — see the runbook):"
  echo "   docs/en/admin/runbooks/pypi-publish.md"
  echo "════════════════════════════════════════════════════════════════"
  echo " Ready-to-run publish command (after owner decides the name):"
  echo "   export PYPI_TOKEN=<api-token>"
  echo "   git checkout vX.Y.Z && scripts/pypi-publish.sh --publish --full-smoke"
fi

print_summary
