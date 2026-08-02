from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from shejane_runtime.plugins.browser_qa import (
    BrowserQAActionExecutor,
    BrowserQAError,
    BrowserQAService,
    prepare_browser_runtime,
    windows_extended_path,
)
from shejane_runtime.plugins.runtime_assets import RuntimeAssetHandle


def runtime_asset(payload: Path) -> RuntimeAssetHandle:
    return RuntimeAssetHandle(
        asset_id="org.shejane.browser-qa.runtime",
        version="1.61.1+chromium1228.3",
        platform="darwin/arm64",
        digest="sha256:" + "a" * 64,
        root=payload.parent,
        payload=payload,
        license="Apache-2.0 AND BSD-3-Clause",
        source_url="https://github.com/microsoft/playwright",
        sbom=payload.parent / "sbom.json",
    )


def invocation(action_id: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "invocation_id": "a23e4567-e89b-42d3-a456-426614174001",
        "operation_id": f"run_01:browser.{action_id}:001",
        "action": {"action_id": action_id},
        "arguments": arguments,
        "limits": {"timeout_ms": 30_000, "memory_mb": 1024, "output_mb": 8},
    }


def test_browser_qa_uses_extended_paths_on_windows() -> None:
    assert (
        windows_extended_path(
            r"C:\Users\Jane\AppData\Roaming\SheJane\browsers",
            platform_name="nt",
        )
        == r"\\?\C:\Users\Jane\AppData\Roaming\SheJane\browsers"
    )
    assert (
        windows_extended_path(
            r"\\server\share\SheJane\browsers",
            platform_name="nt",
        )
        == r"\\?\UNC\server\share\SheJane\browsers"
    )
    assert windows_extended_path("/tmp/shejane/browsers", platform_name="posix") == (
        "/tmp/shejane/browsers"
    )


def test_browser_qa_prepares_short_hardlinked_runtime_on_windows(tmp_path: Path) -> None:
    source = tmp_path / "source"
    executable = source / "chromium-1228" / "chrome-win64" / "chrome.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"browser")
    digest = "sha256:" + "a" * 64

    prepared = prepare_browser_runtime(
        source,
        tmp_path / "browser-runtime",
        digest,
        platform_name="nt",
    )

    linked = prepared / executable.relative_to(source)
    assert linked.read_bytes() == b"browser"
    assert linked.samefile(executable)
    assert (prepared / ".shejane-runtime-digest").read_text(encoding="utf-8") == (digest + "\n")
    assert (
        prepare_browser_runtime(
            source,
            tmp_path / "browser-runtime",
            digest,
            platform_name="nt",
        )
        == prepared
    )


def test_browser_qa_rejects_tampered_windows_runtime_alias(tmp_path: Path) -> None:
    source = tmp_path / "source"
    executable = source / "chromium-1228" / "chrome-win64" / "chrome.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"browser")
    digest = "sha256:" + "a" * 64
    alias_root = tmp_path / "browser-runtime"
    prepared = prepare_browser_runtime(source, alias_root, digest, platform_name="nt")
    linked = prepared / executable.relative_to(source)
    linked.unlink()
    linked.write_bytes(b"tampered")

    with pytest.raises(BrowserQAError, match="alias identity changed"):
        prepare_browser_runtime(source, alias_root, digest, platform_name="nt")


@pytest.mark.asyncio
async def test_browser_qa_action_rejects_private_navigation_before_launch(tmp_path: Path) -> None:
    class UnexpectedService:
        async def call(self, *_args, **_kwargs):
            raise AssertionError("private URL reached browser bridge")

    executor = BrowserQAActionExecutor(UnexpectedService(), "open")  # type: ignore[arg-type]

    with pytest.raises(BrowserQAError, match="private/loopback"):
        await executor.invoke(
            invocation("open", {"url": "http://127.0.0.1/private"}),
            input_root=tmp_path,
            output_root=tmp_path,
        )


@pytest.mark.asyncio
async def test_browser_qa_service_uses_isolated_profile_and_closes_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    package = tmp_path / "package"
    package.mkdir()
    payload = tmp_path / "asset" / "payload"
    payload.mkdir(parents=True)
    bridge = payload / "bridge-server.mjs"
    bridge.write_text(
        """
import readline from 'node:readline';
const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of lines) {
  const request = JSON.parse(line);
  process.stdout.write(JSON.stringify({ id: request.id, result: {
    pid: process.pid,
    profile: process.env.SHEJANE_BROWSER_QA_PROFILE,
    proxy: process.env.SHEJANE_BROWSER_QA_PROXY,
    systemRoot: process.env.SYSTEMROOT,
  } }) + '\\n');
}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SHEJANE_RUNTIME_NODE_PATH", node)
    monkeypatch.setenv("SYSTEMROOT", "C:\\Windows")
    prepared: list[Path] = []

    def prepare_on_first_call(
        source: Path,
        _alias_root: Path,
        _digest: str,
    ) -> Path:
        prepared.append(source)
        return source

    monkeypatch.setattr(
        "shejane_runtime.plugins.browser_qa.prepare_browser_runtime",
        prepare_on_first_call,
    )
    profile = tmp_path / "profiles" / "workspace"
    service = BrowserQAService(
        package,
        workspace_root=tmp_path,
        profile_root=profile,
        browser_runtime_root=tmp_path / "browser-runtime",
        runtime_asset=runtime_asset(payload),
    )
    assert prepared == []

    first = await service.call("observe", {}, timeout_ms=5_000)
    second = await service.call("observe", {}, timeout_ms=5_000)
    assert prepared == [payload / "browsers"]
    assert first["pid"] == second["pid"]
    assert first["profile"] == str(profile)
    assert str(first["proxy"]).startswith("http://127.0.0.1:")
    assert first["systemRoot"] == "C:\\Windows"

    await service.aclose()
    with pytest.raises(ProcessLookupError):
        os.kill(int(first["pid"]), 0)
