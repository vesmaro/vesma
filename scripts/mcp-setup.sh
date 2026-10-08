#!/usr/bin/env bash
# scripts/mcp-setup.sh — register Vesma as an MCP server in VS Code
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/vesmaro/vesma/main/scripts/mcp-setup.sh | bash
#   curl -fsSL .../mcp-setup.sh | bash -s -- --scope workspace
#   curl -fsSL .../mcp-setup.sh | bash -s -- --scope user --data-dir ~/.mnemos --vault ~/vesma-vault
#
# Flags:
#   --scope SCOPE       VS Code config scope: user | workspace (default: user)
#   --data-dir PATH     VESMA_DATA_DIR (default: ~/.mnemos/data)
#   --vault PATH        VESMA_VAULT__VAULT_PATH (default: ~/.mnemos/vault)
#   --command CMD       Command to launch vesma (default: auto-detect: venv → system → vesma)
#   --auto-collect      Set VESMA_AUTO_COLLECT=1 (nag agent to save context)
#   --force             Overwrite an existing 'vesma' entry even if already present
#   --dry-run           Show what would be written, don't modify files
#   --help              Show this help
set -euo pipefail

SCOPE="user"
DATA_DIR="${HOME}/.mnemos/data"
VAULT_PATH="${HOME}/.mnemos/vault"
VESMA_CMD=""
AUTO_COLLECT=false
FORCE=false
DRY_RUN=false

if [[ -t 1 ]]; then
  GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
else
  GREEN=''; YELLOW=''; RED=''; CYAN=''; NC=''
fi
info()  { printf "${CYAN}ℹ${NC}  %s\n" "$*"; }
ok()    { printf "${GREEN}✓${NC}  %s\n" "$*"; }
warn()  { printf "${YELLOW}⚠${NC}  %s\n" "$*"; }
die()   { printf "${RED}✗${NC}  %s\n" "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scope)        SCOPE="$2"; shift 2 ;;
    --data-dir)     DATA_DIR="$2"; shift 2 ;;
    --vault)        VAULT_PATH="$2"; shift 2 ;;
    --command)      VESMA_CMD="$2"; shift 2 ;;
    --auto-collect) AUTO_COLLECT=true; shift ;;
    --force)        FORCE=true; shift ;;
    --dry-run)      DRY_RUN=true; shift ;;
    --help|-h)      sed -n '2,18p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *)              die "Unknown flag: $1 (use --help)" ;;
  esac
done

[[ "$SCOPE" != "user" && "$SCOPE" != "workspace" ]] && die "--scope must be 'user' or 'workspace'"

if [[ -z "$VESMA_CMD" ]]; then
  candidate="${HOME}/.mnemos/venv/bin/vesma"
  if [[ -x "$candidate" ]]; then
    VESMA_CMD="$candidate"; info "Found vesma at: ${VESMA_CMD}"
  elif command -v vesma &>/dev/null; then
    VESMA_CMD="$(command -v vesma)"; info "Found vesma on PATH: ${VESMA_CMD}"
  fi
fi
[[ -z "$VESMA_CMD" ]] && die "vesma executable not found. Install first: curl -fsSL https://raw.githubusercontent.com/vesmaro/vesma/main/scripts/install.sh | bash"

case "$(uname -s)" in
  Darwin)  VSCODE_USER_DIR="${HOME}/Library/Application Support/Code/User" ;;
  Linux)   VSCODE_USER_DIR="${HOME}/.config/Code/User" ;;
  *)       die "Unsupported OS: $(uname -s). Use WSL2 on Windows." ;;
esac

if [[ "$SCOPE" == "user" ]]; then
  MCP_FILE="${VSCODE_USER_DIR}/mcp.json"
else
  MCP_FILE="$(pwd)/.vscode/mcp.json"
fi
info "Target: ${SCOPE} scope → ${MCP_FILE}"

if [[ ! -f "$MCP_FILE" ]]; then
  info "mcp.json does not exist — creating."
  if [[ "$DRY_RUN" == true ]]; then
    info "[dry-run] Would create ${MCP_FILE} with vesma server entry."
  else
    mkdir -p "$(dirname "$MCP_FILE")"
    cat > "$MCP_FILE" <<JSONEOF
{
  "servers": {
    "vesma": {
      "type": "stdio",
      "command": "${VESMA_CMD}",
      "args": ["mcp-server"],
      "env": {
        "VESMA_DATA_DIR": "${DATA_DIR}",
        "VESMA_VAULT__VAULT_PATH": "${VAULT_PATH}"$( [[ "$AUTO_COLLECT" == true ]] && echo -e '\n        "VESMA_AUTO_COLLECT": "1"' )
      }
    }
  }
}
JSONEOF
    ok "Created ${MCP_FILE}"
  fi
else
  info "mcp.json exists — checking for existing 'vesma' entry…"
  if grep -q '"vesma"' "$MCP_FILE" 2>/dev/null; then
    if [[ "$FORCE" == true ]]; then
      warn "Existing 'vesma' entry found — overwriting (--force)."
    else
      ok "MCP server 'vesma' is already registered in ${MCP_FILE} — no changes needed."
      ok "Skipping MCP setup (already configured). Use --force to overwrite or --dry-run to inspect."
      exit 0
    fi
  fi
  if command -v python3 &>/dev/null; then
    info "Using Python for safe JSON merge…"
    if [[ "$DRY_RUN" == true ]]; then
      python3 -c "
import json
with open('$MCP_FILE') as f:
    cfg = json.load(f)
cfg.setdefault('servers', {})['vesma'] = {
    'type': 'stdio', 'command': '$VESMA_CMD', 'args': ['mcp-server'],
    'env': {'VESMA_DATA_DIR': '$DATA_DIR', 'VESMA_VAULT__VAULT_PATH': '$VAULT_PATH'$([ "$AUTO_COLLECT" == true ] && echo ", 'VESMA_AUTO_COLLECT': '1'")}
}
print(json.dumps(cfg, indent=2))
" | info "[dry-run] Would write:\n$(cat)"
    else
      python3 -c "
import json
with open('$MCP_FILE') as f:
    cfg = json.load(f)
cfg.setdefault('servers', {})['vesma'] = {
    'type': 'stdio', 'command': '$VESMA_CMD', 'args': ['mcp-server'],
    'env': {'VESMA_DATA_DIR': '$DATA_DIR', 'VESMA_VAULT__VAULT_PATH': '$VAULT_PATH'$([ "$AUTO_COLLECT" == true ] && echo ", 'VESMA_AUTO_COLLECT': '1'")}
}
with open('$MCP_FILE', 'w') as f:
    json.dump(cfg, f, indent=2); f.write('\n')
"
      ok "Merged 'vesma' into ${MCP_FILE}"
    fi
  else
    warn "python3 not found — using sed fallback (less safe for complex JSON)."
    if [[ "$DRY_RUN" == true ]]; then
      info "[dry-run] Would insert vesma entry."
    else
      sed -i.bak "/\"servers\"[[:space:]]*:[[:space:]]*{/a\\
    \"vesma\": { \"type\": \"stdio\", \"command\": \"${VESMA_CMD}\", \"args\": [\"mcp-server\"], \"env\": { \"VESMA_DATA_DIR\": \"${DATA_DIR}\", \"VESMA_VAULT__VAULT_PATH\": \"${VAULT_PATH}\" } },
" "$MCP_FILE"
      ok "Inserted 'vesma' into ${MCP_FILE} (backup: ${MCP_FILE}.bak)"
    fi
  fi
fi

echo ""
printf '%s✓%s  MCP server registered.\n' "$GREEN" "$NC"
printf "    Command:  %s mcp-server\n" "$VESMA_CMD"
printf "    Data dir: %s\n" "$DATA_DIR"
printf "    Vault:    %s\n" "$VAULT_PATH"
[[ "$AUTO_COLLECT" == true ]] && printf "    Auto-collect: enabled\n"
echo ""
printf '%sNext steps:%s\n' "$CYAN" "$NC"
printf '  1. Reload VS Code window (Ctrl+Shift+P → '"'"'Reload Window'"'"')\n'
printf '  2. Open Copilot Chat — vesma_* tools should appear in the tools picker\n'
printf '  3. Test: ask Copilot to '"'"'use vesma_add to save a memory'"'"'\n'