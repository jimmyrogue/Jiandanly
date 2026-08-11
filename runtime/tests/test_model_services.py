from __future__ import annotations

import asyncio
import json
import sqlite3
from functools import partial
from pathlib import Path

import httpx
import keyring
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage

import shejane_runtime.model_services.catalog as model_catalog
import shejane_runtime.model_services.probes as model_probes
import shejane_runtime.model_services.routes as model_routes
import shejane_runtime.server as server_module
from shejane_runtime.agent.model_runtime import _hosted_tools_for_model_binding
from shejane_runtime.auth import LOCAL_OWNER_PRINCIPAL_ID
from shejane_runtime.config import reset_settings_for_tests
from shejane_runtime.model_services import (
    adapter_for_custom_service,
    list_model_service_presets,
    model_service_preset,
)
from shejane_runtime.model_services.credentials import CredentialStoreError
from shejane_runtime.model_services.profiles import default_model_protocol, discovered_model_profile
from shejane_runtime.runs import RunCoordinator
from shejane_runtime.server import create_app
from shejane_runtime.store.sqlite import LocalStore
from tests.helpers import run_command


def test_default_capability_binding_never_replaces_an_existing_choice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path,
    )
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)

    with TestClient(create_app(settings)) as client:
        store = client.app.state.store
        client.portal.call(
            partial(
                store.create_model_connection,
                principal_id=LOCAL_OWNER_PRINCIPAL_ID,
                connection_id="manual",
                preset_id="custom",
                name="Manual",
                region="custom",
                adapter_id="openai_chat",
                base_url="https://manual.example/v1",
                requires_api_key=True,
                credential_ref="credential:manual",
                models=[],
                catalog_status="ready",
            )
        )
        client.portal.call(
            partial(
                store.create_model_connection,
                principal_id=LOCAL_OWNER_PRINCIPAL_ID,
                connection_id="official",
                preset_id="shejane-official",
                name="Official",
                region="official",
                adapter_id="openai_chat",
                base_url="https://app.shejane.com/v1",
                requires_api_key=True,
                credential_ref="credential:official",
                models=[],
                catalog_status="ready",
            )
        )
        client.portal.call(
            partial(
                store.set_model_capability_binding,
                principal_id=LOCAL_OWNER_PRINCIPAL_ID,
                capability="image_generation",
                connection_id="manual",
                connection_version=1,
                model_id="manual-image",
                protocol="openai_images_generations",
            )
        )
        inserted = client.portal.call(
            partial(
                store.create_model_capability_binding_if_absent,
                principal_id=LOCAL_OWNER_PRINCIPAL_ID,
                capability="image_generation",
                connection_id="official",
                connection_version=1,
                model_id="official-image",
                protocol="openai_images_generations",
            )
        )
        binding = client.portal.call(
            partial(
                store.get_model_capability_binding,
                principal_id=LOCAL_OWNER_PRINCIPAL_ID,
                capability="image_generation",
            )
        )

    assert inserted is None
    assert binding is not None
    assert binding["connection_id"] == "manual"
    assert binding["model_id"] == "manual-image"
    assert binding["revision"] == 1


def test_model_service_presets_prioritize_china_and_expose_editable_addresses() -> None:
    presets = list_model_service_presets()

    assert [preset["id"] for preset in presets] == [
        "shejane-official",
        "deepseek",
        "kimi",
        "qwen",
        "glm",
        "minimax",
        "siliconflow",
        "openai",
        "anthropic",
        "google",
        "custom",
    ]
    official = presets[0]
    assert official == {
        "id": "shejane-official",
        "name": "SheJane 官方服务（推荐）",
        "description": "登录 SheJane Cloud 使用官方托管的模型服务。",
        "connection_method": "browser_authorization",
        "api_key_url": None,
        "billing_url": None,
        "regions": [],
    }
    for preset in presets[1:7]:
        assert preset["regions"][0]["id"] == "cn"
        assert preset["regions"][0]["default"] is True
        assert preset["regions"][0]["base_url"].startswith("https://")
    for preset in presets[7:-1]:
        assert preset["regions"][0]["id"] == "intl"
        assert preset["regions"][0]["default"] is True
        assert preset["regions"][0]["base_url"].startswith("https://")
    for preset in presets[1:-1]:
        assert "adapter_id" not in preset

    official_runtime_preset = model_service_preset("shejane-official")
    assert official_runtime_preset is not None
    assert official_runtime_preset["models"] == ()


def test_native_overseas_adapters_have_explicit_default_protocols() -> None:
    assert default_model_protocol("openai_chat", "agent_chat") == "openai_chat_completions"
    assert default_model_protocol("anthropic_messages", "agent_chat") == "anthropic_messages"
    assert default_model_protocol("google_genai", "agent_chat") == "google_generate_content"


@pytest.mark.asyncio
async def test_google_catalog_discovery_uses_native_models_endpoint(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (
            "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000"
        )
        assert request.headers["x-goog-api-key"] == "secret"
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "models/gemini-test",
                        "baseModelId": "gemini-test",
                        "displayName": "Gemini Test",
                        "inputTokenLimit": 1_000_000,
                        "outputTokenLimit": 65_536,
                    }
                ]
            },
        )

    class PatchedClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(model_catalog.httpx, "AsyncClient", PatchedClient)

    models, status = await model_routes._refresh_model_service_models(
        preset=model_service_preset("google") or {},
        base_url="https://generativelanguage.googleapis.com",
        adapter_id="google_genai",
        api_key="secret",
    )

    assert status == "ready"
    assert models[0]["model_id"] == "gemini-test"
    assert models[0]["display_name"] == "Gemini Test"
    assert models[0]["max_input_tokens"] == 1_000_000
    assert models[0]["max_output_tokens"] == 65_536


@pytest.mark.asyncio
async def test_openai_catalog_defaults_agent_models_to_responses(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.openai.com/v1/models"
        return httpx.Response(200, json={"data": [{"id": "gpt-test"}]})

    class PatchedClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(model_catalog.httpx, "AsyncClient", PatchedClient)

    models, status = await model_routes._refresh_model_service_models(
        preset=model_service_preset("openai") or {},
        base_url="https://api.openai.com/v1",
        adapter_id="openai_chat",
        api_key="secret",
    )

    assert status == "ready"
    assert models[0]["capabilities"] == [
        {
            "capability": "agent_chat",
            "protocol": "openai_responses",
            "verification": "unverified",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("preset_id", "trust_declarations"),
    [("shejane-official", True), ("custom", False)],
)
async def test_model_catalog_purposes_are_declared_only_for_official_service(
    monkeypatch,
    preset_id: str,
    trust_declarations: bool,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://cloud.example.test/v1/models"
        assert request.headers["Authorization"] == "Bearer inference-token"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": model_id,
                        "capabilities": ["image_generation"],
                        "recommended_for": ["image_generation"],
                    }
                    for model_id in ("gpt-image-2", "gpt-image-2-vip")
                ]
            },
        )

    class PatchedClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(model_catalog.httpx, "AsyncClient", PatchedClient)
    preset = model_service_preset(preset_id)
    assert preset is not None
    preset["models"] = ()

    models, status = await model_routes._refresh_model_service_models(
        preset=preset,
        base_url="https://cloud.example.test",
        adapter_id="openai_chat",
        api_key="inference-token",
    )

    assert status == "ready"
    assert [model["model_id"] for model in models] == ["gpt-image-2", "gpt-image-2-vip"]
    for model in models:
        assert model["capabilities"] == (
            [
                {
                    "capability": "image_generation",
                    "protocol": "openai_images_generations",
                    "verification": "verified",
                }
            ]
            if trust_declarations
            else []
        )
        assert model["recommended"] is trust_declarations


@pytest.mark.asyncio
async def test_official_catalog_keeps_known_agent_capabilities_and_limits(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://cloud.example.test/v1/models"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "deepseek-v4-flash",
                        "capabilities": ["agent_chat"],
                        "recommended_for": ["agent_chat"],
                    }
                ]
            },
        )

    class PatchedClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(model_catalog.httpx, "AsyncClient", PatchedClient)
    preset = model_service_preset("shejane-official")
    assert preset is not None

    models, status = await model_routes._refresh_model_service_models(
        preset=preset,
        base_url="https://cloud.example.test/v1",
        adapter_id="openai_chat",
        api_key="inference-token",
    )

    assert status == "ready"
    assert models == [
        {
            "model_id": "deepseek-v4-flash",
            "display_name": "deepseek-v4-flash",
            "capabilities": [
                {
                    "capability": "agent_chat",
                    "protocol": "openai_chat_completions",
                    "verification": "unverified",
                }
            ],
            "tool_calling": True,
            "streaming": True,
            "image_inputs": False,
            "max_input_tokens": 1_000_000,
            "max_output_tokens": 384_000,
            "source": "discovered",
            "verification": "unverified",
            "recommended": True,
            "recommended_for": ["agent_chat"],
        }
    ]


@pytest.mark.asyncio
async def test_model_connection_store_accepts_native_google_adapter(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    try:
        row = await store.create_model_connection(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            connection_id=f"conn_{'c' * 32}",
            preset_id="google",
            name="Google Gemini",
            region="intl",
            adapter_id="google_genai",
            base_url="https://generativelanguage.googleapis.com",
            requires_api_key=True,
            credential_ref="model-service:test",
            models=[],
            catalog_status="ready",
        )
        assert row["adapter_id"] == "google_genai"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_model_connection_adapter_migration_preserves_existing_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE model_connections ("
        "principal_id TEXT NOT NULL, id TEXT NOT NULL, preset_id TEXT NOT NULL, "
        "name TEXT NOT NULL, region TEXT NOT NULL "
        "CHECK (region IN ('cn', 'intl', 'custom')), adapter_id TEXT NOT NULL "
        "CHECK (adapter_id IN ('openai_chat', 'anthropic_messages')), "
        "base_url TEXT NOT NULL, requires_api_key INTEGER NOT NULL, "
        "credential_ref TEXT NOT NULL, models_json TEXT NOT NULL, "
        "catalog_status TEXT NOT NULL, version INTEGER NOT NULL, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "PRIMARY KEY (principal_id, id))"
    )
    connection.execute(
        "INSERT INTO model_connections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            LOCAL_OWNER_PRINCIPAL_ID,
            f"conn_{'d' * 32}",
            "deepseek",
            "DeepSeek",
            "cn",
            "openai_chat",
            "https://api.deepseek.com/v1",
            1,
            "model-service:existing",
            "[]",
            "ready",
            1,
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        ),
    )
    connection.commit()
    connection.close()

    store = await LocalStore.open(db_path)
    try:
        existing = await store.get_model_connection(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            connection_id=f"conn_{'d' * 32}",
        )
        assert existing is not None
        assert existing["adapter_id"] == "openai_chat"
        created = await store.create_model_connection(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            connection_id=f"conn_{'e' * 32}",
            preset_id="google",
            name="Google Gemini",
            region="intl",
            adapter_id="google_genai",
            base_url="https://generativelanguage.googleapis.com",
            requires_api_key=True,
            credential_ref="model-service:google",
            models=[],
            catalog_status="ready",
        )
        assert created["adapter_id"] == "google_genai"
        official = await store.create_model_connection(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            connection_id=f"conn_{'f' * 32}",
            preset_id="shejane-official",
            name="SheJane 官方服务（推荐）",
            region="official",
            adapter_id="openai_chat",
            base_url="https://cloud.example.test",
            requires_api_key=True,
            credential_ref="model-service:official",
            models=[],
            catalog_status="ready",
        )
        assert official["region"] == "official"
    finally:
        await store.close()


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


def test_discovered_models_are_candidates_without_assumed_agent_capabilities() -> None:
    profile = discovered_model_profile(
        {},
        model_id="gpt-image-1",
        display_name="GPT Image 1",
        service_base_url="https://gateway.example/v1",
    )

    assert profile["capabilities"] == []
    assert profile["tool_calling"] is False
    assert profile["streaming"] is False
    assert profile["image_inputs"] is False


def test_legacy_verified_models_remain_agent_models() -> None:
    models = model_routes._model_connection_models(
        {
            "adapter_id": "anthropic_messages",
            "base_url": "https://gateway.example/v1",
            "models_json": json.dumps(
                [
                    {
                        "model_id": "legacy-model",
                        "display_name": "Legacy Model",
                        "source": "manual",
                        "verification": "verified",
                        "tool_calling": True,
                        "streaming": True,
                        "image_inputs": False,
                    }
                ]
            ),
        }
    )

    assert models[0]["capabilities"] == [
        {
            "capability": "agent_chat",
            "protocol": "anthropic_messages",
            "verification": "verified",
        }
    ]


def test_official_image_capabilities_created_before_the_fix_are_restored() -> None:
    models = model_routes._model_connection_models(
        {
            "preset_id": "shejane-official",
            "adapter_id": "openai_chat",
            "base_url": "https://cloud.example.test/v1",
            "models_json": json.dumps(
                [
                    {
                        "model_id": "gpt-image-2",
                        "display_name": "GPT Image 2",
                        "source": "discovered",
                        "verification": "unverified",
                        "capabilities": [
                            {
                                "capability": "image_generation",
                                "protocol": "openai_images_generations",
                                "verification": "unverified",
                            }
                        ],
                    }
                ]
            ),
        }
    )

    assert models[0]["verification"] == "verified"
    assert models[0]["capabilities"][0]["verification"] == "verified"


def test_official_deepseek_agent_capabilities_created_before_the_fix_are_restored() -> None:
    models = model_routes._model_connection_models(
        {
            "preset_id": "shejane-official",
            "adapter_id": "openai_chat",
            "base_url": "https://cloud.example.test/v1",
            "models_json": json.dumps(
                [
                    {
                        "model_id": "deepseek-v4-flash",
                        "display_name": "DeepSeek V4 Flash",
                        "source": "discovered",
                        "verification": "unverified",
                        "tool_calling": False,
                        "streaming": False,
                        "capabilities": [
                            {
                                "capability": "agent_chat",
                                "protocol": "openai_chat_completions",
                                "verification": "unverified",
                            }
                        ],
                    }
                ]
            ),
        }
    )

    assert models[0]["tool_calling"] is True
    assert models[0]["streaming"] is True
    assert models[0]["max_input_tokens"] == 1_000_000
    assert models[0]["max_output_tokens"] == 384_000


def test_bundled_models_are_recommendations_not_preverified_connections() -> None:
    deepseek = model_service_preset("deepseek")

    assert deepseek is not None
    assert [model["verification"] for model in deepseek["models"]] == [
        "unverified",
        "unverified",
    ]
    assert [
        model["capabilities"][0]["protocol"] for model in deepseek["models"]
    ] == [
        "openai_responses",
        "openai_chat_completions",
    ]


def test_catalog_refresh_preserves_manual_and_verified_models() -> None:
    current = [
        {
            "model_id": "verified",
            "verification": "verified",
            "streaming": True,
            "tool_calling": True,
            "source": "discovered",
            "capabilities": [
                {
                    "capability": "agent_chat",
                    "protocol": "openai_chat_completions",
                    "verification": "verified",
                }
            ],
        },
        {
            "model_id": "manual",
            "verification": "unverified",
            "streaming": True,
            "tool_calling": True,
            "source": "manual",
        },
        {
            "model_id": "image",
            "verification": "unverified",
            "streaming": False,
            "tool_calling": False,
            "source": "discovered",
            "capabilities": [
                {
                    "capability": "image_generation",
                    "protocol": "openai_images_generations",
                    "verification": "unverified",
                }
            ],
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
        {
            "model_id": "image",
            "verification": "verified",
            "streaming": False,
            "tool_calling": False,
            "source": "discovered",
            "capabilities": [
                {
                    "capability": "image_generation",
                    "protocol": "openai_images_generations",
                    "verification": "verified",
                }
            ],
        },
    ]

    merged = model_routes._merge_refreshed_model_catalog(current, refreshed)

    assert [model["model_id"] for model in merged] == ["verified", "image", "manual"]
    assert merged[0]["verification"] == "verified"
    assert merged[0]["streaming"] is True
    assert merged[0]["tool_calling"] is True
    assert merged[1]["capabilities"][0]["verification"] == "verified"


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
    services = response.json()["services"]
    assert services[0] == {
        "id": "shejane-official",
        "name": "SheJane 官方服务（推荐）",
        "description": "登录 SheJane Cloud 使用官方托管的模型服务。",
        "connection_method": "browser_authorization",
        "api_key_url": None,
        "billing_url": None,
        "regions": [],
    }
    deepseek = services[1]
    assert deepseek == {
        "id": "deepseek",
        "name": "DeepSeek",
        "description": "推理和通用任务，按 DeepSeek 官方价格计费。",
        "connection_method": "api_key",
        "api_key_url": "https://platform.deepseek.com/api_keys",
        "billing_url": "https://platform.deepseek.com/usage",
        "regions": [
            {
                "id": "cn",
                "name": "中国站",
                "default": True,
                "base_url": "https://api.deepseek.com",
            }
        ],
    }


def test_browser_authorization_preset_rejects_api_key_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path,
    )
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/model-services",
            headers={"Authorization": "Bearer tok"},
            json={"preset_id": "shejane-official", "api_key": "must-not-be-used"},
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "model service requires browser authorization"}


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


def test_model_service_connection_makes_catalog_models_available_without_probe(
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

    monkeypatch.setattr(model_routes, "_refresh_model_service_models", fail_refresh)

    async def reject_compatibility_probe(**_kwargs):
        pytest.fail("connecting a service must not run the model compatibility probe")

    monkeypatch.setattr(
        model_probes,
        "_verify_model_service_compatibility",
        reject_compatibility_probe,
    )

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
        run = client.post(
            "/v1/runs",
            headers={"Authorization": "Bearer tok"},
            json=run_command(
                "say hi",
                model=f"local:{connected['id']}:deepseek-v4-flash",
            ),
        )

    assert connected["preset_id"] == "deepseek"
    assert connected["name"] == "DeepSeek"
    assert connected["region"] == "cn"
    assert connected["adapter_id"] == "openai_chat"
    assert connected["base_url"] == "https://gateway.example/v1"
    assert connected["credential_configured"] is True
    assert connected["catalog_status"] == "stale"
    assert [model["model_id"] for model in connected["models"]] == [
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    ]
    assert [model["verification"] for model in connected["models"]] == [
        "unverified",
        "unverified",
    ]
    assert connected["models"][0]["recommended"] is True
    assert connected["models"][1]["recommended"] is False
    assert connected["models"][0]["provider_family"] == "deepseek"
    assert connected["models"][0]["reasoning"] == {
        "supported": True,
        "modes": ["off", "high", "max"],
        "default_mode": "off",
        "stream_field": "reasoning_content",
        "tool_roundtrip_required": True,
        "display_policy": "activity_only",
    }
    assert "api_key" not in connected
    assert listed.json()["services"] == [connected]
    assert [model["available"] for model in models.json()["models"]] == [True, True]
    assert run.status_code == 200
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
                    "tool_calling": False,
                    "streaming": False,
                    "image_inputs": False,
                }
            ], "ready"
        return [], "unavailable"

    monkeypatch.setattr(model_routes, "_refresh_model_service_models", detect)

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
        gateway = next(model for model in catalog if model["model_id"] == "gateway-model")
        private = next(model for model in catalog if model["model_id"] == "private-model")
        run = client.post(
            "/v1/runs",
            headers={"Authorization": "Bearer tok"},
            json=run_command(
                "say hi",
                model=f"local:{connection['id']}:gateway-model",
            ),
        )
        assert gateway["available"] is True
        assert private["available"] is False
        assert run.status_code == 200


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

    monkeypatch.setattr(model_routes, "_refresh_model_service_models", reject)

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

    monkeypatch.setattr(model_routes, "_refresh_model_service_models", refresh)

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

    monkeypatch.setattr(model_routes, "_refresh_model_service_models", refresh)

    async def reject_compatibility_probe(**_kwargs):
        pytest.fail("reconnecting a service must not run the model compatibility probe")

    monkeypatch.setattr(
        model_probes,
        "_verify_model_service_compatibility",
        reject_compatibility_probe,
    )

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
    assert [model["verification"] for model in replaced.json()["models"]] == [
        "unverified",
        "unverified",
    ]
    assert seen_keys == ["old-secret", "new-secret"]
    assert seen_base_urls == [
        "https://api.deepseek.com",
        "https://gateway.example/v1",
    ]
    assert list(credential_vault.values()) == ["new-secret"]


def test_model_service_list_keeps_inaccessible_connections_recoverable(
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

    monkeypatch.setattr(model_routes, "_refresh_model_service_models", refresh)
    original_get_model_api_key = model_catalog.get_model_api_key

    with TestClient(create_app(settings)) as client:
        inaccessible = client.post(
            "/v1/model-services",
            headers={"Authorization": "Bearer tok"},
            json={"preset_id": "deepseek", "api_key": "old-secret"},
        ).json()
        accessible = client.post(
            "/v1/model-services",
            headers={"Authorization": "Bearer tok"},
            json={"preset_id": "kimi", "api_key": "working-secret"},
        ).json()

        async def load_key(
            principal_id: str,
            connection_id: str,
            credential_reference: str | None = None,
        ) -> str | None:
            if connection_id == inaccessible["id"]:
                raise CredentialStoreError("system credential store is unavailable")
            return await original_get_model_api_key(
                principal_id,
                connection_id,
                credential_reference,
            )

        monkeypatch.setattr(model_catalog, "get_model_api_key", load_key)
        response = client.get(
            "/v1/model-services",
            headers={"Authorization": "Bearer tok"},
        )

    assert response.status_code == 200
    services = {service["id"]: service for service in response.json()["services"]}
    assert services[inaccessible["id"]]["credential_configured"] is False
    assert services[accessible["id"]]["credential_configured"] is True


def test_model_service_reconnects_when_old_credential_cannot_be_deleted(
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

    monkeypatch.setattr(model_routes, "_refresh_model_service_models", refresh)
    original_delete_model_api_key = model_routes.delete_model_api_key

    with TestClient(create_app(settings)) as client:
        connection = client.post(
            "/v1/model-services",
            headers={"Authorization": "Bearer tok"},
            json={"preset_id": "deepseek", "api_key": "old-secret"},
        ).json()
        old_credential_ref = model_routes.credential_ref(connection["id"])

        async def delete_key(
            principal_id: str,
            connection_id: str,
            credential_reference: str | None = None,
        ) -> None:
            if credential_reference == old_credential_ref:
                raise CredentialStoreError("system credential store is unavailable")
            await original_delete_model_api_key(
                principal_id,
                connection_id,
                credential_reference,
            )

        monkeypatch.setattr(model_routes, "delete_model_api_key", delete_key)
        replaced = client.put(
            f"/v1/model-services/{connection['id']}/credential",
            headers={"Authorization": "Bearer tok"},
            json={"api_key": "new-secret"},
        )
        listed = client.get(
            "/v1/model-services",
            headers={"Authorization": "Bearer tok"},
        )

    assert replaced.status_code == 200
    assert replaced.json()["credential_configured"] is True
    assert replaced.json()["version"] == connection["version"] + 1
    assert listed.status_code == 200
    assert listed.json()["services"][0]["credential_configured"] is True
    assert set(credential_vault.values()) == {"old-secret", "new-secret"}


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

    monkeypatch.setattr(model_routes, "_refresh_model_service_models", refresh)
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

    monkeypatch.setattr(model_routes, "_refresh_model_service_models", refresh)

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

    monkeypatch.setattr(model_routes, "_refresh_model_service_models", refresh)
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
    assert connection["base_url"] == "https://api.deepseek.com"
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
async def test_compatibility_verification_completes_model_tool_model_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rounds: list[list] = []
    bound_options: list[dict] = []

    class ProbeModel:
        def bind(self, **kwargs):
            bound_options.append(kwargs)
            return self

        def bind_tools(self, tools, **_kwargs):
            assert [tool["function"]["name"] for tool in tools] == ["shejane_ping"]
            return self

        async def ainvoke(self, messages):
            rounds.append(messages)
            if len(rounds) == 1:
                return AIMessage(
                    content="",
                    additional_kwargs={"reasoning_content": "keep me"},
                    tool_calls=[{"id": "call-ping", "name": "shejane_ping", "args": {}}],
                )
            return AIMessage(content="SHEJANE_MODEL_TOOL_LOOP_OK")

    monkeypatch.setattr(model_probes, "_build_byok_chat_model", lambda **_kwargs: ProbeModel())

    await model_probes._verify_model_service_compatibility(
        settings=reset_settings_for_tests(),
        base_url="https://gateway.example/v1",
        adapter_id="openai_chat",
        api_key="secret",
        model_id="model",
    )

    assert len(rounds) == 2
    assert bound_options == [{"max_tokens": 512}]
    assert rounds[1][1].additional_kwargs["reasoning_content"] == "keep me"
    assert rounds[1][1].tool_calls[0]["name"] == "shejane_ping"
    assert isinstance(rounds[1][2], ToolMessage)
    assert rounds[1][2].name == "shejane_ping"
    assert rounds[1][2].tool_call_id == "call-ping"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("protocol", "image_type"),
    [
        ("openai_chat_completions", "image_url"),
        ("anthropic_messages", "image"),
    ],
)
async def test_image_understanding_verification_sends_an_inline_image(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    image_type: str,
) -> None:
    class ProbeModel:
        def bind(self, **_kwargs):
            return self

        async def ainvoke(self, messages):
            content = messages[0].content
            prompt = next(item["text"] for item in content if item.get("type") == "text")
            assert "RED" not in prompt
            assert any(item.get("type") == image_type for item in content)
            return AIMessage(content="RED")

    monkeypatch.setattr(model_probes, "_build_byok_chat_model", lambda **_kwargs: ProbeModel())

    await model_probes._verify_model_image_understanding(
        settings=reset_settings_for_tests(),
        base_url="https://gateway.example/v1",
        protocol=protocol,
        api_key="secret",
        model_id="vision-model",
    )


@pytest.mark.asyncio
async def test_compatibility_verification_rejects_missing_final_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ProbeModel:
        def bind(self, **_kwargs):
            return self

        def bind_tools(self, _tools, **_kwargs):
            return self

        async def ainvoke(self, messages):
            if len(messages) == 1:
                return AIMessage(
                    content="",
                    tool_calls=[{"id": "call-ping", "name": "shejane_ping", "args": {}}],
                )
            return AIMessage(content="")

    monkeypatch.setattr(model_probes, "_build_byok_chat_model", lambda **_kwargs: ProbeModel())

    with pytest.raises(server_module.HTTPException) as exc_info:
        await model_probes._verify_model_service_compatibility(
            settings=reset_settings_for_tests(),
            base_url="https://gateway.example/v1",
            adapter_id="openai_chat",
            api_key="secret",
            model_id="model",
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["message"] == "模型没有在工具执行后返回最终答案。"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_status", "expected_status", "expected_code"),
    [
        (401, 401, "invalid_api_key"),
        (403, 403, "provider_permission_denied"),
        (402, 402, "billing_required"),
        (429, 429, "rate_limited"),
        (500, 503, "provider_unavailable"),
    ],
)
async def test_compatibility_verification_classifies_provider_failures(
    monkeypatch,
    provider_status: int,
    expected_status: int,
    expected_code: str,
) -> None:
    class ProviderError(RuntimeError):
        status_code = provider_status

    class ProbeModel:
        def bind(self, **_kwargs):
            return self

        def bind_tools(self, _tools, **_kwargs):
            return self

        async def ainvoke(self, _messages):
            raise ProviderError("provider rejected request with secret")

    monkeypatch.setattr(model_probes, "_build_byok_chat_model", lambda **_kwargs: ProbeModel())

    with pytest.raises(server_module.HTTPException) as exc_info:
        await model_probes._verify_model_service_compatibility(
            settings=reset_settings_for_tests(),
            base_url="https://gateway.example/v1",
            adapter_id="openai_chat",
            api_key="secret",
            model_id="model",
        )

    assert exc_info.value.status_code == expected_status
    assert exc_info.value.detail["code"] == expected_code


@pytest.mark.asyncio
async def test_compatibility_verification_reports_timeout_as_provider_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ProbeModel:
        def bind(self, **_kwargs):
            return self

        def bind_tools(self, _tools, **_kwargs):
            return self

        async def ainvoke(self, _messages):
            raise TimeoutError

    monkeypatch.setattr(model_probes, "_build_byok_chat_model", lambda **_kwargs: ProbeModel())

    with pytest.raises(server_module.HTTPException) as exc_info:
        await model_probes._verify_model_service_compatibility(
            settings=reset_settings_for_tests(),
            base_url="https://gateway.example/v1",
            adapter_id="openai_chat",
            api_key="secret",
            model_id="model",
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "provider_unavailable"


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
        if any(message.get("role") == "tool" for message in payload["messages"]):
            event = {
                "id": "chatcmpl-final",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "SHEJANE_MODEL_TOOL_LOOP_OK"},
                        "finish_reason": "stop",
                    }
                ],
            }
        else:
            event = {
                "id": "chatcmpl-tool",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-ping",
                                    "type": "function",
                                    "function": {
                                        "name": "shejane_ping",
                                        "arguments": "{}",
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            }
        return httpx.Response(
            200,
            content=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n".encode(),
            headers={"content-type": "text/event-stream"},
        )

    class PatchedClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(model_routes, "_refresh_model_service_models", discover)
    monkeypatch.setattr(model_catalog.httpx, "AsyncClient", PatchedClient)

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
    assert len(requests) == 2
    assert all("tool_choice" not in request for request in requests)


def test_glm_tool_stream_is_shared_by_probe_and_agent_requests(monkeypatch) -> None:
    import langchain_openai

    captured: dict = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChatOpenAI)

    model_probes._build_byok_chat_model(
        settings=reset_settings_for_tests(),
        model_binding={
            "adapter_id": "openai_chat",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "model_id": "glm-5",
            "profile": {},
        },
        model_api_key="secret",
    )

    assert captured["extra_body"] == {"tool_stream": True}


def test_deepseek_chat_uses_the_reasoning_aware_adapter(monkeypatch) -> None:
    import shejane_runtime.llm.deepseek as deepseek_adapter

    captured: list[dict] = []

    class FakeDeepSeekChatOpenAI:
        def __init__(self, **kwargs) -> None:
            captured.append(kwargs)

    monkeypatch.setattr(deepseek_adapter, "DeepSeekChatOpenAI", FakeDeepSeekChatOpenAI)

    for reasoning_mode in ("off", "high", "max"):
        model_probes._build_byok_chat_model(
            settings=reset_settings_for_tests(),
            model_binding={
                "adapter_id": "openai_chat",
                "protocol": "openai_chat_completions",
                "provider_family": "deepseek",
                "reasoning_mode": reasoning_mode,
                "base_url": "https://api.deepseek.com",
                "model_id": "deepseek-v4-flash",
                "profile": {},
            },
            model_api_key="secret",
        )

    assert [item["extra_body"] for item in captured] == [
        {"thinking": {"type": "disabled"}},
        {"thinking": {"type": "enabled"}},
        {"thinking": {"type": "enabled"}},
    ]
    assert "reasoning_effort" not in captured[0]
    assert captured[1]["reasoning_effort"] == "high"
    assert captured[2]["reasoning_effort"] == "max"


def test_openai_responses_protocol_uses_responses_api_without_provider_session_state(
    monkeypatch,
) -> None:
    import langchain_openai

    captured: dict = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChatOpenAI)

    model_probes._build_byok_chat_model(
        settings=reset_settings_for_tests(),
        model_binding={
            "adapter_id": "openai_chat",
            "preset_id": "openai",
            "protocol": "openai_responses",
            "base_url": "https://api.openai.com/v1",
            "model_id": "gpt-5.6",
            "profile": {},
        },
        model_api_key="secret",
    )

    assert captured["use_responses_api"] is True
    assert captured["use_previous_response_id"] is False
    assert captured["output_version"] == "v1"
    assert captured["store"] is False
    assert captured["include"] == [
        "reasoning.encrypted_content",
        "web_search_call.action.sources",
    ]


def test_deepseek_responses_does_not_send_unsupported_include(monkeypatch) -> None:
    import langchain_openai

    captured: dict = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChatOpenAI)

    model_probes._build_byok_chat_model(
        settings=reset_settings_for_tests(),
        model_binding={
            "adapter_id": "openai_chat",
            "preset_id": "deepseek",
            "protocol": "openai_responses",
            "base_url": "https://api.deepseek.com",
            "model_id": "deepseek-v4-flash",
            "profile": {},
        },
        model_api_key="secret",
    )

    assert "include" not in captured
    assert "store" not in captured


@pytest.mark.parametrize(
    ("binding", "expected"),
    [
        (
            {
                "preset_id": "deepseek",
                "protocol": "openai_responses",
                "base_url": "https://api.deepseek.com",
                "model_id": "deepseek-v4-flash",
            },
            ({"type": "web_search"},),
        ),
        (
            {
                "preset_id": "deepseek",
                "protocol": "openai_chat_completions",
                "base_url": "https://api.deepseek.com",
                "model_id": "deepseek-v4-pro",
            },
            (),
        ),
        (
            {
                "preset_id": "openai",
                "protocol": "openai_responses",
                "base_url": "https://api.openai.com/v1",
                "model_id": "gpt-5.6",
            },
            ({"type": "web_search"},),
        ),
        (
            {
                "preset_id": "custom",
                "protocol": "openai_responses",
                "base_url": "https://gateway.example/v1",
                "model_id": "gpt-5.6",
            },
            (),
        ),
    ],
)
def test_hosted_web_search_is_limited_to_documented_provider_bindings(
    binding: dict,
    expected: tuple[dict, ...],
) -> None:
    assert _hosted_tools_for_model_binding(binding) == expected


def test_google_generate_content_protocol_uses_native_google_adapter(monkeypatch) -> None:
    import langchain_google_genai

    captured: dict = {}

    class FakeChatGoogleGenerativeAI:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(
        langchain_google_genai,
        "ChatGoogleGenerativeAI",
        FakeChatGoogleGenerativeAI,
    )

    model_probes._build_byok_chat_model(
        settings=reset_settings_for_tests(),
        model_binding={
            "adapter_id": "google_genai",
            "protocol": "google_generate_content",
            "base_url": "https://generativelanguage.googleapis.com",
            "model_id": "gemini-test",
            "profile": {},
        },
        model_api_key="secret",
    )

    assert captured["model"] == "gemini-test"
    assert captured["client_options"] == "https://generativelanguage.googleapis.com"
    assert captured["retries"] == 0
    assert captured["streaming"] is True
    assert captured["output_version"] == "v1"


def test_manual_compatibility_test_records_result_without_gating_availability(
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
        if kwargs["adapter_id"] != "openai_chat":
            return [], "unavailable"
        return [
            {
                "model_id": "private-model",
                "display_name": "Private Model",
                "source": "discovered",
                "verification": "unverified",
                "recommended": False,
                "tool_calling": False,
                "streaming": False,
                "image_inputs": False,
            }
        ], "ready"

    async def compatible(**_kwargs):
        return None

    monkeypatch.setattr(model_routes, "_refresh_model_service_models", detected)
    monkeypatch.setattr(model_probes, "_verify_model_service_compatibility", compatible)

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
        before = client.get(
            "/v1/models",
            headers={"Authorization": "Bearer tok"},
        ).json()["models"]

        verified = client.post(
            f"/v1/model-services/{connection['id']}/models/private-model/verify",
            headers={"Authorization": "Bearer tok"},
            json={
                "capability": "agent_chat",
                "protocol": "openai_chat_completions",
            },
        )
        catalog = client.get(
            "/v1/models",
            headers={"Authorization": "Bearer tok"},
        ).json()["models"]

    assert before[0]["available"] is True
    assert before[0]["verification"] == "unverified"
    assert verified.status_code == 200
    assert verified.json()["verification"] == "verified"
    assert verified.json()["capabilities"] == [
        {
            "capability": "agent_chat",
            "protocol": "openai_chat_completions",
            "verification": "verified",
        }
    ]
    assert catalog[0]["available"] is True
    assert catalog[0]["verification"] == "verified"


def test_image_generation_model_uses_images_endpoint_and_stays_out_of_agent_catalog(
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

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [{"url": "https://example.test/image.png"}]})

    class PatchedClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(model_routes, "_refresh_model_service_models", detected)
    monkeypatch.setattr(model_catalog.httpx, "AsyncClient", PatchedClient)

    with TestClient(create_app(settings)) as client:
        connection = client.post(
            "/v1/model-services",
            headers={"Authorization": "Bearer tok"},
            json={
                "preset_id": "custom",
                "name": "Gateway",
                "base_url": "https://gateway.example",
                "api_key": "secret",
            },
        ).json()
        client.post(
            f"/v1/model-services/{connection['id']}/models",
            headers={"Authorization": "Bearer tok"},
            json={"model_id": "gpt-image-1"},
        )
        verified = client.post(
            f"/v1/model-services/{connection['id']}/models/gpt-image-1/verify",
            headers={"Authorization": "Bearer tok"},
            json={
                "capability": "image_generation",
                "protocol": "openai_images_generations",
            },
        )
        catalog = client.get(
            "/v1/models",
            headers={"Authorization": "Bearer tok"},
        ).json()["models"]

    assert verified.status_code == 200
    assert verified.json() == {
        "model_id": "gpt-image-1",
        "display_name": "gpt-image-1",
        "capabilities": [
            {
                "capability": "image_generation",
                "protocol": "openai_images_generations",
                "verification": "verified",
            }
        ],
        "source": "manual",
        "verification": "verified",
        "recommended": False,
        "recommended_for": [],
        "tool_calling": False,
        "streaming": False,
        "image_inputs": False,
        "max_input_tokens": None,
        "max_output_tokens": None,
    }
    assert requests[0].url == "https://gateway.example/v1/images/generations"
    assert json.loads(requests[0].content)["model"] == "gpt-image-1"
    assert catalog[0]["available"] is False


@pytest.mark.asyncio
async def test_image_generation_reports_newapi_missing_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={
                "error": {
                    "message": "分组 Codex 下模型 gpt-image-2 的可用渠道不存在（retry）",
                    "type": "new_api_error",
                    "code": "get_channel_failed",
                }
            },
        )

    class PatchedClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(model_catalog.httpx, "AsyncClient", PatchedClient)

    with pytest.raises(server_module.HTTPException) as exc_info:
        await model_probes._verify_model_image_generation(
            settings=reset_settings_for_tests(),
            base_url="https://gateway.example/v1",
            api_key="secret",
            model_id="gpt-image-2",
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "model_unavailable"
    assert "分组 Codex 下模型 gpt-image-2 的可用渠道不存在" in exc_info.value.detail["message"]


def test_model_keeps_multiple_verified_capabilities_and_binds_image_generation(
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

    monkeypatch.setattr(model_routes, "_refresh_model_service_models", detected)
    monkeypatch.setattr(model_routes, "_verify_model_service_capability", compatible)

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
            json={"model_id": "multi-model"},
        )

        for capability, protocol in (
            ("agent_chat", "openai_chat_completions"),
            ("image_generation", "openai_images_generations"),
        ):
            verified = client.post(
                f"/v1/model-services/{connection['id']}/models/multi-model/verify",
                headers={"Authorization": "Bearer tok"},
                json={"capability": capability, "protocol": protocol},
            )
            assert verified.status_code == 200

        model = verified.json()
        assert model["capabilities"] == [
            {
                "capability": "agent_chat",
                "protocol": "openai_chat_completions",
                "verification": "verified",
            },
            {
                "capability": "image_generation",
                "protocol": "openai_images_generations",
                "verification": "verified",
            },
        ]

        bound = client.put(
            "/v1/model-capability-bindings/image_generation",
            headers={"Authorization": "Bearer tok"},
            json={"model_spec": f"local:{connection['id']}:multi-model"},
        )
        listed = client.get(
            "/v1/model-capability-bindings",
            headers={"Authorization": "Bearer tok"},
        )

    assert bound.status_code == 200
    assert bound.json()["capability"] == "image_generation"
    assert bound.json()["model_spec"] == f"local:{connection['id']}:multi-model"
    assert listed.json() == {"bindings": [bound.json()]}


def test_model_verification_rejects_a_concurrently_reconnected_service(
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

    connection_id = ""

    async def reconnect_during_probe(**_kwargs):
        row = await app.state.store.get_model_connection(
            principal_id=server_module.LOCAL_OWNER_PRINCIPAL_ID,
            connection_id=connection_id,
        )
        assert row is not None
        await app.state.store.replace_model_connection_credential(
            principal_id=server_module.LOCAL_OWNER_PRINCIPAL_ID,
            connection_id=connection_id,
            credential_ref=str(row["credential_ref"]),
            base_url=str(row["base_url"]),
            models=model_routes._model_connection_models(row),
            catalog_status=str(row["catalog_status"]),
        )

    monkeypatch.setattr(model_routes, "_refresh_model_service_models", detected)
    monkeypatch.setattr(model_routes, "_verify_model_service_capability", reconnect_during_probe)
    app = create_app(settings)

    with TestClient(app) as client:
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
        connection_id = connection["id"]
        client.post(
            f"/v1/model-services/{connection_id}/models",
            headers={"Authorization": "Bearer tok"},
            json={"model_id": "private-model"},
        )
        verified = client.post(
            f"/v1/model-services/{connection_id}/models/private-model/verify",
            headers={"Authorization": "Bearer tok"},
            json={
                "capability": "agent_chat",
                "protocol": "openai_chat_completions",
            },
        )

    assert verified.status_code == 409
    assert verified.json()["detail"]["code"] == "model_service_changed"
