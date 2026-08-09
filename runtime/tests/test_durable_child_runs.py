from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage

from shejane_runtime.agent.context_builder import RuntimeContext
from shejane_runtime.auth import LOCAL_OWNER_PRINCIPAL_ID
from shejane_runtime.middleware.tool_execution import (
    ToolExecutionMiddleware,
    tool_operation_identity,
)
from shejane_runtime.runs import RunCoordinator
from shejane_runtime.store.sqlite import (
    CommandConflictError,
    LeaseFenceError,
    LocalStore,
)

CHILD_DEFINITION = {
    "id": "builtin:researcher",
    "version": "sha256:test-researcher-v1",
    "name": "researcher",
    "description": "Research with read-only tools.",
    "system_prompt": "Research the assigned task and cite evidence.",
    "allowed_tools": ["read_file", "web.fetch", "web.search"],
}


async def _claimed_parent(store: LocalStore) -> tuple[dict, dict]:
    run, created = await store.accept_run_command(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        command_id="cmd-parent",
        client_message_id="msg-parent",
        command_payload={"type": "run.start", "goal": "coordinate"},
        goal="coordinate",
        workspace_path=None,
        mode="fast",
        settings={"memory": "on", "_snapshot_version": 1},
        metadata={"source": "test"},
    )
    assert created is True
    job = await store.claim_run_job(worker_id="worker-parent")
    assert job is not None
    assert job["run_id"] == run["id"]
    return run, job


async def _prepare_spawn_receipt(
    store: LocalStore,
    run_id: str,
    execution_attempt_id: str,
) -> None:
    await store.prepare_tool_receipt(
        operation_id="toolop-child-spawn",
        run_id=run_id,
        execution_attempt_id=execution_attempt_id,
        execution_namespace="main",
        tool_call_id="call-child-spawn",
        tool_name="child.spawn",
        tool_version="graph-v1",
        arguments_hash="spawn-args",
        arguments_json=json.dumps(
            {"agent": "builtin:researcher", "task": "Find primary sources"},
            sort_keys=True,
        ),
        risk="control_flow",
    )
    await store.begin_tool_receipt(
        operation_id="toolop-child-spawn",
        run_id=run_id,
        execution_attempt_id=execution_attempt_id,
    )


@pytest.mark.asyncio
async def test_child_admission_reuses_run_job_lease_and_projects_to_parent(
    tmp_path: Path,
) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    try:
        parent, parent_job = await _claimed_parent(store)
        with store.bind_execution_lease(
            job_id=str(parent_job["id"]),
            run_id=str(parent["id"]),
            lease_owner="worker-parent",
            lease_generation=int(parent_job["lease_generation"]),
        ):
            await _prepare_spawn_receipt(
                store,
                str(parent["id"]),
                f"{parent_job['id']}:{parent_job['lease_generation']}",
            )
            child, created = await store.accept_child_run(
                parent_run_id=str(parent["id"]),
                spawn_operation_id="toolop-child-spawn",
                goal="Find primary sources",
                agent_definition=CHILD_DEFINITION,
                execution_policy={
                    "complexity": "complex",
                    "subagent_allowed": True,
                    "reason": "complex_task",
                    "max_model_calls": 100,
                    "soft_model_call_limit": 24,
                    "final_model_call_reserve": 2,
                },
            )

        assert created is True
        assert child["run_kind"] == "child"
        assert child["parent_run_id"] == parent["id"]
        assert child["root_run_id"] == parent["root_run_id"]
        assert child["agent_definition_id"] == CHILD_DEFINITION["id"]
        assert child["agent_definition_version"] == CHILD_DEFINITION["version"]
        assert child["collaboration_depth"] == 1
        assert child["thread_id"] is None
        assert child["assistant_item_id"] is None
        parent_settings = json.loads(str(parent["settings_json"]))
        child_settings = json.loads(str(child["settings_json"]))
        assert {
            key: value for key, value in child_settings.items() if key != "_execution_policy"
        } == parent_settings
        assert child_settings["_execution_policy"]["complexity"] == "complex"
        assert child["workspace_path"] == parent["workspace_path"]

        child_job = await store.get_active_run_job(str(child["id"]))
        assert child_job is not None
        assert child_job["status"] == "pending"
        assert child_job["kind"] == "start"
        child_input = json.loads(str(child_job["input_json"]))
        assert child_input["run_kind"] == "child"
        assert child_input["agent_definition_id"] == CHILD_DEFINITION["id"]
        assert child_input["collaboration_depth"] == 1

        parent_events = await store.events_since(str(parent["id"]))
        assert [event["event_type"] for event in parent_events] == ["child.spawned"]
        spawn_payload = json.loads(str(parent_events[0]["payload_json"]))
        assert spawn_payload["child_run_id"] == child["id"]
        assert spawn_payload["status"] == "queued"

        recent = await store.list_runs(principal_id=LOCAL_OWNER_PRINCIPAL_ID)
        assert [run["id"] for run in recent] == [parent["id"]]
        assert [run["id"] for run in await store.list_child_runs_for_run(parent["id"])] == [
            child["id"]
        ]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_child_spawn_is_idempotent_and_rejects_a_changed_spec(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    try:
        parent, parent_job = await _claimed_parent(store)
        with store.bind_execution_lease(
            job_id=str(parent_job["id"]),
            run_id=str(parent["id"]),
            lease_owner="worker-parent",
            lease_generation=int(parent_job["lease_generation"]),
        ):
            await _prepare_spawn_receipt(
                store,
                str(parent["id"]),
                f"{parent_job['id']}:{parent_job['lease_generation']}",
            )
            first, first_created = await store.accept_child_run(
                parent_run_id=str(parent["id"]),
                spawn_operation_id="toolop-child-spawn",
                goal="Find primary sources",
                agent_definition=CHILD_DEFINITION,
            )
            replay, replay_created = await store.accept_child_run(
                parent_run_id=str(parent["id"]),
                spawn_operation_id="toolop-child-spawn",
                goal="Find primary sources",
                agent_definition=CHILD_DEFINITION,
            )
            with pytest.raises(CommandConflictError, match="different child specification"):
                await store.accept_child_run(
                    parent_run_id=str(parent["id"]),
                    spawn_operation_id="toolop-child-spawn",
                    goal="A changed task",
                    agent_definition=CHILD_DEFINITION,
                )

        assert first_created is True
        assert replay_created is False
        assert replay["id"] == first["id"]
        assert len(await store.list_child_runs_for_run(parent["id"])) == 1
        assert [event["event_type"] for event in await store.events_since(parent["id"])] == [
            "child.spawned"
        ]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_stale_parent_lease_cannot_admit_a_child(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    try:
        parent, parent_job = await _claimed_parent(store)
        stale_generation = int(parent_job["lease_generation"]) + 100
        with store.bind_execution_lease(
            job_id=str(parent_job["id"]),
            run_id=str(parent["id"]),
            lease_owner="worker-parent",
            lease_generation=stale_generation,
        ):
            with pytest.raises(LeaseFenceError, match="stale"):
                await store.accept_child_run(
                    parent_run_id=str(parent["id"]),
                    spawn_operation_id="toolop-child-spawn",
                    goal="Find primary sources",
                    agent_definition=CHILD_DEFINITION,
                )

        assert await store.list_child_runs_for_run(parent["id"]) == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_child_terminal_and_cancel_events_are_atomically_projected_to_parent(
    tmp_path: Path,
) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    try:
        parent, parent_job = await _claimed_parent(store)
        with store.bind_execution_lease(
            job_id=str(parent_job["id"]),
            run_id=str(parent["id"]),
            lease_owner="worker-parent",
            lease_generation=int(parent_job["lease_generation"]),
        ):
            await _prepare_spawn_receipt(
                store,
                str(parent["id"]),
                f"{parent_job['id']}:{parent_job['lease_generation']}",
            )
            child, _created = await store.accept_child_run(
                parent_run_id=str(parent["id"]),
                spawn_operation_id="toolop-child-spawn",
                goal="Find primary sources",
                agent_definition=CHILD_DEFINITION,
            )

        child_job = await store.claim_run_job(worker_id="worker-child")
        assert child_job is not None
        assert child_job["run_id"] == child["id"]
        with store.bind_execution_lease(
            job_id=str(child_job["id"]),
            run_id=str(child["id"]),
            lease_owner="worker-child",
            lease_generation=int(child_job["lease_generation"]),
        ):
            await store.append_event(child["id"], "run.started", {"goal": child["goal"]})
            await store.commit_run_result(
                child["id"],
                status="completed",
                event_type="run.completed",
                payload={"final_text": "The child result", "input_tokens": 12},
            )

        parent_events = await store.events_since(parent["id"])
        assert [event["event_type"] for event in parent_events] == [
            "child.spawned",
            "child.started",
            "child.completed",
        ]
        completed = json.loads(str(parent_events[-1]["payload_json"]))
        assert completed["child_run_id"] == child["id"]
        assert completed["status"] == "completed"
        assert completed["result_preview"] == "The child result"

        # A second child canceled while pending follows the same durable path.
        with store.bind_execution_lease(
            job_id=str(parent_job["id"]),
            run_id=str(parent["id"]),
            lease_owner="worker-parent",
            lease_generation=int(parent_job["lease_generation"]),
        ):
            await store.prepare_tool_receipt(
                operation_id="toolop-child-cancel",
                run_id=str(parent["id"]),
                execution_attempt_id=f"{parent_job['id']}:{parent_job['lease_generation']}",
                execution_namespace="main",
                tool_call_id="call-child-cancel",
                tool_name="child.spawn",
                tool_version="graph-v1",
                arguments_hash="spawn-cancel-args",
                arguments_json="{}",
                risk="control_flow",
            )
            await store.begin_tool_receipt(
                operation_id="toolop-child-cancel",
                run_id=str(parent["id"]),
                execution_attempt_id=f"{parent_job['id']}:{parent_job['lease_generation']}",
            )
            canceled_child, _created = await store.accept_child_run(
                parent_run_id=str(parent["id"]),
                spawn_operation_id="toolop-child-cancel",
                goal="Cancel me",
                agent_definition=CHILD_DEFINITION,
            )
        assert await store.request_run_cancel(str(canceled_child["id"])) == "pending"
        assert (await store.get_run(str(canceled_child["id"])))["status"] == "canceled"
        parent_events = await store.events_since(parent["id"])
        assert parent_events[-1]["event_type"] == "child.canceled"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_child_survives_store_restart_and_remains_claimable(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    store = await LocalStore.open(path)
    parent, parent_job = await _claimed_parent(store)
    with store.bind_execution_lease(
        job_id=str(parent_job["id"]),
        run_id=str(parent["id"]),
        lease_owner="worker-parent",
        lease_generation=int(parent_job["lease_generation"]),
    ):
        await _prepare_spawn_receipt(
            store,
            str(parent["id"]),
            f"{parent_job['id']}:{parent_job['lease_generation']}",
        )
        child, _created = await store.accept_child_run(
            parent_run_id=str(parent["id"]),
            spawn_operation_id="toolop-child-spawn",
            goal="Find primary sources",
            agent_definition=CHILD_DEFINITION,
        )
    await store.close()

    reopened = await LocalStore.open(path)
    try:
        children = await reopened.list_child_runs_for_run(str(parent["id"]))
        assert [item["id"] for item in children] == [child["id"]]
        claimed = await reopened.claim_run_job(worker_id="worker-after-restart")
        assert claimed is not None
        assert claimed["run_id"] == child["id"]
        assert claimed["kind"] == "start"
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_late_child_attempt_is_fenced_before_any_replacement_can_start(
    tmp_path: Path,
) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    try:
        parent, parent_job = await _claimed_parent(store)
        with store.bind_execution_lease(
            job_id=str(parent_job["id"]),
            run_id=str(parent["id"]),
            lease_owner="worker-parent",
            lease_generation=int(parent_job["lease_generation"]),
        ):
            await _prepare_spawn_receipt(
                store,
                str(parent["id"]),
                f"{parent_job['id']}:{parent_job['lease_generation']}",
            )
            child, _created = await store.accept_child_run(
                parent_run_id=str(parent["id"]),
                spawn_operation_id="toolop-child-spawn",
                goal="Find primary sources",
                agent_definition=CHILD_DEFINITION,
            )

        first = await store.claim_run_job(worker_id="worker-child-1", lease_seconds=-1)
        assert first is not None and first["run_id"] == child["id"]
        assert await store.claim_run_job(worker_id="worker-child-2") is None
        assert (await store.get_run(str(child["id"])))["status"] == "cleanup_required"

        with store.bind_execution_lease(
            job_id=str(first["id"]),
            run_id=str(child["id"]),
            lease_owner="worker-child-1",
            lease_generation=int(first["lease_generation"]),
        ):
            with pytest.raises(LeaseFenceError):
                await store.commit_run_result(
                    str(child["id"]),
                    status="failed",
                    event_type="run.failed",
                    payload={"error": "late result"},
                )

        events = await store.events_since(str(child["id"]))
        assert [event["event_type"] for event in events] == ["run.cleanup_required"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_spawn_receipt_recovers_automatically_when_child_was_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    try:
        parent, parent_job = await _claimed_parent(store)
        arguments = {"agent": "builtin:researcher", "task": "Find primary sources"}
        operation_id, arguments_hash, arguments_json = tool_operation_identity(
            run_id=str(parent["id"]),
            tool_call_id="call-recover-spawn",
            tool_name="child.spawn",
            arguments=arguments,
            tool_version="graph-v1",
        )
        attempt_id = f"{parent_job['id']}:{parent_job['lease_generation']}"
        with store.bind_execution_lease(
            job_id=str(parent_job["id"]),
            run_id=str(parent["id"]),
            lease_owner="worker-parent",
            lease_generation=int(parent_job["lease_generation"]),
        ):
            await store.prepare_tool_receipt(
                operation_id=operation_id,
                run_id=str(parent["id"]),
                execution_attempt_id=attempt_id,
                execution_namespace="main",
                tool_call_id="call-recover-spawn",
                tool_name="child.spawn",
                tool_version="graph-v1",
                arguments_hash=arguments_hash,
                arguments_json=arguments_json,
                risk="control_flow",
            )
            await store.begin_tool_receipt(
                operation_id=operation_id,
                run_id=str(parent["id"]),
                execution_attempt_id=attempt_id,
            )
            child, _created = await store.accept_child_run(
                parent_run_id=str(parent["id"]),
                spawn_operation_id=operation_id,
                goal="Find primary sources",
                agent_definition=CHILD_DEFINITION,
            )
            await store.settle_tool_receipt(
                operation_id=operation_id,
                run_id=str(parent["id"]),
                status="outcome_unknown",
                error_type="simulated_process_crash",
            )

        async def must_not_execute(_request: ToolCallRequest) -> ToolMessage:
            raise AssertionError("a committed child.spawn must not execute twice")

        monkeypatch.setattr(
            "shejane_runtime.middleware.tool_execution.interrupt",
            lambda _payload: (_ for _ in ()).throw(
                AssertionError("an internal durable spawn must not ask for reconciliation")
            ),
        )
        request = ToolCallRequest(
            tool_call={
                "id": "call-recover-spawn",
                "name": "child.spawn",
                "args": arguments,
                "type": "tool_call",
            },
            tool=None,
            state={"messages": []},
            runtime=SimpleNamespace(
                context=RuntimeContext(
                    store=store,
                    run_id=str(parent["id"]),
                    execution_attempt_id="replacement-job:2",
                    graph_definition_id="graph-v1",
                )
            ),
        )
        result = await ToolExecutionMiddleware().awrap_tool_call(request, must_not_execute)
        assert isinstance(result, ToolMessage)
        assert json.loads(str(result.content))["id"] == child["id"]
        receipt = await store.get_tool_receipt(operation_id)
        assert receipt is not None and receipt["status"] == "completed"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_child_resume_job_preserves_frozen_agent_identity(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    try:
        parent, parent_job = await _claimed_parent(store)
        with store.bind_execution_lease(
            job_id=str(parent_job["id"]),
            run_id=str(parent["id"]),
            lease_owner="worker-parent",
            lease_generation=int(parent_job["lease_generation"]),
        ):
            await _prepare_spawn_receipt(
                store,
                str(parent["id"]),
                f"{parent_job['id']}:{parent_job['lease_generation']}",
            )
            child, _created = await store.accept_child_run(
                parent_run_id=str(parent["id"]),
                spawn_operation_id="toolop-child-spawn",
                goal="Find primary sources",
                agent_definition=CHILD_DEFINITION,
            )

        child_job = await store.claim_run_job(worker_id="worker-child")
        assert child_job is not None and child_job["run_id"] == child["id"]
        assert await store.finish_run_job(
            str(child_job["id"]),
            lease_owner="worker-child",
            lease_generation=int(child_job["lease_generation"]),
            status="completed",
        )
        question = await store.create_question(
            run_id=str(child["id"]),
            tool_call_id="child-question",
            questions=[{"id": "source", "question": "Which source?"}],
        )
        await store.update_run_status(str(child["id"]), "waiting_input")

        receipt, created = await store.request_question_answer_command(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="answer-child-question",
            question_id=str(question["id"]),
            answers={"source": ["official"]},
        )
        assert created is True and receipt["resumed"] is True
        resume_job = await store.get_active_run_job(str(child["id"]))
        assert resume_job is not None and resume_job["kind"] == "resume"
        resume_input = json.loads(str(resume_job["input_json"]))
        assert resume_input["run_kind"] == "child"
        assert resume_input["root_run_id"] == child["root_run_id"]
        assert resume_input["agent_definition_id"] == CHILD_DEFINITION["id"]
        assert resume_input["agent_definition_version"] == CHILD_DEFINITION["version"]
        assert resume_input["collaboration_depth"] == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_child_wait_temporarily_yields_the_only_execution_slot(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    coordinator = RunCoordinator(
        store=store,
        checkpointer=None,  # type: ignore[arg-type]
        max_concurrent_runs=1,
    )
    current = asyncio.current_task()
    assert current is not None
    coordinator._tasks["parent"] = current
    await coordinator._slots.acquire()
    try:
        async with coordinator._yield_execution_slot("parent"):
            await asyncio.wait_for(
                coordinator._slots.acquire(),
                timeout=0.2,
            )
            coordinator._slots.release()
        assert coordinator._slots.locked()
    finally:
        coordinator._slots.release()
        await store.close()
