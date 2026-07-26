from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import keyring
import pytest
from fastapi.testclient import TestClient

import shejane_runtime.server as server_module
from shejane_runtime.config import reset_settings_for_tests
from shejane_runtime.model_services import (
    adapter_for_custom_service,
    list_model_service_presets,
)
from shejane_runtime.runs import RunCoordinator
from shejane_runtime.server import create_app
from shejane_runtime.store.sqlite import LocalStore


def test_model_service_presets_prioritize_china_and_expose_editable_addresses() -> None:
    presets = list_model_service_presets()

    assert [preset["id"] for preset in presets] == [
        "deepseek",
        "kimi",
        "qwen",
        "glm",
        "minimax",
        "siliconflow",
        "custom",
    ]
    for preset in presets[:-1]:
        assert preset["regions"][0]["id"] == "cn"
        assert preset["regions"][0]["default"] is True
        assert preset["regions"][0]["base_url"].startswith("https://")
        assert "adapter_id" not in preset


def test_custom_service_adapter_detection_prefers_openai_chat_on_a_tie() -> None:
    assert (
        adapter_for_custom_service(
            openai_chat_available=True,
            anthropic_messages_available=True,
        )
        == "openai_chat"
    )
    assert (
        adapter_for_custom_service(
            openai_chat_available=False,
            anthropic_messages_available=True,
        )
        == "anthropic_messages"
    )
    assert (
        adapter_for_custom_service(
            openai_chat_available=False,
            anthropic_messages_available=False,
        )
        is None
    )


def test_catalog_refresh_preserves_manual_and_verified_models() -> None:
    current = [
        {
            "model_id": "verified",
            "verification": "verified",
            "streaming": True,
            "tool_calling": True,
            "source": "discovered",
        },
        {
            "model_id": "manual",
            "verification": "unverified",
            "streaming": True,
            "tool_calling": True,
            "source": "manual",
        },
    ]
    refreshed = [
        {
            "model_id": "verified",
            "verification": "unverified",
            "streaming": False,
            "tool_calling": False,
            "source": "discovered",
        },
    ]

    merged = server_module._merge_refreshed_model_catalog(current, refreshed)

    assert [model["model_id"] for model in merged] == ["verified", "manual"]
    assert merged[0]["verification"] == "verified"
    assert merged[0]["streaming"] is True
    assert merged[0]["tool_calling"] is True


def test_model_service_presets_are_runtime_owned(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path,
    )
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)

    with TestClient(create_app(settings)) as client:
        response = client.get(
            "/v1/model-services/presets",
            headers={"Authorization": "Bearer tok"},
        )

    assert response.status_code == 200
    deepseek = response.json()["services"][0]
    assert deepseek == {
        "id": "deepseek",
        "name": "DeepSeek",
        "description": "推理和通用任务，按 DeepSeek 官方价格计费。",
        "api_key_url": "https://platform.deepseek.com/api_keys",
        "billing_url": "https://platform.deepseek.com/usage",
        "regions": [
            {
                "id": "cn",
                "name": "中国站",
                "default": True,
                "base_url": "https://api.deepseek.com/v1",
            }
        ],
    }


def test_legacy_provider_secrets_are_deleted_before_legacy_table(
    tmp_path: Path,
    credential_vault: dict[str, str],
) -> None:
    database = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE local_model_providers (principal_id TEXT, id TEXT, credential_ref TEXT)"
        )
        conn.execute(
            "INSERT INTO local_model_providers VALUES (?, ?, ?)",
            ("local:owner", "legacy", "keyring:model-provider:legacy"),
        )
    credential_vault["local:owner:legacy"] = "legacy-secret"

    async def migrate() -> None:
        store = await LocalStore.open(database)
        await store.close()

    asyncio.run(migrate())

    assert credential_vault == {}
    with sqlite3.connect(database) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'local_model_providers'"
            ).fetchone()
            is None
        )


@pytest.fixture
def credential_vault(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    values: dict[str, str] = {}
    monkeypatch.setattr(keyring, "get_password", lambda _service, account: values.get(account))
    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda _service, account, password: values.__setitem__(account, password),
    )
    monkeypatch.setattr(
        keyring,
        "delete_password",
        lambda _service, account: values.pop(account, None),
    )
    return values


def test_official_model_service_connects_with_bundled_catalog_when_refresh_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credential_vault: dict[str, str],
) -> None:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path,
    )
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)

    async def fail_refresh(**kwargs):
        return [dict(model) for model in kwargs["preset"]["models"]], "stale"

    monkeypatch.setattr(server_module, "_refresh_model_service_models", fail_refresh)

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/model-services",
            headers={"Authorization": "Bearer tok"},
            json={
                "preset_id": "deepseek",
                "region": "cn",
                "api_key": "deepseek-secret",
                "base_url": "https://gateway.example/v1",
            },
        )
        assert response.status_code == 201
        connected = response.json()
        listed = client.get(
            "/v1/model-services",
            headers={"Authorization": "Bearer tok"},
        )
        models = client.get(
            "/v1/models",
            headers={"Authorization": "Bearer tok"},
        )

    assert connected["preset_id"] == "deepseek"
    assert connected["name"] == "DeepSeek"
    assert connected["region"] == "cn"
    assert connected["adapter_id"] == "openai_chat"
    assert connected["base_url"] == "https://gateway.example/v1"
    assert connected["credential_configured"] is True
    assert connected["catalog_status"] == "stale"
    assert connected["models"][0]["model_id"] == "deepseek-v4-flash"
    assert connected["models"][0]["recommended"] is True
    assert "api_key" not in connected
    assert listed.json()["services"] == [connected]
    assert models.json()["models"][0]["spec"] == (f"local:{connected['id']}:deepseek-v4-flash")
    assert list(credential_vault.values()) == ["deepseek-secret"]


def test_custom_model_service_detects_adapter_and_allows_manual_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credential_vault: dict[str, str],
) -> None:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path,
    )
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)

    async def detect(**kwargs):
        if kwargs["adapter_id"] == "openai_chat":
            return [
                {
                    "model_id": "gateway-model",
                    "display_name": "Gateway Model",
                    "source": "discovered",
                    "verification": "unverified",
                    "recommended": False,
                    "tool_calling": True,
                    "streaming": True,
                    "image_inputs": False,
                }
            ], "ready"
        return [], "unavailable"

    monkeypatch.setattr(server_module, "_refresh_model_service_models", detect)

    with TestClient(create_app(settings)) as client:
        connected = client.post(
            "/v1/model-services",
            headers={"Authorization": "Bearer tok"},
            json={
                "preset_id": "custom",
                "name": "公司网关",
                "base_url": "https://gateway.example/v1",
                "api_key": "gateway-secret",
            },
        )
        assert connected.status_code == 201
        connection = connected.json()
        assert connection["adapter_id"] == "openai_chat"

        manual = client.post(
            f"/v1/model-services/{connection['id']}/models",
            headers={"Authorization": "Bearer tok"},
            json={"model_id": "private-model", "display_name": "Private Model"},
        )
        assert manual.status_code == 201
        assert manual.json()["verification"] == "unverified"
        assert manual.json()["recommended"] is False

        catalog = client.get(
            "/v1/models",
            headers={"Authorization": "Bearer tok"},
        ).json()["models"]
        private = next(model for model in catalog if model["model_id"] == "private-model")
        assert private["available"] is False


def test_custom_model_service_reports_invalid_api_key_before_protocol_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credential_vault: dict[str, str],
) -> None:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path,
    )
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)

    async def reject(**_kwargs):
        raise server_module.HTTPException(status_code=401, detail="invalid key")

    monkeypatch.setattr(server_module, "_refresh_model_service_models", reject)

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/model-services",
            headers={"Authorization": "Bearer tok"},
            json={
                "preset_id": "custom",
                "name": "Gateway",
                "base_url": "https://gateway.example/v1",
                "api_key": "invalid",
            },
        )

    assert response.status_code == 401
    assert credential_vault == {}


def test_model_service_refresh_and_delete_preserve_no_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credential_vault: dict[str, str],
) -> None:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path,
    )
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)

    calls = 0

    async def refresh(**kwargs):
        nonlocal calls
        calls += 1
        return [dict(model) for model in kwargs["preset"]["models"]], "ready"

    monkeypatch.setattr(server_module, "_refresh_model_service_models", refresh)

    with TestClient(create_app(settings)) as client:
        connection = client.post(
            "/v1/model-services",
            headers={"Authorization": "Bearer tok"},
            json={"preset_id": "deepseek", "api_key": "secret"},
        ).json()
        refreshed = client.post(
            f"/v1/model-services/{connection['id']}/refresh",
            headers={"Authorization": "Bearer tok"},
        )
        deleted = client.delete(
            f"/v1/model-services/{connection['id']}",
            headers={"Authorization": "Bearer tok"},
        )
        listed = client.get(
            "/v1/model-services",
            headers={"Authorization": "Bearer tok"},
        )

    assert calls == 2
    assert refreshed.status_code == 200
    assert refreshed.json()["catalog_status"] == "ready"
    assert refreshed.json()["version"] == connection["version"]
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert listed.json() == {"services": []}
    assert credential_vault == {}


def test_model_service_can_replace_its_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credential_vault: dict[str, str],
) -> None:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path,
    )
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)
    seen_keys: list[str] = []
    seen_base_urls: list[str] = []

    async def refresh(**kwargs):
        seen_keys.append(kwargs["api_key"])
        seen_base_urls.append(kwargs["base_url"])
        return [dict(model) for model in kwargs["preset"]["models"]], "ready"

    monkeypatch.setattr(server_module, "_refresh_model_service_models", refresh)

    with TestClient(create_app(settings)) as client:
        connection = client.post(
            "/v1/model-services",
            headers={"Authorization": "Bearer tok"},
            json={"preset_id": "deepseek", "api_key": "old-secret"},
        ).json()
        replaced = client.put(
            f"/v1/model-services/{connection['id']}/credential",
            headers={"Authorization": "Bearer tok"},
            json={
                "api_key": "new-secret",
                "base_url": "https://gateway.example/v1",
            },
        )

    assert replaced.status_code == 200
    assert replaced.json()["credential_configured"] is True
    assert replaced.json()["base_url"] == "https://gateway.example/v1"
    assert replaced.json()["version"] == connection["version"] + 1
    assert seen_keys == ["old-secret", "new-secret"]
    assert seen_base_urls == [
        "https://api.deepseek.com/v1",
        "https://gateway.example/v1",
    ]
    assert list(credential_vault.values()) == ["new-secret"]


def test_model_service_keeps_old_api_key_when_database_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credential_vault: dict[str, str],
) -> None:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path,
    )
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)

    async def refresh(**kwargs):
        return [dict(model) for model in kwargs["preset"]["models"]], "ready"

    monkeypatch.setattr(server_module, "_refresh_model_service_models", refresh)
    app = create_app(settings)

    with TestClient(app, raise_server_exceptions=False) as client:
        connection = client.post(
            "/v1/model-services",
            headers={"Authorization": "Bearer tok"},
            json={"preset_id": "deepseek", "api_key": "old-secret"},
        ).json()

        async def fail_replace(**_kwargs):
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(
            app.state.store,
            "replace_model_connection_credential",
            fail_replace,
        )
        replaced = client.put(
            f"/v1/model-services/{connection['id']}/credential",
            headers={"Authorization": "Bearer tok"},
            json={"api_key": "new-secret"},
        )

    assert replaced.status_code == 500
    assert list(credential_vault.values()) == ["old-secret"]


def test_model_service_keeps_old_api_key_when_new_key_cannot_be_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credential_vault: dict[str, str],
) -> None:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path,
    )
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)
    calls = 0

    async def refresh(**kwargs):
        nonlocal calls
        calls += 1
        return [dict(model) for model in kwargs["preset"]["models"]], (
            "ready" if calls == 1 else "stale"
        )

    monkeypatch.setattr(server_module, "_refresh_model_service_models", refresh)

    with TestClient(create_app(settings)) as client:
        connection = client.post(
            "/v1/model-services",
            headers={"Authorization": "Bearer tok"},
            json={"preset_id": "deepseek", "api_key": "old-secret"},
        ).json()
        replaced = client.put(
            f"/v1/model-services/{connection['id']}/credential",
            headers={"Authorization": "Bearer tok"},
            json={
                "api_key": "unverified-secret",
                "base_url": "https://unverified.example/v1",
            },
        )
        listed = client.get(
            "/v1/model-services",
            headers={"Authorization": "Bearer tok"},
        ).json()["services"][0]

    assert replaced.status_code == 503
    assert replaced.json()["detail"]["message"] == "暂时无法验证新的 API Key，旧 Key 已保留。"
    assert listed["base_url"] == connection["base_url"]
    assert list(credential_vault.values()) == ["old-secret"]


def test_model_service_delete_restores_api_key_when_database_delete_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credential_vault: dict[str, str],
) -> None:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path,
    )
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)

    async def refresh(**kwargs):
        return [dict(model) for model in kwargs["preset"]["models"]], "ready"

    monkeypatch.setattr(server_module, "_refresh_model_service_models", refresh)
    app = create_app(settings)

    with TestClient(app, raise_server_exceptions=False) as client:
        connection = client.post(
            "/v1/model-services",
            headers={"Authorization": "Bearer tok"},
            json={"preset_id": "deepseek", "api_key": "secret"},
        ).json()

        async def fail_delete(**_kwargs):
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(app.state.store, "delete_model_connection", fail_delete)
        deleted = client.delete(
            f"/v1/model-services/{connection['id']}",
            headers={"Authorization": "Bearer tok"},
        )

    assert deleted.status_code == 500
    assert list(credential_vault.values()) == ["secret"]


def test_imported_model_service_keeps_metadata_but_requires_reconnection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credential_vault: dict[str, str],
) -> None:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path,
    )
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)
    connection_id = f"conn_{'a' * 32}"

    with TestClient(create_app(settings)) as client:
        imported = client.post(
            "/v1/model-services/import",
            headers={"Authorization": "Bearer tok"},
            json={
                "id": connection_id,
                "preset_id": "deepseek",
                "name": "Untrusted display name",
                "region": "cn",
                "adapter_id": "anthropic_messages",
                "base_url": "https://attacker.example/v1",
                "models": [],
            },
        )
        catalog = client.get(
            "/v1/models",
            headers={"Authorization": "Bearer tok"},
        ).json()["models"]

    assert imported.status_code == 201
    connection = imported.json()
    assert connection["id"] == connection_id
    assert connection["name"] == "DeepSeek"
    assert connection["adapter_id"] == "openai_chat"
    assert connection["base_url"] == "https://api.deepseek.com/v1"
    assert connection["credential_configured"] is False
    assert connection["catalog_status"] == "stale"
    assert catalog[0]["available"] is False
    assert credential_vault == {}


def test_imported_custom_service_must_be_recreated_manually(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credential_vault: dict[str, str],
) -> None:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path,
    )
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)

    with TestClient(create_app(settings)) as client:
        imported = client.post(
            "/v1/model-services/import",
            headers={"Authorization": "Bearer tok"},
            json={
                "id": f"conn_{'b' * 32}",
                "preset_id": "custom",
                "name": "DeepSeek",
                "region": "custom",
                "adapter_id": "openai_chat",
                "base_url": "https://attacker.example/v1",
                "models": [],
            },
        )

    assert imported.status_code == 400
    assert imported.json()["detail"] == "custom model services must be reconnected manually"
    assert credential_vault == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_id", "base_url", "event", "tool_stream"),
    [
        (
            "openai_chat",
            "https://gateway.example/v1",
            {"choices": [{"delta": {"tool_calls": [{"index": 0}]}}]},
            False,
        ),
        (
            "openai_chat",
            "https://gateway.example/v1",
            {"choices": [{"message": {"tool_calls": [{"id": "call-ping"}]}}]},
            False,
        ),
        (
            "openai_chat",
            "https://open.bigmodel.cn/api/paas/v4",
            {"choices": [{"delta": {"tool_calls": [{"index": 0}]}}]},
            True,
        ),
        (
            "anthropic_messages",
            "https://gateway.example/v1",
            {
                "type": "content_block_start",
                "content_block": {"type": "tool_use", "name": "ping"},
            },
            False,
        ),
    ],
)
async def test_compatibility_verification_requires_streamed_tool_call(
    monkeypatch: pytest.MonkeyPatch,
    adapter_id: str,
    base_url: str,
    event: dict,
    tool_stream: bool,
) -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n".encode(),
            headers={"content-type": "text/event-stream"},
        )

    class PatchedClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(server_module.httpx, "AsyncClient", PatchedClient)

    await server_module._verify_model_service_compatibility(
        base_url=base_url,
        adapter_id=adapter_id,
        api_key="secret",
        model_id="model",
    )

    assert requests[0]["stream"] is True
    assert "tool_choice" not in requests[0]
    assert requests[0].get("tool_stream", False) is tool_stream


@pytest.mark.asyncio
async def test_compatibility_verification_rejects_non_streaming_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    class PatchedClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(server_module.httpx, "AsyncClient", PatchedClient)

    with pytest.raises(server_module.HTTPException) as exc_info:
        await server_module._verify_model_service_compatibility(
            base_url="https://gateway.example/v1",
            adapter_id="openai_chat",
            api_key="secret",
            model_id="model",
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["message"] == "模型没有返回流式结果。"


def test_deepseek_v4_verification_does_not_force_tool_choice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credential_vault: dict[str, str],
) -> None:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path,
    )
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)

    async def discover(**_kwargs):
        return [
            {
                "model_id": "deepseek-v4-pro",
                "display_name": "DeepSeek V4 Pro",
                "source": "discovered",
                "verification": "unverified",
                "recommended": False,
                "tool_calling": True,
                "streaming": True,
                "image_inputs": False,
            }
        ], "ready"

    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if "tool_choice" in payload:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "Thinking mode does not support this tool_choice",
                        "type": "invalid_request_error",
                    }
                },
            )
        event = {"choices": [{"delta": {"tool_calls": [{"index": 0}]}}]}
        return httpx.Response(
            200,
            content=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n".encode(),
            headers={"content-type": "text/event-stream"},
        )

    class PatchedClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(server_module, "_refresh_model_service_models", discover)
    monkeypatch.setattr(server_module.httpx, "AsyncClient", PatchedClient)

    with TestClient(create_app(settings)) as client:
        connection = client.post(
            "/v1/model-services",
            headers={"Authorization": "Bearer tok"},
            json={"preset_id": "deepseek", "api_key": "secret"},
        ).json()
        verified = client.post(
            f"/v1/model-services/{connection['id']}/models/deepseek-v4-pro/verify",
            headers={"Authorization": "Bearer tok"},
        )

    assert verified.status_code == 200
    assert "tool_choice" not in requests[0]


def test_manual_model_becomes_available_only_after_compatibility_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credential_vault: dict[str, str],
) -> None:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path,
    )
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)

    async def detected(**kwargs):
        return [], "ready" if kwargs["adapter_id"] == "openai_chat" else "unavailable"

    async def compatible(**_kwargs):
        return None

    monkeypatch.setattr(server_module, "_refresh_model_service_models", detected)
    monkeypatch.setattr(server_module, "_verify_model_service_compatibility", compatible)

    with TestClient(create_app(settings)) as client:
        connection = client.post(
            "/v1/model-services",
            headers={"Authorization": "Bearer tok"},
            json={
                "preset_id": "custom",
                "name": "Gateway",
                "base_url": "https://gateway.example/v1",
                "api_key": "secret",
            },
        ).json()
        client.post(
            f"/v1/model-services/{connection['id']}/models",
            headers={"Authorization": "Bearer tok"},
            json={"model_id": "private-model"},
        )

        verified = client.post(
            f"/v1/model-services/{connection['id']}/models/private-model/verify",
            headers={"Authorization": "Bearer tok"},
        )
        catalog = client.get(
            "/v1/models",
            headers={"Authorization": "Bearer tok"},
        ).json()["models"]

    assert verified.status_code == 200
    assert verified.json()["verification"] == "verified"
    assert catalog[0]["available"] is True
