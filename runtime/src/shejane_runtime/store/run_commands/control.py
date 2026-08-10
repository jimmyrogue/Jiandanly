"""Run cancellation and steering commands."""

from __future__ import annotations

from typing import Any

import aiosqlite

from ..codec import encode_payload as _encode_payload
from ..database import SqliteDatabase
from ..database import configure_connection as _configure_connection
from ..database import utc_now as _now
from ..errors import RunAdmissionError
from ..events import TERMINAL_RUN_STATUSES as _TERMINAL_RUN_STATUSES
from ..ids import new_id as _new_id


class RunControlCommandStore(SqliteDatabase):
    async def request_run_cancel(self, run_id: str) -> str | None:
        states = await self.request_run_cancel_tree(run_id)
        return states.get(run_id)

    async def request_run_cancel_tree(self, run_id: str) -> dict[str, str]:
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                states = await self._request_run_cancel_tree_uncommitted(conn, run_id)
                if (
                    not states
                    and await (
                        await conn.execute("SELECT 1 FROM local_runs WHERE id = ?", (run_id,))
                    ).fetchone()
                    is None
                ):
                    await conn.rollback()
                    return {}
                await conn.commit()
                return states
            except BaseException:
                await conn.rollback()
                raise

    async def request_run_cancel_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        run_id: str,
    ) -> tuple[dict[str, Any], bool]:
        payload_json = _encode_payload({"type": "run.cancel", "run_id": run_id})
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type="run.cancel",
                    payload_json=payload_json,
                )
                if existing is not None:
                    await conn.rollback()
                    return existing, False
                run = await (
                    await conn.execute(
                        "SELECT id, created_at FROM local_runs WHERE principal_id = ? AND id = ?",
                        (principal_id, run_id),
                    )
                ).fetchone()
                if run is None:
                    raise KeyError(f"unknown run: {run_id}")
                states = await self._request_run_cancel_tree_uncommitted(conn, run_id)
                receipt = {
                    "type": "run.cancel",
                    "command_id": command_id,
                    "run_id": run_id,
                    "canceled": run_id in states,
                }
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) VALUES (?, ?, 'run.cancel', '', ?, ?, ?, ?)",
                    (
                        principal_id,
                        command_id,
                        payload_json,
                        _encode_payload(receipt),
                        run_id,
                        _now(),
                    ),
                )
                await conn.commit()
                return receipt, True
            except BaseException:
                await conn.rollback()
                raise

    async def _request_run_cancel_tree_uncommitted(
        self,
        conn: aiosqlite.Connection,
        run_id: str,
    ) -> dict[str, str]:
        rows = await (
            await conn.execute(
                "WITH RECURSIVE tree(id, depth) AS ("
                "SELECT id, 0 FROM local_runs WHERE id = ? UNION ALL "
                "SELECT r.id, tree.depth + 1 FROM local_runs r "
                "JOIN tree ON r.parent_run_id = tree.id WHERE r.run_kind = 'child'"
                ") SELECT id FROM tree ORDER BY depth DESC, id",
                (run_id,),
            )
        ).fetchall()
        states: dict[str, str] = {}
        for row in rows:
            target_run_id = str(row["id"])
            state = await self._request_run_cancel_uncommitted(conn, target_run_id)
            if state is not None:
                states[target_run_id] = state
        return states

    async def _request_run_cancel_uncommitted(
        self,
        conn: aiosqlite.Connection,
        run_id: str,
    ) -> str | None:
        row = await (
            await conn.execute(
                "SELECT * FROM local_run_jobs WHERE run_id = ? "
                "AND status IN ('pending', 'leased') AND quarantined_at IS NULL",
                (run_id,),
            )
        ).fetchone()
        if row is None:
            run = await (
                await conn.execute(
                    "SELECT status FROM local_runs WHERE id = ?",
                    (run_id,),
                )
            ).fetchone()
            if run is None or run["status"] not in {"waiting_permission", "waiting_input"}:
                return None
            requested_at = _now()
            await self._finish_run_cancel_uncommitted(
                conn,
                run_id=run_id,
                requested_at=requested_at,
            )
            return "waiting"
        job = dict(row)
        requested_at = _now()
        if job["status"] == "leased":
            await conn.execute(
                "UPDATE local_run_jobs SET cancel_requested_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'leased'",
                (requested_at, requested_at, job["id"]),
            )
            return "leased"

        await conn.execute(
            "UPDATE local_run_jobs SET status = 'canceled', cancel_requested_at = ?, "
            "updated_at = ?, finished_at = ? WHERE id = ? AND status = 'pending'",
            (requested_at, requested_at, requested_at, job["id"]),
        )
        await self._finish_run_cancel_uncommitted(
            conn,
            run_id=run_id,
            requested_at=requested_at,
        )
        return "pending"

    async def _finish_run_cancel_uncommitted(
        self,
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        requested_at: str,
    ) -> None:
        cancel_decision = _encode_payload({"type": "cancel", "reason": "run_canceled"})
        await conn.execute(
            "UPDATE local_permissions SET status = 'canceled', decision_json = ?, "
            "resolved_at = ? WHERE run_id = ? AND status = 'pending'",
            (cancel_decision, requested_at, run_id),
        )
        await conn.execute(
            "UPDATE local_questions SET status = 'canceled' "
            "WHERE run_id = ? AND status = 'pending'",
            (run_id,),
        )
        await conn.execute(
            "UPDATE local_wait_candidates SET status = 'resolved', decision_json = ?, "
            "resolved_at = ? WHERE run_id = ? AND status = 'pending'",
            (cancel_decision, requested_at, run_id),
        )
        await conn.execute(
            "UPDATE local_plan_approvals SET status = 'canceled', resolved_at = ? "
            "WHERE run_id = ? AND status = 'pending'",
            (requested_at, run_id),
        )
        await self._cancel_unstarted_tool_receipts_uncommitted(
            conn,
            run_id=run_id,
            canceled_at=requested_at,
        )
        await conn.execute(
            "UPDATE local_runs SET status = 'canceled', updated_at = ?, completed_at = ? "
            "WHERE id = ?",
            (requested_at, requested_at, run_id),
        )
        event = await self._append_event_uncommitted(
            conn,
            run_id,
            "run.canceled",
            payload_json="{}",
            created_at=requested_at,
        )
        await self._update_thread_projection_uncommitted(
            conn,
            run_id=run_id,
            run_status="canceled",
            change_type="run.canceled",
            payload={},
            event_high_watermark=int(event["seq"]),
            changed_at=requested_at,
        )

    async def request_run_inject_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        run_id: str,
        content: str,
    ) -> tuple[dict[str, Any], bool]:
        payload_json = _encode_payload({"type": "run.inject", "run_id": run_id, "content": content})
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type="run.inject",
                    payload_json=payload_json,
                )
                if existing is not None:
                    await conn.rollback()
                    return existing, False
                run = await (
                    await conn.execute(
                        "SELECT id, status FROM local_runs WHERE principal_id = ? AND id = ?",
                        (principal_id, run_id),
                    )
                ).fetchone()
                if run is None:
                    raise KeyError(f"unknown run: {run_id}")
                if str(run["status"]) in _TERMINAL_RUN_STATUSES | {"cleanup_required"}:
                    raise RunAdmissionError("run_not_active", "run is not active")

                now = _now()
                instruction_id = _new_id("steer")
                await conn.execute(
                    "INSERT INTO local_steering "
                    "(id, run_id, content, status, created_at, injected_at) "
                    "VALUES (?, ?, ?, 'pending', ?, NULL)",
                    (instruction_id, run_id, content, now),
                )
                receipt = {
                    "command_id": command_id,
                    "run_id": run_id,
                    "instruction_id": instruction_id,
                    "queued": True,
                }
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) "
                    "VALUES (?, ?, 'run.inject', '', ?, ?, ?, ?)",
                    (
                        principal_id,
                        command_id,
                        payload_json,
                        _encode_payload(receipt),
                        run_id,
                        now,
                    ),
                )
                await conn.commit()
                return receipt, True
            except BaseException:
                await conn.rollback()
                raise
