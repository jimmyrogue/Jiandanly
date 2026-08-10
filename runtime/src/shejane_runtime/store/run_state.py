"""Runs, threads, committed results, event logs, and steering state."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from .codec import encode_payload as _encode_payload
from .codec import json_payload as _json_payload
from .collaboration import _CHILD_EVENT_BY_RUN_EVENT
from .database import CURRENT_EXECUTION_LEASE as _CURRENT_EXECUTION_LEASE
from .database import ExecutionLease, LeaseFenceError, SqliteDatabase
from .database import utc_now as _now
from .errors import GraphDefinitionMismatchError, GraphHeadConflictError, RunResultConflictError
from .events import TERMINAL_RUN_STATUSES as _TERMINAL_RUN_STATUSES
from .ids import new_id as _new_id

_RUN_RESULT_EVENTS = {
    "completed": "run.completed",
    "failed": "run.failed",
    "canceled": "run.canceled",
    "waiting_permission": "run.waiting",
    "waiting_input": "run.waiting",
}


class RunStateStore(SqliteDatabase):
    async def create_run(
        self,
        *,
        principal_id: str,
        goal: str,
        workspace_path: str | None,
        parent_run_id: str | None = None,
        settings: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        mode: str = "fast",
        graph_thread_id: str | None = None,
        graph_checkpoint_id: str | None = None,
        graph_definition_id: str | None = None,
        graph_input_kind: str = "new",
        run_kind: str | None = None,
        root_run_id: str | None = None,
        agent_definition_id: str = "shejane.default",
        agent_definition_version: str = "1",
        collaboration_depth: int = 0,
        collaboration_policy: dict[str, Any] | None = None,
        spawn_operation_id: str | None = None,
    ) -> dict[str, Any]:
        if parent_run_id is not None and root_run_id is None:
            parent = await (
                await self._conn.execute(
                    "SELECT root_run_id FROM local_runs WHERE principal_id = ? AND id = ?",
                    (principal_id, parent_run_id),
                )
            ).fetchone()
            if parent is not None:
                root_run_id = str(parent["root_run_id"] or parent_run_id)
        run = self._new_run_record(
            principal_id=principal_id,
            goal=goal,
            workspace_path=workspace_path,
            parent_run_id=parent_run_id,
            settings=settings,
            metadata=metadata,
            mode=mode,
            graph_thread_id=graph_thread_id,
            graph_checkpoint_id=graph_checkpoint_id,
            graph_definition_id=graph_definition_id,
            graph_input_kind=graph_input_kind,
            run_kind=run_kind,
            root_run_id=root_run_id,
            agent_definition_id=agent_definition_id,
            agent_definition_version=agent_definition_version,
            collaboration_depth=collaboration_depth,
            collaboration_policy=collaboration_policy,
            spawn_operation_id=spawn_operation_id,
        )
        await self._insert_run(self._conn, run)
        return run

    @staticmethod
    def _new_run_record(
        *,
        principal_id: str,
        goal: str,
        workspace_path: str | None,
        parent_run_id: str | None,
        settings: dict[str, Any] | None,
        metadata: dict[str, Any] | None,
        mode: str,
        history: list[dict[str, str]] | None = None,
        graph_thread_id: str | None = None,
        graph_checkpoint_id: str | None = None,
        graph_definition_id: str | None = None,
        graph_input_kind: str = "new",
        thread_id: str | None = None,
        assistant_item_id: str | None = None,
        user_input: str | None = None,
        run_kind: str | None = None,
        root_run_id: str | None = None,
        agent_definition_id: str = "shejane.default",
        agent_definition_version: str = "1",
        collaboration_depth: int = 0,
        collaboration_policy: dict[str, Any] | None = None,
        spawn_operation_id: str | None = None,
    ) -> dict[str, Any]:
        if graph_input_kind not in {"new", "fork"}:
            raise ValueError(f"invalid graph input kind: {graph_input_kind}")
        effective_run_kind = run_kind or ("fork" if graph_input_kind == "fork" else "turn")
        if effective_run_kind not in {"turn", "fork", "child"}:
            raise ValueError(f"invalid run kind: {effective_run_kind}")
        if collaboration_depth < 0:
            raise ValueError("collaboration depth must be non-negative")
        run_id = _new_id("run")
        return {
            "id": run_id,
            "principal_id": principal_id,
            "run_kind": effective_run_kind,
            "root_run_id": root_run_id or run_id,
            "agent_definition_id": agent_definition_id,
            "agent_definition_version": agent_definition_version,
            "collaboration_depth": collaboration_depth,
            "collaboration_policy_json": json.dumps(
                collaboration_policy or {}, ensure_ascii=False, sort_keys=True
            ),
            "spawn_operation_id": spawn_operation_id,
            "graph_thread_id": graph_thread_id or _new_id("thread"),
            "graph_checkpoint_id": graph_checkpoint_id,
            "graph_definition_id": graph_definition_id,
            "graph_input_kind": graph_input_kind,
            "thread_id": thread_id,
            "assistant_item_id": assistant_item_id,
            "user_input": user_input or goal,
            "goal": goal,
            "workspace_path": workspace_path,
            "status": "queued",
            "history_json": json.dumps(history or [], ensure_ascii=False, default=str),
            "parent_run_id": parent_run_id,
            "settings_json": json.dumps(settings or {}, ensure_ascii=False),
            "metadata_json": json.dumps(metadata or {}, ensure_ascii=False, default=str),
            "mode": mode,
            "created_at": _now(),
            "updated_at": _now(),
            "completed_at": None,
        }

    @staticmethod
    async def _insert_run(conn: aiosqlite.Connection, run: dict[str, Any]) -> None:
        await conn.execute(
            "INSERT INTO local_runs "
            "(id, principal_id, run_kind, root_run_id, agent_definition_id, "
            " agent_definition_version, collaboration_depth, collaboration_policy_json, "
            " spawn_operation_id, graph_thread_id, graph_checkpoint_id, graph_definition_id, "
            " graph_input_kind, thread_id, assistant_item_id, user_input, goal, workspace_path, status, history_json, parent_run_id, "
            " settings_json, metadata_json, mode, created_at, updated_at, completed_at) "
            "VALUES (:id, :principal_id, :run_kind, :root_run_id, :agent_definition_id, "
            "        :agent_definition_version, :collaboration_depth, "
            "        :collaboration_policy_json, :spawn_operation_id, "
            "        :graph_thread_id, :graph_checkpoint_id, "
            "        :graph_definition_id, :graph_input_kind, :thread_id, :assistant_item_id, :user_input, :goal, :workspace_path, "
            "        :status, :history_json, :parent_run_id, :settings_json, :metadata_json, "
            "        :mode, :created_at, :updated_at, :completed_at)",
            run,
        )

    async def bind_graph_definition(self, run_id: str, definition_id: str) -> None:
        """Bind once, then reject checkpoint execution with a different graph."""
        async with self.run_write_transaction(run_id) as conn:
            row = await (
                await conn.execute(
                    "SELECT graph_definition_id FROM local_runs WHERE id = ?", (run_id,)
                )
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown run: {run_id}")
            current = row[0]
            if current is not None and current != definition_id:
                raise GraphDefinitionMismatchError(
                    f"run {run_id} checkpoint is incompatible with the current agent definition"
                )
            if current is None:
                await conn.execute(
                    "UPDATE local_runs SET graph_definition_id = ?, updated_at = ? WHERE id = ?",
                    (definition_id, _now(), run_id),
                )

    async def advance_graph_checkpoint(
        self,
        run_id: str,
        *,
        graph_thread_id: str,
        expected_checkpoint_id: str | None,
        checkpoint_id: str,
    ) -> None:
        """Move one product Run's branch head with lease-fenced compare-and-swap."""
        async with self.run_write_transaction(run_id) as conn:
            row = await (
                await conn.execute(
                    "SELECT graph_thread_id, graph_checkpoint_id FROM local_runs WHERE id = ?",
                    (run_id,),
                )
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown run: {run_id}")
            if row[0] != graph_thread_id:
                raise GraphHeadConflictError(f"run {run_id} graph branch head changed")
            if row[1] == checkpoint_id:
                return
            if row[1] != expected_checkpoint_id:
                raise GraphHeadConflictError(f"run {run_id} graph branch head changed")
            if checkpoint_id != expected_checkpoint_id:
                await conn.execute(
                    "UPDATE local_runs SET graph_checkpoint_id = ?, updated_at = ? WHERE id = ?",
                    (checkpoint_id, _now(), run_id),
                )

    async def list_active_runs(self) -> list[dict[str, Any]]:
        """Runs not in a terminal state — used at boot to recover orphans left
        behind by a runtime restart (queued/running are dead and must be failed;
        waiting_permission/waiting_input are resumable from the checkpointer)."""
        cursor = await self._conn.execute(
            "SELECT * FROM local_runs WHERE status IN "
            "('queued', 'running', 'waiting_permission', 'waiting_input')"
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT r.*, c.id AS command_id, c.client_message_id "
            "FROM local_runs r LEFT JOIN local_commands c ON c.run_id = r.id "
            "AND c.principal_id = r.principal_id "
            "AND c.command_type IN ('run.start', 'run.fork') "
            "WHERE r.id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_run_for_principal(
        self, *, principal_id: str, run_id: str
    ) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT r.*, c.id AS command_id, c.client_message_id "
            "FROM local_runs r LEFT JOIN local_commands c ON c.run_id = r.id "
            "AND c.principal_id = r.principal_id "
            "AND c.command_type IN ('run.start', 'run.fork') "
            "WHERE r.principal_id = ? AND r.id = ?",
            (principal_id, run_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_runs(self, *, principal_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent runs newest-first with a per-row events_count.

        The client's `listLocalRuns()` (`client/src/runtime/
        client.ts:283`) reads `{runs: LocalRun[]}` on every boot to
        repopulate the conversation history sidebar. Each row must
        include the `LocalRun` fields the renderer reads:
        id, goal, status, workspace_path, created_at, updated_at,
        completed_at, canceled_at, events_count.
        """
        cursor = await self._conn.execute(
            """
            SELECT r.id, r.graph_thread_id, r.graph_checkpoint_id,
                   r.thread_id, r.assistant_item_id,
                   r.run_kind, r.root_run_id, r.agent_definition_id,
                   r.agent_definition_version, r.collaboration_depth,
                   r.collaboration_policy_json, r.spawn_operation_id,
                   r.goal, r.user_input, r.status, r.workspace_path,
                   r.created_at, r.updated_at, r.completed_at,
                   r.metadata_json, c.id AS command_id, c.client_message_id,
                   (SELECT COUNT(*) FROM local_events e
                      WHERE e.run_id = r.id) AS events_count
              FROM local_runs r
              LEFT JOIN local_commands c ON c.run_id = r.id
                   AND c.principal_id = r.principal_id
                   AND c.command_type IN ('run.start', 'run.fork')
             WHERE r.principal_id = ? AND r.run_kind <> 'child'
             ORDER BY datetime(r.updated_at) DESC, r.id DESC
             LIMIT ?
            """,
            (principal_id, limit),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def update_run_status(
        self, run_id: str, status: str, *, completed_at: str | None = None
    ) -> None:
        async with self.run_write_transaction(run_id) as conn:
            await conn.execute(
                "UPDATE local_runs SET status = ?, updated_at = ?, completed_at = ? WHERE id = ?",
                (status, _now(), completed_at, run_id),
            )

    async def commit_run_result(
        self,
        run_id: str,
        *,
        status: str,
        event_type: str,
        payload: dict[str, Any],
        orphan_recovery: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically persist a waiting or terminal run state and its event.

        Returns ``(event, created)``. Repeating the exact same result returns
        the original event with ``created=False``; a different result cannot
        replace an already persisted waiting/terminal result.
        """
        expected_event = _RUN_RESULT_EVENTS.get(status)
        if expected_event != event_type:
            raise ValueError(f"{status!r} must be committed with {expected_event!r}")

        payload_json = _encode_payload(payload)
        execution_lease = _CURRENT_EXECUTION_LEASE.get()
        async with self.run_write_transaction(run_id) as conn:
            run_row = await (
                await conn.execute("SELECT status FROM local_runs WHERE id = ?", (run_id,))
            ).fetchone()
            if run_row is None:
                raise KeyError(f"unknown run: {run_id}")

            if execution_lease is None:
                if not orphan_recovery:
                    raise LeaseFenceError(
                        f"run {run_id} result requires the active execution lease"
                    )
                active_job = await (
                    await conn.execute(
                        "SELECT 1 FROM local_run_jobs WHERE run_id = ? "
                        "AND status IN ('pending', 'leased') LIMIT 1",
                        (run_id,),
                    )
                ).fetchone()
                if active_job is not None:
                    raise LeaseFenceError(f"run {run_id} still has an unsettled execution job")

            current_status = str(run_row[0])
            if current_status == status:
                existing_row = await (
                    await conn.execute(
                        "SELECT * FROM local_events WHERE run_id = ? AND event_type = ? "
                        "ORDER BY seq DESC LIMIT 1",
                        (run_id, event_type),
                    )
                ).fetchone()
                if existing_row is not None:
                    existing = dict(existing_row)
                    try:
                        existing_payload = _encode_payload(
                            json.loads(existing["payload_json"] or "{}")
                        )
                    except (json.JSONDecodeError, TypeError):
                        existing_payload = str(existing["payload_json"])
                    if existing_payload == payload_json:
                        if execution_lease is not None:
                            await self._settle_execution_job_uncommitted(
                                conn,
                                execution_lease,
                                run_status=status,
                                settled_at=_now(),
                            )
                        return existing, False
                    raise RunResultConflictError(
                        f"run {run_id} already has a different {status} result"
                    )
            elif current_status in _TERMINAL_RUN_STATUSES:
                raise RunResultConflictError(
                    f"run {run_id} is already terminal with status {current_status}"
                )

            committed_at = _now()
            if status in _TERMINAL_RUN_STATUSES:
                await self._cancel_unstarted_tool_receipts_uncommitted(
                    conn,
                    run_id=run_id,
                    canceled_at=committed_at,
                    error_type={
                        "completed": "ParentRunCompleted",
                        "failed": "ParentRunFailed",
                        "canceled": "RunCanceled",
                    }[status],
                )
            completed_at = committed_at if status in _TERMINAL_RUN_STATUSES else None
            await conn.execute(
                "UPDATE local_runs SET status = ?, updated_at = ?, completed_at = ? WHERE id = ?",
                (status, committed_at, completed_at, run_id),
            )
            event = await self._append_event_uncommitted(
                conn,
                run_id,
                event_type,
                payload_json=payload_json,
                created_at=committed_at,
            )
            await self._update_thread_projection_uncommitted(
                conn,
                run_id=run_id,
                run_status=status,
                change_type=event_type,
                payload=payload,
                event_high_watermark=int(event["seq"]),
                changed_at=committed_at,
            )
            if execution_lease is not None:
                await self._settle_execution_job_uncommitted(
                    conn,
                    execution_lease,
                    run_status=status,
                    settled_at=committed_at,
                )
            return event, True

    @staticmethod
    async def _update_thread_projection_uncommitted(
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        run_status: str,
        change_type: str,
        payload: dict[str, Any],
        event_high_watermark: int,
        changed_at: str,
    ) -> None:
        run = await (
            await conn.execute(
                "SELECT principal_id, thread_id, assistant_item_id FROM local_runs WHERE id = ?",
                (run_id,),
            )
        ).fetchone()
        if run is None or not run["thread_id"] or not run["assistant_item_id"]:
            return
        draft = await (
            await conn.execute(
                "SELECT content FROM local_assistant_drafts WHERE run_id = ?",
                (run_id,),
            )
        ).fetchone()
        content = str(payload.get("final_text") or "") if run_status == "completed" else ""
        if not content and draft is not None:
            content = str(draft["content"] or "")
        item_status = {
            "completed": "completed",
            "failed": "failed",
            "canceled": "canceled",
            "waiting_permission": "in_progress",
            "waiting_input": "in_progress",
            "cleanup_required": "cleanup_required",
        }[run_status]
        terminal = run_status in _TERMINAL_RUN_STATUSES
        cursor = await conn.execute(
            "UPDATE local_thread_items SET status = ?, content = ?, event_high_watermark = ?, "
            "version = version + 1, updated_at = ?, completed_at = ? "
            "WHERE id = ? AND run_id = ?",
            (
                item_status,
                content,
                event_high_watermark,
                changed_at,
                changed_at if terminal else None,
                run["assistant_item_id"],
                run_id,
            ),
        )
        if cursor.rowcount != 1:
            raise RunResultConflictError(f"run {run_id} is missing its assistant projection")
        thread = await (
            await conn.execute(
                "SELECT version, title FROM local_threads WHERE id = ? AND principal_id = ?",
                (run["thread_id"], run["principal_id"]),
            )
        ).fetchone()
        if thread is None:
            raise RunResultConflictError(f"run {run_id} is missing its thread projection")
        thread_version = int(thread["version"]) + 1
        generated_title = " ".join(str(payload.get("thread_title") or "").split())[:80]
        title_seed = " ".join(str(payload.get("thread_title_seed") or "").split())[:80]
        next_title = (
            generated_title
            if generated_title and title_seed and str(thread["title"]) == title_seed
            else str(thread["title"])
        )
        await conn.execute(
            "UPDATE local_threads SET version = ?, title = ?, updated_at = ? WHERE id = ?",
            (thread_version, next_title, changed_at, run["thread_id"]),
        )
        await conn.execute(
            "INSERT INTO local_thread_changes "
            "(principal_id, thread_id, thread_version, change_type, run_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                run["principal_id"],
                run["thread_id"],
                thread_version,
                change_type,
                run_id,
                changed_at,
            ),
        )

    @staticmethod
    async def _settle_execution_job_uncommitted(
        conn: aiosqlite.Connection,
        lease: ExecutionLease,
        *,
        run_status: str,
        settled_at: str,
    ) -> None:
        fence_at = _now()
        job_status = {
            "completed": "completed",
            "failed": "dead",
            "canceled": "canceled",
            "waiting_permission": "completed",
            "waiting_input": "completed",
        }[run_status]
        cursor = await conn.execute(
            "UPDATE local_run_jobs SET status = ?, updated_at = ?, finished_at = ?, "
            "lease_expires_at = NULL WHERE id = ? AND run_id = ? AND status = 'leased' "
            "AND lease_owner = ? AND lease_generation = ? AND quarantined_at IS NULL "
            "AND lease_expires_at > ?",
            (
                job_status,
                settled_at,
                settled_at,
                lease.job_id,
                lease.run_id,
                lease.lease_owner,
                lease.lease_generation,
                fence_at,
            ),
        )
        if cursor.rowcount != 1:
            raise LeaseFenceError(
                f"run {lease.run_id} lease generation {lease.lease_generation} is stale"
            )

    async def append_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        async with self.run_write_transaction(run_id) as conn:
            event = await self._append_event_uncommitted(
                conn,
                run_id,
                event_type,
                payload_json=_encode_payload(payload),
                created_at=_now(),
            )
            return event

    async def _append_event_uncommitted(
        self,
        conn: aiosqlite.Connection,
        run_id: str,
        event_type: str,
        *,
        payload_json: str,
        created_at: str,
        project_child: bool = True,
    ) -> dict[str, Any]:
        cursor = await conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM local_events WHERE run_id = ?", (run_id,)
        )
        row = await cursor.fetchone()
        next_seq = (row[0] if row else 0) + 1
        event = {
            "id": _new_id("evt"),
            "run_id": run_id,
            "seq": next_seq,
            "event_type": event_type,
            "payload_json": payload_json,
            "created_at": created_at,
        }
        await conn.execute(
            "INSERT INTO local_events (id, run_id, seq, event_type, payload_json, created_at) "
            "VALUES (:id, :run_id, :seq, :event_type, :payload_json, :created_at)",
            event,
        )
        if project_child and event_type in _CHILD_EVENT_BY_RUN_EVENT:
            parent_event = await self._append_child_parent_lifecycle_uncommitted(
                conn,
                child_run_id=run_id,
                source_event=event,
                source_payload=_json_payload(payload_json),
            )
            if parent_event is not None:
                event["_parent_event"] = parent_event
        return event

    async def _append_child_parent_lifecycle_uncommitted(
        self,
        conn: aiosqlite.Connection,
        *,
        child_run_id: str,
        source_event: dict[str, Any],
        source_payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        child = await (
            await conn.execute(
                "SELECT id, parent_run_id, root_run_id, agent_definition_id, "
                "agent_definition_version, collaboration_depth, goal, status, updated_at "
                "FROM local_runs WHERE id = ? AND run_kind = 'child'",
                (child_run_id,),
            )
        ).fetchone()
        if child is None or not child["parent_run_id"]:
            return None
        parent_event_type, fixed_status = _CHILD_EVENT_BY_RUN_EVENT[str(source_event["event_type"])]
        status = fixed_status or str(child["status"])
        final_text = str(source_payload.get("final_text") or "")
        payload: dict[str, Any] = {
            "child_run_id": str(child["id"]),
            "parent_run_id": str(child["parent_run_id"]),
            "root_run_id": str(child["root_run_id"]),
            "agent_definition_id": str(child["agent_definition_id"]),
            "agent_definition_version": str(child["agent_definition_version"]),
            "collaboration_depth": int(child["collaboration_depth"]),
            "goal": str(child["goal"]),
            "status": status,
            "source_event_id": str(source_event["id"]),
            "source_event_seq": int(source_event["seq"]),
            "updated_at": str(child["updated_at"]),
        }
        if final_text:
            payload["result_preview"] = final_text[:4096]
            payload["result_truncated"] = len(final_text) > 4096
        for key in (
            "error",
            "type",
            "category",
            "retryable",
            "input_tokens",
            "output_tokens",
            "model_calls",
            "final_answer_ref",
        ):
            if key in source_payload:
                payload[key] = source_payload[key]
        parent_event = await self._append_event_uncommitted(
            conn,
            str(child["parent_run_id"]),
            parent_event_type,
            payload_json=_encode_payload(payload),
            created_at=str(source_event["created_at"]),
            project_child=False,
        )
        await self._touch_thread_for_run_event_uncommitted(
            conn,
            run_id=str(child["parent_run_id"]),
            change_type=parent_event_type,
            event_high_watermark=int(parent_event["seq"]),
            changed_at=str(source_event["created_at"]),
        )
        return parent_event

    async def events_since(
        self,
        run_id: str,
        after_seq: int = 0,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM local_events WHERE run_id = ? AND seq > ? ORDER BY seq"
        parameters: tuple[Any, ...] = (run_id, after_seq)
        if limit is not None:
            sql += " LIMIT ?"
            parameters += (limit,)
        cursor = await self._conn.execute(sql, parameters)
        return [dict(row) for row in await cursor.fetchall()]

    async def event_sequence_window(self, run_id: str) -> tuple[int | None, int]:
        row = await (
            await self._conn.execute(
                "SELECT MIN(seq), COALESCE(MAX(seq), 0) FROM local_events WHERE run_id = ?",
                (run_id,),
            )
        ).fetchone()
        return (int(row[0]) if row and row[0] is not None else None, int(row[1]) if row else 0)

    async def create_steering_instruction(
        self,
        *,
        run_id: str,
        content: str,
    ) -> dict[str, Any]:
        record = {
            "id": _new_id("steer"),
            "run_id": run_id,
            "content": content,
            "status": "pending",
            "created_at": _now(),
            "injected_at": None,
        }
        await self._conn.execute(
            "INSERT INTO local_steering "
            "(id, run_id, content, status, created_at, injected_at) "
            "VALUES (:id, :run_id, :content, :status, :created_at, :injected_at)",
            record,
        )
        return record

    async def claim_pending_steering(self, run_id: str) -> list[dict[str, Any]]:
        async with self.run_write_transaction(run_id) as conn:
            cursor = await conn.execute(
                "SELECT * FROM local_steering "
                "WHERE run_id = ? AND status = 'pending' ORDER BY created_at, id",
                (run_id,),
            )
            rows = [dict(row) for row in await cursor.fetchall()]
            if not rows:
                return []
            injected_at = _now()
            await conn.executemany(
                "UPDATE local_steering SET status = 'injected', injected_at = ? "
                "WHERE id = ? AND status = 'pending'",
                [(injected_at, row["id"]) for row in rows],
            )
            return [{**row, "status": "injected", "injected_at": injected_at} for row in rows]
