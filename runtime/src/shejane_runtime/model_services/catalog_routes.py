from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ..api_schemas import (
    AddModelServiceModelRequest,
    ListModelCapabilityBindingsResponse,
    ListModelServiceConnectionsResponse,
    LocalRuntimeModelCatalog,
    ModelCapabilityBinding,
    ModelServiceModel,
    ModelServicePresetCatalog,
    SetModelCapabilityBindingRequest,
    VerifyModelServiceModelRequest,
)
from ..store.sqlite import LocalStore
from . import list_model_service_presets
from .catalog import (
    _ensure_default_model_capability_binding,
    _model_capability_binding_response,
    _model_connection_models,
    _model_service_response,
)
from .credentials import CredentialStoreError, get_model_api_key
from .profiles import (
    MODEL_CAPABILITY_ORDER,
    default_model_protocol,
    model_capability,
)

model_catalog_router = APIRouter()


@model_catalog_router.get(
    "/v1/model-services/presets",
    response_model=ModelServicePresetCatalog,
)
async def list_model_services_presets() -> dict[str, Any]:
    return {"services": list_model_service_presets()}


@model_catalog_router.get(
    "/v1/model-services",
    response_model=ListModelServiceConnectionsResponse,
)
async def list_model_services(request: Request) -> dict[str, Any]:
    store: LocalStore = request.app.state.store
    try:
        rows = await store.list_model_connections(principal_id=request.state.principal_id)
        services = await asyncio.gather(*(_model_service_response(row) for row in rows))
    except CredentialStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"services": services}


@model_catalog_router.post(
    "/v1/model-services/{connection_id}/models",
    response_model=ModelServiceModel,
    status_code=201,
)
async def add_model_service_model(
    request: Request,
    connection_id: str,
    body: AddModelServiceModelRequest,
) -> dict[str, Any]:
    principal_id = request.state.principal_id
    store: LocalStore = request.app.state.store
    model = {
        "model_id": body.model_id,
        "display_name": body.display_name or body.model_id,
        "capabilities": [],
        "source": "manual",
        "verification": "unverified",
        "recommended": False,
        "tool_calling": False,
        "streaming": False,
        "image_inputs": False,
        "max_input_tokens": None,
        "max_output_tokens": None,
        "provider_family": "unknown",
        "reasoning": {
            "supported": False,
            "modes": ["off"],
            "default_mode": "off",
            "stream_field": None,
            "tool_roundtrip_required": False,
            "display_policy": "activity_only",
        },
    }
    async with request.app.state.coordinator.model_connection_catalog_update(
        principal_id=principal_id,
        connection_id=connection_id,
    ):
        current = await store.get_model_connection(
            principal_id=principal_id,
            connection_id=connection_id,
        )
        if current is None:
            raise HTTPException(status_code=404, detail="model service not found")
        models = _model_connection_models(current)
        if any(item["model_id"] == body.model_id for item in models):
            raise HTTPException(status_code=409, detail="model already exists")
        models.append(model)
        await store.update_model_connection_catalog(
            principal_id=principal_id,
            connection_id=connection_id,
            models=models,
            catalog_status=str(current["catalog_status"]),
        )
    return model


@model_catalog_router.post(
    "/v1/model-services/{connection_id}/models/{model_id:path}/verify",
    response_model=ModelServiceModel,
)
async def verify_model_service_model(
    request: Request,
    connection_id: str,
    model_id: str,
    body: VerifyModelServiceModelRequest | None = None,
) -> dict[str, Any]:
    # Keep verification lookup on the routes interface so callers can override it.
    from .routes import _verify_model_service_capability

    principal_id = request.state.principal_id
    store: LocalStore = request.app.state.store
    row = await store.get_model_connection(
        principal_id=principal_id,
        connection_id=connection_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="model service not found")
    expected_connection = (
        int(row.get("version") or 1),
        str(row["credential_ref"]),
        str(row["base_url"]),
    )
    models = _model_connection_models(row)
    model = next((item for item in models if item["model_id"] == model_id), None)
    if model is None:
        raise HTTPException(status_code=404, detail="model not found")
    if body is None:
        capability = model_capability(model, "agent_chat")
        body = VerifyModelServiceModelRequest(
            capability="agent_chat",
            protocol=(
                str(capability["protocol"])
                if capability is not None
                else "openai_responses"
                if row.get("preset_id") == "openai"
                else default_model_protocol(str(row.get("adapter_id")), "agent_chat")
            ),
        )
    try:
        api_key = await get_model_api_key(
            principal_id,
            connection_id,
            str(row["credential_ref"]),
        )
    except CredentialStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not api_key:
        raise HTTPException(status_code=409, detail="model service needs an API key")
    await _verify_model_service_capability(
        settings=request.app.state.settings,
        base_url=str(row["base_url"]),
        capability=body.capability,
        protocol=body.protocol,
        api_key=api_key,
        model_id=model_id,
        provider_family=str(model.get("provider_family") or "unknown"),
    )
    async with request.app.state.coordinator.model_connection_catalog_update(
        principal_id=principal_id,
        connection_id=connection_id,
    ):
        current = await store.get_model_connection(
            principal_id=principal_id,
            connection_id=connection_id,
        )
        if current is None:
            raise HTTPException(status_code=404, detail="model service not found")
        if (
            int(current.get("version") or 1),
            str(current["credential_ref"]),
            str(current["base_url"]),
        ) != expected_connection:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "model_service_changed",
                    "message": "模型服务在验证期间已更新，请重新验证。",
                },
            )
        models = _model_connection_models(current)
        model = next((item for item in models if item["model_id"] == model_id), None)
        if model is None:
            raise HTTPException(status_code=404, detail="model not found")
        capabilities = {
            item["capability"]: dict(item)
            for item in model.get("capabilities", [])
            if isinstance(item, dict) and item.get("capability")
        }
        capabilities[body.capability] = {
            "capability": body.capability,
            "protocol": body.protocol,
            "verification": "verified",
        }
        model["capabilities"] = sorted(
            capabilities.values(),
            key=lambda item: MODEL_CAPABILITY_ORDER[str(item["capability"])],
        )
        model["verification"] = "verified"
        if body.capability == "agent_chat":
            model["tool_calling"] = True
            model["streaming"] = True
        if body.capability == "image_understanding":
            model["image_inputs"] = True
        await store.update_model_connection_catalog(
            principal_id=principal_id,
            connection_id=connection_id,
            models=models,
            catalog_status=str(current["catalog_status"]),
        )
    return model


@model_catalog_router.get(
    "/v1/model-capability-bindings",
    response_model=ListModelCapabilityBindingsResponse,
)
async def list_model_capability_bindings(request: Request) -> dict[str, Any]:
    principal_id = request.state.principal_id
    store: LocalStore = request.app.state.store
    for capability in ("image_generation", "image_editing"):
        await _ensure_default_model_capability_binding(
            store,
            principal_id=principal_id,
            capability_name=capability,
        )
    rows = await store.list_model_capability_bindings(principal_id=principal_id)
    return {
        "bindings": [
            await _model_capability_binding_response(
                store,
                principal_id=principal_id,
                row=row,
            )
            for row in rows
        ]
    }


@model_catalog_router.put(
    "/v1/model-capability-bindings/{capability}",
    response_model=ModelCapabilityBinding,
)
async def set_model_capability_binding(
    request: Request,
    capability: str,
    body: SetModelCapabilityBindingRequest,
) -> dict[str, Any]:
    if capability not in {"image_generation", "image_editing"}:
        raise HTTPException(status_code=404, detail="model capability is not bindable")
    parts = body.model_spec.split(":", 2)
    connection_id, model_id = parts[1], parts[2]
    principal_id = request.state.principal_id
    store: LocalStore = request.app.state.store
    connection = await store.get_model_connection(
        principal_id=principal_id,
        connection_id=connection_id,
    )
    if connection is None:
        raise HTTPException(status_code=404, detail="model service not found")
    model = next(
        (item for item in _model_connection_models(connection) if item.get("model_id") == model_id),
        None,
    )
    selected = model_capability(model, capability) if model is not None else None
    if selected is None or selected.get("verification") != "verified":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "model_capability_unverified",
                "message": "请先验证这个模型的对应能力。",
            },
        )
    row = await store.set_model_capability_binding(
        principal_id=principal_id,
        capability=capability,
        connection_id=connection_id,
        connection_version=int(connection.get("version") or 1),
        model_id=model_id,
        protocol=str(selected["protocol"]),
    )
    return await _model_capability_binding_response(
        store,
        principal_id=principal_id,
        row=row,
    )


@model_catalog_router.delete(
    "/v1/model-capability-bindings/{capability}",
    status_code=204,
    response_class=Response,
)
async def delete_model_capability_binding(
    request: Request,
    capability: str,
) -> Response:
    if capability not in {"image_generation", "image_editing"}:
        raise HTTPException(status_code=404, detail="model capability is not bindable")
    await request.app.state.store.delete_model_capability_binding(
        principal_id=request.state.principal_id,
        capability=capability,
    )
    return Response(status_code=204)


@model_catalog_router.get("/v1/models", response_model=LocalRuntimeModelCatalog)
async def list_runtime_models(request: Request) -> dict[str, Any]:
    store: LocalStore = request.app.state.store
    models: list[dict[str, Any]] = []
    if request.app.state.settings.fake_llm:
        models.append(
            {
                "spec": "local:test:model",
                "model_id": "model",
                "display_name": "Test model",
                "connection_id": "test",
                "service_name": "Test",
                "capabilities": [
                    {
                        "capability": "agent_chat",
                        "protocol": "openai_chat_completions",
                        "verification": "verified",
                    }
                ],
                "tool_calling": True,
                "streaming": True,
                "image_inputs": False,
                "verification": "verified",
                "recommended": True,
                "max_input_tokens": 128_000,
                "max_output_tokens": 8_192,
                "provider_family": "unknown",
                "reasoning": {
                    "supported": False,
                    "modes": ["off"],
                    "default_mode": "off",
                    "stream_field": None,
                    "tool_roundtrip_required": False,
                    "display_policy": "activity_only",
                },
                "available": True,
            }
        )
    try:
        rows = await store.list_model_connections(principal_id=request.state.principal_id)
        configured_connections = await asyncio.gather(
            *(
                get_model_api_key(
                    request.state.principal_id,
                    str(row["id"]),
                    str(row["credential_ref"]),
                )
                for row in rows
            )
        )
        for row, api_key in zip(rows, configured_connections, strict=True):
            configured = bool(api_key)
            for model in _model_connection_models(row):
                agent_capability = model_capability(model, "agent_chat")
                models.append(
                    {
                        "spec": f"local:{row['id']}:{model['model_id']}",
                        "model_id": model["model_id"],
                        "display_name": model["display_name"],
                        "connection_id": row["id"],
                        "service_name": row["name"],
                        "capabilities": model.get("capabilities", []),
                        "tool_calling": bool(model.get("tool_calling")),
                        "streaming": bool(model.get("streaming")),
                        "image_inputs": bool(model.get("image_inputs")),
                        "verification": model.get("verification", "unverified"),
                        "recommended": "agent_chat" in model.get("recommended_for", []),
                        "max_input_tokens": model.get("max_input_tokens"),
                        "max_output_tokens": model.get("max_output_tokens"),
                        "provider_family": model.get("provider_family", "unknown"),
                        "reasoning": model.get("reasoning"),
                        "hosted_web_search": model.get("hosted_web_search"),
                        "available": configured and agent_capability is not None,
                    }
                )
    except CredentialStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"models": models}
