"""Immutable commands that resolve Runtime-owned wait candidates."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from ..permission_policy import require_allowed_permission_scope
from .codec import encode_payload as _encode_payload
from .database import SqliteDatabase
from .database import configure_connection as _configure_connection
from .database import utc_now as _now
from .errors import WaitDecisionConflictError, WorkspaceAdmissionError


class RunWaitCommandStore(SqliteDatabase):
    async def request_question_answer_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        question_id: str,
        answers: dict[str, list[str]],
    ) -> tuple[dict[str, Any], bool]:
        payload_json = _encode_payload(
            {"type": "question.answer", "question_id": question_id, "answers": answers}
        )
        answers_json = json.dumps(
            answers,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type="question.answer",
                    payload_json=payload_json,
                )
                if existing is not None:
                    await conn.rollback()
                    return existing, False

                record = await (
                    await conn.execute(
                        "SELECT q.run_id, q.wait_cycle_id, q.status AS question_status, "
                        "q.answers_json, r.status AS run_status, r.principal_id, "
                        "r.goal, r.user_input, r.workspace_path, r.mode, r.history_json, "
                        "r.settings_json, r.metadata_json, r.id AS id, r.run_kind, "
                        "r.root_run_id, r.agent_definition_id, r.agent_definition_version, "
                        "r.collaboration_depth, r.collaboration_policy_json "
                        "FROM local_questions q JOIN local_runs r ON r.id = q.run_id "
                        "WHERE q.id = ? AND r.principal_id = ?",
                        (question_id, principal_id),
                    )
                ).fetchone()
                if record is None:
                    raise KeyError(f"unknown question: {question_id}")
                run_id = str(record["run_id"])
                workspace_error = await self._workspace_owner_error(
                    conn,
                    principal_id=principal_id,
                    path=record["workspace_path"],
                ) or await self._workspace_path_error(record["workspace_path"])
                if workspace_error is not None:
                    raise WorkspaceAdmissionError(workspace_error)

                if record["question_status"] == "pending":
                    if record["run_status"] not in {"waiting_permission", "waiting_input"}:
                        raise WaitDecisionConflictError("run is not awaiting a question answer")
                    now = _now()
                    question_cursor = await conn.execute(
                        "UPDATE local_questions SET status = 'answered', answers_json = ?, "
                        "answered_at = ? WHERE id = ? AND status = 'pending'",
                        (answers_json, now, question_id),
                    )
                    wait_cursor = await conn.execute(
                        "UPDATE local_wait_candidates SET status = 'resolved', "
                        "decision_json = ?, resolved_at = ? "
                        "WHERE id = ? AND status = 'pending'",
                        (answers_json, now, question_id),
                    )
                    if question_cursor.rowcount != 1 or wait_cursor.rowcount != 1:
                        raise WaitDecisionConflictError("question was answered concurrently")
                    await self._append_event_uncommitted(
                        conn,
                        run_id,
                        "question.answered",
                        payload_json=_encode_payload(
                            {"request_id": question_id, "answers": answers}
                        ),
                        created_at=now,
                    )
                elif (
                    record["question_status"] != "answered"
                    or str(record["answers_json"] or "") != answers_json
                ):
                    raise WaitDecisionConflictError(
                        "question was already resolved with different content"
                    )

                resume_payload = await self._wait_cycle_resume_payload_uncommitted(
                    conn,
                    run_id=run_id,
                    wait_cycle_id=str(record["wait_cycle_id"]),
                )
                resumed = record["run_status"] in {
                    "queued",
                    "running",
                    "completed",
                    "failed",
                }
                if (
                    record["run_status"] in {"waiting_permission", "waiting_input"}
                    and resume_payload is not None
                ):
                    active_job = await (
                        await conn.execute(
                            "SELECT * FROM local_run_jobs WHERE run_id = ? "
                            "AND status IN ('pending', 'leased')",
                            (run_id,),
                        )
                    ).fetchone()
                    if active_job is None:
                        job = self._new_run_job_record(
                            run_id=run_id,
                            kind="resume",
                            input_payload=self._run_job_input(dict(record)),
                            resume_payload=resume_payload,
                        )
                        await self._insert_run_job(conn, job)
                    elif active_job["kind"] != "resume":
                        raise WaitDecisionConflictError("run already has a different active job")
                    resumed = True

                receipt = {
                    "type": "question.answer",
                    "command_id": command_id,
                    "question_id": question_id,
                    "run_id": run_id,
                    "answered": True,
                    "resumed": resumed,
                }
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) "
                    "VALUES (?, ?, 'question.answer', '', ?, ?, ?, ?)",
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

    async def request_permission_resolve_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        permission_id: str,
        decision: str,
        scope: str,
        edited_action: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], bool]:
        command_payload: dict[str, Any] = {
            "type": "permission.resolve",
            "permission_id": permission_id,
            "decision": decision,
            "scope": scope,
        }
        if edited_action is not None:
            command_payload["edited_action"] = edited_action
        payload_json = _encode_payload(command_payload)
        if decision == "approve":
            hitl_decision: dict[str, Any] = {"type": "approve"}
            permission_status = "approved"
        elif decision == "edit":
            hitl_decision = {"type": "edit", "edited_action": edited_action}
            permission_status = "approved"
        else:
            hitl_decision = {
                "type": "reject",
                "message": "Tool execution denied by user.",
            }
            permission_status = "denied"
        decision_json = _encode_payload(hitl_decision)
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type="permission.resolve",
                    payload_json=payload_json,
                )
                if existing is not None:
                    await conn.rollback()
                    return existing, False

                record = await (
                    await conn.execute(
                        "SELECT p.run_id, p.wait_cycle_id, p.status AS permission_status, "
                        "p.scope AS permission_scope, p.decision_json, p.tool_name, p.risk, "
                        "p.operation_id, r.status AS run_status, r.principal_id, "
                        "r.goal, r.user_input, r.workspace_path, r.mode, r.history_json, "
                        "r.settings_json, r.metadata_json, r.id AS id, r.run_kind, "
                        "r.root_run_id, r.agent_definition_id, r.agent_definition_version, "
                        "r.collaboration_depth, r.collaboration_policy_json "
                        "FROM local_permissions p JOIN local_runs r ON r.id = p.run_id "
                        "WHERE p.id = ? AND r.principal_id = ?",
                        (permission_id, principal_id),
                    )
                ).fetchone()
                if record is None:
                    raise KeyError(f"unknown permission: {permission_id}")
                run_id = str(record["run_id"])
                workspace_error = await self._workspace_owner_error(
                    conn,
                    principal_id=principal_id,
                    path=record["workspace_path"],
                ) or await self._workspace_path_error(record["workspace_path"])
                if workspace_error is not None:
                    raise WorkspaceAdmissionError(workspace_error)
                if decision == "edit" and (
                    edited_action is None or edited_action.get("name") != record["tool_name"]
                ):
                    raise WaitDecisionConflictError("tool name cannot be changed")
                require_allowed_permission_scope(
                    tool_name=str(record["tool_name"]),
                    risk=record["risk"],
                    status=permission_status,
                    scope=scope,
                )
                grant_max_uses = -1 if scope == "run" and permission_status == "approved" else 0
                grant_expires_at = None

                if record["permission_status"] == "pending":
                    if record["run_status"] not in {"waiting_permission", "waiting_input"}:
                        raise WaitDecisionConflictError("run is not awaiting a permission decision")
                    now = _now()
                    permission_cursor = await conn.execute(
                        "UPDATE local_permissions SET status = ?, scope = ?, decision_json = ?, "
                        "grant_max_uses = ?, grant_expires_at = ?, resolved_at = ? "
                        "WHERE id = ? AND status = 'pending'",
                        (
                            permission_status,
                            scope,
                            decision_json,
                            grant_max_uses,
                            grant_expires_at,
                            now,
                            permission_id,
                        ),
                    )
                    wait_cursor = await conn.execute(
                        "UPDATE local_wait_candidates SET status = 'resolved', "
                        "decision_json = ?, resolved_at = ? "
                        "WHERE id = ? AND status = 'pending'",
                        (decision_json, now, permission_id),
                    )
                    if permission_cursor.rowcount != 1 or wait_cursor.rowcount != 1:
                        raise WaitDecisionConflictError("permission was resolved concurrently")
                    await self._append_event_uncommitted(
                        conn,
                        run_id,
                        "permission.resolved",
                        payload_json=_encode_payload(
                            {
                                "request_id": permission_id,
                                "tool": record["tool_name"],
                                "tool_name": record["tool_name"],
                                "operation_id": record["operation_id"],
                                "decision": decision,
                                "scope": scope,
                            }
                        ),
                        created_at=now,
                    )
                elif (
                    record["permission_status"] != permission_status
                    or record["permission_scope"] != scope
                    or str(record["decision_json"] or "") != decision_json
                ):
                    raise WaitDecisionConflictError(
                        "permission was already resolved with a different decision"
                    )

                resume_payload = await self._wait_cycle_resume_payload_uncommitted(
                    conn,
                    run_id=run_id,
                    wait_cycle_id=str(record["wait_cycle_id"]),
                )
                resumed = record["run_status"] in {
                    "queued",
                    "running",
                    "completed",
                    "failed",
                }
                if (
                    record["run_status"] in {"waiting_permission", "waiting_input"}
                    and resume_payload is not None
                ):
                    active_job = await (
                        await conn.execute(
                            "SELECT * FROM local_run_jobs WHERE run_id = ? "
                            "AND status IN ('pending', 'leased')",
                            (run_id,),
                        )
                    ).fetchone()
                    if active_job is None:
                        job = self._new_run_job_record(
                            run_id=run_id,
                            kind="resume",
                            input_payload=self._run_job_input(dict(record)),
                            resume_payload=resume_payload,
                        )
                        await self._insert_run_job(conn, job)
                    elif active_job["kind"] != "resume":
                        raise WaitDecisionConflictError("run already has a different active job")
                    resumed = True

                receipt = {
                    "type": "permission.resolve",
                    "command_id": command_id,
                    "permission_id": permission_id,
                    "run_id": run_id,
                    "resolved": True,
                    "decision": decision,
                    "scope": scope,
                    "resumed": resumed,
                }
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) "
                    "VALUES (?, ?, 'permission.resolve', '', ?, ?, ?, ?)",
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

    async def request_plan_resolve_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        approval_id: str,
        decision: str,
        instructions: str | None,
    ) -> tuple[dict[str, Any], bool]:
        command_payload: dict[str, Any] = {
            "type": "plan.resolve",
            "approval_id": approval_id,
            "decision": decision,
        }
        if instructions is not None:
            command_payload["instructions"] = instructions
        payload_json = _encode_payload(command_payload)
        status = {
            "approve": "approved",
            "modify": "modified",
            "reject": "rejected",
        }[decision]
        resume_decision = {
            "approval_id": approval_id,
            "decision": decision,
            "instructions": instructions,
        }
        decision_json = _encode_payload(resume_decision)

        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type="plan.resolve",
                    payload_json=payload_json,
                )
                if existing is not None:
                    await conn.rollback()
                    return existing, False

                record = await (
                    await conn.execute(
                        "SELECT p.run_id, p.wait_cycle_id, p.status AS approval_status, "
                        "p.instructions AS approval_instructions, "
                        "r.status AS run_status, r.principal_id, r.goal, r.user_input, r.workspace_path, "
                        "r.mode, r.history_json, r.settings_json, r.metadata_json, r.id AS id, "
                        "r.run_kind, r.root_run_id, r.agent_definition_id, "
                        "r.agent_definition_version, r.collaboration_depth, "
                        "r.collaboration_policy_json "
                        "FROM local_plan_approvals p JOIN local_runs r ON r.id = p.run_id "
                        "WHERE p.id = ? AND r.principal_id = ?",
                        (approval_id, principal_id),
                    )
                ).fetchone()
                if record is None:
                    raise KeyError(f"unknown plan approval: {approval_id}")
                run_id = str(record["run_id"])
                workspace_error = await self._workspace_owner_error(
                    conn,
                    principal_id=principal_id,
                    path=record["workspace_path"],
                ) or await self._workspace_path_error(record["workspace_path"])
                if workspace_error is not None:
                    raise WorkspaceAdmissionError(workspace_error)

                if record["approval_status"] == "pending":
                    if record["run_status"] not in {"waiting_permission", "waiting_input"}:
                        raise WaitDecisionConflictError(
                            "run is not awaiting a plan approval decision"
                        )
                    now = _now()
                    approval_cursor = await conn.execute(
                        "UPDATE local_plan_approvals SET status = ?, instructions = ?, "
                        "resolved_at = ? WHERE id = ? AND status = 'pending'",
                        (status, instructions, now, approval_id),
                    )
                    wait_cursor = await conn.execute(
                        "UPDATE local_wait_candidates SET status = 'resolved', "
                        "decision_json = ?, resolved_at = ? "
                        "WHERE id = ? AND status = 'pending'",
                        (decision_json, now, approval_id),
                    )
                    if approval_cursor.rowcount != 1 or wait_cursor.rowcount != 1:
                        raise WaitDecisionConflictError("plan approval was resolved concurrently")
                    await self._append_event_uncommitted(
                        conn,
                        run_id,
                        "plan.approval_resolved",
                        payload_json=_encode_payload(
                            {
                                "request_id": approval_id,
                                "decision": decision,
                                "instructions": instructions,
                            }
                        ),
                        created_at=now,
                    )
                elif (
                    record["approval_status"] != status
                    or record["approval_instructions"] != instructions
                ):
                    raise WaitDecisionConflictError(
                        "plan approval was already resolved with a different decision"
                    )

                resume_payload = await self._wait_cycle_resume_payload_uncommitted(
                    conn,
                    run_id=run_id,
                    wait_cycle_id=str(record["wait_cycle_id"]),
                )
                resumed = record["run_status"] in {
                    "queued",
                    "running",
                    "completed",
                    "failed",
                }
                if (
                    record["run_status"] in {"waiting_permission", "waiting_input"}
                    and resume_payload is not None
                ):
                    active_job = await (
                        await conn.execute(
                            "SELECT * FROM local_run_jobs WHERE run_id = ? "
                            "AND status IN ('pending', 'leased')",
                            (run_id,),
                        )
                    ).fetchone()
                    if active_job is None:
                        job = self._new_run_job_record(
                            run_id=run_id,
                            kind="resume",
                            input_payload=self._run_job_input(dict(record)),
                            resume_payload=resume_payload,
                        )
                        await self._insert_run_job(conn, job)
                    elif active_job["kind"] != "resume":
                        raise WaitDecisionConflictError("run already has a different active job")
                    resumed = True

                receipt = {
                    "type": "plan.resolve",
                    "command_id": command_id,
                    "approval_id": approval_id,
                    "run_id": run_id,
                    "resolved": True,
                    "decision": decision,
                    "instructions": instructions,
                    "resumed": resumed,
                }
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) "
                    "VALUES (?, ?, 'plan.resolve', '', ?, ?, ?, ?)",
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

    async def request_tool_reconcile_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        operation_id: str,
        decision: str,
        current_result_json: str | None,
        current_result_hash: str | None,
        prior_result_json: str,
        prior_result_hash: str,
    ) -> tuple[dict[str, Any], bool]:
        payload_json = _encode_payload(
            {
                "type": "tool.reconcile",
                "operation_id": operation_id,
                "decision": decision,
            }
        )
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type="tool.reconcile",
                    payload_json=payload_json,
                )
                if existing is not None:
                    await conn.rollback()
                    return existing, False

                record = await (
                    await conn.execute(
                        "SELECT c.*, r.status AS run_status, r.principal_id, r.goal, "
                        "r.user_input, r.workspace_path, r.mode, r.history_json, "
                        "r.settings_json, r.metadata_json, r.id AS id, r.run_kind, "
                        "r.root_run_id, r.agent_definition_id, r.agent_definition_version, "
                        "r.collaboration_depth, r.collaboration_policy_json "
                        "FROM local_wait_candidates c JOIN local_runs r ON r.id = c.run_id "
                        "WHERE c.id = ? AND c.kind = 'tool_reconciliation' "
                        "AND r.principal_id = ?",
                        (operation_id, principal_id),
                    )
                ).fetchone()
                if record is None:
                    raise KeyError(f"unknown tool reconciliation: {operation_id}")
                run_id = str(record["run_id"])
                workspace_error = await self._workspace_owner_error(
                    conn,
                    principal_id=principal_id,
                    path=record["workspace_path"],
                ) or await self._workspace_path_error(record["workspace_path"])
                if workspace_error is not None:
                    raise WorkspaceAdmissionError(workspace_error)
                if record["status"] == "pending" and record["run_status"] not in {
                    "waiting_permission",
                    "waiting_input",
                }:
                    raise WaitDecisionConflictError(
                        "run is not awaiting a tool reconciliation decision"
                    )

                updated, newly_resolved = await self._resolve_tool_reconciliation_uncommitted(
                    conn,
                    candidate_id=operation_id,
                    decision=decision,
                    current_result_json=current_result_json,
                    current_result_hash=current_result_hash,
                    prior_result_json=prior_result_json,
                    prior_result_hash=prior_result_hash,
                )
                now = _now()
                if newly_resolved:
                    await self._append_event_uncommitted(
                        conn,
                        run_id,
                        "tool.reconciliation_resolved",
                        payload_json=_encode_payload(
                            {
                                "request_id": operation_id,
                                "operation_id": operation_id,
                                "decision": decision,
                            }
                        ),
                        created_at=now,
                    )

                resume_payload = await self._wait_cycle_resume_payload_uncommitted(
                    conn,
                    run_id=run_id,
                    wait_cycle_id=str(updated["wait_cycle_id"]),
                )
                resumed = record["run_status"] in {
                    "queued",
                    "running",
                    "completed",
                    "failed",
                }
                if (
                    record["run_status"] in {"waiting_permission", "waiting_input"}
                    and resume_payload is not None
                ):
                    active_job = await (
                        await conn.execute(
                            "SELECT * FROM local_run_jobs WHERE run_id = ? "
                            "AND status IN ('pending', 'leased')",
                            (run_id,),
                        )
                    ).fetchone()
                    if active_job is None:
                        job = self._new_run_job_record(
                            run_id=run_id,
                            kind="resume",
                            input_payload=self._run_job_input(dict(record)),
                            resume_payload=resume_payload,
                        )
                        await self._insert_run_job(conn, job)
                    elif active_job["kind"] != "resume":
                        raise WaitDecisionConflictError("run already has a different active job")
                    resumed = True

                receipt = {
                    "type": "tool.reconcile",
                    "command_id": command_id,
                    "operation_id": operation_id,
                    "run_id": run_id,
                    "resolved": True,
                    "decision": decision,
                    "resumed": resumed,
                }
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) "
                    "VALUES (?, ?, 'tool.reconcile', '', ?, ?, ?, ?)",
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
