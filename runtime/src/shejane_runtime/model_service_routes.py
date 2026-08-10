from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .api_schemas import (
    AddModelServiceModelRequest,
    ConnectModelServiceRequest,
    ImportModelServiceRequest,
    ListModelCapabilityBindingsResponse,
    ListModelServiceConnectionsResponse,
    LocalRuntimeModelCatalog,
    ModelCapabilityBinding,
    ModelServiceConnection,
    ModelServiceModel,
    ModelServicePresetCatalog,
    ReconnectModelServiceRequest,
    SetModelCapabilityBindingRequest,
    SheJaneAuthorizationStartResponse,
    SheJaneAuthorizationStatusResponse,
    VerifyModelServiceModelRequest,
)
from .model_credentials import (
    CredentialStoreError,
    credential_ref,
    delete_model_api_key,
    get_model_api_key,
    new_credential_ref,
    set_model_api_key,
)
from .model_profiles import MODEL_CAPABILITY_ORDER, default_model_protocol, model_capability
from .model_service_catalog import (
    _ensure_default_model_capability_binding,
    _merge_refreshed_model_catalog,
    _model_capability_binding_response,
    _model_connection_models,
    _model_service_base_url,
    _model_service_response,
    _refresh_model_service_models,
)
from .model_service_probes import (
    _verify_model_service_capability,
)
from .model_services import (
    adapter_for_custom_service,
    list_model_service_presets,
    model_service_preset,
)
from .shejane_authorization import OfficialServiceUnavailable
from .store.sqlite import LocalStore

log = logging.getLogger("shejane_runtime.server")
model_service_router = APIRouter()


@model_service_router.get(
    "/v1/model-services/presets",
    response_model=ModelServicePresetCatalog,
)
async def list_model_services_presets() -> dict[str, Any]:
    return {"services": list_model_service_presets()}


@model_service_router.post(
    "/v1/model-services/shejane/authorization",
    response_model=SheJaneAuthorizationStartResponse,
    status_code=201,
)
async def start_shejane_authorization(request: Request) -> dict[str, Any]:
    if await request.body():
        raise HTTPException(
            status_code=400,
            detail="authorization start does not accept configuration",
        )
    try:
        return await request.app.state.shejane_authorization.start(request.state.principal_id)
    except OfficialServiceUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "official_service_unconfigured",
                "message": "SheJane 官方服务尚未配置。",
            },
        ) from exc


@model_service_router.get(
    "/v1/model-services/shejane/authorization/{authorization_id}",
    response_model=SheJaneAuthorizationStatusResponse,
)
async def get_shejane_authorization(
    request: Request,
    authorization_id: str,
) -> dict[str, Any]:
    try:
        return request.app.state.shejane_authorization.status(
            authorization_id,
            request.state.principal_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="authorization not found") from exc


@model_service_router.get(
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


@model_service_router.get(
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


@model_service_router.put(
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


@model_service_router.delete(
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


@model_service_router.post(
    "/v1/model-services",
    response_model=ModelServiceConnection,
    status_code=201,
)
async def connect_model_service(
    request: Request,
    body: ConnectModelServiceRequest,
) -> ModelServiceConnection:
    preset = model_service_preset(body.preset_id)
    if preset is None:
        raise HTTPException(status_code=400, detail="model service is not supported")
    if preset["connection_method"] == "browser_authorization":
        raise HTTPException(
            status_code=400,
            detail="model service requires browser authorization",
        )
    api_key = body.api_key.strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="API key is required")
    if body.preset_id == "custom":
        if not body.name or not body.base_url:
            raise HTTPException(
                status_code=400,
                detail="custom model service needs a name and address",
            )
        name = body.name.strip()
        region = "custom"
        base_url = _model_service_base_url(body.base_url)
        if body.adapter_id == "google_genai":
            models, catalog_status = await _refresh_model_service_models(
                preset=preset,
                base_url=base_url,
                adapter_id="google_genai",
                api_key=api_key,
            )
            if catalog_status != "ready":
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "adapter_detection_failed",
                        "message": "无法通过 Google GenerateContent 接口读取模型列表。",
                    },
                )
            adapter_id = "google_genai"
        else:
            detected: dict[str, tuple[list[dict[str, Any]], str]] = {}
            credential_error: HTTPException | None = None
            for candidate in ("openai_chat", "anthropic_messages"):
                try:
                    candidate_models, candidate_status = await _refresh_model_service_models(
                        preset=preset,
                        base_url=base_url,
                        adapter_id=candidate,
                        api_key=api_key,
                    )
                except HTTPException as exc:
                    if exc.status_code == 401:
                        credential_error = exc
                    continue
                if candidate_status == "ready":
                    detected[candidate] = (candidate_models, candidate_status)
            adapter_id = body.adapter_id or adapter_for_custom_service(
                openai_chat_available="openai_chat" in detected,
                anthropic_messages_available="anthropic_messages" in detected,
            )
            if adapter_id is None:
                if credential_error is not None:
                    raise credential_error
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "adapter_detection_failed",
                        "message": "无法自动识别接口格式，请在高级设置中选择接口格式。",
                    },
                )
            if (
                body.adapter_id is not None
                and adapter_id not in detected
                and credential_error is not None
            ):
                raise credential_error
            models, catalog_status = detected.get(adapter_id, ([], "unavailable"))
    else:
        if body.name is not None or body.adapter_id is not None:
            raise HTTPException(
                status_code=400,
                detail="official model service transport cannot be overridden",
            )
        regions = list(preset["regions"])
        region_id = body.region or next(str(item["id"]) for item in regions if item["default"])
        region_config = next(
            (item for item in regions if item["id"] == region_id),
            None,
        )
        if region_config is None:
            raise HTTPException(
                status_code=400,
                detail="model service region is not supported",
            )
        name = str(preset["name"])
        region = str(region_config["id"])
        base_url = _model_service_base_url(body.base_url or str(region_config["base_url"]))
        adapter_id = str(preset["adapter_id"])
        models, catalog_status = await _refresh_model_service_models(
            preset=preset,
            base_url=base_url,
            adapter_id=adapter_id,
            api_key=api_key,
        )
    connection_id = f"conn_{uuid.uuid4().hex}"
    next_credential_ref = credential_ref(connection_id)
    principal_id = request.state.principal_id
    try:
        await set_model_api_key(
            principal_id,
            connection_id,
            api_key,
            next_credential_ref,
        )
        try:
            row = await request.app.state.store.create_model_connection(
                principal_id=principal_id,
                connection_id=connection_id,
                preset_id=body.preset_id,
                name=name,
                region=region,
                adapter_id=adapter_id,
                base_url=base_url,
                requires_api_key=True,
                credential_ref=next_credential_ref,
                models=models,
                catalog_status=catalog_status,
            )
        except BaseException:
            await delete_model_api_key(
                principal_id,
                connection_id,
                next_credential_ref,
            )
            raise
    except CredentialStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return await _model_service_response(row, credential_configured=True)


@model_service_router.post(
    "/v1/model-services/import",
    response_model=ModelServiceConnection,
    status_code=201,
)
async def import_model_service(
    request: Request,
    body: ImportModelServiceRequest,
) -> ModelServiceConnection:
    principal_id = request.state.principal_id
    store: LocalStore = request.app.state.store
    if await store.get_model_connection(
        principal_id=principal_id,
        connection_id=body.id,
    ):
        raise HTTPException(status_code=409, detail="model service already exists")
    preset = model_service_preset(body.preset_id)
    if preset is None:
        raise HTTPException(status_code=400, detail="model service is not supported")
    if preset["connection_method"] == "browser_authorization":
        raise HTTPException(
            status_code=400,
            detail="official service must be authorized again after import",
        )
    if body.preset_id == "custom":
        raise HTTPException(
            status_code=400,
            detail="custom model services must be reconnected manually",
        )
    region_config = next(
        (item for item in preset["regions"] if item["id"] == body.region),
        None,
    )
    if region_config is None:
        raise HTTPException(
            status_code=400,
            detail="model service region is not supported",
        )
    name = str(preset["name"])
    region = str(region_config["id"])
    adapter_id = str(preset["adapter_id"])
    base_url = str(region_config["base_url"])
    models = [dict(model) for model in preset["models"]]
    row = await store.create_model_connection(
        principal_id=principal_id,
        connection_id=body.id,
        preset_id=body.preset_id,
        name=name,
        region=region,
        adapter_id=adapter_id,
        base_url=base_url,
        requires_api_key=True,
        credential_ref=credential_ref(body.id),
        models=models,
        catalog_status="stale" if models else "unavailable",
    )
    return await _model_service_response(row, credential_configured=False)


@model_service_router.put(
    "/v1/model-services/{connection_id}/credential",
    response_model=ModelServiceConnection,
)
async def reconnect_model_service(
    request: Request,
    connection_id: str,
    body: ReconnectModelServiceRequest,
) -> ModelServiceConnection:
    principal_id = request.state.principal_id
    store: LocalStore = request.app.state.store
    row = await store.get_model_connection(
        principal_id=principal_id,
        connection_id=connection_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="model service not found")
    if row["preset_id"] == "shejane-official":
        raise HTTPException(
            status_code=400,
            detail="managed official credentials cannot be replaced",
        )
    api_key = body.api_key.strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="API key is required")
    base_url = _model_service_base_url(body.base_url or str(row["base_url"]))
    preset = model_service_preset(str(row["preset_id"])) or {"models": ()}
    models, catalog_status = await _refresh_model_service_models(
        preset=preset,
        base_url=base_url,
        adapter_id=str(row["adapter_id"]),
        api_key=api_key,
    )
    if catalog_status != "ready":
        raise HTTPException(
            status_code=503,
            detail={
                "code": "provider_unavailable",
                "message": "暂时无法验证新的 API Key，旧 Key 已保留。",
            },
        )
    next_credential_ref = new_credential_ref(connection_id)
    credential_swapped = False
    try:
        await set_model_api_key(
            principal_id,
            connection_id,
            api_key,
            next_credential_ref,
        )
        try:
            async with request.app.state.coordinator.model_connection_mutation(
                principal_id=principal_id,
                connection_id=connection_id,
            ):
                current = await store.get_model_connection(
                    principal_id=principal_id,
                    connection_id=connection_id,
                )
                if current is None:
                    raise HTTPException(status_code=404, detail="model service not found")
                previous_credential_ref = str(current["credential_ref"])
                updated = await store.replace_model_connection_credential(
                    principal_id=principal_id,
                    connection_id=connection_id,
                    credential_ref=next_credential_ref,
                    base_url=base_url,
                    models=models,
                    catalog_status=catalog_status,
                )
                assert updated is not None
                credential_swapped = True
                try:
                    await delete_model_api_key(
                        principal_id,
                        connection_id,
                        previous_credential_ref,
                    )
                except CredentialStoreError:
                    log.warning(
                        "old model-service credential remains after reconnect",
                        extra={"connection_id": connection_id},
                    )
        except BaseException:
            if not credential_swapped:
                await delete_model_api_key(
                    principal_id,
                    connection_id,
                    next_credential_ref,
                )
            raise
    except CredentialStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return await _model_service_response(updated, credential_configured=True)


@model_service_router.post(
    "/v1/model-services/{connection_id}/refresh",
    response_model=ModelServiceConnection,
)
async def refresh_model_service(
    request: Request,
    connection_id: str,
) -> ModelServiceConnection:
    principal_id = request.state.principal_id
    store: LocalStore = request.app.state.store
    row = await store.get_model_connection(
        principal_id=principal_id,
        connection_id=connection_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="model service not found")
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
    preset = model_service_preset(str(row["preset_id"])) or {
        "models": tuple(
            model for model in _model_connection_models(row) if model.get("source") == "bundled"
        )
    }
    models, catalog_status = await _refresh_model_service_models(
        preset=preset,
        base_url=str(row["base_url"]),
        adapter_id=str(row["adapter_id"]),
        api_key=api_key,
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
        cached = _model_connection_models(current)
        models = (
            _merge_refreshed_model_catalog(cached, models)
            if catalog_status == "ready"
            else cached or models
        )
        updated = await store.update_model_connection_catalog(
            principal_id=principal_id,
            connection_id=connection_id,
            models=models,
            catalog_status=catalog_status,
        )
    assert updated is not None
    return await _model_service_response(updated, credential_configured=True)


@model_service_router.post(
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


@model_service_router.post(
    "/v1/model-services/{connection_id}/models/{model_id:path}/verify",
    response_model=ModelServiceModel,
)
async def verify_model_service_model(
    request: Request,
    connection_id: str,
    model_id: str,
    body: VerifyModelServiceModelRequest | None = None,
) -> dict[str, Any]:
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


@model_service_router.delete(
    "/v1/model-services/{connection_id}",
    status_code=204,
    response_class=Response,
)
async def delete_model_service(
    request: Request,
    connection_id: str,
) -> Response:
    principal_id = request.state.principal_id
    store: LocalStore = request.app.state.store
    row = await store.get_model_connection(
        principal_id=principal_id,
        connection_id=connection_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="model service not found")
    try:
        async with request.app.state.coordinator.model_connection_mutation(
            principal_id=principal_id,
            connection_id=connection_id,
        ):
            credential_reference = str(row["credential_ref"])
            current_key = await get_model_api_key(
                principal_id,
                connection_id,
                credential_reference,
            )
            await delete_model_api_key(
                principal_id,
                connection_id,
                credential_reference,
            )
            try:
                deleted = await store.delete_model_connection(
                    principal_id=principal_id,
                    connection_id=connection_id,
                )
                assert deleted is not None
            except BaseException:
                if current_key:
                    await set_model_api_key(
                        principal_id,
                        connection_id,
                        current_key,
                        credential_reference,
                    )
                raise
    except CredentialStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return Response(status_code=204)


@model_service_router.get("/v1/models", response_model=LocalRuntimeModelCatalog)
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
                        "available": configured and agent_capability is not None,
                    }
                )
    except CredentialStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"models": models}
