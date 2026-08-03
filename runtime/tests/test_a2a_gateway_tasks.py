from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from shejane_runtime.a2a_gateway.app import GatewayConfig, create_gateway_app


class FakeRuntime:
    def __init__(self) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self.inject_calls: list[dict[str, str]] = []
        self.cancel_calls: list[dict[str, str]] = []
        self.runs: dict[str, dict[str, Any]] = {}
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.artifacts: dict[str, dict[str, Any]] = {}

    async def create_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.create_calls.append(payload)
        run_id = f"run-{len(self.runs) + 1}"
        run = {
            "id": run_id,
            "status": "queued",
            "updated_at": datetime.now(UTC).isoformat(),
            "thread_id": payload["thread_id"],
        }
        self.runs[run_id] = run
        self.events[run_id] = []
        return run

    async def inject(self, *, run_id: str, command_id: str, content: str) -> dict[str, Any]:
        self.inject_calls.append({"run_id": run_id, "command_id": command_id, "content": content})
        return {"instruction_id": f"steer-{len(self.inject_calls)}", "queued": True}

    async def get_run(self, run_id: str) -> dict[str, Any]:
        return self.runs[run_id]

    async def list_events(self, run_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        return [event for event in self.events[run_id] if int(event.get("seq", 0)) > after]

    async def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        return self.artifacts[artifact_id]

    async def open_artifact_content(self, artifact_id: str) -> httpx.Response:
        artifact = self.artifacts[artifact_id]
        return httpx.Response(
            200,
            content=artifact["body"],
            request=httpx.Request("GET", f"http://runtime/v1/artifacts/{artifact_id}/content"),
        )

    async def get_thread_snapshot(self, thread_id: str) -> dict[str, Any]:
        run = next(run for run in self.runs.values() if run["thread_id"] == thread_id)
        events = self.events[str(run["id"])]
        return {
            "runs": [run],
            "events": events,
            "event_high_watermarks": {
                str(run["id"]): max((int(event["seq"]) for event in events), default=0)
            },
        }

    async def cancel(self, *, run_id: str, command_id: str) -> dict[str, Any]:
        self.cancel_calls.append({"run_id": run_id, "command_id": command_id})
        self.runs[run_id]["status"] = "canceled"
        self.runs[run_id]["updated_at"] = datetime.now(UTC).isoformat()
        self.events[run_id].append({"seq": 1, "event_type": "run.canceled", "payload": {}})
        return {"canceled": True}

    async def stream_events(
        self, *, run_id: str, after: int
    ) -> AsyncGenerator[dict[str, Any], None]:
        self.runs[run_id]["status"] = "completed"
        self.runs[run_id]["updated_at"] = datetime.now(UTC).isoformat()
        event = {
            "seq": max(after + 1, 1),
            "event_type": "run.completed",
            "payload": {"final_text": "finished result"},
        }
        self.events[run_id] = [event]
        yield event


async def _gateway(tmp_path: Path, runtime: FakeRuntime, *, push_sender=None):
    app = create_gateway_app(
        GatewayConfig(
            db_path=tmp_path / "gateway.db",
            runtime_base_url="http://127.0.0.1:17371",
            runtime_token="runtime-token",
            public_base_url="https://gateway.test",
            push_credential_key=b"k" * 32,
        ),
        runtime_client=runtime,
        push_sender=push_sender,
    )
    return app


async def _token(app, *, push: bool = False) -> str:
    _peer, token = await app.state.gateway_store.create_peer(
        name="Contract client",
        tenant="contract",
        scopes=[
            "tasks.create",
            "tasks.read",
            "tasks.cancel",
            *(["push.manage"] if push else []),
        ],
        runtime_model="local:test:model",
        runtime_workspace_path=None,
        permission_mode="ask",
        push_origins=["https://callback.example.test"] if push else [],
        expires_at=None,
    )
    return token


def _headers(token: str) -> dict[str, str]:
    return {"A2A-Version": "1.0", "Authorization": f"Bearer {token}"}


def _send_params(
    *,
    message_id: str,
    text: str,
    task_id: str | None = None,
    context_id: str | None = None,
    return_immediately: bool = True,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "messageId": message_id,
        "role": "ROLE_USER",
        "parts": [{"text": text, "mediaType": "text/plain"}],
    }
    if task_id:
        message["taskId"] = task_id
    if context_id:
        message["contextId"] = context_id
    return {
        "tenant": "contract",
        "message": message,
        "configuration": {"returnImmediately": return_immediately},
    }


def _sse_results(response: httpx.Response) -> list[dict[str, Any]]:
    return [
        json.loads(line[5:].strip())["result"]
        for line in response.text.splitlines()
        if line.startswith("data:")
    ]


@pytest.mark.asyncio
async def test_send_get_list_and_cancel_map_only_external_task_ids(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    app = await _gateway(tmp_path, runtime)
    async with app.router.lifespan_context(app):
        token = await _token(app)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://gateway.test"
        ) as client:
            request = {
                "jsonrpc": "2.0",
                "id": "send-1",
                "method": "SendMessage",
                "params": _send_params(message_id="message-1", text="Inspect the tests."),
            }
            first = await client.post("/a2a", headers=_headers(token), json=request)
            replay = await client.post("/a2a", headers=_headers(token), json=request)
            replay_with_new_response_configuration = await client.post(
                "/a2a",
                headers=_headers(token),
                json={
                    **request,
                    "id": "send-1-reconfigured",
                    "params": {
                        **request["params"],
                        "configuration": {
                            "returnImmediately": True,
                            "historyLength": 0,
                        },
                    },
                },
            )
            assert first.status_code == 200
            task = first.json()["result"]["task"]
            assert replay.json()["result"]["task"] == task
            assert (
                replay_with_new_response_configuration.json()["result"]["task"]["id"]
                == (task["id"])
            )
            assert task["id"].startswith("task_")
            assert task["id"] != "run-1"
            assert task["status"]["state"] == "TASK_STATE_SUBMITTED"
            assert task["history"][0]["messageId"] == "message-1"
            assert len(runtime.create_calls) == 1
            assert runtime.create_calls[0]["model"] == "local:test:model"
            assert runtime.create_calls[0]["permission_mode"] == "ask"
            assert runtime.create_calls[0]["metadata"]["a2a_task_id"] == task["id"]
            assert runtime.create_calls[0]["metadata"]["a2a_message_id"] == "message-1"

            runtime.runs["run-1"]["status"] = "completed"
            runtime.artifacts["runtime-secret-artifact-id"] = {
                "id": "runtime-secret-artifact-id",
                "title": "report.bin",
                "content": "",
                "content_type": "application/octet-stream",
                "bytes": 4,
                "sha256": "9f64a747e1b97f131fabb6b447296c9b6f0201e79fb3c5356e6c77e89b6a806a",
                "storage_kind": "blob",
                "created_at": datetime.now(UTC).isoformat(),
                "body": b"\x01\x02\x03\x04",
            }
            runtime.events["run-1"] = [
                {
                    "seq": 1,
                    "event_type": "artifact.created",
                    "payload": {"artifact_id": "runtime-secret-artifact-id"},
                },
                {
                    "seq": 2,
                    "event_type": "run.completed",
                    "payload": {"final_text": "done"},
                },
            ]
            fetched = await client.post(
                "/a2a",
                headers=_headers(token),
                json={
                    "jsonrpc": "2.0",
                    "id": "get-1",
                    "method": "GetTask",
                    "params": {"tenant": "contract", "id": task["id"], "historyLength": 0},
                },
            )
            fetched_task = fetched.json()["result"]
            assert fetched_task["status"]["state"] == "TASK_STATE_COMPLETED"
            assert "history" not in fetched_task
            assert fetched_task["artifacts"][1]["parts"] == [
                {"text": "done", "mediaType": "text/plain"}
            ]
            runtime_artifact = fetched_task["artifacts"][0]
            assert "runtime-secret-artifact-id" not in json.dumps(runtime_artifact)
            artifact_url = runtime_artifact["parts"][0]["url"]
            artifact_body = await client.get(artifact_url, headers=_headers(token))
            assert artifact_body.content == b"\x01\x02\x03\x04"
            assert artifact_body.headers["content-disposition"] == (
                "attachment; filename*=UTF-8''report.bin"
            )
            assert artifact_body.headers["x-content-type-options"] == "nosniff"
            unsigned = await client.get(urlsplit(artifact_url).path, headers=_headers(token))
            assert unsigned.status_code == 404

            _other, other_token = await app.state.gateway_store.create_peer(
                name="Other client",
                tenant="other-contract",
                scopes=["tasks.read"],
                runtime_model="local:test:model",
                runtime_workspace_path=None,
                permission_mode="ask",
                push_origins=[],
                expires_at=None,
            )
            hidden = await client.get(artifact_url, headers=_headers(other_token))
            assert hidden.status_code == 404

            listed = await client.post(
                "/a2a",
                headers=_headers(token),
                json={
                    "jsonrpc": "2.0",
                    "id": "list-1",
                    "method": "ListTasks",
                    "params": {
                        "tenant": "contract",
                        "status": "TASK_STATE_COMPLETED",
                        "includeArtifacts": True,
                    },
                },
            )
            assert [item["id"] for item in listed.json()["result"]["tasks"]] == [task["id"]]
            after_future = await client.post(
                "/a2a",
                headers=_headers(token),
                json={
                    "jsonrpc": "2.0",
                    "id": "list-after",
                    "method": "ListTasks",
                    "params": {
                        "tenant": "contract",
                        "statusTimestampAfter": "2999-01-01T00:00:00Z",
                    },
                },
            )
            assert after_future.json()["result"]["tasks"] == []

            second = await client.post(
                "/a2a",
                headers=_headers(token),
                json={
                    "jsonrpc": "2.0",
                    "id": "send-2",
                    "method": "SendMessage",
                    "params": _send_params(
                        message_id="message-2",
                        text="A second task.",
                        context_id=task["contextId"],
                    ),
                },
            )
            second_task = second.json()["result"]["task"]
            assert second_task["id"] != task["id"]
            assert second_task["contextId"] == task["contextId"]
            canceled = await client.post(
                "/a2a",
                headers=_headers(token),
                json={
                    "jsonrpc": "2.0",
                    "id": "cancel-1",
                    "method": "CancelTask",
                    "params": {"tenant": "contract", "id": second_task["id"]},
                },
            )
            assert canceled.json()["result"]["status"]["state"] == "TASK_STATE_CANCELED"
            assert runtime.cancel_calls[0]["run_id"] == "run-2"


@pytest.mark.asyncio
async def test_followup_is_idempotent_and_terminal_task_cannot_restart(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    app = await _gateway(tmp_path, runtime)
    async with app.router.lifespan_context(app):
        token = await _token(app)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://gateway.test"
        ) as client:
            initial = await client.post(
                "/a2a",
                headers=_headers(token),
                json={
                    "jsonrpc": "2.0",
                    "id": "initial",
                    "method": "SendMessage",
                    "params": _send_params(message_id="message-1", text="Start."),
                },
            )
            task = initial.json()["result"]["task"]
            followup_request = {
                "jsonrpc": "2.0",
                "id": "followup",
                "method": "SendMessage",
                "params": _send_params(
                    message_id="message-2",
                    text="Refine it.",
                    task_id=task["id"],
                ),
            }
            first = await client.post("/a2a", headers=_headers(token), json=followup_request)
            replay = await client.post("/a2a", headers=_headers(token), json=followup_request)
            assert first.json()["result"]["task"]["id"] == task["id"]
            assert replay.json()["result"]["task"]["id"] == task["id"]
            assert len(runtime.inject_calls) == 1
            assert runtime.inject_calls[0]["content"] == "Refine it."

            runtime.runs["run-1"]["status"] = "completed"
            terminal = await client.post(
                "/a2a",
                headers=_headers(token),
                json={
                    "jsonrpc": "2.0",
                    "id": "terminal-followup",
                    "method": "SendMessage",
                    "params": _send_params(
                        message_id="message-3",
                        text="Restart.",
                        task_id=task["id"],
                    ),
                },
            )
            assert terminal.json()["error"]["code"] == -32004


@pytest.mark.asyncio
async def test_blocking_send_waits_for_settled_task_and_rejects_unsupported_parts(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime()
    app = await _gateway(tmp_path, runtime)
    async with app.router.lifespan_context(app):
        token = await _token(app)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://gateway.test"
        ) as client:
            blocking = await client.post(
                "/a2a",
                headers=_headers(token),
                json={
                    "jsonrpc": "2.0",
                    "id": "blocking",
                    "method": "SendMessage",
                    "params": _send_params(
                        message_id="message-1",
                        text="Wait for this.",
                        return_immediately=False,
                    ),
                },
            )
            result = blocking.json()["result"]["task"]
            assert result["status"]["state"] == "TASK_STATE_COMPLETED"
            assert result["artifacts"][0]["parts"][0]["text"] == "finished result"

            with_attachment = await client.post(
                "/a2a",
                headers=_headers(token),
                json={
                    "jsonrpc": "2.0",
                    "id": "raw",
                    "method": "SendMessage",
                    "params": {
                        "tenant": "contract",
                        "message": {
                            "messageId": "message-raw",
                            "role": "ROLE_USER",
                            "parts": [
                                {
                                    "raw": "aGVsbG8=",
                                    "filename": "note.txt",
                                    "mediaType": "text/plain",
                                }
                            ],
                        },
                        "configuration": {"returnImmediately": True},
                    },
                },
            )
            attachment_task = with_attachment.json()["result"]["task"]
            attachment_path = Path(runtime.create_calls[1]["attachment_paths"][0])
            assert attachment_path.name == "note.txt"
            assert attachment_path.read_bytes() == b"hello"
            assert str(attachment_path) not in json.dumps(attachment_task)

            unsupported_media = await client.post(
                "/a2a",
                headers=_headers(token),
                json={
                    "jsonrpc": "2.0",
                    "id": "raw-unsupported-media",
                    "method": "SendMessage",
                    "params": {
                        "tenant": "contract",
                        "message": {
                            "messageId": "message-raw-unsupported-media",
                            "role": "ROLE_USER",
                            "parts": [
                                {
                                    "raw": "aGVsbG8=",
                                    "filename": "payload.bin",
                                    "mediaType": "application/x-unsupported",
                                }
                            ],
                        },
                        "configuration": {"returnImmediately": True},
                    },
                },
            )
            assert unsupported_media.json()["error"]["code"] == -32005

            unsupported_followup = await client.post(
                "/a2a",
                headers=_headers(token),
                json={
                    "jsonrpc": "2.0",
                    "id": "raw-followup",
                    "method": "SendMessage",
                    "params": {
                        "tenant": "contract",
                        "message": {
                            "messageId": "message-raw-followup",
                            "taskId": attachment_task["id"],
                            "role": "ROLE_USER",
                            "parts": [
                                {
                                    "raw": "aGVsbG8=",
                                    "filename": "note.txt",
                                    "mediaType": "text/plain",
                                }
                            ],
                        },
                        "configuration": {"returnImmediately": True},
                    },
                },
            )
            assert unsupported_followup.json()["error"]["code"] == -32005


@pytest.mark.asyncio
async def test_streaming_send_and_subscribe_start_with_snapshot_then_order_updates(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime()
    app = await _gateway(tmp_path, runtime)
    async with app.router.lifespan_context(app):
        token = await _token(app)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://gateway.test"
        ) as client:
            streamed = await client.post(
                "/a2a",
                headers=_headers(token),
                json={
                    "jsonrpc": "2.0",
                    "id": "stream-send",
                    "method": "SendStreamingMessage",
                    "params": _send_params(message_id="message-1", text="Stream this."),
                },
            )
            assert streamed.status_code == 200
            assert streamed.headers["content-type"].startswith("text/event-stream")
            results = _sse_results(streamed)
            assert list(results[0]) == ["task"]
            assert results[0]["task"]["status"]["state"] == "TASK_STATE_SUBMITTED"
            assert list(results[1]) == ["artifactUpdate"]
            assert results[1]["artifactUpdate"]["lastChunk"] is True
            assert list(results[2]) == ["statusUpdate"]
            assert results[2]["statusUpdate"]["status"]["state"] == "TASK_STATE_COMPLETED"

            initial = await client.post(
                "/a2a",
                headers=_headers(token),
                json={
                    "jsonrpc": "2.0",
                    "id": "send-for-subscribe",
                    "method": "SendMessage",
                    "params": _send_params(message_id="message-2", text="Subscribe later."),
                },
            )
            task_id = initial.json()["result"]["task"]["id"]
            subscribed = await client.post(
                "/a2a",
                headers=_headers(token),
                json={
                    "jsonrpc": "2.0",
                    "id": "subscribe",
                    "method": "SubscribeToTask",
                    "params": {"tenant": "contract", "id": task_id},
                },
            )
            subscribed_results = _sse_results(subscribed)
            assert list(subscribed_results[0]) == ["task"]
            assert list(subscribed_results[-1]) == ["statusUpdate"]


@pytest.mark.asyncio
async def test_push_config_uses_encrypted_outbox_and_delete_revokes_pending_delivery(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime()
    deliveries: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    async def sender(url: str, headers: dict[str, str], body: bytes) -> int:
        deliveries.append((url, headers, json.loads(body)))
        return 204

    app = await _gateway(tmp_path, runtime, push_sender=sender)
    async with app.router.lifespan_context(app):
        await app.state.gateway_push.close()
        token = await _token(app, push=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://gateway.test"
        ) as client:
            request = {
                "jsonrpc": "2.0",
                "id": "push-send",
                "method": "SendMessage",
                "params": {
                    **_send_params(message_id="message-push", text="Notify me."),
                    "configuration": {
                        "returnImmediately": True,
                        "taskPushNotificationConfig": {
                            "url": "https://callback.example.test/a2a/events",
                            "token": "private-correlation-token",
                            "authentication": {
                                "scheme": "Bearer",
                                "credentials": "private-callback-credential",
                            },
                        },
                    },
                },
            }
            response = await client.post("/a2a", headers=_headers(token), json=request)
            assert response.status_code == 200
            task = response.json()["result"]["task"]
            assert "private-correlation-token" not in (tmp_path / "gateway.db").read_bytes().decode(
                errors="ignore"
            )
            assert "private-callback-credential" not in (
                tmp_path / "gateway.db"
            ).read_bytes().decode(errors="ignore")

            assert await app.state.gateway_push.deliver_once() is True
            assert deliveries[0][0] == "https://callback.example.test/a2a/events"
            assert deliveries[0][1]["Authorization"] == ("Bearer private-callback-credential")
            assert deliveries[0][1]["Idempotency-Key"].startswith("push_")
            assert deliveries[0][1]["traceparent"].startswith("00-")
            assert deliveries[0][2]["task"]["id"] == task["id"]

            replay = await client.post("/a2a", headers=_headers(token), json=request)
            assert replay.json()["result"]["task"]["id"] == task["id"]
            assert await app.state.gateway_push.deliver_once() is False

            runtime.runs["run-1"]["status"] = "completed"
            runtime.events["run-1"] = [
                {
                    "seq": 1,
                    "event_type": "run.completed",
                    "payload": {"final_text": "push result"},
                }
            ]
            await app.state.gateway_push.produce_once()
            assert await app.state.gateway_push.deliver_once() is True
            assert await app.state.gateway_push.deliver_once() is True
            assert (
                deliveries[-2][2]["artifactUpdate"]["artifact"]["parts"][0]["text"] == "push result"
            )
            assert deliveries[-1][2]["statusUpdate"]["status"]["state"] == ("TASK_STATE_COMPLETED")

            listed = await client.post(
                "/a2a",
                headers=_headers(token),
                json={
                    "jsonrpc": "2.0",
                    "id": "push-list",
                    "method": "ListTaskPushNotificationConfigs",
                    "params": {"tenant": "contract", "taskId": task["id"]},
                },
            )
            config = listed.json()["result"]["configs"][0]
            assert config["token"] == "private-correlation-token"
            assert config["authentication"]["credentials"] == ("private-callback-credential")

            created = await client.post(
                "/a2a",
                headers=_headers(token),
                json={
                    "jsonrpc": "2.0",
                    "id": "push-create",
                    "method": "CreateTaskPushNotificationConfig",
                    "params": {
                        "tenant": "contract",
                        "id": "config-to-delete",
                        "taskId": task["id"],
                        "url": "https://callback.example.test/second",
                    },
                },
            )
            assert created.json()["result"]["id"] == "config-to-delete"
            deleted = await client.post(
                "/a2a",
                headers=_headers(token),
                json={
                    "jsonrpc": "2.0",
                    "id": "push-delete",
                    "method": "DeleteTaskPushNotificationConfig",
                    "params": {
                        "tenant": "contract",
                        "taskId": task["id"],
                        "id": "config-to-delete",
                    },
                },
            )
            assert "error" not in deleted.json()
            deleted_again = await client.post(
                "/a2a",
                headers=_headers(token),
                json={
                    "jsonrpc": "2.0",
                    "id": "push-delete-replay",
                    "method": "DeleteTaskPushNotificationConfig",
                    "params": {
                        "tenant": "contract",
                        "taskId": task["id"],
                        "id": "config-to-delete",
                    },
                },
            )
            assert "error" not in deleted_again.json()
            assert await app.state.gateway_push.deliver_once() is False

            forbidden_origin = await client.post(
                "/a2a",
                headers=_headers(token),
                json={
                    "jsonrpc": "2.0",
                    "id": "push-bad-origin",
                    "method": "CreateTaskPushNotificationConfig",
                    "params": {
                        "tenant": "contract",
                        "taskId": task["id"],
                        "url": "https://attacker.example.test/callback",
                    },
                },
            )
            assert forbidden_origin.json()["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_push_retry_reuses_stable_delivery_identity(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    delivery_ids: list[str] = []
    statuses = iter([503, 204])

    async def sender(_url: str, headers: dict[str, str], _body: bytes) -> int:
        delivery_ids.append(headers["Idempotency-Key"])
        return next(statuses)

    app = await _gateway(tmp_path, runtime, push_sender=sender)
    async with app.router.lifespan_context(app):
        await app.state.gateway_push.close()
        token = await _token(app, push=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://gateway.test"
        ) as client:
            response = await client.post(
                "/a2a",
                headers=_headers(token),
                json={
                    "jsonrpc": "2.0",
                    "id": "push-retry",
                    "method": "SendMessage",
                    "params": {
                        **_send_params(message_id="message-retry", text="Retry push."),
                        "configuration": {
                            "returnImmediately": True,
                            "taskPushNotificationConfig": {
                                "url": "https://callback.example.test/retry"
                            },
                        },
                    },
                },
            )
            assert "error" not in response.json()
        assert await app.state.gateway_push.deliver_once() is True
        pending = await (
            await app.state.gateway_store._conn.execute(
                "SELECT id, status, attempts FROM a2a_push_outbox"
            )
        ).fetchone()
        assert dict(pending) == {
            "id": delivery_ids[0],
            "status": "pending",
            "attempts": 1,
        }
        await app.state.gateway_store._conn.execute(
            "UPDATE a2a_push_outbox SET available_at = '2000-01-01T00:00:00+00:00'"
        )
        assert await app.state.gateway_push.deliver_once() is True
        assert delivery_ids == [delivery_ids[0], delivery_ids[0]]
        settled = await (
            await app.state.gateway_store._conn.execute(
                "SELECT status, attempts FROM a2a_push_outbox"
            )
        ).fetchone()
        assert dict(settled) == {"status": "delivered", "attempts": 2}


@pytest.mark.asyncio
async def test_gateway_restart_delivers_persisted_push_outbox(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    delivered = asyncio.Event()
    deliveries: list[tuple[str, str, dict[str, Any]]] = []

    async def sender(url: str, headers: dict[str, str], body: bytes) -> int:
        deliveries.append((url, headers["Idempotency-Key"], json.loads(body)))
        delivered.set()
        return 204

    first = await _gateway(tmp_path, runtime, push_sender=sender)
    async with first.router.lifespan_context(first):
        await first.state.gateway_push.close()
        token = await _token(first, push=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first), base_url="https://gateway.test"
        ) as client:
            response = await client.post(
                "/a2a",
                headers=_headers(token),
                json={
                    "jsonrpc": "2.0",
                    "id": "push-before-restart",
                    "method": "SendMessage",
                    "params": {
                        **_send_params(
                            message_id="message-before-restart",
                            text="Deliver after restart.",
                        ),
                        "configuration": {
                            "returnImmediately": True,
                            "taskPushNotificationConfig": {
                                "url": "https://callback.example.test/restarted"
                            },
                        },
                    },
                },
            )
            task_id = response.json()["result"]["task"]["id"]
    assert deliveries == []

    restarted = await _gateway(tmp_path, runtime, push_sender=sender)
    async with restarted.router.lifespan_context(restarted):
        await asyncio.wait_for(delivered.wait(), timeout=2)
        assert deliveries[0][0] == "https://callback.example.test/restarted"
        assert deliveries[0][1].startswith("push_")
        assert deliveries[0][2]["task"]["id"] == task_id
