from __future__ import annotations

import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from shejane_runtime.plugins.browser_qa import BROWSER_QA_PLUGIN_VERSION
from shejane_runtime.plugins.manifest import PluginManifest
from shejane_runtime.plugins.package import extract_plugin_archive
from shejane_runtime.plugins.runtime_assets import RuntimeAssetStore

REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT = REPO_ROOT / "runtime" / "plugins" / "browser-qa"
BUILDER = ROOT / "build_package.py"
ASSET_BUILDER = ROOT / "build_runtime_asset.py"
WINDOWS_BUILDER = REPO_ROOT / "scripts" / "build-browser-qa-windows-amd64.ps1"


def test_browser_qa_package_uses_node_for_esbuild_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = runpy.run_path(str(BUILDER))
    with monkeypatch.context() as patch:
        patch.setattr(builder["shutil"], "which", lambda name: "C:\\Node\\node.exe")
        command = builder["esbuild_command"](platform_name="nt")

    assert command == [
        "C:\\Node\\node.exe",
        str((REPO_ROOT / "node_modules" / "esbuild" / "bin" / "esbuild").resolve()),
    ]


def test_windows_browser_qa_gate_uses_short_pytest_workspace() -> None:
    script = WINDOWS_BUILDER.read_text(encoding="utf-8")

    assert 'Join-Path $env:RUNNER_TEMP "sj-bq-test"' in script
    assert "--basetemp $testRoot" in script


def test_browser_qa_manifest_exposes_only_bounded_actions() -> None:
    template = (ROOT / ".shejane-plugin" / "plugin.template.json").read_text(encoding="utf-8")
    manifest = PluginManifest.model_validate_json(
        template.replace("__PLUGIN_VERSION__", BROWSER_QA_PLUGIN_VERSION)
        .replace("__PLATFORM__", "darwin/arm64")
        .replace("__RUNTIME_ASSET_DIGEST__", "sha256:" + "a" * 64)
    )

    assert manifest.runtime.execution.kind == "builtin"
    assert manifest.runtime.execution.handler == "browser_qa"
    assert manifest.runtime.execution.runtime_assets[0].id == "org.shejane.browser-qa.runtime"
    assert {action.id for action in manifest.contributions.actions} == {
        "open",
        "observe",
        "act",
        "inspect",
        "close",
    }
    for action in manifest.contributions.actions:
        for relative in (action.input_schema, action.output_schema):
            schema = json.loads((ROOT / relative).read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            assert schema["additionalProperties"] is False


@pytest.mark.parametrize("target_platform", ["darwin/arm64", "windows/amd64"])
def test_browser_qa_package_is_deterministic(tmp_path: Path, target_platform: str) -> None:
    playwright = tmp_path / "playwright"
    playwright_core = tmp_path / "playwright-core"
    for root, name in (
        (playwright, "playwright"),
        (playwright_core, "playwright-core"),
    ):
        root.mkdir()
        (root / "package.json").write_text(
            json.dumps({"name": name, "version": "1.61.1"}), encoding="utf-8"
        )
        (root / "index.js").write_text("module.exports = {};\n", encoding="utf-8")
    outputs = [tmp_path / "first.shejane-plugin", tmp_path / "second.shejane-plugin"]
    for output in outputs:
        subprocess.run(
            [
                sys.executable,
                str(BUILDER),
                "--platform",
                target_platform,
                "--playwright",
                str(playwright),
                "--playwright-core",
                str(playwright_core),
                "--runtime-asset-digest",
                "sha256:" + "a" * 64,
                "--output",
                str(output),
            ],
            check=True,
        )

    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    extracted = tmp_path / "extracted"
    extract_plugin_archive(outputs[0], extracted)
    assert (extracted / "payload/bridge-server.mjs").is_file()
    assert (extracted / "payload/node_modules/playwright/package.json").is_file()
    assert (extracted / "payload/node_modules/playwright-core/package.json").is_file()
    assert not (extracted / "payload/browsers").exists()


@pytest.mark.parametrize(
    ("target_platform", "executable_name"),
    [
        ("darwin/arm64", "chrome"),
        ("windows/amd64", "chrome.exe"),
    ],
)
def test_browser_qa_runtime_asset_is_deterministic_and_content_addressed(
    tmp_path: Path, target_platform: str, executable_name: str
) -> None:
    browser = tmp_path / "chromium-1228"
    headless_shell = tmp_path / "chromium_headless_shell-1228"
    for root in (browser, headless_shell):
        root.mkdir()
        executable = root / executable_name
        executable.write_bytes(b"browser")
        if target_platform == "darwin/arm64":
            executable.chmod(0o500)
    outputs = [
        tmp_path / "first.shejane-runtime-asset",
        tmp_path / "second.shejane-runtime-asset",
    ]
    for output in outputs:
        subprocess.run(
            [
                sys.executable,
                str(ASSET_BUILDER),
                "--platform",
                target_platform,
                "--browser",
                str(browser),
                "--headless-shell",
                str(headless_shell),
                "--output",
                str(output),
            ],
            check=True,
        )

    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    installed = RuntimeAssetStore(tmp_path / "asset-store").install(
        outputs[0], target_platform=target_platform
    )
    assert installed.asset_id == "org.shejane.browser-qa.runtime"
    assert installed.version == "1.61.1+chromium1228.1"
    assert (installed.payload / "browsers" / "chromium-1228" / executable_name).is_file()
