"""Tests for /v1/runs endpoints.

These exercise the full HTTP -> RunCoordinator -> build_agent -> mocked
TestStreamingChatModel -> SSE response path. Crucially: this validates Phase 3'
parts 1+2+3+4 together.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
from fastapi.testclient import TestClient

from shejane_runtime.auth import LOCAL_OWNER_PRINCIPAL_ID
from shejane_runtime.config import reset_settings_for_tests
from shejane_runtime.plugins.catalog import PluginCatalogError
from shejane_runtime.runs import _completion_failure_payload, _run_failed_payload
from shejane_runtime.server import RequestBodyLimitMiddleware, create_app
from tests.helpers import run_command


def fork_command(checkpoint_id: str, *, suffix: str, goal: str | None = None) -> dict[str, Any]:
    payload = {
        "command_id": f"cmd_fork_{suffix}",
        "client_message_id": f"msg_fork_user_{suffix}",
        "assistant_message_id": f"msg_fork_assistant_{suffix}",
        "thread_id": f"thread_fork_{suffix}",
        "protocol_version": 1,
        "required_capabilities": ["agent.run", "agent.stream", "hitl"],
        "checkpoint_id": checkpoint_id,
        "user_input": f"Retry from checkpoint {checkpoint_id}",
        "thread_title": "Forked conversation",
    }
    if goal is not None:
        payload["goal"] = goal
    return payload


def _stream_response(events: list[tuple[str, str]]) -> httpx.Response:
    body = "".join(f"event: {n}\ndata: {p}\n\n" for n, p in events).encode("utf-8")
    return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


def _patched_async_client(handler):
    class _Patched(httpx.AsyncClient):
        def __init__(self, **kw):
            super().__init__(
                transport=httpx.MockTransport(handler),
                **{k: v for k, v in kw.items() if k != "transport"},
            )

    return _Patched


@pytest.fixture
def client(monkeypatch) -> TestClient:
    tmp = Path(tempfile.mkdtemp(prefix="jdl-runs-"))
    os.environ["SHEJANE_RUNTIME_TOKEN"] = "tok"
    monkeypatch.delenv("SHEJANE_RUNTIME_MCP_SERVERS", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("SHEJANE_RUNTIME_SKILLS_PATH", str(tmp / "empty-skills"))

    def handler(request: httpx.Request) -> httpx.Response:
        # Mock backend returns a clean text response with no tool calls.
        return _stream_response(
            [
                ("llm.delta", '{"content_delta": "Hi from agent."}'),
                ("llm.done", '{"request_id": "r", "finish_reason": "stop"}'),
            ]
        )

    monkeypatch.setattr("tests.streaming_model.httpx.AsyncClient", _patched_async_client(handler))
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_HOST="127.0.0.1",
        SHEJANE_RUNTIME_PORT=17371,
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _parse_sse_lines(body: str) -> list[tuple[str, dict | str]]:
    events: list[tuple[str, dict | str]] = []
    current_event = ""
    data_buffer: list[str] = []

    def flush() -> None:
        if current_event or data_buffer:
            payload = "\n".join(data_buffer)
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                parsed = payload
            events.append((current_event, parsed))

    for raw in body.split("\n"):
        line = raw.rstrip("\r")
        if not line:
            flush()
            current_event = ""
            data_buffer = []
            continue
        if line.startswith("event:"):
            current_event = line[6:].strip()
        elif line.startswith("data:"):
            data_buffer.append(line[5:].strip())
    flush()
    return events


def test_create_run_returns_run_record(client: TestClient) -> None:
    r = client.post(
        "/v1/runs",
        headers={"Authorization": "Bearer tok"},
        json=run_command("say hi"),
    )
    assert r.status_code == 200
    # POST /runs returns the flat LocalRun shape (no `{run: ...}` wrapper) —
    # client.test.ts:63 + createLocalRun() pin this contract.
    run = r.json()
    assert run["id"].startswith("run_")
    assert run["goal"] == "say hi"
    assert run["status"] == "queued"
    stored = asyncio.run(client.app.state.store.get_run(run["id"]))
    assert stored["principal_id"] == LOCAL_OWNER_PRINCIPAL_ID
    snapshot = json.loads(stored["settings_json"])
    assert snapshot["_snapshot_version"] == 1
    assert snapshot["permission_mode"] == "ask"
    assert snapshot["_model_binding"]["credential_ref"] == "tests:streaming_model"
    assert snapshot["_model_binding"]["required_capabilities"] == [
        "streaming",
        "tool_calling",
    ]
    assert "capabilities" not in snapshot["_model_binding"]
    assert "test-cloud-token" not in stored["settings_json"]


@pytest.mark.parametrize(
    "path",
    [
        "/v1/runs/run_missing",
        "/v1/runs/run_missing/stream",
    ],
)
def test_unknown_run_returns_stable_machine_readable_error(
    client: TestClient,
    path: str,
) -> None:
    response = client.get(path, headers={"Authorization": "Bearer tok"})

    assert response.status_code == 404
    assert response.json() == {"detail": {"code": "run_not_found", "message": "run not found"}}


def test_create_run_rejects_unknown_permission_mode(client: TestClient) -> None:
    response = client.post(
        "/v1/runs",
        headers={"Authorization": "Bearer tok"},
        json=run_command("say hi", permission_mode="unrestricted"),
    )

    assert response.status_code == 422


def test_cancel_command_is_idempotent_and_rejects_retargeting(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingProviderStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'event: llm.delta\ndata: {"content_delta": "still running"}\n\n'
            await asyncio.Event().wait()

    def blocking_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=BlockingProviderStream(), request=request)

    monkeypatch.setattr(
        "tests.streaming_model.httpx.AsyncClient",
        _patched_async_client(blocking_handler),
    )
    coordinator = client.app.state.coordinator
    original_cancel_run = coordinator.cancel_run
    cancel_run_calls = 0

    async def cancel_run_once(run_id: str) -> bool:
        nonlocal cancel_run_calls
        cancel_run_calls += 1
        return await original_cancel_run(run_id)

    monkeypatch.setattr(coordinator, "cancel_run", cancel_run_once)
    headers = {"Authorization": "Bearer tok"}
    start_command = run_command(
        "first cancel target",
        thread_id="cancel_command_thread",
        assistant_message_id="cancel_command_assistant",
    )
    first_run = client.post(
        "/v1/runs",
        headers=headers,
        json=start_command,
    ).json()
    command = {
        "type": "run.cancel",
        "command_id": "cmd_cancel_http",
        "run_id": first_run["id"],
    }

    accepted = client.post("/v1/commands", headers=headers, json=command)
    replay = client.post("/v1/commands", headers=headers, json=command)
    assert cancel_run_calls == 1
    second_run = client.post(
        "/v1/runs",
        headers=headers,
        json=run_command("second cancel target"),
    ).json()
    conflict = client.post(
        "/v1/commands",
        headers=headers,
        json={**command, "run_id": second_run["id"]},
    )

    assert accepted.status_code == 200
    assert accepted.json() == {
        "type": "run.cancel",
        "command_id": "cmd_cancel_http",
        "run_id": first_run["id"],
        "canceled": True,
    }
    assert replay.json() == accepted.json()
    assert conflict.status_code == 409
    start_replay = client.post("/v1/runs", headers=headers, json=start_command)
    assert start_replay.status_code == 200
    assert start_replay.json()["command_id"] == start_command["command_id"]
    assert start_replay.json()["client_message_id"] == start_command["client_message_id"]
    listed_runs = client.get("/v1/runs", headers=headers).json()["runs"]
    assert [run["id"] for run in listed_runs].count(first_run["id"]) == 1
    snapshot_runs = client.get(
        "/v1/threads/cancel_command_thread",
        headers=headers,
    ).json()["runs"]
    assert [run["id"] for run in snapshot_runs].count(first_run["id"]) == 1
    assert snapshot_runs[0]["command_id"] == start_command["command_id"]
    assert "data: [DONE]" in client.get(f"/v1/runs/{first_run['id']}/stream", headers=headers).text
    assert client.get(f"/v1/runs/{first_run['id']}", headers=headers).json()["status"] == "canceled"


@pytest.mark.parametrize("waiting_status", ["waiting_permission", "waiting_input"])
def test_cancel_command_terminates_waiting_run(
    client: TestClient,
    waiting_status: str,
) -> None:
    store = client.app.state.store

    async def seed_waiting_run() -> str:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal=f"cancel {waiting_status}",
            workspace_path=None,
        )
        await store.update_run_status(run["id"], waiting_status)
        return str(run["id"])

    run_id = asyncio.run(seed_waiting_run())
    headers = {"Authorization": "Bearer tok"}
    command = {
        "type": "run.cancel",
        "command_id": f"cancel_{waiting_status}",
        "run_id": run_id,
    }

    accepted = client.post("/v1/commands", headers=headers, json=command)
    replay = client.post("/v1/commands", headers=headers, json=command)

    assert accepted.status_code == 200
    assert accepted.json()["canceled"] is True
    assert replay.json() == accepted.json()
    run = client.get(f"/v1/runs/{run_id}", headers=headers).json()
    assert run["status"] == "canceled"
    assert "run.canceled" in {
        json.loads(line.removeprefix("data: "))["event_type"]
        for line in client.get(
            f"/v1/runs/{run_id}/stream",
            headers=headers,
        ).text.splitlines()
        if line.startswith("data: {")
    }


def test_cancel_command_closes_wait_decisions_and_rejects_late_answers(
    client: TestClient,
) -> None:
    store = client.app.state.store

    async def seed_waits() -> tuple[str, str, str, str]:
        permission_run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="cancel permission",
            workspace_path=None,
        )
        permission = await store.create_permission(
            run_id=permission_run["id"],
            tool_call_id="call-cancel",
            tool_name="write_file",
            arguments={"path": "a.txt"},
        )
        await store.update_run_status(permission_run["id"], "waiting_permission")

        question_run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="cancel question",
            workspace_path=None,
        )
        question = await store.create_question(
            run_id=question_run["id"],
            tool_call_id="ask-cancel",
            questions=[{"id": "choice", "question": "Which file?"}],
        )
        await store.update_run_status(question_run["id"], "waiting_input")
        return (
            str(permission_run["id"]),
            str(permission["id"]),
            str(question_run["id"]),
            str(question["id"]),
        )

    permission_run_id, permission_id, question_run_id, question_id = asyncio.run(seed_waits())
    headers = {"Authorization": "Bearer tok"}
    for run_id in (permission_run_id, question_run_id):
        response = client.post(
            "/v1/commands",
            headers=headers,
            json={
                "type": "run.cancel",
                "command_id": f"cancel_{run_id}",
                "run_id": run_id,
            },
        )
        assert response.status_code == 200
        assert response.json()["canceled"] is True

    permission_events_before = asyncio.run(store.events_since(permission_run_id, after_seq=0))
    question_events_before = asyncio.run(store.events_since(question_run_id, after_seq=0))

    permission_response = client.post(
        f"/v1/permissions/{permission_id}",
        headers=headers,
        json={"decision": "approve", "scope": "once"},
    )
    question_response = client.post(
        f"/v1/questions/{question_id}",
        headers=headers,
        json={"answers": {"choice": ["a.txt"]}},
    )

    assert permission_response.status_code == 409
    assert question_response.status_code == 409
    assert asyncio.run(store.get_permission(permission_id))["status"] == "canceled"
    assert asyncio.run(store.get_question(question_id))["status"] == "canceled"
    assert (
        asyncio.run(store.events_since(permission_run_id, after_seq=0)) == permission_events_before
    )
    assert asyncio.run(store.events_since(question_run_id, after_seq=0)) == question_events_before


def test_question_answer_command_is_idempotent_and_rejects_changed_answers(
    client: TestClient,
) -> None:
    store = client.app.state.store

    async def seed_question() -> tuple[str, str]:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="answer durably",
            workspace_path=None,
        )
        question = await store.create_question(
            run_id=run["id"],
            tool_call_id="ask-durable",
            questions=[{"id": "choice", "question": "Which mode?"}],
        )
        await store.update_run_status(run["id"], "waiting_input")
        return str(run["id"]), str(question["id"])

    run_id, question_id = asyncio.run(seed_question())
    headers = {"Authorization": "Bearer tok"}
    command = {
        "type": "question.answer",
        "command_id": "answer_durable",
        "question_id": question_id,
        "answers": {"choice": ["mode X"]},
    }

    accepted = client.post("/v1/commands", headers=headers, json=command)
    replay = client.post("/v1/commands", headers=headers, json=command)
    conflict = client.post(
        "/v1/commands",
        headers=headers,
        json={**command, "answers": {"choice": ["mode Y"]}},
    )

    assert accepted.status_code == 200
    assert accepted.json() == {
        "type": "question.answer",
        "command_id": "answer_durable",
        "question_id": question_id,
        "run_id": run_id,
        "answered": True,
        "resumed": True,
    }
    assert replay.json() == accepted.json()
    assert conflict.status_code == 409


def test_question_answer_command_waits_for_every_candidate_in_the_cycle(
    client: TestClient,
) -> None:
    store = client.app.state.store

    async def seed_questions() -> tuple[str, str, str]:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="answer a batch",
            workspace_path=None,
        )
        first = await store.create_question(
            run_id=run["id"],
            tool_call_id="ask-first",
            questions=[{"id": "first", "question": "First?"}],
            wait_cycle_id="question-batch",
            interrupt_id="interrupt-first",
        )
        second = await store.create_question(
            run_id=run["id"],
            tool_call_id="ask-second",
            questions=[{"id": "second", "question": "Second?"}],
            wait_cycle_id="question-batch",
            interrupt_id="interrupt-second",
        )
        await store.update_run_status(run["id"], "waiting_input")
        return str(run["id"]), str(first["id"]), str(second["id"])

    run_id, first_id, second_id = asyncio.run(seed_questions())
    headers = {"Authorization": "Bearer tok"}

    first = client.post(
        "/v1/commands",
        headers=headers,
        json={
            "type": "question.answer",
            "command_id": "answer_first",
            "question_id": first_id,
            "answers": {"First?": ["one"]},
        },
    )
    second = client.post(
        "/v1/commands",
        headers=headers,
        json={
            "type": "question.answer",
            "command_id": "answer_second",
            "question_id": second_id,
            "answers": {"Second?": ["two"]},
        },
    )

    assert first.status_code == 200
    assert first.json()["answered"] is True
    assert first.json()["resumed"] is False
    assert second.status_code == 200
    assert second.json()["run_id"] == run_id
    assert second.json()["resumed"] is True


def test_permission_resolve_command_is_idempotent_and_resumes_after_the_batch(
    client: TestClient,
) -> None:
    store = client.app.state.store

    async def seed_permissions() -> tuple[str, str, str]:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="approve a batch",
            workspace_path=None,
        )
        first = await store.create_permission(
            run_id=run["id"],
            tool_call_id="call-first",
            tool_name="write_file",
            arguments={"path": "a.txt"},
            risk="workspace_write",
            wait_cycle_id="permission-batch",
            interrupt_id="permission-interrupt",
            action_index=0,
        )
        second = await store.create_permission(
            run_id=run["id"],
            tool_call_id="call-second",
            tool_name="execute",
            arguments={"command": "make test"},
            wait_cycle_id="permission-batch",
            interrupt_id="permission-interrupt",
            action_index=1,
        )
        await store.update_run_status(run["id"], "waiting_permission")
        return str(run["id"]), str(first["id"]), str(second["id"])

    run_id, first_id, second_id = asyncio.run(seed_permissions())
    headers = {"Authorization": "Bearer tok"}
    first_command = {
        "type": "permission.resolve",
        "command_id": "resolve_first",
        "permission_id": first_id,
        "decision": "approve",
        "scope": "run",
    }

    first = client.post("/v1/commands", headers=headers, json=first_command)
    replay = client.post("/v1/commands", headers=headers, json=first_command)
    conflict = client.post(
        "/v1/commands",
        headers=headers,
        json={**first_command, "decision": "deny", "scope": "once"},
    )
    second = client.post(
        "/v1/commands",
        headers=headers,
        json={
            "type": "permission.resolve",
            "command_id": "resolve_second",
            "permission_id": second_id,
            "decision": "deny",
        },
    )

    assert first.status_code == 200
    assert first.json() == {
        "type": "permission.resolve",
        "command_id": "resolve_first",
        "permission_id": first_id,
        "run_id": run_id,
        "resolved": True,
        "decision": "approve",
        "scope": "run",
        "resumed": False,
    }
    assert replay.json() == first.json()
    assert conflict.status_code == 409
    assert second.status_code == 200
    assert second.json()["decision"] == "deny"
    assert second.json()["scope"] == "once"
    assert second.json()["resumed"] is True


def test_plan_resolve_command_is_idempotent_and_rejects_changed_instructions(
    client: TestClient,
) -> None:
    store = client.app.state.store

    async def seed_plan() -> tuple[str, str]:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="approve a plan durably",
            workspace_path=None,
        )
        approval = await store.create_plan_approval(
            run_id=run["id"],
            tool_call_id="call-plan-command",
            todos=[{"content": "Write tests", "status": "pending"}],
            summary="Write tests",
        )
        await store.update_run_status(run["id"], "waiting_input")
        return str(run["id"]), str(approval["id"])

    run_id, approval_id = asyncio.run(seed_plan())
    headers = {"Authorization": "Bearer tok"}
    command = {
        "type": "plan.resolve",
        "command_id": "resolve_plan_durable",
        "approval_id": approval_id,
        "decision": "modify",
        "instructions": "Add verification.",
    }

    accepted = client.post("/v1/commands", headers=headers, json=command)
    replay = client.post("/v1/commands", headers=headers, json=command)
    conflict = client.post(
        "/v1/commands",
        headers=headers,
        json={**command, "instructions": "Skip verification."},
    )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {
        "type": "plan.resolve",
        "command_id": "resolve_plan_durable",
        "approval_id": approval_id,
        "run_id": run_id,
        "resolved": True,
        "decision": "modify",
        "instructions": "Add verification.",
        "resumed": True,
    }
    assert replay.json() == accepted.json()
    assert conflict.status_code == 409


def test_plan_resolve_command_waits_for_other_candidates_in_the_cycle(
    client: TestClient,
) -> None:
    store = client.app.state.store

    async def seed_wait_cycle() -> tuple[str, str, str]:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="approve and answer",
            workspace_path=None,
        )
        approval = await store.create_plan_approval(
            run_id=run["id"],
            tool_call_id="call-plan-batch",
            todos=[{"content": "Write tests", "status": "pending"}],
            wait_cycle_id="mixed-batch",
            interrupt_id="interrupt-plan",
        )
        permission = await store.create_permission(
            run_id=run["id"],
            tool_call_id="call-permission-batch",
            tool_name="execute",
            arguments={"command": "make test"},
            wait_cycle_id="mixed-batch",
            interrupt_id="interrupt-permission",
        )
        await store.update_run_status(run["id"], "waiting_permission")
        return str(run["id"]), str(approval["id"]), str(permission["id"])

    run_id, approval_id, permission_id = asyncio.run(seed_wait_cycle())
    headers = {"Authorization": "Bearer tok"}
    plan = client.post(
        "/v1/commands",
        headers=headers,
        json={
            "type": "plan.resolve",
            "command_id": "resolve_plan_batch",
            "approval_id": approval_id,
            "decision": "approve",
        },
    )
    permission = client.post(
        "/v1/commands",
        headers=headers,
        json={
            "type": "permission.resolve",
            "command_id": "approve_plan_batch_tool",
            "permission_id": permission_id,
            "decision": "approve",
        },
    )

    assert plan.status_code == 200
    assert plan.json()["resumed"] is False
    assert permission.status_code == 200
    assert permission.json()["run_id"] == run_id
    assert permission.json()["resumed"] is True


def test_runtime_discovery_is_authenticated(client: TestClient) -> None:
    assert client.get("/v1/runtime").status_code == 401
    response = client.get(
        "/v1/runtime",
        headers={"Authorization": "Bearer tok"},
    )
    assert response.status_code == 200
    assert response.json()["protocol_version"] == 1
    assert {"agent.run", "agent.stream", "workspace.files", "plugins"}.issubset(
        response.json()["capabilities"]
    )


def test_run_admission_rejects_protocol_and_capability_mismatch(
    client: TestClient,
) -> None:
    headers = {"Authorization": "Bearer tok"}
    bad_protocol = client.post(
        "/v1/runs",
        headers=headers,
        json=run_command("hello", protocol_version=2),
    )
    missing_capability = client.post(
        "/v1/runs",
        headers=headers,
        json=run_command(
            "hello",
            command_id="cmd_missing_capability",
            required_capabilities=["agent.run", "future.feature"],
        ),
    )

    assert bad_protocol.status_code == 409
    assert bad_protocol.json()["detail"]["code"] == "protocol_version_unsupported"
    assert missing_capability.status_code == 409
    assert missing_capability.json()["detail"]["code"] == "capability_unavailable"
    assert client.get("/v1/runs", headers=headers).json() == {"runs": []}


def test_run_admission_requires_a_configured_model_service(tmp_path: Path) -> None:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        SHEJANE_FAKE_LLM=False,
        data_dir=tmp_path,
    )
    with TestClient(create_app(settings)) as local_client:
        response = local_client.post(
            "/v1/runs",
            headers={"Authorization": "Bearer tok"},
            json=run_command("hello", model="local:missing:model"),
        )

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "model_service_missing"
        runtime = local_client.get(
            "/v1/runtime",
            headers={"Authorization": "Bearer tok"},
        )
        assert runtime.json()["model_service_configured"] is False
        assert local_client.get(
            "/v1/runs",
            headers={"Authorization": "Bearer tok"},
        ).json() == {"runs": []}


def test_runtime_discovery_matches_missing_model_service_admission(
    tmp_path: Path,
) -> None:
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp_path,
    )
    with TestClient(create_app(settings)) as local_client:
        headers = {"Authorization": "Bearer tok"}
        runtime = local_client.get("/v1/runtime", headers=headers)
        response = local_client.post(
            "/v1/runs",
            headers=headers,
            json=run_command("hello", model="local:missing:model"),
        )

        assert runtime.json()["model_service_configured"] is False
        assert response.status_code == 409
    assert response.json()["detail"]["code"] == "model_service_missing"


def test_idempotent_replay_returns_the_original_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = {"Authorization": "Bearer tok"}
    command = run_command("hello", command_id="cmd_provider_replay")
    first = client.post("/v1/runs", headers=headers, json=command)
    assert first.status_code == 200

    @asynccontextmanager
    async def unexpected_model_admission(*_args, **_kwargs):
        raise AssertionError("command replay must not re-run model admission")
        yield

    monkeypatch.setattr(
        client.app.state.coordinator,
        "_model_admission",
        unexpected_model_admission,
    )

    replay = client.post("/v1/runs", headers=headers, json=command)

    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]


def test_run_command_persists_only_public_settings_and_workflow_metadata(
    client: TestClient,
) -> None:
    response = client.post(
        "/v1/runs",
        headers={"Authorization": "Bearer tok"},
        json=run_command(
            "repair",
            settings={"memory": "off", "api_key": "must-not-persist"},
            metadata={
                "intent": "repair",
                "attempt": 2,
                "token": "must-not-persist",
            },
        ),
    )
    assert response.status_code == 200

    async def persisted_payloads() -> tuple[str, str]:
        store = client.app.state.store
        run = await store.get_run(response.json()["id"])
        command = await (
            await store._conn.execute(
                "SELECT payload_json FROM local_commands WHERE run_id = ?",
                (response.json()["id"],),
            )
        ).fetchone()
        assert run is not None and command is not None
        return run["metadata_json"], command["payload_json"]

    metadata_json, command_json = asyncio.run(persisted_payloads())
    assert json.loads(metadata_json) == {"intent": "repair", "attempt": 2}
    assert "must-not-persist" not in command_json


def test_create_run_requires_owned_available_workspace(client: TestClient, tmp_path: Path) -> None:
    headers = {"Authorization": "Bearer tok"}
    rejected = client.post(
        "/v1/runs",
        headers=headers,
        json={**run_command("inspect"), "workspace_path": str(tmp_path)},
    )
    assert rejected.status_code == 403
    assert client.get("/v1/runs", headers=headers).json() == {"runs": []}

    authorized = client.post(
        "/v1/workspaces",
        headers=headers,
        json={"path": str(tmp_path), "label": "test"},
    )
    assert authorized.status_code == 200
    accepted = client.post(
        "/v1/runs",
        headers=headers,
        json={
            **run_command("inspect", command_id="cmd_workspace_owned"),
            "workspace_path": str(tmp_path),
        },
    )
    assert accepted.status_code == 200
    assert accepted.json()["workspace_path"] == str(tmp_path.resolve())


def test_create_run_accepts_existing_file_attachments_only(
    client: TestClient, tmp_path: Path
) -> None:
    headers = {"Authorization": "Bearer tok"}
    missing = client.post(
        "/v1/runs",
        headers=headers,
        json={
            **run_command("inspect attachment", command_id="cmd_attachment_missing"),
            "required_capabilities": ["agent.run", "agent.stream", "attachments"],
            "attachment_paths": [str(tmp_path / "missing.txt")],
        },
    )
    assert missing.status_code == 409

    attachment = tmp_path / "brief.txt"
    attachment.write_text("runtime-owned attachment", encoding="utf-8")
    accepted = client.post(
        "/v1/runs",
        headers=headers,
        json={
            **run_command("inspect attachment", command_id="cmd_attachment_ok"),
            "thread_id": "thread_attachment_ok",
            "user_input": "inspect attachment",
            "user_item_metadata": {"attachments": [{"path": str(attachment), "name": "brief.txt"}]},
            "required_capabilities": ["agent.run", "agent.stream", "attachments"],
            "attachment_paths": [str(attachment)],
        },
    )
    assert accepted.status_code == 200
    expected_inputs = [
        {
            "client_index": 0,
            "input_id": "source",
            "virtual_path": "/attachments/brief.txt",
            "original_name": "brief.txt",
            "media_type": "text/plain",
            "bytes": len(b"runtime-owned attachment"),
            "sha256": hashlib.sha256(b"runtime-owned attachment").hexdigest(),
        }
    ]
    assert accepted.json()["inputs"] == expected_inputs
    stored = asyncio.run(client.app.state.store.get_run(accepted.json()["id"]))
    metadata = json.loads(stored["metadata_json"])
    assert metadata["_attachments"] == [
        {"input_id": "source", "virtual_path": "/attachments/brief.txt"}
    ]
    run_inputs = asyncio.run(client.app.state.store.list_run_inputs(accepted.json()["id"]))
    assert len(run_inputs) == 1
    assert run_inputs[0]["original_name"] == "brief.txt"
    assert run_inputs[0]["sha256"] == hashlib.sha256(b"runtime-owned attachment").hexdigest()
    immutable_body = client.app.state.store.run_input_body_path(run_inputs[0])
    attachment.write_text("changed after admission", encoding="utf-8")
    assert immutable_body.read_bytes() == b"runtime-owned attachment"
    downloaded = client.get(
        f"/v1/runs/{accepted.json()['id']}/inputs/source",
        headers=headers,
    )
    assert downloaded.status_code == 200
    assert downloaded.content == b"runtime-owned attachment"
    assert downloaded.headers["content-type"].startswith("application/octet-stream")
    snapshot = client.get("/v1/threads/thread_attachment_ok", headers=headers)
    assert snapshot.status_code == 200
    assert snapshot.json()["runs"][0]["inputs"] == expected_inputs
    user_item = next(
        item for item in snapshot.json()["items"] if item["item_type"] == "user_message"
    )
    assert user_item["metadata"]["attachments"] == [
        {
            "path": str(attachment),
            "name": "brief.txt",
            "input_id": "source",
            "media_type": "text/plain",
            "bytes": len(b"runtime-owned attachment"),
        }
    ]
    fetched_run = client.get(f"/v1/runs/{accepted.json()['id']}", headers=headers)
    assert fetched_run.json()["inputs"] == expected_inputs
    listed_run = next(
        run
        for run in client.get("/v1/runs", headers=headers).json()["runs"]
        if run["id"] == accepted.json()["id"]
    )
    assert listed_run["inputs"] == expected_inputs
    diagnostics = client.get(
        f"/v1/runs/{accepted.json()['id']}/diagnostics",
        headers=headers,
    )
    assert diagnostics.status_code == 200
    assert diagnostics.json()["run"]["inputs"] == expected_inputs

    second = tmp_path / "second.txt"
    second.write_text("second", encoding="utf-8")
    multiple = client.post(
        "/v1/runs",
        headers=headers,
        json={
            **run_command("inspect two", command_id="cmd_attachment_multiple"),
            "required_capabilities": ["agent.run", "agent.stream", "attachments"],
            "attachment_paths": [str(attachment), str(second)],
        },
    )
    assert multiple.status_code == 200
    assert [
        (item["client_index"], item["input_id"], item["original_name"])
        for item in multiple.json()["inputs"]
    ] == [
        (0, "attachment_1", "brief.txt"),
        (1, "attachment_2", "second.txt"),
    ]

    large_attachment = tmp_path / "clip.mp4"
    large_attachment.write_bytes(b"\0" * (10 * 1024 * 1024 + 1))
    large = client.post(
        "/v1/runs",
        headers=headers,
        json={
            **run_command("inspect media", command_id="cmd_attachment_large"),
            "required_capabilities": ["agent.run", "agent.stream", "attachments"],
            "attachment_paths": [str(large_attachment)],
        },
    )
    assert large.status_code == 200

    oversized_attachment = tmp_path / "oversized.pdf"
    with oversized_attachment.open("wb") as stream:
        stream.truncate(200 * 1024 * 1024 + 1)
    oversized = client.post(
        "/v1/runs",
        headers=headers,
        json={
            **run_command("inspect oversized", command_id="cmd_attachment_oversized"),
            "required_capabilities": ["agent.run", "agent.stream", "attachments"],
            "attachment_paths": [str(oversized_attachment)],
        },
    )
    assert oversized.status_code == 409
    assert "attachment exceeds the 200 MiB limit" in oversized.json()["detail"]["message"]

    from pptx import Presentation

    deck_path = tmp_path / "deck.pptx"
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[1])
    slide.shapes.title.text = "Runtime snapshot deck"
    slide.placeholders[1].text = "Immutable outline"
    deck.save(deck_path)
    deck_run = client.post(
        "/v1/runs",
        headers=headers,
        json={
            **run_command("inspect deck", command_id="cmd_attachment_deck"),
            "required_capabilities": ["agent.run", "agent.stream", "attachments"],
            "attachment_paths": [str(deck_path)],
        },
    )
    assert deck_run.status_code == 200
    deck_path.unlink()
    outline = client.get(
        f"/v1/runs/{deck_run.json()['id']}/inputs/source/pptx-outline",
        headers=headers,
    )
    assert outline.status_code == 200
    assert outline.json()["slides"][0]["title"] == "Runtime snapshot deck"


def test_command_replay_precedes_current_workspace_admission(
    client: TestClient, tmp_path: Path
) -> None:
    headers = {"Authorization": "Bearer tok"}
    workspace = client.post(
        "/v1/workspaces",
        headers=headers,
        json={"path": str(tmp_path), "label": "test"},
    ).json()
    command = {
        "command_id": "cmd_workspace_replay",
        "client_message_id": "msg_workspace_replay",
        "protocol_version": 1,
        "required_capabilities": ["agent.run", "agent.stream"],
        "goal": "inspect",
        "model": "local:test:model",
        "workspace_path": str(tmp_path),
    }
    first = client.post("/v1/runs", headers=headers, json=command)
    assert first.status_code == 200
    assert client.delete(f"/v1/workspaces/{workspace['id']}", headers=headers).status_code == 200

    replay = client.post("/v1/runs", headers=headers, json=command)
    conflict = client.post(
        "/v1/runs",
        headers=headers,
        json={**command, "goal": "different"},
    )

    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]
    assert conflict.status_code == 409
    assert client.get(f"/v1/runs/{first.json()['id']}", headers=headers).status_code == 200
    fork = client.post(
        f"/v1/runs/{first.json()['id']}/fork",
        headers=headers,
        json=fork_command("cp_revoked", suffix="revoked"),
    )
    assert fork.status_code == 403


def test_foreign_run_and_children_are_hidden(client: TestClient) -> None:
    store = client.app.state.store

    async def create_foreign_resources() -> tuple[str, str, str]:
        run = await store.create_run(
            principal_id="user:foreign",
            goal="private",
            workspace_path=None,
        )
        artifact = await store.create_artifact(
            run_id=run["id"],
            kind="result",
            title="private",
            content="secret",
        )
        permission = await store.create_permission(
            run_id=run["id"],
            tool_call_id="call_foreign",
            tool_name="execute",
            arguments={},
        )
        return run["id"], artifact["id"], permission["id"]

    run_id, artifact_id, permission_id = asyncio.run(create_foreign_resources())
    headers = {"Authorization": "Bearer tok"}

    assert run_id not in {
        run["id"] for run in client.get("/v1/runs", headers=headers).json()["runs"]
    }
    assert client.get(f"/v1/runs/{run_id}", headers=headers).status_code == 404
    assert client.get(f"/v1/runs/{run_id}/stream", headers=headers).status_code == 404
    assert client.get(f"/v1/runs/{run_id}/diagnostics", headers=headers).status_code == 404
    assert client.get(f"/v1/runs/{run_id}/inputs/source", headers=headers).status_code == 404
    assert client.post(f"/v1/runs/{run_id}/cancel", headers=headers).status_code == 404
    assert (
        client.post(
            f"/v1/runs/{run_id}/inject",
            headers=headers,
            json={"content": "steal"},
        ).status_code
        == 404
    )
    assert client.get(f"/v1/artifacts/{artifact_id}", headers=headers).status_code == 404
    assert client.get(f"/v1/artifacts/{artifact_id}/content", headers=headers).status_code == 404
    assert (
        client.post(
            f"/v1/permissions/{permission_id}",
            headers=headers,
            json={"decision": "deny", "scope": "once"},
        ).status_code
        == 404
    )


def test_blob_artifact_metadata_and_range_download_are_owned(
    client: TestClient,
    tmp_path: Path,
) -> None:
    source = tmp_path / "media.bin"
    source.write_bytes(b"0123456789")

    async def create_blob() -> dict[str, Any]:
        store = client.app.state.store
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="create media",
            workspace_path=None,
        )
        return await store.create_file_artifact(
            artifact_id="artifact_blob",
            run_id=run["id"],
            kind="plugin_output",
            title="media.bin",
            source_path=source,
            content_type="application/octet-stream",
        )

    artifact = asyncio.run(create_blob())
    headers = {"Authorization": "Bearer tok"}
    metadata = client.get(f"/v1/artifacts/{artifact['id']}", headers=headers)
    assert metadata.status_code == 200
    assert metadata.json() == {
        "id": "artifact_blob",
        "title": "media.bin",
        "content": "",
        "content_type": "application/octet-stream",
        "bytes": 10,
        "sha256": hashlib.sha256(b"0123456789").hexdigest(),
        "storage_kind": "blob",
        "tool_name": None,
        "created_at": artifact["created_at"],
    }

    body = client.get(
        f"/v1/artifacts/{artifact['id']}/content",
        headers={**headers, "Range": "bytes=2-5"},
    )
    assert body.status_code == 206
    assert body.content == b"2345"
    assert body.headers["content-range"] == "bytes 2-5/10"


def test_create_run_rejects_foreign_parent_before_writing(client: TestClient) -> None:
    store = client.app.state.store

    async def create_foreign_parent() -> str:
        run = await store.create_run(
            principal_id="user:foreign",
            goal="parent",
            workspace_path=None,
        )
        return run["id"]

    parent_id = asyncio.run(create_foreign_parent())
    response = client.post(
        "/v1/runs",
        headers={"Authorization": "Bearer tok"},
        json={**run_command("child"), "parent_run_id": parent_id},
    )

    assert response.status_code == 404
    local_runs = client.get("/v1/runs", headers={"Authorization": "Bearer tok"}).json()["runs"]
    assert local_runs == []


def test_create_run_rejects_cleanup_required_parent(client: TestClient) -> None:
    store = client.app.state.store

    async def create_quarantined_parent() -> str:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="parent",
            workspace_path=None,
        )
        await store.update_run_status(run["id"], "cleanup_required")
        return str(run["id"])

    parent_id = asyncio.run(create_quarantined_parent())
    response = client.post(
        "/v1/runs",
        headers={"Authorization": "Bearer tok"},
        json={**run_command("child"), "parent_run_id": parent_id},
    )

    assert response.status_code == 409
    assert "safely settled" in response.text


def test_runtime_thread_snapshot_and_change_cursor_are_authoritative(client: TestClient) -> None:
    store = client.app.state.store

    async def seed_thread() -> dict:
        run, _created = await store.accept_run_command(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_thread_snapshot",
            client_message_id="msg_thread_snapshot",
            thread_id="conversation_snapshot",
            assistant_message_id="msg_assistant_snapshot",
            user_input="visible inspect",
            thread_title="Visible snapshot",
            thread_metadata={"pinned": True},
            command_payload={"type": "run.start", "goal": "inspect"},
            goal="inspect",
            workspace_path=None,
            mode="auto",
        )
        job = await store.claim_run_job(worker_id="worker-thread-snapshot")
        assert job is not None
        with store.bind_execution_lease(
            job_id=job["id"],
            run_id=run["id"],
            lease_owner="worker-thread-snapshot",
            lease_generation=int(job["lease_generation"]),
        ):
            await store.commit_run_result(
                run["id"],
                status="completed",
                event_type="run.completed",
                payload={"final_text": "done"},
            )
        return run

    run = asyncio.run(seed_thread())
    headers = {"Authorization": "Bearer tok"}

    listing = client.get("/v1/threads", headers=headers)
    snapshot = client.get("/v1/threads/conversation_snapshot", headers=headers)
    changes = client.get("/v1/threads/changes?after=0", headers=headers)

    assert listing.status_code == 200
    assert listing.json()["threads"][0]["id"] == "conversation_snapshot"
    assert listing.json()["cursor"] >= 2
    assert snapshot.status_code == 200
    assert snapshot.json()["thread"]["version"] == 2
    assert snapshot.json()["thread"]["metadata"] == {"pinned": True}
    assert [item["item_type"] for item in snapshot.json()["items"]] == [
        "user_message",
        "assistant_message",
    ]
    assistant = next(
        item for item in snapshot.json()["items"] if item["item_type"] == "assistant_message"
    )
    assert assistant["status"] == "completed"
    assert assistant["content"] == "done"
    assert assistant["client_id"] == "msg_assistant_snapshot"
    user = next(item for item in snapshot.json()["items"] if item["item_type"] == "user_message")
    assert user["content"] == "visible inspect"
    assert snapshot.json()["runs"][0]["id"] == run["id"]
    assert snapshot.json()["event_high_watermarks"] == {run["id"]: 1}
    assert [change["change_type"] for change in changes.json()["changes"]] == [
        "turn.started",
        "run.completed",
    ]


def test_create_run_command_is_idempotent_and_rejects_conflicting_content(
    client: TestClient,
) -> None:
    headers = {"Authorization": "Bearer tok"}
    command = {
        "command_id": "cmd_http_1",
        "client_message_id": "msg_http_1",
        "protocol_version": 1,
        "required_capabilities": ["agent.run", "agent.stream"],
        "goal": "say hi",
        "model": "local:test:model",
    }

    first = client.post("/v1/runs", headers=headers, json=command)
    replay = client.post("/v1/runs", headers=headers, json=command)
    conflict = client.post(
        "/v1/runs",
        headers=headers,
        json={**command, "goal": "different"},
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["command_id"] == "cmd_http_1"
    assert conflict.status_code == 409


def test_create_run_rejects_partial_command_ids_and_unknown_fields(client: TestClient) -> None:
    headers = {"Authorization": "Bearer tok"}
    partial = client.post(
        "/v1/runs",
        headers=headers,
        json={"command_id": "cmd_partial", "goal": "say hi"},
    )
    missing = client.post(
        "/v1/runs",
        headers=headers,
        json={"goal": "say hi"},
    )
    unknown = client.post(
        "/v1/runs",
        headers=headers,
        json={**run_command("say hi"), "unexpected": True},
    )
    forged_principal = client.post(
        "/v1/runs",
        headers=headers,
        json={**run_command("say hi"), "principal_id": "attacker"},
    )
    malformed_model = client.post(
        "/v1/runs",
        headers=headers,
        json={**run_command("say hi"), "model": "local:open ai:gpt 4.1"},
    )

    assert partial.status_code == 422
    assert missing.status_code == 422
    assert unknown.status_code == 422
    assert forged_principal.status_code == 422
    assert malformed_model.status_code == 422


def test_pairing_token_rotation_keeps_the_same_local_owner(
    tmp_path: Path,
) -> None:
    command = {
        "command_id": "cmd_rotated_token",
        "client_message_id": "msg_rotated_token",
        "protocol_version": 1,
        "required_capabilities": ["agent.run", "agent.stream"],
        "goal": "same command",
        "model": "local:test:model",
    }
    first_settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="token-one",
        SHEJANE_FAKE_LLM=True,
        data_dir=tmp_path,
    )
    with TestClient(create_app(first_settings)) as first_client:
        first = first_client.post(
            "/v1/runs",
            headers={"Authorization": "Bearer token-one"},
            json=command,
        )
        assert first.status_code == 200

    second_settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_TOKEN="token-two",
        SHEJANE_FAKE_LLM=True,
        data_dir=tmp_path,
    )
    with TestClient(create_app(second_settings)) as second_client:
        rejected = second_client.post(
            "/v1/runs",
            headers={"Authorization": "Bearer token-one"},
            json=command,
        )
        replay = second_client.post(
            "/v1/runs",
            headers={"Authorization": "Bearer token-two"},
            json=command,
        )

    assert rejected.status_code == 401
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]


def test_create_run_persists_run_metadata(client: TestClient) -> None:
    r = client.post(
        "/v1/runs",
        headers={"Authorization": "Bearer tok"},
        json=run_command(
            "repair task",
            metadata={
                "intent": "repair",
                "source_run_id": "run_original",
                "source_message_id": "msg_original",
                "attempt": 1,
            },
        ),
    )
    assert r.status_code == 200
    run = r.json()
    assert json.loads(run["metadata_json"]) == {
        "intent": "repair",
        "source_run_id": "run_original",
        "source_message_id": "msg_original",
        "attempt": 1,
    }

    fetched = client.get(
        f"/v1/runs/{run['id']}",
        headers={"Authorization": "Bearer tok"},
    )
    assert fetched.status_code == 200
    assert json.loads(fetched.json()["metadata_json"])["intent"] == "repair"


def test_fork_run_missing_checkpoint_returns_404(client: TestClient) -> None:
    source = client.post(
        "/v1/runs",
        headers={"Authorization": "Bearer tok"},
        json=run_command("source task"),
    )
    assert source.status_code == 200
    source_run_id = source.json()["id"]

    fork = client.post(
        f"/v1/runs/{source_run_id}/fork",
        headers={"Authorization": "Bearer tok"},
        json=fork_command("checkpoint-does-not-exist", suffix="missing"),
    )

    assert fork.status_code == 404
    assert fork.json()["detail"] == "checkpoint not found"


def test_fork_rejects_incompatible_protocol_and_capabilities(client: TestClient) -> None:
    source = client.post(
        "/v1/runs",
        headers={"Authorization": "Bearer tok"},
        json=run_command("source task"),
    )
    assert source.status_code == 200
    endpoint = f"/v1/runs/{source.json()['id']}/fork"
    headers = {"Authorization": "Bearer tok"}

    bad_protocol = fork_command("unused", suffix="bad_protocol")
    bad_protocol["protocol_version"] = 999
    missing_capability = fork_command("unused", suffix="missing_capability")
    missing_capability["required_capabilities"] = ["future.capability"]

    protocol_response = client.post(endpoint, headers=headers, json=bad_protocol)
    capability_response = client.post(endpoint, headers=headers, json=missing_capability)

    assert protocol_response.status_code == 409
    assert protocol_response.json()["detail"]["code"] == "protocol_version_unsupported"
    assert capability_response.status_code == 409
    assert capability_response.json()["detail"]["code"] == "capability_unavailable"
    runs = client.get("/v1/runs", headers=headers)
    assert [run["id"] for run in runs.json()["runs"]] == [source.json()["id"]]


def test_fork_run_from_checkpoint_creates_child_branch_head(client: TestClient) -> None:
    source = client.post(
        "/v1/runs",
        headers={"Authorization": "Bearer tok"},
        json=run_command("draft a plan", permission_mode="auto"),
    )
    assert source.status_code == 200
    source_run_id = source.json()["id"]
    with client.stream(
        "GET",
        f"/v1/runs/{source_run_id}/stream",
        headers={"Authorization": "Bearer tok"},
    ) as resp:
        assert resp.status_code == 200
        resp.read()

    diagnostics = client.get(
        f"/v1/runs/{source_run_id}/diagnostics",
        headers={"Authorization": "Bearer tok"},
    )
    assert diagnostics.status_code == 200
    checkpoint = diagnostics.json()["latest_checkpoint"]
    assert checkpoint is not None
    checkpoint_id = checkpoint["id"]

    fork = client.post(
        f"/v1/runs/{source_run_id}/fork",
        headers={"Authorization": "Bearer tok"},
        json=fork_command(checkpoint_id, suffix="child", goal="retry from that point"),
    )

    assert fork.status_code == 200
    child = fork.json()
    assert child["id"].startswith("run_")
    assert child["id"] != source_run_id
    assert child["parent_run_id"] == source_run_id
    assert child["goal"] == "retry from that point"
    metadata = json.loads(child["metadata_json"])
    assert metadata["intent"] == "checkpoint_fork"
    assert metadata["source_run_id"] == source_run_id
    assert metadata["source_checkpoint_id"] == checkpoint_id
    assert child["graph_thread_id"] == source.json()["graph_thread_id"]
    assert child["graph_checkpoint_id"] == checkpoint_id
    assert child["thread_id"] == "thread_fork_child"
    assert json.loads(child["settings_json"])["permission_mode"] == "auto"
    snapshot = client.get(
        "/v1/threads/thread_fork_child",
        headers={"Authorization": "Bearer tok"},
    )
    assert snapshot.status_code == 200
    assert [item["client_id"] for item in snapshot.json()["items"]] == [
        "msg_fork_user_child",
        "msg_fork_assistant_child",
    ]
    assert (
        client.app.state.checkpointer.get(
            {
                "configurable": {
                    "thread_id": child["id"],
                    "checkpoint_ns": "",
                    "checkpoint_id": checkpoint_id,
                }
            }
        )
        is None
    )

    with client.stream(
        "GET",
        f"/v1/runs/{child['id']}/stream",
        headers={"Authorization": "Bearer tok"},
    ) as resp:
        assert resp.status_code == 200
        resp.read()
    advanced = client.get(
        f"/v1/runs/{child['id']}",
        headers={"Authorization": "Bearer tok"},
    ).json()
    assert advanced["graph_checkpoint_id"] != checkpoint_id

    sibling_head = client.post(
        f"/v1/runs/{source_run_id}/fork",
        headers={"Authorization": "Bearer tok"},
        json=fork_command(advanced["graph_checkpoint_id"], suffix="sibling"),
    )
    assert sibling_head.status_code == 404


def test_fork_rejects_an_existing_product_thread(client: TestClient) -> None:
    source = client.post(
        "/v1/runs",
        headers={"Authorization": "Bearer tok"},
        json=run_command("source branch"),
    )
    with client.stream(
        "GET",
        f"/v1/runs/{source.json()['id']}/stream",
        headers={"Authorization": "Bearer tok"},
    ) as response:
        response.read()
    checkpoint = client.get(
        f"/v1/runs/{source.json()['id']}/diagnostics",
        headers={"Authorization": "Bearer tok"},
    ).json()["latest_checkpoint"]["id"]

    target_command = run_command("target branch")
    target_command["thread_id"] = "existing_fork_target"
    target = client.post(
        "/v1/runs",
        headers={"Authorization": "Bearer tok"},
        json=target_command,
    )
    with client.stream(
        "GET",
        f"/v1/runs/{target.json()['id']}/stream",
        headers={"Authorization": "Bearer tok"},
    ) as response:
        response.read()

    payload = fork_command(checkpoint, suffix="existing")
    payload["thread_id"] = "existing_fork_target"
    fork = client.post(
        f"/v1/runs/{source.json()['id']}/fork",
        headers={"Authorization": "Bearer tok"},
        json=payload,
    )
    assert fork.status_code == 409
    assert "already exists" in fork.text


def test_fork_command_replay_ignores_later_workspace_revocation(
    client: TestClient, tmp_path: Path
) -> None:
    headers = {"Authorization": "Bearer tok"}
    workspace = client.post(
        "/v1/workspaces",
        headers=headers,
        json={"path": str(tmp_path), "label": "fork replay"},
    ).json()
    source = client.post(
        "/v1/runs",
        headers=headers,
        json=run_command("workspace source", workspace_path=str(tmp_path)),
    )
    with client.stream(
        "GET", f"/v1/runs/{source.json()['id']}/stream", headers=headers
    ) as response:
        response.read()
    checkpoint = client.get(f"/v1/runs/{source.json()['id']}/diagnostics", headers=headers).json()[
        "latest_checkpoint"
    ]["id"]
    payload = fork_command(checkpoint, suffix="durable_replay")

    first = client.post(f"/v1/runs/{source.json()['id']}/fork", headers=headers, json=payload)
    assert first.status_code == 200
    assert client.delete(f"/v1/workspaces/{workspace['id']}", headers=headers).status_code == 200
    replay = client.post(f"/v1/runs/{source.json()['id']}/fork", headers=headers, json=payload)
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]


def test_repair_run_emits_workflow_events_and_context(monkeypatch) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="jdl-runs-repair-"))
    os.environ["SHEJANE_RUNTIME_TOKEN"] = "tok"
    monkeypatch.delenv("SHEJANE_RUNTIME_MCP_SERVERS", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("SHEJANE_RUNTIME_SKILLS_PATH", str(tmp / "empty-skills"))

    captured_requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        try:
            captured_requests.append(json.loads(body))
        except json.JSONDecodeError:
            pass
        return _stream_response(
            [
                ("llm.delta", '{"content_delta": "repair done"}'),
                ("llm.done", '{"request_id": "r", "finish_reason": "stop"}'),
            ]
        )

    monkeypatch.setattr("tests.streaming_model.httpx.AsyncClient", _patched_async_client(handler))
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_HOST="127.0.0.1",
        SHEJANE_RUNTIME_PORT=17371,
        SHEJANE_RUNTIME_TOKEN="tok",
        SHEJANE_RUNTIME_REPAIR_WORKFLOW_MAX=3,
        data_dir=tmp,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        r = c.post(
            "/v1/runs",
            headers={"Authorization": "Bearer tok"},
            json=run_command(
                "repair task",
                metadata={
                    "intent": "repair",
                    "source_run_id": "run_original",
                    "source_message_id": "msg_original",
                    "attempt": 2,
                    "failure_category": "validation",
                    "failure_action_kind": "repair",
                },
            ),
        )
        assert r.status_code == 200
        run_id = r.json()["id"]

        with c.stream(
            "GET",
            f"/v1/runs/{run_id}/stream",
            headers={"Authorization": "Bearer tok"},
        ) as resp:
            assert resp.status_code == 200
            body = resp.read().decode("utf-8")

    envelopes = [
        envelope
        for _event, envelope in _parse_sse_lines(body)
        if isinstance(envelope, dict) and "event_type" in envelope
    ]
    repair_events = [
        envelope["payload"]
        for envelope in envelopes
        if envelope.get("event_type") == "repair.workflow"
    ]
    assert [event.get("status") for event in repair_events] == ["started", "completed"]
    assert repair_events[0]["attempt"] == 2
    assert repair_events[0]["max_attempts"] == 3
    assert repair_events[0]["source_run_id"] == "run_original"
    assert repair_events[0]["failure_category"] == "validation"

    assert captured_requests, "expected backend LLM call"
    system_blocks = [
        message["content"]
        for message in captured_requests[0].get("messages", [])
        if message.get("role") == "system"
    ]
    joined_system = "\n".join(system_blocks)
    assert "修复工作流" in joined_system
    assert "第 2/3 次" in joined_system
    assert "run_original" in joined_system
    assert "validation / repair" in joined_system


def test_retry_run_injects_workflow_context(monkeypatch) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="jdl-runs-retry-"))
    os.environ["SHEJANE_RUNTIME_TOKEN"] = "tok"
    monkeypatch.delenv("SHEJANE_RUNTIME_MCP_SERVERS", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("SHEJANE_RUNTIME_SKILLS_PATH", str(tmp / "empty-skills"))

    captured_requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        try:
            captured_requests.append(json.loads(body))
        except json.JSONDecodeError:
            pass
        return _stream_response(
            [
                ("llm.delta", '{"content_delta": "retry done"}'),
                ("llm.done", '{"request_id": "r", "finish_reason": "stop"}'),
            ]
        )

    monkeypatch.setattr("tests.streaming_model.httpx.AsyncClient", _patched_async_client(handler))
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_HOST="127.0.0.1",
        SHEJANE_RUNTIME_PORT=17371,
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp,
    )
    app = create_app(settings)
    with TestClient(app) as c:

        async def seed_answered_choice() -> str:
            source = await c.app.state.store.create_run(
                principal_id=LOCAL_OWNER_PRINCIPAL_ID,
                goal="build a snake game",
                workspace_path=None,
            )
            question = await c.app.state.store.create_question(
                run_id=source["id"],
                tool_call_id="ask-game-type",
                questions=[
                    {
                        "question": "贪吃蛇使用哪种游戏类型？",
                        "header": "游戏类型",
                        "options": [{"label": "经典数字/符号"}, {"label": "图片素材"}],
                    }
                ],
            )
            await c.app.state.store.update_run_status(source["id"], "waiting_input")
            await c.app.state.store.answer_question(
                question["id"],
                answers={"贪吃蛇使用哪种游戏类型？": ["经典数字/符号"]},
                event_payload={
                    "request_id": question["id"],
                    "answers": {"贪吃蛇使用哪种游戏类型？": ["经典数字/符号"]},
                },
            )
            await c.app.state.store.update_run_status(source["id"], "failed")
            return str(source["id"])

        source_run_id = c.portal.call(seed_answered_choice)
        r = c.post(
            "/v1/runs",
            headers={"Authorization": "Bearer tok"},
            json=run_command(
                "retry task",
                metadata={
                    "intent": "retry",
                    "source_run_id": source_run_id,
                    "source_message_id": "msg_failed",
                    "attempt": 2,
                    "failure_category": "auth",
                    "failure_action_kind": "user_action",
                },
            ),
        )
        assert r.status_code == 200
        run_id = r.json()["id"]

        with c.stream(
            "GET",
            f"/v1/runs/{run_id}/stream",
            headers={"Authorization": "Bearer tok"},
        ) as resp:
            assert resp.status_code == 200
            resp.read()

    assert captured_requests, "expected backend LLM call"
    system_blocks = [
        message["content"]
        for message in captured_requests[0].get("messages", [])
        if message.get("role") == "system"
    ]
    joined_system = "\n".join(system_blocks)
    assert "恢复重试" in joined_system
    assert "第 2 次" in joined_system
    assert source_run_id in joined_system
    assert "msg_failed" in joined_system
    assert "auth / user_action" in joined_system
    assert "贪吃蛇使用哪种游戏类型？" in joined_system
    assert "经典数字/符号" in joined_system


def test_repair_run_over_attempt_limit_fails_before_model_call(monkeypatch) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="jdl-runs-repair-limit-"))
    os.environ["SHEJANE_RUNTIME_TOKEN"] = "tok"
    monkeypatch.delenv("SHEJANE_RUNTIME_MCP_SERVERS", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    captured_requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(json.loads(request.read()))
        return _stream_response([("llm.delta", '{"content_delta": "should not run"}')])

    monkeypatch.setattr("tests.streaming_model.httpx.AsyncClient", _patched_async_client(handler))
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_HOST="127.0.0.1",
        SHEJANE_RUNTIME_PORT=17371,
        SHEJANE_RUNTIME_TOKEN="tok",
        SHEJANE_RUNTIME_REPAIR_WORKFLOW_MAX=1,
        data_dir=tmp,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        r = c.post(
            "/v1/runs",
            headers={"Authorization": "Bearer tok"},
            json=run_command(
                "repair task",
                metadata={
                    "intent": "repair",
                    "source_run_id": "run_original",
                    "source_message_id": "msg_original",
                    "attempt": 2,
                },
            ),
        )
        assert r.status_code == 200
        run_id = r.json()["id"]

        with c.stream(
            "GET",
            f"/v1/runs/{run_id}/stream",
            headers={"Authorization": "Bearer tok"},
        ) as resp:
            assert resp.status_code == 200
            body = resp.read().decode("utf-8")

        fetched = c.get(f"/v1/runs/{run_id}", headers={"Authorization": "Bearer tok"})
        assert fetched.status_code == 200
        assert fetched.json()["status"] == "failed"

    envelopes = [
        envelope
        for _event, envelope in _parse_sse_lines(body)
        if isinstance(envelope, dict) and "event_type" in envelope
    ]
    event_names = [envelope["event_type"] for envelope in envelopes]
    assert "run.failed" in event_names
    rejected = [
        envelope["payload"]
        for envelope in envelopes
        if envelope.get("event_type") == "repair.workflow"
    ]
    assert rejected == [
        {
            "status": "rejected",
            "attempt": 2,
            "max_attempts": 1,
            "reason": "repair attempt limit exceeded",
            "source_run_id": "run_original",
            "source_message_id": "msg_original",
        }
    ]
    assert captured_requests == []


def test_create_run_rejects_empty_goal(client: TestClient) -> None:
    r = client.post(
        "/v1/runs",
        headers={"Authorization": "Bearer tok"},
        json=run_command(""),
    )
    assert r.status_code == 400


@pytest.mark.parametrize(
    ("body", "expected_status"),
    [
        ({"goal": "x" * 131_073}, 422),
        ({"goal": "x", "history": [{"role": "user", "content": "x"}] * 257}, 422),
        ({"goal": "x", "metadata": {"oversized": "x" * 1_048_576}}, 413),
    ],
)
def test_create_run_rejects_oversized_persistent_input(
    client: TestClient,
    body: dict,
    expected_status: int,
) -> None:
    response = client.post(
        "/v1/runs",
        headers={"Authorization": "Bearer tok"},
        json={**run_command(str(body["goal"])), **body},
    )
    assert response.status_code == expected_status


async def test_request_body_limit_counts_streamed_chunks() -> None:
    messages = iter(
        [
            {"type": "http.request", "body": b"x" * 600_000, "more_body": True},
            {"type": "http.request", "body": b"x" * 600_000, "more_body": False},
        ]
    )
    sent: list[dict] = []

    async def receive() -> dict:
        return next(messages)

    async def send(message: dict) -> None:
        sent.append(message)

    async def consume_body(_scope: dict, receive, _send) -> None:
        while (await receive()).get("more_body"):
            pass

    middleware = RequestBodyLimitMiddleware(consume_body)
    await middleware(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/runs",
            "raw_path": b"/v1/runs",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 17371),
        },
        receive,
        send,
    )

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413


def test_create_run_rejects_deep_or_excessive_nested_input(client: TestClient) -> None:
    nested: dict = {}
    for _ in range(9):
        nested = {"next": nested}

    deep = client.post(
        "/v1/runs",
        headers={"Authorization": "Bearer tok"},
        json=run_command("x", metadata=nested),
    )
    wide = client.post(
        "/v1/runs",
        headers={"Authorization": "Bearer tok"},
        json=run_command("x", settings={"items": list(range(512))}),
    )

    assert deep.status_code == 422
    assert wide.status_code == 422


def test_auth_rejects_oversized_unauthenticated_body_before_reading_it(
    client: TestClient,
) -> None:
    response = client.post(
        "/v1/runs",
        headers={"Content-Type": "application/json"},
        content=b"x" * 1_048_577,
    )
    assert response.status_code == 401


def test_get_run_404_for_unknown(client: TestClient) -> None:
    r = client.get(
        "/v1/runs/run_does_not_exist",
        headers={"Authorization": "Bearer tok"},
    )
    assert r.status_code == 404


def test_run_failed_payload_includes_failure_policy_for_generic_exceptions() -> None:
    payload = _run_failed_payload(RuntimeError("unexpected implementation error"))

    assert payload["error"] == "unexpected implementation error"
    assert payload["type"] == "RuntimeError"
    assert payload["category"] == "fatal"
    assert payload["recoverable"] is False
    assert payload["retryable"] is False
    assert payload["action_kind"] == "operator_action"
    assert payload["recovery_action"] == "diagnostics"
    assert "implementation" in payload["suggested_action"]


def test_runtime_asset_download_failure_is_retryable() -> None:
    payload = _run_failed_payload(
        PluginCatalogError(
            "plugin_runtime_asset_unavailable",
            "plugin runtime asset is unavailable",
            retryable=True,
        )
    )

    assert payload["code"] == "plugin_runtime_asset_unavailable"
    assert payload["retryable"] is True
    assert payload["recoverable"] is True


def test_completion_router_block_becomes_structured_run_failure() -> None:
    payload = _completion_failure_payload(
        {
            "completion_route": {
                "decision": "blocked",
                "reason": "verification_failed",
                "message": "file missing: report.txt",
                "recoverable": True,
                "attempts": 1,
                "max_attempts": 1,
                "tool_call_id": "verify-1",
                "run_id": "run-1",
            }
        },
        current_run_id="run-1",
    )

    assert payload == {
        "error": "file missing: report.txt",
        "error_code": "verification_failed",
        "source": "completion_router",
        "failure_category": "verification",
        "recoverable": True,
        "retryable": False,
        "details": {
            "attempts": 1,
            "max_attempts": 1,
            "tool_call_id": "verify-1",
        },
        "category": "validation",
        "action_kind": "repair",
        "recovery_action": "repair",
        "suggested_action": "Fix the invalid request arguments before retrying.",
    }


def test_completion_router_scope_mismatch_fails_closed() -> None:
    payload = _completion_failure_payload(
        {
            "completion_route": {
                "decision": "blocked",
                "reason": "verification_failed",
                "message": "old failure",
                "recoverable": True,
                "run_id": "source-run",
            }
        },
        current_run_id="fork-run",
    )

    assert payload is not None
    assert payload["error_code"] == "completion_route_scope_mismatch"
    assert payload["recoverable"] is False


def test_full_run_lifecycle_through_sse(client: TestClient) -> None:
    """Start a run, stream until terminal event, verify run.completed."""
    # Start
    r = client.post(
        "/v1/runs",
        headers={"Authorization": "Bearer tok"},
        json=run_command("say hi"),
    )
    assert r.status_code == 200
    run_id = r.json()["id"]  # flat LocalRun shape (no wrapper)

    # Stream until run.completed or run.failed
    with client.stream(
        "GET",
        f"/v1/runs/{run_id}/stream",
        headers={"Authorization": "Bearer tok"},
    ) as resp:
        assert resp.status_code == 200
        body = resp.read().decode("utf-8")

    events = _parse_sse_lines(body)
    # Each event's `data:` body is now the AgentRunEvent envelope:
    # {event_type, payload, id, run_id, seq, created_at} — see Block 0 fix.
    event_names = [
        e[1].get("event_type") for e in events if isinstance(e[1], dict) and "event_type" in e[1]
    ]
    assert "run.started" in event_names
    assert any(name in {"run.completed", "run.failed"} for name in event_names)
    # If completed, final text now lives at envelope.payload.final_text.
    for _name, envelope in events:
        if not isinstance(envelope, dict):
            continue
        if envelope.get("event_type") == "run.completed":
            assert "Hi from agent" in (envelope.get("payload", {}).get("final_text") or "")
            break


def test_run_diagnostics_include_handoff_summary(client: TestClient) -> None:
    r = client.post(
        "/v1/runs",
        headers={"Authorization": "Bearer tok"},
        json=run_command("say hi"),
    )
    assert r.status_code == 200
    run_id = r.json()["id"]

    with client.stream(
        "GET",
        f"/v1/runs/{run_id}/stream",
        headers={"Authorization": "Bearer tok"},
    ) as resp:
        assert resp.status_code == 200
        _ = resp.read()

    diag = client.get(
        f"/v1/runs/{run_id}/diagnostics",
        headers={"Authorization": "Bearer tok"},
    )
    assert diag.status_code == 200
    body = diag.json()

    assert body["handoff"]["status"] == "completed"
    assert "completed" in body["handoff"]["headline"]
    assert body["handoff"]["ledger_state"] == "not_required"
    assert body["handoff"]["next_actions"]
    assert "run.completed" in body["handoff"]["recent_event_types"]
    checkpoint = body["latest_checkpoint"]
    assert checkpoint is not None
    assert checkpoint["id"]
    assert checkpoint["run_id"] == run_id
    assert checkpoint["step"] >= 0
    assert checkpoint["reason"]
    assert checkpoint["messages_count"] == 2
    trace = body["trace"]
    assert trace["root_span_id"] == f"span:run:{run_id}"
    spans = {span["id"]: span for span in trace["spans"]}
    assert spans[trace["root_span_id"]]["kind"] == "run"
    assert any(span["kind"] == "model" for span in spans.values())
    assert spans[f"span:checkpoint:{checkpoint['id']}"]["parent_id"] == trace["root_span_id"]
    assert spans[f"span:terminal:{run_id}"]["status"] == "completed"


def test_run_diagnostics_include_reflection_summary(client: TestClient) -> None:
    store = client.app.state.store

    async def create_completed_run() -> str:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="Reflect on answer",
            workspace_path=None,
        )
        await store.update_run_status(run["id"], "completed")
        return run["id"]

    run_id = asyncio.run(create_completed_run())

    class FakeCheckpointer:
        async def alist(self, config, limit=1):
            class Item:
                checkpoint: ClassVar[dict[str, object]] = {
                    "id": "ck-reflect",
                    "ts": "2026-06-11T00:00:00Z",
                    "channel_values": {
                        "messages": ["hidden message body"],
                        "reflection": {
                            "ai_messages": 2,
                            "tool_results": 3,
                            "final_answer_chars": 144,
                            "critic": {
                                "coverage": 4,
                                "clarity": 5,
                                "grounding": 3,
                                "notes": ["cite the source", "tighten conclusion"],
                            },
                        },
                    },
                }
                metadata: ClassVar[dict[str, object]] = {"step": 7, "source": "loop"}
                config: ClassVar[dict[str, object]] = {
                    "configurable": {"thread_id": run_id, "checkpoint_id": "ck-reflect"}
                }

            yield Item()

    client.app.state.checkpointer = FakeCheckpointer()

    diag = client.get(
        f"/v1/runs/{run_id}/diagnostics",
        headers={"Authorization": "Bearer tok"},
    )
    assert diag.status_code == 200
    body = diag.json()
    assert body["latest_checkpoint"]["id"] == "ck-reflect"
    reflection = body["reflection"]
    assert reflection["ai_messages"] == 2
    assert reflection["tool_results"] == 3
    assert reflection["final_answer_chars"] == 144
    assert reflection["critic"]["coverage"] == 4
    assert reflection["critic"]["notes"] == ["cite the source", "tighten conclusion"]
    assert "messages" not in reflection


def test_run_diagnostics_include_latest_feature_ledger(client: TestClient) -> None:
    r = client.post(
        "/v1/runs",
        headers={"Authorization": "Bearer tok"},
        json=run_command("say hi"),
    )
    assert r.status_code == 200
    run_id = r.json()["id"]

    store = client.app.state.store

    async def create_ledgers() -> None:
        await store.create_artifact(
            run_id=run_id,
            kind="progress_ledger",
            title="Progress ledger",
            content=json.dumps(
                {
                    "summary": "First draft",
                    "status": "in_progress",
                    "acceptance_criteria": ["old"],
                    "decisions": [],
                    "files_touched": [],
                    "validation_commands": [],
                    "unresolved_risks": [],
                    "next_actions": ["continue"],
                }
            ),
            content_type="application/json",
            tool_name="task.progress",
        )
        await store.create_artifact(
            run_id=run_id,
            kind="progress_ledger",
            title="Progress ledger",
            content=json.dumps(
                {
                    "summary": "Ready for review",
                    "status": "verified",
                    "acceptance_criteria": ["diagnostics exposes latest ledger"],
                    "decisions": ["latest artifact wins"],
                    "files_touched": ["shejane_runtime/server.py"],
                    "validation_commands": ["pytest tests/test_runs_http.py"],
                    "unresolved_risks": [],
                    "next_actions": ["review diff"],
                }
            ),
            content_type="application/json",
            tool_name="task.progress",
        )

    import asyncio

    asyncio.run(create_ledgers())

    diag = client.get(
        f"/v1/runs/{run_id}/diagnostics",
        headers={"Authorization": "Bearer tok"},
    )
    assert diag.status_code == 200
    ledger = diag.json()["feature_ledger"]
    assert ledger["summary"] == "Ready for review"
    assert ledger["status"] == "verified"
    assert ledger["acceptance_criteria"] == ["diagnostics exposes latest ledger"]
    assert ledger["validation_commands"] == ["pytest tests/test_runs_http.py"]


def test_run_diagnostics_marks_missing_progress_ledger_for_handoff(client: TestClient) -> None:
    store = client.app.state.store

    async def create_completed_run() -> str:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="Long task",
            workspace_path=None,
        )
        await store.append_event(
            run["id"],
            "tool.completed",
            {"tool": "task.verify", "name": "task.verify", "status": "ok"},
        )
        await store.append_event(run["id"], "run.completed", {"final_text": "done"})
        await store.update_run_status(run["id"], "completed")
        return run["id"]

    import asyncio

    run_id = asyncio.run(create_completed_run())

    diag = client.get(
        f"/v1/runs/{run_id}/diagnostics",
        headers={"Authorization": "Bearer tok"},
    )
    assert diag.status_code == 200
    handoff = diag.json()["handoff"]
    assert handoff["ledger_state"] == "missing"
    assert any("Progress ledger missing" in blocker for blocker in handoff["blockers"])
    assert any("task.progress" in action for action in handoff["next_actions"])


def test_run_diagnostics_marks_missing_progress_ledger_for_waiting_input(
    client: TestClient,
) -> None:
    store = client.app.state.store

    async def create_waiting_input_run() -> str:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="Ask a clarifying question",
            workspace_path=None,
        )
        await store.append_event(
            run["id"],
            "tool.completed",
            {"tool": "read_file", "name": "read_file", "status": "ok"},
        )
        await store.append_event(
            run["id"],
            "question.asked",
            {"request_id": "q1", "questions": [{"question": "Which file?"}]},
        )
        await store.append_event(run["id"], "run.waiting", {"next": ["tools"], "interrupts": []})
        await store.update_run_status(run["id"], "waiting_input")
        return run["id"]

    import asyncio

    run_id = asyncio.run(create_waiting_input_run())

    diag = client.get(
        f"/v1/runs/{run_id}/diagnostics",
        headers={"Authorization": "Bearer tok"},
    )
    assert diag.status_code == 200
    handoff = diag.json()["handoff"]
    assert handoff["status"] == "waiting_input"
    assert handoff["ledger_state"] == "missing"
    assert any("Progress ledger missing" in blocker for blocker in handoff["blockers"])
    assert any("task.progress" in action for action in handoff["next_actions"])


def test_run_diagnostics_marks_stale_progress_ledger_after_later_tool_event(
    client: TestClient,
) -> None:
    store = client.app.state.store

    async def create_stale_ledger_run() -> str:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="Long task",
            workspace_path=None,
        )
        payload = {
            "summary": "Initial plan",
            "status": "in_progress",
            "acceptance_criteria": ["has a ledger"],
            "decisions": [],
            "files_touched": [],
            "validation_commands": [],
            "unresolved_risks": [],
            "next_actions": ["verify"],
        }
        await store.create_artifact(
            run_id=run["id"],
            kind="progress_ledger",
            title="Progress ledger",
            content=json.dumps(payload),
            content_type="application/json",
            tool_name="task.progress",
            metadata=payload,
        )
        await store.append_event(
            run["id"],
            "tool.completed",
            {"tool": "task.verify", "name": "task.verify", "status": "ok"},
        )
        await store.update_run_status(run["id"], "completed")
        return run["id"]

    import asyncio

    run_id = asyncio.run(create_stale_ledger_run())

    diag = client.get(
        f"/v1/runs/{run_id}/diagnostics",
        headers={"Authorization": "Bearer tok"},
    )
    assert diag.status_code == 200
    handoff = diag.json()["handoff"]
    assert handoff["ledger_state"] == "stale"
    assert any(
        "Progress ledger stale after tool.completed" in blocker for blocker in handoff["blockers"]
    )
    assert any("Refresh task.progress" in action for action in handoff["next_actions"])


def test_run_diagnostics_keeps_fresh_ledger_across_passive_waiting_events(
    client: TestClient,
) -> None:
    store = client.app.state.store

    async def create_waiting_run_with_fresh_ledger() -> str:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="Needs approval",
            workspace_path=None,
        )
        payload = {
            "summary": "Ready for the user's approval",
            "status": "blocked",
            "acceptance_criteria": ["approval card is visible"],
            "decisions": ["wait for user approval"],
            "files_touched": [],
            "validation_commands": [],
            "unresolved_risks": ["pending approval"],
            "next_actions": ["approve or deny the pending request"],
        }
        await store.create_artifact(
            run_id=run["id"],
            kind="progress_ledger",
            title="Progress ledger",
            content=json.dumps(payload),
            content_type="application/json",
            tool_name="task.progress",
            metadata=payload,
        )
        await store.append_event(
            run["id"],
            "permission.required",
            {"request_id": "perm_1", "tool": "write_file", "tool_name": "write_file"},
        )
        await store.append_event(run["id"], "run.waiting", {"next": ["tools"], "interrupts": []})
        await store.update_run_status(run["id"], "waiting_permission")
        return run["id"]

    import asyncio

    run_id = asyncio.run(create_waiting_run_with_fresh_ledger())

    diag = client.get(
        f"/v1/runs/{run_id}/diagnostics",
        headers={"Authorization": "Bearer tok"},
    )
    assert diag.status_code == 200
    handoff = diag.json()["handoff"]
    assert handoff["ledger_state"] == "fresh"
    assert not any("Progress ledger stale" in blocker for blocker in handoff["blockers"])


def test_run_diagnostics_classifies_quota_failures(client: TestClient) -> None:
    store = client.app.state.store

    async def create_failed_run() -> str:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="Call a quota-limited provider",
            workspace_path=None,
        )
        await store.append_event(
            run["id"],
            "run.failed",
            {"error_code": "provider_quota_exceeded", "message": "provider quota exhausted"},
        )
        await store.update_run_status(run["id"], "failed")
        return run["id"]

    import asyncio

    run_id = asyncio.run(create_failed_run())

    diag = client.get(
        f"/v1/runs/{run_id}/diagnostics",
        headers={"Authorization": "Bearer tok"},
    )
    assert diag.status_code == 200
    failure = diag.json()["handoff"]["failure"]
    assert failure["category"] == "quota"
    assert failure["code"] == "provider_quota_exceeded"
    assert failure["recoverable"] is True
    assert failure["retryable"] is False
    assert failure["action_kind"] == "user_action"
    assert failure["recovery_action"] == "diagnostics"
    assert "model-service quota" in failure["suggested_action"]


def test_run_diagnostics_reports_latest_task_verification_pass(
    client: TestClient,
) -> None:
    store = client.app.state.store

    async def create_verified_run() -> str:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="Repair then verify",
            workspace_path=None,
        )
        await store.append_event(
            run["id"],
            "tool.failed",
            {
                "tool": "task.verify",
                "name": "task.verify",
                "content": json.dumps(
                    {
                        "ok": "false",
                        "results": [
                            {
                                "kind": "file_contains",
                                "ok": False,
                                "detail": "substring absent in report.txt",
                            }
                        ],
                        "pass_count": "0",
                        "fail_count": "1",
                    }
                ),
                "status": "error",
            },
        )
        await store.append_event(
            run["id"],
            "tool.completed",
            {
                "tool": "task.verify",
                "name": "task.verify",
                "content": json.dumps(
                    {
                        "ok": "true",
                        "results": [
                            {
                                "kind": "file_contains",
                                "ok": True,
                                "detail": "substring found in report.txt",
                            }
                        ],
                        "pass_count": "1",
                        "fail_count": "0",
                    }
                ),
                "status": "ok",
            },
        )
        await store.update_run_status(run["id"], "completed")
        return run["id"]

    import asyncio

    run_id = asyncio.run(create_verified_run())

    diag = client.get(
        f"/v1/runs/{run_id}/diagnostics",
        headers={"Authorization": "Bearer tok"},
    )
    assert diag.status_code == 200
    handoff = diag.json()["handoff"]
    assert handoff["verification"]["status"] == "passed"
    assert "substring found" in handoff["verification"]["reason"]
    assert handoff["failure"] is None
    assert not any("task.verify" in blocker for blocker in handoff["blockers"])


def test_run_diagnostics_suppresses_recovered_tool_failure_for_completed_run(
    client: TestClient,
) -> None:
    store = client.app.state.store

    async def create_recovered_run() -> str:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="Search then answer",
            workspace_path=None,
        )
        await store.append_event(
            run["id"],
            "tool.failed",
            {
                "tool": "web.search",
                "name": "web.search",
                "error_code": "gateway_transient_response",
                "content": "HTTP 503 from gateway",
                "retryable": True,
            },
        )
        await store.append_event(
            run["id"],
            "tool.completed",
            {
                "tool": "web.search",
                "name": "web.search",
                "content": json.dumps({"ok": "true", "results": [{"title": "answer"}]}),
                "status": "ok",
            },
        )
        await store.update_run_status(run["id"], "completed")
        return run["id"]

    import asyncio

    run_id = asyncio.run(create_recovered_run())

    diag = client.get(
        f"/v1/runs/{run_id}/diagnostics",
        headers={"Authorization": "Bearer tok"},
    )
    assert diag.status_code == 200
    handoff = diag.json()["handoff"]
    assert handoff["status"] == "completed"
    assert handoff["failure"] is None
    assert not any("gateway_transient_response" in blocker for blocker in handoff["blockers"])
    assert "tool.failed" in handoff["recent_event_types"]


def test_run_diagnostics_reports_latest_task_verification_failure(
    client: TestClient,
) -> None:
    store = client.app.state.store

    async def create_unverified_run() -> str:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="Verify and report",
            workspace_path=None,
        )
        await store.append_event(
            run["id"],
            "tool.failed",
            {
                "tool": "task.verify",
                "name": "task.verify",
                "content": json.dumps(
                    {
                        "ok": "false",
                        "results": [
                            {
                                "kind": "file_exists",
                                "ok": False,
                                "detail": "file missing: report.txt",
                            }
                        ],
                        "pass_count": "0",
                        "fail_count": "1",
                    }
                ),
                "status": "error",
            },
        )
        await store.update_run_status(run["id"], "completed")
        return run["id"]

    import asyncio

    run_id = asyncio.run(create_unverified_run())

    diag = client.get(
        f"/v1/runs/{run_id}/diagnostics",
        headers={"Authorization": "Bearer tok"},
    )
    assert diag.status_code == 200
    handoff = diag.json()["handoff"]
    assert handoff["verification"]["status"] == "failed"
    assert "file missing" in handoff["verification"]["reason"]
    assert any("Latest task.verify failed" in blocker for blocker in handoff["blockers"])
    assert any("Fix the failing verification" in action for action in handoff["next_actions"])


def test_run_diagnostics_classifies_auth_failures(client: TestClient) -> None:
    store = client.app.state.store

    async def create_failed_run() -> str:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="Call model provider",
            workspace_path=None,
        )
        await store.append_event(
            run["id"],
            "run.failed",
            {
                "error_code": "unauthorized",
                "content": "The model provider rejected its credential.",
            },
        )
        await store.update_run_status(run["id"], "failed")
        return run["id"]

    import asyncio

    run_id = asyncio.run(create_failed_run())

    diag = client.get(
        f"/v1/runs/{run_id}/diagnostics",
        headers={"Authorization": "Bearer tok"},
    )
    assert diag.status_code == 200
    failure = diag.json()["handoff"]["failure"]
    assert failure["category"] == "auth"
    assert failure["code"] == "unauthorized"
    assert failure["recoverable"] is True
    assert failure["retryable"] is False
    assert failure["action_kind"] == "user_action"
    assert "model-service API Key" in failure["suggested_action"]


def test_run_diagnostics_classifies_transient_failures(client: TestClient) -> None:
    store = client.app.state.store

    async def create_failed_run() -> str:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="Call model",
            workspace_path=None,
        )
        await store.append_event(
            run["id"],
            "run.failed",
            {"type": "TimeoutError", "error": "request timed out"},
        )
        await store.update_run_status(run["id"], "failed")
        return run["id"]

    import asyncio

    run_id = asyncio.run(create_failed_run())

    diag = client.get(
        f"/v1/runs/{run_id}/diagnostics",
        headers={"Authorization": "Bearer tok"},
    )
    assert diag.status_code == 200
    failure = diag.json()["handoff"]["failure"]
    assert failure["category"] == "transient"
    assert failure["code"] == "TimeoutError"
    assert failure["recoverable"] is True
    assert failure["retryable"] is True
    assert failure["action_kind"] == "retry"
    assert failure["recovery_action"] == "retry"
    assert "Retry" in failure["suggested_action"]


def test_model_service_error_becomes_structured_run_failure(monkeypatch) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="jdl-runs-model-error-"))
    os.environ["SHEJANE_RUNTIME_TOKEN"] = "tok"
    monkeypatch.delenv("SHEJANE_RUNTIME_MCP_SERVERS", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("SHEJANE_RUNTIME_SKILLS_PATH", str(tmp / "empty-skills"))

    def handler(request: httpx.Request) -> httpx.Response:
        return _stream_response(
            [
                (
                    "llm.error",
                    json.dumps(
                        {
                            "request_id": "req-rate",
                            "message": "rate limit exceeded",
                            "code": "rate_limit",
                            "recoverable": True,
                            "retryable": True,
                            "provider": "anthropic",
                        }
                    ),
                )
            ]
        )

    monkeypatch.setattr("tests.streaming_model.httpx.AsyncClient", _patched_async_client(handler))
    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_HOST="127.0.0.1",
        SHEJANE_RUNTIME_PORT=17371,
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        r = client.post(
            "/v1/runs",
            headers={"Authorization": "Bearer tok"},
            json=run_command("call the model"),
        )
        assert r.status_code == 200
        run_id = r.json()["id"]

        with client.stream(
            "GET",
            f"/v1/runs/{run_id}/stream",
            headers={"Authorization": "Bearer tok"},
        ) as resp:
            assert resp.status_code == 200
            events = _parse_sse_lines(resp.read().decode("utf-8"))

        envelopes = [event for _name, event in events if isinstance(event, dict)]
        run_failed = [e for e in envelopes if e.get("event_type") == "run.failed"]
        assert len(run_failed) == 1
        failed_payload = run_failed[0]["payload"]
        assert failed_payload["error"] == "rate limit exceeded"
        assert failed_payload["error_code"] == "rate_limit"
        assert failed_payload["request_id"] == "req-rate"
        assert failed_payload["service"] == "anthropic"
        assert failed_payload["recoverable"] is True
        assert failed_payload["retryable"] is True
        assert failed_payload["category"] == "transient"
        assert failed_payload["action_kind"] == "retry"
        assert failed_payload["recovery_action"] == "retry"
        assert "Retry" in failed_payload["suggested_action"]

        diag = client.get(
            f"/v1/runs/{run_id}/diagnostics",
            headers={"Authorization": "Bearer tok"},
        )
        assert diag.status_code == 200
        failure = diag.json()["handoff"]["failure"]
        assert failure["category"] == "transient"
        assert failure["code"] == "rate_limit"
        assert failure["recoverable"] is True
        assert failure["retryable"] is True
        assert failure["recovery_action"] == "retry"
        assert failure["source_event_type"] == "run.failed"


def test_run_diagnostics_respects_tool_failure_retry_fields(client: TestClient) -> None:
    store = client.app.state.store

    async def create_failed_run() -> str:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="Search web",
            workspace_path=None,
        )
        await store.append_event(
            run["id"],
            "tool.failed",
            {
                "tool": "web.search",
                "error_code": "provider_busy",
                "content": "provider is saturated",
                "recoverable": True,
                "retryable": True,
            },
        )
        await store.update_run_status(run["id"], "failed")
        return run["id"]

    import asyncio

    run_id = asyncio.run(create_failed_run())

    diag = client.get(
        f"/v1/runs/{run_id}/diagnostics",
        headers={"Authorization": "Bearer tok"},
    )
    assert diag.status_code == 200
    failure = diag.json()["handoff"]["failure"]
    assert failure["category"] == "transient"
    assert failure["code"] == "provider_busy"
    assert failure["recoverable"] is True
    assert failure["retryable"] is True
    assert "Retry" in failure["suggested_action"]


def test_cancel_unknown_run_returns_not_found(client: TestClient) -> None:
    r = client.post(
        "/v1/runs/run_nope/cancel",
        headers={"Authorization": "Bearer tok"},
    )
    assert r.status_code == 404


def test_untyped_resume_route_is_not_exposed(client: TestClient) -> None:
    assert "/v1/runs/{run_id}/resume" not in client.app.openapi()["paths"]


def test_multi_permission_batch_waits_for_all_decisions_before_resume(
    client: TestClient,
    monkeypatch,
) -> None:
    store = client.app.state.store
    resume_calls: list[dict] = []

    async def create_waiting_run() -> tuple[str, dict, dict]:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="Approve two tools",
            workspace_path=None,
        )
        await store.append_event(run["id"], "run.started", {"goal": run["goal"]})
        first = await store.create_permission(
            run_id=run["id"],
            tool_call_id="",
            tool_name="write_file",
            arguments={"path": "a.txt"},
            wait_cycle_id="wait_batch",
            interrupt_id="interrupt_tools",
            action_index=0,
        )
        second = await store.create_permission(
            run_id=run["id"],
            tool_call_id="",
            tool_name="execute",
            arguments={"command": "make test"},
            wait_cycle_id="wait_batch",
            interrupt_id="interrupt_tools",
            action_index=1,
        )
        for record in (first, second):
            await store.append_event(
                run["id"],
                "permission.required",
                {
                    "request_id": record["id"],
                    "tool": record["tool_name"],
                    "tool_name": record["tool_name"],
                },
            )
        await store.append_event(run["id"], "run.waiting", {"next": ["tools"], "interrupts": []})
        await store.update_run_status(run["id"], "waiting_permission")
        return run["id"], first, second

    async def fake_resume_run(*, run_id: str, decision: dict) -> bool:
        resume_calls.append({"run_id": run_id, "decision": decision})
        return True

    import asyncio

    run_id, first, second = asyncio.run(create_waiting_run())
    monkeypatch.setattr(client.app.state.coordinator, "resume_run", fake_resume_run)

    r1 = client.post(
        f"/v1/permissions/{first['id']}",
        headers={"Authorization": "Bearer tok"},
        json={"decision": "approve"},
    )
    assert r1.status_code == 200
    assert r1.json()["resumed"] is False
    assert resume_calls == []

    r2 = client.post(
        f"/v1/permissions/{second['id']}",
        headers={"Authorization": "Bearer tok"},
        json={"decision": "deny"},
    )
    assert r2.status_code == 200
    assert r2.json()["resumed"] is True
    assert resume_calls == [
        {
            "run_id": run_id,
            "decision": {
                "interrupt_tools": {
                    "decisions": [
                        {"type": "approve"},
                        {"type": "reject", "message": "Tool execution denied by user."},
                    ]
                }
            },
        }
    ]


def test_permission_decision_is_idempotent_and_conflicting_replay_is_rejected(
    client: TestClient,
    monkeypatch,
) -> None:
    store = client.app.state.store
    resume_calls: list[dict] = []

    async def prepare() -> dict:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="write exact file",
            workspace_path=None,
        )
        permission = await store.create_permission(
            run_id=run["id"],
            tool_call_id="call_exact",
            operation_id="toolop_exact",
            tool_name="write_file",
            arguments={"file_path": "a.txt", "text": "A"},
            arguments_hash="hash_exact",
            risk="workspace_write",
        )
        await store.append_event(run["id"], "permission.required", {"request_id": permission["id"]})
        await store.append_event(
            run["id"], "run.waiting", {"next": ["ToolReviewMiddleware.after_model"]}
        )
        await store.update_run_status(run["id"], "waiting_permission")
        return permission

    async def fake_resume_run(*, run_id: str, decision: dict) -> bool:
        resume_calls.append({"run_id": run_id, "decision": decision})
        return True

    permission = asyncio.run(prepare())
    monkeypatch.setattr(client.app.state.coordinator, "resume_run", fake_resume_run)
    endpoint = f"/v1/permissions/{permission['id']}"
    headers = {"Authorization": "Bearer tok"}
    body = {"decision": "approve", "scope": "run"}

    first = client.post(endpoint, headers=headers, json=body)
    replay = client.post(endpoint, headers=headers, json=body)
    conflict = client.post(
        endpoint,
        headers=headers,
        json={"decision": "deny", "scope": "run"},
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["resumed"] is True
    assert conflict.status_code == 409
    # Identical replay rechecks/enqueues the durable resume owner so a crash
    # between decision commit and job creation cannot strand the run.
    assert len(resume_calls) == 2


def test_irreversible_permission_cannot_be_approved_for_the_rest_of_the_run(
    client: TestClient,
) -> None:
    store = client.app.state.store

    async def prepare() -> dict:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="delete a slide",
            workspace_path=None,
        )
        permission = await store.create_permission(
            run_id=run["id"],
            tool_call_id="call_delete_slide",
            operation_id="toolop_delete_slide",
            tool_name="office.delete_slide",
            arguments={"path": "deck.pptx", "index": 1},
            arguments_hash="hash_delete_slide",
            risk="workspace_write",
        )
        await store.update_run_status(run["id"], "waiting_permission")
        return permission

    permission = asyncio.run(prepare())
    response = client.post(
        f"/v1/permissions/{permission['id']}",
        headers={"Authorization": "Bearer tok"},
        json={"decision": "approve", "scope": "run"},
    )
    command_response = client.post(
        "/v1/commands",
        headers={"Authorization": "Bearer tok"},
        json={
            "type": "permission.resolve",
            "command_id": "reject_delete_run_scope",
            "permission_id": permission["id"],
            "decision": "approve",
            "scope": "run",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "this operation cannot be approved for the rest of the run"
    assert command_response.status_code == 400
    assert command_response.json()["detail"] == response.json()["detail"]
    persisted = asyncio.run(store.get_permission(permission["id"]))
    assert persisted is not None
    assert persisted["status"] == "pending"


def test_permission_edit_preserves_tool_name_and_resumes_with_edited_args(
    client: TestClient,
    monkeypatch,
) -> None:
    store = client.app.state.store
    resume_calls: list[dict] = []

    async def prepare() -> dict:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="edit command",
            workspace_path=None,
        )
        permission = await store.create_permission(
            run_id=run["id"],
            tool_call_id="call_edit",
            operation_id="toolop_edit",
            tool_name="execute",
            arguments={"command": "rm -rf build"},
            arguments_hash="hash_edit",
            risk="external_or_unknown",
        )
        await store.append_event(run["id"], "permission.required", {"request_id": permission["id"]})
        await store.append_event(run["id"], "run.waiting", {"next": ["review"]})
        await store.update_run_status(run["id"], "waiting_permission")
        return permission

    async def fake_resume_run(*, run_id: str, decision: dict) -> bool:
        resume_calls.append({"run_id": run_id, "decision": decision})
        return True

    permission = asyncio.run(prepare())
    monkeypatch.setattr(client.app.state.coordinator, "resume_run", fake_resume_run)
    endpoint = f"/v1/permissions/{permission['id']}"
    headers = {"Authorization": "Bearer tok"}
    edited_action = {"name": "execute", "args": {"command": "make test"}}

    response = client.post(
        endpoint,
        headers=headers,
        json={"decision": "edit", "scope": "once", "edited_action": edited_action},
    )
    changed_name = client.post(
        endpoint,
        headers=headers,
        json={
            "decision": "edit",
            "scope": "once",
            "edited_action": {"name": "write_file", "args": {}},
        },
    )

    assert response.status_code == 200, response.text
    assert resume_calls[0]["decision"] == {
        permission["interrupt_id"]: {
            "decisions": [{"type": "edit", "edited_action": edited_action}]
        }
    }
    assert changed_name.status_code == 400


def test_tool_reconciliation_is_persisted_idempotent_and_resumes(
    client: TestClient,
) -> None:
    store = client.app.state.store

    async def prepare() -> tuple[str, str]:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="reconcile uncertain tool",
            workspace_path=None,
        )
        operation_id = "toolop_uncertain"
        await store.prepare_tool_receipt(
            operation_id=operation_id,
            run_id=str(run["id"]),
            execution_attempt_id="job:1",
            tool_call_id="call-uncertain",
            tool_name="execute",
            tool_version="graph-v1",
            arguments_hash="args-hash",
            arguments_json="{}",
            risk="external_or_unknown",
        )
        await store.begin_tool_receipt(
            operation_id=operation_id,
            run_id=str(run["id"]),
            execution_attempt_id="job:1",
        )
        await store.settle_tool_receipt(
            operation_id=operation_id,
            run_id=str(run["id"]),
            status="outcome_unknown",
            error_type="TimeoutError",
        )
        await store.create_tool_reconciliation(
            run_id=run["id"],
            operation_id=operation_id,
            wait_cycle_id="wait_uncertain",
            interrupt_id="interrupt_uncertain",
            payload={"operation_id": operation_id, "tool_name": "execute"},
        )
        await store.update_run_status(run["id"], "waiting_permission")
        return str(run["id"]), operation_id

    _run_id, operation_id = asyncio.run(prepare())
    endpoint = f"/v1/tool-reconciliations/{operation_id}"
    headers = {"Authorization": "Bearer tok"}
    body = {"decision": "retry_not_executed"}

    first = client.post(endpoint, headers=headers, json=body)
    replay = client.post(endpoint, headers=headers, json=body)
    conflict = client.post(endpoint, headers=headers, json={"decision": "confirmed_completed"})

    assert first.status_code == replay.status_code == 200
    assert first.json() == {
        "operation_id": operation_id,
        "resolved": True,
        "decision": "retry_not_executed",
        "resumed": True,
    }
    assert replay.json() == first.json()
    assert conflict.status_code == 409


def test_tool_reconcile_command_is_idempotent_and_rejects_changed_decisions(
    client: TestClient,
) -> None:
    store = client.app.state.store

    async def prepare() -> tuple[str, str, str]:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="reconcile durably",
            workspace_path=None,
        )
        operation_id = "toolop_command"
        await store.prepare_tool_receipt(
            operation_id=operation_id,
            run_id=str(run["id"]),
            execution_attempt_id="job:command",
            tool_call_id="call-command",
            tool_name="execute",
            tool_version="graph-v1",
            arguments_hash="args-command",
            arguments_json="{}",
            risk="external_or_unknown",
        )
        await store.begin_tool_receipt(
            operation_id=operation_id,
            run_id=str(run["id"]),
            execution_attempt_id="job:command",
        )
        await store.settle_tool_receipt(
            operation_id=operation_id,
            run_id=str(run["id"]),
            status="outcome_unknown",
            error_type="TimeoutError",
        )
        await store.create_tool_reconciliation(
            run_id=str(run["id"]),
            operation_id=operation_id,
            wait_cycle_id="wait_command",
            interrupt_id="interrupt_command",
            payload={"operation_id": operation_id, "tool_name": "execute"},
        )
        permission = await store.create_permission(
            run_id=str(run["id"]),
            tool_call_id="call-command-permission",
            tool_name="write_file",
            arguments={"path": "result.txt"},
            wait_cycle_id="wait_command",
            interrupt_id="interrupt_permission",
        )
        await store.update_run_status(str(run["id"]), "waiting_permission")
        return str(run["id"]), operation_id, str(permission["id"])

    run_id, operation_id, permission_id = asyncio.run(prepare())
    headers = {"Authorization": "Bearer tok"}
    command = {
        "type": "tool.reconcile",
        "command_id": "reconcile_tool_command",
        "operation_id": operation_id,
        "decision": "retry_not_executed",
    }

    permission = client.post(
        "/v1/commands",
        headers=headers,
        json={
            "type": "permission.resolve",
            "command_id": "resolve_before_reconciliation",
            "permission_id": permission_id,
            "decision": "deny",
        },
    )
    assert permission.status_code == 200
    assert permission.json()["resumed"] is False

    accepted = client.post("/v1/commands", headers=headers, json=command)
    replay = client.post("/v1/commands", headers=headers, json=command)
    conflict = client.post(
        "/v1/commands",
        headers=headers,
        json={**command, "decision": "confirmed_completed"},
    )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {
        "type": "tool.reconcile",
        "command_id": "reconcile_tool_command",
        "operation_id": operation_id,
        "run_id": run_id,
        "resolved": True,
        "decision": "retry_not_executed",
        "resumed": True,
    }
    assert replay.json() == accepted.json()
    assert conflict.status_code == 409


def test_tool_reconcile_command_settles_an_ancestor_receipt(
    client: TestClient,
) -> None:
    store = client.app.state.store

    async def prepare() -> tuple[str, str, str]:
        source = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="source side effect",
            workspace_path=None,
        )
        source_operation = "toolop_ancestor_source"
        await store.prepare_tool_receipt(
            operation_id=source_operation,
            run_id=str(source["id"]),
            execution_attempt_id="job:ancestor-source",
            tool_call_id="call-ancestor-source",
            tool_name="execute",
            tool_version="graph-v1",
            arguments_hash="ancestor-args",
            arguments_json="{}",
            risk="external_or_unknown",
        )
        await store.begin_tool_receipt(
            operation_id=source_operation,
            run_id=str(source["id"]),
            execution_attempt_id="job:ancestor-source",
        )
        await store.settle_tool_receipt(
            operation_id=source_operation,
            run_id=str(source["id"]),
            status="outcome_unknown",
            error_type="TimeoutError",
        )
        child = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="continue from source",
            workspace_path=None,
            parent_run_id=str(source["id"]),
        )
        child_operation = "toolop_ancestor_child"
        await store.prepare_tool_receipt(
            operation_id=child_operation,
            run_id=str(child["id"]),
            execution_attempt_id="job:ancestor-child",
            tool_call_id="call-ancestor-child",
            tool_name="execute",
            tool_version="graph-v1",
            arguments_hash="ancestor-args",
            arguments_json="{}",
            risk="external_or_unknown",
        )
        await store.create_tool_reconciliation(
            run_id=str(child["id"]),
            operation_id=child_operation,
            wait_cycle_id="wait_ancestor_command",
            interrupt_id="interrupt_ancestor_command",
            payload={
                "operation_id": child_operation,
                "prior_operation_id": source_operation,
                "tool_name": "execute",
            },
        )
        await store.update_run_status(str(child["id"]), "waiting_permission")
        return str(source["id"]), str(child["id"]), child_operation

    source_run_id, child_run_id, operation_id = asyncio.run(prepare())
    headers = {"Authorization": "Bearer tok"}
    response = client.post(
        "/v1/commands",
        headers=headers,
        json={
            "type": "tool.reconcile",
            "command_id": "reconcile_ancestor_command",
            "operation_id": operation_id,
            "decision": "confirmed_completed",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["resumed"] is True
    source_diagnostics = client.get(
        f"/v1/runs/{source_run_id}/diagnostics",
        headers=headers,
    ).json()
    child_diagnostics = client.get(
        f"/v1/runs/{child_run_id}/diagnostics",
        headers=headers,
    ).json()
    assert source_diagnostics["tool_receipts"][0]["status"] == "completed"
    assert child_diagnostics["tool_receipts"][0]["status"] == "completed"


def test_plan_approval_resolution_emits_event_and_resumes(
    client: TestClient,
) -> None:
    store = client.app.state.store

    async def create_waiting_run() -> tuple[str, dict]:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="Plan before editing",
            workspace_path=None,
        )
        approval = await store.create_plan_approval(
            run_id=run["id"],
            tool_call_id="call-plan",
            todos=[{"content": "Write tests", "status": "pending"}],
            summary="Write tests",
        )
        await store.append_event(
            run["id"],
            "plan.approval_required",
            {
                "request_id": approval["id"],
                "tool_call_id": "call-plan",
                "todos": approval["todos"],
            },
        )
        await store.append_event(run["id"], "run.waiting", {"next": ["model"], "interrupts": []})
        await store.update_run_status(run["id"], "waiting_input")
        return run["id"], approval

    import asyncio

    run_id, approval = asyncio.run(create_waiting_run())

    response = client.post(
        f"/v1/plans/{approval['id']}",
        headers={"Authorization": "Bearer tok"},
        json={"decision": "modify", "instructions": "Add verification."},
    )

    assert response.status_code == 200
    assert response.json() == {
        "approval_id": approval["id"],
        "resolved": True,
        "decision": "modify",
        "resumed": True,
    }
    resolved = asyncio.run(store.get_plan_approval(approval["id"]))
    assert resolved["status"] == "modified"
    assert resolved["instructions"] == "Add verification."

    events = asyncio.run(store.events_since(run_id, after_seq=0))
    assert any(event["event_type"] == "plan.approval_resolved" for event in events)


def test_history_is_not_pretruncated_before_token_aware_compaction(
    monkeypatch,
) -> None:
    """The Runtime passes accepted history to Deep Agents unchanged.

    Token-aware summarization owns compaction; the coordinator must not apply
    a second message-count cap or manufacture a heuristic system summary.
    """
    tmp = Path(tempfile.mkdtemp(prefix="jdl-runs-trunc-"))
    os.environ["SHEJANE_RUNTIME_TOKEN"] = "tok"
    monkeypatch.delenv("SHEJANE_RUNTIME_MCP_SERVERS", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    # Pin skills lookup to an empty tmp dir — otherwise this test picks
    # up the dev machine's real `~/.claude/skills/` (which on Jimmy's
    # laptop has ~25 skills totaling ~30 KB), and the skills layer
    # crowds the <state> block out of the 8 KB prompt budget. The
    # invariant under test (dropped-count surfaced in state) is
    # independent of skills, so isolating them is correct.
    monkeypatch.setenv("SHEJANE_RUNTIME_SKILLS_PATH", str(tmp / "empty-skills"))

    captured_requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        try:
            captured_requests.append(json.loads(body))
        except json.JSONDecodeError:
            pass
        return _stream_response(
            [
                ("llm.delta", '{"content_delta": "ok"}'),
                ("llm.done", '{"request_id": "r", "finish_reason": "stop"}'),
            ]
        )

    monkeypatch.setattr("tests.streaming_model.httpx.AsyncClient", _patched_async_client(handler))

    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_HOST="127.0.0.1",
        SHEJANE_RUNTIME_PORT=17371,
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        # This exceeded the removed 40-message coordinator cap.
        history = []
        for i in range(60):
            role = "user" if i % 2 == 0 else "assistant"
            history.append({"role": role, "content": f"msg-{i:03d}"})

        r = client.post(
            "/v1/runs",
            headers={"Authorization": "Bearer tok"},
            json=run_command("what was message 5?", history=history),
        )
        assert r.status_code == 200
        run_id = r.json()["id"]

        with client.stream(
            "GET",
            f"/v1/runs/{run_id}/stream",
            headers={"Authorization": "Bearer tok"},
        ) as resp:
            assert resp.status_code == 200
            resp.read()

    assert captured_requests, "expected at least one backend LLM call"
    first = captured_requests[0]
    messages = first.get("messages", [])
    system_blocks = [m["content"] for m in messages if m.get("role") == "system"]
    joined_system = "\n".join(system_blocks)
    assert "已省略本对话早期" not in joined_system
    assert "早期对话压缩摘要" not in joined_system
    assert "第 61 轮" in joined_system
    user_or_asst = [m for m in messages if m.get("role") in {"user", "assistant"}]
    assert len(user_or_asst) == 61
    assert user_or_asst[0]["content"] == "msg-000"
    assert user_or_asst[-2]["content"] == "msg-059"


def test_removed_history_limit_setting_does_not_change_model_input(monkeypatch) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="jdl-runs-history-override-"))
    os.environ["SHEJANE_RUNTIME_TOKEN"] = "tok"
    monkeypatch.delenv("SHEJANE_RUNTIME_MCP_SERVERS", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("SHEJANE_RUNTIME_SKILLS_PATH", str(tmp / "empty-skills"))

    captured_requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        try:
            captured_requests.append(json.loads(body))
        except json.JSONDecodeError:
            pass
        return _stream_response(
            [
                ("llm.delta", '{"content_delta": "ok"}'),
                ("llm.done", '{"request_id": "r", "finish_reason": "stop"}'),
            ]
        )

    monkeypatch.setattr("tests.streaming_model.httpx.AsyncClient", _patched_async_client(handler))

    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_HOST="127.0.0.1",
        SHEJANE_RUNTIME_PORT=17371,
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        history = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg-{i:03d}"}
            for i in range(15)
        ]

        r = client.post(
            "/v1/runs",
            headers={"Authorization": "Bearer tok"},
            json=run_command(
                "summarize",
                history=history,
                settings={"max_history_turns": 10},
            ),
        )
        assert r.status_code == 200
        run_id = r.json()["id"]

        with client.stream(
            "GET",
            f"/v1/runs/{run_id}/stream",
            headers={"Authorization": "Bearer tok"},
        ) as resp:
            assert resp.status_code == 200
            resp.read()

    assert captured_requests, "expected at least one backend LLM call"
    first = captured_requests[0]
    messages = first.get("messages", [])
    joined_system = "\n".join(m["content"] for m in messages if m.get("role") == "system")
    assert "已省略本对话早期" not in joined_system
    user_or_asst = [m for m in messages if m.get("role") in {"user", "assistant"}]
    assert len(user_or_asst) == 16
    assert any(m.get("content") == "msg-000" for m in user_or_asst)
    assert any(m.get("content") == "msg-014" for m in user_or_asst)


def test_transport_omission_marker_passes_through_without_runtime_rewrite(
    monkeypatch,
) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="jdl-runs-client-marker-"))
    os.environ["SHEJANE_RUNTIME_TOKEN"] = "tok"
    monkeypatch.delenv("SHEJANE_RUNTIME_MCP_SERVERS", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("SHEJANE_RUNTIME_SKILLS_PATH", str(tmp / "empty-skills"))

    captured_requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        try:
            captured_requests.append(json.loads(body))
        except json.JSONDecodeError:
            pass
        return _stream_response(
            [
                ("llm.delta", '{"content_delta": "ok"}'),
                ("llm.done", '{"request_id": "r", "finish_reason": "stop"}'),
            ]
        )

    monkeypatch.setattr("tests.streaming_model.httpx.AsyncClient", _patched_async_client(handler))

    settings = reset_settings_for_tests(
        SHEJANE_RUNTIME_HOST="127.0.0.1",
        SHEJANE_RUNTIME_PORT=17371,
        SHEJANE_RUNTIME_TOKEN="tok",
        data_dir=tmp,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        history = [
            {
                "role": "user",
                "content": (
                    "【上下文提示｜对话较长，已省略更早的 25 条消息，仅保留最近内容；如需更早信息请重述。】\n"
                    "早期摘要：\n- 用户: 重要决定：所有 API 错误必须保留 request_id"
                ),
            },
            *[
                {
                    "role": "user" if i % 2 == 0 else "assistant",
                    "content": f"msg-{i:03d}",
                }
                for i in range(10)
            ],
        ]

        r = client.post(
            "/v1/runs",
            headers={"Authorization": "Bearer tok"},
            json=run_command(
                "what did we decide about request ids?",
                history=history,
            ),
        )
        assert r.status_code == 200
        run_id = r.json()["id"]

        with client.stream(
            "GET",
            f"/v1/runs/{run_id}/stream",
            headers={"Authorization": "Bearer tok"},
        ) as resp:
            assert resp.status_code == 200
            resp.read()

    assert captured_requests, "expected at least one backend LLM call"
    messages = captured_requests[0].get("messages", [])
    user_or_asst = [m for m in messages if m.get("role") in {"user", "assistant"}]
    joined_system = "\n".join(m["content"] for m in messages if m.get("role") == "system")

    assert len(user_or_asst) == 12
    assert "已省略本对话早期" not in joined_system
    assert "早期对话压缩摘要" not in joined_system
    assert "上下文提示｜对话较长" not in joined_system
    assert user_or_asst[0]["content"].startswith("【上下文提示｜对话较长")
    assert "重要决定：所有 API 错误必须保留 request_id" in user_or_asst[0]["content"]
    assert any(m.get("content") == "msg-000" for m in user_or_asst)
    assert any(m.get("content") == "msg-009" for m in user_or_asst)
