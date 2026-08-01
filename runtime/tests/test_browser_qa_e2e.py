from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from shejane_runtime.agent.context_builder import RuntimeContext
from shejane_runtime.auth import LOCAL_OWNER_PRINCIPAL_ID
from shejane_runtime.plugins.browser_qa import (
    BROWSER_QA_PLUGIN_VERSION,
    BrowserQAActionExecutor,
    BrowserQAService,
)
from shejane_runtime.plugins.catalog import PluginActionDescriptor
from shejane_runtime.plugins.package import extract_plugin_archive
from shejane_runtime.plugins.platforms import current_managed_worker_platform
from shejane_runtime.plugins.runtime_assets import RuntimeAssetHandle, RuntimeAssetStore
from shejane_runtime.plugins.tools import PluginToolAdapter
from shejane_runtime.store.sqlite import LocalStore
from shejane_runtime.tools.runtime import RuntimeToolExecution

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE = REPO_ROOT / "runtime" / "plugins" / "browser-qa" / "bridge" / "bridge-server.ts"
PLAYWRIGHT = (
    REPO_ROOT / "node_modules" / ".pnpm" / "playwright@1.61.1" / "node_modules" / "playwright"
)
PLAYWRIGHT_CORE = (
    REPO_ROOT
    / "node_modules"
    / ".pnpm"
    / "playwright-core@1.61.1"
    / "node_modules"
    / "playwright-core"
)


def playwright_browsers_root() -> Path:
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    if sys.platform == "win32":
        return Path(os.environ["LOCALAPPDATA"]) / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


BROWSERS_ROOT = playwright_browsers_root()
BROWSER = BROWSERS_ROOT / "chromium-1228"
PLUGIN_ROOT = REPO_ROOT / "runtime" / "plugins" / "browser-qa"


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"""<!doctype html><html><head><title>Browser QA fixture</title></head>
<body><label>Name <input aria-label="Name"></label>
<button onclick="document.querySelector('#result').textContent='Saved ' + document.querySelector('input').value">Save</button>
<div id="result">Not saved</div></body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


def build_test_package(root: Path) -> None:
    payload = root / "payload"
    modules = payload / "node_modules"
    browsers = payload / "browsers"
    modules.mkdir(parents=True)
    browsers.mkdir()
    (modules / "playwright").symlink_to(PLAYWRIGHT, target_is_directory=True)
    (modules / "playwright-core").symlink_to(PLAYWRIGHT_CORE, target_is_directory=True)
    (browsers / "chromium-1228").symlink_to(BROWSER, target_is_directory=True)
    subprocess.run(
        [
            "pnpm",
            "exec",
            "esbuild",
            str(BRIDGE),
            "--bundle",
            "--platform=node",
            "--format=esm",
            "--target=node20",
            "--external:playwright",
            "--legal-comments=none",
            f"--outfile={payload / 'bridge-server.mjs'}",
        ],
        cwd=REPO_ROOT,
        check=True,
    )


def install_test_artifacts(
    tmp_path: Path, host_platform: str
) -> tuple[Path, RuntimeAssetHandle] | None:
    package_source = os.environ.get("SHEJANE_TEST_BROWSER_QA_PACKAGE")
    asset_source = os.environ.get("SHEJANE_TEST_BROWSER_QA_RUNTIME_ASSET")
    if package_source is None and asset_source is None:
        return None
    if not package_source or not asset_source:
        pytest.fail("Browser QA package and Runtime Asset must be supplied together")
    package = tmp_path / "package"
    extract_plugin_archive(Path(package_source), package)
    runtime_asset = RuntimeAssetStore(tmp_path / "asset-store").install(
        Path(asset_source),
        target_platform=host_platform,
    )
    return package, runtime_asset


def action_descriptor(package: Path, action_id: str) -> PluginActionDescriptor:
    template = json.loads(
        (PLUGIN_ROOT / ".shejane-plugin" / "plugin.template.json").read_text(encoding="utf-8")
    )
    action = next(item for item in template["contributions"]["actions"] if item["id"] == action_id)
    return PluginActionDescriptor(
        plugin_id="org.shejane.browser-qa",
        plugin_version=BROWSER_QA_PLUGIN_VERSION,
        plugin_digest="sha256:" + "b" * 64,
        action_id=action_id,
        tool_name=f"plugin.org.shejane.browser-qa.{action_id}",
        title=action["title"],
        description=action["description"],
        action_schema_digest="sha256:" + "c" * 64,
        input_schema=json.loads((PLUGIN_ROOT / action["input_schema"]).read_text(encoding="utf-8")),
        output_schema=json.loads(
            (PLUGIN_ROOT / action["output_schema"]).read_text(encoding="utf-8")
        ),
        consumes=tuple(action["consumes"]),
        produces=tuple(action["produces"]),
        effects=tuple(action["effects"]),
        determinism=action["determinism"],
        capabilities=tuple(action["capabilities"]),
        limits=action["limits"],
        package_root=package,
        entrypoint=package / "payload" / "bridge-server.mjs",
        entrypoint_digest="sha256:" + "d" * 64,
        execution_kind="builtin",
        execution_handler="browser_qa",
        runtime_assets=(),
        model_binding=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("headless", [True, False], ids=("headless", "headed"))
async def test_browser_qa_real_chromium_open_act_observe_and_screenshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, headless: bool
) -> None:
    node = shutil.which("node")
    artifacts_configured = bool(os.environ.get("SHEJANE_TEST_BROWSER_QA_PACKAGE"))
    source_runtime_missing = (
        not PLAYWRIGHT.is_dir() or not PLAYWRIGHT_CORE.is_dir() or not BROWSER.is_dir()
    )
    if node is None or (not artifacts_configured and source_runtime_missing):
        if os.environ.get("SHEJANE_REQUIRE_FIXED_PLUGIN_E2E") == "1":
            pytest.fail("pinned local Playwright runtime is required")
        pytest.skip("pinned local Playwright runtime is unavailable")
    host_platform = current_managed_worker_platform()
    assert host_platform is not None
    installed = install_test_artifacts(tmp_path, host_platform)
    if installed is None:
        package = tmp_path / "package"
        build_test_package(package)
        runtime_asset = RuntimeAssetHandle(
            asset_id="org.shejane.browser-qa.runtime",
            version="1.61.1+chromium1228.2",
            platform=host_platform,
            digest="sha256:" + "a" * 64,
            root=package,
            payload=package / "payload",
            license="Apache-2.0 AND BSD-3-Clause",
            source_url="https://github.com/microsoft/playwright",
            sbom=package / "sbom.json",
        )
    else:
        package, runtime_asset = installed
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SHEJANE_RUNTIME_NODE_PATH", node)

    def resolve_fixture(_host: str, **_kwargs: object) -> tuple[bool, str, str]:
        return True, "", "127.0.0.1"

    monkeypatch.setattr("shejane_runtime.plugins.browser_qa._resolve_pinned", resolve_fixture)
    monkeypatch.setattr("shejane_runtime.tools.web._resolve_pinned", resolve_fixture)
    store = await LocalStore.open(tmp_path / "runtime.db")
    run = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="verify the browser fixture",
        workspace_path=None,
    )
    service = BrowserQAService(
        package,
        workspace_root=tmp_path,
        profile_root=tmp_path / "profile",
        browser_runtime_root=tmp_path / "browser-runtime",
        runtime_asset=runtime_asset,
        headless=headless,
    )
    adapter = PluginToolAdapter(
        executor_factory=lambda action: BrowserQAActionExecutor(service, action.action_id)
    )
    context = RuntimeContext(store=store, run_id=str(run["id"]), plugin_inputs=())

    async def invoke(action_id: str, arguments: dict[str, object]) -> object:
        return await adapter.invoke(
            action_descriptor(package, action_id),
            arguments,
            RuntimeToolExecution(
                context=context,
                operation_id=f"toolop_browser_{action_id}",
                tool_call_id=f"call_browser_{action_id}",
            ),
        )

    try:
        opened_result = await invoke(
            "open",
            {"url": f"http://fixture.test:{server.server_port}/"},
        )
        assert isinstance(opened_result, dict)
        assert opened_result["provenance"]["plugin"]["id"] == "org.shejane.browser-qa"
        opened = opened_result["output"]
        assert opened["title"] == "Browser QA fixture"
        assert "Not saved" in opened["text"]
        refs = {item["name"]: item["ref"] for item in opened["elements"]}

        filled_result = await invoke(
            "act",
            {
                "state_id": opened["state_id"],
                "action": "fill",
                "ref": refs["Name"],
                "value": "石间",
            },
        )
        assert isinstance(filled_result, dict)
        filled = filled_result["output"]
        filled_refs = {item["name"]: item["ref"] for item in filled["elements"]}
        clicked_result = await invoke(
            "act",
            {"state_id": filled["state_id"], "action": "click", "ref": filled_refs["Save"]},
        )
        assert isinstance(clicked_result, dict)
        clicked = clicked_result["output"]
        assert "Saved 石间" in clicked["text"]

        screenshot = await invoke("inspect", {"kind": "screenshot"})
        assert isinstance(screenshot, list)
        assert screenshot[1]["type"] == "image"
        assert screenshot[1]["mime_type"] == "image/png"
        assert len(screenshot[1]["base64"]) > 1000
        closed_result = await invoke("close", {})
        assert isinstance(closed_result, dict)
        assert closed_result["output"] == {"closed": True}
        assert "authorization" not in json.dumps(screenshot).lower()
    finally:
        await service.aclose()
        await store.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
