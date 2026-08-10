from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from fastapi import HTTPException, Request

from ..api_schemas import (
    PluginDisableCommand,
    PluginEnableCommand,
    PluginInstallCommand,
    PluginModelBindCommand,
    PluginRemoveCommand,
    PluginRollbackCommand,
    PluginSetupAdvanceCommand,
    PluginUpdateCommand,
    RuntimeAssetInstallCommand,
)
from ..plugins.registry import PluginRegistry, PluginRegistryError
from ..runs import RunCoordinator
from ..store.sqlite import CommandConflictError

PluginCommand = (
    PluginInstallCommand
    | PluginModelBindCommand
    | RuntimeAssetInstallCommand
    | PluginEnableCommand
    | PluginDisableCommand
    | PluginUpdateCommand
    | PluginRollbackCommand
    | PluginRemoveCommand
    | PluginSetupAdvanceCommand
)


async def _registry_result(operation: Awaitable[dict[str, Any]]) -> dict[str, Any]:
    try:
        return await operation
    except CommandConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PluginRegistryError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


async def accept_plugin_command(request: Request, body: PluginCommand) -> dict[str, Any]:
    registry: PluginRegistry = request.app.state.plugin_registry
    principal_id = request.state.principal_id

    if isinstance(body, PluginSetupAdvanceCommand):
        return await _registry_result(
            registry.advance_computer_use_setup(
                principal_id=principal_id,
                command_id=body.command_id,
                expected_revision=body.expected_revision,
                action_id=body.action_id,
            )
        )
    if isinstance(body, PluginModelBindCommand):
        coordinator: RunCoordinator = request.app.state.coordinator
        async with coordinator._model_admission(
            principal_id,
            body.model,
            ("image_inputs",),
        ) as (binding, error):
            if error is not None:
                raise HTTPException(
                    status_code=409,
                    detail={"code": error.code, "message": str(error)},
                )
            return await _registry_result(
                registry.bind_model(
                    principal_id=principal_id,
                    command_id=body.command_id,
                    plugin_id=body.plugin_id,
                    binding_id=body.binding_id,
                    requested_model=body.model,
                    model_binding=binding,
                    expected_digest=body.expected_digest,
                )
            )
    if isinstance(body, RuntimeAssetInstallCommand):
        return await _registry_result(
            registry.install_runtime_asset(
                principal_id=principal_id,
                command_id=body.command_id,
                source_path=body.source_path,
                expected_digest=body.expected_digest,
            )
        )
    if isinstance(body, PluginInstallCommand):
        return await _registry_result(
            registry.install(
                principal_id=principal_id,
                command_id=body.command_id,
                source_path=body.source_path,
                expected_digest=body.expected_digest,
                allow_unsigned=body.allow_unsigned,
            )
        )
    if isinstance(body, PluginUpdateCommand):
        return await _registry_result(
            registry.update(
                principal_id=principal_id,
                command_id=body.command_id,
                plugin_id=body.plugin_id,
                source_path=body.source_path,
                expected_digest=body.expected_digest,
                allow_unsigned=body.allow_unsigned,
            )
        )
    if isinstance(body, PluginRollbackCommand):
        return await _registry_result(
            registry.rollback(
                principal_id=principal_id,
                command_id=body.command_id,
                plugin_id=body.plugin_id,
                target_digest=body.target_digest,
                expected_digest=body.expected_digest,
            )
        )
    if isinstance(body, PluginRemoveCommand):
        return await _registry_result(
            registry.remove(
                principal_id=principal_id,
                command_id=body.command_id,
                plugin_id=body.plugin_id,
                expected_digest=body.expected_digest,
            )
        )
    return await _registry_result(
        registry.set_enabled(
            principal_id=principal_id,
            command_id=body.command_id,
            plugin_id=body.plugin_id,
            expected_digest=body.expected_digest,
            enabled=isinstance(body, PluginEnableCommand),
        )
    )
