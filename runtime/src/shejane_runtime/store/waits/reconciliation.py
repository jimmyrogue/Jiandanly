"""Durable tool-outcome reconciliation waits."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from ..codec import encode_payload as _encode_payload
from ..codec import json_payload as _json_payload
from ..database import SqliteDatabase
from ..database import utc_now as _now
from ..errors import WaitDecisionConflictError


class ToolReconciliationWaitStore(SqliteDatabase):
    async def list_wait_candidates_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT * FROM local_wait_candidates WHERE run_id = ? ORDER BY created_at, id",
                (run_id,),
            )
        ).fetchall()
        return [dict(row) for row in rows]

    async def get_wait_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "SELECT * FROM local_wait_candidates WHERE id = ?", (candidate_id,)
            )
        ).fetchone()
        return dict(row) if row else None

    async def create_tool_reconciliation(
        self,
        *,
        run_id: str,
        operation_id: str,
        wait_cycle_id: str,
        interrupt_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        existing = await self.get_wait_candidate(operation_id)
        if existing is not None:
            if existing.get("run_id") != run_id or existing.get("kind") != "tool_reconciliation":
                raise WaitDecisionConflictError(
                    "tool reconciliation identity was reused with different content"
                )
            return existing
        record = {
            "id": operation_id,
            "run_id": run_id,
            "kind": "tool_reconciliation",
            "wait_cycle_id": wait_cycle_id,
            "interrupt_id": interrupt_id,
            "position": 0,
            "status": "pending",
            "payload_json": json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            "decision_json": None,
            "created_at": _now(),
            "resolved_at": None,
        }
        async with self.run_write_transaction(run_id) as conn:
            await conn.execute(
                "INSERT INTO local_wait_candidates "
                "(id, run_id, kind, wait_cycle_id, interrupt_id, position, status, "
                "payload_json, decision_json, created_at, resolved_at) "
                "VALUES (:id, :run_id, :kind, :wait_cycle_id, :interrupt_id, :position, "
                ":status, :payload_json, :decision_json, :created_at, :resolved_at)",
                record,
            )
        return record

    async def resolve_tool_reconciliation(
        self,
        candidate_id: str,
        *,
        decision: str,
        current_result_json: str | None,
        current_result_hash: str | None,
        prior_result_json: str,
        prior_result_hash: str,
    ) -> dict[str, Any] | None:
        if decision not in {"confirmed_completed", "retry_not_executed", "abort"}:
            raise ValueError(f"invalid tool reconciliation decision: {decision}")
        record = await self.get_wait_candidate(candidate_id)
        if record is None or record.get("kind") != "tool_reconciliation":
            return None
        current_run_id = str(record["run_id"])
        async with self.run_write_transaction(current_run_id) as conn:
            updated, _resolved = await self._resolve_tool_reconciliation_uncommitted(
                conn,
                candidate_id=candidate_id,
                decision=decision,
                current_result_json=current_result_json,
                current_result_hash=current_result_hash,
                prior_result_json=prior_result_json,
                prior_result_hash=prior_result_hash,
            )
            return updated

    async def _resolve_tool_reconciliation_uncommitted(
        self,
        conn: aiosqlite.Connection,
        *,
        candidate_id: str,
        decision: str,
        current_result_json: str | None,
        current_result_hash: str | None,
        prior_result_json: str,
        prior_result_hash: str,
    ) -> tuple[dict[str, Any], bool]:
        record = await (
            await conn.execute(
                "SELECT * FROM local_wait_candidates WHERE id = ?",
                (candidate_id,),
            )
        ).fetchone()
        if record is None or record["kind"] != "tool_reconciliation":
            raise KeyError(candidate_id)
        decision_json = _encode_payload({"decision": decision})
        if record["status"] != "pending":
            if str(record["decision_json"] or "") != decision_json:
                raise WaitDecisionConflictError(
                    "tool reconciliation was already resolved differently"
                )
            return dict(record), False
        payload = _json_payload(record["payload_json"])
        prior_operation_id = str(payload.get("prior_operation_id") or candidate_id)
        current_run_id = str(record["run_id"])
        current_receipt = await (
            await conn.execute(
                "SELECT * FROM local_tool_receipts WHERE operation_id = ? AND run_id = ?",
                (candidate_id, current_run_id),
            )
        ).fetchone()
        prior_receipt = await (
            await conn.execute(
                "SELECT * FROM local_tool_receipts WHERE operation_id = ?",
                (prior_operation_id,),
            )
        ).fetchone()
        if current_receipt is None or prior_receipt is None:
            raise WaitDecisionConflictError("tool reconciliation receipt is missing")
        prior_run_id = str(prior_receipt["run_id"])
        if prior_run_id != current_run_id:
            ancestor = await (
                await conn.execute(
                    "WITH RECURSIVE lineage(id, owner, depth) AS ("
                    "SELECT parent_run_id, principal_id, 0 FROM local_runs "
                    "WHERE id = ? AND parent_run_id IS NOT NULL UNION ALL "
                    "SELECT parent.parent_run_id, lineage.owner, lineage.depth + 1 "
                    "FROM local_runs AS parent JOIN lineage ON parent.id = lineage.id "
                    "WHERE parent.principal_id = lineage.owner "
                    "AND parent.parent_run_id IS NOT NULL AND lineage.depth < 64"
                    ") SELECT 1 FROM lineage JOIN local_runs AS ancestor "
                    "ON ancestor.id = lineage.id AND ancestor.principal_id = lineage.owner "
                    "WHERE lineage.id = ? LIMIT 1",
                    (current_run_id, prior_run_id),
                )
            ).fetchone()
            if ancestor is None:
                raise WaitDecisionConflictError(
                    "tool reconciliation source is not an owned ancestor"
                )
        now = _now()
        prior_status = "completed" if decision == "confirmed_completed" else "failed"
        prior_cursor = await conn.execute(
            "UPDATE local_tool_receipts SET status = ?, result_json = ?, result_hash = ?, "
            "error_type = ?, completed_at = ?, updated_at = ? "
            "WHERE operation_id = ? AND status = 'outcome_unknown'",
            (
                prior_status,
                prior_result_json,
                prior_result_hash,
                None if prior_status == "completed" else "ReconciledByUser",
                now,
                now,
                prior_operation_id,
            ),
        )
        if prior_cursor.rowcount != 1:
            raise WaitDecisionConflictError(
                "tool reconciliation source is no longer outcome_unknown"
            )
        projected_operation_ids = [prior_operation_id]
        if prior_operation_id == candidate_id and decision == "retry_not_executed":
            await conn.execute(
                "UPDATE local_tool_receipts SET status = 'prepared', result_json = NULL, "
                "result_hash = NULL, error_type = NULL, completed_at = NULL, updated_at = ? "
                "WHERE operation_id = ?",
                (now, candidate_id),
            )
        elif prior_operation_id != candidate_id and decision != "retry_not_executed":
            current_status = "completed" if decision == "confirmed_completed" else "failed"
            current_cursor = await conn.execute(
                "UPDATE local_tool_receipts SET status = ?, result_json = ?, "
                "result_hash = ?, error_type = ?, completed_at = ?, updated_at = ? "
                "WHERE operation_id = ? AND run_id = ? AND status = 'prepared'",
                (
                    current_status,
                    current_result_json,
                    current_result_hash,
                    None if current_status == "completed" else "ReconciledByUser",
                    now,
                    now,
                    candidate_id,
                    current_run_id,
                ),
            )
            if current_cursor.rowcount != 1:
                raise WaitDecisionConflictError(
                    "current tool reconciliation receipt is no longer prepared"
                )
            projected_operation_ids.append(candidate_id)
        cursor = await conn.execute(
            "UPDATE local_wait_candidates SET status = 'resolved', decision_json = ?, "
            "resolved_at = ? WHERE id = ? AND status = 'pending'",
            (decision_json, now, candidate_id),
        )
        if cursor.rowcount != 1:
            raise WaitDecisionConflictError("tool reconciliation was resolved concurrently")
        for operation_id in projected_operation_ids:
            receipt = await (
                await conn.execute(
                    "SELECT * FROM local_tool_receipts WHERE operation_id = ?",
                    (operation_id,),
                )
            ).fetchone()
            assert receipt is not None
            await self._append_subagent_receipt_event_uncommitted(conn, dict(receipt))
        updated = await (
            await conn.execute(
                "SELECT * FROM local_wait_candidates WHERE id = ?",
                (candidate_id,),
            )
        ).fetchone()
        assert updated is not None
        return dict(updated), True
