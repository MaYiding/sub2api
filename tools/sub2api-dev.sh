#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"
DEPLOY_DIR="$ROOT_DIR/deploy"
ENV_FILE="$DEPLOY_DIR/.env"
APP_DATA_DIR="${SUB2API_DATA_DIR:-$DEPLOY_DIR/data}"
RUN_DIR="$APP_DATA_DIR/run"
BACKUP_ROOT="${SUB2API_BACKUP_DIR:-$HOME/.local/share/sub2api/backups}"

SCRIPT_PATH="$ROOT_DIR/tools/sub2api-dev.sh"
BACKEND_BIN="$RUN_DIR/sub2api-server"
BACKEND_LOG="$RUN_DIR/backend.log"
FRONTEND_LOG="$RUN_DIR/frontend.log"
BACKEND_SESSION="sub2api-local-backend"
FRONTEND_SESSION="sub2api-local-frontend"

PNPM_VERSION="${SUB2API_PNPM_VERSION:-9.15.9}"
BACKEND_URL="${SUB2API_BACKEND_URL:-http://127.0.0.1:8080}"
FRONTEND_URL="${SUB2API_FRONTEND_URL:-http://127.0.0.1:3000}"

log() {
  printf '[sub2api-dev] %s\n' "$*"
}

die() {
  printf '[sub2api-dev] ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "缺少命令: $1"
}

load_env() {
  [[ -f "$ENV_FILE" ]] || die "缺少 $ENV_FILE"

  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a

  : "${POSTGRES_USER:?deploy/.env 缺少 POSTGRES_USER}"
  : "${POSTGRES_PASSWORD:?deploy/.env 缺少 POSTGRES_PASSWORD}"
  : "${POSTGRES_DB:?deploy/.env 缺少 POSTGRES_DB}"
  : "${ADMIN_EMAIL:?deploy/.env 缺少 ADMIN_EMAIL}"
  : "${ADMIN_PASSWORD:?deploy/.env 缺少 ADMIN_PASSWORD}"

  export DATA_DIR="$APP_DATA_DIR"
  export DATABASE_HOST="127.0.0.1"
  export DATABASE_PORT="5432"
  export DATABASE_USER="$POSTGRES_USER"
  export DATABASE_PASSWORD="$POSTGRES_PASSWORD"
  export DATABASE_DBNAME="$POSTGRES_DB"
  export DATABASE_SSLMODE="disable"
  export REDIS_HOST="127.0.0.1"
  export REDIS_PORT="6379"
  export REDIS_PASSWORD="${REDIS_PASSWORD:-}"
  export REDIS_DB="${REDIS_DB:-15}"
  export SERVER_HOST="127.0.0.1"
  export SERVER_PORT="${SERVER_PORT:-8080}"
  export SERVER_MODE="debug"
  export AUTO_SETUP="true"
  export GOPROXY="${GOPROXY:-https://goproxy.cn,direct}"
  export GOSUMDB="${GOSUMDB:-sum.golang.google.cn}"
}

prepare_run_dir() {
  mkdir -p "$RUN_DIR"
  chmod 700 "$RUN_DIR"
}

wait_for_command() {
  local description="$1"
  shift

  local _attempt
  for _attempt in $(seq 1 30); do
    if "$@" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done

  die "$description 在 30 秒内未就绪"
}

wait_for_http() {
  local url="$1"
  local description="$2"

  local _attempt
  for _attempt in $(seq 1 90); do
    if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done

  die "$description 在 90 秒内未就绪"
}

start_infrastructure() {
  require_command brew
  require_command pg_isready
  require_command redis-cli

  if ! pg_isready -h "$DATABASE_HOST" -p "$DATABASE_PORT" >/dev/null 2>&1; then
    log "启动 PostgreSQL Homebrew 服务"
    brew services start postgresql@18 >/dev/null
  fi
  wait_for_command "PostgreSQL" pg_isready -h "$DATABASE_HOST" -p "$DATABASE_PORT"

  if ! redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" ping >/dev/null 2>&1; then
    log "启动 Redis Homebrew 服务"
    brew services start redis >/dev/null
  fi
  wait_for_command "Redis" redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" ping
}

validate_identifier() {
  local label="$1"
  local value="$2"
  [[ "$value" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "$label 不是安全的 PostgreSQL 标识符: $value"
}

ensure_database() {
  require_command psql
  require_command createdb

  validate_identifier "POSTGRES_USER" "$POSTGRES_USER"
  validate_identifier "POSTGRES_DB" "$POSTGRES_DB"

  if ! psql -h "$DATABASE_HOST" -p "$DATABASE_PORT" -d postgres -Atqc \
    "SELECT 1 FROM pg_roles WHERE rolname = '$POSTGRES_USER'" | grep -qx '1'; then
    log "创建 PostgreSQL 角色 $POSTGRES_USER"
    createuser -h "$DATABASE_HOST" -p "$DATABASE_PORT" \
      --login --superuser --createdb --createrole "$POSTGRES_USER"
  fi

  psql -h "$DATABASE_HOST" -p "$DATABASE_PORT" -d postgres \
    -v ON_ERROR_STOP=1 -v role_name="$POSTGRES_USER" -v role_password="$POSTGRES_PASSWORD" \
    >/dev/null <<'SQL'
SELECT format(
  'ALTER ROLE %I LOGIN SUPERUSER CREATEDB CREATEROLE PASSWORD %L',
  :'role_name',
  :'role_password'
) \gexec
SQL

  if ! psql -h "$DATABASE_HOST" -p "$DATABASE_PORT" -d postgres -Atqc \
    "SELECT 1 FROM pg_database WHERE datname = '$POSTGRES_DB'" | grep -qx '1'; then
    log "创建 PostgreSQL 数据库 $POSTGRES_DB"
    createdb -h "$DATABASE_HOST" -p "$DATABASE_PORT" -O "$POSTGRES_USER" "$POSTGRES_DB"
  fi
}

ensure_port_available() {
  local port="$1"
  local name="$2"

  local listener
  listener="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  [[ -z "$listener" ]] || die "$name 端口 $port 已被 PID $listener 占用；请先停止旧进程"
}

screen_session_identifier() {
  local session="$1"
  { screen -ls 2>/dev/null || true; } |
    awk -v target="$session" '$1 ~ "^[0-9]+\\." target "$" { print $1; exit }'
}

screen_session_running() {
  local session="$1"
  [[ -n "$(screen_session_identifier "$session")" ]]
}

screen_session_pid() {
  local identifier
  identifier="$(screen_session_identifier "$1")"
  printf '%s\n' "${identifier%%.*}"
}

start_screen_job() {
  local session="$1"
  local command="$2"

  require_command screen
  screen -dmS "$session" "$SCRIPT_PATH" "$command"
}

start_backend() {
  if screen_session_running "$BACKEND_SESSION"; then
    log "后端已由 screen 管理 (PID $(screen_session_pid "$BACKEND_SESSION"))"
    return 0
  fi

  require_command go
  require_command curl
  require_command lsof
  ensure_port_available "$SERVER_PORT" "后端"

  log "编译 Go 后端"
  (
    cd "$BACKEND_DIR"
    go build -o "$BACKEND_BIN" ./cmd/server
  )

  log "通过 screen 启动 Go 后端"
  start_screen_job "$BACKEND_SESSION" "run-backend"

  if ! wait_for_http "$BACKEND_URL/health" "Go 后端"; then
    tail -n 100 "$BACKEND_LOG" >&2 || true
    return 1
  fi
}

ensure_frontend_dependencies() {
  if [[ -x "$FRONTEND_DIR/node_modules/.bin/vite" ]]; then
    return 0
  fi

  require_command corepack
  log "安装前端依赖 (pnpm $PNPM_VERSION)"
  (
    cd "$FRONTEND_DIR"
    corepack "pnpm@$PNPM_VERSION" install --frozen-lockfile
  )
}

start_frontend() {
  if screen_session_running "$FRONTEND_SESSION"; then
    log "前端已由 screen 管理 (PID $(screen_session_pid "$FRONTEND_SESSION"))"
    return 0
  fi

  require_command curl
  require_command lsof
  ensure_port_available "3000" "前端"
  ensure_frontend_dependencies

  log "通过 screen 启动 Vite 前端"
  start_screen_job "$FRONTEND_SESSION" "run-frontend"

  if ! wait_for_http "$FRONTEND_URL" "Vite 前端"; then
    tail -n 100 "$FRONTEND_LOG" >&2 || true
    return 1
  fi
}

stop_screen_job() {
  local name="$1"
  local session="$2"
  local port="$3"
  local expected_command="$4"
  local identifier
  local listener
  identifier="$(screen_session_identifier "$session")"
  listener="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"

  if [[ -z "$identifier" ]]; then
    if [[ -z "$listener" ]] || [[ "$(ps -p "$listener" -o command= 2>/dev/null || true)" != *"$expected_command"* ]]; then
      log "$name 未运行"
      return 0
    fi
    log "清理 $name 遗留进程 (PID $listener)"
  else
    log "停止 $name (PID ${identifier%%.*})"
    screen -S "$identifier" -X quit
  fi

  if [[ -n "$listener" ]]; then
    kill -TERM "$listener" 2>/dev/null || true
  fi

  local _attempt
  for _attempt in $(seq 1 20); do
    if ! screen_session_running "$session" && ! lsof -tiTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done

  if [[ -n "$listener" ]]; then
    log "$name 未在 20 秒内退出，发送 KILL"
    kill -KILL "$listener" 2>/dev/null || true
  fi
  die "$name 在 20 秒内未停止"
}

start_all() {
  load_env
  prepare_run_dir
  start_infrastructure
  ensure_database
  start_backend
  start_frontend
  log "启动完成: 前端 ${FRONTEND_URL}，后端 ${BACKEND_URL}"
}

stop_apps() {
  prepare_run_dir
  stop_screen_job "Vite 前端" "$FRONTEND_SESSION" "3000" "$FRONTEND_DIR/"
  stop_screen_job "Go 后端" "$BACKEND_SESSION" "8080" "$BACKEND_BIN"
}

stop_all() {
  stop_apps

  if [[ "${1:-}" == "--all" ]]; then
    log "停止 PostgreSQL 和 Redis Homebrew 服务"
    brew services stop postgresql@18 >/dev/null
    brew services stop redis >/dev/null
  fi
}

show_process_status() {
  local name="$1"
  local session="$2"
  local url="$3"

  if screen_session_running "$session"; then
    local health="unreachable"
    if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      health="healthy"
    fi
    printf '%-12s running  pid=%-8s %s\n' "$name" "$(screen_session_pid "$session")" "$health"
  else
    printf '%-12s stopped\n' "$name"
  fi
}

show_status() {
  load_env
  prepare_run_dir
  show_process_status "backend" "$BACKEND_SESSION" "$BACKEND_URL/health"
  show_process_status "frontend" "$FRONTEND_SESSION" "$FRONTEND_URL"

  if pg_isready -h "$DATABASE_HOST" -p "$DATABASE_PORT" >/dev/null 2>&1; then
    printf '%-12s running  %s:%s\n' "postgres" "$DATABASE_HOST" "$DATABASE_PORT"
  else
    printf '%-12s stopped\n' "postgres"
  fi

  if redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" -n "$REDIS_DB" ping >/dev/null 2>&1; then
    printf '%-12s running  %s:%s db=%s\n' "redis" "$REDIS_HOST" "$REDIS_PORT" "$REDIS_DB"
  else
    printf '%-12s stopped\n' "redis"
  fi
}

backup_state() {
  load_env
  start_infrastructure
  ensure_database
  require_command pg_dump

  local backup_dir
  backup_dir="$BACKUP_ROOT/$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$backup_dir"
  chmod 700 "$backup_dir"

  log "备份 PostgreSQL 到 $backup_dir"
  PGPASSWORD="$POSTGRES_PASSWORD" pg_dump \
    -h "$DATABASE_HOST" -p "$DATABASE_PORT" \
    -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -Fc -f "$backup_dir/sub2api.dump"

  cp -p "$ENV_FILE" "$backup_dir/deploy.env"
  if [[ -f "$APP_DATA_DIR/config.yaml" ]]; then
    cp -p "$APP_DATA_DIR/config.yaml" "$backup_dir/config.yaml"
  fi
  if [[ -f "$APP_DATA_DIR/.installed" ]]; then
    cp -p "$APP_DATA_DIR/.installed" "$backup_dir/installed.lock"
  fi
  chmod 600 "$backup_dir"/*

  printf '%s\n' "$backup_dir"
}

update_env_value() {
  local key="$1"
  local value="$2"
  local temp_file
  temp_file="$(mktemp)"

  awk -v key="$key" -v value="$value" '
    BEGIN { updated = 0 }
    index($0, key "=") == 1 {
      print key "=" value
      updated = 1
      next
    }
    { print }
    END {
      if (!updated) {
        print key "=" value
      }
    }
  ' "$ENV_FILE" > "$temp_file"

  chmod 600 "$temp_file"
  mv "$temp_file" "$ENV_FILE"
}

create_api_key() {
  local name="${1:-Local Development}"
  load_env
  require_command curl
  require_command jq

  wait_for_http "$BACKEND_URL/health" "Go 后端"

  local login_payload login token groups group_id payload response api_key key_id
  login_payload="$(jq -cn --arg email "$ADMIN_EMAIL" --arg password "$ADMIN_PASSWORD" \
    '{email: $email, password: $password}')"
  login="$(curl -fsS -X POST "$BACKEND_URL/api/v1/auth/login" \
    -H 'Content-Type: application/json' \
    --data "$login_payload")"
  token="$(printf '%s' "$login" | jq -r '.data.access_token')"
  [[ -n "$token" && "$token" != "null" ]] || die "管理员登录失败，无法创建 API 密钥"

  groups="$(curl -fsS "$BACKEND_URL/api/v1/groups/available" \
    -H "Authorization: Bearer $token")"
  group_id="$(printf '%s' "$groups" | jq -r '.data[0].id')"
  [[ "$group_id" =~ ^[0-9]+$ ]] || die "没有可用分组，无法创建 API 密钥"

  payload="$(jq -n --arg name "$name" --argjson group_id "$group_id" \
    '{name: $name, group_id: $group_id}')"
  response="$(curl -fsS -X POST "$BACKEND_URL/api/v1/keys" \
    -H "Authorization: Bearer $token" \
    -H 'Content-Type: application/json' \
    -H "Idempotency-Key: local-development-$(date +%s)-$RANDOM" \
    --data "$payload")"

  api_key="$(printf '%s' "$response" | jq -r '.data.key')"
  key_id="$(printf '%s' "$response" | jq -r '.data.id')"
  [[ -n "$api_key" && "$api_key" != "null" ]] || die "API 密钥创建失败"

  update_env_value "SUB2API_API_KEY" "$api_key"
  printf 'API_KEY_ID=%s\nSUB2API_API_KEY=%s\n' "$key_id" "$api_key"
}

run_backend_foreground() {
  load_env
  [[ -x "$BACKEND_BIN" ]] || die "后端二进制不存在，请先运行 start"
  exec >>"$BACKEND_LOG" 2>&1
  cd "$BACKEND_DIR"
  exec "$BACKEND_BIN"
}

run_frontend_foreground() {
  [[ -x "$FRONTEND_DIR/node_modules/.bin/vite" ]] || die "前端依赖不存在，请先运行 start"
  exec >>"$FRONTEND_LOG" 2>&1
  cd "$FRONTEND_DIR"
  exec ./node_modules/.bin/vite
}

reset_all() {
  [[ "${1:-}" == "--yes" ]] || {
    cat >&2 <<'EOF'
完整重置会删除本机 sub2api PostgreSQL 数据库、Redis DB 15 和 deploy/data。
运行前会自动备份。确认执行请使用:
  ./tools/sub2api-dev.sh reset --yes
EOF
    exit 2
  }

  load_env
  start_infrastructure
  local backup_dir
  backup_dir="$(backup_state | tail -n 1)"
  log "重置前备份: $backup_dir"

  stop_apps

  log "重建 PostgreSQL 数据库 $POSTGRES_DB"
  dropdb -h "$DATABASE_HOST" -p "$DATABASE_PORT" --if-exists --force "$POSTGRES_DB"
  createdb -h "$DATABASE_HOST" -p "$DATABASE_PORT" -O "$POSTGRES_USER" "$POSTGRES_DB"

  log "清空 Redis DB $REDIS_DB"
  redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" -n "$REDIS_DB" FLUSHDB >/dev/null

  log "清理本地应用数据"
  rm -rf "$APP_DATA_DIR"
  prepare_run_dir

  start_all
  create_api_key "Local Development"
  log "完整重置完成"
}

show_logs() {
  prepare_run_dir
  case "${1:-}" in
    backend)
      touch "$BACKEND_LOG"
      tail -n 100 -f "$BACKEND_LOG"
      ;;
    frontend)
      touch "$FRONTEND_LOG"
      tail -n 100 -f "$FRONTEND_LOG"
      ;;
    *)
      die "logs 需要指定 backend 或 frontend"
      ;;
  esac
}

show_help() {
  cat <<'EOF'
Sub2API 本机开发环境管理脚本

用法:
  ./tools/sub2api-dev.sh start
  ./tools/sub2api-dev.sh stop [--all]
  ./tools/sub2api-dev.sh restart
  ./tools/sub2api-dev.sh status
  ./tools/sub2api-dev.sh logs backend|frontend
  ./tools/sub2api-dev.sh backup
  ./tools/sub2api-dev.sh reset --yes
  ./tools/sub2api-dev.sh create-key [名称]

说明:
  start       启动 Homebrew PostgreSQL/Redis、Go 后端和 Vite 前端
  stop        停止前后端；加 --all 时也停止 PostgreSQL/Redis
  restart     重启前后端，保留数据库和 Redis
  status      显示四个组件的运行状态
  logs        持续查看后端或前端日志
  backup      备份数据库、deploy/.env 和运行配置
  reset       先备份，再完整重建本地开发数据；必须显式传 --yes
  create-key  为管理员创建 API 密钥并写回 deploy/.env
EOF
}

main() {
  local command="${1:-help}"
  shift || true

  case "$command" in
    start)
      start_all
      ;;
    stop)
      stop_all "${1:-}"
      ;;
    restart)
      stop_apps
      start_all
      ;;
    status)
      show_status
      ;;
    logs)
      show_logs "${1:-}"
      ;;
    backup)
      backup_state
      ;;
    reset)
      reset_all "${1:-}"
      ;;
    create-key)
      create_api_key "${1:-Local Development}"
      ;;
    run-backend)
      run_backend_foreground
      ;;
    run-frontend)
      run_frontend_foreground
      ;;
    help|-h|--help)
      show_help
      ;;
    *)
      show_help >&2
      die "未知命令: $command"
      ;;
  esac
}

main "$@"
