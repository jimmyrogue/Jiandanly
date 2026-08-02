from __future__ import annotations

import hashlib
import io
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from shejane_runtime.agent.context_builder import RuntimeContext
from shejane_runtime.auth import LOCAL_OWNER_PRINCIPAL_ID
from shejane_runtime.plugins.catalog import PluginActionDescriptor
from shejane_runtime.plugins.executor import ManagedWorkerActionExecutor
from shejane_runtime.plugins.ocr import OCRActionExecutor
from shejane_runtime.plugins.runtime_assets import RuntimeAssetHandle
from shejane_runtime.plugins.tools import PluginToolAdapter
from shejane_runtime.runs import _plugin_input_snapshots
from shejane_runtime.store.sqlite import LocalStore
from shejane_runtime.tools.runtime import RuntimeToolExecution

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER = REPO_ROOT / "runtime" / "plugins" / "ocr" / "worker" / "ocr_worker.py"
PLUGIN_ROOT = REPO_ROOT / "runtime" / "plugins" / "ocr"


def test_ocr_worker_protocol_uses_utf8_bytes_for_multilingual_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = runpy.run_path(str(WORKER))
    protocol_input = SimpleNamespace(buffer=io.BytesIO('{"text":"石间 OCR"}\n'.encode()))
    protocol_output = SimpleNamespace(buffer=io.BytesIO())

    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdin", protocol_input)
        patch.setattr(sys, "stdout", protocol_output)
        assert worker["receive"]() == {"text": "石间 OCR"}
        worker["send"]({"text": "石间 OCR"})

    assert protocol_output.buffer.getvalue() == '{"text":"石间 OCR"}\n'.encode()


def executable(path: Path, source: str) -> None:
    path.write_text("#!/usr/bin/env python3\n" + source, encoding="utf-8")
    path.chmod(0o500)


def fake_asset(tmp_path: Path) -> RuntimeAssetHandle:
    root = tmp_path / "rapidocr-asset"
    payload = root / "payload"
    binary = payload / "bin" / "ocr-engine"
    binary.parent.mkdir(parents=True)
    executable(
        binary,
        """import json, pathlib, sys
request = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if request["inputs"][0]["id"] == "engine-failure":
    print("ONNX Runtime warning without sensitive data", file=sys.stderr)
    print("OCR engine failed: ImportError|onnxruntime.capi", file=sys.stderr)
    raise SystemExit(2)
if request["inputs"][0]["id"] == "native-failure":
    raise SystemExit(9)
images = []
for index, item in enumerate(request["inputs"], start=1):
    images.append({
        "input_id": item["id"],
        "width": 640,
        "height": 480,
        "lines": [
            {"text": f"page {index} primary", "confidence": 0.987654, "polygon": [[1, 2], [100, 2], [100, 20], [1, 20]]},
            {"text": "low confidence", "confidence": 0.2, "polygon": [[2, 30], [80, 30], [80, 50], [2, 50]]},
        ],
    })
engine_name = "Unexpected" if request["inputs"][0]["id"] == "bad-engine" else "RapidOCR"
response = {
    "engine": {"name": engine_name, "version": "3.9.1", "model": "PP-OCRv6-small", "provider": "CPUExecutionProvider"},
    "images": images,
}
pathlib.Path(sys.argv[2]).write_text(json.dumps(response), encoding="utf-8")
""",
    )
    worker = payload / "worker" / "ocr-worker"
    worker.parent.mkdir()
    worker.write_text(
        f"#!/bin/sh\nexec {sys.executable!s} {WORKER!s}\n",
        encoding="utf-8",
    )
    worker.chmod(0o500)
    sbom = root / "sbom.json"
    sbom.write_text("{}", encoding="utf-8")
    return RuntimeAssetHandle(
        asset_id="org.rapidocr.runtime",
        version="3.9.1+ppocrv6-small.2",
        platform="darwin/arm64",
        digest="sha256:" + "a" * 64,
        root=root,
        payload=payload,
        license="Apache-2.0",
        source_url="https://github.com/RapidAI/RapidOCR",
        sbom=sbom,
    )


def test_windows_ocr_executor_selects_native_worker_entrypoint(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    package_root.mkdir()
    asset_root = tmp_path / "asset"
    payload = asset_root / "payload"
    payload.mkdir(parents=True)
    entrypoint = payload / "worker" / "ocr-worker.exe"
    entrypoint.parent.mkdir()
    entrypoint.write_bytes(b"MZ")
    sbom = asset_root / "sbom.json"
    sbom.write_text("{}", encoding="utf-8")
    asset = RuntimeAssetHandle(
        asset_id="org.rapidocr.runtime",
        version="3.9.1+ppocrv6-small.2",
        platform="windows/amd64",
        digest="sha256:" + "a" * 64,
        root=asset_root,
        payload=payload,
        license="Apache-2.0",
        source_url="https://github.com/RapidAI/RapidOCR",
        sbom=sbom,
    )

    executor = OCRActionExecutor(package_root, asset)

    assert executor._executor.command == (str(entrypoint),)


def invocation(
    input_ids: list[str],
    sources: list[tuple[str, Path, str]],
) -> dict[str, object]:
    inputs = []
    for input_id, source, media_type in sources:
        data = source.read_bytes()
        inputs.append(
            {
                "id": input_id,
                "path": f"/input/source/{source.name}",
                "media_type": media_type,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return {
        "schema_version": 1,
        "invocation_id": "a23e4567-e89b-42d3-a456-426614174001",
        "operation_id": "run_01:ocr.recognize_images:001",
        "action": {
            "plugin_id": "org.shejane.ocr",
            "plugin_version": "0.1.3",
            "plugin_digest": "sha256:" + "b" * 64,
            "action_id": "ocr.recognize_images",
        },
        "arguments": {
            "input_ids": input_ids,
            "minimum_confidence": 0.5,
            "max_lines": 100,
            "max_characters": 1_000,
            "include_text_artifact": True,
            "include_json_artifact": True,
        },
        "inputs": inputs,
        "grants": {"capabilities": ["input.read", "artifact.write"]},
        "limits": {"timeout_ms": 30_000, "memory_mb": 2048, "output_mb": 64},
        "environment": {"locale": "en-US", "timezone": "UTC"},
    }


def image_sources(tmp_path: Path) -> tuple[Path, Path, list[tuple[str, Path, str]]]:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    source_root = input_root / "source"
    source_root.mkdir(parents=True)
    first = source_root / "first.png"
    second = source_root / "second.jpg"
    first.write_bytes(b"fake png")
    second.write_bytes(b"fake jpeg")
    output_root.mkdir()
    return (
        input_root,
        output_root,
        [
            ("first", first, "image/png"),
            ("second", second, "image/jpeg"),
        ],
    )


def action_descriptor(tmp_path: Path, asset: RuntimeAssetHandle) -> PluginActionDescriptor:
    template = json.loads(
        (PLUGIN_ROOT / ".shejane-plugin" / "plugin.template.json").read_text(encoding="utf-8")
    )
    action = template["contributions"]["actions"][0]
    package_root = tmp_path / "plugins" / "packages" / ("b" * 64)
    package_root.mkdir(parents=True)
    return PluginActionDescriptor(
        plugin_id="org.shejane.ocr",
        plugin_version="0.1.3",
        plugin_digest="sha256:" + "b" * 64,
        action_id=action["id"],
        tool_name="plugin.org.shejane.ocr.ocr.recognize_images",
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
        package_root=package_root,
        entrypoint=WORKER,
        entrypoint_digest="sha256:" + "d" * 64,
        execution_kind="builtin",
        execution_handler="ocr",
        runtime_assets=(asset,),
        model_binding=None,
    )


@pytest.mark.asyncio
async def test_ocr_runtime_tool_e2e_persists_text_and_json_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "receipt.png"
    source.write_bytes(b"fake png")
    asset = fake_asset(tmp_path)
    descriptor = action_descriptor(tmp_path, asset)
    store = await LocalStore.open(tmp_path / "runtime.db")
    size, digest, blob_key = await store.prepare_run_input_body(source)
    run, _ = await store.accept_run_command(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        command_id="cmd_ocr_virtual_path",
        client_message_id="msg_ocr_virtual_path",
        command_payload={"goal": "recognize the receipt"},
        goal="recognize the receipt",
        workspace_path=None,
        mode="fast",
        run_inputs=[
            {
                "input_id": "source",
                "virtual_path": "/attachments/receipt.png",
                "original_name": source.name,
                "media_type": "image/png",
                "bytes": size,
                "sha256": digest,
                "blob_key": blob_key,
            }
        ],
    )
    context = RuntimeContext(
        store=store,
        run_id=str(run["id"]),
        plugin_inputs=await _plugin_input_snapshots(store, str(run["id"])),
    )
    executor = OCRActionExecutor(descriptor.package_root, asset)
    try:
        result = await PluginToolAdapter(executor_factory=lambda _action: executor).invoke(
            descriptor,
            {
                "input_ids": ["/attachments/receipt.png"],
                "minimum_confidence": 0.5,
                "max_lines": 100,
                "max_characters": 1_000,
                "include_text_artifact": True,
                "include_json_artifact": True,
            },
            RuntimeToolExecution(
                context=context,
                operation_id="toolop_ocr",
                tool_call_id="call_ocr",
            ),
        )
    finally:
        await store.close()

    assert result["status"] == "succeeded"
    assert result["output"]["images"][0]["full_text"] == "page 1 primary"
    assert [artifact["name"] for artifact in result["artifacts"]] == [
        "ocr.txt",
        "ocr.json",
    ]
    assert result["provenance"]["runtime_assets"] == [
        {
            "id": "org.rapidocr.runtime",
            "version": "3.9.1+ppocrv6-small.2",
            "digest": "sha256:" + "a" * 64,
            "platform": "darwin/arm64",
        }
    ]


@pytest.mark.asyncio
async def test_ocr_worker_preserves_batch_order_filters_and_writes_artifacts(
    tmp_path: Path,
) -> None:
    input_root, output_root, sources = image_sources(tmp_path)
    executor = ManagedWorkerActionExecutor(
        (sys.executable, str(WORKER)), runtime_assets=(fake_asset(tmp_path),)
    )

    result = await executor.invoke(
        invocation(["second", "first"], sources),
        input_root=input_root,
        output_root=output_root,
    )

    assert result["status"] == "succeeded", result
    assert result["output"]["engine"] == {
        "name": "RapidOCR",
        "version": "3.9.1",
        "model": "PP-OCRv6-small",
        "provider": "CPUExecutionProvider",
    }
    assert [image["input_id"] for image in result["output"]["images"]] == [
        "second",
        "first",
    ]
    assert [line["text"] for line in result["output"]["images"][0]["lines"]] == ["page 1 primary"]
    assert result["output"]["images"][0]["lines"][0]["confidence"] == 0.98765
    assert result["output"]["total_lines"] == 2
    assert (output_root / "ocr.txt").is_file()
    assert (output_root / "ocr.json").is_file()
    assert [artifact["name"] for artifact in result["artifacts"]] == [
        "ocr.txt",
        "ocr.json",
    ]


@pytest.mark.asyncio
async def test_ocr_worker_rejects_unexpected_engine_identity_without_artifacts(
    tmp_path: Path,
) -> None:
    input_root, output_root, sources = image_sources(tmp_path)
    bad_source = sources[0]
    sources[0] = ("bad-engine", bad_source[1], bad_source[2])
    executor = ManagedWorkerActionExecutor(
        (sys.executable, str(WORKER)), runtime_assets=(fake_asset(tmp_path),)
    )

    result = await executor.invoke(
        invocation(["bad-engine"], sources),
        input_root=input_root,
        output_root=output_root,
    )

    assert result["status"] == "failed", result
    assert result["error"]["code"] == "engine_protocol_violation"
    assert result["artifacts"] == []
    assert not any(path.is_file() for path in output_root.rglob("*"))


@pytest.mark.asyncio
async def test_ocr_worker_reports_bounded_engine_failure_type(tmp_path: Path) -> None:
    input_root, output_root, sources = image_sources(tmp_path)
    failing_source = sources[0]
    sources[0] = ("engine-failure", failing_source[1], failing_source[2])
    executor = ManagedWorkerActionExecutor(
        (sys.executable, str(WORKER)), runtime_assets=(fake_asset(tmp_path),)
    )

    result = await executor.invoke(
        invocation(["engine-failure"], sources),
        input_root=input_root,
        output_root=output_root,
    )

    assert result["status"] == "failed", result
    assert result["error"] == {
        "code": "ocr_failed",
        "message": (
            "OCR engine could not process the selected images (ImportError: onnxruntime.capi)"
        ),
        "retryable": False,
    }
    assert result["artifacts"] == []


@pytest.mark.asyncio
async def test_ocr_worker_reports_silent_engine_exit_code(tmp_path: Path) -> None:
    input_root, output_root, sources = image_sources(tmp_path)
    failing_source = sources[0]
    sources[0] = ("native-failure", failing_source[1], failing_source[2])
    executor = ManagedWorkerActionExecutor(
        (sys.executable, str(WORKER)), runtime_assets=(fake_asset(tmp_path),)
    )

    result = await executor.invoke(
        invocation(["native-failure"], sources),
        input_root=input_root,
        output_root=output_root,
    )

    assert result["status"] == "failed", result
    assert result["error"]["message"] == (
        "OCR engine exited 0x00000009; it could not process the selected images"
    )
    assert result["artifacts"] == []


@pytest.mark.asyncio
async def test_ocr_worker_applies_global_character_limit_deterministically(
    tmp_path: Path,
) -> None:
    input_root, output_root, sources = image_sources(tmp_path)
    executor = ManagedWorkerActionExecutor(
        (sys.executable, str(WORKER)), runtime_assets=(fake_asset(tmp_path),)
    )
    request = invocation(["first", "second"], sources)
    arguments = request["arguments"]
    assert isinstance(arguments, dict)
    arguments["max_characters"] = 5
    arguments["include_text_artifact"] = False
    arguments["include_json_artifact"] = False

    result = await executor.invoke(
        request,
        input_root=input_root,
        output_root=output_root,
    )

    assert result["status"] == "succeeded", result
    assert result["output"]["images"][0]["full_text"] == "page "
    assert result["output"]["images"][0]["truncated"] is True
    assert result["output"]["images"][1]["lines"] == []
    assert result["output"]["images"][1]["truncated"] is True
    assert result["output"]["total_characters"] == 5
    assert result["artifacts"] == []
