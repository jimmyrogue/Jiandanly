"""Run records, graph branch heads, and Run queries."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from ..database import SqliteDatabase
from ..database import utc_now as _now
from ..errors import GraphDefinitionMismatchError, GraphHeadConflictError
from ..ids import new_id as _new_id


class RunRecordStore(SqliteDatabase):
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
