#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]] || exit 0

UPSTREAM_COMMIT="9f59ed0eeac09b115897732c46b794ee8ca4e5b0"
UPSTREAM_DIR="${SHEJANE_COMPUTER_USE_UPSTREAM:-${ROOT_DIR}/.tmp/computer-use-upstream}"
OUTPUT="${ROOT_DIR}/runtime/plugins/computer-use/dist/computer-use-0.2.1-darwin-arm64.shejane-plugin"

if [[ ! -d "${UPSTREAM_DIR}/.git" ]]; then
  mkdir -p "$(dirname "${UPSTREAM_DIR}")"
  git init "${UPSTREAM_DIR}"
  git -C "${UPSTREAM_DIR}" remote add origin https://github.com/injaneity/pi-computer-use.git
fi

if [[ "$(git -C "${UPSTREAM_DIR}" rev-parse HEAD 2>/dev/null || true)" != "${UPSTREAM_COMMIT}" ]]; then
  git -C "${UPSTREAM_DIR}" fetch --depth 1 origin "${UPSTREAM_COMMIT}"
  git -C "${UPSTREAM_DIR}" checkout --detach FETCH_HEAD
fi

mkdir -p "$(dirname "${OUTPUT}")"
cd "${ROOT_DIR}"
uv run --project runtime python runtime/plugins/computer-use/build_package.py \
  --platform darwin/arm64 \
  --upstream "${UPSTREAM_DIR}" \
  --output "${OUTPUT}"
