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

# The WAL must stay outside copied app data and survive both deployment slots.
capture_dir="$(mktemp -d)"
trap 'rm -f "$fixture" "$rendered"; rmdir "$capture_dir"' EXIT
mock_capture=false
docker_inspect_value() {
  case "$2" in
    *Config.Env*) printf '%s\n' "AI_LOG_ENABLED=$mock_capture" AI_LOG_SOURCE_ID=existing-source AI_LOG_SPOOL_DIR=/app/ai-log-spool SERVER_PORT=9999 ;;
    *Mounts*) printf '%s\n' "$capture_dir" ;;
  esac
}
ACTIVE_DATA_DIR=/test/app-data
STAGING_ROOT=/test/staging
unset AI_LOG_ENABLED AI_LOG_SOURCE_ID AI_LOG_SPOOL_MAX_BYTES SUB2API_AI_LOG_SPOOL_HOST_DIR
build_env_args
if printf '%s\n' "${ENV_ARGS[@]}" | grep -Fq '/app/ai-log-spool:z'; then
  echo 'disabled capture must not add a mount' >&2; exit 1
fi
AI_LOG_ENABLED=true
AI_LOG_SOURCE_ID=new-source
SUB2API_AI_LOG_SPOOL_HOST_DIR="$capture_dir"
build_env_args
printf '%s\n' "${ENV_ARGS[@]}" | grep -Fxq "$capture_dir:/app/ai-log-spool:z"
printf '%s\n' "${ENV_ARGS[@]}" | grep -Fxq 'AI_LOG_SOURCE_ID=new-source'
if printf '%s\n' "${ENV_ARGS[@]}" | grep -Fxq 'AI_LOG_ENABLED=false'; then
  echo 'inherited capture setting must not shadow the override' >&2; exit 1
fi
unset AI_LOG_ENABLED AI_LOG_SOURCE_ID SUB2API_AI_LOG_SPOOL_HOST_DIR
mock_capture=true
build_env_args
printf '%s\n' "${ENV_ARGS[@]}" | grep -Fxq "$capture_dir:/app/ai-log-spool:z"
AI_LOG_ENABLED=false
build_env_args
if printf '%s\n' "${ENV_ARGS[@]}" | grep -Fq '/app/ai-log-spool:z'; then
  echo 'explicit rollback must disable the capture mount' >&2; exit 1
fi
echo 'blue-green shared AI log spool test passed'
