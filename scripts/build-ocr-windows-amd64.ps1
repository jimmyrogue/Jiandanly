param(
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$PSNativeCommandUseErrorActionPreference = $true

$repoRoot = Split-Path -Parent $PSScriptRoot
$runtimeAssets = Join-Path $repoRoot "runtime/plugins/ocr/runtime-assets"
$workRoot = Join-Path $env:RUNNER_TEMP "shejane-ocr-windows-amd64"
if (Test-Path $workRoot) {
    throw "Windows OCR build workspace already exists: $workRoot"
}
New-Item -ItemType Directory -Path $workRoot | Out-Null
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

$wheelhouse = Join-Path $workRoot "wheelhouse"
$models = Join-Path $workRoot "models"
$firstAsset = Join-Path $workRoot "rapidocr-runtime-windows-amd64-a.shejane-runtime-asset"
$secondAsset = Join-Path $workRoot "rapidocr-runtime-windows-amd64-b.shejane-runtime-asset"
$finalAsset = Join-Path $workRoot "rapidocr-runtime-windows-amd64-final.shejane-runtime-asset"
$worker = Join-Path $workRoot "ocr-worker"
$plugin = Join-Path $workRoot "ocr-0.1.4-windows-amd64.shejane-plugin"

Push-Location $repoRoot
try {
    python runtime/plugins/ocr/runtime-assets/fetch_locked_inputs.py `
        --package-lock runtime/plugins/ocr/runtime-assets/rapidocr-3.9.1-windows-amd64.lock.json `
        --model-lock runtime/plugins/ocr/runtime-assets/rapidocr-3.9.1.lock.json `
        --wheelhouse $wheelhouse `
        --model-dir $models

    foreach ($asset in @($firstAsset, $secondAsset)) {
        python runtime/plugins/ocr/runtime-assets/build_windows_amd64.py `
            --wheelhouse $wheelhouse `
            --model-dir $models `
            --output $asset
    }
    $firstHash = (Get-FileHash -Algorithm SHA256 $firstAsset).Hash
    $secondHash = (Get-FileHash -Algorithm SHA256 $secondAsset).Hash
    if ($firstHash -ne $secondHash) {
        throw "Windows RapidOCR Runtime Asset build is not reproducible"
    }

    python runtime/plugins/ocr/runtime-assets/build_worker_windows_amd64.py `
        --wheelhouse $wheelhouse `
        --output $worker

    uv run --project runtime python runtime/plugins/ocr/build_runtime_asset.py `
        --platform windows/amd64 `
        --base-asset $firstAsset `
        --worker $worker `
        --output $finalAsset

    $digestOutput = & uv run --project runtime python -c `
        "import sys; from pathlib import Path; from shejane_runtime.plugins.runtime_assets import RuntimeAssetStore; print(RuntimeAssetStore(Path(sys.argv[2])).install(Path(sys.argv[1]), target_platform='windows/amd64').digest)" `
        $finalAsset (Join-Path $workRoot "asset-store")
    if ($LASTEXITCODE -ne 0) {
        throw "Windows RapidOCR Runtime Asset validation failed"
    }
    $digest = ($digestOutput | Select-Object -Last 1).Trim()
    if ($digest -notmatch "^sha256:[0-9a-f]{64}$") {
        throw "Windows RapidOCR Runtime Asset digest is invalid"
    }

    python runtime/plugins/ocr/build_package.py `
        --platform windows/amd64 `
        --runtime-asset-digest $digest `
        --output $plugin

    $env:SHEJANE_RAPIDOCR_RUNTIME_ASSET = $finalAsset
    $env:SHEJANE_TEST_OCR_WORKER = Join-Path $worker "ocr-worker.exe"
    $testRoot = Join-Path $env:RUNNER_TEMP "sj-ocr-test"
    if (Test-Path $testRoot) {
        throw "Windows OCR test workspace already exists: $testRoot"
    }
    uv run --project runtime python -m pytest -q `
        --basetemp $testRoot `
        runtime/tests/test_ocr_runtime_asset.py

    Copy-Item $finalAsset (
        Join-Path $OutputDirectory "rapidocr-runtime-3.9.1-windows-amd64.shejane-runtime-asset"
    )
    Copy-Item $plugin (
        Join-Path $OutputDirectory "ocr-0.1.4-windows-amd64.shejane-plugin"
    )
}
finally {
    Pop-Location
}
