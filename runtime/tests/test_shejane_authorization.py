from __future__ import annotations

import asyncio
import base64
import hashlib
import sqlite3
import time
from contextlib import asynccontextmanager
from functools import partial
from urllib.parse import parse_qs, urlsplit

import httpx
import keyring
import pytest
from fastapi.testclient import TestClient

import shejane_runtime.server as server_module
import shejane_runtime.shejane_authorization as authorization_module
import shejane_runtime.store.sqlite as sqlite_store_module
from shejane_runtime.auth import LOCAL_OWNER_PRINCIPAL_ID
from shejane_runtime.config import reset_settings_for_tests
from shejane_runtime.runs import RunCoordinator
from shejane_runtime.server import create_app
from shejane_runtime.shejane_authorization import SheJaneAuthorizationManager


def _official_chat_models() -> list[dict[str, object]]:
    return [
        {
            "model_id": "official-chat",
            "display_name": "Official Chat",
            "source": "discovered",
            "verification": "unverified",
            "recommended": True,
            "recommended_for": ["agent_chat"],
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
        }
    ]


def test_official_authorization_cleans_up_when_response_validation_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path,
    )
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)
    credential_vault: dict[str, str] = {}
    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda _service, account, password: credential_vault.__setitem__(account, password),
    )
    monkeypatch.setattr(
        keyring,
        "delete_password",
        lambda _service, account: credential_vault.pop(account, None),
    )

    async def catalog(**_kwargs):
        return [
            {
                "model_id": "official-chat",
                "display_name": "Official Chat",
                "source": "discovered",
                "verification": "unverified",
                "recommended": True,
                "recommended_for": ["agent_chat"],
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
                "max_input_tokens": None,
                "max_output_tokens": None,
            }
        ], "ready"

    async def fail_response(*_args, **_kwargs):
        raise ValueError("response validation failed")

    monkeypatch.setattr(server_module, "_refresh_model_service_models", catalog)
    monkeypatch.setattr(server_module, "_model_service_response", fail_response)

    with TestClient(create_app(settings)) as client:
        with pytest.raises(ValueError, match="response validation failed"):
            client.portal.call(
                partial(
                    server_module._complete_shejane_authorization,
                    client.app,
                    "local:owner",
                    "inference-secret",
                )
            )
        connections = client.portal.call(
            partial(
                client.app.state.store.list_model_connections,
                principal_id="local:owner",
            )
        )

    assert connections == []
    assert credential_vault == {}


@pytest.mark.asyncio
async def test_official_authorization_cleans_completed_keyring_write_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_vault: dict[str, str] = {}
    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda _service, account, password: credential_vault.__setitem__(account, password),
    )
    monkeypatch.setattr(
        keyring,
        "delete_password",
        lambda _service, account: credential_vault.pop(account, None),
    )
    write_started = asyncio.Event()
    release_write = asyncio.Event()
    original_set_model_api_key = server_module.set_model_api_key

    async def delayed_write(*args, **kwargs):
        write_started.set()
        try:
            await release_write.wait()
        except asyncio.CancelledError:
            await original_set_model_api_key(*args, **kwargs)
            raise
        await original_set_model_api_key(*args, **kwargs)

    async def unexpected_catalog(**_kwargs):
        raise AssertionError("canceled authorization must not refresh the catalog")

    monkeypatch.setattr(server_module, "set_model_api_key", delayed_write)
    monkeypatch.setattr(server_module, "_refresh_model_service_models", unexpected_catalog)
    task = asyncio.create_task(
        server_module._complete_shejane_authorization(
            object(),
            LOCAL_OWNER_PRINCIPAL_ID,
            "canceled-token",
        )
    )
    await write_started.wait()
    task.cancel()
    release_write.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert credential_vault == {}


@pytest.mark.asyncio
async def test_official_authorization_cleans_partial_keyring_write_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_vault: dict[str, str] = {}
    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda _service, account, password: credential_vault.__setitem__(account, password),
    )
    monkeypatch.setattr(
        keyring,
        "delete_password",
        lambda _service, account: credential_vault.pop(account, None),
    )
    original_set_model_api_key = server_module.set_model_api_key

    async def failed_write(*args, **kwargs):
        await original_set_model_api_key(*args, **kwargs)
        raise server_module.CredentialStoreError("keyring write outcome is uncertain")

    monkeypatch.setattr(server_module, "set_model_api_key", failed_write)

    with pytest.raises(RuntimeError, match="system credential store is unavailable"):
        await server_module._complete_shejane_authorization(
            object(),
            LOCAL_OWNER_PRINCIPAL_ID,
            "failed-token",
        )

    assert credential_vault == {}


@pytest.mark.asyncio
async def test_loopback_callback_exchanges_pkce_code_without_exposing_token(monkeypatch) -> None:
    exchange: dict[str, str] = {}

    async def cloud(request: httpx.Request) -> httpx.Response:
        exchange["url"] = str(request.url)
        exchange.update(parse_qs(request.content.decode(), strict_parsing=True))
        return httpx.Response(
            200,
            json={"access_token": "inference-secret", "token_type": "Bearer"},
        )

    class CloudClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            super().__init__(transport=httpx.MockTransport(cloud), **kwargs)

    monkeypatch.setattr(authorization_module.httpx, "AsyncClient", CloudClient)

    async def connect(_principal_id: str, token: str) -> dict[str, str]:
        assert token == "inference-secret"
        return {"id": "conn_0123456789abcdef0123456789abcdef"}

    manager = SheJaneAuthorizationManager(
        cloud_origin="https://cloud.example.test",
        app_version="1.2.3",
        complete=connect,
    )
    started = await manager.start("local:owner")
    authorize_url = urlsplit(started["authorization_url"])
    query = parse_qs(authorize_url.query, strict_parsing=True)

    assert (authorize_url.scheme, authorize_url.netloc, authorize_url.path) == (
        "https",
        "cloud.example.test",
        "/shejane/authorize",
    )
    assert query["client_id"] == ["shejane-desktop"]
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["state"][0]) == 43
    assert len(query["code_challenge"][0]) == 43
    assert "code_verifier" not in query

    callback = urlsplit(query["redirect_uri"][0])
    reader, writer = await asyncio.open_connection(callback.hostname, callback.port)
    writer.write(
        (
            f"GET {callback.path}?code=one-time-code&state={query['state'][0]} HTTP/1.1\r\n"
            f"Host: {callback.netloc}\r\nConnection: close\r\n\r\n"
        ).encode()
    )
    await writer.drain()
    browser_response = await reader.read()
    writer.close()
    await writer.wait_closed()

    for _ in range(20):
        status = manager.status(started["authorization_id"], "local:owner")
        if status["status"] != "pending":
            break
        await asyncio.sleep(0)

    assert browser_response.startswith(b"HTTP/1.1 200 OK")
    assert "授权已收到。你可以返回 SheJane。" in browser_response.decode("utf-8")
    assert status == {
        "authorization_id": started["authorization_id"],
        "status": "succeeded",
        "connection": {"id": "conn_0123456789abcdef0123456789abcdef"},
        "error_code": None,
    }
    assert "inference-secret" not in repr(status)
    assert exchange["url"] == "https://cloud.example.test/api/shejane/token"
    assert exchange["grant_type"] == ["authorization_code"]
    assert exchange["client_id"] == ["shejane-desktop"]
    assert exchange["code"] == ["one-time-code"]
    assert exchange["redirect_uri"] == [query["redirect_uri"][0]]
    verifier = exchange["code_verifier"][0]
    assert 43 <= len(verifier) <= 128
    expected_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert query["code_challenge"] == [expected_challenge]

    await manager.close()


@pytest.mark.asyncio
async def test_start_reuses_the_authoritative_pending_flow_for_a_principal() -> None:
    async def unexpected_complete(_principal_id: str, _token: str) -> dict[str, str]:
        raise AssertionError("pending authorization must not complete")

    manager = SheJaneAuthorizationManager(
        cloud_origin="https://cloud.example.test",
        app_version="1.2.3",
        complete=unexpected_complete,
    )

    first, second = await asyncio.gather(
        manager.start("local:owner"),
        manager.start("local:owner"),
    )

    assert second == first
    assert manager.status(first["authorization_id"], "local:owner")["status"] == "pending"
    await manager.close()


def test_authorization_start_uses_compiled_origin_and_rejects_injection(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path,
    )
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)
    monkeypatch.setattr(authorization_module.platform, "node", lambda: "x" * 81)

    with TestClient(create_app(settings)) as client:
        injected = client.post(
            "/v1/model-services/shejane/authorization",
            headers={"Authorization": "Bearer tok"},
            json={"cloud_origin": "https://attacker.example"},
        )
        response = client.post(
            "/v1/model-services/shejane/authorization",
            headers={"Authorization": "Bearer tok"},
        )

    assert injected.status_code == 400
    assert injected.json() == {"detail": "authorization start does not accept configuration"}
    assert response.status_code == 201
    authorization_url = urlsplit(response.json()["authorization_url"])
    assert (
        authorization_url.scheme,
        authorization_url.netloc,
        authorization_url.path,
    ) == ("https", "app.shejane.com", "/shejane/authorize")
    assert parse_qs(authorization_url.query)["device_name"] == ["x" * 80]


def test_runtime_authorization_persists_only_the_official_connection(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path,
    )
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)
    monkeypatch.setattr(server_module, "OFFICIAL_CLOUD_ORIGIN", "https://cloud.example.test")
    credential_vault: dict[str, str] = {}
    monkeypatch.setattr(
        keyring,
        "get_password",
        lambda _service, account: credential_vault.get(account),
    )
    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda _service, account, password: credential_vault.__setitem__(account, password),
    )
    monkeypatch.setattr(
        keyring,
        "delete_password",
        lambda _service, account: credential_vault.pop(account, None),
    )
    catalog_requests = 0

    async def cloud(request: httpx.Request) -> httpx.Response:
        nonlocal catalog_requests
        if request.url.path == "/api/shejane/token":
            return httpx.Response(
                200,
                json={"access_token": "inference-secret", "token_type": "Bearer"},
            )
        assert request.url.path == "/v1/models"
        catalog_requests += 1
        assert list(credential_vault.values()) == ["inference-secret"]
        assert request.headers["authorization"] == "Bearer inference-secret"
        image_models = (
            [
                {
                    "id": "gpt-image-2",
                    "capabilities": ["image_generation", "image_editing"],
                    "recommended_for": ["image_editing"],
                },
                {
                    "id": "gpt-image-2-vip",
                    "capabilities": ["image_generation"],
                    "recommended_for": ["image_generation"] * 5,
                },
            ]
            if catalog_requests == 1
            else [
                {
                    "id": "future-image-model",
                    "capabilities": ["image_generation"],
                    "recommended_for": ["image_generation"],
                }
            ]
        )
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "official-flash", "name": "Official Flash"},
                    {"id": "official-pro", "name": "Official Pro"},
                    *image_models,
                ],
            },
        )

    class CloudClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            super().__init__(transport=httpx.MockTransport(cloud), **kwargs)

    monkeypatch.setattr(authorization_module.httpx, "AsyncClient", CloudClient)

    verified_models: list[str] = []

    async def compatible(**kwargs) -> None:
        assert kwargs["base_url"] == "https://cloud.example.test/v1"
        verified_models.append(kwargs["model_id"])

    monkeypatch.setattr(server_module, "_verify_model_service_compatibility", compatible)

    with TestClient(create_app(settings)) as client:
        started = client.post(
            "/v1/model-services/shejane/authorization",
            headers={"Authorization": "Bearer tok"},
        )
        assert started.status_code == 201
        authorization = started.json()
        query = parse_qs(urlsplit(authorization["authorization_url"]).query)
        callback = query["redirect_uri"][0]
        browser = httpx.get(
            f"{callback}?code=one-time-code&state={query['state'][0]}",
            timeout=5,
        )
        assert browser.status_code == 200

        for _ in range(50):
            status = client.get(
                f"/v1/model-services/shejane/authorization/{authorization['authorization_id']}",
                headers={"Authorization": "Bearer tok"},
            )
            if status.json()["status"] != "pending":
                break
            time.sleep(0.01)
        repeated = client.get(
            f"/v1/model-services/shejane/authorization/{authorization['authorization_id']}",
            headers={"Authorization": "Bearer tok"},
        )
        services = client.get(
            "/v1/model-services",
            headers={"Authorization": "Bearer tok"},
        )
        connection_id = status.json()["connection"]["id"]
        with sqlite3.connect(settings.runtime_db_path) as database:
            persisted_default_bindings = database.execute(
                "SELECT capability, model_id FROM model_capability_bindings ORDER BY capability"
            ).fetchall()
        default_bindings = client.get(
            "/v1/model-capability-bindings",
            headers={"Authorization": "Bearer tok"},
        )
        selected_binding = client.put(
            "/v1/model-capability-bindings/image_generation",
            headers={"Authorization": "Bearer tok"},
            json={"model_spec": f"local:{connection_id}:gpt-image-2"},
        )
        preserved_bindings = client.get(
            "/v1/model-capability-bindings",
            headers={"Authorization": "Bearer tok"},
        )
        replaced = client.put(
            f"/v1/model-services/{connection_id}/credential",
            headers={"Authorization": "Bearer tok"},
            json={"api_key": "attacker-key", "base_url": "https://attacker.example/v1"},
        )
        imported = client.post(
            "/v1/model-services/import",
            headers={"Authorization": "Bearer tok"},
            json={
                "id": "conn_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "preset_id": "shejane-official",
                "name": "spoofed",
                "region": "cn",
                "adapter_id": "openai_chat",
                "base_url": "https://attacker.example/v1",
                "models": [],
            },
        )
        verified_before_manual_test = list(verified_models)
        manual = client.post(
            f"/v1/model-services/{connection_id}/models/official-flash/verify",
            headers={"Authorization": "Bearer tok"},
            json={
                "capability": "agent_chat",
                "protocol": "openai_chat_completions",
            },
        )

    with TestClient(create_app(settings)) as restarted:
        restarted_services = restarted.get(
            "/v1/model-services",
            headers={"Authorization": "Bearer tok"},
        )
        restarted_refresh = restarted.post(
            f"/v1/model-services/{connection_id}/refresh",
            headers={"Authorization": "Bearer tok"},
        )

    assert status.status_code == 200
    payload = status.json()
    assert payload["status"] == "succeeded"
    assert payload["connection"]["preset_id"] == "shejane-official"
    assert payload["connection"]["region"] == "official"
    assert payload["connection"]["base_url"] == "https://cloud.example.test/v1"
    assert verified_before_manual_test == []
    assert verified_models == ["official-flash"]
    assert {
        model["model_id"]: model["verification"] for model in payload["connection"]["models"]
    } == {
        "official-flash": "unverified",
        "official-pro": "unverified",
        "gpt-image-2": "verified",
        "gpt-image-2-vip": "verified",
    }
    assert {
        model["model_id"]: model["capabilities"]
        for model in payload["connection"]["models"]
        if model["model_id"].startswith("gpt-image")
    } == {
        "gpt-image-2": [
            {
                "capability": "image_generation",
                "protocol": "openai_images_generations",
                "verification": "verified",
            },
            {
                "capability": "image_editing",
                "protocol": "openai_images_edits",
                "verification": "verified",
            },
        ],
        "gpt-image-2-vip": [
            {
                "capability": "image_generation",
                "protocol": "openai_images_generations",
                "verification": "verified",
            }
        ],
    }
    assert manual.status_code == 200
    assert manual.json()["verification"] == "verified"
    assert repeated.json() == payload
    assert services.json()["services"] == [payload["connection"]]
    assert persisted_default_bindings == [
        ("image_editing", "gpt-image-2"),
        ("image_generation", "gpt-image-2-vip"),
    ]
    assert {
        binding["capability"]: {
            key: binding[key] for key in ("model_spec", "model_id", "status", "revision")
        }
        for binding in default_bindings.json()["bindings"]
    } == {
        "image_generation": {
            "model_spec": f"local:{connection_id}:gpt-image-2-vip",
            "model_id": "gpt-image-2-vip",
            "status": "ready",
            "revision": 1,
        },
        "image_editing": {
            "model_spec": f"local:{connection_id}:gpt-image-2",
            "model_id": "gpt-image-2",
            "status": "ready",
            "revision": 1,
        },
    }
    assert selected_binding.status_code == 200
    preserved_generation = next(
        binding
        for binding in preserved_bindings.json()["bindings"]
        if binding["capability"] == "image_generation"
    )
    assert preserved_generation["model_id"] == "gpt-image-2"
    assert preserved_generation["revision"] == 2
    assert list(credential_vault.values()) == ["inference-secret"]
    assert replaced.status_code == 400
    assert imported.status_code == 400
    assert {
        model["model_id"]: model["verification"]
        for model in restarted_services.json()["services"][0]["models"]
    } == {
        "official-flash": "verified",
        "official-pro": "unverified",
        "gpt-image-2": "verified",
        "gpt-image-2-vip": "verified",
    }
    assert restarted_refresh.status_code == 200
    assert {
        model["model_id"]: model["verification"] for model in restarted_refresh.json()["models"]
    } == {
        "official-flash": "verified",
        "official-pro": "unverified",
        "future-image-model": "verified",
    }
    assert "inference-secret" not in repr(payload)
    assert "inference-secret" not in settings.runtime_db_path.read_bytes().decode(errors="ignore")
    with sqlite3.connect(settings.runtime_db_path) as database:
        assert database.execute("SELECT COUNT(*) FROM model_connections").fetchone() == (1,)


@pytest.mark.asyncio
async def test_state_mismatch_terminates_authorization_without_exchange(monkeypatch) -> None:
    class UnexpectedClient:
        def __init__(self, **_kwargs) -> None:
            raise AssertionError("state mismatch must not call Cloud")

    monkeypatch.setattr(authorization_module.httpx, "AsyncClient", UnexpectedClient)

    async def unexpected_complete(_principal_id: str, _token: str) -> dict[str, str]:
        raise AssertionError("state mismatch must not create a connection")

    manager = SheJaneAuthorizationManager(
        cloud_origin="https://cloud.example.test",
        app_version="1.2.3",
        complete=unexpected_complete,
    )
    started = await manager.start("local:owner")
    query = parse_qs(urlsplit(started["authorization_url"]).query)
    callback = urlsplit(query["redirect_uri"][0])
    reader, writer = await asyncio.open_connection(callback.hostname, callback.port)
    writer.write(
        (
            f"GET {callback.path}?code=intercepted&state=wrong HTTP/1.1\r\n"
            f"Host: {callback.netloc}\r\nConnection: close\r\n\r\n"
        ).encode()
    )
    await writer.drain()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()

    assert response.startswith(b"HTTP/1.1 400 Bad Request")
    assert manager.status(started["authorization_id"], "local:owner")["error_code"] == (
        "state_mismatch"
    )
    await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "callback_query",
    [
        "error=server_error",
        "error=temporarily_unavailable",
        "error=unknown_error",
        "code=",
    ],
)
async def test_callback_error_or_empty_code_fails_and_closes_listener(
    monkeypatch: pytest.MonkeyPatch,
    callback_query: str,
) -> None:
    class UnexpectedClient:
        def __init__(self, **_kwargs) -> None:
            raise AssertionError("failed callback must not call Cloud")

    monkeypatch.setattr(authorization_module.httpx, "AsyncClient", UnexpectedClient)

    async def unexpected_complete(_principal_id: str, _token: str) -> dict[str, str]:
        raise AssertionError("failed callback must not create a connection")

    manager = SheJaneAuthorizationManager(
        cloud_origin="https://cloud.example.test",
        app_version="1.2.3",
        complete=unexpected_complete,
    )
    started = await manager.start("local:owner")
    query = parse_qs(urlsplit(started["authorization_url"]).query)
    callback = urlsplit(query["redirect_uri"][0])
    reader, writer = await asyncio.open_connection(callback.hostname, callback.port)
    writer.write(
        (
            f"GET {callback.path}?{callback_query}&state={query['state'][0]} HTTP/1.1\r\n"
            f"Host: {callback.netloc}\r\nConnection: close\r\n\r\n"
        ).encode()
    )
    await writer.drain()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()

    status = manager.status(started["authorization_id"], "local:owner")
    assert response.startswith(b"HTTP/1.1 400 Bad Request")
    assert status["status"] == "failed"
    assert status["error_code"]
    with pytest.raises(OSError):
        await asyncio.open_connection(callback.hostname, callback.port)
    await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("slow_target_kind", ["same_callback", "browser_favicon"])
async def test_concurrent_callback_cannot_invalidate_claimed_exchange(
    monkeypatch,
    slow_target_kind: str,
) -> None:
    exchange_count = 0

    async def cloud(_request: httpx.Request) -> httpx.Response:
        nonlocal exchange_count
        exchange_count += 1
        return httpx.Response(
            200,
            json={"access_token": "inference-secret", "token_type": "Bearer"},
        )

    class CloudClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            super().__init__(transport=httpx.MockTransport(cloud), **kwargs)

    monkeypatch.setattr(authorization_module.httpx, "AsyncClient", CloudClient)

    async def connect(_principal_id: str, _token: str) -> dict[str, str]:
        return {"id": "conn_0123456789abcdef0123456789abcdef"}

    manager = SheJaneAuthorizationManager(
        cloud_origin="https://cloud.example.test",
        app_version="1.2.3",
        complete=connect,
    )
    started = await manager.start("local:owner")
    query = parse_qs(urlsplit(started["authorization_url"]).query)
    callback = urlsplit(query["redirect_uri"][0])
    target = f"{callback.path}?code=one-time-code&state={query['state'][0]}"

    slow_reader, slow_writer = await asyncio.open_connection(callback.hostname, callback.port)
    slow_target = target if slow_target_kind == "same_callback" else "/favicon.ico"
    slow_writer.write(
        (f"GET {slow_target} HTTP/1.1\r\nHost: {callback.netloc}\r\nConnection: close\r\n").encode()
    )
    await slow_writer.drain()
    fast_reader, fast_writer = await asyncio.open_connection(callback.hostname, callback.port)
    fast_writer.write(
        (f"GET {target} HTTP/1.1\r\nHost: {callback.netloc}\r\nConnection: close\r\n\r\n").encode()
    )
    await fast_writer.drain()
    assert (await fast_reader.read()).startswith(b"HTTP/1.1 200 OK")
    fast_writer.close()
    await fast_writer.wait_closed()

    slow_writer.write(b"\r\n")
    await slow_writer.drain()
    assert (await slow_reader.read()).startswith(b"HTTP/1.1 410 Gone")
    slow_writer.close()
    await slow_writer.wait_closed()

    for _ in range(20):
        status = manager.status(started["authorization_id"], "local:owner")
        if status["status"] != "pending":
            break
        await asyncio.sleep(0)
    assert status["status"] == "succeeded"
    assert exchange_count == 1
    await manager.close()


def test_official_reauthorization_replaces_connection_and_moves_bindings(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path,
    )
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)
    credential_vault: dict[str, str] = {}
    monkeypatch.setattr(
        keyring,
        "get_password",
        lambda _service, account: credential_vault.get(account),
    )
    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda _service, account, password: credential_vault.__setitem__(account, password),
    )
    monkeypatch.setattr(
        keyring,
        "delete_password",
        lambda _service, account: credential_vault.pop(account, None),
    )

    async def catalog(**_kwargs):
        return [
            {
                "model_id": "official-image",
                "display_name": "Official Image",
                "source": "discovered",
                "verification": "verified",
                "recommended": True,
                "recommended_for": ["image_generation", "image_editing"],
                "capabilities": [
                    {
                        "capability": "image_generation",
                        "protocol": "openai_images_generations",
                        "verification": "verified",
                    },
                    {
                        "capability": "image_editing",
                        "protocol": "openai_images_edits",
                        "verification": "verified",
                    },
                ],
                "tool_calling": False,
                "streaming": False,
                "image_inputs": False,
            }
        ], "ready"

    monkeypatch.setattr(server_module, "_refresh_model_service_models", catalog)

    with TestClient(create_app(settings)) as client:
        first = client.portal.call(
            partial(
                server_module._complete_shejane_authorization,
                client.app,
                LOCAL_OWNER_PRINCIPAL_ID,
                "first-token",
            )
        )
        second = client.portal.call(
            partial(
                server_module._complete_shejane_authorization,
                client.app,
                LOCAL_OWNER_PRINCIPAL_ID,
                "second-token",
            )
        )
        connections = client.portal.call(
            partial(
                client.app.state.store.list_model_connections,
                principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            )
        )
        bindings = client.portal.call(
            partial(
                client.app.state.store.list_model_capability_bindings,
                principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            )
        )

    assert first["id"] != second["id"]
    assert [connection["id"] for connection in connections] == [second["id"]]
    assert {binding["connection_id"] for binding in bindings} == {second["id"]}
    assert {binding["model_id"] for binding in bindings} == {"official-image"}
    assert list(credential_vault.values()) == ["second-token"]


def test_official_reauthorization_retains_last_known_good_catalog_when_refresh_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path,
    )
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)
    credential_vault: dict[str, str] = {}
    monkeypatch.setattr(
        keyring, "get_password", lambda _service, account: credential_vault.get(account)
    )
    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda _service, account, password: credential_vault.__setitem__(account, password),
    )
    monkeypatch.setattr(
        keyring,
        "delete_password",
        lambda _service, account: credential_vault.pop(account, None),
    )
    calls = 0
    last_known_good = [
        {
            "model_id": "official-image",
            "display_name": "Official Image",
            "source": "discovered",
            "verification": "verified",
            "recommended": True,
            "recommended_for": ["image_generation"],
            "capabilities": [
                {
                    "capability": "image_generation",
                    "protocol": "openai_images_generations",
                    "verification": "verified",
                }
            ],
            "tool_calling": False,
            "streaming": False,
            "image_inputs": False,
        }
    ]

    async def catalog(**_kwargs):
        nonlocal calls
        calls += 1
        return (last_known_good, "ready") if calls == 1 else ([], "unavailable")

    monkeypatch.setattr(server_module, "_refresh_model_service_models", catalog)

    with TestClient(create_app(settings)) as client:
        first = client.portal.call(
            partial(
                server_module._complete_shejane_authorization,
                client.app,
                LOCAL_OWNER_PRINCIPAL_ID,
                "first-token",
            )
        )
        second = client.portal.call(
            partial(
                server_module._complete_shejane_authorization,
                client.app,
                LOCAL_OWNER_PRINCIPAL_ID,
                "second-token",
            )
        )
        bindings = client.portal.call(
            partial(
                client.app.state.store.list_model_capability_bindings,
                principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            )
        )

    assert first["models"] == second["models"]
    assert second["catalog_status"] == "stale"
    assert {binding["connection_id"] for binding in bindings} == {second["id"]}
    assert list(credential_vault.values()) == ["second-token"]


def test_official_replacement_fences_old_connections_in_deterministic_order(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path,
    )
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)
    monkeypatch.setattr(keyring, "get_password", lambda _service, _account: "old-token")
    monkeypatch.setattr(keyring, "set_password", lambda _service, _account, _password: None)
    monkeypatch.setattr(keyring, "delete_password", lambda _service, _account: None)

    async def catalog(**_kwargs):
        return [
            {
                "model_id": "official-chat",
                "display_name": "Official Chat",
                "source": "discovered",
                "verification": "unverified",
                "recommended": True,
                "recommended_for": ["agent_chat"],
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
            }
        ], "ready"

    monkeypatch.setattr(server_module, "_refresh_model_service_models", catalog)
    events: list[str] = []

    with TestClient(create_app(settings)) as client:
        for connection_id in ("conn_z", "conn_a"):
            client.portal.call(
                partial(
                    client.app.state.store.create_model_connection,
                    principal_id=LOCAL_OWNER_PRINCIPAL_ID,
                    connection_id=connection_id,
                    preset_id="shejane-official",
                    name="Old Official",
                    region="official",
                    adapter_id="openai_chat",
                    base_url="https://app.shejane.com/v1",
                    requires_api_key=True,
                    credential_ref=f"keyring:model-service:{connection_id}",
                    models=[],
                    catalog_status="ready",
                )
            )

        @asynccontextmanager
        async def mutation(*, principal_id: str, connection_id: str):
            assert principal_id == LOCAL_OWNER_PRINCIPAL_ID
            events.append(f"enter:{connection_id}")
            try:
                yield
            finally:
                events.append(f"exit:{connection_id}")

        monkeypatch.setattr(client.app.state.coordinator, "model_connection_mutation", mutation)
        client.portal.call(
            partial(
                server_module._complete_shejane_authorization,
                client.app,
                LOCAL_OWNER_PRINCIPAL_ID,
                "new-token",
            )
        )

    assert events == ["enter:conn_a", "enter:conn_z", "exit:conn_z", "exit:conn_a"]


def test_official_replacement_resolves_cancellation_during_commit(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path,
    )
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)
    credential_vault: dict[str, str] = {}
    monkeypatch.setattr(
        keyring, "get_password", lambda _service, account: credential_vault.get(account)
    )
    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda _service, account, password: credential_vault.__setitem__(account, password),
    )
    monkeypatch.setattr(
        keyring,
        "delete_password",
        lambda _service, account: credential_vault.pop(account, None),
    )

    async def catalog(**_kwargs):
        return _official_chat_models(), "ready"

    monkeypatch.setattr(server_module, "_refresh_model_service_models", catalog)

    with TestClient(create_app(settings)) as client:
        first = client.portal.call(
            partial(
                server_module._complete_shejane_authorization,
                client.app,
                LOCAL_OWNER_PRINCIPAL_ID,
                "first-token",
            )
        )
        commit_started = asyncio.Event()
        release_commit = asyncio.Event()
        original_commit = sqlite_store_module.aiosqlite.Connection.commit

        async def delayed_commit(connection):
            commit_started.set()
            await release_commit.wait()
            await original_commit(connection)

        monkeypatch.setattr(sqlite_store_module.aiosqlite.Connection, "commit", delayed_commit)

        async def cancel_during_commit():
            task = asyncio.create_task(
                server_module._complete_shejane_authorization(
                    client.app,
                    LOCAL_OWNER_PRINCIPAL_ID,
                    "second-token",
                )
            )
            await commit_started.wait()
            await asyncio.sleep(0)
            task.cancel()
            release_commit.set()
            return await task

        second = client.portal.call(cancel_during_commit)
        connections = client.portal.call(
            partial(
                client.app.state.store.list_model_connections,
                principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            )
        )

    assert first["id"] != second["id"]
    assert [connection["id"] for connection in connections] == [second["id"]]
    assert list(credential_vault.values()) == ["second-token"]


def test_official_authorization_auto_binds_only_the_new_readable_connection(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path,
    )
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)
    credential_vault: dict[str, str] = {}
    monkeypatch.setattr(
        keyring, "get_password", lambda _service, account: credential_vault.get(account)
    )
    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda _service, account, password: credential_vault.__setitem__(account, password),
    )
    monkeypatch.setattr(
        keyring,
        "delete_password",
        lambda _service, account: credential_vault.pop(account, None),
    )

    old_models = [
        {
            "model_id": "old-image",
            "display_name": "Old Image",
            "source": "discovered",
            "verification": "verified",
            "recommended": True,
            "recommended_for": ["image_generation"],
            "capabilities": [
                {
                    "capability": "image_generation",
                    "protocol": "openai_images_generations",
                    "verification": "verified",
                }
            ],
        }
    ]
    new_models = [
        {
            "model_id": "new-image",
            "display_name": "New Image",
            "source": "discovered",
            "verification": "verified",
            "recommended": True,
            "recommended_for": ["image_generation"],
            "capabilities": [
                {
                    "capability": "image_generation",
                    "protocol": "openai_images_generations",
                    "verification": "verified",
                }
            ],
        }
    ]

    async def catalog(**_kwargs):
        return new_models, "ready"

    monkeypatch.setattr(server_module, "_refresh_model_service_models", catalog)

    with TestClient(create_app(settings)) as client:
        client.portal.call(
            partial(
                client.app.state.store.create_model_connection,
                principal_id=LOCAL_OWNER_PRINCIPAL_ID,
                connection_id="conn_unreadable",
                preset_id="shejane-official",
                name="Old Official",
                region="official",
                adapter_id="openai_chat",
                base_url="https://app.shejane.com/v1",
                requires_api_key=True,
                credential_ref="keyring:model-service:conn_unreadable",
                models=old_models,
                catalog_status="ready",
            )
        )
        authorized = client.portal.call(
            partial(
                server_module._complete_shejane_authorization,
                client.app,
                LOCAL_OWNER_PRINCIPAL_ID,
                "readable-token",
            )
        )
        connections = client.portal.call(
            partial(
                client.app.state.store.list_model_connections,
                principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            )
        )
        binding = client.portal.call(
            partial(
                client.app.state.store.get_model_capability_binding,
                principal_id=LOCAL_OWNER_PRINCIPAL_ID,
                capability="image_generation",
            )
        )

    assert [connection["id"] for connection in connections] == [authorized["id"]]
    assert binding is not None
    assert binding["connection_id"] == authorized["id"]
    assert binding["model_id"] == "new-image"


def test_concurrent_official_authorizations_leave_one_connection_and_credential(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path,
    )
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)
    credential_vault: dict[str, str] = {}
    monkeypatch.setattr(
        keyring, "get_password", lambda _service, account: credential_vault.get(account)
    )
    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda _service, account, password: credential_vault.__setitem__(account, password),
    )
    monkeypatch.setattr(
        keyring,
        "delete_password",
        lambda _service, account: credential_vault.pop(account, None),
    )

    async def catalog(**_kwargs):
        await asyncio.sleep(0)
        return _official_chat_models(), "ready"

    monkeypatch.setattr(server_module, "_refresh_model_service_models", catalog)

    with TestClient(create_app(settings)) as client:

        async def authorize_twice():
            return await asyncio.gather(
                server_module._complete_shejane_authorization(
                    client.app,
                    LOCAL_OWNER_PRINCIPAL_ID,
                    "concurrent-token-1",
                ),
                server_module._complete_shejane_authorization(
                    client.app,
                    LOCAL_OWNER_PRINCIPAL_ID,
                    "concurrent-token-2",
                ),
            )

        client.portal.call(authorize_twice)
        connections = client.portal.call(
            partial(
                client.app.state.store.list_model_connections,
                principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            )
        )

    assert len(connections) == 1
    assert len(credential_vault) == 1
    assert next(iter(credential_vault.values())) in {
        "concurrent-token-1",
        "concurrent-token-2",
    }


@pytest.mark.asyncio
async def test_callback_rejects_wrong_path_host_and_query_shape(monkeypatch) -> None:
    class UnexpectedClient:
        def __init__(self, **_kwargs) -> None:
            raise AssertionError("invalid callback must not call Cloud")

    monkeypatch.setattr(authorization_module.httpx, "AsyncClient", UnexpectedClient)

    async def unexpected_complete(_principal_id: str, _token: str) -> dict[str, str]:
        raise AssertionError("invalid callback must not create a connection")

    for case in ("path", "host", "extra", "duplicate", "missing_state"):
        manager = SheJaneAuthorizationManager(
            cloud_origin="https://cloud.example.test",
            app_version="1.2.3",
            complete=unexpected_complete,
        )
        started = await manager.start("local:owner")
        query = parse_qs(urlsplit(started["authorization_url"]).query)
        callback = urlsplit(query["redirect_uri"][0])
        state = query["state"][0]
        path = callback.path
        host = callback.netloc
        callback_query = f"code=code&state={state}"
        if case == "path":
            path = "/wrong"
        elif case == "host":
            host = f"localhost:{callback.port}"
        elif case == "extra":
            callback_query += "&extra=value"
        elif case == "duplicate":
            callback_query += f"&state={state}"
        elif case == "missing_state":
            callback_query = "code=code"

        reader, writer = await asyncio.open_connection(callback.hostname, callback.port)
        writer.write(
            (
                f"GET {path}?{callback_query} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
            ).encode()
        )
        await writer.drain()
        assert (await reader.read()).startswith(b"HTTP/1.1 400 Bad Request")
        writer.close()
        await writer.wait_closed()
        assert manager.status(started["authorization_id"], "local:owner")["status"] == ("failed")
        await manager.close()


@pytest.mark.asyncio
async def test_access_denial_and_timeout_are_terminal() -> None:
    async def unexpected_complete(_principal_id: str, _token: str) -> dict[str, str]:
        raise AssertionError("terminal flow must not create a connection")

    denied = SheJaneAuthorizationManager(
        cloud_origin="https://cloud.example.test",
        app_version="1.2.3",
        complete=unexpected_complete,
    )
    started = await denied.start("local:owner")
    query = parse_qs(urlsplit(started["authorization_url"]).query)
    callback = urlsplit(query["redirect_uri"][0])
    reader, writer = await asyncio.open_connection(callback.hostname, callback.port)
    writer.write(
        (
            f"GET {callback.path}?error=access_denied&state={query['state'][0]} HTTP/1.1\r\n"
            f"Host: {callback.netloc}\r\nConnection: close\r\n\r\n"
        ).encode()
    )
    await writer.drain()
    await reader.read()
    writer.close()
    await writer.wait_closed()
    assert denied.status(started["authorization_id"], "local:owner")["status"] == "denied"
    await denied.close()

    expired = SheJaneAuthorizationManager(
        cloud_origin="https://cloud.example.test",
        app_version="1.2.3",
        complete=unexpected_complete,
        ttl_seconds=0.01,
    )
    started = await expired.start("local:owner")
    await asyncio.sleep(0.02)
    assert expired.status(started["authorization_id"], "local:owner")["status"] == "expired"
    await expired.close()


@pytest.mark.asyncio
async def test_lost_exchange_response_fails_without_retry(monkeypatch) -> None:
    exchange_count = 0

    async def cloud(request: httpx.Request) -> httpx.Response:
        nonlocal exchange_count
        exchange_count += 1
        raise httpx.ReadError("response lost", request=request)

    class CloudClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            super().__init__(transport=httpx.MockTransport(cloud), **kwargs)

    monkeypatch.setattr(authorization_module.httpx, "AsyncClient", CloudClient)

    async def unexpected_complete(_principal_id: str, _token: str) -> dict[str, str]:
        raise AssertionError("a lost exchange response must not create a connection")

    manager = SheJaneAuthorizationManager(
        cloud_origin="https://cloud.example.test",
        app_version="1.2.3",
        complete=unexpected_complete,
    )
    started = await manager.start("local:owner")
    query = parse_qs(urlsplit(started["authorization_url"]).query)
    callback = urlsplit(query["redirect_uri"][0])
    reader, writer = await asyncio.open_connection(callback.hostname, callback.port)
    writer.write(
        (
            f"GET {callback.path}?code=consumed-code&state={query['state'][0]} HTTP/1.1\r\n"
            f"Host: {callback.netloc}\r\nConnection: close\r\n\r\n"
        ).encode()
    )
    await writer.drain()
    assert (await reader.read()).startswith(b"HTTP/1.1 200 OK")
    writer.close()
    await writer.wait_closed()

    for _ in range(20):
        status = manager.status(started["authorization_id"], "local:owner")
        if status["status"] != "pending":
            break
        await asyncio.sleep(0)
    assert status["status"] == "failed"
    assert status["error_code"] == "token_exchange_failed"
    assert exchange_count == 1
    await manager.close()
