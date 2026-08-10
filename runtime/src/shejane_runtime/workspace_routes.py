from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .api_schemas import (
    CreateWorkspaceRequest,
    DiagnoseWorkspaceRequest,
    ListWorkspacesResponse,
    LocalWorkspaceAuthorization,
    LocalWorkspaceDiagnosis,
)
from .http_route_helpers import _normalized_path
from .store.sqlite import LocalStore

workspace_router = APIRouter()


@workspace_router.get("/v1/workspaces", response_model=ListWorkspacesResponse)
async def list_workspaces(request: Request) -> dict[str, Any]:
    store: LocalStore = request.app.state.store
    return {"workspaces": await store.list_workspaces(principal_id=request.state.principal_id)}


@workspace_router.post("/v1/workspaces", response_model=LocalWorkspaceAuthorization)
async def add_workspace(
    request: Request,
    body: CreateWorkspaceRequest,
) -> dict[str, Any]:
    """Authorize a workspace path. Returns the flat row — the TS
    `authorizeLocalWorkspace` reads `.id / .path / .label` directly
    (no wrapper)."""
    store: LocalStore = request.app.state.store
    raw_path = body.path.strip()
    if not raw_path:
        raise HTTPException(status_code=400, detail="path required")
    path = await _normalized_path(raw_path)
    if not await asyncio.to_thread(Path(path).is_dir):
        raise HTTPException(status_code=400, detail="workspace must be an existing directory")
    return await store.create_workspace(
        principal_id=request.state.principal_id,
        path=path,
        label=body.label.strip() or path,
    )


@workspace_router.delete(
    "/v1/workspaces/{workspace_id}",
    response_model=LocalWorkspaceAuthorization,
)
async def remove_workspace(request: Request, workspace_id: str) -> dict[str, Any]:
    """Revoke a workspace authorization. Returns the deleted row
    matching the TS `revokeLocalWorkspace` →
    `Promise<LocalWorkspaceAuthorization>` signature."""
    store: LocalStore = request.app.state.store
    principal_id = request.state.principal_id
    existing = next(
        (
            workspace
            for workspace in await store.list_workspaces(principal_id=principal_id)
            if workspace["id"] == workspace_id
        ),
        None,
    )
    if existing is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    await store.delete_workspace(principal_id=principal_id, workspace_id=workspace_id)
    return existing


@workspace_router.post(
    "/v1/workspaces/diagnose",
    response_model=LocalWorkspaceDiagnosis,
    response_model_exclude_none=True,
)
async def diagnose_workspace(
    request: Request,
    body: DiagnoseWorkspaceRequest,
) -> dict[str, Any]:
    """Inspect a candidate path against the authorization registry.

    Response matches the TS `LocalWorkspaceDiagnosis` shape — the
    `reason` enum drives the workspace-picker's "why disabled?"
    copy, keep it stable.
    """
    store: LocalStore = request.app.state.store
    path = body.path.strip()
    if not path:
        raise HTTPException(status_code=400, detail="path required")
    resolved = await _normalized_path(path)
    path_obj = Path(resolved)
    exists, is_directory = await asyncio.gather(
        asyncio.to_thread(path_obj.exists),
        asyncio.to_thread(path_obj.is_dir),
    )
    workspace = await store.workspace_by_path(
        principal_id=request.state.principal_id,
        path=resolved,
    )
    authorized = workspace is not None
    if not exists:
        reason = "not_found"
    elif not is_directory:
        reason = "not_directory"
    elif authorized:
        reason = "authorized"
    else:
        reason = "not_authorized"
    payload: dict[str, Any] = {
        "path": resolved,
        "exists": exists,
        "is_directory": is_directory,
        "authorized": authorized,
        "reason": reason,
    }
    if workspace is not None:
        payload["workspace"] = workspace
    return payload
