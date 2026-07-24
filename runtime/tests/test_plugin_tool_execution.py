from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import sys
import zipfile
from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import AIMessage, ToolMessage
from PIL import Image

from shejane_runtime.agent import builder
from shejane_runtime.agent.context_builder import RuntimeContext
from shejane_runtime.auth import LOCAL_OWNER_PRINCIPAL_ID
from shejane_runtime.config import reset_settings_for_tests
from shejane_runtime.middleware.tool_execution import (
    ToolExecutionMiddleware,
    tool_version_for_invocation,
)
from shejane_runtime.plugins.catalog import PluginCatalog
from shejane_runtime.plugins.executor import ManagedWorkerActionExecutor
from shejane_runtime.plugins.identity import (
    plugin_action_catalog_hash,
    plugin_action_tool_version,
)
from shejane_runtime.plugins.manifest import load_plugin_manifest
from shejane_runtime.plugins.package import canonical_package_digest
from shejane_runtime.plugins.tools import PluginActionError, PluginToolAdapter
from shejane_runtime.server import create_app
from shejane_runtime.store.sqlite import LocalStore
from shejane_runtime.tools.runtime import RuntimeToolExecution, current_runtime_tool_execution
from tests.helpers import run_command
from tests.test_e2e_capabilities import RecordingHandler, _parse_sse, _patched_async_client, _sse

REPO_ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_FIXTURE = REPO_ROOT / "runtime" / "plugins" / "fixtures" / "wasi-archive"


def _install_fixture(data_dir: Path) -> dict[str, object]:
    digest = canonical_package_digest(ARCHIVE_FIXTURE)
    root = data_dir / "plugins" / "packages" / digest.removeprefix("sha256:")
    root.parent.mkdir(parents=True)
    shutil.copytree(ARCHIVE_FIXTURE, root)
    manifest = load_plugin_manifest(root).model_dump(mode="json")
    return {
        "run_id": "run_plugin_action",
        "plugin_id": manifest["id"],
        "version": manifest["version"],
        "digest": digest,
        "selection_source": "explicit",
        "required": True,
        "command_id": None,
        "action_catalog_hash": plugin_action_catalog_hash(
            manifest,
            plugin_digest=digest,
        ),
    }


def _pack_fixture(destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(ARCHIVE_FIXTURE.rglob("*")):
            if path.is_file() and "target" not in path.parts:
                archive.write(path, path.relative_to(ARCHIVE_FIXTURE).as_posix())


@pytest.mark.asyncio
async def test_plugin_adapter_maps_worker_progress_to_custom_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    run = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="inspect archive",
        workspace_path=None,
    )
    source = tmp_path / "source.zip"
    source.write_bytes(b"fixture")
    binding = _install_fixture(tmp_path)
    streamed: list[dict[str, object]] = []
    monkeypatch.setattr("shejane_runtime.plugins.tools.get_stream_writer", lambda: streamed.append)

    class ProgressExecutor:
        async def invoke(self, invocation, *, input_root, output_root, on_progress=None):
            assert on_progress is not None
            on_progress(
                {
                    "schema_version": 1,
                    "invocation_id": invocation["invocation_id"],
                    "operation_id": invocation["operation_id"],
                    "sequence": 1,
                    "phase": "inspect",
                }
            )
            return {
                "schema_version": 1,
                "invocation_id": invocation["invocation_id"],
                "operation_id": invocation["operation_id"],
                "status": "succeeded",
                "output": {"file_count": 0},
                "artifacts": [],
            }

    try:
        async with PluginCatalog(tmp_path).acquire_snapshot(
            [binding], execution_context=object()
        ) as lease:
            action = replace(
                lease.actions[0],
                runtime_assets=(
                    SimpleNamespace(
                        asset_id="org.example.engine",
                        version="1.2.3",
                        digest="sha256:" + "e" * 64,
                        platform="darwin/arm64",
                    ),
                ),
            )
            context = RuntimeContext(
                store=store,
                run_id=str(run["id"]),
                plugin_inputs=(
                    {
                        "id": "source",
                        "path": "/input/source/source.zip",
                        "media_type": "application/zip",
                        "size_bytes": source.stat().st_size,
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "source_path": str(source),
                    },
                ),
            )
            adapter = PluginToolAdapter(executor_factory=lambda _action: ProgressExecutor())
            result = await adapter.invoke(
                action,
                {"input_id": "source"},
                RuntimeToolExecution(
                    context=context,
                    operation_id="toolop_progress",
                    tool_call_id="call_progress",
                ),
            )
    finally:
        await store.close()

    assert len(streamed) == 1
    assert streamed[0]["event"] == "tool.progress"
    assert streamed[0]["data"] == {
        "schema_version": 1,
        "invocation_id": streamed[0]["data"]["invocation_id"],
        "operation_id": "toolop_progress",
        "sequence": 1,
        "phase": "inspect",
        "tool_call_id": "call_progress",
        "tool": "plugin.dev.shejane.fixture.archive.archive.extract",
    }
    assert result["provenance"]["runtime_assets"] == [
        {
            "id": "org.example.engine",
            "version": "1.2.3",
            "digest": "sha256:" + "e" * 64,
            "platform": "darwin/arm64",
        }
    ]


@pytest.mark.asyncio
async def test_builtin_plugin_adapter_returns_screenshot_as_model_content(
    tmp_path: Path,
) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    run = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="observe desktop",
        workspace_path=None,
    )
    binding = _install_fixture(tmp_path)

    class ScreenshotExecutor:
        async def invoke(self, invocation, *, input_root, output_root, on_progress=None):
            return {
                "schema_version": 1,
                "invocation_id": invocation["invocation_id"],
                "operation_id": invocation["operation_id"],
                "status": "succeeded",
                "output": {
                    "text": "Desktop state s1 with one button",
                    "images": [{"base64": "cG5n", "mime_type": "image/png"}],
                },
                "artifacts": [],
            }

    try:
        async with PluginCatalog(tmp_path).acquire_snapshot(
            [binding], execution_context=object()
        ) as lease:
            action = replace(
                lease.actions[0],
                execution_kind="builtin",
                input_schema={
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "additionalProperties": False,
                },
                output_schema={
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["text", "images"],
                    "properties": {
                        "text": {"type": "string"},
                        "images": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["base64", "mime_type"],
                                "properties": {
                                    "base64": {"type": "string"},
                                    "mime_type": {"const": "image/png"},
                                },
                            },
                        },
                    },
                },
                consumes=(),
                capabilities=(),
            )
            context = RuntimeContext(
                store=store,
                run_id=str(run["id"]),
                plugin_inputs=(),
            )
            result = await PluginToolAdapter(
                executor_factory=lambda _action: ScreenshotExecutor()
            ).invoke(
                action,
                {},
                RuntimeToolExecution(
                    context=context,
                    operation_id="toolop_screenshot",
                    tool_call_id="call_screenshot",
                ),
            )
    finally:
        await store.close()

    assert result[0]["type"] == "text"
    text_result = json.loads(result[0]["text"])
    assert text_result["output"] == {"text": "Desktop state s1 with one button"}
    assert result[1] == {"type": "image", "base64": "cG5n", "mime_type": "image/png"}


@pytest.mark.asyncio
async def test_plugin_adapter_uses_frozen_vision_binding_without_exposing_credentials(
    tmp_path: Path,
) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    run = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="describe image",
        workspace_path=None,
    )
    source = tmp_path / "image.png"
    source.write_bytes(b"png")
    binding = _install_fixture(tmp_path)
    binding["model_binding"] = {
        "id": "vision-default",
        "requested_model": "local:vision:vision-a",
        "adapter_id": "openai_chat",
        "connection_id": "vision",
        "connection_version": 1,
        "base_url": "https://provider.invalid/v1",
        "credential_ref": "keyring:model-service:vision:revision",
        "model_id": "vision-a",
        "profile": {"image_inputs": True},
    }
    script = r"""
import json
import sys

def send(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)

initialize = json.loads(sys.stdin.readline())
send({"jsonrpc":"2.0","id":initialize["id"],"result":{"protocol_version":1,"process_isolated":True,"access_isolated":False,"resource_isolated":False,"sandboxed":False}})
invoke = json.loads(sys.stdin.readline())
params = invoke["params"]
send({"jsonrpc":"2.0","id":"worker:vision:1","method":"model/vision/invoke","params":{"model_binding_id":"vision-default","input_ids":["image"],"task":"describe","prompt":"Describe.","max_output_tokens":64}})
json.loads(sys.stdin.readline())
send({"jsonrpc":"2.0","id":invoke["id"],"result":{"schema_version":1,"invocation_id":params["invocation_id"],"operation_id":params["operation_id"],"status":"succeeded","output":{"file_count":0},"artifacts":[]}})
shutdown = json.loads(sys.stdin.readline())
send({"jsonrpc":"2.0","id":shutdown["id"],"result":{}})
"""
    observed: list[tuple[dict[str, object], dict[str, object]]] = []

    async def invoke_vision(model_binding, params, input_root, inputs):
        observed.append((dict(model_binding), dict(params)))
        assert inputs[0]["path"] == "/input/image/image.png"
        assert (input_root / "image" / "image.png").read_bytes() == b"png"
        return {"text": "A lantern.", "usage": {"input_tokens": 12, "output_tokens": 3}}

    try:
        async with PluginCatalog(tmp_path).acquire_snapshot(
            [binding], execution_context=object()
        ) as lease:
            action = replace(
                lease.actions[0],
                execution_kind="managed_worker",
                consumes=("image/png",),
                capabilities=("input.read", "model.vision.invoke"),
                input_schema={
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["input_id"],
                    "properties": {"input_id": {"type": "string", "minLength": 1}},
                },
            )
            context = RuntimeContext(
                store=store,
                run_id=str(run["id"]),
                plugin_inputs=(
                    {
                        "id": "image",
                        "path": "/input/image/image.png",
                        "media_type": "image/png",
                        "size_bytes": 3,
                        "sha256": hashlib.sha256(b"png").hexdigest(),
                        "source_path": str(source),
                    },
                ),
            )
            adapter = PluginToolAdapter(
                executor_factory=lambda _action: ManagedWorkerActionExecutor(
                    (sys.executable, "-c", script)
                ),
                vision_invoker=invoke_vision,
            )
            result = await adapter.invoke(
                action,
                {"input_id": "image"},
                RuntimeToolExecution(
                    context=context,
                    operation_id="toolop_vision",
                    tool_call_id="call_vision",
                ),
            )
    finally:
        await store.close()

    assert observed[0][0]["model_id"] == "vision-a"
    assert observed[0][1]["model_binding_id"] == "vision-default"
    assert result["provenance"]["model"] == {
        "backend": "cloud",
        "binding_id": "vision-default",
        "connection_id": "vision",
        "connection_version": 1,
        "model_id": "vision-a",
        "usage": {"input_tokens": 12, "output_tokens": 3},
    }
    assert "credential" not in json.dumps(result["provenance"])


@pytest.mark.asyncio
async def test_vision_service_call_uses_authorized_image_and_redacts_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = reset_settings_for_tests(data_dir=tmp_path)
    store = await LocalStore.open(tmp_path / "runtime.db")
    await store.create_model_connection(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        connection_id="vision",
        preset_id="custom",
        name="Vision",
        region="custom",
        adapter_id="openai_chat",
        base_url="https://provider.invalid/v1",
        requires_api_key=True,
        credential_ref="keyring:model-service:vision:revision",
        models=[
            {
                "model_id": "vision-a",
                "display_name": "Vision A",
                "source": "manual",
                "verification": "verified",
                "recommended": False,
                "tool_calling": True,
                "streaming": True,
                "image_inputs": True,
                "max_output_tokens": 512,
            }
        ],
        catalog_status="ready",
    )
    input_root = tmp_path / "input"
    image_path = input_root / "image" / "image.png"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (2, 2), "white").save(image_path)
    captured: dict[str, object] = {}

    class FakeVisionModel:
        def bind(self, **kwargs):
            captured["options"] = kwargs
            return self

        async def ainvoke(self, messages):
            captured["messages"] = messages
            return AIMessage(
                content="A white square.",
                usage_metadata={"input_tokens": 12, "output_tokens": 4, "total_tokens": 16},
            )

    async def get_key(*_args, **_kwargs):
        return "secret-token"

    monkeypatch.setattr(builder, "get_model_api_key", get_key)
    monkeypatch.setattr(builder, "_build_chat_model", lambda *_args, **_kwargs: FakeVisionModel())
    binding = {
        "id": "vision-default",
        "requested_model": "local:vision:vision-a",
        "adapter_id": "openai_chat",
        "connection_id": "vision",
        "connection_version": 1,
        "base_url": "https://provider.invalid/v1",
        "requires_api_key": True,
        "credential_ref": "keyring:model-service:vision:revision",
        "model_id": "vision-a",
        "profile": {
            "model_id": "vision-a",
            "display_name": "Vision A",
            "source": "manual",
            "verification": "verified",
            "recommended": False,
            "tool_calling": True,
            "streaming": True,
            "image_inputs": True,
            "max_output_tokens": 512,
        },
    }
    public_input = {
        "id": "image",
        "path": "/input/image/image.png",
        "media_type": "image/png",
        "size_bytes": image_path.stat().st_size,
        "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
    }

    try:
        result = await builder._invoke_plugin_vision(
            binding,
            {
                "model_binding_id": "vision-default",
                "input_ids": ["image"],
                "task": "describe",
                "prompt": "Do not repeat secret-token.",
                "max_output_tokens": 64,
                "temperature": 0,
                "detail": "low",
            },
            input_root,
            (public_input,),
            store=store,
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            settings=settings,
        )
    finally:
        await store.close()

    messages = captured["messages"]
    assert "secret-token" not in str(messages)
    assert "[REDACTED_CREDENTIAL]" in str(messages)
    assert captured["options"] == {"max_tokens": 64, "temperature": 0}
    assert result == {
        "text": "A white square.",
        "model": {"connection_id": "vision", "connection_version": 1, "model_id": "vision-a"},
        "usage": {"input_tokens": 12, "output_tokens": 4, "total_tokens": 16},
    }


@pytest.mark.asyncio
async def test_plugin_adapter_binds_same_run_file_artifact_as_immutable_input(
    tmp_path: Path,
) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    run = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="chain plugin artifacts",
        workspace_path=None,
    )
    source = tmp_path / "prior-output.zip"
    source.write_bytes(b"immutable artifact input")
    artifact = await store.create_file_artifact(
        run_id=str(run["id"]),
        kind="plugin_output",
        title="frames.zip",
        source_path=source,
        content_type="application/zip",
    )
    binding = _install_fixture(tmp_path)

    class RecordingExecutor:
        async def invoke(self, invocation, *, input_root, output_root, on_progress=None):
            assert invocation["inputs"] == [
                {
                    "id": artifact["id"],
                    "path": f"/input/artifacts/{artifact['id']}/artifact.zip",
                    "media_type": "application/zip",
                    "size_bytes": len(b"immutable artifact input"),
                    "sha256": hashlib.sha256(b"immutable artifact input").hexdigest(),
                }
            ]
            materialized = input_root / "artifacts" / artifact["id"] / "artifact.zip"
            assert materialized.read_bytes() == b"immutable artifact input"
            return {
                "schema_version": 1,
                "invocation_id": invocation["invocation_id"],
                "operation_id": invocation["operation_id"],
                "status": "succeeded",
                "output": {"file_count": 0},
                "artifacts": [],
            }

    try:
        async with PluginCatalog(tmp_path).acquire_snapshot(
            [binding], execution_context=object()
        ) as lease:
            action = replace(
                lease.actions[0],
                input_schema={
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["input_id"],
                    "properties": {"input_id": {"type": "string", "minLength": 1}},
                },
            )
            context = RuntimeContext(store=store, run_id=str(run["id"]), plugin_inputs=())
            adapter = PluginToolAdapter(executor_factory=lambda _action: RecordingExecutor())

            result = await adapter.invoke(
                action,
                {"input_id": artifact["id"]},
                RuntimeToolExecution(
                    context=context,
                    operation_id="toolop_artifact_chain",
                    tool_call_id="call_artifact_chain",
                ),
            )

        assert result["status"] == "succeeded"
        assert result["provenance"]["inputs"][0]["id"] == artifact["id"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_plugin_adapter_rejects_artifact_from_another_run(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    first = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="produce artifact",
        workspace_path=None,
    )
    second = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="attempt cross-run read",
        workspace_path=None,
    )
    source = tmp_path / "private.zip"
    source.write_bytes(b"private")
    artifact = await store.create_file_artifact(
        run_id=str(first["id"]),
        kind="plugin_output",
        title="private.zip",
        source_path=source,
        content_type="application/zip",
    )
    binding = _install_fixture(tmp_path)

    try:
        async with PluginCatalog(tmp_path).acquire_snapshot(
            [binding], execution_context=object()
        ) as lease:
            action = replace(
                lease.actions[0],
                input_schema={
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["input_id"],
                    "properties": {"input_id": {"type": "string", "minLength": 1}},
                },
            )
            context = RuntimeContext(store=store, run_id=str(second["id"]), plugin_inputs=())
            adapter = PluginToolAdapter(
                executor_factory=lambda _action: (_ for _ in ()).throw(
                    AssertionError("cross-run artifact reached the executor")
                )
            )

            with pytest.raises(PluginActionError, match="compatible attachment"):
                await adapter.invoke(
                    action,
                    {"input_id": artifact["id"]},
                    RuntimeToolExecution(
                        context=context,
                        operation_id="toolop_cross_run_artifact",
                        tool_call_id="call_cross_run_artifact",
                    ),
                )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_plugin_adapter_binds_ordered_same_run_artifact_batch(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    run = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="chain artifact batch",
        workspace_path=None,
    )
    artifacts = []
    for index, body in enumerate((b"first archive", b"second archive"), start=1):
        source = tmp_path / f"page-{index}.zip"
        source.write_bytes(body)
        artifacts.append(
            await store.create_file_artifact(
                run_id=str(run["id"]),
                kind="plugin_output",
                title=f"page-{index}.zip",
                source_path=source,
                content_type="application/zip",
            )
        )
    binding = _install_fixture(tmp_path)

    class BatchExecutor:
        async def invoke(self, invocation, *, input_root, output_root, on_progress=None):
            assert [item["id"] for item in invocation["inputs"]] == [
                artifacts[1]["id"],
                artifacts[0]["id"],
            ]
            assert [
                (input_root / PurePosixPath(item["path"]).relative_to("/input")).read_bytes()
                for item in invocation["inputs"]
            ] == [b"second archive", b"first archive"]
            return {
                "schema_version": 1,
                "invocation_id": invocation["invocation_id"],
                "operation_id": invocation["operation_id"],
                "status": "succeeded",
                "output": {"file_count": 0},
                "artifacts": [],
            }

    try:
        async with PluginCatalog(tmp_path).acquire_snapshot(
            [binding], execution_context=object()
        ) as lease:
            action = replace(
                lease.actions[0],
                input_schema={
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["input_ids"],
                    "properties": {
                        "input_ids": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                            "minItems": 1,
                            "maxItems": 16,
                            "uniqueItems": True,
                        }
                    },
                },
            )
            context = RuntimeContext(store=store, run_id=str(run["id"]), plugin_inputs=())
            adapter = PluginToolAdapter(executor_factory=lambda _action: BatchExecutor())

            result = await adapter.invoke(
                action,
                {"input_ids": [artifacts[1]["id"], artifacts[0]["id"]]},
                RuntimeToolExecution(
                    context=context,
                    operation_id="toolop_artifact_batch",
                    tool_call_id="call_artifact_batch",
                ),
            )
    finally:
        await store.close()

    assert result["status"] == "succeeded"
    assert [item["id"] for item in result["provenance"]["inputs"]] == [
        artifacts[1]["id"],
        artifacts[0]["id"],
    ]


@pytest.mark.asyncio
async def test_plugin_artifact_input_digest_strengthens_receipt_tool_version(
    tmp_path: Path,
) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    run = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="bind artifact digest",
        workspace_path=None,
    )
    source = tmp_path / "frame.png"
    source.write_bytes(b"png artifact bytes")
    artifact = await store.create_file_artifact(
        run_id=str(run["id"]),
        kind="plugin_output",
        title="frame.png",
        source_path=source,
        content_type="image/png",
    )
    context = RuntimeContext(store=store, run_id=str(run["id"]), plugin_inputs=())
    context.plugin_tool_versions["plugin.example.ocr"] = "plugin-action-v1:sha256:base"

    try:
        first = await tool_version_for_invocation(
            context,
            "plugin.example.ocr",
            {"input_id": artifact["id"]},
        )
        second = await tool_version_for_invocation(
            context,
            "plugin.example.ocr",
            {"input_id": artifact["id"]},
        )
        batched = await tool_version_for_invocation(
            context,
            "plugin.example.ocr",
            {"input_ids": [artifact["id"]]},
        )
    finally:
        await store.close()

    assert first == second
    assert first.startswith("plugin-action-v1:sha256:base:artifact-input:sha256:")
    assert first != "plugin-action-v1:sha256:base"

    assert batched.startswith("plugin-action-v1:sha256:base:artifact-input:sha256:")
    assert batched != "plugin-action-v1:sha256:base"


@pytest.mark.asyncio
async def test_wasi_action_uses_existing_receipt_and_persists_runtime_artifact(
    tmp_path: Path,
) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    run = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="extract archive",
        workspace_path=None,
    )
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("readme.txt", "plugin receipt replay\n")
    archive_bytes = archive_buffer.getvalue()
    source = tmp_path / "source.zip"
    source.write_bytes(archive_bytes)
    binding = _install_fixture(tmp_path)
    calls = 0

    try:
        async with PluginCatalog(tmp_path).acquire_snapshot(
            [binding],
            execution_context=object(),
        ) as lease:
            action = lease.actions[0]
            public_input = {
                "id": "source",
                "path": "/input/source/source.zip",
                "media_type": "application/zip",
                "size_bytes": len(archive_bytes),
                "sha256": hashlib.sha256(archive_bytes).hexdigest(),
            }
            context = RuntimeContext(
                store=store,
                run_id=str(run["id"]),
                execution_attempt_id="job-plugin:1",
                plugin_inputs=({**public_input, "source_path": str(source)},),
            )
            identity = {
                "action": {
                    "plugin_id": action.plugin_id,
                    "plugin_version": action.plugin_version,
                    "plugin_digest": action.plugin_digest,
                    "action_id": action.action_id,
                },
                "inputs": [public_input],
                "grants": {"capabilities": list(action.capabilities)},
                "limits": dict(action.limits),
                "environment": {"locale": "en-US", "timezone": "UTC"},
            }
            context.plugin_tool_versions[action.tool_name] = plugin_action_tool_version(
                identity,
                action_schema_digest=action.action_schema_digest,
            )
            request = ToolCallRequest(
                tool_call={
                    "id": "call-plugin-1",
                    "name": action.tool_name,
                    "args": {"input_id": "source"},
                    "type": "tool_call",
                },
                tool=None,
                state={"messages": []},
                runtime=SimpleNamespace(context=context, config={}),
            )
            adapter = PluginToolAdapter()

            async def handler(active_request: ToolCallRequest) -> ToolMessage:
                nonlocal calls
                calls += 1
                result = await adapter.invoke(
                    action,
                    dict(active_request.tool_call["args"]),
                    current_runtime_tool_execution(),
                )
                return ToolMessage(
                    content=json.dumps(result, ensure_ascii=False),
                    name=action.tool_name,
                    tool_call_id=active_request.tool_call["id"],
                )

            middleware = ToolExecutionMiddleware()
            first = await middleware.awrap_tool_call(request, handler)
            second = await middleware.awrap_tool_call(request, handler)

        assert first.content == second.content
        result = json.loads(str(first.content))
        assert result["provenance"]["plugin"] == {
            "id": action.plugin_id,
            "version": action.plugin_version,
            "digest": action.plugin_digest,
        }
        assert result["provenance"]["action_id"] == action.action_id
        assert result["provenance"]["operation_id"].startswith("toolop_")
        assert result["provenance"]["inputs"] == [public_input]
        assert result["provenance"]["parameters"] == {"input_id": "source"}
        assert calls == 1
        receipts = await store.list_tool_receipts_for_run(str(run["id"]))
        assert len(receipts) == 1
        assert receipts[0]["status"] == "completed"
        assert receipts[0]["risk"] == "plugin_action"
        artifacts = await store.list_artifacts_for_run(str(run["id"]))
        assert len(artifacts) == 1
        assert artifacts[0]["kind"] == "plugin_output"
        assert artifacts[0]["metadata"]["plugin_digest"] == binding["digest"]
        assert artifacts[0]["metadata"]["provenance"] == result["provenance"]
        assert artifacts[0]["storage_kind"] == "blob"
        assert artifacts[0]["content"] == ""
        assert store.artifact_body_path(artifacts[0]).read_bytes() == b"plugin receipt replay\n"
        assert list((tmp_path / "plugins" / "executions").iterdir()) == []
    finally:
        await store.close()


def test_agent_executes_plugin_structured_tool_with_injected_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_name = "plugin.dev.shejane.fixture.archive.archive.extract"
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "_", tool_name).strip("_")
    provider_wire_tool_name = stem
    legacy_wire_tool_name = f"{stem[:55]}_{hashlib.sha256(tool_name.encode()).hexdigest()[:8]}"

    class ArtifactAwareHandler(RecordingHandler):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            body = json.loads(request.read())
            self.requests.append(body)
            if len(self.requests) == 1:
                return _sse(
                    [
                        (
                            "llm.tool_call",
                            {
                                "id": "call_plugin_e2e",
                                "name": legacy_wire_tool_name,
                                "arguments": {"input_id": "source"},
                            },
                        ),
                        ("llm.done", {"request_id": "r1", "finish_reason": "tool_calls"}),
                    ]
                )
            prompt = next(item["content"] for item in body["messages"] if item["role"] == "system")
            if "<runtime-artifact-delivery>" in prompt or len(self.requests) > 2:
                return _sse(
                    [
                        ("llm.delta", {"content_delta": "Archive extracted."}),
                        ("llm.done", {"request_id": "r3", "finish_reason": "stop"}),
                    ]
                )
            return _sse(
                [
                    (
                        "llm.tool_call",
                        {
                            "id": "call_plugin_e2e_retry",
                            "name": legacy_wire_tool_name,
                            "arguments": {"input_id": "source"},
                        },
                    ),
                    ("llm.done", {"request_id": "r2", "finish_reason": "tool_calls"}),
                ]
            )

    handler = ArtifactAwareHandler(scripts=[])
    monkeypatch.setattr(
        "tests.streaming_model.httpx.AsyncClient",
        _patched_async_client(handler),
    )
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path / "runtime",
    )
    plugin_package = tmp_path / "archive.shejane-plugin"
    _pack_fixture(plugin_package)
    input_package = tmp_path / "input.zip"
    with zipfile.ZipFile(input_package, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("inside.txt", "real graph plugin tool\n")

    with TestClient(create_app(settings)) as client:
        installed = client.post(
            "/v1/commands",
            headers={"Authorization": "Bearer tok"},
            json={
                "type": "plugin.install",
                "command_id": "cmd_install_plugin_e2e",
                "source_path": str(plugin_package),
                "allow_unsigned": True,
            },
        ).json()
        enabled = client.post(
            "/v1/commands",
            headers={"Authorization": "Bearer tok"},
            json={
                "type": "plugin.enable",
                "command_id": "cmd_enable_plugin_e2e",
                "plugin_id": "dev.shejane.fixture.archive",
                "expected_digest": installed["digest"],
            },
        )
        assert enabled.status_code == 200, enabled.text
        response = client.post(
            "/v1/runs",
            headers={"Authorization": "Bearer tok"},
            json=run_command(
                "extract the attached archive",
                attachment_paths=[str(input_package)],
                permission_mode="auto",
                plugin_refs=[
                    {
                        "plugin_id": "dev.shejane.fixture.archive",
                        "expected_digest": installed["digest"],
                    }
                ],
            ),
        )
        assert response.status_code == 200, response.text
        run_id = response.json()["id"]
        with client.stream(
            "GET",
            f"/v1/runs/{run_id}/stream",
            headers={"Authorization": "Bearer tok"},
        ) as stream:
            events = _parse_sse(stream.read().decode("utf-8"))
        receipts = client.portal.call(client.app.state.store.list_tool_receipts_for_run, run_id)
        artifacts = client.portal.call(client.app.state.store.list_artifacts_for_run, run_id)
        artifact_body = client.app.state.store.artifact_body_path(artifacts[0]).read_bytes()

    assert any(name == "run.completed" for name, _payload in events), json.dumps(receipts)
    assert any(name == "tool.completed" for name, _payload in events)
    assert not any(name == "permission.required" for name, _payload in events)
    auto_approved = next(payload for name, payload in events if name == "permission.auto_approved")
    assert auto_approved["source"] == "rule"
    plugin_receipt = next(receipt for receipt in receipts if receipt["risk"] == "plugin_action")
    assert plugin_receipt["review_decision"] == "allow"
    assert plugin_receipt["review_source"] == "rule"
    assert len(artifacts) == 1
    assert artifact_body == b"real graph plugin tool\n"
    advertised_names = [
        tool.get("function", {}).get("name")
        for tool in handler.requests[0]["tools"]
        if isinstance(tool, dict)
    ]
    assert provider_wire_tool_name in advertised_names
