from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import httpx

from shejane_runtime.auth import LOCAL_OWNER_PRINCIPAL_ID
from shejane_runtime.store.sqlite import LocalStore


async def test_image_generate_creates_a_file_artifact_without_returning_base64(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from shejane_runtime.tools import image as image_tools

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAEUlEQVR42mP8z4AdMDEMKQkA"
        "zUEBD7t4NqoAAAAASUVORK5CYII="
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/images/generations"
        assert json.loads(request.content)["model"] == "image-model"
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png).decode()}]})

    class PatchedClient(httpx.AsyncClient):
        def __init__(self, **kwargs):
            kwargs.pop("trust_env", None)
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(image_tools.httpx, "AsyncClient", PatchedClient)
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
    finally:
        await store.close()


async def _async(value):
    return value
