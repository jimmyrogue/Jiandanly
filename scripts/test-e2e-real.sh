#!/usr/bin/env bash
# Run normal Agent and Tool paths against a real BYOK model in an isolated Runtime.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL="${SHEJANE_EVAL_MODEL:?SHEJANE_EVAL_MODEL is required}"
PHASE="${SHEJANE_E2E_REAL_PHASE:-all}"
TOOL_PATTERN="${SHEJANE_E2E_REAL_TOOL_PATTERN:-keeps every published Tool|tool:}"
SOURCE_DATA_DIR="${SHEJANE_E2E_REAL_SOURCE_DATA_DIR:-${HOME}/.shejane/runtime}"
PORT="${SHEJANE_E2E_REAL_PORT:-17401}"
TOKEN="${SHEJANE_E2E_REAL_TOKEN:-real-e2e-token}"
URL="http://127.0.0.1:${PORT}"
CLIENT_PORT="${SHEJANE_E2E_REAL_CLIENT_PORT:-55274}"
CLIENT_URL="http://127.0.0.1:${CLIENT_PORT}"
TMP_DIR="$(mktemp -d)"
DATA_DIR="${TMP_DIR}/data"
BIN_DIR="${TMP_DIR}/bin"
LOG_FILE="${TMP_DIR}/runtime.log"
CLIENT_LOG_FILE="${TMP_DIR}/client-vite.log"
PLAYWRIGHT_ARTIFACT_DIR="${SHEJANE_E2E_ARTIFACT_DIR:-${ROOT_DIR}/.tmp/e2e-real-artifacts}"
mkdir -p "$DATA_DIR" "$BIN_DIR"

RUNTIME_PID=""
CLIENT_PID=""
cleanup() {
  local status=$?
  trap - EXIT
  if [[ -n "$CLIENT_PID" ]]; then
    kill "$CLIENT_PID" 2>/dev/null || true
    wait "$CLIENT_PID" 2>/dev/null || true
  fi
  if [[ -n "$RUNTIME_PID" ]]; then
    kill -9 "$RUNTIME_PID" 2>/dev/null || true
    wait "$RUNTIME_PID" 2>/dev/null || true
  fi
  if [[ $status -ne 0 ]]; then
    echo "Runtime log:" >&2
    tail -80 "$LOG_FILE" >&2 || true
    if [[ -f "$CLIENT_LOG_FILE" ]]; then
      echo "Client renderer log:" >&2
      tail -80 "$CLIENT_LOG_FILE" >&2 || true
    fi
  fi
  rm -rf "$TMP_DIR"
  exit "$status"
}
trap cleanup EXIT

if [[ ! "$PHASE" =~ ^(all|baseline|tools|agents|client)$ ]]; then
  echo "SHEJANE_E2E_REAL_PHASE must be one of: all, baseline, tools, agents, client" >&2
  exit 2
fi

for command in pbcopy pbpaste xclip open xdg-open; do
  ln -s "$(type -P true)" "$BIN_DIR/$command"
done

uv run --project "$ROOT_DIR/runtime" python -m shejane_runtime.eval.seed_service \
  --source-dir "$SOURCE_DATA_DIR" --destination-dir "$DATA_DIR" --model "$MODEL"

env -i \
  "PATH=$BIN_DIR:$PATH" "HOME=$HOME" "USER=${USER:-}" "TMPDIR=${TMPDIR:-/tmp}" \
  "DISPLAY=${DISPLAY:-:99}" "BROWSER=true" "PYTHONUNBUFFERED=1" \
  "$ROOT_DIR/runtime/.venv/bin/python" -m shejane_runtime \
    --host 127.0.0.1 --port "$PORT" --token "$TOKEN" --data-dir "$DATA_DIR" \
    >"$LOG_FILE" 2>&1 &
RUNTIME_PID="$!"

for _ in $(seq 1 60); do
  if curl -fsS "$URL/v1/health" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 1
done
if [[ "${READY:-0}" != "1" ]]; then
  echo "real-E2E Runtime failed to start" >&2
  exit 1
fi

if [[ "$PHASE" == "all" || "$PHASE" == "baseline" ]]; then
  echo "→ Real LLM baseline trajectories"
  (
    cd "$ROOT_DIR/runtime"
    SHEJANE_EVAL_RUNTIME_URL="$URL" SHEJANE_EVAL_TOKEN="$TOKEN" \
      SHEJANE_EVAL_MODEL="$MODEL" uv run python -m shejane_runtime.eval
  )
fi

if [[ "$PHASE" == "all" || "$PHASE" == "tools" ]]; then
  echo "→ Every normal published Runtime Tool through the real LLM"
  (
    cd "$ROOT_DIR/client"
    VITE_TEST_RUNTIME_URL="$URL" VITE_TEST_RUNTIME_TOKEN="$TOKEN" \
      VITE_TEST_REAL_LLM_MODEL="$MODEL" \
      pnpm exec vitest run --config vitest.contract.config.ts \
        src/runtime/runtime-tools.contract.test.ts \
        --testNamePattern "$TOOL_PATTERN"
  )
fi

if [[ "$PHASE" == "all" || "$PHASE" == "agents" ]]; then
  echo "→ Normal Agent capabilities through the real LLM"
  (
    cd "$ROOT_DIR/client"
    VITE_TEST_RUNTIME_URL="$URL" VITE_TEST_RUNTIME_TOKEN="$TOKEN" \
      VITE_TEST_REAL_LLM_MODEL="$MODEL" \
      pnpm exec vitest run --config vitest.contract.config.ts \
        src/runtime/runtime-agent.contract.test.ts \
        --testNamePattern 'tool:read_file|binds an enabled Skill|runs a Subagent|updates the injected Todo|tool:write_file|tool:user.ask'
  )
fi

if [[ "$PHASE" == "all" || "$PHASE" == "client" ]]; then
  echo "→ Normal Client flows through the real LLM"
  pnpm --filter @shejane/client exec vite \
    --host 127.0.0.1 --port "$CLIENT_PORT" --strictPort >"$CLIENT_LOG_FILE" 2>&1 &
  CLIENT_PID="$!"
  for _ in $(seq 1 30); do
    if curl -fsS "$CLIENT_URL" >/dev/null 2>&1; then
      CLIENT_READY=1
      break
    fi
    sleep 1
  done
  if [[ "${CLIENT_READY:-0}" != "1" ]]; then
    echo "real-E2E Client renderer failed to start" >&2
    exit 1
  fi

  SHEJANE_E2E_CLIENT_URL="$CLIENT_URL" \
  SHEJANE_E2E_RUNTIME_URL="$URL" \
  SHEJANE_E2E_RUNTIME_TOKEN="$TOKEN" \
  SHEJANE_E2E_RUNTIME_DATA_DIR="$DATA_DIR" \
  SHEJANE_E2E_RUNTIME_HOME="$HOME" \
  SHEJANE_E2E_RUNTIME_BIN_DIR="$BIN_DIR" \
  SHEJANE_E2E_RUNTIME_LOG="$LOG_FILE" \
  SHEJANE_E2E_TMP_DIR="$TMP_DIR" \
  SHEJANE_E2E_ARTIFACT_DIR="$PLAYWRIGHT_ARTIFACT_DIR" \
  SHEJANE_E2E_REAL_LLM_MODEL="$MODEL" \
  pnpm --filter @shejane/client exec playwright test \
    --config playwright.config.ts \
    --grep 'launches, connects, sends a task|binds a workspace and resolves a Tool approval|answers a structured user.ask|dismisses a structured user.ask'
fi

echo "✅ Real LLM normal-path phase passed: $PHASE"
