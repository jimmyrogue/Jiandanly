from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request

from .api_schemas import (
    FixedRuntimeAssetStatus,
    ListPluginsResponse,
    PluginDetail,
    PluginReadinessSnapshot,
    RuntimeAssetCleanupResult,
    RuntimeAssetStorage,
)
from .plugins.registry import PluginRegistry, PluginRegistryError

plugin_router = APIRouter()


def _plugin_http_error(exc: PluginRegistryError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc)},
    )


@plugin_router.get("/v1/plugins", response_model=ListPluginsResponse)
async def list_plugins(request: Request) -> dict[str, Any]:
    registry: PluginRegistry = request.app.state.plugin_registry
    return {"plugins": await registry.list(principal_id=request.state.principal_id)}


@plugin_router.get(
    "/v1/plugins/runtime-assets/storage",
    response_model=RuntimeAssetStorage,
)
async def inspect_runtime_asset_storage(request: Request) -> dict[str, Any]:
    registry: PluginRegistry = request.app.state.plugin_registry
    return await registry.runtime_asset_storage()


@plugin_router.delete(
    "/v1/plugins/runtime-assets/storage",
    response_model=RuntimeAssetCleanupResult,
)
async def cleanup_runtime_asset_storage(
    request: Request,
    scope: Literal["history", "all"] = Query(...),
) -> dict[str, Any]:
    registry: PluginRegistry = request.app.state.plugin_registry
    try:
        return await registry.cleanup_runtime_asset_storage(scope)
    except PluginRegistryError as exc:
        raise _plugin_http_error(exc) from exc


@plugin_router.get("/v1/plugins/{plugin_id}", response_model=PluginDetail)
async def inspect_plugin(request: Request, plugin_id: str) -> dict[str, Any]:
    registry: PluginRegistry = request.app.state.plugin_registry
    try:
        return await registry.inspect(
            principal_id=request.state.principal_id,
            plugin_id=plugin_id,
        )
    except PluginRegistryError as exc:
        raise _plugin_http_error(exc) from exc


@plugin_router.get(
    "/v1/plugins/{plugin_id}/runtime-asset",
    response_model=FixedRuntimeAssetStatus,
    response_model_exclude_defaults=True,
)
async def inspect_fixed_runtime_asset(request: Request, plugin_id: str) -> dict[str, Any]:
    registry: PluginRegistry = request.app.state.plugin_registry
    try:
        return await registry.fixed_runtime_asset_status(
            principal_id=request.state.principal_id,
            plugin_id=plugin_id,
        )
    except PluginRegistryError as exc:
        if exc.status_code == 404 and plugin_id in {
            "org.shejane.browser-qa",
            "org.shejane.ocr",
        }:
            return {
                "plugin_id": plugin_id,
                "available": False,
                "downloaded": False,
            }
        raise _plugin_http_error(exc) from exc


@plugin_router.put(
    "/v1/plugins/{plugin_id}/runtime-asset",
    response_model=FixedRuntimeAssetStatus,
    response_model_exclude_defaults=True,
)
async def prepare_fixed_runtime_asset(request: Request, plugin_id: str) -> dict[str, Any]:
    registry: PluginRegistry = request.app.state.plugin_registry
    try:
        return await registry.prepare_fixed_runtime_asset(
            principal_id=request.state.principal_id,
            plugin_id=plugin_id,
        )
    except PluginRegistryError as exc:
        raise _plugin_http_error(exc) from exc


@plugin_router.delete(
    "/v1/plugins/{plugin_id}/runtime-asset",
    response_model=FixedRuntimeAssetStatus,
    response_model_exclude_defaults=True,
)
async def remove_fixed_runtime_asset(request: Request, plugin_id: str) -> dict[str, Any]:
    registry: PluginRegistry = request.app.state.plugin_registry
    try:
        return await registry.remove_fixed_runtime_asset(
            principal_id=request.state.principal_id,
            plugin_id=plugin_id,
        )
    except PluginRegistryError as exc:
        raise _plugin_http_error(exc) from exc


@plugin_router.get(
    "/v1/plugins/{plugin_id}/readiness",
    response_model=PluginReadinessSnapshot,
)
async def inspect_plugin_readiness(request: Request, plugin_id: str) -> dict[str, Any]:
    if plugin_id != "org.shejane.computer-use":
        raise HTTPException(status_code=404, detail="plugin readiness is unavailable")
    registry: PluginRegistry = request.app.state.plugin_registry
    try:
        return await registry.computer_use_readiness(principal_id=request.state.principal_id)
    except PluginRegistryError as exc:
        raise _plugin_http_error(exc) from exc
