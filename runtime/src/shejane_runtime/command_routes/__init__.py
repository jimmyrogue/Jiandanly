from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..api_schemas import (
    AnswerQuestionCommandReceipt,
    CancelRunCommand,
    CancelRunCommandReceipt,
    PlanResolveCommandReceipt,
    PluginInstallCommandReceipt,
    PluginModelBindCommandReceipt,
    PluginRemoveCommandReceipt,
    PluginSetupAdvanceCommandReceipt,
    PluginStateCommandReceipt,
    PluginVersionSwitchCommandReceipt,
    ResolvePermissionCommandReceipt,
    RuntimeAssetInstallCommandReceipt,
    ToolReconcileCommandReceipt,
)
from ..runs import RunCoordinator
from ..store.sqlite import CommandConflictError, LocalStore
from .plugins import PluginCommand, accept_plugin_command
from .waits import WaitCommand, accept_wait_command

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
    body: CancelRunCommand | WaitCommand | PluginCommand,
) -> dict[str, Any]:
    if isinstance(body, PluginCommand):
        return await accept_plugin_command(request, body)
    if isinstance(body, WaitCommand):
        return await accept_wait_command(request, body)

    store: LocalStore = request.app.state.store
    coordinator: RunCoordinator = request.app.state.coordinator
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
