from __future__ import annotations

import asyncio
import base64
import hashlib
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import keyring
import pytest
from fastapi.testclient import TestClient

import shejane_runtime.server as server_module
import shejane_runtime.shejane_authorization as authorization_module
from shejane_runtime.config import reset_settings_for_tests
from shejane_runtime.runs import RunCoordinator
from shejane_runtime.server import create_app
from shejane_runtime.shejane_authorization import SheJaneAuthorizationManager


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
                    "capabilities": ["image_generation"],
                    "recommended_for": ["image_generation"],
                },
                {
                    "id": "gpt-image-2-vip",
                    "capabilities": ["image_generation"],
                    "recommended_for": ["image_generation"],
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

    assert verified_models == ["official-flash", "official-pro"]

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
    assert verified_models == [
        "official-flash",
        "official-pro",
        "official-flash",
        "official-pro",
    ]
    assert {
        model["model_id"]: model["verification"] for model in payload["connection"]["models"]
    } == {
        "official-flash": "verified",
        "official-pro": "verified",
        "gpt-image-2": "verified",
        "gpt-image-2-vip": "verified",
    }
    assert repeated.json() == payload
    assert services.json()["services"] == [payload["connection"]]
    assert list(credential_vault.values()) == ["inference-secret"]
    assert replaced.status_code == 400
    assert imported.status_code == 400
    assert restarted_services.json()["services"] == [payload["connection"]]
    assert restarted_refresh.status_code == 200
    assert {
        model["model_id"]: model["verification"] for model in restarted_refresh.json()["models"]
    } == {
        "official-flash": "verified",
        "official-pro": "verified",
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
