from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from shejane_runtime.auth import LOCAL_OWNER_PRINCIPAL_ID
from shejane_runtime.config import Settings
from shejane_runtime.runs import RunCoordinator, RunOutcome
from shejane_runtime.store.fenced_checkpointer import FencedCheckpointer
from shejane_runtime.store.sqlite import LeaseFenceError, LocalStore, RunAdmissionError


async def _accepted_run(
    store: LocalStore,
    command_id: str = "cmd_job",
    *,
    workspace_path: str | None = None,
) -> dict:
    run, _created = await store.accept_run_command(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        command_id=command_id,
        client_message_id=f"msg_{command_id}",
        command_payload={
            "type": "run.start",
            "goal": "inspect",
            "workspace_path": workspace_path,
            "model": "auto",
        },
        goal="inspect",
        workspace_path=workspace_path,
        mode="auto",
    )
    return run


async def test_accepting_a_command_atomically_creates_one_pending_job(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    try:
        run = await _accepted_run(store)
        job = await store.get_active_run_job(run["id"])

        assert job is not None
        assert job["status"] == "pending"
        assert job["kind"] == "start"
        assert job["attempt"] == 0
        assert json.loads(job["input_json"])["principal_id"] == LOCAL_OWNER_PRINCIPAL_ID
    finally:
        await store.close()


async def test_pending_job_can_be_claimed_after_store_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "local.db"
    store = await LocalStore.open(db_path)
    run = await _accepted_run(store, "cmd_restart")
    await store.close()

    reopened = await LocalStore.open(db_path)
    try:
        claimed = await reopened.claim_run_job(worker_id="worker-after-restart")
        assert claimed is not None
        assert claimed["run_id"] == run["id"]
    finally:
        await reopened.close()


async def test_boot_recovery_enqueues_a_fully_resolved_wait_cycle(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    coordinator = RunCoordinator(
        store=store,
        checkpointer=None,  # type: ignore[arg-type]
        settings=Settings(SHEJANE_FAKE_LLM=True),
    )
    try:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="resume after crash",
            workspace_path=None,
        )
        permission = await store.create_permission(
            run_id=str(run["id"]),
            tool_call_id="call-1",
            tool_name="write_file",
            arguments={"path": "a.txt"},
            wait_cycle_id="wait-1",
            interrupt_id="interrupt-1",
        )
        await store.resolve_permission(
            str(permission["id"]),
            status="approved",
            decision={"type": "approve"},
        )
        await store.update_run_status(str(run["id"]), "waiting_permission")

        await coordinator.recover_orphans()

        job = await store.get_active_run_job(str(run["id"]))
        assert job is not None
        assert job["kind"] == "resume"
        assert json.loads(job["resume_json"]) == {
            "interrupt-1": {"decisions": [{"type": "approve"}]}
        }
    finally:
        await store.close()


async def test_worker_rechecks_frozen_model_credential_reference(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    settings = Settings(SHEJANE_FAKE_LLM=True)
    coordinator = RunCoordinator(
        store=store,
        checkpointer=None,  # type: ignore[arg-type]
        settings=settings,
    )
    try:
        run = await coordinator.start_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_credential_recheck",
            client_message_id="msg_credential_recheck",
            protocol_version=1,
            required_capabilities=["agent.run", "agent.stream"],
            goal="inspect",
        )
        job = await store.claim_run_job(worker_id=coordinator._worker_id)
        assert job is not None

        settings.fake_llm = False
        await coordinator._execute_claimed_job(job)

        failed = await store.get_run(run["id"])
        assert failed is not None
        assert failed["status"] == "failed"
        events = await store.events_since(run["id"], after_seq=0)
        assert json.loads(events[-1]["payload_json"])["type"] == "ExecutionModelBindingError"
    finally:
        await store.close()


async def test_run_admission_freezes_required_image_generation_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    coordinator = RunCoordinator(
        store=store,
        checkpointer=None,  # type: ignore[arg-type]
        settings=Settings(SHEJANE_FAKE_LLM=False),
    )
    monkeypatch.setattr("shejane_runtime.runs.get_model_api_key", lambda *_args: _async("secret"))
    try:
        models = [
            {
                "model_id": "agent-model",
                "display_name": "Agent",
                "source": "manual",
                "tool_calling": True,
                "streaming": True,
                "capabilities": [
                    {
                        "capability": "agent_chat",
                        "protocol": "openai_chat_completions",
                        "verification": "verified",
                    }
                ],
            },
            {
                "model_id": "image-model",
                "display_name": "Image",
                "source": "manual",
                "capabilities": [
                    {
                        "capability": "image_generation",
                        "protocol": "openai_images_generations",
                        "verification": "verified",
                    }
                ],
            },
        ]
        await store.create_model_connection(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            connection_id="conn-models",
            preset_id="custom",
            name="Models",
            region="custom",
            adapter_id="openai_chat",
            base_url="https://models.example/v1",
            requires_api_key=True,
            credential_ref="keyring:model-service:conn-models",
            models=models,
            catalog_status="ready",
        )
        await store.set_model_capability_binding(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            capability="image_generation",
            connection_id="conn-models",
            connection_version=1,
            model_id="image-model",
            protocol="openai_images_generations",
        )

        run = await coordinator.start_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_image_binding",
            client_message_id="msg_image_binding",
            protocol_version=1,
            required_capabilities=["agent.run", "agent.stream"],
            required_tools=["image.generate"],
            goal="draw",
            mode="local:conn-models:agent-model",
        )

        settings = json.loads(run["settings_json"])
        assert settings["_required_tools"] == ["image.generate"]
        assert settings["_model_binding"]["protocol"] == "openai_chat_completions"
        assert settings["_capability_bindings"]["image_generation"]["model_id"] == "image-model"
    finally:
        await store.close()


async def _async(value: Any) -> Any:
    return value


async def test_preflight_crash_is_persisted_as_a_failed_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    coordinator = RunCoordinator(
        store=store,
        checkpointer=None,  # type: ignore[arg-type]
        settings=Settings(SHEJANE_FAKE_LLM=True),
    )
    original_admission = store.workspace_admission_error
    calls = 0

    async def fail_once(**kwargs: Any) -> str | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected preflight crash")
        return await original_admission(**kwargs)

    monkeypatch.setattr(store, "workspace_admission_error", fail_once)
    try:
        run = await coordinator.start_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_preflight_crash",
            client_message_id="msg_preflight_crash",
            protocol_version=1,
            required_capabilities=["agent.run", "agent.stream"],
            goal="inspect",
        )
        job = await store.claim_run_job(worker_id=coordinator._worker_id)
        assert job is not None
        await coordinator._slots.acquire()

        await coordinator._execute_claimed_job(job)

        failed = await store.get_run(run["id"])
        assert failed is not None
        assert failed["status"] == "failed"
        settled_job = await store.get_run_job(job["id"])
        assert settled_job is not None
        assert settled_job["status"] == "dead"
        events = await store.events_since(run["id"])
        failure = json.loads(events[-1]["payload_json"])
        assert events[-1]["event_type"] == "run.failed"
        assert failure["type"] == "RuntimeError"
    finally:
        await store.close()


async def test_job_finalization_failure_still_releases_worker_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    coordinator = RunCoordinator(
        store=store,
        checkpointer=None,  # type: ignore[arg-type]
        settings=Settings(SHEJANE_FAKE_LLM=True),
        max_concurrent_runs=1,
    )

    async def drive(**_kwargs: Any) -> RunOutcome:
        return RunOutcome("failed", "run.failed", {"error": "expected"})

    async def fail_finish(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("injected job finalization failure")

    coordinator._drive_run = drive  # type: ignore[method-assign]
    monkeypatch.setattr(store, "finish_run_job", fail_finish)
    try:
        run = await coordinator.start_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_finish_failure",
            client_message_id="msg_finish_failure",
            protocol_version=1,
            required_capabilities=["agent.run", "agent.stream"],
            goal="inspect",
        )
        job = await store.claim_run_job(worker_id=coordinator._worker_id)
        assert job is not None
        await coordinator._slots.acquire()

        await coordinator._execute_claimed_job(job)

        await asyncio.wait_for(coordinator._slots.acquire(), timeout=0.1)
        coordinator._slots.release()
        assert run["id"] not in coordinator._wakeups
        assert run["id"] not in coordinator._goals
        assert run["id"] not in coordinator._tasks
    finally:
        await store.close()


async def test_execution_resources_close_when_claimed_attempt_finishes(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    coordinator = RunCoordinator(
        store=store,
        checkpointer=None,  # type: ignore[arg-type]
        settings=Settings(SHEJANE_FAKE_LLM=True),
    )
    closed = asyncio.Event()

    async def drive(**kwargs: Any) -> None:
        kwargs["resource_stack"].callback(closed.set)
        assert not closed.is_set()

    coordinator._drive_run = drive  # type: ignore[method-assign]
    try:
        await coordinator.start_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_resource_scope",
            client_message_id="msg_resource_scope",
            protocol_version=1,
            required_capabilities=["agent.run", "agent.stream"],
            goal="inspect",
        )
        job = await store.claim_run_job(worker_id=coordinator._worker_id)
        assert job is not None

        await coordinator._execute_claimed_job(job)

        assert closed.is_set()
    finally:
        await store.close()


async def test_execution_resources_close_before_result_is_committed(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    coordinator = RunCoordinator(
        store=store,
        checkpointer=None,  # type: ignore[arg-type]
        settings=Settings(SHEJANE_FAKE_LLM=True),
    )
    order: list[str] = []

    async def drive(**kwargs: Any) -> RunOutcome:
        kwargs["resource_stack"].callback(order.append, "cleanup")
        await store.update_assistant_draft(
            run_id=kwargs["run_id"],
            message_key="answer-v1",
            content="done",
            tool_calls=[],
        )
        return RunOutcome("completed", "run.completed", {})

    original_commit = coordinator._commit_run_result

    async def commit(*args: Any, **kwargs: Any) -> None:
        order.append("commit")
        await original_commit(*args, **kwargs)

    coordinator._drive_run = drive  # type: ignore[method-assign]
    coordinator._commit_run_result = commit  # type: ignore[method-assign]
    try:
        run = await coordinator.start_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_resource_order",
            client_message_id="msg_resource_order",
            protocol_version=1,
            required_capabilities=["agent.run", "agent.stream"],
            goal="inspect",
        )
        job = await store.claim_run_job(worker_id=coordinator._worker_id)
        assert job is not None

        await coordinator._execute_claimed_job(job)

        assert order == ["cleanup", "commit"]
        completed = await store.get_run(run["id"])
        assert completed is not None
        assert completed["status"] == "completed"
    finally:
        await store.close()


async def test_each_live_stream_subscriber_receives_the_complete_ordered_event_log(
    tmp_path: Path,
) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    coordinator = RunCoordinator(
        store=store,
        checkpointer=None,  # type: ignore[arg-type]
        settings=Settings(SHEJANE_FAKE_LLM=True),
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def drive(**kwargs: Any) -> RunOutcome:
        entered.set()
        await release.wait()
        await coordinator.emit_for_run(kwargs["run_id"], "test.first", {"value": 1})
        await coordinator.emit_for_run(kwargs["run_id"], "llm.delta", {"content": "live"})
        await store.append_event(kwargs["run_id"], "test.database_only", {"value": 2})
        await coordinator.emit_for_run(kwargs["run_id"], "test.second", {"value": 2})
        return RunOutcome("failed", "run.failed", {"error": "expected test failure"})

    coordinator._drive_run = drive  # type: ignore[method-assign]
    try:
        run = await coordinator.start_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_stream_fanout",
            client_message_id="msg_stream_fanout",
            protocol_version=1,
            required_capabilities=["agent.run", "agent.stream"],
            goal="inspect",
        )
        job = await store.claim_run_job(worker_id=coordinator._worker_id)
        assert job is not None
        execution = asyncio.create_task(coordinator._execute_claimed_job(job))
        await asyncio.wait_for(entered.wait(), timeout=1)

        async def collect() -> list[str]:
            return [event["event_type"] async for event in coordinator.stream(run["id"])]

        first = asyncio.create_task(collect())
        second = asyncio.create_task(collect())
        await asyncio.sleep(0)
        release.set()
        first_events, second_events, _ = await asyncio.wait_for(
            asyncio.gather(first, second, execution),
            timeout=1,
        )

        expected = [
            "test.first",
            "llm.delta",
            "test.database_only",
            "test.second",
            "run.failed",
        ]
        assert first_events == expected
        assert second_events == expected
        assert [event["event_type"] for event in await store.events_since(run["id"])] == [
            "test.first",
            "test.database_only",
            "test.second",
            "run.failed",
        ]
    finally:
        release.set()
        await store.close()


async def test_stream_registration_failure_removes_its_subscriber(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    coordinator = RunCoordinator(store=store, checkpointer=None)  # type: ignore[arg-type]
    run = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="fail initial replay",
        workspace_path=None,
    )

    async def fail_events_since(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("injected replay failure")

    store.events_since = fail_events_since  # type: ignore[method-assign]
    try:
        stream = coordinator.stream(run["id"])
        with pytest.raises(RuntimeError, match="injected replay failure"):
            await anext(stream)
        assert run["id"] not in coordinator._live_subscribers
        assert run["id"] not in coordinator._stream_locks
    finally:
        await store.close()


async def test_live_stream_preserves_transient_order_while_filling_a_durable_gap(
    tmp_path: Path,
) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    coordinator = RunCoordinator(store=store, checkpointer=None)  # type: ignore[arg-type]
    run = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="preserve mixed event order",
        workspace_path=None,
    )
    stream = coordinator.stream(run["id"])
    try:
        first_event = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        await coordinator.emit_for_run(run["id"], "test.first", {"value": 1})
        assert (await asyncio.wait_for(first_event, timeout=1))["event_type"] == "test.first"

        await coordinator.emit_for_run(run["id"], "llm.delta", {"content": "live"})
        await store.append_event(run["id"], "test.database_only", {"value": 2})
        await coordinator.emit_for_run(run["id"], "test.second", {"value": 3})

        assert [
            (await asyncio.wait_for(anext(stream), timeout=1))["event_type"] for _ in range(3)
        ] == [
            "llm.delta",
            "test.database_only",
            "test.second",
        ]
    finally:
        await stream.aclose()
        await store.close()


async def test_slow_initial_replay_does_not_block_another_run_stream(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    coordinator = RunCoordinator(store=store, checkpointer=None)  # type: ignore[arg-type]
    first = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="slow replay",
        workspace_path=None,
    )
    second = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="independent replay",
        workspace_path=None,
    )
    replay_started = asyncio.Event()
    release_replay = asyncio.Event()
    events_since = store.events_since

    async def delay_first_run(run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        if run_id == first["id"]:
            replay_started.set()
            await release_replay.wait()
        return await events_since(run_id, after_seq=after_seq)

    store.events_since = delay_first_run  # type: ignore[method-assign]
    first_stream = coordinator.stream(first["id"])
    second_stream = coordinator.stream(second["id"])
    first_event = asyncio.create_task(anext(first_stream))
    try:
        await asyncio.wait_for(replay_started.wait(), timeout=1)
        second_event = asyncio.create_task(anext(second_stream))
        await asyncio.sleep(0)
        await coordinator.emit_for_run(second["id"], "llm.delta", {"content": "live"})
        event = await asyncio.wait_for(second_event, timeout=1)
        assert event["payload"] == {"content": "live"}
    finally:
        release_replay.set()
        first_event.cancel()
        await asyncio.gather(first_event, return_exceptions=True)
        await first_stream.aclose()
        await second_stream.aclose()
        await store.close()


async def test_cleanup_failure_quarantines_without_releasing_the_job(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    coordinator = RunCoordinator(
        store=store,
        checkpointer=None,  # type: ignore[arg-type]
        settings=Settings(SHEJANE_FAKE_LLM=True),
    )

    async def drive(**kwargs: Any) -> RunOutcome:
        async def fail_cleanup() -> None:
            raise RuntimeError("cleanup failed")

        kwargs["resource_stack"].push_async_callback(fail_cleanup)
        return RunOutcome("waiting_input", "run.waiting", {"next": ["tools"]})

    coordinator._drive_run = drive  # type: ignore[method-assign]
    quarantine_committed = asyncio.Event()
    release_quarantine_return = asyncio.Event()
    quarantine = store.quarantine_execution_attempt

    async def delayed_quarantine(*args: Any, **kwargs: Any) -> dict[str, Any]:
        event = await quarantine(*args, **kwargs)
        quarantine_committed.set()
        await release_quarantine_return.wait()
        return event

    store.quarantine_execution_attempt = delayed_quarantine  # type: ignore[method-assign]
    try:
        run = await coordinator.start_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_cleanup_failure",
            client_message_id="msg_cleanup_failure",
            protocol_version=1,
            required_capabilities=["agent.run", "agent.stream"],
            goal="inspect",
        )
        job = await store.claim_run_job(worker_id=coordinator._worker_id)
        assert job is not None

        execution = asyncio.create_task(coordinator._execute_claimed_job(job))
        await asyncio.wait_for(quarantine_committed.wait(), timeout=1)
        # Reproduce heartbeat observing the just-committed quarantine before
        # quarantine_execution_attempt() returns to its owner.
        await coordinator._heartbeat_job(job, execution)
        assert not execution.cancelled()
        release_quarantine_return.set()
        await asyncio.wait_for(execution, timeout=1)

        saved = await store.get_run(run["id"])
        finished_job = await store.get_run_job(str(job["id"]))
        events = await store.events_since(run["id"])
        payload = json.loads(events[-1]["payload_json"])
        assert saved is not None and saved["status"] == "cleanup_required"
        assert finished_job is not None and finished_job["status"] == "leased"
        assert finished_job["quarantined_at"] is not None
        assert finished_job["lease_owner"] == coordinator._worker_id
        assert payload["cleanup"] == {
            "status": "failed",
            "error_type": "RuntimeError",
        }
        assert events[-1]["event_type"] == "run.cleanup_required"
        assert await store.enqueue_run_job(run["id"], kind="start") == finished_job
        replayed = [event async for event in coordinator.stream(run["id"])]
        assert [event["event_type"] for event in replayed] == ["run.cleanup_required"]
        assert run["id"] not in coordinator._goals
        assert run["id"] not in coordinator._user_inputs
        assert run["id"] not in coordinator._histories
    finally:
        release_quarantine_return.set()
        await store.close()


async def test_expired_owner_confirms_cleanup_before_quarantine_is_released(
    tmp_path: Path,
) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    coordinator = RunCoordinator(
        store=store,
        checkpointer=None,  # type: ignore[arg-type]
        settings=Settings(SHEJANE_FAKE_LLM=True),
        lease_seconds=0.1,
    )
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    async def drive(**kwargs: Any) -> RunOutcome:
        async def mark_cleaned() -> None:
            cleaned.set()

        kwargs["resource_stack"].push_async_callback(mark_cleaned)
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    coordinator._drive_run = drive  # type: ignore[method-assign]
    try:
        run = await coordinator.start_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_expired_cleanup",
            client_message_id="msg_expired_cleanup",
            protocol_version=1,
            required_capabilities=["agent.run", "agent.stream"],
            goal="inspect",
        )
        job = await store.claim_run_job(
            worker_id=coordinator._worker_id,
            lease_seconds=0.1,
        )
        assert job is not None
        task = asyncio.create_task(coordinator._execute_claimed_job(job))
        await asyncio.wait_for(entered.wait(), timeout=1)
        await store._conn.execute(
            "UPDATE local_run_jobs SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (job["id"],),
        )
        await store._conn.commit()

        await asyncio.wait_for(task, timeout=2)

        assert await store.claim_run_job(worker_id="worker-new") is None
        assert cleaned.is_set()
        saved = await store.get_run(run["id"])
        finished_job = await store.get_run_job(str(job["id"]))
        events = await store.events_since(run["id"])
        assert saved is not None and saved["status"] == "failed"
        assert finished_job is not None and finished_job["status"] == "dead"
        assert [event["event_type"] for event in events] == [
            "run.cleanup_required",
            "run.failed",
        ]
        failure = json.loads(events[-1]["payload_json"])
        assert failure["execution"]["lease"] == "lost"
        assert failure["execution"]["cleanup"] == {"status": "completed"}
    finally:
        await store.close()


async def test_stop_refuses_teardown_while_execution_cleanup_is_unconfirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shejane_runtime.runs as runs_module

    store = await LocalStore.open(tmp_path / "local.db")
    coordinator = RunCoordinator(
        store=store,
        checkpointer=None,  # type: ignore[arg-type]
        settings=Settings(SHEJANE_FAKE_LLM=True),
    )
    entered = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def drive(**kwargs: Any) -> RunOutcome:
        async def blocked_cleanup() -> None:
            await release_cleanup.wait()

        kwargs["resource_stack"].push_async_callback(blocked_cleanup)
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    coordinator._drive_run = drive  # type: ignore[method-assign]
    monkeypatch.setattr(runs_module, "RUN_SHUTDOWN_TIMEOUT_SECONDS", 0.05)
    try:
        run = await coordinator.start_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_shutdown_cleanup",
            client_message_id="msg_shutdown_cleanup",
            protocol_version=1,
            required_capabilities=["agent.run", "agent.stream"],
            goal="inspect",
        )
        job = await store.claim_run_job(worker_id=coordinator._worker_id)
        assert job is not None
        task = asyncio.create_task(coordinator._execute_claimed_job(job))
        coordinator._tasks[run["id"]] = task
        await asyncio.wait_for(entered.wait(), timeout=1)

        with pytest.raises(RuntimeError, match="could not confirm cleanup"):
            await coordinator.stop()
        assert not task.done()

        release_cleanup.set()
        await asyncio.wait_for(task, timeout=1)
        assert (await store.get_run(run["id"]))["status"] == "failed"
    finally:
        release_cleanup.set()
        await asyncio.gather(*coordinator._tasks.values(), return_exceptions=True)
        await store.close()


async def test_settlement_uses_durable_assistant_and_ledgers(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    coordinator = RunCoordinator(
        store=store,
        checkpointer=None,  # type: ignore[arg-type]
        settings=Settings(SHEJANE_FAKE_LLM=True),
    )
    try:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="settle",
            workspace_path=None,
        )
        await store.update_assistant_draft(
            run_id=run["id"],
            message_key="answer-v2",
            content="authoritative answer",
            tool_calls=[],
        )
        call = await store.reserve_model_call(
            run_id=run["id"],
            execution_attempt_id="attempt-1",
            model="fake",
            max_calls=5,
        )
        await store.settle_model_call(
            run_id=run["id"],
            call_id=call["id"],
            provider_request_id="provider-1",
            input_tokens=7,
            output_tokens=3,
        )

        outcome = await coordinator._settle_execution_outcome(
            run_id=run["id"],
            execution_attempt_id="attempt-1",
            outcome=RunOutcome("completed", "run.completed", {}),
            cleanup_report={"status": "completed"},
        )

        assert outcome.status == "completed"
        assert outcome.payload["final_text"] == "authoritative answer"
        assert outcome.payload["final_answer_ref"] == {
            "message_key": "answer-v2",
            "revision": 1,
        }
        assert outcome.payload["input_tokens"] == 7
        assert outcome.payload["execution"]["model_calls"]["statuses"] == {"completed": 1}
    finally:
        await store.close()


async def test_settlement_rejects_completion_without_durable_answer(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    coordinator = RunCoordinator(
        store=store,
        checkpointer=None,  # type: ignore[arg-type]
        settings=Settings(SHEJANE_FAKE_LLM=True),
    )
    try:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="missing answer",
            workspace_path=None,
        )
        outcome = await coordinator._settle_execution_outcome(
            run_id=run["id"],
            execution_attempt_id="attempt-1",
            outcome=RunOutcome("completed", "run.completed", {}),
            cleanup_report={"status": "completed"},
        )
        assert outcome.status == "failed"
        assert outcome.payload["type"] == "ExecutionSettlementError"
        assert "draft is missing" in outcome.payload["error"]
    finally:
        await store.close()


async def test_stop_cancels_attempts_before_waiting_for_cleanup(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    coordinator = RunCoordinator(
        store=store,
        checkpointer=None,  # type: ignore[arg-type]
        settings=Settings(SHEJANE_FAKE_LLM=True),
    )
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def active_attempt() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    task = asyncio.create_task(active_attempt())
    await started.wait()
    coordinator._tasks["run-1"] = task
    try:
        await coordinator.stop()
        assert task.cancelled()
        assert cleaned.is_set()
    finally:
        await store.close()


async def test_waiting_run_accepts_resume_before_previous_task_bookkeeping_finishes(
    tmp_path: Path,
) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    coordinator = RunCoordinator(
        store=store,
        checkpointer=None,  # type: ignore[arg-type]
        settings=Settings(SHEJANE_FAKE_LLM=True),
    )
    resumed: list[bool] = []

    async def drive(**_kwargs: Any) -> RunOutcome:
        return RunOutcome(
            "waiting_input",
            "run.waiting",
            {"next": ["tools"], "interrupts": []},
        )

    original_commit = coordinator._commit_run_result

    async def commit(*args: Any, **kwargs: Any) -> None:
        await original_commit(*args, **kwargs)
        resumed.append(
            await coordinator.resume_run(
                run_id=str(args[1]),
                decision={"answers": {"question": ["answer"]}},
            )
        )

    coordinator._drive_run = drive  # type: ignore[method-assign]
    coordinator._commit_run_result = commit  # type: ignore[method-assign]
    try:
        run = await coordinator.start_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_waiting_resume_race",
            client_message_id="msg_waiting_resume_race",
            protocol_version=1,
            required_capabilities=["agent.run", "agent.stream"],
            goal="inspect",
        )
        job = await store.claim_run_job(worker_id=coordinator._worker_id)
        assert job is not None
        owner = asyncio.current_task()
        assert owner is not None
        coordinator._tasks[run["id"]] = owner

        await coordinator._execute_claimed_job(job)

        assert resumed == [True]
        next_job = await store.get_active_run_job(run["id"])
        assert next_job is not None
        assert next_job["kind"] == "resume"
        assert next_job["status"] == "pending"
    finally:
        await store.close()


async def test_resumed_attempt_keeps_its_wakeup_while_previous_attempt_finishes(
    tmp_path: Path,
) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    coordinator = RunCoordinator(
        store=store,
        checkpointer=None,  # type: ignore[arg-type]
        settings=Settings(SHEJANE_FAKE_LLM=True),
    )
    first_commit_blocked = asyncio.Event()
    release_first_commit = asyncio.Event()
    second_entered = asyncio.Event()
    inspect_second = asyncio.Event()
    wakeup_survived: list[bool] = []
    drive_count = 0

    async def drive(**kwargs: Any) -> RunOutcome:
        nonlocal drive_count
        drive_count += 1
        if drive_count == 1:
            return RunOutcome(
                "waiting_input",
                "run.waiting",
                {"next": ["tools"], "interrupts": [{"kind": "question"}]},
            )
        second_entered.set()
        await inspect_second.wait()
        wakeup_survived.append(kwargs["run_id"] in coordinator._wakeups)
        return RunOutcome("failed", "run.failed", {"error": "expected test failure"})

    original_commit = coordinator._commit_run_result

    async def commit(*args: Any, **kwargs: Any) -> None:
        await original_commit(*args, **kwargs)
        if kwargs["status"] != "waiting_input":
            return
        assert await coordinator.resume_run(
            run_id=str(args[1]),
            decision={"answers": {"question": ["answer"]}},
        )
        first_commit_blocked.set()
        await release_first_commit.wait()

    coordinator._drive_run = drive  # type: ignore[method-assign]
    coordinator._commit_run_result = commit  # type: ignore[method-assign]
    try:
        await coordinator.start_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_attempt_wakeup",
            client_message_id="msg_attempt_wakeup",
            protocol_version=1,
            required_capabilities=["agent.run", "agent.stream"],
            goal="inspect",
        )
        first_job = await store.claim_run_job(worker_id=coordinator._worker_id)
        assert first_job is not None
        first = asyncio.create_task(coordinator._execute_claimed_job(first_job))
        await asyncio.wait_for(first_commit_blocked.wait(), timeout=1)

        second_job = await store.claim_run_job(worker_id=coordinator._worker_id)
        assert second_job is not None and second_job["kind"] == "resume"
        second = asyncio.create_task(coordinator._execute_claimed_job(second_job))
        await asyncio.wait_for(second_entered.wait(), timeout=1)

        release_first_commit.set()
        await asyncio.wait_for(first, timeout=1)
        inspect_second.set()
        await asyncio.wait_for(second, timeout=1)

        assert wakeup_survived == [True]
    finally:
        release_first_commit.set()
        inspect_second.set()
        await store.close()


async def test_worker_rejects_unknown_settings_snapshot_version(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    coordinator = RunCoordinator(store=store, checkpointer=None)  # type: ignore[arg-type]
    try:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="inspect",
            workspace_path=None,
            settings={"_snapshot_version": 2},
            mode="auto",
        )
        await store.enqueue_run_job(run["id"], kind="start")
        job = await store.claim_run_job(worker_id=coordinator._worker_id)
        assert job is not None

        await coordinator._execute_claimed_job(job)

        failed = await store.get_run(run["id"])
        assert failed is not None and failed["status"] == "failed"
        events = await store.events_since(run["id"], after_seq=0)
        assert json.loads(events[-1]["payload_json"])["type"] == "ExecutionModelBindingError"
    finally:
        await store.close()


async def test_start_run_rejects_unknown_trusted_snapshot_version(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    coordinator = RunCoordinator(
        store=store,
        checkpointer=None,  # type: ignore[arg-type]
        settings=Settings(SHEJANE_FAKE_LLM=True),
    )
    try:
        with pytest.raises(RunAdmissionError, match="snapshot version is unsupported"):
            await coordinator.start_run(
                principal_id=LOCAL_OWNER_PRINCIPAL_ID,
                command_id="cmd_unknown_snapshot",
                client_message_id="msg_unknown_snapshot",
                protocol_version=1,
                required_capabilities=["agent.run", "agent.stream"],
                goal="inspect",
                settings={"_snapshot_version": 2},
                settings_are_frozen=True,
            )
    finally:
        await store.close()


async def test_worker_rejects_job_model_that_differs_from_run(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    coordinator = RunCoordinator(store=store, checkpointer=None)  # type: ignore[arg-type]
    try:
        run = await _accepted_run(store, "cmd_model_mismatch")
        job = await store.get_active_run_job(run["id"])
        assert job is not None
        payload = json.loads(job["input_json"])
        payload["mode"] = "different-model"
        await store._conn.execute(
            "UPDATE local_run_jobs SET input_json = ? WHERE id = ?",
            (json.dumps(payload), job["id"]),
        )
        await store._conn.commit()
        claimed = await store.claim_run_job(worker_id=coordinator._worker_id)
        assert claimed is not None

        await coordinator._execute_claimed_job(claimed)

        failed = await store.get_run(run["id"])
        assert failed is not None and failed["status"] == "failed"
        events = await store.events_since(run["id"], after_seq=0)
        assert json.loads(events[-1]["payload_json"])["type"] == "ExecutionIdentityError"
    finally:
        await store.close()


async def test_legacy_job_input_is_backfilled_from_its_run_owner(tmp_path: Path) -> None:
    db_path = tmp_path / "local.db"
    store = await LocalStore.open(db_path)
    run = await _accepted_run(store, "cmd_legacy_job")
    job = await store.get_active_run_job(run["id"])
    assert job is not None
    payload = json.loads(job["input_json"])
    payload.pop("principal_id")
    await store._conn.execute(
        "UPDATE local_run_jobs SET input_json = ? WHERE id = ?",
        (json.dumps(payload), job["id"]),
    )
    await store._conn.commit()
    await store.close()

    reopened = await LocalStore.open(db_path)
    try:
        migrated = await reopened.get_active_run_job(run["id"])
        assert migrated is not None
        assert json.loads(migrated["input_json"])["principal_id"] == LOCAL_OWNER_PRINCIPAL_ID
    finally:
        await reopened.close()


async def test_job_with_conflicting_owner_fails_without_running(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    coordinator = RunCoordinator(store=store, checkpointer=None)  # type: ignore[arg-type]
    try:
        run = await _accepted_run(store, "cmd_wrong_owner")
        job = await store.claim_run_job(worker_id=coordinator._worker_id)
        assert job is not None
        payload = json.loads(job["input_json"])
        payload["principal_id"] = "user:attacker"
        job["input_json"] = json.dumps(payload)

        await coordinator._slots.acquire()
        await coordinator._execute_claimed_job(job)

        assert (await store.get_run(run["id"]))["status"] == "failed"
        settled_job = await store.get_run_job(job["id"])
        assert settled_job is not None
        assert settled_job["status"] == "dead"
        events = await store.events_since(run["id"])
        assert events[-1]["event_type"] == "run.failed"
        failure = json.loads(events[-1]["payload_json"])
        assert failure["type"] == "ExecutionIdentityError"
        assert failure["error"]
        assert failure["category"]
    finally:
        await store.close()


async def test_job_fails_if_its_workspace_was_revoked(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    coordinator = RunCoordinator(store=store, checkpointer=None)  # type: ignore[arg-type]
    try:
        workspace = await store.create_workspace(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            path=str(tmp_path),
            label="test",
        )
        run = await _accepted_run(
            store,
            "cmd_revoked_workspace",
            workspace_path=str(tmp_path),
        )
        job = await store.claim_run_job(worker_id=coordinator._worker_id)
        assert job is not None
        assert await store.delete_workspace(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            workspace_id=workspace["id"],
        )

        await coordinator._slots.acquire()
        await coordinator._execute_claimed_job(job)

        assert (await store.get_run(run["id"]))["status"] == "failed"
        settled_job = await store.get_run_job(job["id"])
        assert settled_job is not None
        assert settled_job["status"] == "dead"
        events = await store.events_since(run["id"])
        failure = json.loads(events[-1]["payload_json"])
        assert failure["type"] == "ExecutionWorkspaceError"
    finally:
        await store.close()


async def test_job_rejects_workspace_root_replaced_by_symlink(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    store = await LocalStore.open(tmp_path / "local.db")
    coordinator = RunCoordinator(store=store, checkpointer=None)  # type: ignore[arg-type]
    try:
        await store.create_workspace(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            path=str(root),
            label="test",
        )
        run = await _accepted_run(
            store,
            "cmd_replaced_workspace",
            workspace_path=str(root),
        )
        job = await store.claim_run_job(worker_id=coordinator._worker_id)
        assert job is not None
        root.rmdir()
        root.symlink_to(other, target_is_directory=True)

        await coordinator._slots.acquire()
        await coordinator._execute_claimed_job(job)

        assert (await store.get_run(run["id"]))["status"] == "failed"
        failure = json.loads((await store.events_since(run["id"]))[-1]["payload_json"])
        assert failure["type"] == "ExecutionWorkspaceError"
    finally:
        await store.close()


async def test_job_with_missing_run_is_settled_dead(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    coordinator = RunCoordinator(store=store, checkpointer=None)  # type: ignore[arg-type]
    try:
        run = await _accepted_run(store, "cmd_missing_run")
        job = await store.claim_run_job(worker_id=coordinator._worker_id)
        assert job is not None
        await store._conn.execute("PRAGMA foreign_keys = OFF")
        await store._conn.execute("DELETE FROM local_runs WHERE id = ?", (run["id"],))
        await store._conn.commit()

        await coordinator._slots.acquire()
        await coordinator._execute_claimed_job(job)

        settled_job = await store.get_run_job(job["id"])
        assert settled_job is not None
        assert settled_job["status"] == "dead"
    finally:
        await store.close()


async def test_job_insert_failure_rolls_back_the_command_and_run(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    try:
        await store._conn.execute(
            "CREATE TRIGGER reject_job BEFORE INSERT ON local_run_jobs "
            "BEGIN SELECT RAISE(ABORT, 'injected job failure'); END"
        )
        await store._conn.commit()

        with pytest.raises(Exception, match="injected job failure"):
            await _accepted_run(store, "cmd_rollback")

        assert await store.list_runs(principal_id=LOCAL_OWNER_PRINCIPAL_ID) == []
        assert await store._conn.execute_fetchall("SELECT id FROM local_commands") == []
    finally:
        await store.close()


async def test_concurrent_workers_can_only_lease_a_job_once(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    try:
        run = await _accepted_run(store)
        claims = await asyncio.gather(
            *(store.claim_run_job(worker_id=f"worker-{index}") for index in range(8))
        )
        leased = [claim for claim in claims if claim is not None]

        assert len(leased) == 1
        assert leased[0]["run_id"] == run["id"]
        assert leased[0]["lease_generation"] == 1
        assert leased[0]["attempt"] == 1
        assert (await store.get_run(run["id"]))["status"] == "running"
    finally:
        await store.close()


async def test_canceling_a_pending_job_atomically_cancels_the_run(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    try:
        run = await _accepted_run(store)

        assert await store.request_run_cancel(run["id"]) == "pending"
        assert (await store.get_run(run["id"]))["status"] == "canceled"
        assert await store.get_active_run_job(run["id"]) is None
        events = await store.events_since(run["id"])
        assert [event["event_type"] for event in events] == ["run.canceled"]
    finally:
        await store.close()


async def test_expired_cancel_request_is_quarantined_until_cleanup_is_confirmed(
    tmp_path: Path,
) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    try:
        run = await _accepted_run(store, "cmd_cancel_leased")
        job = await store.claim_run_job(worker_id="worker-old")
        assert job is not None
        assert await store.request_run_cancel(run["id"]) == "leased"
        await store._conn.execute(
            "UPDATE local_run_jobs SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (job["id"],),
        )
        await store._conn.commit()

        assert await store.claim_run_job(worker_id="worker-new") is None
        quarantined = await store._conn.execute_fetchall(
            "SELECT status, quarantined_at FROM local_run_jobs WHERE id = ?", (job["id"],)
        )
        assert quarantined[0][0] == "leased"
        assert quarantined[0][1] is not None
        assert (await store.get_run(run["id"]))["status"] == "cleanup_required"
        events = await store.events_since(run["id"])
        assert [event["event_type"] for event in events] == ["run.cleanup_required"]
    finally:
        await store.close()


async def test_lease_updates_require_the_current_owner_and_generation(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    try:
        await _accepted_run(store)
        job = await store.claim_run_job(worker_id="worker-current")
        assert job is not None

        assert await store.renew_run_job(
            job["id"], lease_owner="worker-old", lease_generation=1
        ) == (False, False)
        assert (
            await store.finish_run_job(
                job["id"],
                lease_owner="worker-old",
                lease_generation=1,
                status="completed",
            )
            is False
        )
        assert await store.renew_run_job(
            job["id"], lease_owner="worker-current", lease_generation=1
        ) == (True, False)
        assert (
            await store.finish_run_job(
                job["id"],
                lease_owner="worker-current",
                lease_generation=1,
                status="completed",
            )
            is True
        )
    finally:
        await store.close()


async def test_heartbeat_does_not_cancel_the_execution_that_settled_its_job(
    tmp_path: Path,
) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    coordinator = RunCoordinator(store=store, checkpointer=None)  # type: ignore[arg-type]
    owner_task = asyncio.create_task(asyncio.sleep(10))
    try:
        run = await _accepted_run(store, "cmd_settled_heartbeat")
        job = await store.claim_run_job(worker_id=coordinator._worker_id)
        assert job is not None
        with store.bind_execution_lease(
            job_id=job["id"],
            run_id=run["id"],
            lease_owner=coordinator._worker_id,
            lease_generation=1,
        ):
            await store.commit_run_result(
                run["id"],
                status="completed",
                event_type="run.completed",
                payload={},
            )

        await coordinator._heartbeat_job(job, owner_task)
        assert not owner_task.cancelled()
        assert not coordinator._lost_leases
    finally:
        owner_task.cancel()
        await asyncio.gather(owner_task, return_exceptions=True)
        await store.close()


@pytest.mark.parametrize(
    ("run_status", "job_status"),
    [
        ("completed", "completed"),
        ("failed", "dead"),
        ("canceled", "canceled"),
        ("waiting_permission", "completed"),
        ("waiting_input", "completed"),
    ],
)
async def test_expired_job_keeps_an_already_committed_run_result(
    tmp_path: Path,
    run_status: str,
    job_status: str,
) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    try:
        run = await _accepted_run(store, f"cmd_settled_{run_status}")
        job = await store.claim_run_job(worker_id="worker-old")
        assert job is not None
        with store.bind_execution_lease(
            job_id=job["id"],
            run_id=run["id"],
            lease_owner="worker-old",
            lease_generation=1,
        ):
            await store.commit_run_result(
                run["id"],
                status=run_status,
                event_type={
                    "completed": "run.completed",
                    "failed": "run.failed",
                    "canceled": "run.canceled",
                    "waiting_permission": "run.waiting",
                    "waiting_input": "run.waiting",
                }[run_status],
                payload={},
            )
        settled_before_expiry = await store._conn.execute_fetchall(
            "SELECT status FROM local_run_jobs WHERE id = ?", (job["id"],)
        )
        assert settled_before_expiry[0][0] == job_status
        await store._conn.execute(
            "UPDATE local_run_jobs SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (job["id"],),
        )
        await store._conn.commit()

        assert await store.claim_run_job(worker_id="worker-new") is None
        settled = await store._conn.execute_fetchall(
            "SELECT status FROM local_run_jobs WHERE id = ?", (job["id"],)
        )
        assert settled[0][0] == job_status
        assert (await store.get_run(run["id"]))["status"] == run_status
        assert len(await store.events_since(run["id"])) == 1
    finally:
        await store.close()


async def test_expired_job_fails_safely_and_fences_old_writes(
    tmp_path: Path,
) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    try:
        run = await _accepted_run(store, "cmd_fence")
        first = await store.claim_run_job(worker_id="worker-old")
        assert first is not None
        with store.bind_execution_lease(
            job_id=first["id"],
            run_id=run["id"],
            lease_owner="worker-old",
            lease_generation=1,
        ):
            await store.append_event(run["id"], "old.before_expiry", {})

        await store._conn.execute(
            "UPDATE local_run_jobs SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (first["id"],),
        )
        await store._conn.commit()
        assert (
            await store.finish_run_job(
                first["id"],
                lease_owner="worker-old",
                lease_generation=1,
                status="completed",
            )
            is False
        )
        assert await store.renew_run_job(
            first["id"],
            lease_owner="worker-old",
            lease_generation=1,
        ) == (False, False)
        assert await store.claim_run_job(worker_id="worker-new") is None
        failed_job = await store._conn.execute_fetchall(
            "SELECT status, quarantined_at FROM local_run_jobs WHERE id = ?", (first["id"],)
        )
        assert failed_job[0][0] == "leased"
        assert failed_job[0][1] is not None
        assert (await store.get_run(run["id"]))["status"] == "cleanup_required"

        with store.bind_execution_lease(
            job_id=first["id"],
            run_id=run["id"],
            lease_owner="worker-old",
            lease_generation=1,
        ):
            with pytest.raises(LeaseFenceError):
                await store.append_event(run["id"], "old.after_reclaim", {})
            with pytest.raises(LeaseFenceError):
                await store.create_artifact(
                    run_id=run["id"],
                    kind="progress",
                    title="stale",
                    content="must not persist",
                )
        assert await store.list_artifacts_for_run(run["id"]) == []
    finally:
        await store.close()


async def test_checkpointer_writes_are_fenced_by_the_same_job_lease(tmp_path: Path) -> None:
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    class RecordingCheckpointer:
        serde = JsonPlusSerializer()

        def __init__(self) -> None:
            self.writes = 0

        @property
        def config_specs(self) -> list:
            return []

        async def aput_writes(self, *_args: Any) -> None:
            self.writes += 1

    store = await LocalStore.open(tmp_path / "local.db")
    try:
        run = await _accepted_run(store, "cmd_checkpoint_fence")
        job = await store.claim_run_job(worker_id="worker-old")
        assert job is not None
        delegate = RecordingCheckpointer()
        with store.bind_execution_lease(
            job_id=job["id"],
            run_id=run["id"],
            lease_owner="worker-old",
            lease_generation=1,
        ) as lease:
            checkpointer = FencedCheckpointer(delegate, store, lease)  # type: ignore[arg-type]
            await checkpointer.aput_writes(
                {"configurable": {"thread_id": run["id"], "checkpoint_id": "cp_1"}},
                [("messages", "ok")],
                "task_1",
            )
        assert delegate.writes == 1

        await store._conn.execute(
            "UPDATE local_run_jobs SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (job["id"],),
        )
        await store._conn.commit()
        assert await store.claim_run_job(worker_id="worker-new") is None

        with pytest.raises(LeaseFenceError):
            await checkpointer.aput_writes(
                {"configurable": {"thread_id": run["id"], "checkpoint_id": "cp_2"}},
                [("messages", "stale")],
                "task_2",
            )
        assert delegate.writes == 1
    finally:
        await store.close()


async def test_checkpoint_commit_finishes_before_an_expired_job_is_failed(
    tmp_path: Path,
) -> None:
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingCheckpointer:
        serde = JsonPlusSerializer()

        @property
        def config_specs(self) -> list:
            return []

        async def aput_writes(self, *_args: Any) -> None:
            entered.set()
            await release.wait()

    store = await LocalStore.open(tmp_path / "local.db")
    try:
        run = await _accepted_run(store, "cmd_checkpoint_reclaim")
        job = await store.claim_run_job(worker_id="worker-old", lease_seconds=0.1)
        assert job is not None
        with store.bind_execution_lease(
            job_id=job["id"],
            run_id=run["id"],
            lease_owner="worker-old",
            lease_generation=1,
        ) as lease:
            checkpointer = FencedCheckpointer(  # type: ignore[arg-type]
                BlockingCheckpointer(), store, lease
            )
            checkpoint_write = asyncio.create_task(
                checkpointer.aput_writes(
                    {"configurable": {"thread_id": run["id"], "checkpoint_id": "cp_1"}},
                    [("messages", "committing")],
                    "task_1",
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=1)
            await asyncio.sleep(0.15)
            reclaim = asyncio.create_task(store.claim_run_job(worker_id="worker-new"))
            await asyncio.sleep(0.05)
            assert not reclaim.done()

            release.set()
            await asyncio.wait_for(checkpoint_write, timeout=1)
            reclaimed = await asyncio.wait_for(reclaim, timeout=1)

        assert reclaimed is None
        failed_job = await store._conn.execute_fetchall(
            "SELECT status, quarantined_at FROM local_run_jobs WHERE id = ?", (job["id"],)
        )
        assert failed_job[0][0] == "leased"
        assert failed_job[0][1] is not None
    finally:
        release.set()
        await store.close()


async def test_graph_definition_and_branch_head_are_compare_and_swap(tmp_path: Path) -> None:
    from shejane_runtime.store.sqlite import GraphDefinitionMismatchError, GraphHeadConflictError

    store = await LocalStore.open(tmp_path / "local.db")
    try:
        run = await _accepted_run(store, "cmd_graph_head")
        await store.bind_graph_definition(run["id"], "definition-a")
        await store.bind_graph_definition(run["id"], "definition-a")
        with pytest.raises(GraphDefinitionMismatchError):
            await store.bind_graph_definition(run["id"], "definition-b")

        await store.advance_graph_checkpoint(
            run["id"],
            graph_thread_id=run["graph_thread_id"],
            expected_checkpoint_id=None,
            checkpoint_id="checkpoint-a",
        )
        with pytest.raises(GraphHeadConflictError):
            await store.advance_graph_checkpoint(
                run["id"],
                graph_thread_id=run["graph_thread_id"],
                expected_checkpoint_id=None,
                checkpoint_id="checkpoint-b",
            )
    finally:
        await store.close()


async def test_graph_head_reconciles_checkpoint_committed_before_product_cas(
    tmp_path: Path,
) -> None:
    from typing import TypedDict

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.graph import END, START, StateGraph

    class State(TypedDict):
        value: int

    store = await LocalStore.open(tmp_path / "local.db")
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "agent.db")) as checkpointer:
        await checkpointer.setup()
        graph = (
            StateGraph(State)
            .add_node("increment", lambda state: {"value": state["value"] + 1})
            .add_edge(START, "increment")
            .add_edge("increment", END)
            .compile(checkpointer=checkpointer)
        )
        try:
            run = await _accepted_run(store, "cmd_reconcile_head")
            await graph.ainvoke(
                {"value": 1},
                {
                    "configurable": {
                        "thread_id": run["graph_thread_id"],
                        "runtime_run_id": run["id"],
                        "runtime_attempt_id": "job:1",
                    }
                },
                durability="sync",
            )
            assert (await store.get_run(run["id"]))["graph_checkpoint_id"] is None

            coordinators = [
                RunCoordinator(store=store, checkpointer=checkpointer),
                RunCoordinator(store=store, checkpointer=checkpointer),
            ]
            reconciled = await asyncio.gather(
                *(coordinator._reconcile_graph_head(run) for coordinator in coordinators)
            )
            assert reconciled[0]["graph_checkpoint_id"] is not None
            assert reconciled[0]["graph_checkpoint_id"] == reconciled[1]["graph_checkpoint_id"]
            assert (await store.get_run(run["id"]))["graph_checkpoint_id"] == reconciled[0][
                "graph_checkpoint_id"
            ]
        finally:
            await store.close()


async def test_dispatcher_acquires_a_slot_before_leasing_the_next_job(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "local.db")
    gate = asyncio.Event()
    coordinator = RunCoordinator(
        store=store,
        checkpointer=None,  # type: ignore[arg-type]
        max_concurrent_runs=1,
    )

    async def blocked_drive(**kwargs: Any) -> RunOutcome:
        await gate.wait()
        await store.update_assistant_draft(
            run_id=kwargs["run_id"],
            message_key=f"answer-{kwargs['run_id']}",
            content="done",
            tool_calls=[],
        )
        return RunOutcome("completed", "run.completed", {})

    coordinator._drive_run = blocked_drive  # type: ignore[method-assign]
    try:
        first_run = await _accepted_run(store, "cmd_slot_1")
        permission = await store.create_permission(
            run_id=first_run["id"],
            tool_call_id="call_1",
            tool_name="write_file",
            arguments={},
            risk="workspace_write",
        )
        await store.resolve_permission(permission["id"], status="approved", scope="run")
        await _accepted_run(store, "cmd_slot_2")
        coordinator.start()

        for _ in range(100):
            rows = await (
                await store._conn.execute(
                    "SELECT status, COUNT(*) FROM local_run_jobs GROUP BY status"
                )
            ).fetchall()
            counts = {row[0]: row[1] for row in rows}
            if counts.get("leased") == 1:
                break
            await asyncio.sleep(0.01)

        assert counts == {"leased": 1, "pending": 1}
        # Permission grants are durable, parameter-bound store records; the
        # coordinator must not rebuild an in-memory tool-name allowlist.
        assert not hasattr(coordinator, "_run_grants")
        gate.set()
        for _ in range(100):
            if not await store._conn.execute_fetchall(
                "SELECT id FROM local_run_jobs WHERE status IN ('pending', 'leased')"
            ):
                break
            await asyncio.sleep(0.01)
        finished = await store._conn.execute_fetchall(
            "SELECT status FROM local_run_jobs ORDER BY created_at, id"
        )
        assert [row[0] for row in finished] == ["completed", "completed"]
    finally:
        gate.set()
        await coordinator.stop()
        await store.close()
