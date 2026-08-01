#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]] || exit 0

cd "${ROOT_DIR}"
pnpm --filter @shejane/client exec playwright install chromium

PLAYWRIGHT_TEST_PACKAGE="$(
  cd "${ROOT_DIR}/client"
  node -e "console.log(require.resolve('@playwright/test/package.json'))"
)"
PLAYWRIGHT_PACKAGE="$(node -e \
  "console.log(require.resolve('playwright/package.json', {paths: [require('path').dirname(process.argv[1])]}))" \
  "${PLAYWRIGHT_TEST_PACKAGE}")"
PLAYWRIGHT_ROOT="$(dirname "${PLAYWRIGHT_PACKAGE}")"
PLAYWRIGHT_CORE_PACKAGE="$(node -e \
  "console.log(require.resolve('playwright-core/package.json', {paths: [require('path').dirname(process.argv[1])]}))" \
  "${PLAYWRIGHT_PACKAGE}")"
PLAYWRIGHT_CORE_ROOT="$(dirname "${PLAYWRIGHT_CORE_PACKAGE}")"
BROWSERS_ROOT="${PLAYWRIGHT_BROWSERS_PATH:-${HOME}/Library/Caches/ms-playwright}"
OUTPUT="${ROOT_DIR}/runtime/plugins/browser-qa/dist/browser-qa-0.1.1-darwin-arm64.shejane-plugin"
RUNTIME_ASSET="${ROOT_DIR}/runtime/plugins/browser-qa/dist/browser-qa-runtime-1.61.1-darwin-arm64.shejane-runtime-asset"

mkdir -p "$(dirname "${OUTPUT}")"
uv run --project runtime python runtime/plugins/browser-qa/build_runtime_asset.py \
  --platform darwin/arm64 \
  --browser "${BROWSERS_ROOT}/chromium-1228" \
  --headless-shell "${BROWSERS_ROOT}/chromium_headless_shell-1228" \
  --output "${RUNTIME_ASSET}"
RUNTIME_ASSET_DIGEST="$({
  cd "${ROOT_DIR}/runtime"
  uv run python - "${RUNTIME_ASSET}" <<'PY'
from pathlib import Path
import sys
import tempfile

from shejane_runtime.plugins.runtime_assets import RuntimeAssetStore

with tempfile.TemporaryDirectory(prefix="shejane-browser-qa-stage-") as temporary:
    installed = RuntimeAssetStore(Path(temporary)).install(
        Path(sys.argv[1]), target_platform="darwin/arm64"
    )
    if (
        installed.asset_id != "org.shejane.browser-qa.runtime"
        or installed.version != "1.61.1+chromium1228.1"
        or installed.platform != "darwin/arm64"
    ):
        raise SystemExit("Browser QA Runtime Asset identity is incompatible")
    print(installed.digest)
PY
})"
uv run --project runtime python runtime/plugins/browser-qa/build_package.py \
  --platform darwin/arm64 \
  --playwright "${PLAYWRIGHT_ROOT}" \
  --playwright-core "${PLAYWRIGHT_CORE_ROOT}" \
  --runtime-asset-digest "${RUNTIME_ASSET_DIGEST}" \
  --output "${OUTPUT}"
