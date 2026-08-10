from __future__ import annotations

from pathlib import Path

import pytest

from shejane_runtime.auth import LOCAL_OWNER_PRINCIPAL_ID
from shejane_runtime.store.sqlite import (
    CommandConflictError,
    LocalStore,
    RunAdmissionError,
)


async def _mailbox_runs(store: LocalStore) -> tuple[dict, dict, dict, dict, dict]:
    root = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="coordinate",
        workspace_path=None,
    )
    first = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="research",
        workspace_path=None,
        parent_run_id=str(root["id"]),
        root_run_id=str(root["id"]),
        run_kind="child",
        agent_definition_id="subagent:researcher",
        agent_definition_version="sha256:researcher-v1",
        collaboration_depth=1,
    )
    second = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="review",
        workspace_path=None,
        parent_run_id=str(root["id"]),
        root_run_id=str(root["id"]),
        run_kind="child",
        agent_definition_id="subagent:writer",
        agent_definition_version="sha256:writer-v1",
        collaboration_depth=1,
    )
    await store.enqueue_run_job(str(first["id"]), kind="start")
    await store.enqueue_run_job(str(second["id"]), kind="start")
    first_job = await store.claim_run_job(worker_id="worker-first")
    second_job = await store.claim_run_job(worker_id="worker-second")
    assert first_job is not None and first_job["run_id"] == first["id"]
    assert second_job is not None and second_job["run_id"] == second["id"]
    return root, first, second, first_job, second_job


async def _begin_mailbox_receipt(
    store: LocalStore,
    run: dict,
    job: dict,
    *,
    operation_id: str,
    tool_name: str,
) -> None:
    attempt_id = f"{job['id']}:{job['lease_generation']}"
    await store.prepare_tool_receipt(
        operation_id=operation_id,
        run_id=str(run["id"]),
        execution_attempt_id=attempt_id,
        execution_namespace="main",
        tool_call_id=f"call-{operation_id}",
        tool_name=tool_name,
        tool_version="graph-v1",
        arguments_hash=f"args-{operation_id}",
        arguments_json="{}",
        risk="control_flow",
    )
    await store.begin_tool_receipt(
        operation_id=operation_id,
        run_id=str(run["id"]),
        execution_attempt_id=attempt_id,
    )


@pytest.mark.asyncio
async def test_mailbox_request_delivery_reply_and_ack_are_durable(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    try:
        root, first, second, first_job, second_job = await _mailbox_runs(store)
        with store.bind_execution_lease(
            job_id=str(first_job["id"]),
            run_id=str(first["id"]),
            lease_owner="worker-first",
            lease_generation=int(first_job["lease_generation"]),
        ):
            await _begin_mailbox_receipt(
                store,
                first,
                first_job,
                operation_id="toolop-send-question",
                tool_name="mailbox.send",
            )
            question, created = await store.send_agent_message(
                sender_run_id=str(first["id"]),
                sender_operation_id="toolop-send-question",
                recipient_run_id=str(second["id"]),
                kind="question",
                text="Which claim needs stronger evidence?",
                data={"claim_id": "claim-1"},
                artifact_refs=[],
                ttl_seconds=3600,
            )
        assert created is True
        assert question["root_run_id"] == root["id"]
        assert question["correlation_id"] == question["id"]
        assert question["sequence"] == 1
        assert question["status"] == "queued"

        with store.bind_execution_lease(
            job_id=str(second_job["id"]),
            run_id=str(second["id"]),
            lease_owner="worker-second",
            lease_generation=int(second_job["lease_generation"]),
        ):
            delivered = await store.deliver_agent_messages(str(second["id"]))
            assert [item["id"] for item in delivered] == [question["id"]]
            assert delivered[0]["status"] == "delivered"
            await _begin_mailbox_receipt(
                store,
                second,
                second_job,
                operation_id="toolop-reply-result",
                tool_name="mailbox.reply",
            )
            reply, reply_created = await store.reply_agent_message(
                sender_run_id=str(second["id"]),
                sender_operation_id="toolop-reply-result",
                in_reply_to=str(question["id"]),
                kind="result",
                text="Claim 1 needs a primary source.",
                data={},
                artifact_refs=[],
                ttl_seconds=3600,
            )
        assert reply_created is True
        assert reply["recipient_run_id"] == first["id"]
        assert reply["correlation_id"] == question["id"]
        assert reply["in_reply_to"] == question["id"]
        assert reply["sequence"] == 2

        with store.bind_execution_lease(
            job_id=str(second_job["id"]),
            run_id=str(second["id"]),
            lease_owner="worker-second",
            lease_generation=int(second_job["lease_generation"]),
        ):
            replayed_reply, replay_created = await store.reply_agent_message(
                sender_run_id=str(second["id"]),
                sender_operation_id="toolop-reply-result",
                in_reply_to=str(question["id"]),
                kind="result",
                text="Claim 1 needs a primary source.",
                data={},
                artifact_refs=[],
                ttl_seconds=3600,
            )
        assert replay_created is False
        assert replayed_reply["id"] == reply["id"]

        with store.bind_execution_lease(
            job_id=str(first_job["id"]),
            run_id=str(first["id"]),
            lease_owner="worker-first",
            lease_generation=int(first_job["lease_generation"]),
        ):
            received = await store.deliver_agent_messages(str(first["id"]))
            assert [item["id"] for item in received] == [reply["id"]]
            await _begin_mailbox_receipt(
                store,
                first,
                first_job,
                operation_id="toolop-ack-result",
                tool_name="mailbox.ack",
            )
            acknowledged = await store.ack_agent_messages(
                recipient_run_id=str(first["id"]),
                message_ids=[str(reply["id"])],
            )
        assert acknowledged[0]["status"] == "acknowledged"
        assert [item["id"] for item in await store.list_agent_inbox(str(first["id"]))] == [
            reply["id"]
        ]
        assert [item["id"] for item in await store.list_agent_outbox(str(first["id"]))] == [
            question["id"]
        ]

        sender_events = await store.events_since(str(first["id"]))
        recipient_events = await store.events_since(str(second["id"]))
        assert "agent.message.sent" in [event["event_type"] for event in sender_events]
        assert "agent.message.received" in [event["event_type"] for event in recipient_events]
        assert "agent.message.acknowledged" in [event["event_type"] for event in sender_events]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_mailbox_send_is_idempotent_and_rejects_changed_replay(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    try:
        _root, first, second, first_job, _second_job = await _mailbox_runs(store)
        with store.bind_execution_lease(
            job_id=str(first_job["id"]),
            run_id=str(first["id"]),
            lease_owner="worker-first",
            lease_generation=int(first_job["lease_generation"]),
        ):
            await _begin_mailbox_receipt(
                store,
                first,
                first_job,
                operation_id="toolop-idempotent-send",
                tool_name="mailbox.send",
            )
            first_message, first_created = await store.send_agent_message(
                sender_run_id=str(first["id"]),
                sender_operation_id="toolop-idempotent-send",
                recipient_run_id=str(second["id"]),
                kind="request",
                text="Review claim 1.",
                data={},
                artifact_refs=[],
                ttl_seconds=3600,
            )
            replay, replay_created = await store.send_agent_message(
                sender_run_id=str(first["id"]),
                sender_operation_id="toolop-idempotent-send",
                recipient_run_id=str(second["id"]),
                kind="request",
                text="Review claim 1.",
                data={},
                artifact_refs=[],
                ttl_seconds=3600,
            )
            with pytest.raises(CommandConflictError):
                await store.send_agent_message(
                    sender_run_id=str(first["id"]),
                    sender_operation_id="toolop-idempotent-send",
                    recipient_run_id=str(second["id"]),
                    kind="request",
                    text="Changed request.",
                    data={},
                    artifact_refs=[],
                    ttl_seconds=3600,
                )
        assert first_created is True
        assert replay_created is False
        assert replay["id"] == first_message["id"]
        assert len(await store.list_agent_outbox(str(first["id"]))) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_mailbox_rejects_self_foreign_root_and_backpressure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    try:
        _root, first, second, first_job, _second_job = await _mailbox_runs(store)
        foreign = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="foreign",
            workspace_path=None,
        )
        foreign_artifact = await store.create_artifact(
            run_id=str(foreign["id"]),
            kind="note",
            title="foreign",
            content="not part of this collaboration root",
        )
        monkeypatch.setattr("shejane_runtime.store.collaboration.MAX_AGENT_MAILBOX_PENDING", 1)
        with store.bind_execution_lease(
            job_id=str(first_job["id"]),
            run_id=str(first["id"]),
            lease_owner="worker-first",
            lease_generation=int(first_job["lease_generation"]),
        ):
            for operation_id in (
                "toolop-self",
                "toolop-foreign",
                "toolop-foreign-artifact",
                "toolop-first",
                "toolop-full",
            ):
                await _begin_mailbox_receipt(
                    store,
                    first,
                    first_job,
                    operation_id=operation_id,
                    tool_name="mailbox.send",
                )
            with pytest.raises(RunAdmissionError, match="itself"):
                await store.send_agent_message(
                    sender_run_id=str(first["id"]),
                    sender_operation_id="toolop-self",
                    recipient_run_id=str(first["id"]),
                    kind="update",
                    text="loop",
                    data={},
                    artifact_refs=[],
                    ttl_seconds=60,
                )
            with pytest.raises(RunAdmissionError, match="collaboration root"):
                await store.send_agent_message(
                    sender_run_id=str(first["id"]),
                    sender_operation_id="toolop-foreign",
                    recipient_run_id=str(foreign["id"]),
                    kind="update",
                    text="escape",
                    data={},
                    artifact_refs=[],
                    ttl_seconds=60,
                )
            with pytest.raises(RunAdmissionError, match="artifact references"):
                await store.send_agent_message(
                    sender_run_id=str(first["id"]),
                    sender_operation_id="toolop-foreign-artifact",
                    recipient_run_id=str(second["id"]),
                    kind="update",
                    text="escape through artifact",
                    data={},
                    artifact_refs=[str(foreign_artifact["id"])],
                    ttl_seconds=60,
                )
            await store.send_agent_message(
                sender_run_id=str(first["id"]),
                sender_operation_id="toolop-first",
                recipient_run_id=str(second["id"]),
                kind="update",
                text="first",
                data={},
                artifact_refs=[],
                ttl_seconds=60,
            )
            with pytest.raises(RunAdmissionError, match="backpressure"):
                await store.send_agent_message(
                    sender_run_id=str(first["id"]),
                    sender_operation_id="toolop-full",
                    recipient_run_id=str(second["id"]),
                    kind="update",
                    text="too many",
                    data={},
                    artifact_refs=[],
                    ttl_seconds=60,
                )
    finally:
        await store.close()
