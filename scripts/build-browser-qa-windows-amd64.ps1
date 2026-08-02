param(
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$PSNativeCommandUseErrorActionPreference = $true

$repoRoot = Split-Path -Parent $PSScriptRoot
$workRoot = Join-Path $env:RUNNER_TEMP "shejane-browser-qa-windows-amd64"
if (Test-Path $workRoot) {
    throw "Windows Browser QA build workspace already exists: $workRoot"
}
New-Item -ItemType Directory -Path $workRoot | Out-Null
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

$browsersRoot = Join-Path $workRoot "ms-playwright"
$firstAsset = Join-Path $workRoot "browser-qa-runtime-windows-amd64-a.shejane-runtime-asset"
$secondAsset = Join-Path $workRoot "browser-qa-runtime-windows-amd64-b.shejane-runtime-asset"
$plugin = Join-Path $workRoot "browser-qa-0.1.3-windows-amd64.shejane-plugin"

Push-Location $repoRoot
try {
    $env:PLAYWRIGHT_BROWSERS_PATH = $browsersRoot
    pnpm --filter @shejane/client exec playwright install --no-shell chromium

    $browser = Join-Path $browsersRoot "chromium-1228"
    if (-not (Test-Path $browser -PathType Container)) {
        throw "Playwright 1.61.1 Chromium 1228 was not installed"
    }

    Push-Location (Join-Path $repoRoot "client")
    try {
        $playwrightTestPackage = (
            & node -e "console.log(require.resolve('@playwright/test/package.json'))"
        ).Trim()
    }
    finally {
        Pop-Location
    }
    $playwrightPackage = (
        & node -e `
            "console.log(require.resolve('playwright/package.json', {paths: [require('path').dirname(process.argv[1])]}))" `
            $playwrightTestPackage
    ).Trim()
    $playwrightCorePackage = (
        & node -e `
            "console.log(require.resolve('playwright-core/package.json', {paths: [require('path').dirname(process.argv[1])]}))" `
            $playwrightPackage
    ).Trim()
    $playwrightRoot = Split-Path -Parent $playwrightPackage
    $playwrightCoreRoot = Split-Path -Parent $playwrightCorePackage

    foreach ($asset in @($firstAsset, $secondAsset)) {
        python runtime/plugins/browser-qa/build_runtime_asset.py `
            --platform windows/amd64 `
            --browser $browser `
            --playwright $playwrightRoot `
            --playwright-core $playwrightCoreRoot `
            --output $asset
    }
    $firstHash = (Get-FileHash -Algorithm SHA256 $firstAsset).Hash
    $secondHash = (Get-FileHash -Algorithm SHA256 $secondAsset).Hash
    if ($firstHash -ne $secondHash) {
        throw "Windows Browser QA Runtime Asset build is not reproducible"
    }

    $digestOutput = & uv run --project runtime python -c `
        "import sys; from pathlib import Path; from shejane_runtime.plugins.runtime_assets import RuntimeAssetStore; print(RuntimeAssetStore(Path(sys.argv[2])).install(Path(sys.argv[1]), target_platform='windows/amd64').digest)" `
        $firstAsset (Join-Path $workRoot "asset-store")
    if ($LASTEXITCODE -ne 0) {
        throw "Windows Browser QA Runtime Asset validation failed"
    }
    $digest = ($digestOutput | Select-Object -Last 1).Trim()
    if ($digest -notmatch "^sha256:[0-9a-f]{64}$") {
        throw "Windows Browser QA Runtime Asset digest is invalid"
    }

    python runtime/plugins/browser-qa/build_package.py `
        --platform windows/amd64 `
        --runtime-asset-digest $digest `
        --output $plugin

    $env:SHEJANE_TEST_BROWSER_QA_PACKAGE = $plugin
    $env:SHEJANE_TEST_BROWSER_QA_RUNTIME_ASSET = $firstAsset
    $env:SHEJANE_REQUIRE_FIXED_PLUGIN_E2E = "1"
    $testRoot = Join-Path $env:RUNNER_TEMP "sj-bq-test"
    if (Test-Path $testRoot) {
        throw "Windows Browser QA test workspace already exists: $testRoot"
    }
    uv run --project runtime python -m pytest -q `
        --basetemp $testRoot `
        runtime/tests/test_browser_qa_e2e.py

    Copy-Item $firstAsset (
        Join-Path $OutputDirectory `
            "browser-qa-runtime-1.61.1-windows-amd64.shejane-runtime-asset"
    )
    Copy-Item $plugin (
        Join-Path $OutputDirectory "browser-qa-0.1.3-windows-amd64.shejane-plugin"
    )
}
finally {
    Pop-Location
}
