from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import httpx

from shejane_runtime.auth import LOCAL_OWNER_PRINCIPAL_ID
from shejane_runtime.failure_policy import classify_failure_payload
from shejane_runtime.store.sqlite import LocalStore


async def test_image_generate_creates_a_file_artifact_without_returning_base64(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from shejane_runtime.tools import image as image_tools
    from shejane_runtime.tools import image_provider

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAEUlEQVR42mP8z4AdMDEMKQkA"
        "zUEBD7t4NqoAAAAASUVORK5CYII="
    )
    source_path = tmp_path / "uploaded.png"
    source_path.write_bytes(png)
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/v1/images/edits":
            assert b'filename="uploaded.png"' in request.content
            assert png in request.content
            return httpx.Response(
                200,
                json={"data": [{"b64_json": base64.b64encode(png).decode()}]},
            )
        payload = json.loads(request.content)
        if payload["prompt"] == "fail":
            return httpx.Response(
                500,
                json={
                    "request_id": "tuzi-request-123",
                    "error": {
                        "code": "get_channel_failed",
                        "message": "模型 gpt-image-2 的可用渠道不存在",
                    },
                },
            )
        assert request.url.path == "/v1/images/generations"
        assert payload["model"] == "image-model"
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png).decode()}]})

    class PatchedClient(httpx.AsyncClient):
        def __init__(self, **kwargs):
            kwargs.pop("trust_env", None)
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(image_provider.httpx, "AsyncClient", PatchedClient)
    monkeypatch.setattr(
        image_tools, "get_model_api_key", lambda *_args, **_kwargs: _async("secret")
    )

    store = await LocalStore.open(tmp_path / "local.db")
    try:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="draw",
            workspace_path=None,
        )
        await store.create_model_connection(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            connection_id="conn-image",
            preset_id="custom",
            name="Images",
            region="custom",
            adapter_id="openai_chat",
            base_url="https://images.example",
            requires_api_key=True,
            credential_ref="keyring:model-service:conn-image",
            models=[
                {
                    "model_id": "image-model",
                    "display_name": "Image Model",
                    "source": "manual",
                    "capabilities": [
                        {
                            "capability": "image_generation",
                            "protocol": "openai_images_generations",
                            "verification": "verified",
                        }
                    ],
                }
            ],
            catalog_status="ready",
        )
        context = SimpleNamespace(
            store=store,
            run_id=run["id"],
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            capability_bindings={
                "image_generation": {
                    "capability": "image_generation",
                    "connection_id": "conn-image",
                    "connection_version": 1,
                    "base_url": "https://images.example",
                    "credential_ref": "keyring:model-service:conn-image",
                    "model_id": "image-model",
                    "protocol": "openai_images_generations",
                    "revision": 1,
                }
            },
            plugin_inputs=(
                {
                    "virtual_path": "/attachments/uploaded.png",
                    "source_path": str(source_path),
                    "media_type": "image/png",
                },
            ),
        )
        generate = next(
            tool for tool in image_tools.make_image_tools() if tool.name == "image.generate"
        )

        result = await generate.coroutine("a red stone", runtime=SimpleNamespace(context=context))

        assert result["ok"] == "true"
        assert "b64_json" not in json.dumps(result)
        assert result["artifacts"][0]["media_type"] == "image/png"
        artifact = await store.get_artifact(result["artifacts"][0]["artifact_id"])
        assert artifact is not None
        assert artifact["storage_kind"] == "blob"
        assert store.artifact_body_path(artifact).read_bytes() == png

        referenced = await generate.coroutine(
            "use the uploaded style",
            runtime=SimpleNamespace(context=context),
            source_attachment_path="/attachments/uploaded.png",
        )
        assert referenced["ok"] == "true"
        assert requested_paths[-1] == "/v1/images/edits"

        rejected = await generate.coroutine(
            "read an arbitrary path",
            runtime=SimpleNamespace(context=context),
            source_attachment_path=str(source_path),
        )
        assert rejected["error_code"] == "image_source_not_found"

        failed = await generate.coroutine("fail", runtime=SimpleNamespace(context=context))
        assert failed["error_code"] == "image_model_unavailable"
        assert failed["request_id"] == "tuzi-request-123"
        assert classify_failure_payload("tool.failed", failed)["category"] == "configuration"
    finally:
        await store.close()


async def _async(value):
    return value
