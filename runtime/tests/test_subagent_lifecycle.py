from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import HumanMessage, ToolMessage

from shejane_runtime.agent.context_builder import RuntimeContext
from shejane_runtime.api_schemas import LocalSubagentInvocation
from shejane_runtime.auth import LOCAL_OWNER_PRINCIPAL_ID
from shejane_runtime.llm.fake import FakeBackendChatModel
from shejane_runtime.llm.ledger import LedgerChatModel
from shejane_runtime.middleware.tool_execution import ToolExecutionMiddleware
from shejane_runtime.store.sqlite import (
    LocalStore,
    RunResultConflictError,
    ToolReceiptStateError,
)
from shejane_runtime.tools.runtime import (
    RuntimeToolExecution,
    bind_runtime_tool_execution,
    current_runtime_tool_execution,
)


async def _store_and_run(tmp_path: Path) -> tuple[LocalStore, dict[str, object]]:
    store = await LocalStore.open(tmp_path / "runtime.db")
    run = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="test subagent lifecycle",
        workspace_path=None,
    )
    return store, run


def test_subagent_snapshot_contract_requires_explicit_nullable_fields() -> None:
    schema = LocalSubagentInvocation.model_json_schema()
    assert {
        "parent_operation_id",
        "error_type",
        "started_at",
        "completed_at",
        "usage",
    } <= set(schema["required"])


@pytest.mark.asyncio
async def test_task_receipt_and_spawn_event_roll_back_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, run = await _store_and_run(tmp_path)
    run_id = str(run["id"])

    async def fail_event_append(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("event append failed")

    monkeypatch.setattr(store, "_append_event_uncommitted", fail_event_append)
    try:
        with pytest.raises(RuntimeError, match="event append failed"):
            await store.prepare_tool_receipt(
                operation_id="toolop-atomic",
                run_id=run_id,
                execution_attempt_id="job-atomic:1",
                execution_namespace="main",
                tool_call_id="call-atomic",
                tool_name="task",
                tool_version="graph-v1",
                arguments_hash="atomic-args",
                arguments_json=json.dumps(
                    {"subagent_type": "researcher", "description": "Test atomicity"}
                ),
                risk="control_flow",
            )

        assert await store.list_tool_receipts_for_run(run_id) == []
        assert await store.events_since(run_id) == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_task_parent_must_be_an_existing_receipt_in_the_same_run(
    tmp_path: Path,
) -> None:
    store, first_run = await _store_and_run(tmp_path)
    second_run = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="second run",
        workspace_path=None,
    )
    first_run_id = str(first_run["id"])
    second_run_id = str(second_run["id"])
    await store.prepare_tool_receipt(
        operation_id="toolop-parent-first-run",
        run_id=first_run_id,
        execution_attempt_id="job-parent:1",
        execution_namespace="main",
        tool_call_id="call-parent",
        tool_name="task",
        tool_version="graph-v1",
        arguments_hash="parent-args",
        arguments_json=json.dumps({"subagent_type": "researcher", "description": "Parent task"}),
        risk="control_flow",
    )

    async def prepare_child(*, parent_operation_id: str, operation_id: str) -> None:
        await store.prepare_tool_receipt(
            operation_id=operation_id,
            parent_operation_id=parent_operation_id,
            run_id=second_run_id,
            execution_attempt_id="job-child:1",
            execution_namespace="child",
            tool_call_id=f"call-{operation_id}",
            tool_name="task",
            tool_version="graph-v1",
            arguments_hash=f"args-{operation_id}",
            arguments_json=json.dumps({"subagent_type": "writer", "description": "Child task"}),
            risk="control_flow",
        )

    try:
        with pytest.raises(ToolReceiptStateError, match="same run"):
            await prepare_child(
                parent_operation_id="toolop-parent-first-run",
                operation_id="toolop-cross-run-child",
            )
        with pytest.raises(ToolReceiptStateError, match="does not exist"):
            await prepare_child(
                parent_operation_id="toolop-missing-parent",
                operation_id="toolop-missing-parent-child",
            )
        with pytest.raises(ToolReceiptStateError, match="cannot parent itself"):
            await prepare_child(
                parent_operation_id="toolop-self-parent",
                operation_id="toolop-self-parent",
            )

        assert await store.list_tool_receipts_for_run(second_run_id) == []
        assert await store.events_since(second_run_id) == []
        assert await store.list_subagent_invocations_for_runs([second_run_id]) == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_task_receipt_lifecycle_is_durable_and_replayable(tmp_path: Path) -> None:
    store, run = await _store_and_run(tmp_path)
    run_id = str(run["id"])
    operation_id = "toolop_task_1"
    arguments = {
        "subagent_type": "researcher",
        "description": "Find the primary sources",
    }

    await store.prepare_tool_receipt(
        operation_id=operation_id,
        run_id=run_id,
        execution_attempt_id="job-1:1",
        execution_namespace="main",
        tool_call_id="call-task-1",
        tool_name="task",
        tool_version="graph-v1",
        arguments_hash="args-hash",
        arguments_json=json.dumps(arguments),
        risk="control_flow",
    )
    await store.begin_tool_receipt(
        operation_id=operation_id,
        run_id=run_id,
        execution_attempt_id="job-1:1",
    )
    await store.settle_tool_receipt(
        operation_id=operation_id,
        run_id=run_id,
        status="completed",
        result_json='{"kind":"tool_message"}',
        result_hash="result-hash",
    )

    events = await store.events_since(run_id)
    assert [event["event_type"] for event in events] == [
        "subagent.spawned",
        "subagent.started",
        "subagent.completed",
    ]
    payloads = [json.loads(str(event["payload_json"])) for event in events]
    assert [payload["status"] for payload in payloads] == [
        "queued",
        "running",
        "completed",
    ]
    assert all(payload["operation_id"] == operation_id for payload in payloads)
    assert all(payload["tool_call_id"] == "call-task-1" for payload in payloads)
    assert all(payload["subagent_type"] == "researcher" for payload in payloads)
    assert all(payload["description"] == "Find the primary sources" for payload in payloads)

    await store.close()
    reopened = await LocalStore.open(tmp_path / "runtime.db")
    try:
        assert [event["event_type"] for event in await reopened.events_since(run_id)] == [
            "subagent.spawned",
            "subagent.started",
            "subagent.completed",
        ]
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_subagent_snapshot_uses_receipt_parentage_and_model_ledger_usage(
    tmp_path: Path,
) -> None:
    store, run = await _store_and_run(tmp_path)
    run_id = str(run["id"])
    try:
        await store.prepare_tool_receipt(
            operation_id="toolop_parent",
            run_id=run_id,
            execution_attempt_id="job-1:1",
            execution_namespace="main",
            tool_call_id="call-parent",
            tool_name="task",
            tool_version="graph-v1",
            arguments_hash="parent-args",
            arguments_json=json.dumps(
                {"subagent_type": "researcher", "description": "Research the topic"}
            ),
            risk="control_flow",
        )
        await store.prepare_tool_receipt(
            operation_id="toolop_child",
            parent_operation_id="toolop_parent",
            run_id=run_id,
            execution_attempt_id="job-1:1",
            execution_namespace="child",
            tool_call_id="call-child",
            tool_name="task",
            tool_version="graph-v1",
            arguments_hash="child-args",
            arguments_json=json.dumps(
                {"subagent_type": "writer", "description": "Write the result"}
            ),
            risk="control_flow",
        )
        metered = await store.reserve_model_call(
            run_id=run_id,
            execution_attempt_id="job-1:1",
            model="fake:model",
            max_calls=10,
            parent_tool_operation_id="toolop_child",
        )
        await store.settle_model_call(
            run_id=run_id,
            call_id=str(metered["id"]),
            provider_request_id="provider-1",
            input_tokens=120,
            output_tokens=30,
        )
        unmetered = await store.reserve_model_call(
            run_id=run_id,
            execution_attempt_id="job-1:1",
            model="fake:model",
            max_calls=10,
            parent_tool_operation_id="toolop_child",
        )
        await store.settle_model_call(
            run_id=run_id,
            call_id=str(unmetered["id"]),
            provider_request_id=None,
            input_tokens=None,
            output_tokens=None,
        )

        invocations = await store.list_subagent_invocations_for_runs([run_id])
        child = next(item for item in invocations if item["operation_id"] == "toolop_child")
        assert child == {
            "operation_id": "toolop_child",
            "parent_run_id": run_id,
            "parent_operation_id": "toolop_parent",
            "tool_call_id": "call-child",
            "subagent_type": "writer",
            "description": "Write the result",
            "status": "queued",
            "receipt_status": "prepared",
            "attempt_count": 0,
            "usage": {
                "model_calls": 2,
                "input_tokens": 120,
                "output_tokens": 30,
                "unmetered_calls": 1,
                "outcome_unknown_calls": 0,
            },
            "error_type": None,
            "created_at": child["created_at"],
            "started_at": None,
            "completed_at": None,
            "updated_at": child["updated_at"],
        }
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_model_ledger_attributes_child_usage_to_bound_task_operation(
    tmp_path: Path,
) -> None:
    store, run = await _store_and_run(tmp_path)
    run_id = str(run["id"])
    await store.prepare_tool_receipt(
        operation_id="toolop_child_usage",
        run_id=run_id,
        execution_attempt_id="job-1:1",
        execution_namespace="main",
        tool_call_id="call-child-usage",
        tool_name="task",
        tool_version="graph-v1",
        arguments_hash="child-usage-args",
        arguments_json=json.dumps({"subagent_type": "researcher", "description": "Use the model"}),
        risk="control_flow",
    )
    await store.begin_tool_receipt(
        operation_id="toolop_child_usage",
        run_id=run_id,
        execution_attempt_id="job-1:1",
    )
    model = LedgerChatModel(
        delegate=FakeBackendChatModel(profile={"max_input_tokens": 4096}),
        store=store,
        run_id=run_id,
        execution_attempt_id="job-1:1",
        model_name="fake:model",
        max_calls=10,
        profile={"max_input_tokens": 4096},
    )
    try:
        with bind_runtime_tool_execution(
            RuntimeToolExecution(
                context=object(),
                operation_id="toolop_child_usage",
                tool_call_id="call-child-usage",
            )
        ):
            await model.ainvoke([HumanMessage(content="hello")])

        (row,) = await store.list_model_calls_for_run(run_id)
        assert row["parent_tool_operation_id"] == "toolop_child_usage"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_model_call_parent_must_be_an_existing_receipt_in_the_same_run(
    tmp_path: Path,
) -> None:
    store, first_run = await _store_and_run(tmp_path)
    second_run = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="second model run",
        workspace_path=None,
    )
    first_run_id = str(first_run["id"])
    second_run_id = str(second_run["id"])
    await store.prepare_tool_receipt(
        operation_id="toolop-model-parent",
        run_id=first_run_id,
        execution_attempt_id="job-model-parent:1",
        execution_namespace="main",
        tool_call_id="call-model-parent",
        tool_name="task",
        tool_version="graph-v1",
        arguments_hash="model-parent-args",
        arguments_json=json.dumps(
            {"subagent_type": "researcher", "description": "Parent model call"}
        ),
        risk="control_flow",
    )

    async def reserve(*, parent_tool_operation_id: str) -> None:
        await store.reserve_model_call(
            run_id=second_run_id,
            execution_attempt_id="job-model-child:1",
            model="fake:model",
            max_calls=10,
            parent_tool_operation_id=parent_tool_operation_id,
        )

    try:
        with pytest.raises(ToolReceiptStateError, match="same run"):
            await reserve(parent_tool_operation_id="toolop-model-parent")
        with pytest.raises(ToolReceiptStateError, match="does not exist"):
            await reserve(parent_tool_operation_id="toolop-missing-model-parent")

        assert await store.list_model_calls_for_run(second_run_id) == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_running_subagent_cancellation_is_durably_canceled(tmp_path: Path) -> None:
    store, run = await _store_and_run(tmp_path)
    run_id = str(run["id"])
    context = RuntimeContext(
        store=store,
        run_id=run_id,
        execution_attempt_id="job-cancel:1",
    )
    request = ToolCallRequest(
        tool_call={
            "id": "call-cancel",
            "name": "task",
            "args": {"subagent_type": "writer", "description": "Write slowly"},
            "type": "tool_call",
        },
        tool=None,
        state={"messages": []},
        runtime=SimpleNamespace(context=context),
    )

    async def cancel(_request: ToolCallRequest) -> ToolMessage:
        raise asyncio.CancelledError

    try:
        with pytest.raises(asyncio.CancelledError):
            await ToolExecutionMiddleware().awrap_tool_call(request, cancel)

        receipt = (await store.list_tool_receipts_for_run(run_id))[0]
        assert receipt["status"] == "canceled"
        events = await store.events_since(run_id)
        assert [event["event_type"] for event in events] == [
            "subagent.spawned",
            "subagent.started",
            "subagent.canceled",
        ]
        assert json.loads(str(events[-1]["payload_json"]))["status"] == "canceled"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_cancel_committed_between_precheck_and_begin_fences_tool_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    run, _created = await store.accept_run_command(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        command_id="cmd-cancel-before-begin",
        client_message_id="msg-cancel-before-begin-user",
        assistant_message_id="msg-cancel-before-begin-assistant",
        thread_id="thread-cancel-before-begin",
        user_input="delegate then cancel",
        command_payload={"type": "run.start"},
        goal="delegate then cancel",
        workspace_path=None,
        mode="auto",
    )
    run_id = str(run["id"])
    job = await store.claim_run_job(worker_id="worker-cancel-before-begin")
    assert job is not None
    lease_generation = int(job["lease_generation"])
    attempt_id = f"{job['id']}:{lease_generation}"
    context = RuntimeContext(
        store=store,
        run_id=run_id,
        execution_attempt_id=attempt_id,
    )
    request = ToolCallRequest(
        tool_call={
            "id": "call-cancel-before-begin",
            "name": "task",
            "args": {"subagent_type": "writer", "description": "Must not start"},
            "type": "tool_call",
        },
        tool=None,
        state={"messages": []},
        runtime=SimpleNamespace(context=context),
    )
    original_begin = store.begin_tool_receipt

    async def cancel_then_begin(**kwargs: object) -> dict[str, object]:
        assert await store.request_run_cancel(run_id) == "leased"
        return await original_begin(**kwargs)  # type: ignore[arg-type]

    calls = 0

    async def handler(_request: ToolCallRequest) -> ToolMessage:
        nonlocal calls
        calls += 1
        return ToolMessage(
            content="must not run",
            name="task",
            tool_call_id="call-cancel-before-begin",
        )

    monkeypatch.setattr(store, "begin_tool_receipt", cancel_then_begin)
    try:
        with store.bind_execution_lease(
            job_id=str(job["id"]),
            run_id=run_id,
            lease_owner="worker-cancel-before-begin",
            lease_generation=lease_generation,
        ):
            with pytest.raises(asyncio.CancelledError):
                await ToolExecutionMiddleware().awrap_tool_call(request, handler)

        assert calls == 0
        [receipt] = await store.list_tool_receipts_for_run(run_id)
        assert receipt["status"] == "canceled"
        assert receipt["attempt_count"] == 0
        assert receipt["error_type"] == "RunCanceledBeforeToolStart"
        assert [event["event_type"] for event in await store.events_since(run_id)] == [
            "subagent.spawned",
            "subagent.canceled",
        ]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_parent_run_cancellation_settles_queued_and_waiting_subagents(
    tmp_path: Path,
) -> None:
    store, run = await _store_and_run(tmp_path)
    run_id = str(run["id"])

    async def prepare(operation_id: str, call_id: str) -> None:
        await store.prepare_tool_receipt(
            operation_id=operation_id,
            run_id=run_id,
            execution_attempt_id="job-parent-cancel:1",
            execution_namespace=operation_id,
            tool_call_id=call_id,
            tool_name="task",
            tool_version="graph-v1",
            arguments_hash=f"{operation_id}-args",
            arguments_json=json.dumps({"subagent_type": "researcher", "description": operation_id}),
            risk="control_flow",
        )

    try:
        await prepare("toolop-cancel-queued", "call-cancel-queued")
        await prepare("toolop-cancel-waiting", "call-cancel-waiting")
        await store.begin_tool_receipt(
            operation_id="toolop-cancel-waiting",
            run_id=run_id,
            execution_attempt_id="job-parent-cancel:1",
        )
        await store.settle_tool_receipt(
            operation_id="toolop-cancel-waiting",
            run_id=run_id,
            status="paused",
        )
        await store.update_run_status(run_id, "waiting_permission")

        assert await store.request_run_cancel(run_id) == "waiting"

        receipts = await store.list_tool_receipts_for_run(run_id)
        assert {receipt["status"] for receipt in receipts} == {"canceled"}
        invocations = await store.list_subagent_invocations_for_runs([run_id])
        assert {invocation["status"] for invocation in invocations} == {"canceled"}
        assert [event["event_type"] for event in await store.events_since(run_id)][-3:] == [
            "subagent.canceled",
            "subagent.canceled",
            "run.canceled",
        ]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_leased_run_cancellation_settles_unstarted_subagent(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    run, _created = await store.accept_run_command(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        command_id="cmd-leased-cancel",
        client_message_id="msg-leased-cancel-user",
        assistant_message_id="msg-leased-cancel-assistant",
        thread_id="thread-leased-cancel",
        user_input="delegate then cancel",
        command_payload={"type": "run.start"},
        goal="delegate then cancel",
        workspace_path=None,
        mode="auto",
    )
    run_id = str(run["id"])
    job = await store.claim_run_job(worker_id="worker-leased-cancel")
    assert job is not None
    lease_generation = int(job["lease_generation"])
    attempt_id = f"{job['id']}:{lease_generation}"
    try:
        with store.bind_execution_lease(
            job_id=str(job["id"]),
            run_id=run_id,
            lease_owner="worker-leased-cancel",
            lease_generation=lease_generation,
        ):
            await store.prepare_tool_receipt(
                operation_id="toolop-leased-cancel",
                run_id=run_id,
                execution_attempt_id=attempt_id,
                execution_namespace="main",
                tool_call_id="call-leased-cancel",
                tool_name="task",
                tool_version="graph-v1",
                arguments_hash="leased-cancel-args",
                arguments_json=json.dumps(
                    {"subagent_type": "writer", "description": "Never started"}
                ),
                risk="control_flow",
            )
            await store.commit_run_result(
                run_id,
                status="canceled",
                event_type="run.canceled",
                payload={},
            )

        [invocation] = await store.list_subagent_invocations_for_runs([run_id])
        assert invocation["status"] == "canceled"
        assert [event["event_type"] for event in await store.events_since(run_id)][-2:] == [
            "subagent.canceled",
            "run.canceled",
        ]
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_status", "run_event"),
    [("completed", "run.completed"), ("failed", "run.failed")],
)
async def test_terminal_parent_run_cancels_unstarted_subagent(
    tmp_path: Path,
    run_status: str,
    run_event: str,
) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    run, _created = await store.accept_run_command(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        command_id=f"cmd-parent-{run_status}",
        client_message_id=f"msg-parent-{run_status}-user",
        assistant_message_id=f"msg-parent-{run_status}-assistant",
        thread_id=f"thread-parent-{run_status}",
        user_input="delegate work",
        command_payload={"type": "run.start"},
        goal="delegate work",
        workspace_path=None,
        mode="auto",
    )
    run_id = str(run["id"])
    job = await store.claim_run_job(worker_id=f"worker-parent-{run_status}")
    assert job is not None
    lease_generation = int(job["lease_generation"])
    try:
        with store.bind_execution_lease(
            job_id=str(job["id"]),
            run_id=run_id,
            lease_owner=f"worker-parent-{run_status}",
            lease_generation=lease_generation,
        ):
            await store.prepare_tool_receipt(
                operation_id=f"toolop-parent-{run_status}",
                run_id=run_id,
                execution_attempt_id=f"{job['id']}:{lease_generation}",
                execution_namespace="main",
                tool_call_id=f"call-parent-{run_status}",
                tool_name="task",
                tool_version="graph-v1",
                arguments_hash=f"parent-{run_status}-args",
                arguments_json=json.dumps(
                    {"subagent_type": "researcher", "description": "Never started"}
                ),
                risk="control_flow",
            )
            await store.commit_run_result(
                run_id,
                status=run_status,
                event_type=run_event,
                payload={"final_text": "done"} if run_status == "completed" else {"error": "x"},
            )

        [invocation] = await store.list_subagent_invocations_for_runs([run_id])
        assert invocation["status"] == "canceled"
        assert [event["event_type"] for event in await store.events_since(run_id)][-2:] == [
            "subagent.canceled",
            run_event,
        ]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_nested_task_receipt_records_its_parent_operation(tmp_path: Path) -> None:
    store, run = await _store_and_run(tmp_path)
    run_id = str(run["id"])
    await store.prepare_tool_receipt(
        operation_id="toolop_outer",
        run_id=run_id,
        execution_attempt_id="job-nested:1",
        execution_namespace="outer",
        tool_call_id="call-outer",
        tool_name="task",
        tool_version="graph-v1",
        arguments_hash="outer-args",
        arguments_json=json.dumps(
            {"subagent_type": "researcher", "description": "Coordinate nested work"}
        ),
        risk="control_flow",
    )
    await store.begin_tool_receipt(
        operation_id="toolop_outer",
        run_id=run_id,
        execution_attempt_id="job-nested:1",
    )
    context = RuntimeContext(
        store=store,
        run_id=run_id,
        execution_attempt_id="job-nested:1",
    )
    request = ToolCallRequest(
        tool_call={
            "id": "call-inner",
            "name": "task",
            "args": {"subagent_type": "writer", "description": "Write nested result"},
            "type": "tool_call",
        },
        tool=None,
        state={"messages": []},
        runtime=SimpleNamespace(context=context),
    )

    async def complete(inner_request: ToolCallRequest) -> ToolMessage:
        assert current_runtime_tool_execution().operation_id != "toolop_outer"
        return ToolMessage(
            content="done",
            name="task",
            tool_call_id=str(inner_request.tool_call["id"]),
        )

    try:
        with bind_runtime_tool_execution(
            RuntimeToolExecution(
                context=context,
                operation_id="toolop_outer",
                tool_call_id="call-outer",
            )
        ):
            await ToolExecutionMiddleware().awrap_tool_call(request, complete)

        receipts = await store.list_tool_receipts_for_run(run_id)
        inner = next(receipt for receipt in receipts if receipt["tool_call_id"] == "call-inner")
        assert inner["parent_operation_id"] == "toolop_outer"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_quarantined_attempt_projects_running_subagent_as_outcome_unknown(
    tmp_path: Path,
) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    run, _created = await store.accept_run_command(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        command_id="cmd-quarantine",
        client_message_id="msg-quarantine-user",
        assistant_message_id="msg-quarantine-assistant",
        thread_id="thread-quarantine",
        user_input="delegate work",
        command_payload={"type": "run.start"},
        goal="delegate work",
        workspace_path=None,
        mode="auto",
    )
    run_id = str(run["id"])
    job = await store.claim_run_job(worker_id="worker-quarantine")
    assert job is not None
    lease_generation = int(job["lease_generation"])
    attempt_id = f"{job['id']}:{lease_generation}"
    try:
        with store.bind_execution_lease(
            job_id=str(job["id"]),
            run_id=run_id,
            lease_owner="worker-quarantine",
            lease_generation=lease_generation,
        ):
            await store.prepare_tool_receipt(
                operation_id="toolop-quarantine",
                run_id=run_id,
                execution_attempt_id=attempt_id,
                execution_namespace="main",
                tool_call_id="call-quarantine",
                tool_name="task",
                tool_version="graph-v1",
                arguments_hash="quarantine-args",
                arguments_json=json.dumps(
                    {"subagent_type": "researcher", "description": "Long research"}
                ),
                risk="control_flow",
            )
            await store.begin_tool_receipt(
                operation_id="toolop-quarantine",
                run_id=run_id,
                execution_attempt_id=attempt_id,
            )
            await store.prepare_tool_receipt(
                operation_id="toolop-quarantine-queued",
                run_id=run_id,
                execution_attempt_id=attempt_id,
                execution_namespace="queued",
                tool_call_id="call-quarantine-queued",
                tool_name="task",
                tool_version="graph-v1",
                arguments_hash="quarantine-queued-args",
                arguments_json=json.dumps(
                    {"subagent_type": "writer", "description": "Queued sibling"}
                ),
                risk="control_flow",
            )
            await store.prepare_tool_receipt(
                operation_id="toolop-quarantine-paused",
                run_id=run_id,
                execution_attempt_id=attempt_id,
                execution_namespace="paused",
                tool_call_id="call-quarantine-paused",
                tool_name="task",
                tool_version="graph-v1",
                arguments_hash="quarantine-paused-args",
                arguments_json=json.dumps(
                    {"subagent_type": "writer", "description": "Paused sibling"}
                ),
                risk="control_flow",
            )
            await store.begin_tool_receipt(
                operation_id="toolop-quarantine-paused",
                run_id=run_id,
                execution_attempt_id=attempt_id,
            )
            await store.settle_tool_receipt(
                operation_id="toolop-quarantine-paused",
                run_id=run_id,
                status="paused",
            )
            await store.reserve_model_call(
                run_id=run_id,
                execution_attempt_id=attempt_id,
                model="fake:model",
                max_calls=10,
                parent_tool_operation_id="toolop-quarantine",
            )
            await store.quarantine_execution_attempt(
                run_id,
                reason="execution_cleanup_unconfirmed",
                payload={"error": "cleanup could not be confirmed"},
            )

        receipts = {
            receipt["operation_id"]: receipt
            for receipt in await store.list_tool_receipts_for_run(run_id)
        }
        assert receipts["toolop-quarantine"]["status"] == "outcome_unknown"
        assert receipts["toolop-quarantine-queued"]["status"] == "canceled"
        assert receipts["toolop-quarantine-paused"]["status"] == "canceled"
        events = await store.events_since(run_id)
        lifecycle_by_operation: dict[str, list[dict[str, object]]] = {}
        for event in events:
            if not str(event["event_type"]).startswith("subagent."):
                continue
            payload = json.loads(str(event["payload_json"]))
            lifecycle_by_operation.setdefault(str(payload["operation_id"]), []).append(
                {"event_type": event["event_type"], "payload": payload}
            )
        assert [event["event_type"] for event in lifecycle_by_operation["toolop-quarantine"]] == [
            "subagent.spawned",
            "subagent.started",
            "subagent.outcome_unknown",
        ]
        assert [
            event["event_type"] for event in lifecycle_by_operation["toolop-quarantine-queued"]
        ] == ["subagent.spawned", "subagent.canceled"]
        assert [
            event["event_type"] for event in lifecycle_by_operation["toolop-quarantine-paused"]
        ] == [
            "subagent.spawned",
            "subagent.started",
            "subagent.waiting",
            "subagent.canceled",
        ]
        unknown = lifecycle_by_operation["toolop-quarantine"][-1]["payload"]
        assert unknown["status"] == "unknown"
        assert unknown["receipt_status"] == "outcome_unknown"
        assert unknown["usage"]["outcome_unknown_calls"] == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_thread_snapshot_atomically_includes_subagents_and_current_event_cursor(
    tmp_path: Path,
) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    run, _created = await store.accept_run_command(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        command_id="cmd-snapshot",
        client_message_id="msg-snapshot-user",
        assistant_message_id="msg-snapshot-assistant",
        thread_id="thread-snapshot",
        user_input="delegate snapshot work",
        command_payload={"type": "run.start"},
        goal="delegate snapshot work",
        workspace_path=None,
        mode="auto",
    )
    run_id = str(run["id"])
    try:
        before = await store.get_thread_snapshot(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            thread_id="thread-snapshot",
        )
        assert before is not None
        before_version = int(before["thread"]["version"])
        before_cursor = int(before["cursor"])
        before_assistant = next(
            item for item in before["items"] if item["item_type"] == "assistant_message"
        )
        await store.prepare_tool_receipt(
            operation_id="toolop-snapshot",
            run_id=run_id,
            execution_attempt_id="job-snapshot:1",
            execution_namespace="main",
            tool_call_id="call-snapshot",
            tool_name="task",
            tool_version="graph-v1",
            arguments_hash="snapshot-args",
            arguments_json=json.dumps(
                {"subagent_type": "writer", "description": "Write snapshot result"}
            ),
            risk="control_flow",
        )
        with pytest.raises(RunResultConflictError, match="thread changed"):
            await store.get_thread_snapshot(
                principal_id=LOCAL_OWNER_PRINCIPAL_ID,
                thread_id="thread-snapshot",
                expected_version=before_version,
            )
        changes, change_cursor = await store.thread_changes_since(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            after_cursor=before_cursor,
        )
        assert [change["change_type"] for change in changes] == ["subagent.spawned"]
        latest = await store.append_event(run_id, "test.snapshot_tail", {"tail": True})

        snapshot = await store.get_thread_snapshot(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            thread_id="thread-snapshot",
            event_limit=1,
        )
        assert snapshot is not None
        assert int(snapshot["thread"]["version"]) == before_version + 1
        assert int(snapshot["cursor"]) == change_cursor
        projected_run = next(item for item in snapshot["runs"] if item["id"] == run_id)
        assert [
            invocation["operation_id"] for invocation in projected_run["subagent_invocations"]
        ] == ["toolop-snapshot"]
        spawned = next(
            event for event in snapshot["events"] if event["event_type"] == "subagent.spawned"
        )
        assistant = next(
            item for item in snapshot["items"] if item["item_type"] == "assistant_message"
        )
        assert assistant["status"] == before_assistant["status"]
        assert assistant["content"] == before_assistant["content"]
        assert int(assistant["version"]) == int(before_assistant["version"]) + 1
        assert int(assistant["event_high_watermark"]) == int(spawned["seq"])
        assert snapshot["events_truncated"] is True
        assert snapshot["event_high_watermarks"] == {run_id: int(spawned["seq"])}
        assert [
            event["id"]
            for event in await store.events_since(
                run_id,
                after_seq=snapshot["event_high_watermarks"][run_id],
            )
        ] == [latest["id"]]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_truncated_snapshot_watermark_replays_omitted_permission_event(
    tmp_path: Path,
) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    older, _created = await store.accept_run_command(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        command_id="cmd-watermark-older",
        client_message_id="msg-watermark-older-user",
        assistant_message_id="msg-watermark-older-assistant",
        thread_id="thread-watermark",
        user_input="older turn",
        command_payload={"type": "run.start"},
        goal="older turn",
        workspace_path=None,
        mode="auto",
    )
    await store.append_event(str(older["id"]), "tool.completed", {"tool": "ls"})
    older_job = await store.claim_run_job(worker_id="worker-watermark-older")
    assert older_job is not None
    with store.bind_execution_lease(
        job_id=str(older_job["id"]),
        run_id=str(older["id"]),
        lease_owner="worker-watermark-older",
        lease_generation=int(older_job["lease_generation"]),
    ):
        await store.commit_run_result(
            str(older["id"]),
            status="completed",
            event_type="run.completed",
            payload={"final_text": "older done"},
        )
    newer, _created = await store.accept_run_command(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        command_id="cmd-watermark-newer",
        client_message_id="msg-watermark-newer-user",
        assistant_message_id="msg-watermark-newer-assistant",
        thread_id="thread-watermark",
        user_input="newer turn",
        command_payload={"type": "run.start"},
        goal="newer turn",
        workspace_path=None,
        mode="ask",
    )
    newer_id = str(newer["id"])
    permission = await store.append_event(
        newer_id,
        "permission.required",
        {"request_id": "permission-watermark", "tool": "execute"},
    )
    await store.update_run_status(newer_id, "waiting_permission")
    await store._conn.execute(
        "UPDATE local_runs SET created_at = ? WHERE id = ?",
        ("2026-08-02T00:00:00Z", older["id"]),
    )
    await store._conn.execute(
        "UPDATE local_runs SET created_at = ? WHERE id = ?",
        ("2026-08-02T00:00:01Z", newer_id),
    )
    await store._conn.commit()

    try:
        snapshot = await store.get_thread_snapshot(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            thread_id="thread-watermark",
            event_limit=1,
        )
        assert snapshot is not None
        assert snapshot["events_truncated"] is True
        assert {event["run_id"] for event in snapshot["events"]} == {older["id"]}
        assert snapshot["event_high_watermarks"][newer_id] == 0
        replay = await store.events_since(
            newer_id,
            after_seq=snapshot["event_high_watermarks"][newer_id],
        )
        assert [event["id"] for event in replay] == [permission["id"]]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_subagent_failure_pause_resume_replay_and_reconciliation_are_projected(
    tmp_path: Path,
) -> None:
    store, run = await _store_and_run(tmp_path)
    run_id = str(run["id"])

    async def prepare(operation_id: str, call_id: str) -> dict[str, object]:
        return await store.prepare_tool_receipt(
            operation_id=operation_id,
            run_id=run_id,
            execution_attempt_id="job-states:1",
            execution_namespace=operation_id,
            tool_call_id=call_id,
            tool_name="task",
            tool_version="graph-v1",
            arguments_hash=f"{operation_id}-args",
            arguments_json=json.dumps({"subagent_type": "writer", "description": operation_id}),
            risk="control_flow",
        )

    try:
        await prepare("toolop-failed", "call-failed")
        await store.begin_tool_receipt(
            operation_id="toolop-failed",
            run_id=run_id,
            execution_attempt_id="job-states:1",
        )
        await store.settle_tool_receipt(
            operation_id="toolop-failed",
            run_id=run_id,
            status="failed",
            error_type="RuntimeError",
        )

        await prepare("toolop-resume", "call-resume")
        await store.begin_tool_receipt(
            operation_id="toolop-resume",
            run_id=run_id,
            execution_attempt_id="job-states:1",
        )
        await store.settle_tool_receipt(
            operation_id="toolop-resume",
            run_id=run_id,
            status="paused",
        )
        await store.begin_tool_receipt(
            operation_id="toolop-resume",
            run_id=run_id,
            execution_attempt_id="job-states:2",
        )
        await store.settle_tool_receipt(
            operation_id="toolop-resume",
            run_id=run_id,
            status="completed",
        )
        event_count_before_replay = len(await store.events_since(run_id))
        replayed = await prepare("toolop-resume", "call-resume")
        assert replayed["status"] == "completed"
        replayed = await store.begin_tool_receipt(
            operation_id="toolop-resume",
            run_id=run_id,
            execution_attempt_id="job-states:3",
        )
        assert replayed["status"] == "completed"
        assert len(await store.events_since(run_id)) == event_count_before_replay

        await prepare("toolop-unknown", "call-unknown")
        await store.begin_tool_receipt(
            operation_id="toolop-unknown",
            run_id=run_id,
            execution_attempt_id="job-states:1",
        )
        await store.settle_tool_receipt(
            operation_id="toolop-unknown",
            run_id=run_id,
            status="outcome_unknown",
            error_type="execution_lease_expired",
        )
        await store.reconcile_tool_receipt(
            operation_id="toolop-unknown",
            run_id=run_id,
            decision="confirmed_completed",
            result_json='{"kind":"tool_message"}',
            result_hash="confirmed-hash",
        )

        await prepare("toolop-retry", "call-retry")
        await store.begin_tool_receipt(
            operation_id="toolop-retry",
            run_id=run_id,
            execution_attempt_id="job-states:1",
        )
        await store.settle_tool_receipt(
            operation_id="toolop-retry",
            run_id=run_id,
            status="outcome_unknown",
            result_json='{"stale":true}',
            result_hash="stale-hash",
            error_type="execution_lease_expired",
        )
        retried = await store.reconcile_tool_receipt(
            operation_id="toolop-retry",
            run_id=run_id,
            decision="retry_not_executed",
            result_json='{"must":"be cleared"}',
            result_hash="must-be-cleared",
        )
        assert retried["status"] == "prepared"
        assert retried["result_json"] is None
        assert retried["result_hash"] is None
        assert retried["error_type"] is None
        assert retried["completed_at"] is None
        retry_projection = next(
            invocation
            for invocation in await store.list_subagent_invocations_for_runs([run_id])
            if invocation["operation_id"] == "toolop-retry"
        )
        assert retry_projection["status"] == "queued"
        assert retry_projection["error_type"] is None
        assert retry_projection["completed_at"] is None

        events = await store.events_since(run_id)
        by_operation: dict[str, list[str]] = {}
        for event in events:
            payload = json.loads(str(event["payload_json"]))
            by_operation.setdefault(str(payload["operation_id"]), []).append(
                str(event["event_type"])
            )
        assert by_operation == {
            "toolop-failed": [
                "subagent.spawned",
                "subagent.started",
                "subagent.failed",
            ],
            "toolop-resume": [
                "subagent.spawned",
                "subagent.started",
                "subagent.waiting",
                "subagent.started",
                "subagent.completed",
            ],
            "toolop-unknown": [
                "subagent.spawned",
                "subagent.started",
                "subagent.outcome_unknown",
                "subagent.completed",
            ],
            "toolop-retry": [
                "subagent.spawned",
                "subagent.started",
                "subagent.outcome_unknown",
                "subagent.spawned",
            ],
        }
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_parallel_subagent_receipts_keep_independent_lifecycle_sequences(
    tmp_path: Path,
) -> None:
    store, run = await _store_and_run(tmp_path)
    run_id = str(run["id"])

    async def execute(index: int) -> None:
        operation_id = f"toolop-parallel-{index}"
        await store.prepare_tool_receipt(
            operation_id=operation_id,
            run_id=run_id,
            execution_attempt_id="job-parallel:1",
            execution_namespace="parallel",
            tool_call_id=f"call-parallel-{index}",
            tool_name="task",
            tool_version="graph-v1",
            arguments_hash=f"parallel-{index}-args",
            arguments_json=json.dumps(
                {"subagent_type": "researcher", "description": f"branch {index}"}
            ),
            risk="control_flow",
        )
        await store.begin_tool_receipt(
            operation_id=operation_id,
            run_id=run_id,
            execution_attempt_id="job-parallel:1",
        )
        await store.settle_tool_receipt(
            operation_id=operation_id,
            run_id=run_id,
            status="completed",
        )

    try:
        await asyncio.gather(execute(1), execute(2))
        events = await store.events_since(run_id)
        statuses: dict[str, list[str]] = {}
        for event in events:
            payload = json.loads(str(event["payload_json"]))
            statuses.setdefault(str(payload["operation_id"]), []).append(str(payload["status"]))
        assert statuses == {
            "toolop-parallel-1": ["queued", "running", "completed"],
            "toolop-parallel-2": ["queued", "running", "completed"],
        }
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_open_removes_only_legacy_transient_subagent_spawn_events(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    store, run = await _store_and_run(tmp_path)
    run_id = str(run["id"])
    await store.append_event(run_id, "subagent.spawned", {"id": "legacy-call"})
    await store.prepare_tool_receipt(
        operation_id="toolop-durable",
        run_id=run_id,
        execution_attempt_id="job-open:1",
        execution_namespace="main",
        tool_call_id="call-durable",
        tool_name="task",
        tool_version="graph-v1",
        arguments_hash="durable-args",
        arguments_json=json.dumps({"subagent_type": "writer", "description": "Durable child"}),
        risk="control_flow",
    )
    await store.close()

    reopened = await LocalStore.open(db_path)
    try:
        spawned = [
            event
            for event in await reopened.events_since(run_id)
            if event["event_type"] == "subagent.spawned"
        ]
        assert len(spawned) == 1
        assert json.loads(str(spawned[0]["payload_json"]))["operation_id"] == "toolop-durable"
    finally:
        await reopened.close()
