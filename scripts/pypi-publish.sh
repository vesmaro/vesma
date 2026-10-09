#!/usr/bin/env bash
# pypi-publish.sh — local PyPI publish pipeline for the vesma
# distribution FAMILY in ONE run (codified from the manual 5.5.0-train
# procedure): the primary dist (PyPI project: vesma) plus the live mirror
# (vesma-memory-server). Import package: vesma — since the 6.0.0
# rebrand (5.x shipped src/vesmaro); 6.0 is the clean sheet — the CLI
# entry points are `vesma` and `vesma-train` only (legacy alias commands
# retired with the 5.x line).
#
# WHY: GitHub Actions is billing-locked (#117) so the release workflow
# does not fire, and a first PyPI publish is an IRREVERSIBLE owner
# decision (project name + version immutability). This script prepares
# everything — name gates, version gates, wheel/sdist builds, twine
# checks, smoke installs — and STOPS before upload unless --publish is
# passed explicitly together with credentials.
#
# Mirror mechanics (formerly a manual runbook dance): for every candidate
# dist the script substitutes the name in pyproject.toml → rebuilds →
# re-runs the gates → uploads, and RESTORES pyproject.toml byte-identical
# afterwards (backup + EXIT/INT/TERM trap — the restore is guaranteed
# even on a mid-run crash; a post-restore `git diff` check fails loud).
#
# Usage:
#   scripts/pypi-publish.sh                 # check mode (default): gates + build + twine check + metadata smoke for EVERY candidate dist
#   scripts/pypi-publish.sh --dists "vesma" # restrict candidates (default: primary + mirror, see CANDIDATE_DISTS below)
#   scripts/pypi-publish.sh --full-smoke    # + install each wheel into a throwaway venv WITH deps, run `vesma --version`
#   scripts/pypi-publish.sh --publish       # run all checks + MANDATORY image build/smoke, then twine upload per dist, then image push+anon-verify (requires release tag, PyPI creds, GHCR_TOKEN)
#   scripts/pypi-publish.sh --publish --full-smoke    # recommended pre-upload combination
#   scripts/pypi-publish.sh --i-own-name    # allow upload when a PyPI project already exists (updates only)
#   scripts/pypi-publish.sh --reuse-dist    # skip build when dist/<name>/ already holds matching artifacts
#   scripts/pypi-publish.sh --dry-run       # print steps, no mutations, no upload (works without .venv)
#   scripts/pypi-publish.sh --help
#
# Container image (owner directive 2026-10-05, card vesma-ghcr-5x-parity):
# in --publish mode the image phase is MANDATORY and has NO skip flag.
# Ordering: image build + smoke BEFORE the first upload (an unusable
# image environment blocks the whole train before anything is published
# anywhere); image push + anonymous-pull verification AFTER all uploads
# succeeded. If the push fails after PyPI succeeded, the run ends with
# a loud RELEASE INCOMPLETE banner naming the exact catch-up command
# and exits non-zero. The implementation lives in ONE place —
# scripts/image-publish.sh (preflight|build|push|catch-up).
#
# Candidate dists (CANDIDATE_DISTS): default is the pyproject.toml name
# FIRST (canonical channel) then the `vesma-memory-server` mirror — the
# same code, a different PyPI name. Override per run:
#   CANDIDATE_DISTS="vesma" scripts/pypi-publish.sh
# Names are validated against ^[A-Za-z0-9][A-Za-z0-9._-]*$ (they are
# spliced into a sed expression and PyPI URLs).
#
# Gates (HARD in --publish mode, warning otherwise; per dist unless noted):
#   CL  changelog preflight: [Unreleased] in CHANGELOG.md has <3 content
#       lines while commits accumulated since the last tag → WARN only
#       (never blocks; run once)
#   G0  PyPI name per dist: 404 = free (first publish), 200 +
#       --i-own-name = update of our own project; a version already on
#       PyPI is ALWAYS an error (PyPI versions are immutable — bump and
#       rebuild, never re-upload)
#   G1  HEAD is exactly on a tag vX.Y.Z (once)
#   G2  tag version == pyproject.toml version (once)
#   G3  wheel/sdist filename version == pyproject.toml version (per dist)
#   G4  smoke-installed package version == pyproject.toml version (per dist)
#
# Credentials (--publish):
#   Preferred: PYPI_TOKEN env var (PyPI API token). Converted to
#   TWINE_USERNAME=__token__ / TWINE_PASSWORD — never written to disk.
#   Fallback: twine reads ~/.pypirc if it exists.
#
# Prereqs: venv with dev extras (.venv/ — bootstrap a release worktree
# ONLY via `uv sync --extra dev` so uv.lock pins hold; see the runbook's
# environment-canon section for why `uv pip install -e ".[dev]"` is
# forbidden here), git tag cut from a release branch merged to main,
# network access to pypi.org for G0 (--publish only), twine check and
# --full-smoke. For --publish also: a container builder (podman preferred;
# buildah/docker work — see scripts/image-publish.sh) and GHCR_TOKEN
# (classic PAT, repo+write:packages) — both are checked by the image
# preflight BEFORE any upload happens.
#
# First publish is an OWNER-executed step (irreversible on PyPI; the
# PyPI projects are live: vesma + vesma-memory-server — update uploads
# use --i-own-name). Name-matrix history + procedure + the npm twin of
# this pipeline (@vesmaro/vesma, packaging/npm/publish.sh):
#   docs/en/admin/runbooks/pypi-publish.md
#
# See: issue #122 (ADR-0017 Phase 0), scripts/image-publish.sh (the
# mandatory container image phase this script orders around the upload),
# scripts/local-release.sh (deprecated fallback: GitHub Release half)
#

set -euo pipefail

# args
FULL_SMOKE=false; PUBLISH=false; DRY_RUN=false; REUSE_DIST=false; I_OWN_NAME=false
DISTS_ARG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --full-smoke) FULL_SMOKE=true ;;
    --publish)    PUBLISH=true ;;
    --i-own-name) I_OWN_NAME=true ;;
    --reuse-dist) REUSE_DIST=true ;;
    --dry-run)    DRY_RUN=true ;;
    --dists)
      shift
      DISTS_ARG="${1:-}"
      [[ -n "$DISTS_ARG" ]] || { echo "ERROR: --dists requires a space-separated list" >&2; exit 1; }
      ;;
    --help|-h) awk 'NR>1 && /^set -/{exit} NR>1 {sub(/^#( |$)/,""); print}' "$0"; exit 0 ;;
    *) echo "ERROR: unknown arg: $1" >&2; exit 1 ;;
  esac
  shift
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

declare -a STEP_NAMES=() STEP_RESULTS=()
record() { STEP_NAMES+=("$1"); STEP_RESULTS+=("$2"); }

print_summary() {
  echo ""; echo "=== Summary ==="
  local failed=0 skipped=0 warned=0 i=0
  for name in "${STEP_NAMES[@]}"; do
    local r="${STEP_RESULTS[$i]}"
    local m; case "$r" in
      PASS) m="✅";;
      FAIL) m="❌"; failed=$((failed+1));;
      SKIP) m="⏭️"; skipped=$((skipped+1));;
      WARN) m="⚠️"; warned=$((warned+1));;
      *) m="?";;
    esac
    printf "  %s  %s — %s\n" "$m" "$r" "$name"; i=$((i+1))
  done
  echo ""
  [[ $failed -gt 0 ]] && { echo "❌ FAILED — $failed failed, $warned warned, $skipped skipped."; exit 1; }
  echo "✅ Complete — $skipped skipped, $warned warned."; exit 0
}

# --- pyproject.toml name-substitution safety net -----------------------------
# The mirror dist is built by rewriting the [project] name in
# pyproject.toml. The restore is guaranteed: a backup plus EXIT/INT/TERM
# traps, and after every restore a `git diff` check fails loud if the
# file is not byte-identical to the committed state.

PYBACKUP=""
restore_pyproject() {
  if [[ -n "$PYBACKUP" && -f "$PYBACKUP" ]]; then
    cp -- "$PYBACKUP" "$ROOT_DIR/pyproject.toml"
    rm -f -- "$PYBACKUP"
    PYBACKUP=""
    echo "→ pyproject.toml restored from backup"
    if git diff --exit-code --quiet -- pyproject.toml; then
      echo "✓ pyproject.toml verified byte-identical to git HEAD"
    else
      echo "ERROR: pyproject.toml differs from git HEAD after restore — inspect manually:" >&2
      echo "  git diff -- pyproject.toml   (backup copy was: $ROOT_DIR/pyproject.toml.pypi-publish.bak)" >&2
      exit 2
    fi
  fi
}
trap restore_pyproject EXIT
trap 'restore_pyproject; exit 130' INT
trap 'restore_pyproject; exit 143' TERM

# --- candidate dists ----------------------------------------------------------

PRIMARY_NAME=$(grep -m1 '^name' pyproject.toml | cut -d'"' -f2)
if [[ -n "$DISTS_ARG" ]]; then
  read -r -a CANDIDATE_DISTS <<< "$DISTS_ARG"
elif [[ -n "${CANDIDATE_DISTS:-}" ]]; then
  read -r -a CANDIDATE_DISTS <<< "$CANDIDATE_DISTS"
else
  CANDIDATE_DISTS=("$PRIMARY_NAME" "vesma-memory-server")
fi

if [[ "${#CANDIDATE_DISTS[@]}" -eq 0 ]]; then
  echo "ERROR: candidate dist list is empty" >&2; exit 1
fi
for d in "${CANDIDATE_DISTS[@]}"; do
  if [[ ! "$d" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    echo "ERROR: invalid dist name '$d' (expected PEP 508-ish ^[A-Za-z0-9][A-Za-z0-9._-]*\$)" >&2; exit 1
  fi
done

# venv (required for build/twine + smoke venvs are separate; dry-run
# executes no build tools, so it may run without a venv)
if [[ -f "$ROOT_DIR/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091  # venv path is created by uv sync / scripts/local-ci.sh
  source "$ROOT_DIR/.venv/bin/activate"
elif $DRY_RUN; then
  echo "→ DRY-RUN: .venv missing — continuing (dry-run executes no build tools)"
else
  echo "ERROR: .venv missing — bootstrap the release worktree with: uv sync --extra dev" >&2
  echo "  (NEVER 'uv pip install -e \".[dev]\"' — it bypasses uv.lock and breaks the bench pins;" >&2
  echo "   see docs/en/admin/runbooks/pypi-publish.md, environment canon)" >&2
  exit 2
fi

# --- pre-flight: tree, tag, versions --------------------------------------

echo "=== PyPI publish pipeline — dists: ${CANDIDATE_DISTS[*]} ==="
if [[ -f "$ROOT_DIR/pyproject.toml.pypi-publish.bak" ]]; then
  echo "ERROR: stale backup pyproject.toml.pypi-publish.bak found — a previous run crashed mid-substitution." >&2
  echo "  Compare it with pyproject.toml and remove it only if identical:" >&2
  echo "  cmp pyproject.toml pyproject.toml.pypi-publish.bak && rm pyproject.toml.pypi-publish.bak" >&2
  exit 2
fi
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "ERROR: dirty working tree" >&2; git status --short; exit 2
fi

# PEP 503 / wheel-filename normalization: hyphens and dots become underscores
# (dist "vesma" -> wheel "vesma-..."; mirror "vesma-memory-server" ->
# wheel "vesma_memory_server-...").
PYV=$(grep -m1 '^version' pyproject.toml | cut -d'"' -f2)

if git describe --tags --exact-match HEAD >/dev/null 2>&1; then
  TAG="$(git describe --tags --exact-match HEAD)"; ON_TAG=true
else
  TAG="(none)"; ON_TAG=false
fi
TAGV="${TAG#v}"

# G1 — on-tag check (hard for publish, warning for check mode)
if $ON_TAG; then
  echo "Tag:$TAG  pyproject:$PYV  Dists:${CANDIDATE_DISTS[*]}"
else
  if $PUBLISH && ! $DRY_RUN; then
    echo "ERROR [G1]: --publish requires HEAD exactly on a release tag (vX.Y.Z)." >&2
    echo "  Cut the release first: release/X.Y.Z → main → git tag vX.Y.Z → checkout." >&2
    exit 2
  fi
  echo "⚠ [G1 warning]: HEAD not on a tag — check mode continues, publish gate NOT satisfied."
  echo "  pyproject:$PYV  Dists:${CANDIDATE_DISTS[*]}"
fi

# G2 — tag == pyproject (hard for publish, warning otherwise)
if $ON_TAG && [[ "$TAGV" != "$PYV" ]]; then
  if $PUBLISH; then echo "ERROR [G2]: tag $TAG != pyproject $PYV" >&2; exit 2
  else echo "⚠ [G2 warning]: tag $TAG != pyproject $PYV"; fi
fi

# steps: changelog preflight -> image [preflight (+ build+smoke in --publish)]
#        -> per dist [G0 -> build -> G3 -> twine check -> G4 metadata smoke ->
#        [full smoke] -> [upload]] -> [image push+verify in --publish]
ND="${#CANDIDATE_DISTS[@]}"
TOTAL=$(( 1 + ND*5 ))
if $FULL_SMOKE; then TOTAL=$((TOTAL+ND)); fi
if $PUBLISH; then TOTAL=$((TOTAL+ND)); fi
# image phase rows: check mode = 1 (preflight); --publish = 3 (preflight,
# build+smoke, push+verify — the phase is unconditional, there is no skip)
if $PUBLISH; then TOTAL=$((TOTAL+3)); else TOTAL=$((TOTAL+1)); fi
IDX=0

# --- image phase (MANDATORY in --publish — owner directive 2026-10-05,
#     card vesma-ghcr-5x-parity). Build + smoke run BEFORE any upload so
#     an unusable image environment blocks the whole train before anything
#     is published anywhere. There is NO skip path here: a missing builder,
#     missing GHCR_TOKEN (--publish), or a failed build/smoke is a HARD
#     failure of the train. In check mode the cheap preflight runs as a
#     WARN-capable row (consistent with G0/G1 check-mode semantics).

if $PUBLISH; then
  IDX=$((IDX+1))
  echo ""
  echo "=== [$IDX/$TOTAL] Image preflight (builder, GHCR_TOKEN, version gates) ==="
  if $DRY_RUN; then
    echo "→ DRY-RUN: scripts/image-publish.sh preflight --strict"; record "Image preflight" "SKIP"
  else
    set +e; bash "$SCRIPT_DIR/image-publish.sh" preflight --strict; rc=$?; set -e
    if [[ $rc -ne 0 ]]; then record "Image preflight" "FAIL"; print_summary; fi
    record "Image preflight" "PASS"
  fi
  IDX=$((IDX+1))
  echo ""
  echo "=== [$IDX/$TOTAL] Image build + smoke (mandatory, before any upload) ==="
  if $DRY_RUN; then
    echo "→ DRY-RUN: scripts/image-publish.sh build"; record "Image build+smoke" "SKIP"
  else
    set +e; bash "$SCRIPT_DIR/image-publish.sh" build; rc=$?; set -e
    if [[ $rc -ne 0 ]]; then record "Image build+smoke" "FAIL"; print_summary; fi
    record "Image build+smoke" "PASS"
  fi
else
  IDX=$((IDX+1))
  echo ""
  echo "=== [$IDX/$TOTAL] Image preflight (check mode — warn-capable) ==="
  set +e; bash "$SCRIPT_DIR/image-publish.sh" preflight; rc=$?; set -e
  if [[ $rc -eq 0 ]]; then record "Image preflight" "PASS"
  elif [[ $rc -eq 3 ]]; then record "Image preflight" "WARN"
  else record "Image preflight" "FAIL"; print_summary; fi
fi

# --- CL: changelog preflight (WARN only — never blocks) ----------------------

IDX=$((IDX+1))
echo ""
echo "=== [$IDX/$TOTAL] CL changelog preflight ([Unreleased] hygiene) ==="
LAST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || true)
if [[ -n "$LAST_TAG" ]]; then
  COMMITS_SINCE=$(git log "${LAST_TAG}..HEAD" --oneline | wc -l | tr -d ' ')
else
  COMMITS_SINCE=0
fi
if [[ ! -f CHANGELOG.md ]]; then
  echo "⚠ [CL warning]: CHANGELOG.md missing"
  record "CL changelog preflight" "WARN"
else
  # content lines of the [Unreleased] section (up to the next '## ' header)
  UNREL_LINES=$(awk '/^## \[Unreleased\]/{f=1;next} f && /^## /{f=0} f && NF' CHANGELOG.md | wc -l | tr -d ' ')
  if [[ "$UNREL_LINES" -ge 3 ]]; then
    echo "✓ [Unreleased] holds $UNREL_LINES content lines"
    record "CL changelog preflight" "PASS"
  elif [[ "$COMMITS_SINCE" -eq 0 ]]; then
    echo "✓ [Unreleased] thin ($UNREL_LINES lines) but $COMMITS_SINCE commits since ${LAST_TAG:-<no tag>} — fresh cut, nothing owed"
    record "CL changelog preflight" "PASS"
  else
    echo "⚠ [CL warning]: [Unreleased] holds $UNREL_LINES content lines (< 3) while $COMMITS_SINCE commits accumulated since ${LAST_TAG}."
    echo "  Entries target [Unreleased] only (CONTRIBUTING.md) — backfill before the next changelog freeze."
    record "CL changelog preflight" "WARN"
  fi
fi

# --- per-dist pipeline --------------------------------------------------------

pipeline_for_dist() {
  local dist="$1"
  PKG_NAME="$dist"
  PKG_FS="${PKG_NAME//[-.]/_}"
  DIST_DIR="dist/$PKG_FS"
  local wheel sdist rc now WFV SFV HTTP PIP_INSTALL SMOKE_DIR SMV OUT

  # name substitution (backup + guaranteed trap restore)
  if $DRY_RUN; then
    echo ""
    echo "→ DRY-RUN [$PKG_NAME]: substitute pyproject.toml name → \"$PKG_NAME\" (backup + EXIT/INT/TERM trap restore), build into $DIST_DIR/"
  else
    PYBACKUP="$ROOT_DIR/pyproject.toml.pypi-publish.bak"
    cp pyproject.toml "$PYBACKUP"
    sed -i "1,/^name = /s/^name = \"[^\"]*\"/name = \"$PKG_NAME\"/" pyproject.toml
    now="$(grep -m1 '^name' pyproject.toml | cut -d'"' -f2)"
    if [[ "$now" != "$PKG_NAME" ]]; then
      echo "ERROR [$PKG_NAME]: pyproject.toml name substitution failed (got '$now')" >&2
      restore_pyproject
      record "G0 name gate [$PKG_NAME]" "FAIL"; print_summary
    fi
  fi

  # --- G0: PyPI name gate (per dist) ---------------------------------------

  # 404 = name free (first publish registers it); 200 = project exists (needs
  # --i-own-name to proceed, and an existing version is ALWAYS an error);
  # anything else = pypi.org unreachable (hard fail for --publish, warn-skip
  # in check mode).
  IDX=$((IDX+1))
  echo ""
  echo "=== [$IDX/$TOTAL] G0 PyPI name gate ($PKG_NAME) ==="
  if $DRY_RUN; then
    echo "→ DRY-RUN: curl https://pypi.org/simple/$PKG_NAME/"
    record "G0 name gate [$PKG_NAME]" "SKIP"
  elif ! command -v curl >/dev/null 2>&1; then
    echo "⚠ [G0 warning]: curl not found — name gate skipped"
    record "G0 name gate [$PKG_NAME]" "SKIP"
  else
    HTTP=$(curl -s -o /dev/null -w '%{http_code}' -m 15 "https://pypi.org/simple/${PKG_NAME}/" || echo 000)
    if [[ "$HTTP" == "404" ]]; then
      echo "✓ name '$PKG_NAME' is FREE on PyPI — first publish will register it"
      record "G0 name gate [$PKG_NAME]" "PASS"
    elif [[ "$HTTP" == "200" ]]; then
      if curl -s -m 15 "https://pypi.org/simple/${PKG_NAME}/" | grep -qE "${PKG_FS}-${PYV}(-|\.)"; then
        echo "ERROR [G0]: $PKG_NAME $PYV is ALREADY on PyPI — versions are immutable. Bump the version and rebuild." >&2
        record "G0 name gate [$PKG_NAME]" "FAIL"; print_summary
      elif $I_OWN_NAME; then
        echo "⚠ project exists on PyPI and --i-own-name given — proceeding as an update of OUR project"
        record "G0 name gate [$PKG_NAME]" "PASS"
      elif $PUBLISH; then
        echo "ERROR [G0]: name '$PKG_NAME' is TAKEN on PyPI (https://pypi.org/simple/${PKG_NAME}/)." >&2
        echo "  If (and only if) that PyPI project is ours, re-run with --i-own-name." >&2
        echo "  Otherwise pick a free name in pyproject.toml — see the runbook's name matrix." >&2
        record "G0 name gate [$PKG_NAME]" "FAIL"; print_summary
      else
        echo "⚠ [G0 warning]: name '$PKG_NAME' is TAKEN on PyPI — check mode continues; --publish would refuse it."
        echo "  Free fallbacks + procedure: docs/en/admin/runbooks/pypi-publish.md"
        record "G0 name gate [$PKG_NAME]" "SKIP"
      fi
    elif $PUBLISH; then
      echo "ERROR [G0]: cannot reach pypi.org (HTTP $HTTP) — publishing needs network. Aborting." >&2
      record "G0 name gate [$PKG_NAME]" "FAIL"; print_summary
    else
      echo "⚠ [G0 warning]: cannot reach pypi.org (HTTP $HTTP) — name gate skipped in check mode"
      record "G0 name gate [$PKG_NAME]" "SKIP"
    fi
  fi

  # --- build wheel + sdist (per dist, isolated outdir) ----------------------

  IDX=$((IDX+1))
  echo ""
  echo "=== [$IDX/$TOTAL] Build wheel + sdist ($PKG_NAME → $DIST_DIR/) ==="
  if $DRY_RUN; then echo "→ DRY-RUN: python -m build --outdir $DIST_DIR"; record "Build wheel+sdist [$PKG_NAME]" "SKIP"
  else
    if $REUSE_DIST && compgen -G "$DIST_DIR/${PKG_FS}-${PYV}*" >/dev/null; then
      echo "→ reuse $DIST_DIR/${PKG_FS}-${PYV}* (existing artifacts match version)"; record "Build wheel+sdist [$PKG_NAME]" "SKIP"
    else
      rm -rf "$DIST_DIR"
      # Vendor the gRPC stubs into the artifacts (issue #514 tail; cli-audit
      # 2026-10-08 finding #2): the wheel force-includes federation/gen/python
      # and the sdist carries it — regenerate FIRST so even a fresh checkout
      # ships the stubs (6.0.0 shipped without them and serve/fetch crashed
      # on mesh-enabled configs). gencode-guarded and idempotent; a failure
      # aborts the build loudly — a stubless wheel is exactly the defect
      # this step exists to prevent.
      echo "→ gen-proto (vendor the gRPC stubs into the artifacts)"
      if ! PYTHON="$(command -v python)" bash "$SCRIPT_DIR/gen-proto.sh"; then
        echo "ERROR [$PKG_NAME]: gen-proto failed — refusing to build stubless artifacts" >&2
        record "Build wheel+sdist [$PKG_NAME]" "FAIL"; print_summary
      fi
      python -c "import build" 2>/dev/null || pip install -q build
      set +e; python -m build --outdir "$DIST_DIR"; rc=$?; set -e
      if [[ $rc -ne 0 ]]; then record "Build wheel+sdist [$PKG_NAME]" "FAIL"; print_summary; fi
      record "Build wheel+sdist [$PKG_NAME]" "PASS"; find "$DIST_DIR" -maxdepth 1 -type f -printf '  %f  %k KB\n'
    fi
  fi

  if $DRY_RUN; then
    wheel="(dry-run)"; sdist="(dry-run)"
  else
    wheel=$(find "$DIST_DIR" -maxdepth 1 -name "${PKG_FS}-*.whl" | sort | head -1 || true)
    sdist=$(find "$DIST_DIR" -maxdepth 1 -name "${PKG_FS}-*.tar.gz" | sort | head -1 || true)
    [[ -n "$wheel" && -n "$sdist" ]] || { echo "ERROR [$PKG_NAME]: wheel or sdist missing in $DIST_DIR/" >&2; exit 2; }
  fi

  # G3 — artifact version: wheel/sdist filenames embed the version (twine
  # check below validates the metadata inside them)
  IDX=$((IDX+1))
  echo ""
  echo "=== [$IDX/$TOTAL] G3 artifact version gate ($PKG_NAME) ==="
  if $DRY_RUN; then echo "→ DRY-RUN: filename version check for $PKG_FS-$PYV"; record "G3 artifact version [$PKG_NAME]" "SKIP"
  else
    # PEP 427 wheel filename: {name}-{version}-{python}-{abi}-{platform}.whl —
    # versions contain no hyphens, so the version is exactly the first segment.
    WFV=$(basename "$wheel" | sed -E "s/^${PKG_FS}-([^-]+)-[^-]+-[^-]+-[^-]+\\.whl$/\\1/")
    SFV=$(basename "$sdist" | sed -E "s/^${PKG_FS}-([^-]+)\.tar\.gz$/\\1/")
    if [[ "$WFV" == "$PYV" && "$SFV" == "$PYV" ]]; then
      echo "✓ wheel=$WFV sdist=$SFV == pyproject=$PYV"; record "G3 artifact version [$PKG_NAME]" "PASS"
    else
      record "G3 artifact version [$PKG_NAME]" "FAIL"
      if $PUBLISH; then echo "ERROR [G3]: artifact versions (wheel=$WFV, sdist=$SFV) != pyproject $PYV" >&2; print_summary
      else echo "⚠ [G3 warning]: artifact versions (wheel=$WFV, sdist=$SFV) != pyproject $PYV"; fi
    fi
  fi

  # --- twine check (per dist) -------------------------------------------------

  IDX=$((IDX+1))
  echo ""
  echo "=== [$IDX/$TOTAL] twine check ($PKG_NAME) ==="
  if $DRY_RUN; then echo "→ DRY-RUN: twine check $DIST_DIR/*"; record "twine check [$PKG_NAME]" "SKIP"
  else
    python -c "import twine" 2>/dev/null || pip install -q twine
    set +e; twine check "$DIST_DIR"/*; rc=$?; set -e
    if [[ $rc -ne 0 ]]; then record "twine check [$PKG_NAME]" "FAIL"; print_summary; fi
    record "twine check [$PKG_NAME]" "PASS"
  fi

  # --- G4 metadata smoke: install wheel (--no-deps) into throwaway venv --------

  IDX=$((IDX+1))
  echo ""
  echo "=== [$IDX/$TOTAL] G4 metadata smoke ($PKG_NAME, --no-deps venv) ==="
  if $DRY_RUN; then echo "→ DRY-RUN: venv install --no-deps + version + integrations/scripts check"; record "G4 metadata smoke [$PKG_NAME]" "SKIP"
  else
    SMOKE_DIR=$(mktemp -d /tmp/vesma-pypi-smoke.XXXXXX)
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
    $PIP_INSTALL "$wheel" \
      && EXPECTED="$PYV" NAME="$PKG_NAME" "$SMV/bin/python" - <<'PY'
import os, importlib.resources as r
from importlib.metadata import version
name, expected = os.environ["NAME"], os.environ["EXPECTED"]
v = version(name)
assert v == expected, f"installed {v} != expected {expected}"
p = r.files("vesma")
assert (p / "integrations").is_dir(), "integrations/ missing from wheel — integration setup would break on pip installs"
assert (p / "scripts").is_dir(), "scripts/ missing from wheel — mcp-setup.sh would not be found"
print(f"✓ installed {name} {v}; integrations/ + scripts/ shipped")
PY
    rc=$?
    set -e
    rm -rf "$SMOKE_DIR"
    if [[ $rc -ne 0 ]]; then record "G4 metadata smoke [$PKG_NAME]" "FAIL"; print_summary; fi
    record "G4 metadata smoke [$PKG_NAME]" "PASS"
  fi

  # --- full smoke: install WITH deps, run the CLI (per dist) --------------------

  if $FULL_SMOKE; then
    IDX=$((IDX+1))
    echo ""; echo "=== [$IDX/$TOTAL] Full smoke ($PKG_NAME: throwaway venv, full deps, CLI) ==="
    if $DRY_RUN; then echo "→ DRY-RUN: venv install + vesma --version"; record "Full smoke [$PKG_NAME]" "SKIP"
    else
      SMOKE_DIR=$(mktemp -d /tmp/vesma-pypi-fullsmoke.XXXXXX)
      SMV="$SMOKE_DIR/.venv"
      FULL_PIP="$SMV/bin/pip install -q"
      if ! python -m venv "$SMV" 2>/dev/null || [[ ! -x "$SMV/bin/pip" ]]; then
        rm -rf "$SMV"
        python -m venv --without-pip "$SMV" && FULL_PIP="pip --python $SMV/bin/python install -q"
      fi
      set +e
      $FULL_PIP "$wheel" \
        && OUT="$("$SMV/bin/vesma" --version 2>&1)"; rc=$?
      set -e
      rm -rf "$SMOKE_DIR"
      if [[ $rc -eq 0 ]]; then echo "→ $OUT"; record "Full smoke [$PKG_NAME]" "PASS"
      else record "Full smoke [$PKG_NAME]" "FAIL"; print_summary; fi
    fi
  fi

  # --- publish (owner decision — explicit flag + creds required, per dist) -------

  if $PUBLISH; then
    IDX=$((IDX+1))
    echo ""; echo "=== [$IDX/$TOTAL] twine upload → PyPI ($PKG_NAME) ==="
    if $DRY_RUN; then echo "→ DRY-RUN: twine upload $DIST_DIR/*"; record "twine upload [$PKG_NAME]" "SKIP"
    else
      # credential resolution: PYPI_TOKEN → TWINE_* env; else ~/.pypirc; else abort
      if [[ -n "${PYPI_TOKEN:-}" ]]; then
        export TWINE_USERNAME="__token__"; export TWINE_PASSWORD="$PYPI_TOKEN"
      elif [[ ! -f "$HOME/.pypirc" ]] && [[ -z "${TWINE_USERNAME:-}" || -z "${TWINE_PASSWORD:-}" ]]; then
        echo "ERROR: no credentials — set PYPI_TOKEN (recommended) or configure ~/.pypirc" >&2
        record "twine upload [$PKG_NAME]" "FAIL"; print_summary
      fi
      set +e
      twine upload --non-interactive "$DIST_DIR"/*
      rc=$?; set -e
      if [[ $rc -eq 0 ]]; then
        record "twine upload [$PKG_NAME]" "PASS"
        echo ""
        echo "Published $PKG_NAME. Verify:  pip index versions ${PKG_NAME}   (or: curl -s -o /dev/null -w '%{http_code}' https://pypi.org/simple/${PKG_NAME}/)"
      else
        record "twine upload [$PKG_NAME]" "FAIL"; print_summary
      fi
    fi
  fi

  # restore the canonical pyproject.toml before the next dist
  if ! $DRY_RUN; then
    restore_pyproject
  fi
}

for d in "${CANDIDATE_DISTS[@]}"; do
  pipeline_for_dist "$d"
done

# --- image push + verify (MANDATORY in --publish, AFTER all uploads succeeded).
# If this fails after PyPI succeeded, image-publish.sh prints the loud
# RELEASE INCOMPLETE banner with the exact catch-up command, and this run
# exits non-zero — a published PyPI version without its container image is
# never reported as a green release.

if $PUBLISH; then
  IDX=$((IDX+1))
  echo ""
  echo "=== [$IDX/$TOTAL] Image push + anonymous verify (ghcr.io) ==="
  if $DRY_RUN; then
    echo "→ DRY-RUN: scripts/image-publish.sh push"; record "Image push+verify" "SKIP"
  else
    set +e; bash "$SCRIPT_DIR/image-publish.sh" push; rc=$?; set -e
    if [[ $rc -ne 0 ]]; then
      record "Image push+verify" "FAIL"
      print_summary   # exits 1 — the RELEASE INCOMPLETE banner is above
    fi
    record "Image push+verify" "PASS"
  fi
fi

# --- default exit: prepared, not published --------------------------------------

if ! $PUBLISH; then
  echo ""
  echo "════════════════════════════════════════════════════════════════"
  echo " HARD STOP — everything prepared, NOTHING uploaded to PyPI"
  echo " (dists: ${CANDIDATE_DISTS[*]})."
  echo " Uploads are OWNER-executed steps (projects live: vesma +"
  echo " vesma-memory-server; updates need --i-own-name; PyPI names/versions"
  echo " are immutable — runbook): docs/en/admin/runbooks/pypi-publish.md"
  echo "════════════════════════════════════════════════════════════════"
  echo " Ready-to-run publish command (version must NOT already be on PyPI):"
  echo "   export PYPI_TOKEN=<api-token>"
  echo "   export GHCR_TOKEN=<classic PAT, repo+write:packages>   # mandatory: image phase"
  echo "   git checkout vX.Y.Z && scripts/pypi-publish.sh --publish --full-smoke"
  echo " (default candidates = vesma + vesma-memory-server, both in one run;"
  echo "  restrict via --dists \"vesma\" or CANDIDATE_DISTS)"
fi

print_summary
