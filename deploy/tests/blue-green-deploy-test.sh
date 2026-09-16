#!/usr/bin/env bash
set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "$TEST_DIR/.." && pwd)"
SCRIPT="$DEPLOY_DIR/blue-green-deploy.sh"

bash -n "$SCRIPT"
grep -Eq 'docker run --detach' "$SCRIPT"
grep -Eq 'does not run `docker build`|no build' "$DEPLOY_DIR/BLUE_GREEN.md" "$SCRIPT"
if grep -Eq '^[[:space:]]*docker[[:space:]]+(compose[[:space:]]+)?down([[:space:]]|$)' "$SCRIPT"; then
  echo 'blue-green script must not stop the stack with compose down' >&2
  exit 1
fi
if grep -Eq '^[[:space:]]*docker[[:space:]]+build([[:space:]]|$)' "$SCRIPT"; then
  echo 'blue-green script must not build images' >&2
  exit 1
fi
grep -Eq 'caddy validate' "$SCRIPT"
grep -Eq 'caddy reload' "$SCRIPT"
grep -Eq 'docker stop --time' "$SCRIPT"
grep -Eq 'lb_policy[[:space:]]+first' "$DEPLOY_DIR/Caddyfile"
grep -Eq 'stream_close_delay' "$DEPLOY_DIR/Caddyfile"

fixture="$(mktemp)"
rendered="$(mktemp)"
trap 'rm -f "$fixture" "$rendered"' EXIT
cp "$DEPLOY_DIR/Caddyfile" "$fixture"
source "$SCRIPT"
CADDYFILE="$fixture"
CADDY_SITE_HOST=api.sub2api.com
TARGET_PORT=18081
ACTIVE_PORT=8080
render_caddy_candidate "$rendered"
grep -Fq 'reverse_proxy 127.0.0.1:18081 127.0.0.1:8080' "$rendered"
cp "$rendered" "$fixture"
TARGET_PORT=8080
ACTIVE_PORT=18081
render_caddy_candidate "$rendered"
grep -Fq 'reverse_proxy 127.0.0.1:8080 127.0.0.1:18081' "$rendered"

echo 'blue-green deployment script test passed'
