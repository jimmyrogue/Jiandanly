#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]] || exit 0
output_dir="${repo_root}/runtime/plugins/ocr/dist"
plugin_output="${output_dir}/ocr-0.1.4-darwin-arm64.shejane-plugin"
asset_output="${output_dir}/rapidocr-runtime-3.9.1-darwin-arm64.shejane-runtime-asset"

: "${SHEJANE_RAPIDOCR_RUNTIME_ASSET:?Set SHEJANE_RAPIDOCR_RUNTIME_ASSET to the verified RapidOCR Runtime Asset}"

asset="$(cd "$(dirname "${SHEJANE_RAPIDOCR_RUNTIME_ASSET}")" && pwd)/$(basename "${SHEJANE_RAPIDOCR_RUNTIME_ASSET}")"
test -f "${asset}"
mkdir -p "${output_dir}"

worker_build="${repo_root}/runtime/build/ocr-worker-darwin-arm64"
rm -rf "${worker_build}"
(
  cd "${repo_root}/runtime"
  uv run python -m PyInstaller \
    --noconfirm \
    --clean \
    --onedir \
    --name ocr-worker \
    --distpath "${worker_build}" \
    --workpath "${repo_root}/runtime/build/ocr-worker-darwin-arm64-work" \
    --specpath "${repo_root}/runtime/build" \
    plugins/ocr/worker/ocr_worker.py
)
worker="${worker_build}/ocr-worker"

(
  cd "${repo_root}/runtime"
  uv run python plugins/ocr/build_runtime_asset.py \
    --platform darwin/arm64 \
    --base-asset "${asset}" \
    --worker "${worker}" \
    --output "${asset_output}"
)

digest="$({
  cd "${repo_root}/runtime"
  uv run python - "${asset_output}" <<'PY'
from pathlib import Path
import sys
import tempfile

from shejane_runtime.plugins.runtime_assets import RuntimeAssetStore

with tempfile.TemporaryDirectory(prefix="shejane-ocr-stage-") as temporary:
    installed = RuntimeAssetStore(Path(temporary)).install(
        Path(sys.argv[1]), target_platform="darwin/arm64"
    )
    if (
        installed.asset_id != "org.rapidocr.runtime"
        or installed.version != "3.9.1+ppocrv6-small.2"
        or installed.platform != "darwin/arm64"
    ):
        raise SystemExit("RapidOCR Runtime Asset identity is incompatible")
    print(installed.digest)
PY
})"

(
  cd "${repo_root}/runtime"
  uv run python plugins/ocr/build_package.py \
    --platform darwin/arm64 \
    --runtime-asset-digest "${digest}" \
    --output "${plugin_output}"
)

echo "OCR fixed capability staged: ${plugin_output}"
