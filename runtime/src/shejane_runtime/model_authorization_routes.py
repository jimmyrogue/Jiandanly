from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .api_schemas import (
    SheJaneAuthorizationStartResponse,
    SheJaneAuthorizationStatusResponse,
)
from .shejane_authorization import OfficialServiceUnavailable

model_authorization_router = APIRouter()


@model_authorization_router.post(
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


@model_authorization_router.get(
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
