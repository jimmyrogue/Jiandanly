from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .api_schemas import (
    AnswerQuestionCommand,
    AnswerQuestionCommandReceipt,
    CancelRunCommand,
    CancelRunCommandReceipt,
    PlanResolveCommand,
    PlanResolveCommandReceipt,
    PluginDisableCommand,
    PluginEnableCommand,
    PluginInstallCommand,
    PluginInstallCommandReceipt,
    PluginModelBindCommand,
    PluginModelBindCommandReceipt,
    PluginRemoveCommand,
    PluginRemoveCommandReceipt,
    PluginRollbackCommand,
    PluginSetupAdvanceCommand,
    PluginSetupAdvanceCommandReceipt,
    PluginStateCommandReceipt,
    PluginUpdateCommand,
    PluginVersionSwitchCommandReceipt,
    ResolvePermissionCommand,
    ResolvePermissionCommandReceipt,
    RuntimeAssetInstallCommand,
    RuntimeAssetInstallCommandReceipt,
    ToolReconcileCommand,
    ToolReconcileCommandReceipt,
)
from .http_route_helpers import (
    _authorized_workspace_path,
    _owned_run,
    _tool_reconciliation_results,
)
from .permission_policy import PermissionScopeNotAllowedError
from .plugins.registry import PluginRegistry, PluginRegistryError
from .runs import RunCoordinator
from .store.sqlite import (
    CommandConflictError,
    LocalStore,
    WaitDecisionConflictError,
    WorkspaceAdmissionError,
)

command_router = APIRouter()


@command_router.post(
    "/v1/commands",
    response_model=(
        CancelRunCommandReceipt
        | AnswerQuestionCommandReceipt
        | ResolvePermissionCommandReceipt
        | PlanResolveCommandReceipt
        | ToolReconcileCommandReceipt
        | PluginInstallCommandReceipt
        | PluginModelBindCommandReceipt
        | RuntimeAssetInstallCommandReceipt
        | PluginStateCommandReceipt
        | PluginVersionSwitchCommandReceipt
        | PluginRemoveCommandReceipt
        | PluginSetupAdvanceCommandReceipt
    ),
)
async def accept_command(
    request: Request,
    body: (
        CancelRunCommand
        | AnswerQuestionCommand
        | ResolvePermissionCommand
        | PlanResolveCommand
        | ToolReconcileCommand
        | PluginInstallCommand
        | PluginModelBindCommand
        | RuntimeAssetInstallCommand
        | PluginEnableCommand
        | PluginDisableCommand
        | PluginUpdateCommand
        | PluginRollbackCommand
        | PluginRemoveCommand
        | PluginSetupAdvanceCommand
    ),
) -> dict[str, Any]:
    store: LocalStore = request.app.state.store
    coordinator: RunCoordinator = request.app.state.coordinator
    if isinstance(body, PluginSetupAdvanceCommand):
        registry: PluginRegistry = request.app.state.plugin_registry
        try:
            return await registry.advance_computer_use_setup(
                principal_id=request.state.principal_id,
                command_id=body.command_id,
                expected_revision=body.expected_revision,
                action_id=body.action_id,
            )
        except CommandConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PluginRegistryError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
    if isinstance(body, PluginModelBindCommand):
        registry: PluginRegistry = request.app.state.plugin_registry
        async with coordinator._model_admission(
            request.state.principal_id,
            body.model,
            ("image_inputs",),
        ) as (binding, error):
            if error is not None:
                raise HTTPException(
                    status_code=409,
                    detail={"code": error.code, "message": str(error)},
                )
            try:
                return await registry.bind_model(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    plugin_id=body.plugin_id,
                    binding_id=body.binding_id,
                    requested_model=body.model,
                    model_binding=binding,
                    expected_digest=body.expected_digest,
                )
            except CommandConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except PluginRegistryError as exc:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={"code": exc.code, "message": str(exc)},
                ) from exc
    if isinstance(body, RuntimeAssetInstallCommand):
        registry: PluginRegistry = request.app.state.plugin_registry
        try:
            return await registry.install_runtime_asset(
                principal_id=request.state.principal_id,
                command_id=body.command_id,
                source_path=body.source_path,
                expected_digest=body.expected_digest,
            )
        except CommandConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PluginRegistryError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
    if isinstance(body, PluginInstallCommand):
        registry: PluginRegistry = request.app.state.plugin_registry
        try:
            return await registry.install(
                principal_id=request.state.principal_id,
                command_id=body.command_id,
                source_path=body.source_path,
                expected_digest=body.expected_digest,
                allow_unsigned=body.allow_unsigned,
            )
        except CommandConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PluginRegistryError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
    if isinstance(body, PluginUpdateCommand):
        registry = request.app.state.plugin_registry
        try:
            return await registry.update(
                principal_id=request.state.principal_id,
                command_id=body.command_id,
                plugin_id=body.plugin_id,
                source_path=body.source_path,
                expected_digest=body.expected_digest,
                allow_unsigned=body.allow_unsigned,
            )
        except CommandConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PluginRegistryError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
    if isinstance(body, PluginRollbackCommand):
        registry = request.app.state.plugin_registry
        try:
            return await registry.rollback(
                principal_id=request.state.principal_id,
                command_id=body.command_id,
                plugin_id=body.plugin_id,
                target_digest=body.target_digest,
                expected_digest=body.expected_digest,
            )
        except CommandConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PluginRegistryError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
    if isinstance(body, PluginRemoveCommand):
        registry = request.app.state.plugin_registry
        try:
            return await registry.remove(
                principal_id=request.state.principal_id,
                command_id=body.command_id,
                plugin_id=body.plugin_id,
                expected_digest=body.expected_digest,
            )
        except CommandConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PluginRegistryError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
    if isinstance(body, (PluginEnableCommand, PluginDisableCommand)):
        registry = request.app.state.plugin_registry
        try:
            return await registry.set_enabled(
                principal_id=request.state.principal_id,
                command_id=body.command_id,
                plugin_id=body.plugin_id,
                expected_digest=body.expected_digest,
                enabled=isinstance(body, PluginEnableCommand),
            )
        except CommandConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PluginRegistryError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
    if isinstance(body, ToolReconcileCommand):
        command_payload = {
            "type": body.type,
            "operation_id": body.operation_id,
            "decision": body.decision,
        }
        try:
            replay = await store.accepted_command_receipt(
                principal_id=request.state.principal_id,
                command_id=body.command_id,
                command_type=body.type,
                payload=command_payload,
            )
            if replay is not None:
                if replay.get("resumed"):
                    coordinator.wake_jobs()
                return replay
            reconciliation = await store.get_wait_candidate(body.operation_id)
            if reconciliation is None or reconciliation.get("kind") != "tool_reconciliation":
                raise KeyError(body.operation_id)
            run = await _owned_run(
                store,
                principal_id=request.state.principal_id,
                run_id=str(reconciliation["run_id"]),
                not_found_detail="tool reconciliation not found",
            )
            await _authorized_workspace_path(
                store,
                principal_id=request.state.principal_id,
                path=run.get("workspace_path"),
            )
            await coordinator.reconcile_resume_head(str(reconciliation["run_id"]))
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
            replay = await store.accepted_command_receipt(
                principal_id=request.state.principal_id,
                command_id=body.command_id,
                command_type=body.type,
                payload=command_payload,
            )
            if replay is not None:
                if replay.get("resumed"):
                    coordinator.wake_jobs()
                return replay
            approval = await store.get_plan_approval(body.approval_id)
            if approval is None:
                raise KeyError(body.approval_id)
            run = await _owned_run(
                store,
                principal_id=request.state.principal_id,
                run_id=str(approval["run_id"]),
                not_found_detail="plan approval not found",
            )
            await _authorized_workspace_path(
                store,
                principal_id=request.state.principal_id,
                path=run.get("workspace_path"),
            )
            await coordinator.reconcile_resume_head(str(approval["run_id"]))
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
            replay = await store.accepted_command_receipt(
                principal_id=request.state.principal_id,
                command_id=body.command_id,
                command_type=body.type,
                payload=command_payload,
            )
            if replay is not None:
                if replay.get("resumed"):
                    coordinator.wake_jobs()
                return replay
            permission = await store.get_permission(body.permission_id)
            if permission is None:
                raise KeyError(body.permission_id)
            run = await _owned_run(
                store,
                principal_id=request.state.principal_id,
                run_id=str(permission["run_id"]),
                not_found_detail="permission not found",
            )
            await _authorized_workspace_path(
                store,
                principal_id=request.state.principal_id,
                path=run.get("workspace_path"),
            )
            await coordinator.reconcile_resume_head(str(permission["run_id"]))
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
    if isinstance(body, AnswerQuestionCommand):
        command_payload = {
            "type": body.type,
            "question_id": body.question_id,
            "answers": body.answers,
        }
        try:
            replay = await store.accepted_command_receipt(
                principal_id=request.state.principal_id,
                command_id=body.command_id,
                command_type=body.type,
                payload=command_payload,
            )
            if replay is not None:
                if replay.get("resumed"):
                    coordinator.wake_jobs()
                return replay
            question = await store.get_question(body.question_id)
            if question is None:
                raise KeyError(body.question_id)
            run = await _owned_run(
                store,
                principal_id=request.state.principal_id,
                run_id=str(question["run_id"]),
                not_found_detail="question not found",
            )
            await _authorized_workspace_path(
                store,
                principal_id=request.state.principal_id,
                path=run.get("workspace_path"),
            )
            await coordinator.reconcile_resume_head(str(question["run_id"]))
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
    try:
        receipt, created = await store.request_run_cancel_command(
            principal_id=request.state.principal_id,
            command_id=body.command_id,
            run_id=body.run_id,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "run_not_found", "message": "run not found"},
        ) from exc
    except CommandConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if created and receipt["canceled"]:
        await coordinator.cancel_run(body.run_id)
    return receipt
