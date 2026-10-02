#!/usr/bin/env bash
# scripts/install.sh — one-command Vesma install
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/vesmaro/vesma/main/scripts/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --version 2.0.0 --extra mcp
#   curl -fsSL .../install.sh | bash -s -- --venv ~/.mnemos-venv --extra mcp,ollama
#
# Flags:
#   --version VERSION   Vesma version to install (default: latest from PyPI)
#   --extra EXTRAS      Comma-separated extras: ollama,openai,anthropic,gemini,dev,all
#                       (default: none — the MCP server ships in the base package since 4.1.0)
#   --venv PATH         Create a venv at PATH and install there (default: ~/.mnemos/venv)
#   --no-venv           Install into the current Python (system/user), no venv
#   --uv                Use uv instead of pip (auto-detected if available)
#   --mcp               Set up VS Code MCP integration automatically (no prompt)
#   --no-mcp            Skip VS Code MCP integration (no prompt)
#   --instructions      Deploy agent integration pack (instructions+skills+prompts) automatically (no prompt)
#   --no-instructions   Skip agent integration pack deployment (no prompt)
#   --wire-agents       Wire the Vesma MCP tools into Copilot agent tools: frontmatter automatically (no prompt)
#   --no-wire-agents    Skip Copilot agent MCP wiring (no prompt)
#   --container         Pull and run the container image instead of a Python install
#   --port PORT         Container host port (default: 8787, only with --container)
#   --help              Show this help
#
# The installer drops a `vesma` launcher into ~/.local/bin so the CLI works in
# any shell — no venv activation needed. If MCP is enabled it can wire VS Code
# for you (interactively, or via --mcp / --no-mcp).
set -euo pipefail

VERSION=""
EXTRAS=""
VENV_PATH="${HOME}/.mnemos/venv"
NO_VENV=false
USE_UV=false
CONTAINER=false
CONTAINER_PORT=8787
MCP_SETUP="ask"          # ask | yes | no
INSTRUCTIONS_SETUP="ask"  # ask | yes | no
WIRE_AGENTS="ask"        # ask | yes | no
LOCAL_BIN="${HOME}/.local/bin"

# ── Colour helpers ────────────────────────────────────────────────
if [[ -t 1 ]]; then
  GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
else
  GREEN=''; YELLOW=''; RED=''; CYAN=''; NC=''
fi
info()  { printf "${CYAN}ℹ${NC}  %s\n" "$*"; }
ok()    { printf "${GREEN}✓${NC}  %s\n" "$*"; }
warn()  { printf "${YELLOW}⚠${NC}  %s\n" "$*"; }
die()   { printf "${RED}✗${NC}  %s\n" "$*" >&2; exit 1; }

# Visual separator for interactive prompts — makes them stand out from info lines.
SEPARATOR="────────────────────────────────────────────────────────────"
prompt_ask() {
  # $1 — the question text (including the [Y/n] suffix)
  printf "\n%s\n[?] %s\n%s\n" "$SEPARATOR" "$1" "$SEPARATOR"
}

# ── Parse args ────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)  VERSION="$2"; shift 2 ;;
    --extra)    EXTRAS="$2"; shift 2 ;;
    --venv)     VENV_PATH="$2"; NO_VENV=false; shift 2 ;;
    --no-venv)  NO_VENV=true; shift ;;
    --uv)       USE_UV=true; shift ;;
    --mcp)             MCP_SETUP="yes"; shift ;;
    --no-mcp)          MCP_SETUP="no"; shift ;;
    --instructions)    INSTRUCTIONS_SETUP="yes"; shift ;;
    --no-instructions) INSTRUCTIONS_SETUP="no"; shift ;;
    --wire-agents)     WIRE_AGENTS="yes"; shift ;;
    --no-wire-agents)  WIRE_AGENTS="no"; shift ;;
    --container) CONTAINER=true; shift ;;
    --port)      CONTAINER_PORT="$2"; shift 2 ;;
    --help|-h)  sed -n '2,27p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *)          die "Unknown flag: $1 (use --help)" ;;
  esac
done

# ── Detect Python ─────────────────────────────────────────────────
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" &>/dev/null; then
    ver="$("$candidate" -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo 0)"
    if [[ "$(printf '%s\n' "3.11" "$ver" | sort -V | head -1)" == "3.11" ]]; then
      PYTHON="$candidate"; break
    fi
  fi
done
[[ -z "$PYTHON" ]] && die "Python >= 3.11 not found. Install from https://python.org or your package manager."
info "Using Python: $PYTHON ($($PYTHON --version 2>&1))"

# ── Detect uv ─────────────────────────────────────────────────────
if [[ "$USE_UV" == false ]] && command -v uv &>/dev/null; then
  USE_UV=true
  info "Found uv — will use it for faster installs."
fi

# ── Resolve version ───────────────────────────────────────────────
VERSION_EXPLICIT=true
if [[ -z "$VERSION" ]]; then
  VERSION_EXPLICIT=false
  info "Detecting latest Vesma version on PyPI…"
  VERSION="$(curl -fsSL "https://pypi.org/pypi/vesma-memory-server/json" 2>/dev/null \
    | "$PYTHON" -c 'import json, sys; print(json.load(sys.stdin)["info"]["version"])' 2>/dev/null || true)"
  [[ -z "$VERSION" ]] && die "Could not detect latest version. Specify --version manually."
fi
info "Installing Vesma v${VERSION} (extras: ${EXTRAS})"

# ── Container path ────────────────────────────────────────────────
if [[ "$CONTAINER" == true ]]; then
  info "Container mode — pulling image ghcr.io/vesmaro/vesma:${VERSION}…"

  RUNTIME=""
  for r in podman docker; do
    if command -v "$r" &>/dev/null; then RUNTIME="$r"; break; fi
  done
  [[ -z "$RUNTIME" ]] && die "Neither podman nor docker found. Install one to use --container."

  # PyPI and GHCR are independent registries: an auto-detected PyPI version
  # may not be tagged on GHCR yet. Fall back to :latest only when the version
  # was not pinned explicitly by the caller.
  if ! "$RUNTIME" pull "ghcr.io/vesmaro/vesma:${VERSION}"; then
    [[ "$VERSION_EXPLICIT" == true ]] && die "Failed to pull image ghcr.io/vesmaro/vesma:${VERSION}."
    warn "Tag ${VERSION} is not on GHCR (yet) — falling back to :latest."
    VERSION="latest"
    "$RUNTIME" pull "ghcr.io/vesmaro/vesma:${VERSION}" || die "Failed to pull image."
  fi

  if "$RUNTIME" ps -a --format '{{.Names}}' 2>/dev/null | grep -q '^vesma$'; then
    warn "Container 'vesma' already exists. Remove it first: $RUNTIME rm -f vesma"
    die "Aborting to avoid clobbering existing container."
  fi

  "$RUNTIME" volume create vesma-data 2>/dev/null || true
  "$RUNTIME" volume create vesma-vault 2>/dev/null || true

  if [[ -z "${VESMA_API__TOTP_MASTER_KEY:-}" ]]; then
    warn "VESMA_API__TOTP_MASTER_KEY is not set."
    warn "The container binds 0.0.0.0 and requires auth — it will refuse to start without the key."
    warn "Generate one: python3 -c \"import secrets; print(secrets.token_urlsafe(32))\""
    die "Set VESMA_API__TOTP_MASTER_KEY and re-run, or see docs/en/admin/runbooks/container-deployment.md"
  fi

  "$RUNTIME" run -d \
    --name vesma \
    -p "${CONTAINER_PORT}:8787" \
    -v vesma-data:/data \
    -v vesma-vault:/vault \
    -e VESMA_API__TOTP_MASTER_KEY="${VESMA_API__TOTP_MASTER_KEY}" \
    "ghcr.io/vesmaro/vesma:${VERSION}" || die "Failed to start container."

  ok "Vesma container started on port ${CONTAINER_PORT}."
  echo ""
  ok "Done. Next steps:"
  printf "    curl -s http://localhost:%s/health | jq\n" "$CONTAINER_PORT"
  printf "    # Swagger UI: http://localhost:%s/docs\n" "$CONTAINER_PORT"
  printf "    # Logs: %s logs -f vesma\n" "$RUNTIME"
  printf "    # Stop: %s stop vesma\n" "$RUNTIME"
  printf "    Docs: docs/en/admin/runbooks/container-deployment.md\n"
  exit 0
fi

# ── Build package spec (PyPI is the primary distribution channel) ─
PKG_SPEC="mnemos-memory-server"
if [[ -n "$EXTRAS" ]]; then
  PKG_SPEC="${PKG_SPEC}[${EXTRAS}]"
fi
PKG_SPEC="${PKG_SPEC}==${VERSION}"

# ── Create venv (unless --no-venv) ────────────────────────────────
if [[ "$NO_VENV" == false ]]; then
  info "Creating virtual environment at ${VENV_PATH}…"
  if [[ "$USE_UV" == true ]]; then
    uv venv "$VENV_PATH"
  else
    "$PYTHON" -m venv "$VENV_PATH"
  fi
  # shellcheck disable=SC1091
  source "$VENV_PATH/bin/activate"
  ok "Virtual environment activated: $(which python)"
fi

# ── Install ───────────────────────────────────────────────────────
info "Installing ${PKG_SPEC} from PyPI…"

if [[ "$USE_UV" == true ]]; then
  uv pip install --reinstall "${PKG_SPEC}"
else
  pip install --upgrade pip
  pip install --force-reinstall "${PKG_SPEC}"
fi

# ── Resolve the vesma binary ─────────────────────────────────────
if [[ "$NO_VENV" == false ]]; then
  MNEMOS_BIN="${VENV_PATH}/bin/mnemos"
else
  MNEMOS_BIN="$(command -v vesma 2>/dev/null || true)"
fi

# ── Drop a launcher into ~/.local/bin (no venv activation needed) ──
LINKED=false
if [[ "$NO_VENV" == false && -x "$MNEMOS_BIN" ]]; then
  mkdir -p "$LOCAL_BIN"
  ln -sf "$MNEMOS_BIN" "${LOCAL_BIN}/mnemos"
  LINKED=true
  MNEMOS_BIN="${LOCAL_BIN}/mnemos"
fi

# ── Verify ────────────────────────────────────────────────────────
if [[ -x "$MNEMOS_BIN" ]] || command -v vesma &>/dev/null; then
  ok "Vesma v${VERSION} installed successfully!"
else
  warn "vesma CLI not found — check the install output above."
fi

# ── Optional: VS Code MCP integration ─────────────────────────────
setup_mcp() {
  info "Setting up VS Code MCP integration…"
  if curl -fsSL "https://raw.githubusercontent.com/vesmaro/vesmaro/main/scripts/mcp-setup.sh" \
       | bash -s -- --command "$MNEMOS_BIN"; then
    ok "VS Code MCP integration ready — reload your VS Code window."
  else
    warn "MCP setup didn't complete. Run it later:"
    printf "    curl -fsSL https://raw.githubusercontent.com/vesmaro/vesmaro/main/scripts/mcp-setup.sh | bash\n"
  fi
}

MCP_DONE=false
case "$MCP_SETUP" in
  yes) echo ""; setup_mcp; MCP_DONE=true ;;
  no)  : ;;
  ask)
    if [[ -r /dev/tty ]]; then
      prompt_ask "Set up VS Code MCP integration now? [Y/n]"
      read -r reply < /dev/tty || reply=""
      case "$reply" in
        [Nn]*) info "Skipped MCP setup. You can run it anytime later." ;;
        *)     setup_mcp; MCP_DONE=true ;;
      esac
    fi
    ;;
esac

# ── Optional: agent integration pack (instructions + skills + prompts) ──
setup_instructions() {
  info "Deploying agent integration pack (instructions, skills, prompts)…"
  # --no-wire-agents: agent wiring is handled separately by setup_wire_agents
  # below. Without this flag, the default flow prints a noisy "Non-interactive
  # terminal — skipping agent wiring" message even though wiring is about to
  # run in its own dedicated step.
  if "$MNEMOS_BIN" integration setup --target all --no-mcp --no-wire-agents; then
    ok "Agent integration pack deployed — reload your VS Code window."
  else
    warn "Integration pack deployment didn't complete. Run it later:"
    printf "    vesma integration setup --target all\n"
  fi
}

INSTRUCTIONS_DONE=false
case "$INSTRUCTIONS_SETUP" in
  yes) echo ""; setup_instructions; INSTRUCTIONS_DONE=true ;;
  no)  : ;;
  ask)
    if [[ -r /dev/tty ]]; then
      prompt_ask "Deploy agent integration pack (instructions+skills+prompts)? [Y/n]"
      read -r reply < /dev/tty || reply=""
      case "$reply" in
        [Nn]*) info "Skipped integration pack. You can deploy it anytime: vesma integration setup" ;;
        *)     setup_instructions; INSTRUCTIONS_DONE=true ;;
      esac
    fi
    ;;
esac

# ── Optional: wire Vesma MCP into Copilot agent tools: frontmatter ────
setup_wire_agents() {
  info "Wiring Vesma MCP into Copilot agent tools: frontmatter…"
  if "$MNEMOS_BIN" integration setup --wire-agents --all --no-mcp; then
    ok "Agent MCP wiring complete — reload your VS Code window."
  else
    warn "Agent wiring didn't complete. Run it later:"
    printf "    vesma integration setup --wire-agents --all\n"
  fi
}

case "$WIRE_AGENTS" in
  yes) echo ""; setup_wire_agents ;;
  no)  : ;;
  ask)
    if [[ -r /dev/tty ]]; then
      prompt_ask "Wire Vesma MCP to all Copilot agents? [Y/n]"
      read -r reply < /dev/tty || reply=""
      case "$reply" in
        [Nn]*) info "Skipped agent wiring. Run it anytime: vesma integration setup --wire-agents --all" ;;
        *)     setup_wire_agents ;;
      esac
    fi
    ;;
esac

# ── Done ──────────────────────────────────────────────────────────
echo ""
ok "Vesma installed. Next steps:"
echo "  • Run 'vesma completion' to enable shell autocompletion"
echo "  • Run 'vesma integration setup' to deploy behavioral instructions to your agent harness"
echo "  • Run 'vesma doctor' to verify your installation"
echo ""
ok "Try it:"
printf "    vesma add 'Hello' --tags project:test,agent:setup,mnemos:learning\n"
printf "    vesma search 'Hello'\n"
if [[ "$MCP_DONE" == false ]]; then
  echo ""
  info "Enable VS Code MCP integration later:"
  printf "    curl -fsSL https://raw.githubusercontent.com/vesmaro/vesmaro/main/scripts/mcp-setup.sh | bash\n"
fi

if [[ "$INSTRUCTIONS_DONE" == false ]]; then
  echo ""
  info "Deploy the agent integration pack later:"
  printf "    vesma integration setup --target all\n"
fi

# ── PATH hint (only if ~/.local/bin isn't already on PATH) ─────────
if [[ "$LINKED" == true ]]; then
  case ":${PATH}:" in
    *":${LOCAL_BIN}:"*) : ;;  # already reachable — nothing to do
    *)
      echo ""
      warn "Add ~/.local/bin to your PATH so 'vesma' works in every shell:"
      printf "    echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc && source ~/.bashrc\n"
      ;;
  esac
fi