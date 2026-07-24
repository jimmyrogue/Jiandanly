from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

from shejane_runtime.plugins.executor import ManagedWorkerActionExecutor
from shejane_runtime.plugins.macos_vm import load_macos_vm_resources
from shejane_runtime.plugins.runtime_assets import RuntimeAssetHandle

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER = REPO_ROOT / "runtime" / "plugins" / "vision" / "worker" / "vision_worker.py"


def cloud_executor(invoke_vision) -> ManagedWorkerActionExecutor:
    frozen = os.environ.get("SHEJANE_TEST_VISION_WORKER")
    command = (frozen,) if frozen else (sys.executable, str(WORKER))
    vm_manifest = os.environ.get("SHEJANE_TEST_MACOS_VM_ASSETS")
    return ManagedWorkerActionExecutor(
        command,
        vision_handler=invoke_vision,
        vm_resources=(
            load_macos_vm_resources(Path(vm_manifest).resolve(strict=True)) if vm_manifest else None
        ),
        package_root=Path(command[0]).resolve(strict=True).parent if vm_manifest else None,
    )


def executable(path: Path, source: str) -> None:
    path.write_text("#!/usr/bin/env python3\n" + source, encoding="utf-8")
    path.chmod(0o500)


def fake_local_asset(tmp_path: Path) -> RuntimeAssetHandle:
    root = tmp_path / "vision-asset"
    payload = root / "payload"
    engine = payload / "bin" / "vision-engine"
    engine.parent.mkdir(parents=True)
    executable(
        engine,
        """import json, pathlib, sys
request = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
response = {
    "text": f"Local view of {len(request['inputs'])} image(s): a paper lantern.",
    "model_id": "smolvlm2-500m-video-instruct-q8_0-test",
    "usage": {"input_tokens": 24, "output_tokens": 9, "total_tokens": 33},
    "warnings": ["Conformance fixture, not a production quality claim."],
}
pathlib.Path(sys.argv[2]).write_text(json.dumps(response), encoding="utf-8")
""",
    )
    sbom = root / "sbom.json"
    sbom.write_text("{}", encoding="utf-8")
    return RuntimeAssetHandle(
        asset_id="org.llama-mtmd.runtime",
        version="0.1.0-smolvlm2-test",
        platform="darwin/arm64",
        digest="sha256:" + "a" * 64,
        root=root,
        payload=payload,
        license="Apache-2.0",
        source_url="https://github.com/ggml-org/llama.cpp",
        sbom=sbom,
    )


def staged_image(tmp_path: Path) -> tuple[Path, Path, Path]:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    source = input_root / "source" / "image.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"fake image")
    output_root.mkdir()
    return input_root, output_root, source


def invocation(source: Path, *, backend: str) -> dict[str, object]:
    body = source.read_bytes()
    plugin_id = "org.shejane.vision.local" if backend == "local" else "org.shejane.vision.cloud"
    capabilities = ["input.read", "artifact.write"]
    if backend == "cloud":
        capabilities.append("model.vision.invoke")
    return {
        "schema_version": 1,
        "invocation_id": "a23e4567-e89b-42d3-a456-426614174003",
        "operation_id": f"run_01:vision.analyze_images:{backend}",
        "action": {
            "plugin_id": plugin_id,
            "plugin_version": "0.1.0",
            "plugin_digest": "sha256:" + "b" * 64,
            "action_id": "vision.analyze_images",
        },
        "arguments": {
            "input_ids": ["image"],
            "backend": backend,
            "task": "question",
            "prompt": "What is shown?",
            "max_output_tokens": 128,
            "temperature": 0,
            "detail": "low",
            "include_text_artifact": True,
            "include_json_artifact": True,
        },
        "inputs": [
            {
                "id": "image",
                "path": "/input/source/image.png",
                "media_type": "image/png",
                "size_bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        ],
        "grants": {"capabilities": capabilities},
        "limits": {"timeout_ms": 30_000, "memory_mb": 4096, "output_mb": 64},
        "environment": {"locale": "en-US", "timezone": "UTC"},
        **({"model_binding_id": "vision-default"} if backend == "cloud" else {}),
    }


@pytest.mark.asyncio
async def test_local_vision_worker_uses_exact_asset_and_writes_common_artifacts(
    tmp_path: Path,
) -> None:
    input_root, output_root, source = staged_image(tmp_path)
    executor = ManagedWorkerActionExecutor(
        (sys.executable, str(WORKER)),
        runtime_assets=(fake_local_asset(tmp_path),),
    )

    result = await executor.invoke(
        invocation(source, backend="local"),
        input_root=input_root,
        output_root=output_root,
    )

    assert result["status"] == "succeeded", result
    assert result["output"]["backend"] == "local"
    assert result["output"]["model"] == {
        "binding_id": "sha256:" + "a" * 64,
        "model_id": "smolvlm2-500m-video-instruct-q8_0-test",
        "runtime_asset_id": "org.llama-mtmd.runtime",
        "runtime_asset_version": "0.1.0-smolvlm2-test",
        "runtime_asset_digest": "sha256:" + "a" * 64,
    }
    assert [artifact["name"] for artifact in result["artifacts"]] == [
        "vision.txt",
        "vision.json",
    ]
    assert "paper lantern" in (output_root / "vision.txt").read_text(encoding="utf-8")
    assert not (output_root / ".runtime-tmp").exists()


@pytest.mark.asyncio
async def test_cloud_vision_worker_uses_frozen_host_binding_without_asset(
    tmp_path: Path,
) -> None:
    input_root, output_root, source = staged_image(tmp_path)
    calls: list[dict[str, object]] = []

    async def invoke_vision(params):
        calls.append(dict(params))
        return {
            "text": "A paper lantern.",
            "model": {
                "connection_id": "vision-provider",
                "connection_version": 3,
                "model_id": "vision-model",
            },
            "usage": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
        }

    result = await cloud_executor(invoke_vision).invoke(
        invocation(source, backend="cloud"),
        input_root=input_root,
        output_root=output_root,
    )

    assert result["status"] == "succeeded", result
    assert calls == [
        {
            "model_binding_id": "vision-default",
            "input_ids": ["image"],
            "task": "question",
            "prompt": "What is shown?",
            "max_output_tokens": 128,
            "temperature": 0,
            "detail": "low",
        }
    ]
    assert result["output"]["model"] == {
        "binding_id": "vision-default",
        "connection_id": "vision-provider",
        "connection_version": 3,
        "model_id": "vision-model",
    }
    assert result["output"]["warnings"] == [
        "Image content was processed by the configured remote provider."
    ]


@pytest.mark.asyncio
async def test_vision_worker_rejects_backend_substitution(tmp_path: Path) -> None:
    input_root, output_root, source = staged_image(tmp_path)
    request = invocation(source, backend="local")
    arguments = request["arguments"]
    assert isinstance(arguments, dict)
    arguments["backend"] = "cloud"

    result = await ManagedWorkerActionExecutor(
        (sys.executable, str(WORKER)),
        runtime_assets=(fake_local_asset(tmp_path),),
    ).invoke(request, input_root=input_root, output_root=output_root)

    assert result["status"] == "failed"
    assert result["error"]["code"] == "backend_mismatch"
    assert not any(path.is_file() for path in output_root.rglob("*"))
