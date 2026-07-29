#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

LOG_DIR="${SHEJANE_DEV_LOG_DIR:-${ROOT_DIR}/.tmp/dev}"
TOKEN="${SHEJANE_RUNTIME_TOKEN:-dev-runtime-token}"
RUNTIME_PORT="${SHEJANE_RUNTIME_PORT:-17371}"
RUNTIME_URL="${SHEJANE_RUNTIME_URL:-http://127.0.0.1:${RUNTIME_PORT}}"
CLIENT_DEV_PORT="${CLIENT_DEV_PORT:-55173}"
CLIENT_DEV_URL="${ELECTRON_DEV_URL:-http://127.0.0.1:${CLIENT_DEV_PORT}}"
NODE_BIN="$(command -v node || true)"
SRT_DEV_CLI="${ROOT_DIR}/client/node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js"

PIDS=()
RUNTIME_ENV=(
  env -i
  "PATH=$PATH"
  "HOME=$HOME"
  "USER=${USER:-}"
  "TMPDIR=${TMPDIR:-/tmp}"
  "SHELL=${SHELL:-/bin/zsh}"
  "PYTHONUNBUFFERED=1"
)
RUNTIME_FIXED_CAPABILITY_ARGS=()
[[ -n "${SHEJANE_RUNTIME_MCP_SERVERS:-}" ]] && RUNTIME_ENV+=("SHEJANE_RUNTIME_MCP_SERVERS=$SHEJANE_RUNTIME_MCP_SERVERS")
[[ -n "${SHEJANE_RUNTIME_SKILLS_PATH:-}" ]] && RUNTIME_ENV+=("SHEJANE_RUNTIME_SKILLS_PATH=$SHEJANE_RUNTIME_SKILLS_PATH")
RUNTIME_ENV+=("SHEJANE_RUNTIME_NODE_PATH=$(command -v node)")
if [[ -n "$NODE_BIN" ]]; then
  RUNTIME_ENV+=("SHEJANE_MANAGED_WORKER_SANDBOX_COMMAND=[\"${NODE_BIN}\",\"${SRT_DEV_CLI}\"]")
fi

kill_tree() {
  local pid="$1"
  local child
  while read -r child; do
    [[ -n "$child" ]] && kill_tree "$child"
  done < <(pgrep -P "$pid" 2>/dev/null || true)
  kill "$pid" >/dev/null 2>&1 || true
}

cleanup() {
  trap - EXIT INT TERM
  for pid in "${PIDS[@]:-}"; do
    kill_tree "$pid"
  done
}
trap cleanup EXIT INT TERM

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 127
  fi
}

prepare_fixed_capability_args() {
  RUNTIME_FIXED_CAPABILITY_ARGS=()
  local flag path
  while IFS='|' read -r flag path; do
    [[ -f "$path" ]] && RUNTIME_FIXED_CAPABILITY_ARGS+=("$flag" "$path")
  done <<EOF
--computer-use-package|${ROOT_DIR}/runtime/plugins/computer-use/dist/computer-use-0.2.0-darwin-arm64.shejane-plugin
--browser-qa-package|${ROOT_DIR}/runtime/plugins/browser-qa/dist/browser-qa-0.1.0-darwin-arm64.shejane-plugin
--browser-qa-runtime-asset|${ROOT_DIR}/runtime/plugins/browser-qa/dist/browser-qa-runtime-1.61.1-darwin-arm64.shejane-runtime-asset
--ocr-package|${ROOT_DIR}/runtime/plugins/ocr/dist/ocr-0.1.1-darwin-arm64.shejane-plugin
--ocr-runtime-asset|${ROOT_DIR}/runtime/plugins/ocr/dist/rapidocr-runtime-3.9.1-darwin-arm64.shejane-runtime-asset
EOF
}

ensure_node_modules() {
  if [[ ! -d "${ROOT_DIR}/node_modules/.pnpm" ]]; then
    echo "Installing pnpm workspace dependencies"
    (cd "$ROOT_DIR" && pnpm install)
  fi
}

force_kill_stragglers() {
  # Runtime and Electron may survive SIGTERM; default to a clean restart.
  if [[ "${SHEJANE_DEV_REUSE:-0}" == "1" ]]; then
    echo "[dev] SHEJANE_DEV_REUSE=1 — keeping existing processes"
    return
  fi

  echo "[dev] hard-restart: killing stragglers"
  pkill -9 -f 'shejane-runtime' >/dev/null 2>&1 || true
  pkill -9 -f 'electron/main\.cjs' >/dev/null 2>&1 || true
  pkill -9 -f "vite.*--port ${CLIENT_DEV_PORT}" \
    >/dev/null 2>&1 || true
  for port in "$RUNTIME_PORT" "$CLIENT_DEV_PORT"; do
    local pids
    pids="$(lsof -ti "tcp:${port}" 2>/dev/null || true)"
    if [[ -n "$pids" ]]; then
      echo "[dev] freeing port ${port} (pids: ${pids})"
      # shellcheck disable=SC2086
      kill -9 $pids >/dev/null 2>&1 || true
    fi
  done
  sleep 1
}

open_log_tail_terminal() {
  # macOS-only live Runtime log. Disable with SHEJANE_DEV_LOG_TAIL=0.
  [[ "${SHEJANE_DEV_LOG_TAIL:-1}" == "1" ]] || return 0
  [[ "$(uname -s)" == "Darwin" ]] || return 0
  local log="${LOG_DIR}/runtime.log"
  : > "$log" 2>/dev/null || true
  osascript >/dev/null 2>&1 <<APPLESCRIPT || true
tell application "Terminal"
  activate
  do script "tail -F '${log}' | grep --line-buffered -iE 'POST /v1|HTTP/1\\.1 [45]|run\\.(waiting|completed|failed|started)|permission\\.|question\\.|llm\\.error|KeyError|Traceback' || tail -F '${log}'"
end tell
APPLESCRIPT
}

wait_for_url() {
  local url="$1"
  local label="$2"
  local log_file="${3:-}"
  for _ in $(seq 1 120); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "Timed out waiting for ${label}: ${url}" >&2
  if [[ -n "$log_file" && -f "$log_file" ]]; then
    echo "Last ${label} log lines:" >&2
    tail -80 "$log_file" >&2 || true
  fi
  exit 1
}

start_runtime() {
  if curl -fsS "${RUNTIME_URL}/v1/health" >/dev/null 2>&1; then
    echo "Runtime already running at ${RUNTIME_URL}"
    return
  fi

  local log_file="${LOG_DIR}/runtime.log"
  echo "Starting Runtime at ${RUNTIME_URL}"
  "${ROOT_DIR}/scripts/build-computer-use-builtin.sh"
  prepare_fixed_capability_args
  (
    cd "${ROOT_DIR}/runtime"
    "${RUNTIME_ENV[@]}" uv run shejane-runtime \
      --host 127.0.0.1 --port "$RUNTIME_PORT" --token "$TOKEN" \
      "${RUNTIME_FIXED_CAPABILITY_ARGS[@]}" \
      >"$log_file" 2>&1
  ) &
  PIDS+=("$!")
  wait_for_url "${RUNTIME_URL}/v1/health" "Runtime" "$log_file"
}

start_client_dev_server() {
  if curl -fsS "$CLIENT_DEV_URL" >/dev/null 2>&1; then
    echo "Client dev server already running at ${CLIENT_DEV_URL}"
    return
  fi

  local log_file="${LOG_DIR}/client-vite.log"
  echo "Starting client dev server at ${CLIENT_DEV_URL}"
  (
    cd "${ROOT_DIR}/client"
    env -i \
      "PATH=$PATH" \
      "HOME=$HOME" \
      "USER=${USER:-}" \
      "TMPDIR=${TMPDIR:-/tmp}" \
      "SHELL=${SHELL:-/bin/zsh}" \
      pnpm dev --host 127.0.0.1 --port "$CLIENT_DEV_PORT" >"$log_file" 2>&1
  ) &
  PIDS+=("$!")
  wait_for_url "$CLIENT_DEV_URL" "client dev server" "$log_file"
}

launch_electron() {
  echo "Launching Electron. Close the app window to stop local dev helper processes."
  (
    cd "${ROOT_DIR}/client"
    env -i \
      "PATH=$PATH" \
      "HOME=$HOME" \
      "USER=${USER:-}" \
      "TMPDIR=${TMPDIR:-/tmp}" \
      "SHELL=${SHELL:-/bin/zsh}" \
      "ELECTRON_DEV=true" \
      "ELECTRON_DEV_URL=$CLIENT_DEV_URL" \
      "SHEJANE_RUNTIME_URL=$RUNTIME_URL" \
      "SHEJANE_RUNTIME_TOKEN=$TOKEN" \
      pnpm electron
  ) &
  local electron_pid="$!"
  PIDS+=("$electron_pid")
  wait "$electron_pid"
}

start_stack() {
  require_command curl
  require_command node
  require_command pnpm
  require_command uv
  mkdir -p "$LOG_DIR"
  ensure_node_modules

  force_kill_stragglers

  start_runtime
  start_client_dev_server
  open_log_tail_terminal
  launch_electron
}

start_runtime_only() {
  require_command curl
  require_command node
  require_command uv
  mkdir -p "$LOG_DIR"
  restart_runtime
}

start_client_only() {
  require_command curl
  require_command pnpm
  mkdir -p "$LOG_DIR"
  ensure_node_modules
  pkill -9 -f 'electron/main\.cjs' >/dev/null 2>&1 || true
  pkill -9 -f "vite.*--port ${CLIENT_DEV_PORT}" >/dev/null 2>&1 || true
  start_client_dev_server
  launch_electron
}

restart_runtime() {
  local old_pid new_pid log_file="${LOG_DIR}/runtime.log"
  mkdir -p "$LOG_DIR"
  old_pid="$(lsof -ti :"$RUNTIME_PORT" 2>/dev/null | head -1 || true)"
  if [[ -n "$old_pid" ]]; then
    echo "→ Old Runtime PID $old_pid started: $(ps -p "$old_pid" -o lstart= 2>/dev/null || echo unknown)"
  else
    echo "→ No Runtime currently bound to :$RUNTIME_PORT"
  fi

  lsof -ti :"$RUNTIME_PORT" 2>/dev/null | xargs -r kill -9 2>/dev/null || true
  for _ in $(seq 1 10); do
    lsof -ti :"$RUNTIME_PORT" >/dev/null 2>&1 || break
    sleep 0.5
  done
  if lsof -ti :"$RUNTIME_PORT" >/dev/null 2>&1; then
    echo "Port :$RUNTIME_PORT is still occupied after kill -9." >&2
    exit 1
  fi

  echo "→ Starting Runtime at ${RUNTIME_URL}"
  prepare_fixed_capability_args
  (
    cd "${ROOT_DIR}/runtime"
    nohup "${RUNTIME_ENV[@]}" uv run shejane-runtime \
      --host 127.0.0.1 --port "$RUNTIME_PORT" --token "$TOKEN" \
      "${RUNTIME_FIXED_CAPABILITY_ARGS[@]}" \
      >"$log_file" 2>&1 &
    echo "$!" >"${LOG_DIR}/runtime.pid"
  )
  wait_for_url "${RUNTIME_URL}/v1/health" "Runtime" "$log_file"
  new_pid="$(lsof -ti :"$RUNTIME_PORT" 2>/dev/null | head -1 || true)"
  echo "✅ Runtime ready. PID ${new_pid:-?}. Reload the Client window to reconnect."
}

row() {
  printf '  %s %-32s %s\n' "$1" "$2" "${3:-}"
}

section() {
  printf '\n\033[1m%s\033[0m\n' "$1"
}

doctor() {
  local runtime_pid started health_json runtime_env path port pids
  local leaks=()

  section "Runtime (:${RUNTIME_PORT})"
  runtime_pid="$(lsof -ti :"$RUNTIME_PORT" 2>/dev/null | head -1 || true)"
  if [[ -z "$runtime_pid" ]]; then
    row "❌" "not running" "→ make dev-runtime"
  else
    started="$(ps -p "$runtime_pid" -o lstart= 2>/dev/null | xargs || echo unknown)"
    row "✅" "PID $runtime_pid" "started: $started"
    health_json="$(curl -fsS --max-time 2 "${RUNTIME_URL}/v1/health" 2>/dev/null || true)"
    if [[ "$health_json" == *'"status":"ok"'* ]]; then
      row "✅" "health" "status=ok"
    else
      row "❌" "health" "${RUNTIME_URL}/v1/health did not respond correctly"
    fi
    if [[ "$health_json" == *'"pairing_configured":true'* ]]; then
      row "✅" "pairing token" "configured"
    else
      row "⚠️" "pairing token" "not configured"
    fi

    runtime_env="$(ps eww -p "$runtime_pid" 2>/dev/null | tr ' ' '\n' || true)"
    for path in OPENAI_API_KEY TAVILY_API_KEY ANTHROPIC_API_KEY; do
      [[ "$runtime_env" == *"${path}="* ]] && leaks+=("$path")
    done
    if [[ ${#leaks[@]} -eq 0 ]]; then
      row "✅" "provider keys" "not present in process env"
    else
      row "❌" "provider keys leaked" "${leaks[*]}"
    fi
  fi

  section "Dev ports"
  for port in "$RUNTIME_PORT" "$CLIENT_DEV_PORT"; do
    pids="$(lsof -ti tcp:"$port" 2>/dev/null || true)"
    [[ -n "$pids" ]] && row "✅" "port $port" "pids $pids"
  done

  section "Dependencies"
  [[ -d node_modules/.pnpm ]] && row "✅" "pnpm workspace" "ready" \
    || row "❌" "pnpm workspace" "→ pnpm install"
  [[ -d runtime/.venv ]] && row "✅" "Runtime venv" "ready" \
    || row "❌" "Runtime venv" "→ cd runtime && uv sync"
}

logs() {
  local target="${1:-all}" lines="${LOG_LINES:-200}" file
  case "$target" in
    runtime|client)
      [[ "$target" == "runtime" ]] && file="${LOG_DIR}/runtime.log" || file="${LOG_DIR}/client-vite.log"
      [[ -f "$file" ]] || { echo "Log does not exist yet: $file" >&2; exit 1; }
      tail -n "$lines" -f "$file"
      ;;
    all)
      echo "== Runtime =="
      tail -n "${LOG_LINES:-80}" "${LOG_DIR}/runtime.log" 2>/dev/null || true
      echo "== Client =="
      tail -n "${LOG_LINES:-80}" "${LOG_DIR}/client-vite.log" 2>/dev/null || true
      ;;
    *) echo "Usage: $0 logs [runtime|client|all]" >&2; exit 2 ;;
  esac
}

case "${1:-start}" in
  start) start_stack ;;
  start-client) start_client_only ;;
  start-runtime) start_runtime_only ;;
  restart) restart_runtime ;;
  doctor) doctor ;;
  logs) shift; logs "${1:-all}" ;;
  *) echo "Usage: $0 [start|start-client|start-runtime|restart|doctor|logs]" >&2; exit 2 ;;
esac
