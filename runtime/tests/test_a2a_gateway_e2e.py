from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from shejane_runtime.a2a_gateway.app import GatewayConfig, create_gateway_app
from shejane_runtime.a2a_gateway.runtime_client import RuntimeHTTPClient
from shejane_runtime.config import reset_settings_for_tests
from shejane_runtime.server import create_app


def _headers(token: str) -> dict[str, str]:
    return {"A2A-Version": "1.0", "Authorization": f"Bearer {token}"}


def _send_request(message_id: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": "send",
        "method": "SendStreamingMessage",
        "params": {
            "tenant": "contract",
            "message": {
                "messageId": message_id,
                "role": "ROLE_USER",
                "parts": [{"text": "Say hello.", "mediaType": "text/plain"}],
            },
            "configuration": {"returnImmediately": False},
        },
    }


def _sse_results(response: httpx.Response) -> list[dict[str, object]]:
    return [
        json.loads(line[5:].strip())["result"]
        for line in response.text.splitlines()
        if line.startswith("data:")
    ]


@pytest.mark.asyncio
async def test_gateway_drives_real_runtime_and_replay_creates_one_run(
    tmp_path: Path,
) -> None:
    runtime_settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="runtime-token",
        SHEJANE_FAKE_LLM=True,
        data_dir=tmp_path / "runtime",
    )
    runtime_app = create_app(runtime_settings)
    async with runtime_app.router.lifespan_context(runtime_app):
        runtime = RuntimeHTTPClient(
            base_url="http://runtime.test",
            token="runtime-token",
            transport=httpx.ASGITransport(app=runtime_app),
        )
        gateway_app = create_gateway_app(
            GatewayConfig(
                db_path=tmp_path / "gateway.db",
                runtime_base_url="http://127.0.0.1:17371",
                runtime_token="runtime-token",
                public_base_url="https://gateway.test",
                push_credential_key=b"k" * 32,
            ),
            runtime_client=runtime,
        )
        try:
            async with gateway_app.router.lifespan_context(gateway_app):
                _peer, token = await gateway_app.state.gateway_store.create_peer(
                    name="Contract client",
                    tenant="contract",
                    scopes=["tasks.create", "tasks.read", "tasks.cancel"],
                    runtime_model="local:test:model",
                    runtime_workspace_path=None,
                    permission_mode="ask",
                    push_origins=[],
                    expires_at=None,
                )
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=gateway_app),
                    base_url="https://gateway.test",
                ) as client:
                    request = _send_request("message-e2e")
                    streamed = await client.post("/a2a", headers=_headers(token), json=request)
                    replay = await client.post("/a2a", headers=_headers(token), json=request)

                    assert streamed.status_code == 200
                    results = _sse_results(streamed)
                    assert list(results[0]) == ["task"]
                    task = results[0]["task"]
                    assert isinstance(task, dict)
                    assert task["id"].startswith("task_")
                    assert results[-1]["statusUpdate"]["status"]["state"] == (
                        "TASK_STATE_COMPLETED"
                    )
                    artifact_update = next(
                        result["artifactUpdate"] for result in results if "artifactUpdate" in result
                    )
                    assert "Fake runtime reply" in artifact_update["artifact"]["parts"][0]["text"]
                    assert _sse_results(replay)[0]["task"]["id"] == task["id"]

                    attachment_response = await client.post(
                        "/a2a",
                        headers=_headers(token),
                        json={
                            "jsonrpc": "2.0",
                            "id": "attachment",
                            "method": "SendMessage",
                            "params": {
                                "tenant": "contract",
                                "message": {
                                    "messageId": "message-attachment",
                                    "role": "ROLE_USER",
                                    "parts": [
                                        {
                                            "raw": "aGVsbG8gZnJvbSBhMmE=",
                                            "filename": "note.txt",
                                            "mediaType": "text/plain",
                                        }
                                    ],
                                },
                                "configuration": {"returnImmediately": True},
                            },
                        },
                    )
                    assert "error" not in attachment_response.json()

                stored_message = await gateway_app.state.gateway_store.get_message(
                    peer_id=str(_peer["id"]), message_id="message-attachment"
                )
                assert stored_message is not None
                mapped_task = await gateway_app.state.gateway_store.get_task(
                    peer_id=str(_peer["id"]),
                    tenant="contract",
                    task_id=str(stored_message["task_id"]),
                )
                assert mapped_task is not None
                inputs = await runtime_app.state.store.list_run_inputs(
                    str(mapped_task["runtime_run_id"])
                )
                assert len(inputs) == 1
                assert runtime_app.state.store.run_input_body_path(inputs[0]).read_bytes() == (
                    b"hello from a2a"
                )

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=runtime_app),
                    base_url="http://runtime.test",
                    headers={"Authorization": "Bearer runtime-token"},
                ) as runtime_api:
                    runs = await runtime_api.get("/v1/runs")
                    assert runs.status_code == 200
                    assert len(runs.json()["runs"]) == 2
        finally:
            await runtime.close()
