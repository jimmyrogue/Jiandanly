from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ..api_schemas import (
    ConnectModelServiceRequest,
    ImportModelServiceRequest,
    ModelServiceConnection,
    ReconnectModelServiceRequest,
)
from ..store.sqlite import LocalStore
from . import (
    adapter_for_custom_service,
    model_service_preset,
)
from .authorization_routes import (
    get_shejane_authorization as get_shejane_authorization,
)
from .authorization_routes import (
    model_authorization_router,
)
from .authorization_routes import (
    start_shejane_authorization as start_shejane_authorization,
)
from .catalog import (
    _merge_refreshed_model_catalog,
    _model_connection_models,
    _model_service_base_url,
    _model_service_response,
    _refresh_model_service_models,
)
from .catalog_routes import (
    add_model_service_model as add_model_service_model,
)
from .catalog_routes import (
    delete_model_capability_binding as delete_model_capability_binding,
)
from .catalog_routes import (
    list_model_capability_bindings as list_model_capability_bindings,
)
from .catalog_routes import (
    list_model_services as list_model_services,
)
from .catalog_routes import (
    list_model_services_presets as list_model_services_presets,
)
from .catalog_routes import (
    list_runtime_models as list_runtime_models,
)
from .catalog_routes import (
    model_catalog_router,
)
from .catalog_routes import (
    set_model_capability_binding as set_model_capability_binding,
)
from .catalog_routes import (
    verify_model_service_model as verify_model_service_model,
)
from .credentials import (
    CredentialStoreError,
    credential_ref,
    delete_model_api_key,
    get_model_api_key,
    new_credential_ref,
    set_model_api_key,
)
from .probes import (
    _verify_model_service_capability as _verify_model_service_capability,
)

log = logging.getLogger("shejane_runtime.server")
model_service_router = APIRouter()
model_service_router.include_router(model_catalog_router)
model_service_router.include_router(model_authorization_router)


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
