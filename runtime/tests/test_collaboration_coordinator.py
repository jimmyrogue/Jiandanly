from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from shejane_runtime.auth import LOCAL_OWNER_PRINCIPAL_ID
from shejane_runtime.runs import (
    RunCoordinator,
    RunOutcome,
    _collaboration_completion_summary,
)
from shejane_runtime.store.sqlite import LocalStore, RunAdmissionError

CHILD_DEFINITION = {
    "id": "builtin:researcher",
    "version": "sha256:test-researcher-v1",
    "name": "researcher",
    "description": "Research with read-only tools.",
    "system_prompt": "Research the assigned task and cite evidence.",
    "allowed_tools": ["read_file", "write_file"],
}


async def _claimed_parent(
    store: LocalStore, workspace: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    await store.create_workspace(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        path=str(workspace),
        label="test",
    )
    run, created = await store.accept_run_command(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        command_id="cmd-parent",
        client_message_id="msg-parent",
        command_payload={"type": "run.start", "goal": "coordinate"},
        goal="coordinate",
        workspace_path=str(workspace),
        mode="fast",
        settings={"memory": "on", "_snapshot_version": 1},
        metadata={"source": "test"},
    )
    assert created is True
    job = await store.claim_run_job(worker_id="worker-parent")
    assert job is not None
    return run, job


async def _spawn(
    store: LocalStore,
    *,
    parent: dict[str, Any],
    parent_job: dict[str, Any],
    suffix: str,
    coordination: dict[str, Any] | None = None,
) -> dict[str, Any]:
    operation_id = f"toolop-child-{suffix}"
    execution_attempt_id = f"{parent_job['id']}:{parent_job['lease_generation']}"
    await store.prepare_tool_receipt(
        operation_id=operation_id,
        run_id=str(parent["id"]),
        execution_attempt_id=execution_attempt_id,
        execution_namespace="main",
        tool_call_id=f"call-child-{suffix}",
        tool_name="child.spawn",
        tool_version="graph-v1",
        arguments_hash=f"spawn-{suffix}",
        arguments_json=json.dumps(
            {
                "agent": "builtin:researcher",
                "task": f"task {suffix}",
                **(coordination or {}),
            },
            sort_keys=True,
        ),
        risk="control_flow",
    )
    await store.begin_tool_receipt(
        operation_id=operation_id,
        run_id=str(parent["id"]),
        execution_attempt_id=execution_attempt_id,
    )
    child, created = await store.accept_child_run(
        parent_run_id=str(parent["id"]),
        spawn_operation_id=operation_id,
        goal=f"task {suffix}",
        agent_definition=CHILD_DEFINITION,
        coordination=coordination,
    )
    assert created is True
    return child


@pytest.mark.asyncio
async def test_dependencies_gate_job_claim_and_cancel_dependents_after_failure(
    tmp_path: Path,
) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    try:
        parent, parent_job = await _claimed_parent(store, tmp_path)
        with store.bind_execution_lease(
            job_id=str(parent_job["id"]),
            run_id=str(parent["id"]),
            lease_owner="worker-parent",
            lease_generation=int(parent_job["lease_generation"]),
        ):
            first = await _spawn(
                store,
                parent=parent,
                parent_job=parent_job,
                suffix="first",
            )
            second = await _spawn(
                store,
                parent=parent,
                parent_job=parent_job,
                suffix="second",
                coordination={"depends_on": [first["id"]]},
            )

        first_job = await store.claim_run_job(worker_id="worker-first")
        assert first_job is not None
        assert first_job["run_id"] == first["id"]
        assert await store.claim_run_job(worker_id="worker-blocked") is None

        with store.bind_execution_lease(
            job_id=str(first_job["id"]),
            run_id=str(first["id"]),
            lease_owner="worker-first",
            lease_generation=int(first_job["lease_generation"]),
        ):
            await store.commit_run_result(
                first["id"],
                status="failed",
                event_type="run.failed",
                payload={"error": "dependency failed", "type": "TestFailure"},
            )

        assert await store.claim_run_job(worker_id="worker-after-failure") is None
        second_record = await store.get_run(second["id"])
        assert second_record is not None
        assert second_record["status"] == "canceled"
        parent_events = await store.events_since(parent["id"])
        assert [event["event_type"] for event in parent_events][-2:] == [
            "child.failed",
            "child.canceled",
        ]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_resource_enforcement_is_inert_without_claims(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    try:
        parent, _ = await _claimed_parent(store, tmp_path)
        await store.assert_workspace_resource_owner(
            run_id=parent["id"],
            requested_path="/ordinary-single-agent-write.txt",
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_resource_owner_is_unique_and_enforced_for_workspace_writes(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    try:
        parent, parent_job = await _claimed_parent(store, tmp_path)
        with store.bind_execution_lease(
            job_id=str(parent_job["id"]),
            run_id=str(parent["id"]),
            lease_owner="worker-parent",
            lease_generation=int(parent_job["lease_generation"]),
        ):
            owner = await _spawn(
                store,
                parent=parent,
                parent_job=parent_job,
                suffix="owner",
                coordination={"resource_claims": ["reports/final.md"]},
            )
            with pytest.raises(RunAdmissionError, match="already owned"):
                await _spawn(
                    store,
                    parent=parent,
                    parent_job=parent_job,
                    suffix="conflict",
                    coordination={"resource_claims": ["reports/final.md"]},
                )

        await store.assert_workspace_resource_owner(
            run_id=owner["id"],
            requested_path="/reports/final.md",
        )
        assert await store.has_foreign_workspace_resource_claims(owner["id"]) is False
        assert await store.has_foreign_workspace_resource_claims(parent["id"]) is True
        with pytest.raises(RunAdmissionError, match="owned by another"):
            await store.assert_workspace_resource_owner(
                run_id=parent["id"],
                requested_path="/reports/final.md",
            )
    finally:
        await store.close()


def test_completion_summary_handles_required_quorum_and_best_effort() -> None:
    summary = _collaboration_completion_summary(
        [
            {"id": "required", "status": "completed", "completion_mode": "required"},
            {
                "id": "q1",
                "status": "completed",
                "completion_mode": "quorum",
                "quorum_group": "sources",
                "quorum_required": 2,
            },
            {
                "id": "q2",
                "status": "completed",
                "completion_mode": "quorum",
                "quorum_group": "sources",
                "quorum_required": 2,
            },
            {
                "id": "q3",
                "status": "running",
                "completion_mode": "quorum",
                "quorum_group": "sources",
                "quorum_required": 2,
            },
            {"id": "optional", "status": "running", "completion_mode": "best_effort"},
        ]
    )

    assert summary["satisfied"] is True
    assert summary["impossible"] is False
    assert summary["wait_for"] == []
    assert summary["cancel"] == ["optional", "q3"]
    assert summary["quorum_groups"] == [
        {
            "group": "sources",
            "required": 2,
            "completed": 2,
            "active": 1,
            "failed": 0,
            "satisfied": True,
            "impossible": False,
        }
    ]


@pytest.mark.asyncio
async def test_parent_success_fails_and_cancels_remaining_children_when_required_child_fails(
    tmp_path: Path,
) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    try:
        parent, parent_job = await _claimed_parent(store, tmp_path)
        with store.bind_execution_lease(
            job_id=str(parent_job["id"]),
            run_id=str(parent["id"]),
            lease_owner="worker-parent",
            lease_generation=int(parent_job["lease_generation"]),
        ):
            required = await _spawn(
                store,
                parent=parent,
                parent_job=parent_job,
                suffix="required",
            )
            optional = await _spawn(
                store,
                parent=parent,
                parent_job=parent_job,
                suffix="optional",
                coordination={"completion_mode": "best_effort"},
            )

        required_job = await store.claim_run_job(worker_id="worker-required")
        assert required_job is not None
        assert required_job["run_id"] == required["id"]
        with store.bind_execution_lease(
            job_id=str(required_job["id"]),
            run_id=str(required["id"]),
            lease_owner="worker-required",
            lease_generation=int(required_job["lease_generation"]),
        ):
            await store.commit_run_result(
                required["id"],
                status="failed",
                event_type="run.failed",
                payload={"error": "required failed", "type": "TestFailure"},
            )

        coordinator = RunCoordinator(store, None)  # type: ignore[arg-type]
        outcome, summary = await coordinator._settle_child_coordination(
            parent["id"],
            RunOutcome("completed", "run.completed", {"final_text": "premature"}),
        )

        assert outcome.status == "failed"
        assert outcome.payload["type"] == "ChildCoordinationError"
        assert summary["impossible"] is True
        optional_record = await store.get_run(optional["id"])
        assert optional_record is not None
        assert optional_record["status"] == "canceled"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_collaboration_snapshot_is_complete_and_cursor_safe(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    try:
        parent, parent_job = await _claimed_parent(store, tmp_path)
        with store.bind_execution_lease(
            job_id=str(parent_job["id"]),
            run_id=str(parent["id"]),
            lease_owner="worker-parent",
            lease_generation=int(parent_job["lease_generation"]),
        ):
            child = await _spawn(
                store,
                parent=parent,
                parent_job=parent_job,
                suffix="snapshot",
                coordination={
                    "completion_mode": "quorum",
                    "quorum_group": "review",
                    "quorum_required": 1,
                    "resource_claims": ["review.md"],
                },
            )

        snapshot = await store.collaboration_snapshot(parent["id"])
        assert snapshot["root"]["id"] == parent["id"]
        assert [item["id"] for item in snapshot["children"]] == [child["id"]]
        assert snapshot["children"][0]["completion_mode"] == "quorum"
        assert snapshot["children"][0]["resource_claims"] == ["review.md"]
        assert snapshot["event_high_watermarks"] == {parent["id"]: 1, child["id"]: 0}
        assert snapshot["resource_owners"] == [
            {
                "resource_key": "review.md",
                "owner_run_id": child["id"],
                "created_at": snapshot["resource_owners"][0]["created_at"],
            }
        ]
        assert snapshot["dependencies"] == []
        assert snapshot["messages"] == []
        assert snapshot["pending_waits"] == []
        await store.close()
        store = await LocalStore.open(tmp_path / "runtime.db")
        recovered = await store.collaboration_snapshot(parent["id"])
        assert recovered["children"] == snapshot["children"]
        assert recovered["event_high_watermarks"] == snapshot["event_high_watermarks"]
        assert recovered["resource_owners"] == snapshot["resource_owners"]
    finally:
        await store.close()
