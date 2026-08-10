"""P10 durable tool-execution receipts and receipt projections."""

from __future__ import annotations

from typing import Any

import aiosqlite

from .database import CURRENT_EXECUTION_LEASE as _CURRENT_EXECUTION_LEASE
from .database import LeaseFenceError
from .database import utc_now as _now
from .errors import (
    ToolOutcomeUnknownError,
    ToolReceiptConflictError,
    ToolReceiptStateError,
)
from .subagent_receipts import SubagentReceiptStore


class ToolReceiptStore(SubagentReceiptStore):
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
