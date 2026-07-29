from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import shejane_runtime.central_diagnostics as diagnostics_module
from shejane_runtime.central_diagnostics import (
    CentralDiagnosticsManager,
    CentralDiagnosticsUnavailable,
)
from shejane_runtime.config import reset_settings_for_tests
from shejane_runtime.runs import RunCoordinator
from shejane_runtime.server import create_app


class DiagnosticsStore:
    def __init__(self) -> None:
        self.settings: dict[str, Any] = {}
        self.connection = {
            "id": "conn_0123456789abcdef0123456789abcdef",
            "principal_id": "local:owner",
            "preset_id": "shejane-official",
            "region": "official",
            "base_url": "https://cloud.example.test/v1",
            "credential_ref": "keyring:model-service:conn_0123456789abcdef0123456789abcdef",
        }
        self.run = {
            "id": "run_private_identifier",
            "principal_id": "local:owner",
            "status": "failed",
            "created_at": "2026-07-29T02:24:19+00:00",
            "updated_at": "2026-07-29T02:24:20+00:00",
            "completed_at": "2026-07-29T02:24:20+00:00",
            "settings_json": json.dumps(
                {
                    "_model_binding": {
                        "connection_id": "conn_0123456789abcdef0123456789abcdef",
                        "adapter_id": "openai_chat",
                        "model_id": "secret-provider-model-id",
                    }
                }
            ),
        }

    async def get_runtime_settings(self) -> dict[str, Any] | None:
        return {"settings": dict(self.settings), "version": 1, "updated_at": "now"}

    async def patch_runtime_settings(
        self,
        patch: dict[str, Any],
        *,
        initial_settings: dict[str, Any],
    ) -> dict[str, Any]:
        self.settings = {**initial_settings, **self.settings, **patch}
        return {"settings": dict(self.settings), "version": 2, "updated_at": "now"}

    async def get_model_connection(
        self,
        *,
        principal_id: str,
        connection_id: str,
    ) -> dict[str, Any] | None:
        if principal_id == "local:owner" and connection_id == self.connection["id"]:
            return dict(self.connection)
        return None

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        return dict(self.run) if run_id == self.run["id"] else None

    async def list_tool_receipts_for_run(self, run_id: str) -> list[dict[str, Any]]:
        assert run_id == self.run["id"]
        return [
            {
                "tool_name": "read_file",
                "arguments_json": '{"path":"/private/file"}',
                "result_json": '"private tool output"',
            }
        ]

    async def model_usage_summary(self, run_id: str) -> dict[str, int]:
        assert run_id == self.run["id"]
        return {"input_tokens": 120, "output_tokens": 30}


@pytest.mark.asyncio
async def test_opt_in_mints_and_stores_a_separate_diagnostics_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DiagnosticsStore()
    vault: dict[tuple[str, str], str] = {}
    requests: list[httpx.Request] = []

    async def get_inference(*_args: Any) -> str:
        return "inference-secret"

    async def get_diagnostics(principal_id: str, connection_id: str) -> str | None:
        return vault.get((principal_id, connection_id))

    async def set_diagnostics(principal_id: str, connection_id: str, token: str) -> None:
        vault[(principal_id, connection_id)] = token

    async def delete_diagnostics(principal_id: str, connection_id: str) -> None:
        vault.pop((principal_id, connection_id), None)

    async def cloud(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/api/shejane/telemetry/token"
        assert request.headers["authorization"] == "Bearer inference-secret"
        assert request.content == b""
        return httpx.Response(
            201,
            json={
                "token_type": "Bearer",
                "telemetry_token": f"st-{'A' * 43}",
                "expires_at": 1787875200,
            },
        )

    class CloudClient(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(transport=httpx.MockTransport(cloud), **kwargs)

    monkeypatch.setattr(diagnostics_module, "get_model_api_key", get_inference)
    monkeypatch.setattr(diagnostics_module, "get_diagnostics_token", get_diagnostics)
    monkeypatch.setattr(diagnostics_module, "set_diagnostics_token", set_diagnostics)
    monkeypatch.setattr(diagnostics_module, "delete_diagnostics_token", delete_diagnostics)
    monkeypatch.setattr(diagnostics_module.httpx, "AsyncClient", CloudClient)

    manager = CentralDiagnosticsManager(
        store=store, cloud_origin="https://cloud.example.test", app_version="0.1.8"
    )
    status = await manager.configure(
        principal_id="local:owner",
        enabled=True,
        connection_id="conn_0123456789abcdef0123456789abcdef",
        success_sample_rate=0.25,
    )

    assert status == {
        "enabled": True,
        "connection_id": "conn_0123456789abcdef0123456789abcdef",
        "success_sample_rate": 0.25,
        "credential_configured": True,
    }
    assert vault == {("local:owner", "conn_0123456789abcdef0123456789abcdef"): f"st-{'A' * 43}"}
    assert "inference-secret" not in repr(store.settings)
    assert "st-diagnostics-secret" not in repr(store.settings)
    assert len(requests) == 1

    disabled = await manager.configure(
        principal_id="local:owner",
        enabled=False,
        connection_id=None,
        success_sample_rate=0,
    )
    assert disabled["enabled"] is False
    assert vault == {}


@pytest.mark.asyncio
async def test_terminal_upload_contains_only_allowlisted_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DiagnosticsStore()
    store.settings = {
        "central_diagnostics_enabled": True,
        "central_diagnostics_connection_id": "conn_0123456789abcdef0123456789abcdef",
        "central_diagnostics_success_sample_rate": 0.0,
        "central_diagnostics_expires_at": 4_102_444_800,
    }
    captured: list[httpx.Request] = []

    async def get_diagnostics(_principal_id: str, _connection_id: str) -> str:
        return "st-diagnostics-secret"

    async def cloud(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(202)

    class CloudClient(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(transport=httpx.MockTransport(cloud), **kwargs)

    monkeypatch.setattr(diagnostics_module, "get_diagnostics_token", get_diagnostics)
    monkeypatch.setattr(diagnostics_module.httpx, "AsyncClient", CloudClient)
    manager = CentralDiagnosticsManager(
        store=store, cloud_origin="https://cloud.example.test", app_version="0.1.8"
    )

    await manager.submit_terminal(
        run_id="run_private_identifier",
        status="failed",
        payload={
            "error": "private prompt and provider response",
            "category": "provider_unavailable",
            "final_text": "private output",
            "execution": {"attempt_id": "job-id:1"},
        },
    )

    assert len(captured) == 1
    request = captured[0]
    assert request.url.path == "/api/shejane/telemetry/events"
    assert request.headers["authorization"] == "Bearer st-diagnostics-secret"
    event = json.loads(request.content)
    assert event == {
        "schema_version": 1,
        "event_id": event["event_id"],
        "run_id": event["event_id"],
        "attempt_id": "job-id:1",
        "release_version": "0.1.8",
        "platform": diagnostics_module.platform_name(),
        "status": "failed",
        "started_at": "2026-07-29T02:24:19Z",
        "ended_at": "2026-07-29T02:24:20Z",
        "duration_ms": 1000,
        "model_category": "openai_chat",
        "tool_names": ["read_file"],
        "input_tokens": 120,
        "output_tokens": 30,
        "failure_category": "provider_unavailable",
    }
    serialized = request.content.decode()
    for secret in (
        "private prompt",
        "private output",
        "/private/file",
        "secret-provider-model-id",
        "inference-secret",
    ):
        assert secret not in serialized


@pytest.mark.asyncio
async def test_opt_in_rejects_a_noncanonical_cloud_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DiagnosticsStore()

    async def get_inference(*_args: Any) -> str:
        return "inference-secret"

    async def cloud(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "token_type": "Bearer",
                "telemetry_token": "st-short",
                "expires_at": 1787875200,
            },
        )

    class CloudClient(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(transport=httpx.MockTransport(cloud), **kwargs)

    monkeypatch.setattr(diagnostics_module, "get_model_api_key", get_inference)
    monkeypatch.setattr(diagnostics_module.httpx, "AsyncClient", CloudClient)
    manager = CentralDiagnosticsManager(
        store=store, cloud_origin="https://cloud.example.test", app_version="0.1.8"
    )

    with pytest.raises(CentralDiagnosticsUnavailable):
        await manager.configure(
            principal_id="local:owner",
            enabled=True,
            connection_id="conn_0123456789abcdef0123456789abcdef",
            success_sample_rate=0,
        )
    assert store.settings == {}


@pytest.mark.asyncio
async def test_terminal_upload_rejects_path_and_url_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DiagnosticsStore()
    store.settings = {
        "central_diagnostics_enabled": True,
        "central_diagnostics_connection_id": "conn_0123456789abcdef0123456789abcdef",
        "central_diagnostics_success_sample_rate": 0.0,
        "central_diagnostics_expires_at": 4_102_444_800,
    }
    captured: list[httpx.Request] = []

    async def receipts(_run_id: str) -> list[dict[str, str]]:
        return [
            {"tool_name": "/Users/alice/private.txt"},
            {"tool_name": "private.txt"},
        ]

    async def get_diagnostics(_principal_id: str, _connection_id: str) -> str:
        return "st-diagnostics-secret"

    async def cloud(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(202)

    class CloudClient(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(transport=httpx.MockTransport(cloud), **kwargs)

    monkeypatch.setattr(store, "list_tool_receipts_for_run", receipts)
    monkeypatch.setattr(diagnostics_module, "get_diagnostics_token", get_diagnostics)
    monkeypatch.setattr(diagnostics_module.httpx, "AsyncClient", CloudClient)
    manager = CentralDiagnosticsManager(
        store=store, cloud_origin="https://cloud.example.test", app_version="0.1.8"
    )

    await manager.submit_terminal(
        run_id="run_private_identifier",
        status="failed",
        payload={"category": "urn:private"},
    )

    event = json.loads(captured[0].content)
    assert event["tool_names"] == []
    assert event["failure_category"] == "unknown_failure"


@pytest.mark.asyncio
async def test_opt_in_maps_cloud_network_failure_to_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DiagnosticsStore()

    async def get_inference(*_args: Any) -> str:
        return "inference-secret"

    async def cloud(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    class CloudClient(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(transport=httpx.MockTransport(cloud), **kwargs)

    monkeypatch.setattr(diagnostics_module, "get_model_api_key", get_inference)
    monkeypatch.setattr(diagnostics_module.httpx, "AsyncClient", CloudClient)
    manager = CentralDiagnosticsManager(
        store=store, cloud_origin="https://cloud.example.test", app_version="0.1.8"
    )

    with pytest.raises(CentralDiagnosticsUnavailable):
        await manager.configure(
            principal_id="local:owner",
            enabled=True,
            connection_id="conn_0123456789abcdef0123456789abcdef",
            success_sample_rate=0,
        )


@pytest.mark.asyncio
async def test_expired_diagnostics_credential_is_not_reported_as_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DiagnosticsStore()
    store.settings = {
        "central_diagnostics_enabled": True,
        "central_diagnostics_connection_id": "conn_0123456789abcdef0123456789abcdef",
        "central_diagnostics_success_sample_rate": 0.0,
        "central_diagnostics_expires_at": 1,
    }

    async def get_diagnostics(_principal_id: str, _connection_id: str) -> str:
        return "st-diagnostics-secret"

    monkeypatch.setattr(diagnostics_module, "get_diagnostics_token", get_diagnostics)
    manager = CentralDiagnosticsManager(
        store=store, cloud_origin="https://cloud.example.test", app_version="0.1.8"
    )

    status = await manager.status("local:owner")

    assert status["enabled"] is True
    assert status["credential_configured"] is False


@pytest.mark.asyncio
async def test_expired_diagnostics_credential_is_renewed_before_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DiagnosticsStore()
    store.settings = {
        "central_diagnostics_enabled": True,
        "central_diagnostics_connection_id": "conn_0123456789abcdef0123456789abcdef",
        "central_diagnostics_success_sample_rate": 0.0,
        "central_diagnostics_expires_at": 1,
    }
    paths: list[str] = []

    async def get_diagnostics(_principal_id: str, _connection_id: str) -> str:
        return "st-expired"

    async def set_diagnostics(_principal_id: str, _connection_id: str, _token: str) -> None:
        return None

    async def get_inference(*_args: Any) -> str:
        return "inference-secret"

    async def cloud(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/token"):
            return httpx.Response(
                201,
                json={
                    "token_type": "Bearer",
                    "telemetry_token": f"st-{'A' * 43}",
                    "expires_at": 4_102_444_800,
                },
            )
        return httpx.Response(202)

    class CloudClient(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(transport=httpx.MockTransport(cloud), **kwargs)

    monkeypatch.setattr(diagnostics_module, "get_diagnostics_token", get_diagnostics)
    monkeypatch.setattr(diagnostics_module, "set_diagnostics_token", set_diagnostics)
    monkeypatch.setattr(diagnostics_module, "get_model_api_key", get_inference)
    monkeypatch.setattr(diagnostics_module.httpx, "AsyncClient", CloudClient)
    manager = CentralDiagnosticsManager(
        store=store, cloud_origin="https://cloud.example.test", app_version="0.1.8"
    )

    await manager.submit_terminal(
        run_id="run_private_identifier",
        status="failed",
        payload={"category": "provider_unavailable"},
    )

    assert paths == [
        "/api/shejane/telemetry/token",
        "/api/shejane/telemetry/events",
    ]
    assert store.settings["central_diagnostics_expires_at"] == 4_102_444_800


@pytest.mark.asyncio
async def test_concurrent_ingestion_401_renews_once_and_retries_each_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DiagnosticsStore()
    store.settings = {
        "central_diagnostics_enabled": True,
        "central_diagnostics_connection_id": "conn_0123456789abcdef0123456789abcdef",
        "central_diagnostics_success_sample_rate": 0.0,
        "central_diagnostics_expires_at": 4_102_444_800,
    }
    vault = {"token": "st-old"}
    mint_count = 0
    event_tokens: list[str] = []
    both_old_events = asyncio.Event()

    async def get_diagnostics(_principal_id: str, _connection_id: str) -> str:
        return vault["token"]

    async def set_diagnostics(_principal_id: str, _connection_id: str, token: str) -> None:
        vault["token"] = token

    async def get_inference(*_args: Any) -> str:
        return "inference-secret"

    async def cloud(request: httpx.Request) -> httpx.Response:
        nonlocal mint_count
        if request.url.path.endswith("/token"):
            mint_count += 1
            return httpx.Response(
                201,
                json={
                    "token_type": "Bearer",
                    "telemetry_token": f"st-{'A' * 43}",
                    "expires_at": 4_102_444_800,
                },
            )
        token = request.headers["authorization"]
        event_tokens.append(token)
        if token == "Bearer st-old":
            if event_tokens.count("Bearer st-old") == 2:
                both_old_events.set()
            await both_old_events.wait()
            return httpx.Response(401)
        return httpx.Response(202)

    class CloudClient(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(transport=httpx.MockTransport(cloud), **kwargs)

    monkeypatch.setattr(diagnostics_module, "get_diagnostics_token", get_diagnostics)
    monkeypatch.setattr(diagnostics_module, "set_diagnostics_token", set_diagnostics)
    monkeypatch.setattr(diagnostics_module, "get_model_api_key", get_inference)
    monkeypatch.setattr(diagnostics_module.httpx, "AsyncClient", CloudClient)
    manager = CentralDiagnosticsManager(
        store=store, cloud_origin="https://cloud.example.test", app_version="0.1.8"
    )

    await asyncio.gather(
        manager.submit_terminal(
            run_id="run_private_identifier",
            status="failed",
            payload={"category": "transient"},
        ),
        manager.submit_terminal(
            run_id="run_private_identifier",
            status="failed",
            payload={"category": "transient"},
        ),
    )

    assert mint_count == 1
    assert event_tokens.count("Bearer st-old") == 2
    assert event_tokens.count(f"Bearer st-{'A' * 43}") == 2


@pytest.mark.asyncio
async def test_disabling_diagnostics_wins_over_an_inflight_renewal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DiagnosticsStore()
    connection_id = "conn_0123456789abcdef0123456789abcdef"
    store.settings = {
        "central_diagnostics_enabled": True,
        "central_diagnostics_connection_id": connection_id,
        "central_diagnostics_success_sample_rate": 0.0,
        "central_diagnostics_expires_at": 1,
    }
    vault = {"token": "st-old"}
    mint_started = asyncio.Event()
    finish_mint = asyncio.Event()
    uploaded_events = 0

    async def get_diagnostics(_principal_id: str, _connection_id: str) -> str | None:
        return vault.get("token")

    async def set_diagnostics(_principal_id: str, _connection_id: str, token: str) -> None:
        vault["token"] = token

    async def delete_diagnostics(_principal_id: str, _connection_id: str) -> None:
        vault.pop("token", None)

    async def get_inference(*_args: Any) -> str:
        return "inference-secret"

    async def cloud(request: httpx.Request) -> httpx.Response:
        nonlocal uploaded_events
        if request.url.path.endswith("/token"):
            mint_started.set()
            await finish_mint.wait()
            return httpx.Response(
                201,
                json={
                    "token_type": "Bearer",
                    "telemetry_token": f"st-{'A' * 43}",
                    "expires_at": 4_102_444_800,
                },
            )
        uploaded_events += 1
        return httpx.Response(202)

    class CloudClient(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(transport=httpx.MockTransport(cloud), **kwargs)

    monkeypatch.setattr(diagnostics_module, "get_diagnostics_token", get_diagnostics)
    monkeypatch.setattr(diagnostics_module, "set_diagnostics_token", set_diagnostics)
    monkeypatch.setattr(diagnostics_module, "delete_diagnostics_token", delete_diagnostics)
    monkeypatch.setattr(diagnostics_module, "get_model_api_key", get_inference)
    monkeypatch.setattr(diagnostics_module.httpx, "AsyncClient", CloudClient)
    manager = CentralDiagnosticsManager(
        store=store, cloud_origin="https://cloud.example.test", app_version="0.1.8"
    )

    upload = asyncio.create_task(
        manager.submit_terminal(
            run_id="run_private_identifier",
            status="failed",
            payload={"category": "transient"},
        )
    )
    await mint_started.wait()
    disable = asyncio.create_task(
        manager.configure(
            principal_id="local:owner",
            enabled=False,
            connection_id=None,
            success_sample_rate=0,
        )
    )
    await asyncio.sleep(0)
    finish_mint.set()
    await asyncio.gather(upload, disable)

    assert vault == {}
    assert store.settings["central_diagnostics_enabled"] is False
    assert uploaded_events == 0


@pytest.mark.asyncio
async def test_disabling_diagnostics_wins_over_an_inflight_enable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DiagnosticsStore()
    connection_id = "conn_0123456789abcdef0123456789abcdef"
    vault: dict[str, str] = {}
    mint_started = asyncio.Event()
    finish_mint = asyncio.Event()

    async def get_inference(*_args: Any) -> str:
        return "inference-secret"

    async def get_diagnostics(_principal_id: str, _connection_id: str) -> str | None:
        return vault.get("token")

    async def set_diagnostics(_principal_id: str, _connection_id: str, token: str) -> None:
        vault["token"] = token

    async def delete_diagnostics(_principal_id: str, _connection_id: str) -> None:
        vault.pop("token", None)

    async def cloud(_request: httpx.Request) -> httpx.Response:
        mint_started.set()
        await finish_mint.wait()
        return httpx.Response(
            201,
            json={
                "token_type": "Bearer",
                "telemetry_token": f"st-{'A' * 43}",
                "expires_at": 4_102_444_800,
            },
        )

    class CloudClient(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(transport=httpx.MockTransport(cloud), **kwargs)

    monkeypatch.setattr(diagnostics_module, "get_model_api_key", get_inference)
    monkeypatch.setattr(diagnostics_module, "get_diagnostics_token", get_diagnostics)
    monkeypatch.setattr(diagnostics_module, "set_diagnostics_token", set_diagnostics)
    monkeypatch.setattr(diagnostics_module, "delete_diagnostics_token", delete_diagnostics)
    monkeypatch.setattr(diagnostics_module.httpx, "AsyncClient", CloudClient)
    manager = CentralDiagnosticsManager(
        store=store, cloud_origin="https://cloud.example.test", app_version="0.1.8"
    )

    enable = asyncio.create_task(
        manager.configure(
            principal_id="local:owner",
            enabled=True,
            connection_id=connection_id,
            success_sample_rate=0,
        )
    )
    await mint_started.wait()
    disable = asyncio.create_task(
        manager.configure(
            principal_id="local:owner",
            enabled=False,
            connection_id=None,
            success_sample_rate=0,
        )
    )
    await asyncio.sleep(0)
    finish_mint.set()
    await asyncio.gather(enable, disable)

    assert vault == {}
    assert store.settings["central_diagnostics_enabled"] is False


def test_runtime_diagnostics_http_contract_is_explicit_and_never_returns_tokens(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = reset_settings_for_tests(SHEJANE_RUNTIME_TOKEN="tok", data_dir=tmp_path)
    monkeypatch.setattr(RunCoordinator, "start", lambda _self: None)
    captured: dict[str, Any] = {}

    async def status(_self: Any, principal_id: str) -> dict[str, Any]:
        assert principal_id == "local:owner"
        return {
            "enabled": False,
            "connection_id": None,
            "success_sample_rate": 0,
            "credential_configured": False,
        }

    async def configure(_self: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "enabled": True,
            "connection_id": "conn_0123456789abcdef0123456789abcdef",
            "success_sample_rate": 0.25,
            "credential_configured": True,
        }

    monkeypatch.setattr(CentralDiagnosticsManager, "status", status)
    monkeypatch.setattr(CentralDiagnosticsManager, "configure", configure)

    with TestClient(create_app(settings)) as client:
        read = client.get(
            "/v1/shejane/diagnostics",
            headers={"Authorization": "Bearer tok"},
        )
        updated = client.put(
            "/v1/shejane/diagnostics",
            headers={"Authorization": "Bearer tok"},
            json={
                "enabled": True,
                "connection_id": "conn_0123456789abcdef0123456789abcdef",
                "success_sample_rate": 0.25,
            },
        )
        injected = client.put(
            "/v1/shejane/diagnostics",
            headers={"Authorization": "Bearer tok"},
            json={
                "enabled": True,
                "connection_id": "conn_0123456789abcdef0123456789abcdef",
                "success_sample_rate": 0.25,
                "telemetry_token": "attacker-token",
            },
        )

    assert read.status_code == 200
    assert read.json()["enabled"] is False
    assert updated.status_code == 200
    assert updated.json()["credential_configured"] is True
    assert "token" not in repr(updated.json())
    assert captured == {
        "principal_id": "local:owner",
        "enabled": True,
        "connection_id": "conn_0123456789abcdef0123456789abcdef",
        "success_sample_rate": 0.25,
    }
    assert injected.status_code == 422
    assert "attacker-token" not in injected.text
