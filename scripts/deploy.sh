#!/bin/bash
# Vesma — container deployment helper
# Usage: ./scripts/deploy.sh [command]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
IMAGE_NAME="localhost/vesma:latest"

cd "$PROJECT_DIR"

usage() {
    cat <<EOF
Vesma container deployment

Usage: $0 <command>

Commands:
  build          Build container image
  up             Start with podman-compose
  down           Stop podman-compose
  logs           Show logs
  up-ollama      Start with Ollama sidecar
  kube-up        Deploy via podman kube play
  kube-down      Stop kube deployment
  quadlet        Install systemd quadlet unit
  shell          Open shell in running container
  cli <args>     Run vesma CLI in container
  status         Show container status

EOF
}

cmd_build() {
    echo "Building $IMAGE_NAME..."
    podman build -t "$IMAGE_NAME" -f Containerfile .
    echo "Done. Image: $IMAGE_NAME"
}

cmd_up() {
    podman-compose up -d
    echo "Vesma API:   http://localhost:8787"
    echo "Swagger UI:   http://localhost:8787/docs"
}

cmd_down() {
    podman-compose down
}

cmd_logs() {
    podman-compose logs -f vesma
}

cmd_up_ollama() {
    podman-compose --profile ollama up -d
    echo "Pulling nomic-embed-text model..."
    podman exec vesma-ollama ollama pull nomic-embed-text
    echo ""
    echo "Vesma API:   http://localhost:8787"
    echo "Ollama:       http://localhost:11434"
    echo ""
    echo "Update config.container.yaml:"
    echo "  embedding.provider: ollama"
    echo "  embedding.ollama_url: http://ollama:11434"
}

cmd_kube_up() {
    # Ensure volumes exist
    podman volume create vesma-data 2>/dev/null || true
    podman volume create vesma-vault 2>/dev/null || true
    podman kube play deploy/podman/kube/vesma-pod.yaml
    echo "Vesma API:   http://localhost:8787"
}

cmd_kube_down() {
    podman kube down deploy/podman/kube/vesma-pod.yaml
}

cmd_quadlet() {
    local target_dir="$HOME/.config/containers/systemd"
    mkdir -p "$target_dir"
    cp deploy/podman/quadlet/vesma.container "$target_dir/"
    systemctl --user daemon-reload
    echo "Quadlet installed. Start with:"
    echo "  systemctl --user start vesma"
    echo "  systemctl --user enable vesma  # autostart"
}

cmd_shell() {
    podman exec -it vesma /bin/bash
}

cmd_cli() {
    podman exec vesma vesma "$@"
}

cmd_status() {
    echo "=== Containers ==="
    podman ps --filter name=vesma --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    echo ""
    echo "=== Volumes ==="
    podman volume ls --filter name=vesma
}

case "${1:-}" in
    build)      cmd_build ;;
    up)         cmd_up ;;
    down)       cmd_down ;;
    logs)       cmd_logs ;;
    up-ollama)  cmd_up_ollama ;;
    kube-up)    cmd_kube_up ;;
    kube-down)  cmd_kube_down ;;
    quadlet)    cmd_quadlet ;;
    shell)      cmd_shell ;;
    cli)        shift; cmd_cli "$@" ;;
    status)     cmd_status ;;
    *)          usage ;;
esac
