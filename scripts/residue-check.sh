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
# ALLOWLIST DISCIPLINE (cascade fix P2, 2026-10-07)
#   NO bare-word classes. A word like "deprecated" must NEVER exclude a
#   whole line: that hides a Zone-A tail sitting on the same line after the
#   word (silent erosion of the clean sheet + a trivial bypass). Instead:
#   * exact-token classes carry the legacy token itself (inherently
#     co-occurrent) plus its documented reason;
#   * the ONE prose class is the DIAG regex below: a diagnostic word and a
#     legacy name must co-occur inside the SAME pre-comment span (max 80
#     chars, stop at "#"). A tail in a trailing comment after a
#     diagnostic word is NOT covered — it is flagged.
#
# SELF-TEST (run: scripts/residue-check.sh --self-test)
#   Proves the pipeline is not blind. Uses the committed bait line
#   RESIDUE-BAIT below (a tail hidden after the word "deprecated"), runs
#   the SAME filter pipeline over a temp tree containing it WITHOUT the
#   neutralizer allowlist entry, and asserts the bait IS flagged. If the
#   bait survives the filters, the gate exits 2 — do not ship a blind gate.
#
# KNOWN LIMIT (cascade fix P3-5b, 2026-10-07)
#   A grep gate sees OCCURRENCES, not semantics: it cannot tell a write
#   of a legacy name from a read/diagnostic mention. The invariant
#   "the write path emits canonical vesma:* only" is therefore NOT carried
#   by this gate — it rides on the code pin-tests (first of all
#   tests/test_canonical_tag_prefix.py and the brand-guard pins). This
#   script is the residue census for NAMES on live surfaces, nothing more.
#
# HOW TO UPDATE THE ALLOWLIST
#   1. A NEW legitimate occurrence must fall into an EXISTING class below —
#      if it does not, it is a naming tail: fix the surface, not the gate.
#      Only exception: a genuinely new frozen contract (new wire constant,
#      new external artifact) — add the exact token WITH a comment naming
#      the contract and a TL sign-off. Never re-add a bare-word class.
#   2. The printed match counts must be byte-stable between runs; a count
#      change without a diff is a finding, not noise.
#
# NOT in scope (checked nowhere, by design):
#   docs/project/**  — historical documents (Zone В.1, never rewritten)
#   CHANGELOG.md     — history (Zone В.1)
#   tests/           — legacy-behavior pins and mover fixtures (Zone В.2/В.4)
#   federation/proto, mnemos_core_api gencode — Zone Б (mesh window)

set -euo pipefail
cd "$(dirname "$0")/.."

MODE="${1:-}"

# ── Committed bait (self-test) ────────────────────────────────────────────────
# A Zone-A tail hidden in a TRAILING COMMENT on a line whose prose says
# "deprecated" — the exact erosion shape the word-classes used to hide. The
# main run neutralizes this line via the 'RESIDUE-BAIT' allowlist entry;
# --self-test re-runs the identical filter pipeline over a temp tree with the
# same bait line and REQUIRES it to be flagged. If this line ever stops being
# flagged, the gate has gone blind — fix the pipeline, not the bait.
#
# deprecated compat helper — # RESIDUE-BAIT tail: the old mnemos_add call must be flagged by --self-test

# Live surfaces in scope. Every entry MUST exist (cascade fix P3-5a): a
# missing path would otherwise be silently skipped and narrow the surface.
PATHS=(src/vesma docs/en docs/ru integrations scripts contrib deploy
       compose.yaml pyproject.toml README.md README.ru.md CONTRIBUTING.md
       CONTRIBUTING.ru.md ARCHITECTURE.md NOTICE PLAN.md)
for p in "${PATHS[@]}"; do
  if [[ ! -e "$p" ]]; then
    echo "residue-check: FAIL — scan path does not exist: $p (refusing to silently narrow the surface)" >&2
    exit 2
  fi
done

# Exact-token classes: the legacy token itself + the documented reason.
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
  'mnemos_session'            # frozen session cookie name (ADR-0044)
  'mnemos.api.auth.fernet.v1' # frozen fernet salt (ADR-0044)
  'mnemos-agent-token'        # frozen token header type (federation auth)
  'x-mnemos-'                 # frozen gateway wire headers (mesh contract)
  # Zone Б — external mesh/agent Go artifacts (renamed in the B4 window)
  'mnemos-mesh'
  'vesmaro-agent'
  '/run/mnemos/core.sock'
  'mnemos.core_'              # external mesh config keys (mesh.yaml)
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
  'vesmaro/vesma'             # real repo slug shorthand (org/repo)
  'GHCR_USER:-vesmaro'        # real ghcr username (registry identifies token)
  'canon.vesmaro.dev'         # upstream canon pin identities (vendor contract)
  'vesmaro.org'               # service component-manifest $id (ADR-0042 window)
  'korrnals'                  # legacy pre-rebrand registry namespace (factual)
  '(Vesmaro Project)'         # literal PyPI summary string of published dists
  '5.x shipped src/vesmaro'   # pypi-publish history note (pinned sentence)
  # Stored-format constants written into record content (byte-stable)
  'retrieve via mnemos_retrieve'
  '[mnemos:<memory-id>'
  '[mnemos:<id>'
  '[mnemos:{memory.id}'
  'mnemos:legacy'             # lax-mode tag written onto legacy rows
  'mnemos_memories'           # vector collection name (existing collections)
  '__mnemos_fts5_no_match_placeholder__' # FTS sentinel token (stored)
  # ADR-0044 Decision 6 — the tag dual-accept window (input alias; closes
  # with the 5.x EOL, removed no earlier than 6.1): read-path code and prose
  'mnemos:<subtype>'
  'mnemos:<known-subtype'
  'mnemos:*'
  'mnemos:checkpoint'
  'mnemos:decision'
  'startswith("mnemos:")'
  'startswith("mnemos/")'
  'tag[len("mnemos:")'
  'tag.startswith("mnemos:")'
  '"mnemos:"'
  'vesma|mnemos'              # dual-accept regex alternations (read window)
  'mnemos_version'            # pre-6.0 export schema key, accepted on import
  'mnemos-side'               # the gateway side of the federation trust model
  'mnemos-validated'          # the mesh gateway (mnemos-mesh) validation term
  'mnemos-B'                  # federation default self id (identity continuity
                              # during the ADR-0044 transition window)
  # Mover (Zone В.2) — the module's whole purpose is naming the legacy store
  'store_migration'
  'project:mnemos ->'         # mover report labels (naming the legacy slug)
  'section mnemos: ->'        # mover report labels (config section flip)
  '`mnemos:`'                 # backticked tag-spelling mentions (dual-accept/mover)
  'mnemos-vitals'             # external vitals repo provenance (vendor notes)
  'Project-Mnemos'            # external vitals repo path
  # Pinned sentences/notes (specific wording, not a bare word)
  'MNEMOS_API__'              # retired env spellings, named in retirement notes
  'VESMARO_API__'
  'VESMARO_SYNC_'
  'MNEMOS_*'                  # retirement notes on the retired env prefixes
  'VESMARO_MCP_BRAND'         # retirement note on the removed brand switch
  '``mnemos_*``'              # retirement prose naming the removed wildcard
  '``mnemos/*``'              # wiring migration prose naming the stale token
  '``mnemos``'                # A8 prose naming the foreign/legacy key
  'mnemos-prod'               # prod-venv machine-map glob (owner host layout)
  'mnemos:integration'        # A8 deploy diagnostic naming the refused legacy block
  'mnemos.bash'               # completion docstrings/rc rules naming removal targets
  'vesmaro.bash'              # completion docstrings/rc rules naming removal targets
  '"mnemos/completion/"'      # rc migration: removal rule (A4 migration aid)
  'several mnemos-A'          # federation docstring: external peer id example
  'gcw: tag'                  # ai-brain migration doc reference (gcw history)
  'mnemos:open-question'      # vendor-pinned canon schema bytes (canon window)
  'mnemos:bogus'              # tag-contract example: the refused-unknown-subtype pair
  'mnemos:learning'           # tag-contract python example of the legacy input alias
  'vesma|vesmaro|mnemos'      # completion anchor: the program-name alternation
  'mnemos:session'            # vendor-pinned canon schema bytes (canon window)
  '``VESMARO_`` spellings'    # config.py retirement comment (pinned sentence)
  '``mnemos_retrieve`` round-trip' # ccr stored-format prose (pinned sentence)
  'MNEMOS_/VESMARO_ are retired'   # quadlet retirement comment (pinned)
  'mnemos-*/vesmaro-*'        # compose header naming the renamed prefixes
  '# "vesmaro" (the registry' # image-publish registry note
  'RESIDUE-BAIT'              # the committed self-test bait line (see header)
  'residue-check'             # this gate file itself
  'org is named vesmaro'      # this gate file's own comment
)

# The ONE prose diagnostic class (cascade fix P2): a diagnostic word and a
# legacy name co-occurring in the SAME pre-comment span (<=80 chars, stop at
# "#"). Directional on purpose: "diagnostic-word … legacy-name" is how a real
# migration sentence reads; a legacy tail sitting AFTER the diagnostic word in
# a trailing comment (or beyond the span) is NOT covered and gets flagged.
DIAG_RE='(deprecated|retired|no[ ]longer[ ]supported|legacy|вывед|вывел|устар)[^#]{0,80}(mnemos|vesmaro)'

die_missing_path() { # internal — kept for callers importing the guard
  echo "residue-check: FAIL — scan path does not exist: $1" >&2
  exit 2
}

collect() {
  # $1 = scan root prefix override (empty = repo); prints candidate lines
  local root="${1:-}"
  local args=()
  if [[ -n "$root" ]]; then
    args=("$root")
  else
    args=("${PATHS[@]}")
  fi
  grep -rniI "mnemos\|vesmaro" "${args[@]}" 2>/dev/null || true
}

filter() {
  # args: exact-token classes; stdin: candidate lines; stdout: residue lines
  local -a pats=("$@")
  local tmp tmp2
  tmp="$(mktemp)"; tmp2="$(mktemp)"
  cat > "$tmp"
  local pat
  for pat in "${pats[@]}"; do
    grep -viF -- "$pat" "$tmp" > "$tmp2" || true
    mv "$tmp2" "$tmp"
  done
  grep -viE -- "$DIAG_RE" "$tmp" > "$tmp2" || true
  mv "$tmp2" "$tmp"
  cat "$tmp"
  rm -f "$tmp"
}

main_run() {
  # NOTE: no EXIT trap here — a trap on a function-local variable fires
  # unbound at script exit under `set -u` (bash scope); clean up inline.
  local tmp
  tmp="$(mktemp)"
  collect > "$tmp"
  local total
  total=$(wc -l < "$tmp")
  local residue
  residue=$(filter "${ALLOW[@]}" < "$tmp" | wc -l)
  echo "residue-check: scanned live surfaces, raw legacy-name lines: ${total}"
  if [[ "$residue" -ne 0 ]]; then
    filter "${ALLOW[@]}" < "$tmp" >&2
    rm -f "$tmp"
    echo "residue-check: FAIL — ${residue} line(s) outside the allowlist:" >&2
    exit 1
  fi
  rm -f "$tmp"
  echo "residue-check: OK — 0 residue lines outside the allowlist (exact-token + DIAG classes)"
}

self_test() {
  local tmp
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  mkdir -p "$tmp/scripts"
  # The SAME bait sentence as the committed one, in a temp scan root.
  cat > "$tmp/scripts/bait.md" <<'EOF'
vesma_add ok  # deprecated compat helper — # RESIDUE-BAIT tail: the old mnemos_add call must be flagged by --self-test
EOF
  local out
  # The pipeline WITHOUT the RESIDUE-BAIT neutralizer: rebuild the exact-token
  # list from this file, minus the neutralizer entry.
  local allow_without_neutralizer=()
  local a
  for a in "${ALLOW[@]}"; do
    [[ "$a" == "RESIDUE-BAIT" ]] && continue
    allow_without_neutralizer+=("$a")
  done
  out="$(collect "$tmp" | filter "${allow_without_neutralizer[@]}")"
  if ! grep -q "mnemos_add call must be flagged" <<<"$out"; then
    echo "self-test: FAIL — the bait line was NOT flagged; the gate pipeline is blind." >&2
    exit 2
  fi
  # The neutralizer must work: WITH 'RESIDUE-BAIT' in the classes the same
  # bait is covered, so the main run can honestly reach zero.
  local out_neutralized
  out_neutralized="$(collect "$tmp" | filter "${ALLOW[@]}")"
  if grep -q "mnemos_add call must be flagged" <<<"$out_neutralized"; then
    echo "self-test: FAIL — the RESIDUE-BAIT neutralizer does not cover the bait." >&2
    exit 2
  fi
  echo "self-test: OK — the bait (tail after 'deprecated') is flagged without the neutralizer and covered with it."
  rm -rf "$tmp"
  trap - EXIT
}

case "$MODE" in
  --self-test) self_test ;;
  "") main_run ;;
  *) echo "usage: $0 [--self-test]" >&2; exit 2 ;;
esac
