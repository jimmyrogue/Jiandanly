"""Durable Runtime waits: plans, permissions, questions, and reconciliation."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from ..permission_policy import require_allowed_permission_scope
from .codec import encode_payload as _encode_payload
from .database import utc_now as _now
from .errors import PermissionDecisionConflictError, WaitDecisionConflictError
from .ids import new_id as _new_id
from .wait_questions import QuestionWaitStore
from .wait_reconciliation import ToolReconciliationWaitStore


def _decode_plan_approval_record(record: dict[str, Any]) -> dict[str, Any]:
    try:
        todos = json.loads(str(record.get("todos_json") or "[]"))
    except json.JSONDecodeError:
        todos = []
    return {
        **record,
        "todos": todos if isinstance(todos, list) else [],
    }


class WaitStore(QuestionWaitStore, ToolReconciliationWaitStore):
    # --- plan approvals ---

    async def create_plan_approval(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        todos: list[dict[str, Any]],
        summary: str = "",
        wait_cycle_id: str | None = None,
        interrupt_id: str | None = None,
    ) -> dict[str, Any]:
        record = {
            "id": _new_id("plan"),
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "wait_cycle_id": wait_cycle_id,
            "interrupt_id": interrupt_id,
            "todos_json": json.dumps(todos, ensure_ascii=False, default=str),
            "summary": summary,
            "status": "pending",
            "instructions": None,
            "created_at": _now(),
            "resolved_at": None,
        }
        record["wait_cycle_id"] = record["wait_cycle_id"] or record["id"]
        record["interrupt_id"] = record["interrupt_id"] or record["id"]
        try:
            async with self.run_write_transaction(run_id) as conn:
                await conn.execute(
                    "INSERT INTO local_plan_approvals "
                    "(id, run_id, tool_call_id, wait_cycle_id, interrupt_id, todos_json, "
                    "summary, status, instructions, created_at, resolved_at) "
                    "VALUES (:id, :run_id, :tool_call_id, :wait_cycle_id, :interrupt_id, "
                    ":todos_json, :summary, :status, :instructions, :created_at, :resolved_at)",
                    record,
                )
                await conn.execute(
                    "INSERT INTO local_wait_candidates "
                    "(id, run_id, kind, wait_cycle_id, interrupt_id, position, status, "
                    "payload_json, decision_json, created_at, resolved_at) "
                    "VALUES (?, ?, 'plan', ?, ?, 0, 'pending', ?, NULL, ?, NULL)",
                    (
                        record["id"],
                        run_id,
                        record["wait_cycle_id"],
                        record["interrupt_id"],
                        record["todos_json"],
                        record["created_at"],
                    ),
                )
        except aiosqlite.IntegrityError:
            existing = await self.get_plan_approval_by_tool_call(
                run_id=run_id,
                tool_call_id=tool_call_id,
            )
            assert existing is not None
            if (
                wait_cycle_id
                and interrupt_id
                and (not existing.get("wait_cycle_id") or not existing.get("interrupt_id"))
            ):
                async with self.run_write_transaction(run_id) as conn:
                    await conn.execute(
                        "UPDATE local_plan_approvals SET wait_cycle_id = ?, interrupt_id = ? "
                        "WHERE id = ?",
                        (wait_cycle_id, interrupt_id, existing["id"]),
                    )
                    await conn.execute(
                        "INSERT OR IGNORE INTO local_wait_candidates "
                        "(id, run_id, kind, wait_cycle_id, interrupt_id, position, status, "
                        "payload_json, decision_json, created_at, resolved_at) "
                        "VALUES (?, ?, 'plan', ?, ?, 0, 'pending', ?, NULL, ?, NULL)",
                        (
                            existing["id"],
                            run_id,
                            wait_cycle_id,
                            interrupt_id,
                            existing["todos_json"],
                            existing["created_at"],
                        ),
                    )
                existing = {
                    **existing,
                    "wait_cycle_id": wait_cycle_id,
                    "interrupt_id": interrupt_id,
                }
            return existing
        return _decode_plan_approval_record(record)

    async def get_plan_approval(self, approval_id: str) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM local_plan_approvals WHERE id = ?",
            (approval_id,),
        )
        row = await cursor.fetchone()
        return _decode_plan_approval_record(dict(row)) if row else None

    async def get_plan_approval_by_tool_call(
        self,
        *,
        run_id: str,
        tool_call_id: str,
    ) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM local_plan_approvals WHERE run_id = ? AND tool_call_id = ?",
            (run_id, tool_call_id),
        )
        row = await cursor.fetchone()
        return _decode_plan_approval_record(dict(row)) if row else None

    # --- permissions (HumanInTheLoop pause record) ---

    async def create_permission(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        tool_version: str = "",
        operation_id: str | None = None,
        arguments_hash: str | None = None,
        risk: str | None = None,
        wait_cycle_id: str | None = None,
        interrupt_id: str | None = None,
        action_index: int = 0,
        scope: str = "once",
    ) -> dict[str, Any]:
        """Get or create the approval record for one concrete tool operation.

        The returned `id` is what gets surfaced to the renderer as the
        `request_id` in the `permission.required` SSE event, and what
        the client posts back to `POST /v1/permissions/{id}` to
        approve or deny. Without this row, the client cannot look up
        which paused run to resume.
        """
        if operation_id:
            cursor = await self._conn.execute(
                "SELECT * FROM local_permissions WHERE run_id = ? AND operation_id = ?",
                (run_id, operation_id),
            )
            row = await cursor.fetchone()
            if row is not None:
                existing = dict(row)
                if (
                    existing.get("tool_call_id") != tool_call_id
                    or existing.get("tool_name") != tool_name
                    or str(existing.get("tool_version") or "") != tool_version
                    or existing.get("arguments_hash") != arguments_hash
                ):
                    raise PermissionDecisionConflictError(
                        "permission operation identity was reused with different content"
                    )
                return existing

        record = {
            "id": _new_id("perm"),
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "wait_cycle_id": wait_cycle_id,
            "interrupt_id": interrupt_id,
            "action_index": max(0, int(action_index)),
            "operation_id": operation_id,
            "tool_name": tool_name,
            "tool_version": tool_version,
            "arguments_hash": arguments_hash,
            "arguments_json": json.dumps(arguments or {}, ensure_ascii=False, default=str),
            "risk": risk,
            "decision_json": None,
            "status": "pending",
            "scope": scope,
            "grant_max_uses": 0,
            "grant_use_count": 0,
            "grant_expires_at": None,
            "created_at": _now(),
            "resolved_at": None,
        }
        record["wait_cycle_id"] = record["wait_cycle_id"] or record["id"]
        record["interrupt_id"] = record["interrupt_id"] or record["id"]
        async with self.run_write_transaction(run_id) as conn:
            await conn.execute(
                "INSERT INTO local_permissions "
                "(id, run_id, tool_call_id, wait_cycle_id, interrupt_id, action_index, "
                "operation_id, tool_name, tool_version, arguments_hash, "
                " arguments_json, risk, decision_json, status, scope, grant_max_uses, "
                " grant_use_count, grant_expires_at, created_at, resolved_at) "
                "VALUES (:id, :run_id, :tool_call_id, :wait_cycle_id, :interrupt_id, "
                ":action_index, :operation_id, :tool_name, :tool_version, "
                "        :arguments_hash, :arguments_json, :risk, :decision_json, :status, :scope, "
                "        :grant_max_uses, :grant_use_count, :grant_expires_at, "
                "        :created_at, :resolved_at)",
                record,
            )
            await conn.execute(
                "INSERT INTO local_wait_candidates "
                "(id, run_id, kind, wait_cycle_id, interrupt_id, position, status, "
                "payload_json, decision_json, created_at, resolved_at) "
                "VALUES (?, ?, 'tool_review', ?, ?, ?, 'pending', ?, NULL, ?, NULL)",
                (
                    record["id"],
                    run_id,
                    record["wait_cycle_id"],
                    record["interrupt_id"],
                    record["action_index"],
                    json.dumps(
                        {
                            "tool_call_id": tool_call_id,
                            "operation_id": operation_id,
                            "tool_name": tool_name,
                            "tool_version": tool_version,
                            "arguments_hash": arguments_hash,
                            "arguments": arguments or {},
                            "risk": risk,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    record["created_at"],
                ),
            )
        return record

    async def get_permission(self, permission_id: str) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM local_permissions WHERE id = ?", (permission_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_permission_for_operation(
        self, *, run_id: str, operation_id: str
    ) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM local_permissions WHERE run_id = ? AND operation_id = ?",
            (run_id, operation_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def resolve_permission(
        self,
        permission_id: str,
        *,
        status: str,
        scope: str | None = None,
        decision: dict[str, Any] | None = None,
        event_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if status not in {"approved", "denied"}:
            raise ValueError("permission status must be approved or denied")
        record = await self.get_permission(permission_id)
        if record is None:
            return None
        current_status = str(record.get("status") or "")
        current_scope = str(record.get("scope") or "once")
        requested_scope = scope or current_scope
        require_allowed_permission_scope(
            tool_name=str(record["tool_name"]),
            risk=record.get("risk"),
            status=status,
            scope=requested_scope,
        )
        grant_max_uses = -1 if requested_scope == "run" and status == "approved" else 0
        grant_expires_at = None
        decision_json = json.dumps(
            decision or ({"type": "approve"} if status == "approved" else {"type": "reject"}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if current_status != "pending":
            if (
                current_status != status
                or current_scope != requested_scope
                or str(record.get("decision_json") or "") != decision_json
            ):
                raise PermissionDecisionConflictError(
                    "permission was already resolved with a different decision"
                )
            return record
        async with self.run_write_transaction(str(record["run_id"])) as conn:
            run = await (
                await conn.execute(
                    "SELECT status FROM local_runs WHERE id = ?",
                    (record["run_id"],),
                )
            ).fetchone()
            if run is None or run["status"] in {
                "completed",
                "failed",
                "canceled",
                "cleanup_required",
            }:
                raise WaitDecisionConflictError("run is not awaiting a decision")
            cursor = await conn.execute(
                "UPDATE local_permissions SET status = ?, scope = ?, decision_json = ?, "
                "grant_max_uses = ?, grant_expires_at = ?, resolved_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (
                    status,
                    requested_scope,
                    decision_json,
                    grant_max_uses,
                    grant_expires_at,
                    _now(),
                    permission_id,
                ),
            )
            if cursor.rowcount != 1:
                raise PermissionDecisionConflictError("permission was resolved concurrently")
            wait_cursor = await conn.execute(
                "UPDATE local_wait_candidates SET status = 'resolved', "
                "decision_json = ?, resolved_at = ? WHERE id = ? AND status = 'pending'",
                (decision_json, _now(), permission_id),
            )
            if wait_cursor.rowcount != 1:
                raise WaitDecisionConflictError(
                    "permission wait candidate was resolved concurrently"
                )
            if event_payload is not None:
                await self._append_event_uncommitted(
                    conn,
                    str(record["run_id"]),
                    "permission.resolved",
                    payload_json=_encode_payload(event_payload),
                    created_at=_now(),
                )
        return await self.get_permission(permission_id)

    async def consume_run_permission_grant(
        self,
        *,
        run_id: str,
        operation_id: str,
        tool_name: str,
        tool_version: str = "",
        risk: str,
    ) -> bool:
        """Atomically apply one tool-level grant for the rest of this run.

        Every invocation still passes schema, capability, workspace, version,
        and risk checks before reaching this grant.
        """
        async with self.run_write_transaction(run_id) as conn:
            existing = await (
                await conn.execute(
                    "SELECT permission_id FROM local_permission_grant_uses "
                    "WHERE run_id = ? AND operation_id = ?",
                    (run_id, operation_id),
                )
            ).fetchone()
            if existing is not None:
                return True
            grant = await (
                await conn.execute(
                    "SELECT id FROM local_permissions "
                    "WHERE run_id = ? AND tool_name = ? AND tool_version = ? "
                    "AND risk = ? "
                    "AND status = 'approved' AND scope = 'run' "
                    "ORDER BY resolved_at DESC LIMIT 1",
                    (run_id, tool_name, tool_version, risk),
                )
            ).fetchone()
            if grant is None:
                return False
            permission_id = str(grant[0])
            await conn.execute(
                "INSERT INTO local_permission_grant_uses "
                "(permission_id, run_id, operation_id, created_at) VALUES (?, ?, ?, ?)",
                (permission_id, run_id, operation_id, _now()),
            )
            cursor = await conn.execute(
                "UPDATE local_permissions SET grant_use_count = grant_use_count + 1 WHERE id = ?",
                (permission_id,),
            )
            if cursor.rowcount != 1:
                raise PermissionDecisionConflictError("permission grant changed concurrently")
            return True

    async def wait_cycle_resume_payload(
        self,
        *,
        run_id: str,
        wait_cycle_id: str,
    ) -> dict[str, Any] | None:
        """Build LangGraph's interrupt-id keyed resume payload when complete."""
        return await self._wait_cycle_resume_payload_uncommitted(
            self._conn,
            run_id=run_id,
            wait_cycle_id=wait_cycle_id,
        )

    @staticmethod
    async def _wait_cycle_resume_payload_uncommitted(
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        wait_cycle_id: str,
    ) -> dict[str, Any] | None:
        cursor = await conn.execute(
            "SELECT * FROM local_wait_candidates "
            "WHERE run_id = ? AND wait_cycle_id = ? "
            "ORDER BY interrupt_id, position",
            (run_id, wait_cycle_id),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        if not rows or any(row.get("status") != "resolved" for row in rows):
            return None
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row["interrupt_id"]), []).append(row)
        resume: dict[str, Any] = {}
        for interrupt_id, candidates in grouped.items():
            kinds = {str(candidate.get("kind") or "") for candidate in candidates}
            if kinds == {"tool_review"}:
                resume[interrupt_id] = {
                    "decisions": [
                        json.loads(str(candidate.get("decision_json") or "{}"))
                        for candidate in candidates
                    ]
                }
            elif kinds == {"question"} and len(candidates) == 1:
                resume[interrupt_id] = json.loads(str(candidates[0].get("decision_json") or "{}"))
            elif kinds == {"tool_reconciliation"} and len(candidates) == 1:
                resume[interrupt_id] = json.loads(str(candidates[0].get("decision_json") or "{}"))
            elif kinds == {"plan"} and len(candidates) == 1:
                resume[interrupt_id] = json.loads(str(candidates[0].get("decision_json") or "{}"))
            else:
                raise WaitDecisionConflictError(
                    f"unsupported wait candidate group for interrupt {interrupt_id}"
                )
        return resume

    async def latest_resolved_wait_cycle_payload(self, run_id: str) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "SELECT wait_cycle_id FROM local_wait_candidates WHERE run_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (run_id,),
            )
        ).fetchone()
        if row is None:
            return None
        return await self.wait_cycle_resume_payload(
            run_id=run_id,
            wait_cycle_id=str(row[0]),
        )

    async def list_permissions_for_run(self, run_id: str) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(
            "SELECT * FROM local_permissions WHERE run_id = ? ORDER BY created_at",
            (run_id,),
        )
        return [
            {**dict(row), "arguments": json.loads(row["arguments_json"] or "{}")}
            for row in await cursor.fetchall()
        ]
