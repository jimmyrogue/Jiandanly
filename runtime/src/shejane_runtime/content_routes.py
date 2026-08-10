from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

from .api_schemas import LocalArtifact, PptxOutlineResponse
from .http_route_helpers import _normalized_path, _owned_run
from .store.sqlite import ArtifactConflictError, LocalStore, RunInputSnapshotError

content_router = APIRouter()


@content_router.get("/v1/artifacts/{artifact_id}", response_model=LocalArtifact)
async def get_artifact(request: Request, artifact_id: str) -> dict[str, Any]:
    """Return a single artifact record.

    Shape matches the TS `LocalArtifact` interface
    (`client.ts:38-44`): `{id, title, content, tool_name?, created_at?}`.
    """
    store: LocalStore = request.app.state.store
    record = await store.get_artifact(artifact_id)
    if record is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    await _owned_run(
        store,
        principal_id=request.state.principal_id,
        run_id=record["run_id"],
        not_found_detail="artifact not found",
    )
    return {
        "id": record["id"],
        "title": record["title"],
        "content": record["content"],
        "content_type": record["content_type"],
        "bytes": record["bytes"],
        "sha256": record.get("sha256"),
        "storage_kind": record.get("storage_kind") or "inline_text",
        "tool_name": record.get("tool_name"),
        "created_at": record["created_at"],
    }


@content_router.get("/v1/artifacts/{artifact_id}/content")
async def get_artifact_content(request: Request, artifact_id: str) -> Response:
    store: LocalStore = request.app.state.store
    record = await store.get_artifact(artifact_id)
    if record is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    await _owned_run(
        store,
        principal_id=request.state.principal_id,
        run_id=record["run_id"],
        not_found_detail="artifact not found",
    )
    if record.get("storage_kind") != "blob":
        return Response(
            content=record["content"],
            media_type=record["content_type"],
            headers={"Content-Disposition": f'attachment; filename="{record["id"]}"'},
        )
    try:
        body = await asyncio.to_thread(store.artifact_body_path, record)
    except ArtifactConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FileResponse(
        body,
        filename=record["title"],
        media_type=record["content_type"],
    )


@content_router.get("/v1/workspace-files")
async def get_workspace_file(
    request: Request,
    path: str = Query(..., description="Absolute file path inside an authorized workspace"),
):
    """Stream a file's bytes back to the renderer.

    Gated by `local_workspaces` — the file's parent chain must be inside
    a path the user previously authorized in the client. We do
    NOT serve arbitrary paths; that would let a compromised renderer
    exfiltrate the entire disk.

    Used by the right-side DocPreviewPanel to fetch .docx / .xlsx
    bytes for in-browser rendering (docx-preview, exceljs). No
    response_model — this is a binary stream, not a JSON shape, so
    it stays out of api_schemas.py / openapi.json by design.
    """
    if not path:
        raise HTTPException(status_code=400, detail="path required")
    resolved = Path(await _normalized_path(path))
    try:
        if not await asyncio.to_thread(resolved.is_file):
            raise HTTPException(status_code=404, detail="file not found")
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"invalid path: {exc}") from exc
    store: LocalStore = request.app.state.store
    workspaces = await store.list_workspaces(principal_id=request.state.principal_id)
    # `is_relative_to` walks the parent chain; we need the file to live
    # under *some* authorized workspace root.
    roots = [Path(ws["path"]) for ws in workspaces]
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise HTTPException(
            status_code=403,
            detail="path is not inside any authorized workspace",
        )
    # Let FileResponse pick the right Content-Type from the extension.
    # docx → application/vnd.openxmlformats-officedocument.wordprocessingml.document
    # xlsx → application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
    #
    # Do NOT override `Content-Disposition`. Starlette's FileResponse
    # already emits an RFC-5987-compliant header (`filename*=utf-8''…`)
    # when `filename=` contains non-ASCII characters; setting a custom
    # header with raw CJK in the value triggers an ASGI latin-1
    # encoding error and the renderer sees "Failed to fetch". The
    # fetch() consumer doesn't care about the disposition anyway —
    # it reads response.arrayBuffer() directly.
    return FileResponse(resolved, filename=resolved.name)


@content_router.get(
    "/v1/runs/{run_id}/inputs/{input_id}",
    response_class=FileResponse,
    responses={
        200: {
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            }
        }
    },
)
async def get_run_input(request: Request, run_id: str, input_id: str):
    """Stream one immutable Runtime-owned input to its Run owner."""
    store: LocalStore = request.app.state.store
    await _owned_run(
        store,
        principal_id=request.state.principal_id,
        run_id=run_id,
    )
    record = next(
        (item for item in await store.list_run_inputs(run_id) if item["input_id"] == input_id),
        None,
    )
    if record is None:
        raise HTTPException(status_code=404, detail="run input not found")
    try:
        body = await asyncio.to_thread(store.run_input_body_path, record)
    except RunInputSnapshotError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FileResponse(
        body,
        filename=record["original_name"],
        media_type="application/octet-stream",
    )


@content_router.get("/v1/pptx-outline", response_model=PptxOutlineResponse)
async def get_pptx_outline(
    request: Request,
    path: str = Query(..., description="Absolute .pptx path inside an authorized workspace"),
) -> dict[str, Any]:
    """Return the slide outline JSON for a .pptx file.

    Used by the right-side DocPreviewPanel's PptxPreview component
    — pptx has no mature pure-browser renderer, so the panel renders
    a structured outline (title + bullets + notes per slide) here
    rather than embedding a viewer in iframe.

    Gated by `local_workspaces`, same as `/workspace-files`. The
    path's parent chain must be inside a previously-authorized
    workspace. Calls the shared `_outline_pptx` helper that
    `office.outline` and `office.read_slides` also use, so the
    JSON shape is identical to those tools.
    """
    if not path:
        raise HTTPException(status_code=400, detail="path required")
    resolved = Path(await _normalized_path(path))
    try:
        if not await asyncio.to_thread(resolved.is_file):
            raise HTTPException(status_code=404, detail="file not found")
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"invalid path: {exc}") from exc
    if resolved.suffix.lower() != ".pptx":
        raise HTTPException(status_code=400, detail="path must point to a .pptx file")
    store: LocalStore = request.app.state.store
    workspaces = await store.list_workspaces(principal_id=request.state.principal_id)
    roots = [Path(ws["path"]) for ws in workspaces]
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise HTTPException(
            status_code=403,
            detail="path is not inside any authorized workspace",
        )
    # Defer the import so the runtime boot path doesn't pay for
    # python-pptx unless someone actually previews a deck.
    from .tools.office import _outline_pptx

    try:
        return await asyncio.to_thread(_outline_pptx, str(resolved))
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"failed to outline .pptx: {exc.__class__.__name__}: {exc}",
        ) from exc


@content_router.get(
    "/v1/runs/{run_id}/inputs/{input_id}/pptx-outline",
    response_model=PptxOutlineResponse,
)
async def get_run_input_pptx_outline(
    request: Request,
    run_id: str,
    input_id: str,
) -> dict[str, Any]:
    """Return a deck outline from the immutable Runtime-owned input."""
    store: LocalStore = request.app.state.store
    await _owned_run(
        store,
        principal_id=request.state.principal_id,
        run_id=run_id,
    )
    record = next(
        (item for item in await store.list_run_inputs(run_id) if item["input_id"] == input_id),
        None,
    )
    if record is None:
        raise HTTPException(status_code=404, detail="run input not found")
    if Path(str(record["original_name"])).suffix.lower() != ".pptx":
        raise HTTPException(status_code=400, detail="run input must be a .pptx file")
    try:
        body = await asyncio.to_thread(store.run_input_body_path, record)
    except RunInputSnapshotError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    from .tools.office import _outline_pptx

    try:
        return await asyncio.to_thread(_outline_pptx, str(body))
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"failed to outline .pptx: {exc.__class__.__name__}: {exc}",
        ) from exc
