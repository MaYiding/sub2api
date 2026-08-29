#!/usr/bin/env bash
set -Eeuo pipefail

# Deploy a pre-built Sub2API image beside the active container, switch Caddy
# only after the candidate is healthy, and keep the previous container for
# rollback. This script deliberately has no build, compose down, or pull step.

SCRIPT_NAME="$(basename "$0")"
DEPLOY_DIR="${SUB2API_DEPLOY_DIR:-/opt/sub2api/deploy}"
CADDYFILE="${SUB2API_CADDYFILE:-/opt/sub2api-https/Caddyfile}"
CADDY_CONTAINER="${SUB2API_CADDY_CONTAINER:-sub2api-https}"
CADDY_CONFIG_PATH="${SUB2API_CADDY_CONFIG_PATH:-/etc/caddy/Caddyfile}"
CADDY_SITE_HOST="${SUB2API_CADDY_SITE_HOST:-}"
STATE_FILE="${SUB2API_BLUE_GREEN_STATE_FILE:-$DEPLOY_DIR/.blue-green-active}"
STAGING_ROOT="${SUB2API_BLUE_GREEN_STAGING_DIR:-$DEPLOY_DIR/.blue-green}"
LOCK_FILE="${SUB2API_BLUE_GREEN_LOCK_FILE:-/var/lock/sub2api-blue-green.lock}"
BLUE_PORT="${SUB2API_BLUE_PORT:-8080}"
GREEN_PORT="${SUB2API_GREEN_PORT:-18081}"
LEGACY_CONTAINER="${SUB2API_LEGACY_CONTAINER:-sub2api}"
STARTUP_TIMEOUT_SECONDS="${SUB2API_STARTUP_TIMEOUT_SECONDS:-180}"
MONITOR_SECONDS="${SUB2API_MONITOR_SECONDS:-30}"
DRAIN_SECONDS="${SUB2API_DRAIN_SECONDS:-60}"

TARGET_IMAGE=""
TARGET_SLOT=""
ACTIVE_SLOT=""
ACTIVE_CONTAINER=""
ACTIVE_PORT=""
TARGET_CONTAINER=""
TARGET_PORT=""
NETWORK_NAME=""
ACTIVE_DATA_DIR=""
TARGET_DATA_DIR=""
CADDY_BACKUP=""
CADDY_CHANGED=0
TARGET_STARTED=0
CUTOVER_COMPLETE=0

log() {
  printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

die() {
  printf '[%s] ERROR: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage:
  blue-green-deploy.sh IMAGE

Required environment for the first production rollout:
  SUB2API_CADDY_SITE_HOST   Caddy site host, e.g. sub2api.example.com:8443

Useful overrides:
  SUB2API_DEPLOY_DIR         default: /opt/sub2api/deploy
  SUB2API_CADDYFILE          default: /opt/sub2api-https/Caddyfile
  SUB2API_CADDY_CONTAINER    default: sub2api-https
  SUB2API_BLUE_PORT          default: 8080
  SUB2API_GREEN_PORT         default: 18081
  SUB2API_STARTUP_TIMEOUT_SECONDS  default: 180
  SUB2API_MONITOR_SECONDS    default: 30
  SUB2API_DRAIN_SECONDS      default: 60

The image must already exist locally and be linux/amd64. The script never
builds, pulls, or stops the active container before the candidate is healthy.
EOF
}

state_value() {
  local key="$1"
  awk -F= -v wanted="$key" '$1 == wanted { sub(/^[^=]*=/, ""); print; exit }' "$STATE_FILE"
}

docker_inspect_value() {
  docker inspect "$1" --format "$2"
}

require_integer() {
  local name="$1"
  local value="$2"
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || die "$name must be a positive integer"
}

require_tools() {
  local tool
  for tool in docker curl awk cp mktemp mv flock; do
    command -v "$tool" >/dev/null 2>&1 || die "required command is missing: $tool"
  done
}

load_active_slot() {
  if [[ -f "$STATE_FILE" ]]; then
    ACTIVE_SLOT="$(state_value active_slot)"
    ACTIVE_CONTAINER="$(state_value active_container)"
    ACTIVE_PORT="$(state_value active_port)"
    [[ "$ACTIVE_SLOT" == blue || "$ACTIVE_SLOT" == green ]] || die "invalid active_slot in $STATE_FILE"
    [[ -n "$ACTIVE_CONTAINER" && -n "$ACTIVE_PORT" ]] || die "incomplete state in $STATE_FILE"
    require_integer active_port "$ACTIVE_PORT"
  else
    ACTIVE_SLOT=blue
    ACTIVE_CONTAINER="$LEGACY_CONTAINER"
    ACTIVE_PORT="$BLUE_PORT"
    log "no state file; treating $ACTIVE_CONTAINER:$ACTIVE_PORT as the legacy blue slot"
  fi

  docker inspect "$ACTIVE_CONTAINER" >/dev/null 2>&1 || die "active container does not exist: $ACTIVE_CONTAINER"
  [[ "$(docker_inspect_value "$ACTIVE_CONTAINER" '{{.State.Status}}')" == running ]] || die "active container is not running: $ACTIVE_CONTAINER"
}

select_target_slot() {
  case "$ACTIVE_SLOT" in
    blue)
      TARGET_SLOT=green
      TARGET_CONTAINER=sub2api-green
      TARGET_PORT="$GREEN_PORT"
      ;;
    green)
      TARGET_SLOT=blue
      TARGET_CONTAINER=sub2api-blue
      TARGET_PORT="$BLUE_PORT"
      ;;
  esac

  [[ "$TARGET_CONTAINER" != "$ACTIVE_CONTAINER" ]] || die "target and active containers are identical"
  if docker inspect "$TARGET_CONTAINER" >/dev/null 2>&1; then
    die "target container already exists: $TARGET_CONTAINER; inspect it and remove it manually before retrying"
  fi
}

validate_inputs() {
  [[ -n "$TARGET_IMAGE" ]] || die "image is required"
  [[ -f "$CADDYFILE" ]] || die "Caddyfile does not exist: $CADDYFILE"
  [[ -n "$CADDY_SITE_HOST" ]] || die "SUB2API_CADDY_SITE_HOST is required"
  require_integer SUB2API_STARTUP_TIMEOUT_SECONDS "$STARTUP_TIMEOUT_SECONDS"
  require_integer SUB2API_MONITOR_SECONDS "$MONITOR_SECONDS"
  require_integer SUB2API_DRAIN_SECONDS "$DRAIN_SECONDS"
  require_integer SUB2API_BLUE_PORT "$BLUE_PORT"
  require_integer SUB2API_GREEN_PORT "$GREEN_PORT"
  [[ "$BLUE_PORT" != "$GREEN_PORT" ]] || die "blue and green ports must differ"

  docker image inspect "$TARGET_IMAGE" >/dev/null 2>&1 || die "image is not loaded locally: $TARGET_IMAGE"
  local platform
  platform="$(docker image inspect "$TARGET_IMAGE" --format '{{.Os}}/{{.Architecture}}')"
  [[ "$platform" == linux/amd64 ]] || die "image platform must be linux/amd64, got $platform"

  grep -Eq '^[[:space:]]*lb_policy[[:space:]]+first([[:space:]]|$)' "$CADDYFILE" \
    || die "Caddyfile must configure lb_policy first before blue-green deployment"
  grep -Eq '^[[:space:]]*health_uri[[:space:]]+/health([[:space:]]|$)' "$CADDYFILE" \
    || die "Caddyfile must configure health_uri /health before blue-green deployment"
  grep -Eq '^[[:space:]]*stream_close_delay[[:space:]]+' "$CADDYFILE" \
    || die "Caddyfile must configure stream_close_delay before blue-green deployment"
}

load_network_and_data() {
  NETWORK_NAME="$(docker_inspect_value "$ACTIVE_CONTAINER" '{{range $name, $network := .NetworkSettings.Networks}}{{$name}}{{end}}')"
  [[ -n "$NETWORK_NAME" ]] || die "could not determine Docker network for $ACTIVE_CONTAINER"

  ACTIVE_DATA_DIR="$(docker_inspect_value "$ACTIVE_CONTAINER" '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Source}}{{end}}{{end}}')"
  [[ -d "$ACTIVE_DATA_DIR" ]] || die "active app data directory does not exist: $ACTIVE_DATA_DIR"
}

stage_data() {
  mkdir -p "$STAGING_ROOT"
  TARGET_DATA_DIR="$STAGING_ROOT/data-${TARGET_SLOT}-$(date -u +%Y%m%d%H%M%S)"
  mkdir "$TARGET_DATA_DIR"
  cp -a "$ACTIVE_DATA_DIR/." "$TARGET_DATA_DIR/"
  chmod 700 "$TARGET_DATA_DIR"
  log "staged app data at $TARGET_DATA_DIR"
}

build_env_args() {
  local env_value
  ENV_ARGS=()
  while IFS= read -r env_value; do
    [[ -n "$env_value" ]] || continue
    case "$env_value" in
      SERVER_HOST=*|SERVER_PORT=*) continue ;;
    esac
    ENV_ARGS+=(--env "$env_value")
  done < <(docker_inspect_value "$ACTIVE_CONTAINER" '{{range .Config.Env}}{{println .}}{{end}}')
  ENV_ARGS+=(--env SERVER_HOST=0.0.0.0 --env SERVER_PORT=8080)
}

start_candidate() {
  build_env_args
  log "starting candidate $TARGET_CONTAINER on 127.0.0.1:$TARGET_PORT with $TARGET_IMAGE"
  docker run --detach \
    --name "$TARGET_CONTAINER" \
    --restart unless-stopped \
    --network "$NETWORK_NAME" \
    --ulimit nofile=100000:100000 \
    --security-opt no-new-privileges:true \
    --publish "127.0.0.1:${TARGET_PORT}:8080" \
    --volume "${TARGET_DATA_DIR}:/app/data:Z" \
    "${ENV_ARGS[@]}" \
    "$TARGET_IMAGE" >/dev/null
  TARGET_STARTED=1
}

candidate_is_healthy() {
  local state health
  state="$(docker_inspect_value "$TARGET_CONTAINER" '{{.State.Status}}')"
  health="$(docker_inspect_value "$TARGET_CONTAINER" '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}')"
  [[ "$state" == running && "$health" == healthy ]] || return 1
  curl -fsS --max-time 5 "http://127.0.0.1:${TARGET_PORT}/health" >/dev/null
}

wait_for_candidate() {
  local deadline=$((SECONDS + STARTUP_TIMEOUT_SECONDS))
  while (( SECONDS < deadline )); do
    if candidate_is_healthy; then
      log "candidate $TARGET_CONTAINER is healthy"
      return 0
    fi
    sleep 2
  done

  log "candidate did not become healthy within ${STARTUP_TIMEOUT_SECONDS}s"
  docker logs --tail 120 "$TARGET_CONTAINER" >&2 || true
  return 1
}

render_caddy_candidate() {
  local output="$1"
  awk -v site_host="$CADDY_SITE_HOST" \
      -v upstream="reverse_proxy 127.0.0.1:${TARGET_PORT} 127.0.0.1:${ACTIVE_PORT}" '
    BEGIN { in_site = 0; depth = 0; replaced = 0 }
    {
      if (!in_site && index($0, site_host) > 0 && $0 ~ /{/) {
        in_site = 1
        depth = 0
      }

      if (in_site && $0 ~ /^[[:space:]]*reverse_proxy[[:space:]]+(127[.]0[.]0[.]1|localhost):[0-9]+([[:space:]]+(127[.]0[.]0[.]1|localhost):[0-9]+)*[[:space:]]*[{]?[[:space:]]*$/ && !replaced) {
        match($0, /^[[:space:]]*/)
        print substr($0, 1, RLENGTH) upstream (index($0, "{") > 0 ? " {" : "")
        replaced = 1
      } else {
        print
      }

      if (in_site) {
        line = $0
        opens = gsub(/{/, "", line)
        closes = gsub(/}/, "", line)
        depth += opens - closes
        if (depth <= 0) {
          in_site = 0
          depth = 0
        }
      }
    }
    END { if (!replaced) exit 42 }
  ' "$CADDYFILE" > "$output" || {
    local status=$?
    [[ "$status" -eq 42 ]] && die "could not find the Sub2API reverse_proxy line under Caddy site $CADDY_SITE_HOST"
    return "$status"
  }
}

restore_caddy() {
  [[ "$CADDY_CHANGED" -eq 1 && -n "$CADDY_BACKUP" && -f "$CADDY_BACKUP" ]] || return 0
  cp "$CADDY_BACKUP" "$CADDYFILE"
  docker exec "$CADDY_CONTAINER" caddy reload --config "$CADDY_CONFIG_PATH" --adapter caddyfile >/dev/null
  CADDY_CHANGED=0
  log "restored Caddy configuration from $CADDY_BACKUP"
}

switch_caddy() {
  local candidate
  candidate="$(mktemp "${CADDYFILE}.tmp.XXXXXX")"
  render_caddy_candidate "$candidate"

  CADDY_BACKUP="${CADDYFILE}.backup.$(date -u +%Y%m%d%H%M%S)"
  cp -p "$CADDYFILE" "$CADDY_BACKUP"
  cp "$candidate" "$CADDYFILE"
  rm -f "$candidate"
  CADDY_CHANGED=1

  if ! docker exec "$CADDY_CONTAINER" caddy validate --config "$CADDY_CONFIG_PATH" --adapter caddyfile >/dev/null; then
    restore_caddy
    die "Caddy validation failed; old upstream remains active"
  fi
  if ! docker exec "$CADDY_CONTAINER" caddy reload --config "$CADDY_CONFIG_PATH" --adapter caddyfile >/dev/null; then
    restore_caddy
    die "Caddy reload failed; old upstream was restored"
  fi
  log "Caddy switched new traffic to $TARGET_CONTAINER:$TARGET_PORT; old upstream remains available"
}

monitor_candidate() {
  local deadline=$((SECONDS + MONITOR_SECONDS))
  while (( SECONDS < deadline )); do
    candidate_is_healthy || return 1
    sleep 2
  done
}

write_state() {
  local temporary="${STATE_FILE}.tmp.$$"
  mkdir -p "$(dirname "$STATE_FILE")"
  {
    printf '# Generated by %s; do not edit while a deployment is running.\n' "$SCRIPT_NAME"
    printf 'active_slot=%s\n' "$TARGET_SLOT"
    printf 'active_container=%s\n' "$TARGET_CONTAINER"
    printf 'active_port=%s\n' "$TARGET_PORT"
    printf 'active_image=%s\n' "$TARGET_IMAGE"
    printf 'updated_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } > "$temporary"
  chmod 600 "$temporary"
  mv "$temporary" "$STATE_FILE"
}

cleanup_on_failure() {
  local status=$?
  if [[ "$status" -ne 0 && "$CUTOVER_COMPLETE" -eq 0 ]]; then
    restore_caddy || true
    if [[ "$TARGET_STARTED" -eq 1 ]]; then
      docker rm -f "$TARGET_CONTAINER" >/dev/null 2>&1 || true
      log "removed failed candidate $TARGET_CONTAINER; staged data was retained for diagnosis"
    fi
  fi
  exit "$status"
}

main() {
  [[ "${1:-}" != "" && "${1:-}" != --help && "${1:-}" != -h ]] || { usage; [[ "${1:-}" == --help || "${1:-}" == -h ]] && return 0 || return 1; }
  [[ "$#" -eq 1 ]] || { usage >&2; return 1; }
  TARGET_IMAGE="$1"

  require_tools
  exec 9>"$LOCK_FILE"
  flock -n 9 || die "another blue-green deployment is already running"
  validate_inputs
  load_active_slot
  select_target_slot
  load_network_and_data
  stage_data
  start_candidate
  wait_for_candidate
  switch_caddy

  if ! monitor_candidate; then
    restore_caddy
    die "candidate failed during ${MONITOR_SECONDS}s monitor window; old upstream was restored"
  fi

  write_state
  CUTOVER_COMPLETE=1
  if ! docker stop --time "$DRAIN_SECONDS" "$ACTIVE_CONTAINER" >/dev/null; then
    log "WARNING: new traffic is on $TARGET_CONTAINER, but old container could not be stopped: $ACTIVE_CONTAINER"
  else
    log "drained and stopped old container $ACTIVE_CONTAINER"
  fi
  log "deployment complete: active=$TARGET_CONTAINER image=$TARGET_IMAGE; Caddy backup=$CADDY_BACKUP"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  trap cleanup_on_failure EXIT
  main "$@"
fi
