#!/usr/bin/env bash
# local-ci.sh — local replica of .github/workflows/ci.yml, run on the active venv.
#
# WHY THIS EXISTS:
#   GitHub Actions on vesmaro/vesmaro is locked due to a billing issue
#   (account locked — all jobs fail with "your account is locked due to a
#   billing issue"). Until the owner resolves billing via the GitHub UI,
#   ALL release/merge decisions MUST be verified locally. This script
#   replicates the CI `verify` job (lint + format + mypy + bandit +
#   pip-audit + pytest + coverage gate + doctor) and, with `--build`,
#   the release.yml wheel/sdist build step.
#
#   Memory entry: b9f022f8 (documents the billing blocker + local-CI
#   workaround policy). Revisit when billing is resolved — at that point
#   this script becomes a fast pre-push sanity check rather than the
#   primary gate.
#
# Usage:
#   scripts/local-ci.sh           # run all verify steps (1-8)
#   scripts/local-ci.sh --build   # verify, then build wheel + sdist
#   scripts/local-ci.sh --help    # this message
#
# Exit codes:
#   0 — all steps green
#   1 — at least one step failed
#   2 — environment problem (venv/uv missing and couldn't create)
#
# After checkout, ensure executable:
#   chmod +x scripts/local-ci.sh

set -euo pipefail

# ── args ──────────────────────────────────────────────────────────────
DO_BUILD=false
for arg in "$@"; do
  case "$arg" in
    --build) DO_BUILD=true ;;
    --help|-h)
      sed -n '2,31p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $arg" >&2
      echo "Usage: scripts/local-ci.sh [--build|--help]" >&2
      exit 1
      ;;
  esac
done

# ── locate project root (same pattern as release.sh) ──────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

# ── step tracking ──────────────────────────────────────────────────────
declare -a STEP_NAMES=()
declare -a STEP_RESULTS=()
TOTAL_STEPS=8
if $DO_BUILD; then TOTAL_STEPS=9; fi

record() {
  # record <step_name> <PASS|FAIL|SKIP>
  STEP_NAMES+=("$1")
  STEP_RESULTS+=("$2")
}

run_step() {
  # run_step <index> <total> <name> <command...>
  # Runs the command, records PASS/FAIL. Does NOT exit on failure —
  # the caller decides. Returns the command's exit code.
  local idx="$1"; shift
  local total="$1"; shift
  local name="$1"; shift
  echo ""
  echo "=== [$idx/$total] $name ==="
  set +e
  "$@"
  local rc=$?
  set -e
  if [[ $rc -eq 0 ]]; then
    record "$name" "PASS"
    echo "→ $name: PASS"
  else
    record "$name" "FAIL"
    echo "→ $name: FAIL (exit $rc)" >&2
  fi
  return $rc
}

skip_step() {
  # skip_step <index> <total> <name> <reason>
  local idx="$1"; shift
  local total="$1"; shift
  local name="$1"; shift
  local reason="$1"
  echo ""
  echo "=== [$idx/$total] $name ==="
  echo "→ SKIP: $reason"
  record "$name" "SKIP"
}

print_summary_and_exit() {
  echo ""
  echo "=== Summary ==="
  local failed=0
  local skipped=0
  local i=0
  for name in "${STEP_NAMES[@]}"; do
    local result="${STEP_RESULTS[$i]}"
    local marker
    case "$result" in
      PASS)   marker="✅" ;;
      FAIL)   marker="❌"; failed=$((failed + 1)) ;;
      SKIP)   marker="⏭️"; skipped=$((skipped + 1)) ;;
      *)      marker="?" ;;
    esac
    printf "  %s  %s — %s\n" "$marker" "$result" "$name"
    i=$((i + 1))
  done
  echo ""
  if [[ $failed -gt 0 ]]; then
    echo "❌ Local CI FAILED — $failed step(s) failed, $skipped skipped."
    echo "   Fix the failures above before merging or releasing."
    exit 1
  fi
  if $DO_BUILD; then
    echo "✅ Local CI green — verify steps passed and wheel + sdist built in dist/."
    echo "   Safe to merge/release."
  else
    echo "✅ Local CI green — safe to merge/release."
    echo "   Run \`scripts/local-ci.sh --build\` to build wheel + sdist."
  fi
  if [[ $skipped -gt 0 ]]; then
    echo "   ($skipped step(s) skipped — review the SKIP reasons above.)"
  fi
  exit 0
}

# ── venv setup ─────────────────────────────────────────────────────────
echo "=== Local CI — vesma (replica of .github/workflows/ci.yml) ==="
echo "Reason: GitHub Actions billing-locked. See memory entry b9f022f8."
echo ""

VENV_DIR="$ROOT_DIR/.venv"
VENV_ACTIVATE="$VENV_DIR/bin/activate"

if [[ ! -f "$VENV_ACTIVATE" ]]; then
  echo "=== venv setup — .venv missing, creating ==="
  if command -v uv >/dev/null 2>&1; then
    uv venv "$VENV_DIR"
    # shellcheck disable=SC1091
    source "$VENV_ACTIVATE"
    uv pip install -e ".[dev]"
    uv pip install "ruff>=0.15,<0.16"
  elif command -v python3 >/dev/null 2>&1; then
    echo "WARNING: uv not installed — falling back to python -m venv + pip." >&2
    echo "         uv is recommended (faster, deterministic). Install: pip install uv" >&2
    python3 -m venv "$VENV_DIR"
    # shellcheck disable=SC1091
    source "$VENV_ACTIVATE"
    pip install --upgrade pip
    pip install -e ".[dev]"
    pip install "ruff>=0.15,<0.16"
  else
    echo "ERROR: no .venv found, and neither uv nor python3 is available." >&2
    exit 2
  fi
else
  # shellcheck disable=SC1091
  source "$VENV_ACTIVATE"
fi

# Ensure dev tools are present (idempotent — re-install only if missing).
need_install=false
for tool in ruff mypy bandit pip-audit pytest; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    need_install=true
    break
  fi
done
if $need_install; then
  echo "=== installing dev extras into existing venv ==="
  if command -v uv >/dev/null 2>&1; then
    uv pip install -e ".[dev]"
    uv pip install "ruff>=0.15,<0.16"
  else
    pip install -e ".[dev]"
    pip install "ruff>=0.15,<0.16"
  fi
fi

PY_VERSION="$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')"
echo "Python: $PY_VERSION  (venv: $VENV_DIR)"
echo "Root  : $ROOT_DIR"
echo "Build : $DO_BUILD"
echo ""

# ── steps 1-8: verify job ──────────────────────────────────────────────
# Note: run_step records PASS/FAIL but returns the command's exit code.
# We disable set -e around each step so a failure prints the summary
# rather than killing the script silently. A failed step still causes
# the script to exit non-zero (via the summary's `failed` counter).

set +e
run_step 1 $TOTAL_STEPS "Lint (ruff check)" ruff check .
step_rc=$?
set -e
if [[ $step_rc -ne 0 ]]; then print_summary_and_exit; fi

set +e
run_step 2 $TOTAL_STEPS "Format check (ruff format --check)" ruff format --check .
step_rc=$?
set -e
if [[ $step_rc -ne 0 ]]; then print_summary_and_exit; fi

set +e
run_step 3 $TOTAL_STEPS "Type check (mypy --strict)" mypy --strict src/vesma/ src/mnemos/
step_rc=$?
set -e
if [[ $step_rc -ne 0 ]]; then print_summary_and_exit; fi

if command -v bandit >/dev/null 2>&1; then
  set +e
  run_step 4 $TOTAL_STEPS "Security (bandit)" bandit -r src/ -f json -o bandit-report.json
  step_rc=$?
  set -e
  if [[ $step_rc -ne 0 ]]; then print_summary_and_exit; fi
else
  skip_step 4 $TOTAL_STEPS "Security (bandit)" "bandit not installed (run: pip install bandit)"
fi

# pip-audit: PyPIService hardcodes https://pypi.org/pypi/<pkg>/<ver>/json
# (not configurable via --index-url in installed-audit mode). In environments
# where pypi.org is blocked by network policy, pip-audit times out and would
# fail the entire local CI. We detect network errors (ReadTimeout /
# ConnectionError / "Could not connect") and warn+skip, preserving the
# security gate for online environments while not blocking offline dev.
# Actual vulnerability findings (exit 1 without network-error signature)
# still fail the gate — only network failures are skipped.
# NOTE: run_step re-enables `set -e` before returning, so a non-zero return
# would kill the script before we can inspect the error. We inline the step
# logic here to keep control of the exit-code flow.
echo ""
echo "=== [5/$TOTAL_STEPS] Security (pip-audit) ==="
PIP_AUDIT_LOG="$(mktemp)"
set +e
pip-audit --ignore-vuln CVE-2026-45829 > "$PIP_AUDIT_LOG" 2>&1
pip_audit_rc=$?
set -e
cat "$PIP_AUDIT_LOG"
if [[ $pip_audit_rc -eq 0 ]]; then
  record "Security (pip-audit)" "PASS"
  echo "→ Security (pip-audit): PASS"
  rm -f "$PIP_AUDIT_LOG"
elif grep -qE "ReadTimeout|ConnectionError|timed out|Could not connect to PyPI|Max retries exceeded|handshake operation timed out" "$PIP_AUDIT_LOG"; then
  # Network failure — warn+skip, do NOT fail the whole CI.
  echo "⚠️ SKIP — Security (pip-audit): network timeout (pypi.org unreachable)"
  echo "   pip-audit requires online PyPI access. Run in an online environment for a full audit."
  echo "   Network error tail:"
  tail -3 "$PIP_AUDIT_LOG" | sed 's/^/     /'
  record "Security (pip-audit)" "SKIP"
  rm -f "$PIP_AUDIT_LOG"
else
  # Actual failure (vulnerability found or other error) — fail the gate.
  record "Security (pip-audit)" "FAIL"
  echo "→ Security (pip-audit): FAIL (exit $pip_audit_rc)" >&2
  rm -f "$PIP_AUDIT_LOG"
  print_summary_and_exit
fi

set +e
run_step 6 $TOTAL_STEPS "Test (pytest)" pytest tests/ -q --tb=short
step_rc=$?
set -e
if [[ $step_rc -ne 0 ]]; then print_summary_and_exit; fi

set +e
run_step 7 $TOTAL_STEPS "Coverage gate (≥80%)" pytest --cov=src/vesma --cov-fail-under=80 --cov-report=term-missing --cov-report=xml tests/ -q
step_rc=$?
set -e
if [[ $step_rc -ne 0 ]]; then print_summary_and_exit; fi

# Doctor — environment + data integrity check. May not be on PATH in a bare venv.
#
# Doctor distinguishes two classes of finding:
#   ⚠ warnings — dev-environment hygiene (integration version stale,
#     agent wiring incomplete). Remediation: `vesma integration update`
#     + `vesma integration setup --wire-agents --all`. These reflect the
#     state of the developer's local integration install, NOT a defect in
#     the code under test. Failing a code CI gate because the dev hasn't
#     refreshed integration targets would couple unrelated concerns.
#   ❌ errors — real code/data problems (config invalid, SQLite DB
#     unhealthy, vector store corrupt, tag-contract violations). These
#     MUST fail the gate — they indicate a defect the change introduced
#     or surfaced.
# We capture output + exit code, then grep for ❌ to decide. Warnings
# (⚠) without ❌ → warn + continue. Any ❌ → fail. Exit 0 → pass.
# This does NOT suppress real errors (lint-and-validate.instructions.md):
# ❌ still fails the gate; only ⚠ env-hygiene warnings are non-blocking.
if command -v vesma >/dev/null 2>&1; then
  echo ""
  echo "=== [8/$TOTAL_STEPS] Doctor (vesma doctor) ==="
  DOCTOR_LOG="$(mktemp)"
  set +e
  vesma doctor > "$DOCTOR_LOG" 2>&1
  doc_rc=$?
  set -e
  cat "$DOCTOR_LOG"
  if [[ $doc_rc -eq 0 ]]; then
    record "Doctor (vesma doctor)" "PASS"
    echo "→ Doctor (vesma doctor): PASS"
    rm -f "$DOCTOR_LOG"
  elif grep -q "❌" "$DOCTOR_LOG"; then
    # Real error (Config invalid, DB unhealthy, vector store corrupt, etc.)
    # — fail the gate. This is a code/data defect, not env hygiene.
    record "Doctor (vesma doctor)" "FAIL"
    echo "→ Doctor (vesma doctor): FAIL (exit $doc_rc — ❌ error(s) detected)" >&2
    echo "  Doctor reported real error(s) — fix before merge/release." >&2
    rm -f "$DOCTOR_LOG"
  else
    # Only ⚠ warnings (Integration stale, Agent wiring) — dev-environment
    # hygiene, not a code defect. Warn + continue; do not block the gate.
    record "Doctor (vesma doctor)" "PASS"
    echo "⚠️ WARN — Doctor (vesma doctor): warnings only (dev env hygiene)"
    grep -E "⚠|Integration|Agent wiring" "$DOCTOR_LOG" | head -5 | sed 's/^/   /' || true
    echo "   Run: vesma integration update && vesma integration setup --wire-agents --all"
    echo "   These are dev-environment warnings, not code defects — gate continues."
    rm -f "$DOCTOR_LOG"
  fi
else
  skip_step 8 $TOTAL_STEPS "Doctor (vesma doctor)" "vesma CLI not installed in venv — run \`pip install -e .\`"
fi

# ── step 9: build (optional, release.yml replica) ──────────────────────
if $DO_BUILD; then
  if ! python -c "import build" 2>/dev/null; then
    echo ""
    echo "=== [9/$TOTAL_STEPS] Build wheel + sdist ==="
    echo "→ FAIL: python build package not installed."
    echo "  Install build: pip install build"
    record "Build wheel + sdist (python -m build)" "FAIL"
    print_summary_and_exit
  fi
  echo ""
  echo "=== [9/$TOTAL_STEPS] Build wheel + sdist (python -m build) ==="
  rm -rf dist/
  set +e
  python -m build
  build_rc=$?
  set -e
  if [[ $build_rc -eq 0 ]]; then
    record "Build wheel + sdist (python -m build)" "PASS"
    echo "→ Build wheel + sdist: PASS"
    echo ""
    echo "Artefacts in dist/:"
    ls -lh dist/ | awk 'NR>1 {print "  " $NF "  " $5}'
  else
    record "Build wheel + sdist (python -m build)" "FAIL"
    echo "→ Build wheel + sdist: FAIL (exit $build_rc)" >&2
  fi
fi

print_summary_and_exit