from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from .api_schemas import (
    CreateScheduledRunRequest,
    ListScheduledRunsResponse,
    LocalScheduledRun,
)
from .http_route_helpers import _normalized_path
from .run_configuration import freeze_run_settings, sanitize_run_metadata
from .store.sqlite import LocalStore, WorkspaceAdmissionError

schedule_router = APIRouter()


def _normalize_schedule_time(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise HTTPException(status_code=400, detail="run_at required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="run_at must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


@schedule_router.get("/v1/schedules", response_model=ListScheduledRunsResponse)
async def list_schedules(
    request: Request,
    status: str | None = Query(default=None),
    notify_pending: bool = Query(default=False),
) -> dict[str, Any]:
    store: LocalStore = request.app.state.store
    schedules = await store.list_scheduled_runs_for_principal(
        principal_id=request.state.principal_id,
        status=status,
        notify_pending=notify_pending,
    )
    return {"schedules": schedules}


@schedule_router.post("/v1/schedules", response_model=LocalScheduledRun)
async def create_schedule(
    request: Request,
    body: CreateScheduledRunRequest,
) -> dict[str, Any]:
    goal = body.goal.strip()
    if not goal:
        raise HTTPException(status_code=400, detail="goal required")
    store: LocalStore = request.app.state.store
    principal_id = request.state.principal_id
    workspace_path = (
        await _normalized_path(body.workspace_path) if body.workspace_path is not None else None
    )
    try:
        frozen_settings = freeze_run_settings(
            request.app.state.settings,
            {**(body.settings or {}), "permission_mode": body.permission_mode},
        )
        if body.reasoning_mode is not None:
            frozen_settings["reasoning_mode"] = body.reasoning_mode
        return await store.create_scheduled_run(
            principal_id=principal_id,
            goal=goal,
            run_at=_normalize_schedule_time(body.run_at),
            workspace_path=workspace_path,
            model=body.model.strip(),
            history=body.history or [],
            settings=frozen_settings,
            metadata=sanitize_run_metadata(body.metadata),
        )
    except WorkspaceAdmissionError as exc:
        status_code = 409 if "no longer available" in str(exc) else 403
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@schedule_router.delete("/v1/schedules/{schedule_id}", response_model=LocalScheduledRun)
async def cancel_schedule(request: Request, schedule_id: str) -> dict[str, Any]:
    store: LocalStore = request.app.state.store
    schedule = await store.cancel_scheduled_run(
        principal_id=request.state.principal_id,
        schedule_id=schedule_id,
    )
    if schedule is None:
        raise HTTPException(status_code=404, detail="schedule not found")
    return schedule


@schedule_router.post(
    "/v1/schedules/{schedule_id}/notified",
    response_model=LocalScheduledRun,
)
async def mark_schedule_notified(request: Request, schedule_id: str) -> dict[str, Any]:
    store: LocalStore = request.app.state.store
    schedule = await store.mark_scheduled_run_notified(
        principal_id=request.state.principal_id,
        schedule_id=schedule_id,
    )
    if schedule is None:
        raise HTTPException(status_code=404, detail="schedule not found")
    return schedule
