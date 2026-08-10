"""Scheduled Run persistence."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from .database import SqliteDatabase
from .database import configure_connection as _configure_connection
from .database import utc_now as _now
from .errors import WorkspaceAdmissionError
from .ids import new_id as _new_id


class ScheduledRunStore(SqliteDatabase):
    # --- scheduled runs ---

    async def create_scheduled_run(
        self,
        *,
        principal_id: str,
        goal: str,
        run_at: str,
        workspace_path: str | None = None,
        model: str = "auto",
        history: list[dict[str, str]] | None = None,
        settings: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = {
            "id": _new_id("sched"),
            "principal_id": principal_id,
            "goal": goal,
            "workspace_path": workspace_path,
            "model": model or "auto",
            "history_json": json.dumps(history or [], ensure_ascii=False, default=str),
            "settings_json": json.dumps(settings or {}, ensure_ascii=False, default=str),
            "metadata_json": json.dumps(metadata or {}, ensure_ascii=False, default=str),
            "run_at": run_at,
            "status": "scheduled",
            "run_id": None,
            "result_text": None,
            "error_message": None,
            "created_at": _now(),
            "updated_at": _now(),
            "completed_at": None,
            "notified_at": None,
        }
        path_error = await self._workspace_path_error(workspace_path)
        if path_error is not None:
            raise WorkspaceAdmissionError(path_error)
        async with aiosqlite.connect(str(self._db_path)) as transaction_conn:
            await _configure_connection(transaction_conn)
            await transaction_conn.execute("BEGIN IMMEDIATE")
            try:
                workspace_error = await self._workspace_owner_error(
                    transaction_conn,
                    principal_id=principal_id,
                    path=workspace_path,
                )
                if workspace_error is not None:
                    raise WorkspaceAdmissionError(workspace_error)
                await transaction_conn.execute(
                    "INSERT INTO local_scheduled_runs "
                    "(id, principal_id, goal, workspace_path, model, history_json, "
                    " settings_json, metadata_json, run_at, status, run_id, result_text, "
                    " error_message, created_at, updated_at, completed_at, notified_at) "
                    "VALUES (:id, :principal_id, :goal, :workspace_path, :model, "
                    " :history_json, :settings_json, :metadata_json, :run_at, :status, "
                    " :run_id, :result_text, :error_message, :created_at, :updated_at, "
                    " :completed_at, :notified_at)",
                    record,
                )
                await transaction_conn.commit()
            except BaseException:
                await transaction_conn.rollback()
                raise
        return record

    async def get_scheduled_run(self, schedule_id: str) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM local_scheduled_runs WHERE id = ?",
            (schedule_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_scheduled_runs(
        self,
        *,
        limit: int = 50,
        status: str | None = None,
        notify_pending: bool = False,
    ) -> list[dict[str, Any]]:
        if notify_pending:
            cursor = await self._conn.execute(
                """
                SELECT * FROM local_scheduled_runs
                 WHERE status IN ('completed', 'failed')
                   AND notified_at IS NULL
                 ORDER BY datetime(updated_at) ASC, id ASC
                 LIMIT ?
                """,
                (limit,),
            )
            return [dict(row) for row in await cursor.fetchall()]
        if status:
            cursor = await self._conn.execute(
                """
                SELECT * FROM local_scheduled_runs
                 WHERE status = ?
                 ORDER BY datetime(run_at) ASC, id ASC
                 LIMIT ?
                """,
                (status, limit),
            )
            return [dict(row) for row in await cursor.fetchall()]
        cursor = await self._conn.execute(
            """
            SELECT * FROM local_scheduled_runs
             ORDER BY datetime(run_at) DESC, id DESC
             LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def claim_due_scheduled_runs(
        self,
        *,
        now: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                rows = await (
                    await conn.execute(
                        "SELECT * FROM local_scheduled_runs "
                        "WHERE status = 'scheduled' AND run_at <= ? "
                        "ORDER BY run_at ASC, id ASC LIMIT ?",
                        (now, limit),
                    )
                ).fetchall()
                if not rows:
                    await conn.commit()
                    return []
                updated_at = _now()
                await conn.executemany(
                    "UPDATE local_scheduled_runs SET status = 'running', updated_at = ? "
                    "WHERE id = ? AND status = 'scheduled'",
                    [(updated_at, row["id"]) for row in rows],
                )
                await conn.commit()
                return [
                    {**dict(row), "status": "running", "updated_at": updated_at} for row in rows
                ]
            except BaseException:
                await conn.rollback()
                raise

    async def mark_scheduled_run_started(
        self, schedule_id: str, run_id: str
    ) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "UPDATE local_scheduled_runs SET status = 'running', run_id = ?, updated_at = ? "
            "WHERE id = ? AND status = 'running' AND (run_id IS NULL OR run_id = ?) "
            "RETURNING *",
            (run_id, _now(), schedule_id, run_id),
        )
        row = await cursor.fetchone()
        if row is not None:
            return dict(row)
        return await self.get_scheduled_run(schedule_id)

    async def complete_scheduled_run(
        self,
        schedule_id: str,
        *,
        status: str,
        result_text: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None:
        if status not in {"completed", "failed", "canceled"}:
            raise ValueError(f"invalid scheduled run terminal status: {status}")
        completed_at = _now()
        cursor = await self._conn.execute(
            "UPDATE local_scheduled_runs "
            "SET status = ?, result_text = ?, error_message = ?, completed_at = ?, updated_at = ? "
            "WHERE id = ? AND status = 'running' RETURNING *",
            (status, result_text, error_message, completed_at, completed_at, schedule_id),
        )
        row = await cursor.fetchone()
        if row is not None:
            return dict(row)
        return await self.get_scheduled_run(schedule_id)

    async def cancel_scheduled_run(
        self, *, principal_id: str, schedule_id: str
    ) -> dict[str, Any] | None:
        canceled_at = _now()
        cursor = await self._conn.execute(
            "UPDATE local_scheduled_runs "
            "SET status = 'canceled', completed_at = ?, updated_at = ? "
            "WHERE principal_id = ? AND id = ? AND status = 'scheduled' RETURNING *",
            (canceled_at, canceled_at, principal_id, schedule_id),
        )
        row = await cursor.fetchone()
        if row is not None:
            return dict(row)
        return await self._scheduled_run_for_principal(principal_id, schedule_id)

    async def mark_scheduled_run_notified(
        self, *, principal_id: str, schedule_id: str
    ) -> dict[str, Any] | None:
        notified_at = _now()
        cursor = await self._conn.execute(
            "UPDATE local_scheduled_runs SET notified_at = ?, updated_at = ? "
            "WHERE principal_id = ? AND id = ? "
            "AND status IN ('completed', 'failed') AND notified_at IS NULL RETURNING *",
            (notified_at, notified_at, principal_id, schedule_id),
        )
        row = await cursor.fetchone()
        if row is not None:
            return dict(row)
        return await self._scheduled_run_for_principal(principal_id, schedule_id)

    async def _scheduled_run_for_principal(
        self, principal_id: str, schedule_id: str
    ) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM local_scheduled_runs WHERE principal_id = ? AND id = ?",
            (principal_id, schedule_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
