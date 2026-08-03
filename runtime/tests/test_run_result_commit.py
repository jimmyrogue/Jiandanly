from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from shejane_runtime.auth import LOCAL_OWNER_PRINCIPAL_ID
from shejane_runtime.store.sqlite import LeaseFenceError, LocalStore, RunResultConflictError


async def _open_store(tmp_path: Path) -> LocalStore:
    return await LocalStore.open(tmp_path / "local.db")


async def test_commit_run_result_writes_status_and_event_together(tmp_path: Path) -> None:
    store = await _open_store(tmp_path)
    try:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="finish atomically",
            workspace_path=None,
        )

        event, created = await store.commit_run_result(
            run["id"],
            status="completed",
            event_type="run.completed",
            payload={"final_text": "done"},
            orphan_recovery=True,
        )

        saved = await store.get_run(run["id"])
        assert saved is not None
        assert saved["status"] == "completed"
        assert saved["completed_at"] == event["created_at"]
        assert created is True
        assert [item["event_type"] for item in await store.events_since(run["id"])] == [
            "run.completed"
        ]
    finally:
        await store.close()


async def test_result_transaction_finalizes_runtime_thread_projection(tmp_path: Path) -> None:
    store = await _open_store(tmp_path)
    try:
        run, created = await store.accept_run_command(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_projection",
            client_message_id="msg_user_projection",
            thread_id="thread_projection",
            assistant_message_id="msg_assistant_projection",
            user_input="visible question",
            thread_title="Visible thread",
            thread_metadata={"pinned": True},
            user_item_metadata={"attachments": [{"name": "brief.pdf"}]},
            command_payload={"type": "run.start", "goal": "internal directives\nfinish"},
            goal="internal directives\nfinish",
            workspace_path=None,
            mode="auto",
        )
        assert created is True
        await store.update_assistant_draft(
            run_id=run["id"],
            message_key="assistant-v1",
            content="authoritative answer",
            tool_calls=[],
        )

        job = await store.claim_run_job(worker_id="worker-projection")
        assert job is not None
        with store.bind_execution_lease(
            job_id=job["id"],
            run_id=run["id"],
            lease_owner="worker-projection",
            lease_generation=int(job["lease_generation"]),
        ):
            await store.commit_run_result(
                run["id"],
                status="completed",
                event_type="run.completed",
                payload={
                    "final_text": "authoritative answer",
                    "thread_title": "Generated title",
                    "thread_title_seed": "Visible thread",
                },
            )

        thread = await store._conn.execute_fetchall(
            "SELECT principal_id, title, metadata_json, version "
            "FROM local_threads WHERE id = 'thread_projection'"
        )
        items = await store._conn.execute_fetchall(
            "SELECT item_type, status, content, client_id, version "
            "FROM local_thread_items WHERE thread_id = 'thread_projection' "
            "ORDER BY position, id"
        )
        changes = await store._conn.execute_fetchall(
            "SELECT cursor, thread_version, change_type FROM local_thread_changes "
            "WHERE thread_id = 'thread_projection' ORDER BY cursor"
        )
        assert [tuple(row) for row in thread] == [
            (LOCAL_OWNER_PRINCIPAL_ID, "Generated title", '{"pinned":true}', 2)
        ]
        assert [tuple(item) for item in items] == [
            ("user_message", "completed", "visible question", "msg_user_projection", 1),
            (
                "assistant_message",
                "completed",
                "authoritative answer",
                "msg_assistant_projection",
                2,
            ),
        ]
        assert [(row[1], row[2]) for row in changes] == [
            (1, "turn.started"),
            (2, "run.completed"),
        ]
        assert changes[0][0] < changes[1][0]
    finally:
        await store.close()


async def test_waiting_projection_records_the_event_watermark_covered_by_its_draft(
    tmp_path: Path,
) -> None:
    store = await _open_store(tmp_path)
    try:
        run, _created = await store.accept_run_command(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_waiting_projection",
            client_message_id="msg_waiting_user",
            assistant_message_id="msg_waiting_assistant",
            thread_id="thread_waiting_projection",
            user_input="ask before continuing",
            command_payload={"type": "run.start"},
            goal="ask before continuing",
            workspace_path=None,
            mode="auto",
        )
        await store.append_event(run["id"], "llm.delta", {"content": "draft answer"})
        await store.update_assistant_draft(
            run_id=run["id"],
            message_key="assistant-waiting-v1",
            content="draft answer",
            tool_calls=[],
        )
        job = await store.claim_run_job(worker_id="worker-waiting-projection")
        assert job is not None
        with store.bind_execution_lease(
            job_id=job["id"],
            run_id=run["id"],
            lease_owner="worker-waiting-projection",
            lease_generation=int(job["lease_generation"]),
        ):
            wait_event, created = await store.commit_run_result(
                run["id"],
                status="waiting_input",
                event_type="run.waiting",
                payload={"question_id": "question-1"},
            )

        assert created is True
        snapshot = await store.get_thread_snapshot(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            thread_id="thread_waiting_projection",
        )
        assert snapshot is not None
        assistant = next(
            item
            for item in snapshot["items"]
            if item["run_id"] == run["id"] and item["item_type"] == "assistant_message"
        )
        assert assistant["content"] == "draft answer"
        assert snapshot["event_high_watermarks"] == {run["id"]: wait_event["seq"]}
        assert [
            event["event_type"]
            for event in await store.events_since(
                run["id"],
                after_seq=snapshot["event_high_watermarks"][run["id"]],
            )
        ] == []
    finally:
        await store.close()


async def test_open_backfills_resumed_projection_watermark_from_legacy_events(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy-waiting-watermark.db"
    store = await LocalStore.open(db_path)
    run, _created = await store.accept_run_command(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        command_id="cmd_legacy_waiting_projection",
        client_message_id="msg_legacy_waiting_user",
        assistant_message_id="msg_legacy_waiting_assistant",
        thread_id="thread_legacy_waiting_projection",
        user_input="legacy question",
        command_payload={"type": "run.start"},
        goal="legacy question",
        workspace_path=None,
        mode="auto",
    )
    await store.append_event(run["id"], "llm.delta", {"content": "legacy draft"})
    await store.update_assistant_draft(
        run_id=run["id"],
        message_key="assistant-legacy-waiting-v1",
        content="legacy draft",
        tool_calls=[],
    )
    job = await store.claim_run_job(worker_id="worker-legacy-waiting")
    assert job is not None
    with store.bind_execution_lease(
        job_id=job["id"],
        run_id=run["id"],
        lease_owner="worker-legacy-waiting",
        lease_generation=int(job["lease_generation"]),
    ):
        wait_event, _created = await store.commit_run_result(
            run["id"],
            status="waiting_input",
            event_type="run.waiting",
            payload={"question_id": "legacy-question-1"},
        )
    await store._conn.execute(
        "UPDATE local_runs SET status = 'running' WHERE id = ?",
        (run["id"],),
    )
    await store._conn.commit()
    await store.close()

    import aiosqlite

    conn = await aiosqlite.connect(str(db_path))
    await conn.execute("ALTER TABLE local_thread_items DROP COLUMN event_high_watermark")
    await conn.commit()
    await conn.close()

    reopened = await LocalStore.open(db_path)
    try:
        snapshot = await reopened.get_thread_snapshot(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            thread_id="thread_legacy_waiting_projection",
        )
        assert snapshot is not None
        assistant = next(
            item
            for item in snapshot["items"]
            if item["run_id"] == run["id"] and item["item_type"] == "assistant_message"
        )
        assert assistant["content"] == "legacy draft"
        assert snapshot["event_high_watermarks"] == {run["id"]: wait_event["seq"]}
        assert (
            await reopened.events_since(
                run["id"],
                after_seq=snapshot["event_high_watermarks"][run["id"]],
            )
            == []
        )
    finally:
        await reopened.close()


async def test_runtime_thread_pages_are_bounded_and_version_consistent(tmp_path: Path) -> None:
    store = await _open_store(tmp_path)
    try:
        admitted_runs = []
        for index in range(2):
            run, _created = await store.accept_run_command(
                principal_id=LOCAL_OWNER_PRINCIPAL_ID,
                command_id=f"cmd_page_{index}",
                client_message_id=f"msg_page_user_{index}",
                assistant_message_id=f"msg_page_assistant_{index}",
                thread_id="thread_page",
                user_input=f"question {index}",
                command_payload={"type": "run.start", "index": index},
                goal=f"question {index}",
                workspace_path=None,
                mode="auto",
            )
            admitted_runs.append(run)
            await store.append_event(run["id"], "model.delta", {"index": index})
            job = await store.claim_run_job(worker_id="worker-page")
            assert job is not None
            with store.bind_execution_lease(
                job_id=job["id"],
                run_id=run["id"],
                lease_owner="worker-page",
                lease_generation=int(job["lease_generation"]),
            ):
                await store.commit_run_result(
                    run["id"],
                    status="completed",
                    event_type="run.completed",
                    payload={"final_text": f"answer {index}"},
                )

        newest = await store.get_thread_snapshot(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            thread_id="thread_page",
            item_limit=2,
            event_limit=1,
        )
        assert newest is not None
        assert [item["position"] for item in newest["items"]] == [3, 4]
        assert newest["has_more_items"] is True
        assert newest["next_before_position"] == 3
        assert newest["events_truncated"] is True
        assert newest["event_high_watermarks"][run["id"]] == newest["events"][0]["seq"]
        assert [
            event["event_type"]
            for event in await store.events_since(
                run["id"],
                after_seq=newest["event_high_watermarks"][run["id"]],
            )
        ] == ["run.completed"]

        oldest = await store.get_thread_snapshot(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            thread_id="thread_page",
            before_position=3,
            item_limit=2,
            expected_version=int(newest["thread"]["version"]),
        )
        assert oldest is not None
        assert [item["position"] for item in oldest["items"]] == [1, 2]
        assert oldest["has_more_items"] is False

        odd_page = await store.get_thread_snapshot(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            thread_id="thread_page",
            item_limit=3,
        )
        assert odd_page is not None
        assert [item["position"] for item in odd_page["items"]] == [2, 3, 4]
        user_only_page = await store.get_thread_snapshot(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            thread_id="thread_page",
            before_position=2,
            item_limit=2,
            expected_version=int(odd_page["thread"]["version"]),
        )
        assert user_only_page is not None
        assert [item["position"] for item in user_only_page["items"]] == [1]
        assert [run["id"] for run in user_only_page["runs"]] == [admitted_runs[0]["id"]]

        await store.update_thread(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            thread_id="thread_page",
            title="changed",
            metadata=None,
            archived=None,
        )
        with pytest.raises(RunResultConflictError, match="changed while reading"):
            await store.get_thread_snapshot(
                principal_id=LOCAL_OWNER_PRINCIPAL_ID,
                thread_id="thread_page",
                before_position=3,
                expected_version=int(newest["thread"]["version"]),
            )
    finally:
        await store.close()


async def test_runtime_imports_legacy_history_once_then_owns_context(tmp_path: Path) -> None:
    store = await _open_store(tmp_path)
    try:
        first, _created = await store.accept_run_command(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_history_first",
            client_message_id="msg_history_first",
            assistant_message_id="assistant_history_first",
            thread_id="thread_history",
            user_input="current question",
            history=[
                {"role": "user", "content": "legacy question"},
                {"role": "assistant", "content": "legacy answer"},
            ],
            command_payload={"type": "run.start", "turn": 1},
            goal="current question",
            workspace_path=None,
            mode="auto",
        )
        assert json.loads(first["history_json"]) == [
            {"role": "user", "content": "legacy question"},
            {"role": "assistant", "content": "legacy answer"},
        ]
        job = await store.claim_run_job(worker_id="worker-history")
        assert job is not None
        with store.bind_execution_lease(
            job_id=job["id"],
            run_id=first["id"],
            lease_owner="worker-history",
            lease_generation=int(job["lease_generation"]),
        ):
            await store.commit_run_result(
                first["id"],
                status="completed",
                event_type="run.completed",
                payload={"final_text": "current answer"},
            )

        second, _created = await store.accept_run_command(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_history_second",
            client_message_id="msg_history_second",
            assistant_message_id="assistant_history_second",
            thread_id="thread_history",
            user_input="follow up",
            history=[{"role": "user", "content": "client supplied stale history"}],
            command_payload={"type": "run.start", "turn": 2},
            goal="follow up",
            workspace_path=None,
            mode="auto",
        )
        assert [
            item["content"]
            for item in (
                await store.get_thread_snapshot(
                    principal_id=LOCAL_OWNER_PRINCIPAL_ID,
                    thread_id="thread_history",
                )
            )["items"]
        ] == [
            "legacy question",
            "legacy answer",
            "current question",
            "current answer",
            "follow up",
            "",
        ]
        assert "client supplied stale history" not in second["history_json"]
        assert "current answer" in second["history_json"]
    finally:
        await store.close()


async def test_projection_failure_rolls_back_run_event_message_and_version(tmp_path: Path) -> None:
    store = await _open_store(tmp_path)
    try:
        run, _created = await store.accept_run_command(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_projection_rollback",
            client_message_id="msg_projection_rollback",
            thread_id="thread_projection_rollback",
            command_payload={"type": "run.start", "goal": "finish"},
            goal="finish",
            workspace_path=None,
            mode="auto",
        )
        await store._conn.execute(
            "CREATE TRIGGER reject_projection_change BEFORE INSERT ON local_thread_changes "
            "WHEN NEW.change_type = 'run.completed' "
            "BEGIN SELECT RAISE(ABORT, 'injected projection failure'); END"
        )
        await store._conn.commit()
        job = await store.claim_run_job(worker_id="worker-projection-rollback")
        assert job is not None

        with store.bind_execution_lease(
            job_id=job["id"],
            run_id=run["id"],
            lease_owner="worker-projection-rollback",
            lease_generation=int(job["lease_generation"]),
        ):
            with pytest.raises(Exception, match="injected projection failure"):
                await store.commit_run_result(
                    run["id"],
                    status="completed",
                    event_type="run.completed",
                    payload={"final_text": "must roll back"},
                )

        saved = await store.get_run(run["id"])
        assistant = await store._conn.execute_fetchall(
            "SELECT status, content, version FROM local_thread_items WHERE id = ?",
            (run["assistant_item_id"],),
        )
        thread = await store._conn.execute_fetchall(
            "SELECT version FROM local_threads WHERE id = 'thread_projection_rollback'"
        )
        changes = await store._conn.execute_fetchall(
            "SELECT change_type FROM local_thread_changes "
            "WHERE thread_id = 'thread_projection_rollback'"
        )
        assert saved is not None and saved["status"] == "running"
        assert await store.events_since(run["id"]) == []
        assert [tuple(row) for row in assistant] == [("in_progress", "", 1)]
        assert [tuple(row) for row in thread] == [(1,)]
        assert [tuple(row) for row in changes] == [("turn.started",)]
    finally:
        await store.close()


async def test_thread_metadata_and_delete_are_versioned_runtime_changes(tmp_path: Path) -> None:
    store = await _open_store(tmp_path)
    try:
        run, _created = await store.accept_run_command(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_thread_lifecycle",
            client_message_id="msg_thread_lifecycle",
            thread_id="thread_lifecycle",
            command_payload={"type": "run.start", "goal": "finish"},
            goal="finish",
            workspace_path=None,
            mode="auto",
        )
        job = await store.claim_run_job(worker_id="worker-thread-lifecycle")
        assert job is not None
        with store.bind_execution_lease(
            job_id=job["id"],
            run_id=run["id"],
            lease_owner="worker-thread-lifecycle",
            lease_generation=int(job["lease_generation"]),
        ):
            await store.commit_run_result(
                run["id"],
                status="completed",
                event_type="run.completed",
                payload={"final_text": "done"},
            )

        updated = await store.update_thread(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            thread_id="thread_lifecycle",
            title="Renamed",
            metadata={"pinned": True},
            archived=False,
        )
        assert updated is not None
        assert updated["version"] == 3
        version = await store.delete_thread(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            thread_id="thread_lifecycle",
        )
        assert version == 4
        threads, cursor, has_more = await store.list_threads(principal_id=LOCAL_OWNER_PRINCIPAL_ID)
        changes, next_cursor = await store.thread_changes_since(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            after_cursor=0,
        )
        assert threads == []
        assert has_more is False
        assert (
            await store.get_thread_snapshot(
                principal_id=LOCAL_OWNER_PRINCIPAL_ID,
                thread_id="thread_lifecycle",
            )
            is None
        )
        assert [change["change_type"] for change in changes] == [
            "turn.started",
            "run.completed",
            "thread.updated",
            "thread.deleted",
        ]
        assert cursor == next_cursor == changes[-1]["cursor"]
    finally:
        await store.close()


async def test_thread_delete_rejects_unsettled_run(tmp_path: Path) -> None:
    store = await _open_store(tmp_path)
    try:
        await store.accept_run_command(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_thread_active",
            client_message_id="msg_thread_active",
            thread_id="thread_active",
            command_payload={"type": "run.start", "goal": "running"},
            goal="running",
            workspace_path=None,
            mode="auto",
        )
        with pytest.raises(RunResultConflictError, match="unsettled run"):
            await store.delete_thread(
                principal_id=LOCAL_OWNER_PRINCIPAL_ID,
                thread_id="thread_active",
            )
    finally:
        await store.close()


async def test_thread_rewrite_keeps_history_immutable_and_projects_only_new_branch(
    tmp_path: Path,
) -> None:
    store = await _open_store(tmp_path)
    try:
        first, _created = await store.accept_run_command(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_branch_first",
            client_message_id="msg_branch_first",
            assistant_message_id="assistant_branch_first",
            thread_id="thread_branch",
            user_input="original question",
            command_payload={"type": "run.start", "goal": "original question"},
            goal="original question",
            workspace_path=None,
            mode="auto",
        )
        first_job = await store.claim_run_job(worker_id="worker-branch")
        assert first_job is not None
        with store.bind_execution_lease(
            job_id=first_job["id"],
            run_id=first["id"],
            lease_owner="worker-branch",
            lease_generation=int(first_job["lease_generation"]),
        ):
            await store.commit_run_result(
                first["id"],
                status="completed",
                event_type="run.completed",
                payload={"final_text": "old answer"},
            )

        second, _created = await store.accept_run_command(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_branch_second",
            client_message_id="msg_branch_second",
            assistant_message_id="assistant_branch_second",
            thread_id="thread_branch",
            user_input="edited question",
            replace_from_client_id="msg_branch_first",
            command_payload={"type": "run.start", "goal": "edited question"},
            goal="edited question",
            workspace_path=None,
            mode="auto",
        )

        snapshot = await store.get_thread_snapshot(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            thread_id="thread_branch",
        )
        assert snapshot is not None
        assert [item["client_id"] for item in snapshot["items"]] == [
            "msg_branch_second",
            "assistant_branch_second",
        ]
        assert [run["id"] for run in snapshot["runs"]] == [second["id"]]
        old_items = await store._conn.execute_fetchall(
            "SELECT client_id, superseded_by_run_id FROM local_thread_items "
            "WHERE run_id = ? ORDER BY position",
            (first["id"],),
        )
        assert [tuple(row) for row in old_items] == [
            ("msg_branch_first", second["id"]),
            ("assistant_branch_first", second["id"]),
        ]
        changes, _cursor = await store.thread_changes_since(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            after_cursor=0,
        )
        assert changes[-1]["change_type"] == "thread.rewritten"
    finally:
        await store.close()


async def test_commit_run_result_is_idempotent_for_the_same_result(tmp_path: Path) -> None:
    store = await _open_store(tmp_path)
    try:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="retry safely",
            workspace_path=None,
        )
        first, first_created = await store.commit_run_result(
            run["id"],
            status="failed",
            event_type="run.failed",
            payload={"error": "provider unavailable", "retryable": True},
            orphan_recovery=True,
        )
        replay, replay_created = await store.commit_run_result(
            run["id"],
            status="failed",
            event_type="run.failed",
            payload={"retryable": True, "error": "provider unavailable"},
            orphan_recovery=True,
        )

        assert first_created is True
        assert replay_created is False
        assert replay["id"] == first["id"]
        assert len(await store.events_since(run["id"])) == 1
    finally:
        await store.close()


async def test_commit_run_result_rejects_a_conflicting_terminal_result(tmp_path: Path) -> None:
    store = await _open_store(tmp_path)
    try:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="one outcome",
            workspace_path=None,
        )
        await store.commit_run_result(
            run["id"],
            status="completed",
            event_type="run.completed",
            payload={"final_text": "done"},
            orphan_recovery=True,
        )

        with pytest.raises(RunResultConflictError):
            await store.commit_run_result(
                run["id"],
                status="failed",
                event_type="run.failed",
                payload={"error": "too late"},
                orphan_recovery=True,
            )

        assert (await store.get_run(run["id"]))["status"] == "completed"
        assert len(await store.events_since(run["id"])) == 1
    finally:
        await store.close()


async def test_commit_run_result_rolls_back_status_when_event_insert_fails(
    tmp_path: Path,
) -> None:
    store = await _open_store(tmp_path)
    try:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="rollback",
            workspace_path=None,
        )
        await store._conn.execute(
            "CREATE TRIGGER reject_terminal_event BEFORE INSERT ON local_events "
            "WHEN NEW.event_type = 'run.completed' "
            "BEGIN SELECT RAISE(ABORT, 'injected event failure'); END"
        )
        await store._conn.commit()

        with pytest.raises(Exception, match="injected event failure"):
            await store.commit_run_result(
                run["id"],
                status="completed",
                event_type="run.completed",
                payload={"final_text": "must not persist"},
                orphan_recovery=True,
            )

        saved = await store.get_run(run["id"])
        assert saved is not None
        assert saved["status"] == "queued"
        assert saved["completed_at"] is None
        assert await store.events_since(run["id"]) == []
    finally:
        await store.close()


async def test_active_job_result_requires_its_execution_lease(tmp_path: Path) -> None:
    store = await _open_store(tmp_path)
    try:
        run, _created = await store.accept_run_command(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_requires_lease",
            client_message_id="msg_requires_lease",
            thread_id="thread_requires_lease",
            command_payload={"type": "run.start", "goal": "finish"},
            goal="finish",
            workspace_path=None,
            mode="auto",
        )

        with pytest.raises(LeaseFenceError, match="active execution lease"):
            await store.commit_run_result(
                run["id"],
                status="completed",
                event_type="run.completed",
                payload={"final_text": "must not persist"},
            )

        saved = await store.get_run(run["id"])
        jobs = await store._conn.execute_fetchall(
            "SELECT status FROM local_run_jobs WHERE run_id = ?", (run["id"],)
        )
        assert saved is not None and saved["status"] == "queued"
        assert [row["status"] for row in jobs] == ["pending"]
        assert await store.events_since(run["id"]) == []

        await store._conn.execute(
            "UPDATE local_run_jobs SET quarantined_at = datetime('now') WHERE run_id = ?",
            (run["id"],),
        )
        await store._conn.commit()
        with pytest.raises(LeaseFenceError, match="unsettled execution job"):
            await store.commit_run_result(
                run["id"],
                status="failed",
                event_type="run.failed",
                payload={"error": "must remain quarantined"},
                orphan_recovery=True,
            )
        assert (await store.get_run(run["id"]))["status"] == "queued"
        assert await store.events_since(run["id"]) == []
    finally:
        await store.close()


async def test_no_job_result_still_requires_explicit_orphan_recovery(tmp_path: Path) -> None:
    store = await _open_store(tmp_path)
    try:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="not implicitly recoverable",
            workspace_path=None,
        )

        with pytest.raises(LeaseFenceError, match="active execution lease"):
            await store.commit_run_result(
                run["id"],
                status="failed",
                event_type="run.failed",
                payload={"error": "must not persist"},
            )

        assert (await store.get_run(run["id"]))["status"] == "queued"
        assert await store.events_since(run["id"]) == []
    finally:
        await store.close()


async def test_execution_lease_cannot_commit_a_different_run(tmp_path: Path) -> None:
    store = await _open_store(tmp_path)
    try:
        first = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="first",
            workspace_path=None,
        )
        second, _created = await store.accept_run_command(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_other_lease",
            client_message_id="msg_other_lease",
            thread_id="thread_other_lease",
            command_payload={"type": "run.start", "goal": "second"},
            goal="second",
            workspace_path=None,
            mode="auto",
        )
        job = await store.claim_run_job(worker_id="worker-other-lease")
        assert job is not None and job["run_id"] == second["id"]

        with store.bind_execution_lease(
            job_id=job["id"],
            run_id=second["id"],
            lease_owner="worker-other-lease",
            lease_generation=int(job["lease_generation"]),
        ):
            with pytest.raises(LeaseFenceError, match="cannot write run"):
                await store.commit_run_result(
                    first["id"],
                    status="completed",
                    event_type="run.completed",
                    payload={"final_text": "must not persist"},
                )

        assert (await store.get_run(first["id"]))["status"] == "queued"
        assert (await store.get_run(second["id"]))["status"] == "running"
        assert await store.events_since(first["id"]) == []
        jobs = await store._conn.execute_fetchall(
            "SELECT status FROM local_run_jobs WHERE id = ?", (job["id"],)
        )
        assert [row["status"] for row in jobs] == ["leased"]
    finally:
        await store.close()


async def test_expired_execution_lease_cannot_commit_a_result(tmp_path: Path) -> None:
    store = await _open_store(tmp_path)
    try:
        run, _created = await store.accept_run_command(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_expired_lease",
            client_message_id="msg_expired_lease",
            thread_id="thread_expired_lease",
            command_payload={"type": "run.start", "goal": "finish"},
            goal="finish",
            workspace_path=None,
            mode="auto",
        )
        job = await store.claim_run_job(worker_id="worker-expired", lease_seconds=-1)
        assert job is not None

        with store.bind_execution_lease(
            job_id=job["id"],
            run_id=run["id"],
            lease_owner="worker-expired",
            lease_generation=int(job["lease_generation"]),
        ):
            with pytest.raises(LeaseFenceError, match="stale"):
                await store.commit_run_result(
                    run["id"],
                    status="completed",
                    event_type="run.completed",
                    payload={"final_text": "must not persist"},
                )

        assert (await store.get_run(run["id"]))["status"] == "running"
        assert await store.events_since(run["id"]) == []
        jobs = await store._conn.execute_fetchall(
            "SELECT status FROM local_run_jobs WHERE id = ?", (job["id"],)
        )
        assert [row["status"] for row in jobs] == ["leased"]
    finally:
        await store.close()


async def test_lease_expiring_during_result_commit_rolls_back_every_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = await _open_store(tmp_path)
    try:
        run, _created = await store.accept_run_command(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            command_id="cmd_mid_commit_expiry",
            client_message_id="msg_mid_commit_expiry",
            assistant_message_id="assistant_mid_commit_expiry",
            thread_id="thread_mid_commit_expiry",
            command_payload={"type": "run.start", "goal": "finish"},
            goal="finish",
            workspace_path=None,
            mode="auto",
        )
        job = await store.claim_run_job(worker_id="worker-mid-commit-expiry")
        assert job is not None
        expires_at = (datetime.now(UTC) + timedelta(milliseconds=100)).isoformat()
        await store._conn.execute(
            "UPDATE local_run_jobs SET lease_expires_at = ? WHERE id = ?",
            (expires_at, job["id"]),
        )
        await store._conn.commit()

        before_events = await store.events_since(run["id"])
        before_items = await store._conn.execute_fetchall(
            "SELECT status, content, version FROM local_thread_items "
            "WHERE thread_id = ? ORDER BY position, id",
            (run["thread_id"],),
        )
        before_thread = await store._conn.execute_fetchall(
            "SELECT version FROM local_threads WHERE id = ?", (run["thread_id"],)
        )
        before_changes = await store._conn.execute_fetchall(
            "SELECT change_type FROM local_thread_changes WHERE thread_id = ? ORDER BY cursor",
            (run["thread_id"],),
        )
        original_projection = LocalStore._update_thread_projection_uncommitted

        async def delayed_projection(*args, **kwargs) -> None:
            await asyncio.sleep(0.2)
            await original_projection(*args, **kwargs)

        monkeypatch.setattr(
            LocalStore,
            "_update_thread_projection_uncommitted",
            staticmethod(delayed_projection),
        )

        with store.bind_execution_lease(
            job_id=job["id"],
            run_id=run["id"],
            lease_owner="worker-mid-commit-expiry",
            lease_generation=int(job["lease_generation"]),
        ):
            with pytest.raises(LeaseFenceError, match="stale"):
                await store.commit_run_result(
                    run["id"],
                    status="completed",
                    event_type="run.completed",
                    payload={"final_text": "must roll back"},
                )

        assert (await store.get_run(run["id"]))["status"] == "running"
        assert await store.events_since(run["id"]) == before_events
        assert (
            await store._conn.execute_fetchall(
                "SELECT status, content, version FROM local_thread_items "
                "WHERE thread_id = ? ORDER BY position, id",
                (run["thread_id"],),
            )
            == before_items
        )
        assert (
            await store._conn.execute_fetchall(
                "SELECT version FROM local_threads WHERE id = ?", (run["thread_id"],)
            )
            == before_thread
        )
        assert (
            await store._conn.execute_fetchall(
                "SELECT change_type FROM local_thread_changes WHERE thread_id = ? ORDER BY cursor",
                (run["thread_id"],),
            )
            == before_changes
        )
        jobs = await store._conn.execute_fetchall(
            "SELECT status FROM local_run_jobs WHERE id = ?", (job["id"],)
        )
        assert [row["status"] for row in jobs] == ["leased"]
    finally:
        await store.close()


async def test_concurrent_event_appends_keep_unique_contiguous_sequences(tmp_path: Path) -> None:
    store = await _open_store(tmp_path)
    try:
        run = await store.create_run(
            principal_id=LOCAL_OWNER_PRINCIPAL_ID,
            goal="ordered events",
            workspace_path=None,
        )
        await asyncio.gather(
            *(store.append_event(run["id"], "model.delta", {"index": index}) for index in range(20))
        )

        events = await store.events_since(run["id"])
        assert [event["seq"] for event in events] == list(range(1, 21))
    finally:
        await store.close()


async def test_open_repairs_legacy_duplicate_event_sequences(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    store = await LocalStore.open(db_path)
    run = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="legacy events",
        workspace_path=None,
    )
    await store.append_event(run["id"], "model.delta", {"index": 1})
    await store.close()

    import aiosqlite

    conn = await aiosqlite.connect(str(db_path))
    await conn.execute("DROP INDEX idx_local_events_run_seq")
    await conn.execute(
        "INSERT INTO local_events "
        "(id, run_id, seq, event_type, payload_json, created_at) "
        "VALUES ('evt_duplicate', ?, 1, 'model.delta', '{}', 'later')",
        (run["id"],),
    )
    await conn.commit()
    await conn.close()

    reopened = await LocalStore.open(db_path)
    try:
        assert [event["seq"] for event in await reopened.events_since(run["id"])] == [1, 2]
        index_rows = await (
            await reopened._conn.execute("PRAGMA index_list(local_events)")
        ).fetchall()
        sequence_index = next(row for row in index_rows if row[1] == "idx_local_events_run_seq")
        assert sequence_index[2] == 1
    finally:
        await reopened.close()


async def test_open_removes_legacy_transient_events_from_durable_replay(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-transient-events.db"
    store = await LocalStore.open(db_path)
    run = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="legacy transient events",
        workspace_path=None,
    )
    await store.append_event(run["id"], "run.started", {"goal": "legacy"})
    for event_type in (
        "llm.delta",
        "llm.round.started",
        "llm.reasoning",
        "llm.usage",
        "llm.tool_call_chunk",
        "subagent.spawned",
    ):
        await store.append_event(run["id"], event_type, {"legacy": True})
    await store.append_event(run["id"], "run.completed", {"final_text": "done"})
    await store.close()

    reopened = await LocalStore.open(db_path)
    try:
        assert [event["event_type"] for event in await reopened.events_since(run["id"])] == [
            "run.started",
            "run.completed",
        ]
    finally:
        await reopened.close()
