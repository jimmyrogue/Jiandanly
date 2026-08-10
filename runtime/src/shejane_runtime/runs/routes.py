"""Run lifecycle, projection, streaming, cancel, and injection routes."""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

from ..api_schemas import (
    CancelRunResponse,
    CreateRunRequest,
    ForkRunRequest,
    InjectRunInstructionRequest,
    InjectRunInstructionResponse,
    ListAgentMessagesResponse,
    ListChildRunsResponse,
    ListRunEventsResponse,
    ListRunsResponse,
    LocalCollaborationSnapshot,
    LocalRun,
)
from ..command_routes import command_router
from ..http_route_helpers import (
    _event_payload,
    _normalized_path,
    _owned_run,
    _run_with_inputs,
    _runs_with_inputs,
)
from ..schedule_routes import schedule_router
from ..store.sqlite import (
    CommandConflictError,
    LocalStore,
    ParentRunAdmissionError,
    RunAdmissionError,
    ThreadAdmissionError,
    WorkspaceAdmissionError,
)
from . import RunCoordinator
from .errors import CheckpointNotFoundError, RunNotFoundError

run_router = APIRouter()


@run_router.get("/v1/runs", response_model=ListRunsResponse)
async def list_runs(request: Request) -> dict[str, Any]:
    """Recent runs newest-first.

    Client `listLocalRuns()` (runtime/sdk/src/client.ts:283)
    reads `{runs: LocalRun[]}` on every boot. Previously this route
    didn't exist — every Electron launch silently 404'd here and
    the conversation history sidebar came up empty.
    """
    store: LocalStore = request.app.state.store
    runs = await store.list_runs(principal_id=request.state.principal_id)
    return {"runs": await _runs_with_inputs(store, runs)}


run_router.include_router(command_router)


@run_router.post("/v1/runs", response_model=LocalRun)
async def create_run(request: Request, body: CreateRunRequest) -> dict[str, Any]:
    """Create a new run. Returns the flat `LocalRun` shape (NOT
    `{run: {...}}`) — that's the contract `client.test.ts:63-92`
    pins and what TypeScript's `createLocalRun` reads via
    `decodeLocalResponse<LocalRun>`."""
    goal = body.goal.strip()
    if not goal:
        raise HTTPException(status_code=400, detail="goal required")
    principal_id = request.state.principal_id
    workspace_path = (
        await _normalized_path(body.workspace_path) if body.workspace_path is not None else None
    )
    attachment_paths = [await _normalized_path(path) for path in body.attachment_paths]
    coordinator: RunCoordinator = request.app.state.coordinator
    try:
        run = await coordinator.start_run(
            principal_id=principal_id,
            command_id=body.command_id,
            client_message_id=body.client_message_id,
            protocol_version=body.protocol_version,
            required_capabilities=body.required_capabilities,
            required_tools=body.required_tools,
            goal=goal,
            thread_id=body.thread_id,
            user_input=body.user_input,
            assistant_message_id=body.assistant_message_id,
            thread_title=body.thread_title,
            thread_metadata=body.thread_metadata,
            user_item_metadata=body.user_item_metadata,
            replace_from_client_id=body.replace_from_client_id,
            workspace_path=workspace_path,
            attachment_paths=attachment_paths,
            # The runtime's legacy `mode` column carries the Runtime model selection.
            mode=body.model,
            permission_mode=body.permission_mode,
            history=body.history or [],
            parent_run_id=body.parent_run_id,
            plugin_refs=[reference.model_dump(mode="json") for reference in body.plugin_refs],
            plugin_command=(
                body.plugin_command.model_dump(mode="json")
                if body.plugin_command is not None
                else None
            ),
            settings=body.settings,
            metadata=body.metadata,
        )
        return await _run_with_inputs(request.app.state.store, run)
    except CommandConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WorkspaceAdmissionError as exc:
        status_code = 409 if "no longer available" in str(exc) else 403
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    except ParentRunAdmissionError as exc:
        status_code = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    except ThreadAdmissionError as exc:
        status_code = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    except RunAdmissionError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


run_router.include_router(schedule_router)


@run_router.post("/v1/runs/{run_id}/fork", response_model=LocalRun)
async def fork_run(request: Request, run_id: str, body: ForkRunRequest) -> dict[str, Any]:
    checkpoint_id = body.checkpoint_id.strip()
    if not checkpoint_id:
        raise HTTPException(status_code=400, detail="checkpoint_id required")
    await _owned_run(
        request.app.state.store,
        principal_id=request.state.principal_id,
        run_id=run_id,
    )
    coordinator: RunCoordinator = request.app.state.coordinator
    try:
        run = await coordinator.fork_run(
            principal_id=request.state.principal_id,
            source_run_id=run_id,
            command_id=body.command_id,
            client_message_id=body.client_message_id,
            assistant_message_id=body.assistant_message_id,
            thread_id=body.thread_id,
            protocol_version=body.protocol_version,
            required_capabilities=body.required_capabilities,
            checkpoint_id=checkpoint_id,
            goal=body.goal,
            user_input=body.user_input,
            thread_title=body.thread_title,
            thread_metadata=body.thread_metadata,
            user_item_metadata=body.user_item_metadata,
            metadata=body.metadata,
        )
        return await _run_with_inputs(request.app.state.store, run)
    except RunNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "run_not_found", "message": "run not found"},
        ) from exc
    except CheckpointNotFoundError as exc:
        raise HTTPException(status_code=404, detail="checkpoint not found") from exc
    except WorkspaceAdmissionError as exc:
        status_code = 409 if "no longer available" in str(exc) else 403
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    except ThreadAdmissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CommandConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RunAdmissionError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@run_router.get("/v1/runs/{run_id}", response_model=LocalRun)
async def get_run(request: Request, run_id: str) -> dict[str, Any]:
    """Return the flat run record (same shape as POST /runs)."""
    store: LocalStore = request.app.state.store
    run = await _owned_run(
        store,
        principal_id=request.state.principal_id,
        run_id=run_id,
    )
    return await _run_with_inputs(store, run)


@run_router.get("/v1/runs/{run_id}/children", response_model=ListChildRunsResponse)
async def list_child_runs(request: Request, run_id: str) -> dict[str, Any]:
    store: LocalStore = request.app.state.store
    await _owned_run(
        store,
        principal_id=request.state.principal_id,
        run_id=run_id,
    )
    return {"children": await store.list_child_runs_for_run(run_id)}


@run_router.get(
    "/v1/runs/{run_id}/collaboration",
    response_model=LocalCollaborationSnapshot,
)
async def get_collaboration_snapshot(request: Request, run_id: str) -> dict[str, Any]:
    await _owned_run(
        request.app.state.store,
        principal_id=request.state.principal_id,
        run_id=run_id,
    )
    try:
        return await request.app.state.coordinator.collaboration_snapshot(run_id)
    except RunAdmissionError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


@run_router.get("/v1/runs/{run_id}/mailbox", response_model=ListAgentMessagesResponse)
async def list_agent_messages(
    request: Request,
    run_id: str,
    box: Literal["inbox", "outbox"] = Query(default="inbox"),
) -> dict[str, Any]:
    store: LocalStore = request.app.state.store
    await _owned_run(
        store,
        principal_id=request.state.principal_id,
        run_id=run_id,
    )
    messages = (
        await store.list_agent_inbox(run_id)
        if box == "inbox"
        else await store.list_agent_outbox(run_id)
    )
    return {"messages": messages}


@run_router.get("/v1/runs/{run_id}/events", response_model=ListRunEventsResponse)
async def list_run_events(
    request: Request,
    run_id: str,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=1000, ge=1, le=5000),
) -> dict[str, Any]:
    store: LocalStore = request.app.state.store
    await _owned_run(
        store,
        principal_id=request.state.principal_id,
        run_id=run_id,
    )
    raw_events = await store.events_since(run_id, after_seq=after, limit=limit + 1)
    has_more = len(raw_events) > limit
    page = raw_events[:limit]
    return {
        "events": [{**event, "payload": _event_payload(event)} for event in page],
        "has_more": has_more,
        "next_after": int(page[-1]["seq"]) if page else after,
    }


@run_router.get("/v1/runs/{run_id}/stream")
async def stream_run(
    request: Request,
    run_id: str,
    after: int = Query(default=0, ge=0),
) -> EventSourceResponse:
    store: LocalStore = request.app.state.store
    await _owned_run(
        store,
        principal_id=request.state.principal_id,
        run_id=run_id,
    )
    first_seq, latest_seq = await store.event_sequence_window(run_id)
    if after > latest_seq or (first_seq is not None and after < first_seq - 1):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "event_cursor_reset_required",
                "message": "event cursor is outside the retained event window",
                "requested_after": after,
                "first_available_seq": first_seq,
                "latest_seq": latest_seq,
            },
        )
    coordinator: RunCoordinator = request.app.state.coordinator

    async def gen():
        # The client's `parseAgentSSEChunk` (sse.ts) reads
        #   data: {"event_type": "...", "payload": {...}, "id":...}
        # and recognizes only `data: [DONE]` as the completion mark.
        # So we must:
        #   • dump the whole envelope into `data:` (not the bare
        #     payload like the old shape — that made event_type
        #     undefined on the client and the entire UI silently
        #     no-op'd);
        #   • end with `data: [DONE]` so the stream resolves.
        try:
            async for event in coordinator.stream(run_id, after_seq=after):
                yield {
                    "id": str(event.get("seq") or event["id"]),
                    "event": event["event_type"],
                    "data": json.dumps(event, default=str, ensure_ascii=False),
                }
        finally:
            yield {"data": "[DONE]"}

    # `sep="\n"` (LF) matches the Runtime SDK parser, which splits on
    # `/\n\n/`. sse-starlette's default `\r\n` is spec-correct but does
    # not match that protocol contract.
    return EventSourceResponse(gen(), sep="\n")


@run_router.post("/v1/runs/{run_id}/cancel", response_model=CancelRunResponse)
async def cancel_run(request: Request, run_id: str) -> dict[str, Any]:
    await _owned_run(
        request.app.state.store,
        principal_id=request.state.principal_id,
        run_id=run_id,
    )
    coordinator: RunCoordinator = request.app.state.coordinator
    ok = await coordinator.cancel_run(run_id)
    return {"canceled": ok}


@run_router.post(
    "/v1/runs/{run_id}/inject",
    response_model=InjectRunInstructionResponse,
)
async def inject_run_instruction(
    request: Request,
    run_id: str,
    body: InjectRunInstructionRequest,
) -> dict[str, Any]:
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="content required")
    store: LocalStore = request.app.state.store
    try:
        receipt, _created = await store.request_run_inject_command(
            principal_id=request.state.principal_id,
            command_id=body.command_id,
            run_id=run_id,
            content=content,
        )
        return receipt
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    except (CommandConflictError, RunAdmissionError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# ---- compatibility shims the client expects (pre-existing Node API) ----
#
# Some of these are full features that didn't make it into Phase 3'/4'
# yet. They return safe defaults / 501 so the client can boot without
# crashing on missing routes. Implementations land in Phase 6'+.
