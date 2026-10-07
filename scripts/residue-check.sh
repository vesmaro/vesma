#!/usr/bin/env bash
# residue-check.sh — rebrand residue gate for the 6.0 "clean sheet" sweep
# (contract: handoff/vesma-sweep-contract-gate.md v2, Zone A).
#
# WHAT IT DOES
#   Greps the LIVE surfaces (src, user/admin docs, integrations, deploy,
#   contrib, scripts, root docs) for the legacy brand names and fails with
#   exit 1 when a match is NOT covered by the allowlist below. Zone Б/В
#   (frozen wire/format constants, external artifacts, migration-UX
#   diagnostics) are allowlisted explicitly — every class carries the gate
#   item it traces back to.
#
# HOW TO UPDATE THE ALLOWLIST
#   1. A NEW legitimate occurrence must fall into an EXISTING class below —
#      if it does not, it is a naming tail: fix the surface, not the gate.
#   2. A genuinely new class (e.g. a new frozen wire constant agreed with
#      the mesh repo) is added ONLY with a comment naming the contract
#      (ADR / gate item / external artifact) and a TL sign-off — changes to
#      this file go through review like any contract line.
#   3. The printed match counts must be byte-stable between runs; a count
#      change without a diff is a finding, not noise.
#
# NOT in scope (checked nowhere, by design):
#   docs/project/**  — historical documents (Zone В.1, never rewritten)
#   CHANGELOG.md     — history (Zone В.1)
#   tests/           — legacy-behavior pins and mover fixtures (Zone В.2/В.4)
#   federation/proto, mnemos_core_api gencode — Zone Б (mesh window)

set -euo pipefail
cd "$(dirname "$0")/.."

# Live surfaces in scope.
PATHS=(src/vesma docs/en docs/ru integrations scripts contrib deploy
       compose.yaml pyproject.toml README.md README.ru.md CONTRIBUTING.md
       CONTRIBUTING.ru.md ARCHITECTURE.md NOTICE PLAN.md)

# Allowlist: every class is a legitimate old-name occurrence (gate item in parens).
ALLOW=(
  # Zone В.5 — permanent byte-stable trust marker (+ its GitHub anchors)
  'mnemos:no-federate'
  'mnemosno-federate'
  # Zone Б3 — store format magic (frozen until the mover window decision)
  'MNEMOS1'
  # Zone Б1/Б2 — federation wire identity (mesh window, ADR-0044 scope note)
  'MnemosCore'
  'mnemoscore'
  'mnemos_core_api'
  'mnemos-core'
  'mnemos.federation'
  'vesmaro.federation'
  'mnemos_session'          # frozen session cookie name (ADR-0044)
  'mnemos.api.auth.fernet.v1' # frozen fernet salt (ADR-0044)
  'mnemos-agent-token'      # frozen token header type (federation auth)
  'x-mnemos-'               # frozen gateway wire headers (mesh contract)
  # Zone Б — external mesh/agent Go artifacts (renamed in the B4 window)
  'mnemos-mesh'
  'vesmaro-agent'
  '/run/mnemos/core.sock'
  'mnemos.core_'            # external mesh config keys (mesh.yaml)
  'mnemos.transport'
  # B3 store home — renamed by the mover wave, docs flip rides that wave
  '.mnemos'
  'mnemos.db'
  'mnemos.log'
  'mnemos.yaml'
  'mnemos-vault'
  # Real external packages (factual references, deprecation context only)
  'mnemos-memory-server'
  'vesma-memory-server'
  'pi-mnemos'
  'mnemos-pi'
  'mnemospi'
  'korrlabs/mnemos'
  # Real org coordinates (the GitHub org is named vesmaro today)
  'github.com/vesmaro'
  'github.com:vesmaro'
  'githubusercontent.com/vesmaro'
  'ghcr.io/vesmaro'
  '@vesmaro/'
  'shields.io/github/v/release/vesmaro'
  'canon.vesmaro.dev'       # upstream canon pin identities (vendor contract)
  'korrnals'                # legacy pre-rebrand registry namespace (factual)
  '(Vesmaro Project)'       # literal PyPI summary string of published dists
  # Stored-format constants written into record content (byte-stable)
  'retrieve via mnemos_retrieve'
  '[mnemos:<memory-id>'
  '[mnemos:{memory.id}'
  'mnemos:legacy'           # lax-mode tag written onto legacy rows
  # ADR-0044 Decision 6 — the tag dual-accept window (input alias; closes
  # with the 5.x EOL, removed no earlier than 6.1): code and prose that
  # accept/normalize the legacy tag spelling on READ paths
  'mnemos:<subtype>'
  'mnemos:*'
  'startswith("mnemos:")'
  'tag[len("mnemos:")'
  'tag.startswith("mnemos:")'
  '"mnemos:"'
  'mnemos_version'          # pre-6.0 export schema key, accepted on import
  'mnemos-side'             # the gateway side of the federation trust model
  # Stored-format constants written into store/internals (byte-stable,
  # same class as MNEMOS1 — renaming strands existing data)
  'mnemos_memories'         # vector collection name (existing collections)
  '__mnemos_fts5_no_match_placeholder__'  # FTS sentinel token (stored)
  # Service component-manifest $id (eyes/B-shim service window, ADR-0042)
  'vesmaro.org'
  # B3 mover — the module's whole purpose is naming the legacy store
  'store_migration'
  '`mnemos:`'               # backticked tag-spelling mentions (dual-accept/mover)
  'mnemos:checkpoint'       # dual-accept SQL literal (read window)
  'vesma|mnemos'            # dual-accept regex alternations (read window)
  '``mnemos_*``'            # retirement prose naming the removed wildcard
  'mnemos-validated'        # the mesh gateway (mnemos-mesh) validation term
  'MNEMOS_*'                # retirement notes on the retired env prefixes
  'VESMARO_MCP_BRAND'       # retirement note on the removed brand switch
  'mnemos-prod'             # prod-venv machine-map glob (owner host layout)
  'mnemos:integration'      # A8 deploy diagnostic naming the refused legacy block
  '``mnemos``'              # A8 prose naming the foreign/legacy key
  'legacy_key = "mnemos"'  # doctor diagnostic detector (A8)
  'gcw: tag'                # ai-brain migration doc reference (gcw history)
  'mnemos-B'                # federation default self id (identity continuity
                            # during the ADR-0044 transition window)
  '[mnemos:<id>'            # provenance header format mentions (stored)
  'mnemos:<known-subtype'   # dual-accept prose (input alias)
  'project:mnemos ->'       # mover report labels (naming the legacy slug)
  'startswith("mnemos/")'   # stale-legacy-token cleanup in agent wiring (A8)
  '"mnemos/completion/"'    # rc migration: removal target (A4 migration aid)
  'mnemos.bash'             # completion docstrings naming removal targets
  'vesmaro.bash'            # completion docstrings naming removal targets
  'several mnemos-A'        # federation docstring: external peer id example
  'stale legacy ``mnemos``' # doctor diagnostic prose (A8)
  '``mnemos/*``'            # wiring migration prose naming the stale token
  'section mnemos: ->'      # mover report labels (config section flip)
  'mnemos-vitals'           # external vitals repo provenance (vendor notes)
  'Project-Mnemos'          # external vitals repo path
  # Zone A8 — migration-UX diagnostics naming the old to explain the move
  'MNEMOS_API__'            # retired env spellings, named in retirement notes
  'VESMARO_API__'
  'VESMARO_SYNC_'
  'legacy `mnemos'          # pyproject note on the bytes-pinned corpus import
  'round-trip'              # ccr/cache_aligner marker round-trip prose
  'mnemos_*`/`vesmaro_*'    # retirement notes naming the removed spellings
  'VESMARO_*` эпохи'        # ru retirement notes
  '`VESMARO_*` (5.0'        # ru retirement notes
  '`VESMARO_*` выведено'    # ru retirement notes
  'из обращения VESMARO_'   # ru retirement notes
  'mnemos-*/vesmaro-*'      # compose header naming the renamed prefixes
  'GHCR_USER:-vesmaro'      # real ghcr username (registry identifies token)
  '# "vesmaro" (the registry' # image-publish registry note
  '5.x shipped src/vesmaro' # pypi-publish history note
  'org is named vesmaro'    # this gate file's own comment
  'residue-check'           # this gate file itself
  'vesmaro/vesma'           # real repo slug shorthand (org/repo)
  'no longer supported'
  'stale legacy mnemos'
  'legacy mnemos'
  'LEGACY mnemos'
  'retired'
  'deprecated'
  # Upstream-canon-pinned vendored schema bytes (B4/canon window)
  'integrations/schemas/'
)

# Build one big regex: grep -v (exclude) per allowlist entry, then count.
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
grep -rniI "mnemos\|vesmaro" "${PATHS[@]}" 2>/dev/null > "$tmp" || true

total=$(wc -l < "$tmp")
for pat in "${ALLOW[@]}"; do
  grep -viF -- "$pat" "$tmp" > "$tmp.2" || true
  mv "$tmp.2" "$tmp"
done

residue=$(wc -l < "$tmp")
echo "residue-check: scanned live surfaces, raw legacy-name lines: ${total}"
if [[ "$residue" -ne 0 ]]; then
  echo "residue-check: FAIL — ${residue} line(s) outside the allowlist:" >&2
  cat "$tmp" >&2
  exit 1
fi
echo "residue-check: OK — 0 residue lines outside the allowlist (Zone Б/В/A8 classes)"
