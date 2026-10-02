#!/usr/bin/env bash
# scripts/smoke-federation.sh — local federation smoke test.
#
# Verifies the full Phase 0 federation roundtrip on a single host using
# two isolated vesma instances (per-instance config.yaml selected via
# the VESMA_CONFIG env var — the only instance-selection mechanism the
# app understands; see find_config_file in src/vesma/config.py):
#
#   1. Seed peer B with a clean decision memory.
#   2. Export B's memories as a compact federation payload.
#   3. Import the payload into peer A.
#   4. Search on A — the imported record must be findable.
#   5. Re-import the same payload — idempotent (skip, no duplicate).
#
# Prerequisites: vesma CLI on PATH (or set VESMA_BIN), jq, mktemp.
# Runtime: < 10 s. Exits 0 on success, non-zero on any failure.
#
# See docs/en/admin/federation-testing.md for the cross-host variant.
set -euo pipefail

VESMA_BIN="${VESMA_BIN:-vesma}"

command -v "$VESMA_BIN" >/dev/null 2>&1 || { echo "FATAL: $VESMA_BIN not on PATH"; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "FATAL: jq not on PATH"; exit 1; }

TMPDIR="$(mktemp -d -t vesma-smoke-XXXXXX)"
trap 'rm -rf "$TMPDIR"' EXIT

CONF_A="$TMPDIR/instance-a/config.yaml"
CONF_B="$TMPDIR/instance-b/config.yaml"
for inst_dir in "$TMPDIR/instance-a" "$TMPDIR/instance-b"; do
  mkdir -p "$inst_dir/data" "$inst_dir/vault"
  printf 'vesma:\n  data_dir: %s/data\n  vault_path: %s/vault\n' \
    "$inst_dir" "$inst_dir" > "$inst_dir/config.yaml"
done

PROJECT="smoke-fed"
AGENT_B="smoke-b"
PAYLOAD="$TMPDIR/compact.json"

echo "1. Seed peer B with a clean decision memory"
VESMA_CONFIG="$CONF_B" "$VESMA_BIN" add \
  "Smoke test: vesma federation verified via local roundtrip." \
  --tags "project:$PROJECT,agent:$AGENT_B,mnemos:decision" \
  --title "Federation smoke seed" >/dev/null

echo "2. Export B's memories as a compact federation payload"
VESMA_CONFIG="$CONF_B" "$VESMA_BIN" sync export \
  --output "$PAYLOAD" \
  --shared-projects "$PROJECT" >/dev/null

RECORD_COUNT=$(jq '.records | length' "$PAYLOAD")
[[ "$RECORD_COUNT" -ge 1 ]] || { echo "FAIL: export produced 0 records"; exit 1; }
echo "   exported $RECORD_COUNT record(s)"

echo "3. Import the payload into peer A"
IMPORT_OUT="$(VESMA_CONFIG="$CONF_A" "$VESMA_BIN" sync import "$PAYLOAD" 2>&1)"
IMPORTED=$(echo "$IMPORT_OUT" | grep -oE 'Imported: [0-9]+' | grep -oE '[0-9]+' || true)
IMPORTED="${IMPORTED:-0}"
[[ "$IMPORTED" -ge 1 ]] || { echo "FAIL: import did not import any record"; echo "$IMPORT_OUT"; exit 1; }
echo "   imported $IMPORTED record(s)"

echo "4. Search on A — imported record must be findable"
SEARCH_OUT="$(VESMA_CONFIG="$CONF_A" "$VESMA_BIN" search "federation smoke" --limit 3 2>&1)"
echo "$SEARCH_OUT" | grep -q "Federation smoke seed" \
  || { echo "FAIL: imported record not found in search"; echo "$SEARCH_OUT"; exit 1; }
echo "   search found the imported record"

echo "5. Re-import the same payload — idempotent (skip, no duplicate)"
REIMPORT_OUT="$(VESMA_CONFIG="$CONF_A" "$VESMA_BIN" sync import "$PAYLOAD" 2>&1)"
REIMPORTED=$(echo "$REIMPORT_OUT" | grep -oE 'Imported: [0-9]+' | grep -oE '[0-9]+' || true)
REIMPORTED="${REIMPORTED:-0}"
SKIPPED=$(echo "$REIMPORT_OUT" | grep -oE 'skipped: [0-9]+' | grep -oE '[0-9]+' || true)
SKIPPED="${SKIPPED:-0}"
[[ "$REIMPORTED" -eq 0 ]] || { echo "FAIL: re-import duplicated records"; echo "$REIMPORT_OUT"; exit 1; }
[[ "$SKIPPED" -ge 1 ]] || { echo "FAIL: re-import did not skip existing"; echo "$REIMPORT_OUT"; exit 1; }
echo "   re-import idempotent: imported=$REIMPORTED, skipped=$SKIPPED"

echo
echo "SMOKE PASS: federation A→B compact→import→search roundtrip works, idempotent re-import OK"