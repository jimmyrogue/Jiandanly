"""Legacy-compatible Run wait decision routes."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .api_schemas import (
    AnswerQuestionRequest,
    PermissionResolution,
    PlanApprovalResolution,
    QuestionAnswer,
    ReconcileToolRequest,
    ResolvePermissionRequest,
    ResolvePlanApprovalRequest,
    ToolReconciliationResolution,
)
from .http_route_helpers import (
    _authorized_workspace_path,
    _event_payload,
    _owned_run,
    _tool_reconciliation_results,
)
from .permission_policy import PermissionScopeNotAllowedError
from .run_diagnostics_projection import first_string as _first_string
from .runs import (
    RunCoordinator,
)
from .store.events import TERMINAL_RUN_STATUSES
from .store.sqlite import (
    CommandConflictError,
    LocalStore,
    PermissionDecisionConflictError,
    WaitDecisionConflictError,
    WorkspaceAdmissionError,
)

run_decision_router = APIRouter()
_TERMINAL_RUN_STATUSES = TERMINAL_RUN_STATUSES | {"cleanup_required"}


@run_decision_router.post("/v1/permissions/{permission_id}", response_model=PermissionResolution)
async def resolve_permission(
    request: Request, permission_id: str, body: ResolvePermissionRequest
) -> dict[str, Any]:
    """Approve, edit, or deny a parameter-bound tool review.

    Translates the client's `{decision, scope}` body into the
    `{"decisions": [{"type": "approve"|"edit"|"reject", ...}]}` shape
    that `ToolReviewMiddleware` verifies on resume. One LangGraph
    interrupt can contain multiple action requests, so the run
    resumes only after every permission in the current pause batch is
    resolved, preserving the original `permission.required` order.

    `scope=run` is a durable grant for an eligible ordinary tool for the
    rest of the run, with the same version and risk class. Irreversible
    and unknown actions cannot receive this scope.
    """
    decision_text = body.decision
    scope = body.scope
    store: LocalStore = request.app.state.store
    record = await store.get_permission(permission_id)
    if record is None:
        raise HTTPException(status_code=404, detail="permission not found")
    run = await _owned_run(
        store,
        principal_id=request.state.principal_id,
        run_id=record["run_id"],
        not_found_detail="permission not found",
    )
    await _authorized_workspace_path(
        store,
        principal_id=request.state.principal_id,
        path=run.get("workspace_path"),
    )
    if decision_text == "approve":
        hitl_decision: dict[str, Any] = {"type": "approve"}
        persisted_status = "approved"
    elif decision_text == "edit":
        assert body.edited_action is not None
        if body.edited_action.name != record["tool_name"]:
            raise HTTPException(status_code=400, detail="tool name cannot be changed")
        hitl_decision = {
            "type": "edit",
            "edited_action": body.edited_action.model_dump(),
        }
        persisted_status = "approved"
    else:
        hitl_decision = {
            "type": "reject",
            "message": "Tool execution denied by user.",
        }
        persisted_status = "denied"
    already_resolved = record.get("status") != "pending"
    if not already_resolved and run.get("status") in _TERMINAL_RUN_STATUSES:
        raise HTTPException(status_code=409, detail="run is not awaiting a decision")
    resolution_event = {
        "request_id": permission_id,
        "tool": record["tool_name"],
        "tool_name": record["tool_name"],
        "operation_id": record.get("operation_id"),
        "decision": decision_text,
        "scope": str(scope),
    }
    try:
        await store.resolve_permission(
            permission_id,
            status=persisted_status,
            scope=str(scope),
            decision=hitl_decision,
            event_payload=None if already_resolved else resolution_event,
        )
    except PermissionScopeNotAllowedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (PermissionDecisionConflictError, WaitDecisionConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    coordinator: RunCoordinator = request.app.state.coordinator
    if not already_resolved:
        coordinator.wake_run(str(record["run_id"]))
    resume_payload = await store.wait_cycle_resume_payload(
        run_id=str(record["run_id"]),
        wait_cycle_id=str(record.get("wait_cycle_id") or record["id"]),
    )
    if resume_payload is None:
        ok = False
    else:
        ok = await _ensure_resume_job(
            store=store,
            coordinator=coordinator,
            run_id=record["run_id"],
            decision=resume_payload,
        )
    return {
        "permission_id": permission_id,
        "resolved": True,
        "decision": decision_text,
        "scope": scope,
        "resumed": ok,
    }


@run_decision_router.post("/v1/questions/{question_id}", response_model=QuestionAnswer)
async def answer_question(
    request: Request, question_id: str, body: AnswerQuestionRequest
) -> dict[str, Any]:
    """Submit answers to a paused user.ask interrupt.

    Body shape (per `client.ts:answerLocalQuestion`):
    `{answers: Record<string, string[]>}`. We look up the question
    by id to find its run_id, persist the answers, then resume.
    """
    answers = body.answers
    store: LocalStore = request.app.state.store
    record = await store.get_question(question_id)
    if record is None:
        raise HTTPException(status_code=404, detail="question not found")
    run = await _owned_run(
        store,
        principal_id=request.state.principal_id,
        run_id=record["run_id"],
        not_found_detail="question not found",
    )
    await _authorized_workspace_path(
        store,
        principal_id=request.state.principal_id,
        path=run.get("workspace_path"),
    )
    already_answered = record.get("status") != "pending"
    if not already_answered and run.get("status") in _TERMINAL_RUN_STATUSES:
        raise HTTPException(status_code=409, detail="run is not awaiting a decision")
    answer_event = {"request_id": question_id, "answers": answers}
    try:
        await store.answer_question(
            question_id,
            answers=answers,
            event_payload=None if already_answered else answer_event,
        )
    except WaitDecisionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    coordinator: RunCoordinator = request.app.state.coordinator
    if not already_answered:
        coordinator.wake_run(str(record["run_id"]))
    resume_payload = await store.wait_cycle_resume_payload(
        run_id=str(record["run_id"]),
        wait_cycle_id=str(record.get("wait_cycle_id") or record["id"]),
    )
    if resume_payload is not None:
        ok = await _ensure_resume_job(
            store=store,
            coordinator=coordinator,
            run_id=record["run_id"],
            decision=resume_payload,
        )
    else:
        ok = False
    return {
        "question_id": question_id,
        "answered": True,
        "resumed": ok,
    }


@run_decision_router.post(
    "/v1/tool-reconciliations/{operation_id}",
    response_model=ToolReconciliationResolution,
)
async def reconcile_tool_operation(
    request: Request,
    operation_id: str,
    body: ReconcileToolRequest,
) -> dict[str, Any]:
    store: LocalStore = request.app.state.store
    command_id = f"legacy_reconcile_{operation_id}"
    try:
        replay = await store.accepted_command_receipt(
            principal_id=request.state.principal_id,
            command_id=command_id,
            command_type="tool.reconcile",
            payload={
                "type": "tool.reconcile",
                "operation_id": operation_id,
                "decision": body.decision,
            },
        )
    except CommandConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if replay is not None:
        if replay.get("resumed"):
            request.app.state.coordinator.wake_jobs()
        return replay
    record = await store.get_wait_candidate(operation_id)
    if record is None or record.get("kind") != "tool_reconciliation":
        raise HTTPException(status_code=404, detail="tool reconciliation not found")
    run = await _owned_run(
        store,
        principal_id=request.state.principal_id,
        run_id=str(record["run_id"]),
        not_found_detail="tool reconciliation not found",
    )
    await _authorized_workspace_path(
        store,
        principal_id=request.state.principal_id,
        path=run.get("workspace_path"),
    )
    if run.get("status") in _TERMINAL_RUN_STATUSES:
        raise HTTPException(status_code=409, detail="run is not awaiting a decision")
    coordinator: RunCoordinator = request.app.state.coordinator
    await coordinator.reconcile_resume_head(str(record["run_id"]))
    try:
        results = await _tool_reconciliation_results(
            store,
            operation_id=operation_id,
            decision=body.decision,
        )
        receipt, _created = await store.request_tool_reconcile_command(
            principal_id=request.state.principal_id,
            command_id=command_id,
            operation_id=operation_id,
            decision=body.decision,
            **results,
        )
    except (CommandConflictError, WaitDecisionConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WorkspaceAdmissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if receipt["resumed"]:
        coordinator.wake_jobs()
    return receipt


@run_decision_router.post("/v1/plans/{approval_id}", response_model=PlanApprovalResolution)
async def resolve_plan_approval(
    request: Request,
    approval_id: str,
    body: ResolvePlanApprovalRequest,
) -> dict[str, Any]:
    """Approve, revise, or reject a Plan Mode `write_todos` pause."""
    decision_text = body.decision
    instructions = (body.instructions or "").strip() or None
    if decision_text == "modify" and not instructions:
        raise HTTPException(status_code=400, detail="instructions required")

    store: LocalStore = request.app.state.store
    record = await store.get_plan_approval(approval_id)
    if record is None:
        raise HTTPException(status_code=404, detail="plan approval not found")
    run = await _owned_run(
        store,
        principal_id=request.state.principal_id,
        run_id=record["run_id"],
        not_found_detail="plan approval not found",
    )
    await _authorized_workspace_path(
        store,
        principal_id=request.state.principal_id,
        path=run.get("workspace_path"),
    )
    if run.get("status") in _TERMINAL_RUN_STATUSES:
        raise HTTPException(status_code=409, detail="run is not awaiting a decision")
    coordinator: RunCoordinator = request.app.state.coordinator
    await coordinator.reconcile_resume_head(str(record["run_id"]))
    try:
        receipt, _created = await store.request_plan_resolve_command(
            principal_id=request.state.principal_id,
            command_id=f"legacy_plan_{approval_id}",
            approval_id=approval_id,
            decision=decision_text,
            instructions=instructions,
        )
    except (CommandConflictError, WaitDecisionConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WorkspaceAdmissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if receipt["resumed"]:
        coordinator.wake_jobs()
    return receipt


def _current_permission_batch(
    raw_events: list[dict[str, Any]],
    permissions: list[dict[str, Any]],
    fallback_permission_id: str,
) -> list[dict[str, Any]]:
    """Return permission rows for the currently paused HITL batch.

    deepagents/LangGraph can bundle several tool approvals into one interrupt
    and expects one ordered `decisions` list on resume. We derive the batch from
    `permission.required` events emitted since the latest run start/resume
    boundary; if old rows or a sparse event log confuse that lookup, fall back
    to the single permission the user just resolved.
    """
    permission_by_id = {
        permission_id: permission
        for permission in permissions
        if (permission_id := str(permission.get("id") or ""))
    }
    batch_ids = _current_permission_batch_ids(raw_events)
    if fallback_permission_id not in batch_ids:
        batch_ids = [fallback_permission_id]

    batch: list[dict[str, Any]] = []
    seen: set[str] = set()
    for permission_id in batch_ids:
        if permission_id in seen:
            continue
        record = permission_by_id.get(permission_id)
        if record is None:
            continue
        seen.add(permission_id)
        batch.append(record)
    fallback = permission_by_id.get(fallback_permission_id)
    if not batch and fallback is not None:
        return [fallback]
    return batch


async def _ensure_resume_job(
    *,
    store: LocalStore,
    coordinator: RunCoordinator,
    run_id: str,
    decision: dict[str, Any],
) -> bool:
    """Idempotently ensure a resolved wait has a durable resume owner.

    The decision and resume job are currently separate SQLite transactions.
    Replaying the same decision repairs the crash window between them instead
    of falsely acknowledging a run that remains permanently paused.
    """
    if await coordinator.resume_run(run_id=run_id, decision=decision):
        return True
    run = await store.get_run(run_id)
    if run is None:
        return False
    if run.get("status") in {"completed", "failed", "canceled"}:
        return True
    if run.get("status") not in {"waiting_permission", "waiting_input"}:
        return False
    active_job = await store.get_active_run_job(run_id)
    return bool(
        active_job
        and active_job.get("kind") == "resume"
        and active_job.get("status") in {"pending", "leased"}
    )


def _current_permission_batch_ids(raw_events: list[dict[str, Any]]) -> list[str]:
    boundary_index = -1
    for index, event in enumerate(raw_events):
        if event.get("event_type") in {"run.started", "run.resumed"}:
            boundary_index = index

    request_ids: list[str] = []
    seen: set[str] = set()
    for event in raw_events[boundary_index + 1 :]:
        if event.get("event_type") != "permission.required":
            continue
        payload = _event_payload(event)
        request_id = _first_string(payload.get("request_id"), payload.get("id"))
        if request_id is None or request_id in seen:
            continue
        seen.add(request_id)
        request_ids.append(request_id)
    return request_ids


def _hitl_decision_for_permission(permission: dict[str, Any]) -> dict[str, Any]:
    raw = permission.get("decision_json")
    if isinstance(raw, str) and raw:
        try:
            decision = json.loads(raw)
        except json.JSONDecodeError:
            decision = None
        if isinstance(decision, dict):
            return decision
    # Compatibility for permission rows created before decision_json existed.
    if permission.get("status") == "approved":
        return {"type": "approve"}
    return {"type": "reject", "message": "Tool execution denied by user."}
