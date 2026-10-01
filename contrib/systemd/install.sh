#!/usr/bin/env bash
# Install Vesma systemd user services.
# Usage: ./install.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="$HOME/.config/systemd/user"

mkdir -p "$TARGET"
cp "$SCRIPT_DIR/vesma-server.service" "$TARGET/"
cp "$SCRIPT_DIR/vesma-watcher.service" "$TARGET/"

systemctl --user daemon-reload
echo "Installed. Enable with:"
echo "  systemctl --user enable --now vesma-server"
echo "  systemctl --user enable --now vesma-watcher"
