from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from . import __version__
from .api_schemas import (
    CentralDiagnosticsStatusResponse,
    ClearMemoryResponse,
    HealthResponse,
    RuntimeInfo,
    RuntimeSettingsResponse,
    UpdateCentralDiagnosticsRequest,
    UpdateRuntimeSettingsRequest,
)
from .auth import LOCAL_OWNER_PRINCIPAL_ID
from .central_diagnostics import (
    CentralDiagnosticsConfigurationError,
    CentralDiagnosticsUnavailable,
)
from .config import Settings
from .model_credentials import CredentialStoreError, get_model_api_key
from .runs import RUNTIME_PROTOCOL_VERSION, runtime_capabilities
from .store.sqlite import LocalStore

runtime_router = APIRouter()
log = logging.getLogger("shejane_runtime.runtime_routes")

_RUNTIME_SETTINGS_TO_FIELDS = {
    "max_model_calls": "max_model_calls",
    "max_tool_retries": "max_tool_retries",
    "research_search_limit": "research_search_limit",
    "unknown_model_max_input_tokens": "unknown_model_max_input_tokens",
    "unknown_model_max_output_tokens": "unknown_model_max_output_tokens",
    "model_request_timeout_seconds": "model_request_timeout_seconds",
    "browser_headless": "browser_headless",
    "subagents": "enable_subagents",
    "input_guard": "input_guard_mode",
    "plan_first": "plan_first_mode",
    "verification_repair_max": "verification_repair_max",
    "repair_workflow_max": "repair_workflow_max",
    "pii_redact": "pii_redact_types",
}


def _runtime_settings_payload(settings: Settings, *, version: int) -> dict[str, Any]:
    return {
        "version": version,
        **{
            public_name: getattr(settings, field_name)
            for public_name, field_name in _RUNTIME_SETTINGS_TO_FIELDS.items()
        },
    }


def _apply_runtime_settings(settings: Settings, values: dict[str, Any]) -> Settings:
    updates = {
        field_name: values[public_name]
        for public_name, field_name in _RUNTIME_SETTINGS_TO_FIELDS.items()
        if public_name in values
    }
    return settings.model_copy(update=updates)


@runtime_router.get("/v1/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    settings: Settings = request.app.state.bootstrap_settings
    return HealthResponse(
        version=__version__,
        pairing_configured=bool(settings.pairing_token),
    )


@runtime_router.get("/v1/runtime", response_model=RuntimeInfo)
async def runtime_info(request: Request) -> RuntimeInfo:
    runtime_settings: Settings = request.app.state.settings
    service_configured = False
    store: LocalStore = request.app.state.store
    try:
        connections = await store.list_model_connections(principal_id=request.state.principal_id)
        for connection in connections:
            if await get_model_api_key(
                request.state.principal_id,
                str(connection["id"]),
                str(connection["credential_ref"]),
            ):
                service_configured = True
                break
    except CredentialStoreError:
        service_configured = False
    return RuntimeInfo(
        protocol_version=RUNTIME_PROTOCOL_VERSION,
        runtime_version=__version__,
        capabilities=sorted(runtime_capabilities(runtime_settings)),
        model_service_configured=service_configured,
    )


@runtime_router.get("/v1/settings", response_model=RuntimeSettingsResponse)
async def get_runtime_settings(request: Request) -> dict[str, Any]:
    return _runtime_settings_payload(
        request.app.state.settings,
        version=request.app.state.runtime_settings_version,
    )


@runtime_router.put("/v1/settings", response_model=RuntimeSettingsResponse)
async def update_runtime_settings(
    request: Request,
    body: UpdateRuntimeSettingsRequest,
) -> dict[str, Any]:
    patch = body.model_dump(exclude_none=True)
    async with request.app.state.runtime_settings_lock:
        current = _runtime_settings_payload(
            request.app.state.settings,
            version=request.app.state.runtime_settings_version,
        )
        current.update(patch)
        validated = RuntimeSettingsResponse(**current)
        candidate_values = validated.model_dump(exclude={"version"})
        store: LocalStore = request.app.state.store
        stored = await store.patch_runtime_settings(
            patch,
            initial_settings=candidate_values,
        )
        persisted = RuntimeSettingsResponse(
            **{**candidate_values, **stored["settings"]},
            version=int(stored["version"]),
        )
        values = persisted.model_dump(exclude={"version"})
        updated = _apply_runtime_settings(request.app.state.settings, values)
        request.app.state.settings = updated
        request.app.state.coordinator.settings = updated
        request.app.state.runtime_settings_version = int(stored["version"])
        return _runtime_settings_payload(updated, version=int(stored["version"]))


@runtime_router.get(
    "/v1/shejane/diagnostics",
    response_model=CentralDiagnosticsStatusResponse,
)
async def get_central_diagnostics(request: Request) -> dict[str, Any]:
    try:
        return await request.app.state.central_diagnostics.status(request.state.principal_id)
    except CredentialStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@runtime_router.put(
    "/v1/shejane/diagnostics",
    response_model=CentralDiagnosticsStatusResponse,
)
async def update_central_diagnostics(
    request: Request,
    body: UpdateCentralDiagnosticsRequest,
) -> dict[str, Any]:
    try:
        return await request.app.state.central_diagnostics.configure(
            principal_id=request.state.principal_id,
            enabled=body.enabled,
            connection_id=body.connection_id,
            success_sample_rate=body.success_sample_rate,
        )
    except CentralDiagnosticsConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (CentralDiagnosticsUnavailable, CredentialStoreError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@runtime_router.get("/v1/tools")
async def list_tools(request: Request) -> dict[str, Any]:
    from .tools.registry import describe_tools

    store = getattr(request.app.state, "store", None)
    return {"tools": describe_tools(store=store, workspace_root=None)}


@runtime_router.delete("/v1/memory", response_model=ClearMemoryResponse)
async def clear_memory(request: Request) -> dict[str, Any]:
    """Wipe this authenticated principal's long-term memory namespaces.

    Backs the "清空记忆 / Clear memory" button in the agent settings
    dialog. Walks every ("notes", ...) namespace in pages of 200
    (matches the BaseStore default search limit ceiling for SQLite stores)
    and deletes each key. Returns the total count so the UI can render an
    accurate "cleared N memories" toast.

    Idempotent: calling it on an empty store returns
    `deleted_count: 0` without error.
    """
    from .tools.memory import NAMESPACE, memory_namespace_prefix

    agent_store = getattr(request.app.state, "agent_store", None)
    if agent_store is None:
        raise HTTPException(status_code=503, detail="memory store not initialized")
    deleted = 0
    page_size = 200
    principal_prefix = memory_namespace_prefix(request.state.principal_id)
    namespaces = [principal_prefix]
    if request.state.principal_id == LOCAL_OWNER_PRINCIPAL_ID:
        namespaces.insert(0, NAMESPACE)
    if hasattr(agent_store, "alist_namespaces"):
        namespaces = [NAMESPACE] if request.state.principal_id == LOCAL_OWNER_PRINCIPAL_ID else []
        offset = 0
        while True:
            page = await agent_store.alist_namespaces(
                prefix=principal_prefix,
                limit=page_size,
                offset=offset,
            )
            if not page:
                break
            namespaces.extend(page)
            if len(page) < page_size:
                break
            offset += len(page)
    for namespace in namespaces:
        while True:
            items = await agent_store.asearch(namespace, limit=page_size)
            if not items:
                break
            for item in items:
                try:
                    await agent_store.adelete(namespace, item.key)
                    deleted += 1
                except Exception as exc:
                    log.warning(
                        "memory delete failed namespace=%s key=%s: %s",
                        namespace,
                        item.key,
                        exc,
                    )
            if len(items) < page_size:
                break
    return {"cleared": True, "deleted_count": deleted}
