from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from ..api_schemas import (
    AnswerQuestionCommand,
    PlanResolveCommand,
    ResolvePermissionCommand,
    ToolReconcileCommand,
)
from ..http_route_helpers import (
    _authorized_workspace_path,
    _owned_run,
    _tool_reconciliation_results,
)
from ..permission_policy import PermissionScopeNotAllowedError
from ..runs import RunCoordinator
from ..store.sqlite import (
    CommandConflictError,
    LocalStore,
    WaitDecisionConflictError,
    WorkspaceAdmissionError,
)

WaitCommand = (
    AnswerQuestionCommand | ResolvePermissionCommand | PlanResolveCommand | ToolReconcileCommand
)


async def _accepted_replay(
    request: Request,
    *,
    command_id: str,
    command_type: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    store: LocalStore = request.app.state.store
    replay = await store.accepted_command_receipt(
        principal_id=request.state.principal_id,
        command_id=command_id,
        command_type=command_type,
        payload=payload,
    )
    if replay is not None and replay.get("resumed"):
        request.app.state.coordinator.wake_jobs()
    return replay


async def _authorize_resume(
    request: Request,
    *,
    run_id: str,
    not_found_detail: str,
) -> None:
    store: LocalStore = request.app.state.store
    run = await _owned_run(
        store,
        principal_id=request.state.principal_id,
        run_id=run_id,
        not_found_detail=not_found_detail,
    )
    await _authorized_workspace_path(
        store,
        principal_id=request.state.principal_id,
        path=run.get("workspace_path"),
    )
    coordinator: RunCoordinator = request.app.state.coordinator
    await coordinator.reconcile_resume_head(run_id)


async def accept_wait_command(request: Request, body: WaitCommand) -> dict[str, Any]:
    store: LocalStore = request.app.state.store
    coordinator: RunCoordinator = request.app.state.coordinator

    if isinstance(body, ToolReconcileCommand):
        command_payload = {
            "type": body.type,
            "operation_id": body.operation_id,
            "decision": body.decision,
        }
        try:
            replay = await _accepted_replay(
                request,
                command_id=body.command_id,
                command_type=body.type,
                payload=command_payload,
            )
            if replay is not None:
                return replay
            reconciliation = await store.get_wait_candidate(body.operation_id)
            if reconciliation is None or reconciliation.get("kind") != "tool_reconciliation":
                raise KeyError(body.operation_id)
            run_id = str(reconciliation["run_id"])
            await _authorize_resume(
                request,
                run_id=run_id,
                not_found_detail="tool reconciliation not found",
            )
            results = await _tool_reconciliation_results(
                store,
                operation_id=body.operation_id,
                decision=body.decision,
            )
            receipt, _created = await store.request_tool_reconcile_command(
                principal_id=request.state.principal_id,
                command_id=body.command_id,
                operation_id=body.operation_id,
                decision=body.decision,
                **results,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="tool reconciliation not found") from exc
        except (CommandConflictError, WaitDecisionConflictError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except WorkspaceAdmissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if receipt["resumed"]:
            coordinator.wake_jobs()
        return receipt

    if isinstance(body, PlanResolveCommand):
        instructions = (body.instructions or "").strip() or None
        command_payload: dict[str, Any] = {
            "type": body.type,
            "approval_id": body.approval_id,
            "decision": body.decision,
        }
        if instructions is not None:
            command_payload["instructions"] = instructions
        try:
            replay = await _accepted_replay(
                request,
                command_id=body.command_id,
                command_type=body.type,
                payload=command_payload,
            )
            if replay is not None:
                return replay
            approval = await store.get_plan_approval(body.approval_id)
            if approval is None:
                raise KeyError(body.approval_id)
            await _authorize_resume(
                request,
                run_id=str(approval["run_id"]),
                not_found_detail="plan approval not found",
            )
            receipt, _created = await store.request_plan_resolve_command(
                principal_id=request.state.principal_id,
                command_id=body.command_id,
                approval_id=body.approval_id,
                decision=body.decision,
                instructions=instructions,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="plan approval not found") from exc
        except (CommandConflictError, WaitDecisionConflictError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except WorkspaceAdmissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if receipt["resumed"]:
            coordinator.wake_jobs()
        return receipt

    if isinstance(body, ResolvePermissionCommand):
        edited_action = body.edited_action.model_dump() if body.edited_action else None
        command_payload: dict[str, Any] = {
            "type": body.type,
            "permission_id": body.permission_id,
            "decision": body.decision,
            "scope": body.scope,
        }
        if edited_action is not None:
            command_payload["edited_action"] = edited_action
        try:
            replay = await _accepted_replay(
                request,
                command_id=body.command_id,
                command_type=body.type,
                payload=command_payload,
            )
            if replay is not None:
                return replay
            permission = await store.get_permission(body.permission_id)
            if permission is None:
                raise KeyError(body.permission_id)
            await _authorize_resume(
                request,
                run_id=str(permission["run_id"]),
                not_found_detail="permission not found",
            )
            receipt, _created = await store.request_permission_resolve_command(
                principal_id=request.state.principal_id,
                command_id=body.command_id,
                permission_id=body.permission_id,
                decision=body.decision,
                scope=body.scope,
                edited_action=edited_action,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="permission not found") from exc
        except (CommandConflictError, WaitDecisionConflictError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PermissionScopeNotAllowedError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except WorkspaceAdmissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if receipt["resumed"]:
            coordinator.wake_jobs()
        return receipt

    command_payload = {
        "type": body.type,
        "question_id": body.question_id,
        "answers": body.answers,
    }
    try:
        replay = await _accepted_replay(
            request,
            command_id=body.command_id,
            command_type=body.type,
            payload=command_payload,
        )
        if replay is not None:
            return replay
        question = await store.get_question(body.question_id)
        if question is None:
            raise KeyError(body.question_id)
        await _authorize_resume(
            request,
            run_id=str(question["run_id"]),
            not_found_detail="question not found",
        )
        receipt, _created = await store.request_question_answer_command(
            principal_id=request.state.principal_id,
            command_id=body.command_id,
            question_id=body.question_id,
            answers=body.answers,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="question not found") from exc
    except (CommandConflictError, WaitDecisionConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WorkspaceAdmissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if receipt["resumed"]:
        coordinator.wake_jobs()
    return receipt
