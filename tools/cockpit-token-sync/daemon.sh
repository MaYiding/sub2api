#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYNC_SCRIPT="$SCRIPT_DIR/sync.js"

while true; do
  /opt/homebrew/bin/node "$SYNC_SCRIPT" || true
  sleep 30
done
