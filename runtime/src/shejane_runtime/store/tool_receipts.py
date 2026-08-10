"""P10 durable tool-execution receipts and receipt projections."""

from __future__ import annotations

from typing import Any

import aiosqlite

from .codec import encode_payload as _encode_payload
from .codec import json_payload as _json_payload
from .database import CURRENT_EXECUTION_LEASE as _CURRENT_EXECUTION_LEASE
from .database import LeaseFenceError, SqliteDatabase
from .database import utc_now as _now
from .errors import (
    RunResultConflictError,
    ToolOutcomeUnknownError,
    ToolReceiptConflictError,
    ToolReceiptStateError,
)

_SUBAGENT_EVENT_BY_RECEIPT_STATUS = {
    "prepared": ("subagent.spawned", "queued"),
    "running": ("subagent.started", "running"),
    "paused": ("subagent.waiting", "waiting"),
    "completed": ("subagent.completed", "completed"),
    "failed": ("subagent.failed", "failed"),
    "rejected": ("subagent.failed", "failed"),
    "canceled": ("subagent.canceled", "canceled"),
    "outcome_unknown": ("subagent.outcome_unknown", "unknown"),
}


class ToolReceiptStore(SqliteDatabase):
    @staticmethod
    async def _require_tool_receipt_in_run_uncommitted(
        conn: aiosqlite.Connection,
        *,
        operation_id: str,
        run_id: str,
    ) -> None:
        receipt = await (
            await conn.execute(
                "SELECT run_id FROM local_tool_receipts WHERE operation_id = ?",
                (operation_id,),
            )
        ).fetchone()
        if receipt is None:
            raise ToolReceiptStateError(f"parent tool receipt {operation_id} does not exist")
        if str(receipt["run_id"]) != run_id:
            raise ToolReceiptStateError(
                f"parent tool receipt {operation_id} must belong to the same run"
            )

    # --- durable tool execution receipts ---

    @staticmethod
    def _subagent_invocation_projection(record: dict[str, Any]) -> dict[str, Any]:
        receipt_status = str(record["status"])
        _event_type, status = _SUBAGENT_EVENT_BY_RECEIPT_STATUS[receipt_status]
        arguments = _json_payload(record.get("arguments_json"))
        return {
            "operation_id": str(record["operation_id"]),
            "parent_run_id": str(record["run_id"]),
            "parent_operation_id": (
                str(record["parent_operation_id"])
                if record.get("parent_operation_id") is not None
                else None
            ),
            "tool_call_id": str(record["tool_call_id"]),
            "subagent_type": str(arguments.get("subagent_type") or ""),
            "description": str(arguments.get("description") or ""),
            "status": status,
            "receipt_status": receipt_status,
            "attempt_count": int(record.get("attempt_count") or 0),
            "usage": {
                "model_calls": int(record.get("usage_model_calls") or 0),
                "input_tokens": int(record.get("usage_input_tokens") or 0),
                "output_tokens": int(record.get("usage_output_tokens") or 0),
                "unmetered_calls": int(record.get("usage_unmetered_calls") or 0),
                "outcome_unknown_calls": int(record.get("usage_outcome_unknown_calls") or 0),
            },
            "error_type": (
                str(record["error_type"]) if record.get("error_type") is not None else None
            ),
            "created_at": str(record["created_at"]),
            "started_at": (
                str(record["started_at"]) if record.get("started_at") is not None else None
            ),
            "completed_at": (
                str(record["completed_at"]) if record.get("completed_at") is not None else None
            ),
            "updated_at": str(record["updated_at"]),
        }

    @staticmethod
    async def _subagent_invocations_uncommitted(
        conn: aiosqlite.Connection,
        run_ids: list[str],
    ) -> list[dict[str, Any]]:
        if not run_ids:
            return []
        placeholders = ",".join("?" for _ in run_ids)
        rows = await (
            await conn.execute(
                "SELECT r.*, COUNT(m.id) AS usage_model_calls, "
                "COALESCE(SUM(m.input_tokens), 0) AS usage_input_tokens, "
                "COALESCE(SUM(m.output_tokens), 0) AS usage_output_tokens, "
                "COALESCE(SUM(CASE WHEN m.status = 'completed_unmetered' THEN 1 ELSE 0 END), 0) "
                "AS usage_unmetered_calls, "
                "COALESCE(SUM(CASE WHEN m.status = 'outcome_unknown' THEN 1 ELSE 0 END), 0) "
                "AS usage_outcome_unknown_calls "
                "FROM local_tool_receipts r LEFT JOIN local_model_calls m "
                "ON m.parent_tool_operation_id = r.operation_id "
                f"WHERE r.tool_name = 'task' AND r.run_id IN ({placeholders}) "
                "GROUP BY r.operation_id ORDER BY r.created_at, r.operation_id",
                run_ids,
            )
        ).fetchall()
        return [ToolReceiptStore._subagent_invocation_projection(dict(row)) for row in rows]

    async def list_subagent_invocations_for_runs(
        self,
        run_ids: list[str],
    ) -> list[dict[str, Any]]:
        """Return receipt-owned SubAgent snapshots for a batch of parent Runs."""
        normalized_run_ids = list(dict.fromkeys(str(run_id) for run_id in run_ids if run_id))
        return await self._subagent_invocations_uncommitted(self._conn, normalized_run_ids)

    @staticmethod
    async def _touch_thread_for_run_event_uncommitted(
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        change_type: str,
        event_high_watermark: int,
        changed_at: str,
    ) -> None:
        """Publish a Run event to P4 without changing assistant content or status."""
        run = await (
            await conn.execute(
                "SELECT principal_id, thread_id, assistant_item_id FROM local_runs WHERE id = ?",
                (run_id,),
            )
        ).fetchone()
        if run is None or not run["thread_id"] or not run["assistant_item_id"]:
            return
        item_cursor = await conn.execute(
            "UPDATE local_thread_items SET event_high_watermark = "
            "MAX(event_high_watermark, ?), version = version + 1, updated_at = ? "
            "WHERE id = ? AND run_id = ?",
            (event_high_watermark, changed_at, run["assistant_item_id"], run_id),
        )
        if item_cursor.rowcount != 1:
            raise RunResultConflictError(f"run {run_id} is missing its assistant projection")
        thread = await (
            await conn.execute(
                "SELECT version FROM local_threads WHERE id = ? AND principal_id = ?",
                (run["thread_id"], run["principal_id"]),
            )
        ).fetchone()
        if thread is None:
            raise RunResultConflictError(f"run {run_id} is missing its thread projection")
        thread_version = int(thread["version"]) + 1
        await conn.execute(
            "UPDATE local_threads SET version = ?, updated_at = ? WHERE id = ?",
            (thread_version, changed_at, run["thread_id"]),
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

    async def _append_subagent_receipt_event_uncommitted(
        self,
        conn: aiosqlite.Connection,
        receipt: dict[str, Any],
    ) -> dict[str, Any] | None:
        if str(receipt.get("tool_name") or "") != "task":
            return None
        usage = await (
            await conn.execute(
                "SELECT COUNT(*) AS usage_model_calls, "
                "COALESCE(SUM(input_tokens), 0) AS usage_input_tokens, "
                "COALESCE(SUM(output_tokens), 0) AS usage_output_tokens, "
                "COALESCE(SUM(CASE WHEN status = 'completed_unmetered' THEN 1 ELSE 0 END), 0) "
                "AS usage_unmetered_calls, "
                "COALESCE(SUM(CASE WHEN status = 'outcome_unknown' THEN 1 ELSE 0 END), 0) "
                "AS usage_outcome_unknown_calls FROM local_model_calls "
                "WHERE parent_tool_operation_id = ?",
                (receipt["operation_id"],),
            )
        ).fetchone()
        projected = self._subagent_invocation_projection(
            {**receipt, **(dict(usage) if usage is not None else {})}
        )
        event_type, _status = _SUBAGENT_EVENT_BY_RECEIPT_STATUS[str(receipt["status"])]
        event = await self._append_event_uncommitted(
            conn,
            str(receipt["run_id"]),
            event_type,
            payload_json=_encode_payload(projected),
            created_at=str(receipt["updated_at"]),
        )
        await self._touch_thread_for_run_event_uncommitted(
            conn,
            run_id=str(receipt["run_id"]),
            change_type=event_type,
            event_high_watermark=int(event["seq"]),
            changed_at=str(receipt["updated_at"]),
        )
        return event

    async def _mark_running_tool_receipts_outcome_unknown_uncommitted(
        self,
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        execution_attempt_id: str,
        error_type: str,
        now: str,
    ) -> None:
        """Fence a lost attempt and project every transitioned task atomically."""
        running = await (
            await conn.execute(
                "SELECT * FROM local_tool_receipts WHERE run_id = ? "
                "AND execution_attempt_id = ? AND status = 'running' "
                "ORDER BY created_at, operation_id",
                (run_id, execution_attempt_id),
            )
        ).fetchall()
        if not running:
            return
        await conn.execute(
            "UPDATE local_tool_receipts SET status = 'outcome_unknown', "
            "error_type = ?, updated_at = ?, completed_at = ? "
            "WHERE run_id = ? AND execution_attempt_id = ? AND status = 'running'",
            (error_type, now, now, run_id, execution_attempt_id),
        )
        for row in running:
            receipt = {
                **dict(row),
                "status": "outcome_unknown",
                "error_type": error_type,
                "updated_at": now,
                "completed_at": now,
            }
            await self._append_subagent_receipt_event_uncommitted(conn, receipt)

    async def _cancel_unstarted_tool_receipts_uncommitted(
        self,
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        canceled_at: str,
        error_type: str = "RunCanceled",
    ) -> None:
        """Close queued or paused tools when their parent Run can no longer resume them."""
        open_receipts = await (
            await conn.execute(
                "SELECT * FROM local_tool_receipts WHERE run_id = ? "
                "AND status IN ('prepared', 'paused') ORDER BY created_at, operation_id",
                (run_id,),
            )
        ).fetchall()
        if not open_receipts:
            return
        await conn.execute(
            "UPDATE local_tool_receipts SET status = 'canceled', "
            "error_type = ?, completed_at = ?, updated_at = ? "
            "WHERE run_id = ? AND status IN ('prepared', 'paused')",
            (error_type, canceled_at, canceled_at, run_id),
        )
        for row in open_receipts:
            await self._append_subagent_receipt_event_uncommitted(
                conn,
                {
                    **dict(row),
                    "status": "canceled",
                    "error_type": error_type,
                    "completed_at": canceled_at,
                    "updated_at": canceled_at,
                },
            )

    async def prepare_tool_receipt(
        self,
        *,
        operation_id: str,
        run_id: str,
        execution_attempt_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments_hash: str,
        arguments_json: str,
        risk: str,
        tool_version: str = "",
        execution_namespace: str = "main",
        parent_operation_id: str | None = None,
    ) -> dict[str, Any]:
        async with self.run_write_transaction(run_id) as conn:
            if parent_operation_id is not None:
                if parent_operation_id == operation_id:
                    raise ToolReceiptStateError(f"tool receipt {operation_id} cannot parent itself")
                await self._require_tool_receipt_in_run_uncommitted(
                    conn,
                    operation_id=parent_operation_id,
                    run_id=run_id,
                )
            existing = await (
                await conn.execute(
                    "SELECT * FROM local_tool_receipts WHERE run_id = ? "
                    "AND execution_namespace = ? AND tool_call_id = ?",
                    (run_id, execution_namespace, tool_call_id),
                )
            ).fetchone()
            if existing is not None:
                record = dict(existing)
                existing_parent_operation_id = record.get("parent_operation_id")
                if (
                    record["operation_id"] != operation_id
                    or record["execution_namespace"] != execution_namespace
                    or (
                        parent_operation_id is not None
                        and existing_parent_operation_id not in {None, parent_operation_id}
                    )
                    or record["tool_name"] != tool_name
                    or record["tool_version"] != tool_version
                    or record["arguments_hash"] != arguments_hash
                ):
                    raise ToolReceiptConflictError(
                        f"tool call {tool_call_id} was reused with a different operation identity"
                    )
                if parent_operation_id is not None and existing_parent_operation_id is None:
                    await conn.execute(
                        "UPDATE local_tool_receipts SET parent_operation_id = ? "
                        "WHERE operation_id = ? AND parent_operation_id IS NULL",
                        (parent_operation_id, operation_id),
                    )
                    record["parent_operation_id"] = parent_operation_id
                return record
            now = _now()
            await conn.execute(
                "INSERT INTO local_tool_receipts "
                "(operation_id, run_id, execution_attempt_id, execution_namespace, "
                "parent_operation_id, "
                "tool_call_id, tool_name, "
                "tool_version, arguments_hash, arguments_json, risk, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)",
                (
                    operation_id,
                    run_id,
                    execution_attempt_id,
                    execution_namespace,
                    parent_operation_id,
                    tool_call_id,
                    tool_name,
                    tool_version,
                    arguments_hash,
                    arguments_json,
                    risk,
                    now,
                    now,
                ),
            )
            row = await (
                await conn.execute(
                    "SELECT * FROM local_tool_receipts WHERE operation_id = ?",
                    (operation_id,),
                )
            ).fetchone()
            assert row is not None
            record = dict(row)
            await self._append_subagent_receipt_event_uncommitted(conn, record)
            return record

    async def record_tool_review(
        self,
        *,
        operation_id: str,
        run_id: str,
        decision: str,
        source: str,
        reason: str,
        model: str | None = None,
    ) -> dict[str, Any]:
        if decision not in {"allow", "ask", "deny"}:
            raise ValueError("tool review decision is invalid")
        if source not in {"rule", "llm", "fallback", "user", "run_grant"}:
            raise ValueError("tool review source is invalid")
        async with self.run_write_transaction(run_id) as conn:
            row = await (
                await conn.execute(
                    "SELECT * FROM local_tool_receipts WHERE operation_id = ? AND run_id = ?",
                    (operation_id, run_id),
                )
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown tool receipt: {operation_id}")
            record = dict(row)
            if record.get("review_decision") is not None:
                if (
                    record.get("review_decision") != decision
                    or record.get("review_source") != source
                    or str(record.get("review_reason") or "") != reason
                    or record.get("review_model") != model
                ):
                    raise ToolReceiptStateError(
                        f"tool receipt {operation_id} already has a different review decision"
                    )
                return record
            reviewed_at = _now()
            await conn.execute(
                "UPDATE local_tool_receipts SET review_decision = ?, review_source = ?, "
                "review_reason = ?, review_model = ?, reviewed_at = ?, updated_at = ? "
                "WHERE operation_id = ? AND run_id = ? AND review_decision IS NULL",
                (
                    decision,
                    source,
                    reason,
                    model,
                    reviewed_at,
                    reviewed_at,
                    operation_id,
                    run_id,
                ),
            )
            updated = await (
                await conn.execute(
                    "SELECT * FROM local_tool_receipts WHERE operation_id = ? AND run_id = ?",
                    (operation_id, run_id),
                )
            ).fetchone()
            assert updated is not None
            return dict(updated)

    async def begin_tool_receipt(
        self,
        *,
        operation_id: str,
        run_id: str,
        execution_attempt_id: str,
    ) -> dict[str, Any]:
        execution_lease = _CURRENT_EXECUTION_LEASE.get()
        async with self.run_write_transaction(run_id) as conn:
            row = await (
                await conn.execute(
                    "SELECT * FROM local_tool_receipts WHERE operation_id = ? AND run_id = ?",
                    (operation_id, run_id),
                )
            ).fetchone()
            if row is None:
                raise ToolReceiptStateError(f"tool receipt {operation_id} is missing")
            record = dict(row)
            status = str(record["status"])
            if status in {"completed", "failed", "rejected", "canceled"}:
                return record
            if status in {"running", "outcome_unknown"}:
                raise ToolOutcomeUnknownError(
                    f"tool operation {operation_id} has unresolved outcome {status}"
                )
            if status not in {"prepared", "paused"}:
                raise ToolReceiptStateError(
                    f"tool operation {operation_id} cannot start from {status}"
                )
            now = _now()
            if execution_lease is not None:
                job = await (
                    await conn.execute(
                        "SELECT cancel_requested_at FROM local_run_jobs "
                        "WHERE id = ? AND run_id = ? AND status = 'leased' "
                        "AND lease_owner = ? AND lease_generation = ?",
                        (
                            execution_lease.job_id,
                            run_id,
                            execution_lease.lease_owner,
                            execution_lease.lease_generation,
                        ),
                    )
                ).fetchone()
                if job is None:
                    raise LeaseFenceError("tool execution lease is stale")
                if job["cancel_requested_at"] is not None:
                    if str(record["tool_name"]) == "task":
                        await self._settle_task_descendants_uncommitted(
                            conn,
                            run_id=run_id,
                            operation_id=operation_id,
                            parent_status="canceled",
                            now=now,
                        )
                    await conn.execute(
                        "UPDATE local_tool_receipts SET status = 'canceled', "
                        "error_type = 'RunCanceledBeforeToolStart', completed_at = ?, "
                        "updated_at = ? WHERE operation_id = ? AND run_id = ? "
                        "AND status IN ('prepared', 'paused')",
                        (now, now, operation_id, run_id),
                    )
                    canceled = await (
                        await conn.execute(
                            "SELECT * FROM local_tool_receipts WHERE operation_id = ?",
                            (operation_id,),
                        )
                    ).fetchone()
                    assert canceled is not None
                    record = dict(canceled)
                    await self._append_subagent_receipt_event_uncommitted(conn, record)
                    return record
            await conn.execute(
                "UPDATE local_tool_receipts SET status = 'running', "
                "execution_attempt_id = ?, attempt_count = attempt_count + 1, "
                "started_at = COALESCE(started_at, ?), updated_at = ? "
                "WHERE operation_id = ? AND run_id = ?",
                (execution_attempt_id, now, now, operation_id, run_id),
            )
            updated = await (
                await conn.execute(
                    "SELECT * FROM local_tool_receipts WHERE operation_id = ?",
                    (operation_id,),
                )
            ).fetchone()
            assert updated is not None
            record = dict(updated)
            await self._append_subagent_receipt_event_uncommitted(conn, record)
            return record

    async def settle_tool_receipt(
        self,
        *,
        operation_id: str,
        run_id: str,
        status: str,
        result_json: str | None = None,
        result_hash: str | None = None,
        error_type: str | None = None,
    ) -> dict[str, Any]:
        if status not in {
            "paused",
            "completed",
            "failed",
            "outcome_unknown",
            "rejected",
            "canceled",
        }:
            raise ValueError(f"invalid tool receipt status: {status}")
        async with self.run_write_transaction(run_id) as conn:
            now = _now()
            cursor = await conn.execute(
                "UPDATE local_tool_receipts SET status = ?, result_json = ?, result_hash = ?, "
                "error_type = ?, updated_at = ?, completed_at = CASE WHEN ? = 'paused' "
                "THEN completed_at ELSE ? END WHERE operation_id = ? AND run_id = ? "
                "AND (status = 'running' OR (? IN ('rejected', 'failed') "
                "AND status = 'prepared') OR (? = 'canceled' "
                "AND status IN ('prepared', 'paused')))",
                (
                    status,
                    result_json,
                    result_hash,
                    error_type,
                    now,
                    status,
                    now,
                    operation_id,
                    run_id,
                    status,
                    status,
                ),
            )
            if cursor.rowcount != 1:
                raise ToolReceiptStateError(f"tool operation {operation_id} is not running")
            row = await (
                await conn.execute(
                    "SELECT * FROM local_tool_receipts WHERE operation_id = ?",
                    (operation_id,),
                )
            ).fetchone()
            assert row is not None
            record = dict(row)
            await self._append_subagent_receipt_event_uncommitted(conn, record)
            return record

    async def settle_task_receipt(
        self,
        *,
        operation_id: str,
        run_id: str,
        status: str,
        result_json: str | None = None,
        result_hash: str | None = None,
        error_type: str | None = None,
    ) -> dict[str, Any]:
        """Settle a task and fence every descendant receipt in the same transaction."""
        if status not in {"completed", "failed", "canceled"}:
            raise ValueError(f"invalid task receipt status: {status}")
        async with self.run_write_transaction(run_id) as conn:
            parent = await (
                await conn.execute(
                    "SELECT * FROM local_tool_receipts WHERE operation_id = ? AND run_id = ?",
                    (operation_id, run_id),
                )
            ).fetchone()
            if parent is None or str(parent["tool_name"]) != "task":
                raise ToolReceiptStateError(f"task receipt {operation_id} is missing")

            now = _now()
            await self._settle_task_descendants_uncommitted(
                conn,
                run_id=run_id,
                operation_id=operation_id,
                parent_status=status,
                now=now,
            )

            cursor = await conn.execute(
                "UPDATE local_tool_receipts SET status = ?, result_json = ?, result_hash = ?, "
                "error_type = ?, updated_at = ?, completed_at = ? "
                "WHERE operation_id = ? AND run_id = ? AND (status = 'running' OR "
                "(? = 'canceled' AND status IN ('prepared', 'paused')))",
                (
                    status,
                    result_json,
                    result_hash,
                    error_type,
                    now,
                    now,
                    operation_id,
                    run_id,
                    status,
                ),
            )
            if cursor.rowcount != 1:
                raise ToolReceiptStateError(f"task operation {operation_id} is not running")
            row = await (
                await conn.execute(
                    "SELECT * FROM local_tool_receipts WHERE operation_id = ?",
                    (operation_id,),
                )
            ).fetchone()
            assert row is not None
            record = dict(row)
            await self._append_subagent_receipt_event_uncommitted(conn, record)
            return record

    async def _settle_task_descendants_uncommitted(
        self,
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        operation_id: str,
        parent_status: str,
        now: str,
    ) -> None:
        descendant_error = {
            "completed": "ParentSubagentCompleted",
            "failed": "ParentSubagentFailed",
            "canceled": "ParentSubagentCanceled",
        }[parent_status]
        descendants = await (
            await conn.execute(
                "WITH RECURSIVE descendants(operation_id, depth) AS ("
                "SELECT operation_id, 1 FROM local_tool_receipts "
                "WHERE run_id = ? AND parent_operation_id = ? UNION "
                "SELECT child.operation_id, descendants.depth + 1 "
                "FROM local_tool_receipts AS child JOIN descendants "
                "ON child.parent_operation_id = descendants.operation_id "
                "WHERE child.run_id = ? AND descendants.depth < 64"
                ") SELECT receipt.*, descendants.depth FROM local_tool_receipts AS receipt "
                "JOIN descendants ON descendants.operation_id = receipt.operation_id "
                "ORDER BY descendants.depth DESC, receipt.created_at, receipt.operation_id",
                (run_id, operation_id, run_id),
            )
        ).fetchall()
        for row in descendants:
            child = dict(row)
            child_status = str(child["status"])
            if child_status in {"prepared", "paused"}:
                settled_status = "canceled"
            elif child_status == "running":
                settled_status = "outcome_unknown"
            else:
                continue
            await conn.execute(
                "UPDATE local_tool_receipts SET status = ?, error_type = ?, "
                "completed_at = ?, updated_at = ? WHERE operation_id = ? AND run_id = ?",
                (
                    settled_status,
                    descendant_error,
                    now,
                    now,
                    child["operation_id"],
                    run_id,
                ),
            )
            await self._append_subagent_receipt_event_uncommitted(
                conn,
                {
                    **child,
                    "status": settled_status,
                    "error_type": descendant_error,
                    "completed_at": now,
                    "updated_at": now,
                },
            )

    async def reconcile_tool_receipt(
        self,
        *,
        operation_id: str,
        run_id: str,
        decision: str,
        result_json: str | None = None,
        result_hash: str | None = None,
    ) -> dict[str, Any]:
        """Resolve an uncertain side effect without guessing or blind retry."""
        if decision not in {"confirmed_completed", "retry_not_executed", "abort"}:
            raise ValueError(f"invalid tool reconciliation decision: {decision}")
        status = {
            "confirmed_completed": "completed",
            "retry_not_executed": "prepared",
            "abort": "failed",
        }[decision]
        settled_result_json = None if decision == "retry_not_executed" else result_json
        settled_result_hash = None if decision == "retry_not_executed" else result_hash
        error_type = "ReconciledByUser" if decision == "abort" else None
        async with self.run_write_transaction(run_id) as conn:
            now = _now()
            cursor = await conn.execute(
                "UPDATE local_tool_receipts SET status = ?, result_json = ?, "
                "result_hash = ?, error_type = ?, updated_at = ?, "
                "completed_at = CASE WHEN ? = 'prepared' THEN NULL ELSE ? END "
                "WHERE operation_id = ? AND run_id = ? AND status = 'outcome_unknown'",
                (
                    status,
                    settled_result_json,
                    settled_result_hash,
                    error_type,
                    now,
                    status,
                    now,
                    operation_id,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ToolReceiptStateError(
                    f"tool operation {operation_id} is not awaiting reconciliation"
                )
            row = await (
                await conn.execute(
                    "SELECT * FROM local_tool_receipts WHERE operation_id = ?",
                    (operation_id,),
                )
            ).fetchone()
            assert row is not None
            record = dict(row)
            await self._append_subagent_receipt_event_uncommitted(conn, record)
            return record

    async def tool_execution_cancel_requested(self, run_id: str) -> bool:
        """Fence a tool start against the currently leased run job."""
        lease = _CURRENT_EXECUTION_LEASE.get()
        if lease is None:
            return False
        if lease.run_id != run_id:
            raise LeaseFenceError("tool execution is missing its run job lease")
        row = await (
            await self._conn.execute(
                "SELECT status, lease_owner, lease_generation, lease_expires_at, "
                "cancel_requested_at FROM local_run_jobs WHERE id = ? AND run_id = ?",
                (lease.job_id, run_id),
            )
        ).fetchone()
        if (
            row is None
            or row["status"] != "leased"
            or row["lease_owner"] != lease.lease_owner
            or int(row["lease_generation"] or 0) != lease.lease_generation
            or str(row["lease_expires_at"] or "") <= _now()
        ):
            raise LeaseFenceError("tool execution lease is stale")
        return row["cancel_requested_at"] is not None

    async def get_tool_receipt(self, operation_id: str) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "SELECT * FROM local_tool_receipts WHERE operation_id = ?",
                (operation_id,),
            )
        ).fetchone()
        return dict(row) if row is not None else None

    async def list_tool_receipts_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT * FROM local_tool_receipts WHERE run_id = ? ORDER BY created_at, operation_id",
                (run_id,),
            )
        ).fetchall()
        return [dict(row) for row in rows]
