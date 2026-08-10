from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from .api_schemas import (
    DeleteLocalThreadResponse,
    ListThreadChangesResponse,
    ListThreadsResponse,
    LocalThread,
    LocalThreadSnapshot,
    UpdateLocalThreadRequest,
)
from .http_route_helpers import _runs_with_inputs
from .presentation import project_run_presentation
from .store.sqlite import LocalStore, RunResultConflictError

thread_router = APIRouter()


@thread_router.get("/v1/threads", response_model=ListThreadsResponse)
async def list_threads(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    before_created_at: str | None = Query(default=None),
    before_id: str | None = Query(default=None),
):
    store: LocalStore = request.app.state.store
    if (before_created_at is None) != (before_id is None):
        raise HTTPException(status_code=400, detail="both thread page cursors are required")
    threads, cursor, has_more = await store.list_threads(
        principal_id=request.state.principal_id,
        limit=limit,
        before_created_at=before_created_at,
        before_id=before_id,
    )
    return {
        "threads": [_thread_record_for_api(thread) for thread in threads],
        "cursor": cursor,
        "has_more": has_more,
        "next_before_created_at": threads[-1]["created_at"] if has_more and threads else None,
        "next_before_id": threads[-1]["id"] if has_more and threads else None,
    }


@thread_router.get("/v1/threads/changes", response_model=ListThreadChangesResponse)
async def list_thread_changes(
    request: Request,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=1000),
):
    store: LocalStore = request.app.state.store
    changes, cursor = await store.thread_changes_since(
        principal_id=request.state.principal_id,
        after_cursor=after,
        limit=limit,
    )
    return {"changes": changes, "cursor": cursor}


@thread_router.get("/v1/threads/{thread_id}", response_model=LocalThreadSnapshot)
async def get_thread_snapshot(
    request: Request,
    thread_id: str,
    before_position: int | None = Query(default=None, ge=1),
    item_limit: int = Query(default=200, ge=2, le=500),
    event_limit: int = Query(default=5000, ge=1, le=10000),
    expected_version: int | None = Query(default=None, ge=1),
):
    store: LocalStore = request.app.state.store
    try:
        snapshot = await store.get_thread_snapshot(
            principal_id=request.state.principal_id,
            thread_id=thread_id,
            before_position=before_position,
            item_limit=item_limit,
            event_limit=event_limit,
            expected_version=expected_version,
        )
    except RunResultConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if snapshot is None:
        raise HTTPException(status_code=404, detail="thread not found")
    tool_receipts_by_run = snapshot.pop("tool_receipts_by_run", {})
    wait_candidates_by_run = snapshot.pop("wait_candidates_by_run", {})
    artifacts_by_run = snapshot.pop("artifacts_by_run", {})
    raw_presentation_events = snapshot.pop("presentation_events", [])
    presentation_high_watermarks = snapshot.pop("presentation_high_watermarks", {})
    items = []
    for item in snapshot["items"]:
        try:
            metadata = json.loads(item.get("metadata_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            metadata = {}
        items.append({**item, "metadata": metadata if isinstance(metadata, dict) else {}})
    events = []
    for event in snapshot["events"]:
        try:
            payload = json.loads(event.get("payload_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {}
        events.append({**event, "payload": payload if isinstance(payload, dict) else {}})
    presentation_events_by_run: dict[str, list[dict[str, Any]]] = {}
    for event in raw_presentation_events:
        try:
            payload = json.loads(event.get("payload_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {}
        decoded = {**event, "payload": payload if isinstance(payload, dict) else {}}
        presentation_events_by_run.setdefault(str(event["run_id"]), []).append(decoded)
    presentations = {}
    for run in snapshot["runs"]:
        run_id = str(run["id"])
        presentations[run_id] = project_run_presentation(
            run=run,
            assistant_item=next(
                (
                    item
                    for item in items
                    if item.get("run_id") == run_id and item.get("item_type") == "assistant_message"
                ),
                None,
            ),
            events=presentation_events_by_run.get(run_id, []),
            tool_receipts=tool_receipts_by_run.get(run_id, []),
            wait_candidates=wait_candidates_by_run.get(run_id, []),
            artifacts=artifacts_by_run.get(run_id, []),
            event_high_watermark=int(presentation_high_watermarks.get(run_id, 0)),
        )
    return {
        **snapshot,
        "thread": _thread_record_for_api(snapshot["thread"]),
        "items": items,
        "runs": await _runs_with_inputs(store, snapshot["runs"]),
        "events": events,
        "presentations": presentations,
    }


@thread_router.patch("/v1/threads/{thread_id}", response_model=LocalThread)
async def update_thread(
    request: Request,
    thread_id: str,
    body: UpdateLocalThreadRequest,
):
    store: LocalStore = request.app.state.store
    thread = await store.update_thread(
        principal_id=request.state.principal_id,
        thread_id=thread_id,
        title=body.title,
        metadata=body.metadata,
        archived=body.archived,
    )
    if thread is None:
        raise HTTPException(status_code=404, detail="thread not found")
    return _thread_record_for_api(thread)


@thread_router.delete("/v1/threads/{thread_id}", response_model=DeleteLocalThreadResponse)
async def delete_thread(request: Request, thread_id: str):
    store: LocalStore = request.app.state.store
    try:
        version = await store.delete_thread(
            principal_id=request.state.principal_id,
            thread_id=thread_id,
        )
    except RunResultConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if version is None:
        raise HTTPException(status_code=404, detail="thread not found")
    return {"id": thread_id, "deleted": True, "version": version}


def _thread_record_for_api(thread: dict[str, Any]) -> dict[str, Any]:
    try:
        metadata = json.loads(thread.get("metadata_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        metadata = {}
    return {**thread, "metadata": metadata if isinstance(metadata, dict) else {}}
