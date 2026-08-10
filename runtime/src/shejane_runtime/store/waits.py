"""Durable Runtime waits: plans, permissions, questions, and reconciliation."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from ..permission_policy import require_allowed_permission_scope
from .codec import encode_payload as _encode_payload
from .codec import json_payload as _json_payload
from .database import SqliteDatabase
from .database import utc_now as _now
from .errors import PermissionDecisionConflictError, WaitDecisionConflictError
from .ids import new_id as _new_id


def _decode_plan_approval_record(record: dict[str, Any]) -> dict[str, Any]:
    try:
        todos = json.loads(str(record.get("todos_json") or "[]"))
    except json.JSONDecodeError:
        todos = []
    return {
        **record,
        "todos": todos if isinstance(todos, list) else [],
    }


class WaitStore(SqliteDatabase):
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

    # --- questions (user.ask interrupt record) ---

    async def create_question(
        self,
        *,
        run_id: str,
        tool_call_id: str | None,
        questions: list[dict[str, Any]],
        wait_cycle_id: str | None = None,
        interrupt_id: str | None = None,
    ) -> dict[str, Any]:
        """Record a `user.ask` interrupt.

        `questions` is a list to allow future multi-question interrupts;
        today user.ask emits one. The returned `id` is the `request_id`
        the client posts back via `POST /v1/questions/{id}` with
        `{answers}`.
        """
        if interrupt_id:
            existing = await (
                await self._conn.execute(
                    "SELECT * FROM local_questions WHERE run_id = ? AND interrupt_id = ?",
                    (run_id, interrupt_id),
                )
            ).fetchone()
            if existing is not None:
                return dict(existing)
        record = {
            "id": _new_id("q"),
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "wait_cycle_id": wait_cycle_id,
            "interrupt_id": interrupt_id,
            "questions_json": json.dumps(questions, ensure_ascii=False, default=str),
            "status": "pending",
            "answers_json": None,
            "created_at": _now(),
            "answered_at": None,
        }
        record["wait_cycle_id"] = record["wait_cycle_id"] or record["id"]
        record["interrupt_id"] = record["interrupt_id"] or record["id"]
        async with self.run_write_transaction(run_id) as conn:
            await conn.execute(
                "INSERT INTO local_questions (id, run_id, tool_call_id, wait_cycle_id, "
                " interrupt_id, questions_json, "
                " status, answers_json, created_at, answered_at) "
                "VALUES (:id, :run_id, :tool_call_id, :wait_cycle_id, :interrupt_id, "
                "        :questions_json, :status, "
                "        :answers_json, :created_at, :answered_at)",
                record,
            )
            await conn.execute(
                "INSERT INTO local_wait_candidates "
                "(id, run_id, kind, wait_cycle_id, interrupt_id, position, status, "
                "payload_json, decision_json, created_at, resolved_at) "
                "VALUES (?, ?, 'question', ?, ?, 0, 'pending', ?, NULL, ?, NULL)",
                (
                    record["id"],
                    run_id,
                    record["wait_cycle_id"],
                    record["interrupt_id"],
                    record["questions_json"],
                    record["created_at"],
                ),
            )
        return record

    async def get_question(self, question_id: str) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM local_questions WHERE id = ?", (question_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def count_questions_for_run(self, run_id: str) -> int:
        row = await (
            await self._conn.execute(
                "SELECT COUNT(*) FROM local_questions WHERE run_id = ?",
                (run_id,),
            )
        ).fetchone()
        return int(row[0] if row else 0)

    async def list_answered_question_choices_for_run(
        self,
        *,
        principal_id: str,
        run_id: str,
    ) -> list[dict[str, Any]]:
        """Return the source run's resolved choices without crossing principals.

        Later answers replace earlier answers for the same question. This keeps
        retry context compact when a failed run asked the same thing repeatedly.
        """
        rows = await (
            await self._conn.execute(
                "SELECT q.answers_json FROM local_questions q "
                "JOIN local_runs r ON r.id = q.run_id "
                "WHERE r.principal_id = ? AND q.run_id = ? "
                "AND q.status = 'answered' AND q.answers_json IS NOT NULL "
                "ORDER BY q.created_at, q.id",
                (principal_id, run_id),
            )
        ).fetchall()
        choices: dict[str, list[str]] = {}
        for row in rows:
            try:
                answers = json.loads(str(row["answers_json"]))
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(answers, dict):
                continue
            for raw_question, raw_values in answers.items():
                question = str(raw_question).strip()
                if not question:
                    continue
                values = raw_values if isinstance(raw_values, list) else [raw_values]
                normalized = [
                    str(value).strip()
                    for value in values
                    if value is not None and str(value).strip()
                ]
                if normalized:
                    choices[question] = normalized
        return [{"question": question, "answers": answers} for question, answers in choices.items()]

    async def answer_question(
        self,
        question_id: str,
        *,
        answers: dict[str, Any],
        event_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        record = await self.get_question(question_id)
        if record is None:
            return None
        answers_json = json.dumps(
            answers,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if record.get("status") != "pending":
            try:
                existing = json.dumps(
                    json.loads(record.get("answers_json") or "{}"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            except json.JSONDecodeError:
                existing = str(record.get("answers_json") or "")
            if existing != answers_json:
                raise WaitDecisionConflictError(
                    "question was already answered with different content"
                )
            return record
        now = _now()
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
                "UPDATE local_questions SET status = 'answered', answers_json = ?, "
                "answered_at = ? WHERE id = ? AND status = 'pending'",
                (answers_json, now, question_id),
            )
            wait_cursor = await conn.execute(
                "UPDATE local_wait_candidates SET status = 'resolved', "
                "decision_json = ?, resolved_at = ? WHERE id = ? AND status = 'pending'",
                (answers_json, now, question_id),
            )
            if cursor.rowcount != 1 or wait_cursor.rowcount != 1:
                raise WaitDecisionConflictError("question was answered concurrently")
            if event_payload is not None:
                await self._append_event_uncommitted(
                    conn,
                    str(record["run_id"]),
                    "question.answered",
                    payload_json=_encode_payload(event_payload),
                    created_at=now,
                )
        return await self.get_question(question_id)

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
