"""FastAPI application + HTTP route surface.

Phase 2' deliverables:
- `/v1/health` (no auth)
- `/v1/tools` (list available tools — placeholder for now)
- `/v1/workspaces` (CRUD authorization records)
- `/v1/runs` (placeholder: real impl lands in Phase 3')
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from langchain_core.messages import ToolMessage
from sse_starlette.sse import EventSourceResponse

from . import __version__
from .agent.builder import (
    open_checkpointer,
    open_store,
)
from .api_schemas import (
    AnswerQuestionCommand,
    AnswerQuestionCommandReceipt,
    AnswerQuestionRequest,
    CancelRunCommand,
    CancelRunCommandReceipt,
    CancelRunResponse,
    CentralDiagnosticsStatusResponse,
    ClearMemoryResponse,
    CreateRunRequest,
    CreateScheduledRunRequest,
    CreateWorkspaceRequest,
    DeleteLocalThreadResponse,
    DiagnoseWorkspaceRequest,
    FixedRuntimeAssetStatus,
    ForkRunRequest,
    HealthResponse,
    InjectRunInstructionRequest,
    InjectRunInstructionResponse,
    ListAgentMessagesResponse,
    ListChildRunsResponse,
    ListPluginsResponse,
    ListRunEventsResponse,
    ListRunsResponse,
    ListScheduledRunsResponse,
    ListThreadChangesResponse,
    ListThreadsResponse,
    ListWorkspacesResponse,
    LocalCollaborationSnapshot,
    LocalRun,
    LocalScheduledRun,
    LocalThread,
    LocalThreadSnapshot,
    LocalWorkspaceAuthorization,
    LocalWorkspaceDiagnosis,
    PermissionResolution,
    PlanApprovalResolution,
    PlanResolveCommand,
    PlanResolveCommandReceipt,
    PluginDetail,
    PluginDisableCommand,
    PluginEnableCommand,
    PluginInstallCommand,
    PluginInstallCommandReceipt,
    PluginModelBindCommand,
    PluginModelBindCommandReceipt,
    PluginReadinessSnapshot,
    PluginRemoveCommand,
    PluginRemoveCommandReceipt,
    PluginRollbackCommand,
    PluginSetupAdvanceCommand,
    PluginSetupAdvanceCommandReceipt,
    PluginStateCommandReceipt,
    PluginUpdateCommand,
    PluginVersionSwitchCommandReceipt,
    QuestionAnswer,
    ReconcileToolRequest,
    ResolvePermissionCommand,
    ResolvePermissionCommandReceipt,
    ResolvePermissionRequest,
    ResolvePlanApprovalRequest,
    RuntimeAssetCleanupResult,
    RuntimeAssetInstallCommand,
    RuntimeAssetInstallCommandReceipt,
    RuntimeAssetStorage,
    RuntimeInfo,
    RuntimeSettingsResponse,
    ToolReconcileCommand,
    ToolReconcileCommandReceipt,
    ToolReconciliationResolution,
    UpdateCentralDiagnosticsRequest,
    UpdateLocalThreadRequest,
    UpdateRuntimeSettingsRequest,
)
from .auth import LOCAL_OWNER_PRINCIPAL_ID, PairingTokenAuthMiddleware
from .catalog_routes import catalog_router
from .central_diagnostics import (
    CentralDiagnosticsConfigurationError,
    CentralDiagnosticsManager,
    CentralDiagnosticsUnavailable,
)
from .config import Settings, get_settings
from .content_routes import content_router
from .diagnostics_routes import _first_string, diagnostics_router
from .http_body_limit import RequestBodyLimitMiddleware
from .http_route_helpers import (
    _authorized_workspace_path,
    _normalized_path,
    _owned_run,
    _run_with_inputs,
    _runs_with_inputs,
)
from .middleware.tool_execution import serialize_tool_result
from .model_credentials import (
    CredentialStoreError,
    get_model_api_key,
)
from .model_service_authorization import _complete_shejane_authorization
from .model_service_routes import model_service_router
from .permission_policy import PermissionScopeNotAllowedError
from .plugins.browser_qa import BROWSER_QA_PLUGIN_ID
from .plugins.catalog import PluginCatalog
from .plugins.platforms import current_managed_worker_platform
from .plugins.registry import PluginRegistry, PluginRegistryError
from .presentation import project_run_presentation
from .runs import (
    RUNTIME_PROTOCOL_VERSION,
    CheckpointNotFoundError,
    RunCoordinator,
    RunNotFoundError,
    freeze_run_settings,
    runtime_capabilities,
    sanitize_run_metadata,
)
from .scheduler import ScheduledRunDispatcher
from .shejane_authorization import (
    OFFICIAL_CLOUD_ORIGIN,
    SheJaneAuthorizationManager,
)
from .store.sqlite import (
    CommandConflictError,
    LocalStore,
    ParentRunAdmissionError,
    PermissionDecisionConflictError,
    RunAdmissionError,
    RunResultConflictError,
    ThreadAdmissionError,
    WaitDecisionConflictError,
    WorkspaceAdmissionError,
)

log = logging.getLogger("shejane_runtime.server")


_RUNTIME_SETTINGS_TO_FIELDS = {
    "max_model_calls": "max_model_calls",
    "max_tool_retries": "max_tool_retries",
    "research_search_limit": "research_search_limit",
    "unknown_model_max_input_tokens": "unknown_model_max_input_tokens",
    "unknown_model_max_output_tokens": "unknown_model_max_output_tokens",
    "model_request_timeout_seconds": "model_request_timeout_seconds",
    "browser_headless": "browser_headless",
    "subagents": "enable_subagents",
    "input_guard": "input_guard_mode",
    "plan_first": "plan_first_mode",
    "verification_repair_max": "verification_repair_max",
    "repair_workflow_max": "repair_workflow_max",
    "pii_redact": "pii_redact_types",
}


def _fixed_runtime_asset_sources(settings: Settings) -> dict[str, Path | str]:
    sources: dict[str, Path | str] = {}
    if settings.browser_qa_runtime_asset is not None:
        sources[BROWSER_QA_PLUGIN_ID + ".runtime"] = settings.browser_qa_runtime_asset
    if settings.ocr_runtime_asset is not None:
        sources["org.rapidocr.runtime"] = settings.ocr_runtime_asset
    if settings.fixed_runtime_asset_base_url is None:
        return sources
    platform = current_managed_worker_platform()
    target = {
        "darwin/arm64": "darwin-arm64",
        "windows/amd64": "windows-amd64",
    }.get(platform)
    if target is None:
        return sources
    filenames = {
        BROWSER_QA_PLUGIN_ID + ".runtime": (
            f"browser-qa-runtime-1.61.1-{target}.shejane-runtime-asset"
        ),
        "org.rapidocr.runtime": f"rapidocr-runtime-3.9.1-{target}.shejane-runtime-asset",
    }
    for asset_id, filename in filenames.items():
        sources.setdefault(
            asset_id,
            urljoin(settings.fixed_runtime_asset_base_url + "/", filename),
        )
    return sources


def _runtime_settings_payload(settings: Settings, *, version: int) -> dict[str, Any]:
    return {
        "version": version,
        **{
            public_name: getattr(settings, field_name)
            for public_name, field_name in _RUNTIME_SETTINGS_TO_FIELDS.items()
        },
    }


def _apply_runtime_settings(settings: Settings, values: dict[str, Any]) -> Settings:
    updates = {
        field_name: values[public_name]
        for public_name, field_name in _RUNTIME_SETTINGS_TO_FIELDS.items()
        if public_name in values
    }
    return settings.model_copy(update=updates)


_TERMINAL_RUN_STATUSES = {"completed", "failed", "canceled", "cleanup_required"}


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.bootstrap_settings
    settings.ensure_data_dir()
    # Make sure the canonical user-managed skills dir exists from boot —
    # otherwise it's invisible to the UI until the user manually creates
    # it, and the "Personal" section silently disappears from the list.
    (Path.home() / ".shejane" / "skills").mkdir(parents=True, exist_ok=True)
    store = await LocalStore.open(settings.runtime_db_path)
    persisted_settings = await store.get_runtime_settings()
    if persisted_settings is not None:
        old_defaults = persisted_settings["settings"]
        migration = {}
        if old_defaults.get("max_model_calls") == 20:
            migration["max_model_calls"] = 100
        if old_defaults.get("research_search_limit") == 3:
            migration["research_search_limit"] = 10
        if migration:
            persisted_settings = await store.patch_runtime_settings(
                migration,
                initial_settings=old_defaults,
            )
        settings = _apply_runtime_settings(settings, persisted_settings["settings"])
    checkpointer, ck_stack = await open_checkpointer(settings)
    agent_store, store_stack = await open_store(settings)
    central_diagnostics = CentralDiagnosticsManager(
        store=store,
        cloud_origin=OFFICIAL_CLOUD_ORIGIN,
        app_version=__version__,
    )
    plugin_catalog = PluginCatalog(
        settings.data_dir,
        runtime_asset_sources=_fixed_runtime_asset_sources(settings),
    )
    coordinator = RunCoordinator(
        store=store,
        checkpointer=checkpointer,
        agent_store=agent_store,
        settings=settings,
        plugin_catalog=plugin_catalog,
        terminal_callback=lambda run_id, status, payload: central_diagnostics.submit_terminal(
            run_id=run_id,
            status=status,
            payload=payload,
        ),
    )
    await coordinator.mcp_catalog.hydrate()
    coordinator.mcp_catalog.request_refresh()
    scheduler = ScheduledRunDispatcher(store=store, coordinator=coordinator)
    plugin_registry = PluginRegistry(
        store=store,
        data_dir=settings.data_dir,
        runtime_version=__version__,
        plugin_catalog=plugin_catalog,
        computer_use_package=settings.computer_use_package,
        browser_qa_package=settings.browser_qa_package,
        ocr_package=settings.ocr_package,
    )
    await plugin_registry.initialize_fixed_capabilities(LOCAL_OWNER_PRINCIPAL_ID)
    app.state.store = store
    app.state.plugin_registry = plugin_registry
    app.state.settings = settings
    app.state.checkpointer = checkpointer
    app.state.agent_store = agent_store
    app.state.coordinator = coordinator
    app.state.mcp_catalog = coordinator.mcp_catalog
    app.state.scheduler = scheduler
    app.state.shejane_authorization = SheJaneAuthorizationManager(
        cloud_origin=OFFICIAL_CLOUD_ORIGIN,
        app_version=__version__,
        complete=partial(_complete_shejane_authorization, app),
    )
    app.state.shejane_authorization_lock = asyncio.Lock()
    app.state.central_diagnostics = central_diagnostics
    app.state.runtime_settings_lock = asyncio.Lock()
    app.state.runtime_settings_version = int(
        persisted_settings["version"] if persisted_settings is not None else 0
    )
    # Reconcile runs the previous process left non-terminal (the runtime is
    # SIGKILLed on every `make dev` restart): fail dead queued/running
    # runs, leave waiting_permission runs resumable. Without this they sit
    # `running` forever and the client never sees a terminal state.
    await coordinator.recover_orphans()
    coordinator.start()
    await scheduler.recover_running()
    scheduler.start()
    log.info(
        "runtime started host=%s port=%s data=%s",
        settings.host,
        settings.port,
        settings.data_dir,
    )
    try:
        yield
    finally:
        await app.state.shejane_authorization.close()
        await scheduler.stop()
        await coordinator.stop()
        await coordinator.mcp_catalog.close()
        await store_stack.aclose()
        await ck_stack.aclose()
        await store.close()
        log.info("runtime shutdown clean")


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = get_settings()

    app = FastAPI(
        title="SheJane Runtime",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.bootstrap_settings = settings

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        # FastAPI normally includes the rejected input in its 422 payload.
        # Local requests can contain model-service credentials, so return only
        # the location, message, and error type across the entire API.
        errors = [
            {key: value for key, value in error.items() if key not in {"input", "ctx"}}
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": errors})

    app.add_middleware(RequestBodyLimitMiddleware)

    # Order matters: middleware added LAST runs FIRST on the request path
    # (Starlette wraps outward). PairingTokenAuthMiddleware must sit
    # behind CORSMiddleware so that:
    #   1. CORS preflight (OPTIONS) is answered by CORSMiddleware without
    #      ever hitting auth — preflight by spec carries no credentials,
    #      so rejecting it with 401 makes every browser fetch fail.
    #   2. Even authenticated-but-401 responses still ship the
    #      Access-Control-Allow-Origin header, otherwise the browser
    #      hides the error body from the JS layer.
    app.add_middleware(PairingTokenAuthMiddleware, token=settings.pairing_token)

    # CORS — the runtime binds loopback only, but the Vite dev server (and
    # the production Electron renderer when loaded over file://) live on a
    # different origin than `:17371`. Without these headers, every
    # browser-side fetch fails preflight. Bearer-token auth
    # (PairingTokenAuthMiddleware above) is the real gate; CORS is just
    # plumbing.
    #
    # Override via env if you front the runtime with a custom reverse proxy.
    cors_origins_env = os.environ.get("SHEJANE_RUNTIME_CORS_ORIGINS", "").strip()
    if cors_origins_env:
        allow_origins = [o.strip() for o in cors_origins_env.split(",") if o.strip()]
        allow_origin_regex = None
    else:
        # Permit any localhost/loopback origin (dev Vite at 55173, prod
        # Electron file:// shows up as `null`, plus any 5173/5174/etc.).
        allow_origins = ["null"]
        allow_origin_regex = r"^(?:https?://)?(?:127\.0\.0\.1|localhost)(?::\d+)?$"
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_origin_regex=allow_origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/v1/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        # Two consumers, two contracts — both must be satisfied:
        #   • the live Client contract test checks `ok == true`
        #   • runtime/sdk/src/client.ts:probeRuntime
        #     checks `status === "ok"` and reads mode/worker
        # The HealthResponse defaults already encode `ok=True status="ok"`
        # mode="ready" worker="python-langgraph" — only `version` and
        # `pairing_configured` need to be filled per-request.
        return HealthResponse(
            version=__version__,
            pairing_configured=bool(settings.pairing_token),
        )

    @app.get("/v1/runtime", response_model=RuntimeInfo)
    async def runtime_info(request: Request) -> RuntimeInfo:
        runtime_settings: Settings = app.state.settings
        service_configured = False
        store: LocalStore = app.state.store
        try:
            connections = await store.list_model_connections(
                principal_id=request.state.principal_id
            )
            for connection in connections:
                if await get_model_api_key(
                    request.state.principal_id,
                    str(connection["id"]),
                    str(connection["credential_ref"]),
                ):
                    service_configured = True
                    break
        except CredentialStoreError:
            service_configured = False
        return RuntimeInfo(
            protocol_version=RUNTIME_PROTOCOL_VERSION,
            runtime_version=__version__,
            capabilities=sorted(runtime_capabilities(runtime_settings)),
            model_service_configured=service_configured,
        )

    @app.get("/v1/settings", response_model=RuntimeSettingsResponse)
    async def get_runtime_settings() -> dict[str, Any]:
        return _runtime_settings_payload(
            app.state.settings,
            version=app.state.runtime_settings_version,
        )

    @app.put("/v1/settings", response_model=RuntimeSettingsResponse)
    async def update_runtime_settings(body: UpdateRuntimeSettingsRequest) -> dict[str, Any]:
        patch = body.model_dump(exclude_none=True)
        async with app.state.runtime_settings_lock:
            current = _runtime_settings_payload(
                app.state.settings,
                version=app.state.runtime_settings_version,
            )
            current.update(patch)
            validated = RuntimeSettingsResponse(**current)
            candidate_values = validated.model_dump(exclude={"version"})
            store: LocalStore = app.state.store
            stored = await store.patch_runtime_settings(
                patch,
                initial_settings=candidate_values,
            )
            persisted = RuntimeSettingsResponse(
                **{**candidate_values, **stored["settings"]},
                version=int(stored["version"]),
            )
            values = persisted.model_dump(exclude={"version"})
            updated = _apply_runtime_settings(app.state.settings, values)
            app.state.settings = updated
            app.state.coordinator.settings = updated
            app.state.runtime_settings_version = int(stored["version"])
            return _runtime_settings_payload(updated, version=int(stored["version"]))

    @app.get(
        "/v1/shejane/diagnostics",
        response_model=CentralDiagnosticsStatusResponse,
    )
    async def get_central_diagnostics(request: Request) -> dict[str, Any]:
        try:
            return await app.state.central_diagnostics.status(request.state.principal_id)
        except CredentialStoreError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.put(
        "/v1/shejane/diagnostics",
        response_model=CentralDiagnosticsStatusResponse,
    )
    async def update_central_diagnostics(
        request: Request,
        body: UpdateCentralDiagnosticsRequest,
    ) -> dict[str, Any]:
        try:
            return await app.state.central_diagnostics.configure(
                principal_id=request.state.principal_id,
                enabled=body.enabled,
                connection_id=body.connection_id,
                success_sample_rate=body.success_sample_rate,
            )
        except CentralDiagnosticsConfigurationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (CentralDiagnosticsUnavailable, CredentialStoreError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/v1/tools")
    async def list_tools() -> dict[str, Any]:
        from .tools.registry import describe_tools

        # Phase 2': describe with the current store. workspace_root is
        # None at this layer because fs tools are bound per-run by the
        # agent builder (Phase 3'). Callers wanting the per-run view will
        # use a different endpoint then.
        store = getattr(app.state, "store", None)
        return {"tools": describe_tools(store=store, workspace_root=None)}

    @app.get("/v1/workspaces", response_model=ListWorkspacesResponse)
    async def list_workspaces(request: Request) -> dict[str, Any]:
        store: LocalStore = app.state.store
        return {"workspaces": await store.list_workspaces(principal_id=request.state.principal_id)}

    @app.post("/v1/workspaces", response_model=LocalWorkspaceAuthorization)
    async def add_workspace(request: Request, body: CreateWorkspaceRequest) -> dict[str, Any]:
        """Authorize a workspace path. Returns the flat row — the TS
        `authorizeLocalWorkspace` reads `.id / .path / .label` directly
        (no wrapper)."""
        store: LocalStore = app.state.store
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

    @app.delete(
        "/v1/workspaces/{workspace_id}",
        response_model=LocalWorkspaceAuthorization,
    )
    async def remove_workspace(request: Request, workspace_id: str) -> dict[str, Any]:
        """Revoke a workspace authorization. Returns the deleted row
        matching the TS `revokeLocalWorkspace` →
        `Promise<LocalWorkspaceAuthorization>` signature."""
        store: LocalStore = app.state.store
        # Fetch before delete so we can return the record. If it didn't
        # exist, surface a 404 — client `decodeLocalResponse` throws
        # which the renderer catches and shows to the user.
        existing = None
        principal_id = request.state.principal_id
        for ws in await store.list_workspaces(principal_id=principal_id):
            if ws["id"] == workspace_id:
                existing = ws
                break
        if existing is None:
            raise HTTPException(status_code=404, detail="workspace not found")
        await store.delete_workspace(principal_id=principal_id, workspace_id=workspace_id)
        return existing

    @app.get("/v1/threads", response_model=ListThreadsResponse)
    async def list_threads(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        before_created_at: str | None = Query(default=None),
        before_id: str | None = Query(default=None),
    ):
        store: LocalStore = app.state.store
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

    @app.get("/v1/threads/changes", response_model=ListThreadChangesResponse)
    async def list_thread_changes(
        request: Request,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=1000),
    ):
        store: LocalStore = app.state.store
        changes, cursor = await store.thread_changes_since(
            principal_id=request.state.principal_id,
            after_cursor=after,
            limit=limit,
        )
        return {"changes": changes, "cursor": cursor}

    @app.get("/v1/threads/{thread_id}", response_model=LocalThreadSnapshot)
    async def get_thread_snapshot(
        request: Request,
        thread_id: str,
        before_position: int | None = Query(default=None, ge=1),
        item_limit: int = Query(default=200, ge=2, le=500),
        event_limit: int = Query(default=5000, ge=1, le=10000),
        expected_version: int | None = Query(default=None, ge=1),
    ):
        store: LocalStore = app.state.store
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
                        if item.get("run_id") == run_id
                        and item.get("item_type") == "assistant_message"
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

    @app.patch("/v1/threads/{thread_id}", response_model=LocalThread)
    async def update_thread(
        request: Request,
        thread_id: str,
        body: UpdateLocalThreadRequest,
    ):
        store: LocalStore = app.state.store
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

    @app.delete("/v1/threads/{thread_id}", response_model=DeleteLocalThreadResponse)
    async def delete_thread(request: Request, thread_id: str):
        store: LocalStore = app.state.store
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

    @app.get("/v1/runs", response_model=ListRunsResponse)
    async def list_runs(request: Request) -> dict[str, Any]:
        """Recent runs newest-first.

        Client `listLocalRuns()` (runtime/sdk/src/client.ts:283)
        reads `{runs: LocalRun[]}` on every boot. Previously this route
        didn't exist — every Electron launch silently 404'd here and
        the conversation history sidebar came up empty.
        """
        store: LocalStore = app.state.store
        runs = await store.list_runs(principal_id=request.state.principal_id)
        return {"runs": await _runs_with_inputs(store, runs)}

    @app.get("/v1/plugins", response_model=ListPluginsResponse)
    async def list_plugins(request: Request) -> dict[str, Any]:
        registry: PluginRegistry = app.state.plugin_registry
        return {"plugins": await registry.list(principal_id=request.state.principal_id)}

    @app.get(
        "/v1/plugins/runtime-assets/storage",
        response_model=RuntimeAssetStorage,
    )
    async def inspect_runtime_asset_storage(request: Request) -> dict[str, Any]:
        registry: PluginRegistry = app.state.plugin_registry
        return await registry.runtime_asset_storage()

    @app.delete(
        "/v1/plugins/runtime-assets/storage",
        response_model=RuntimeAssetCleanupResult,
    )
    async def cleanup_runtime_asset_storage(
        request: Request,
        scope: Literal["history", "all"] = Query(...),
    ) -> dict[str, Any]:
        registry: PluginRegistry = app.state.plugin_registry
        try:
            return await registry.cleanup_runtime_asset_storage(scope)
        except PluginRegistryError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    @app.get("/v1/plugins/{plugin_id}", response_model=PluginDetail)
    async def inspect_plugin(request: Request, plugin_id: str) -> dict[str, Any]:
        registry: PluginRegistry = app.state.plugin_registry
        try:
            return await registry.inspect(
                principal_id=request.state.principal_id,
                plugin_id=plugin_id,
            )
        except PluginRegistryError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    @app.get(
        "/v1/plugins/{plugin_id}/runtime-asset",
        response_model=FixedRuntimeAssetStatus,
        response_model_exclude_defaults=True,
    )
    async def inspect_fixed_runtime_asset(request: Request, plugin_id: str) -> dict[str, Any]:
        registry: PluginRegistry = app.state.plugin_registry
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
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    @app.put(
        "/v1/plugins/{plugin_id}/runtime-asset",
        response_model=FixedRuntimeAssetStatus,
        response_model_exclude_defaults=True,
    )
    async def prepare_fixed_runtime_asset(request: Request, plugin_id: str) -> dict[str, Any]:
        registry: PluginRegistry = app.state.plugin_registry
        try:
            return await registry.prepare_fixed_runtime_asset(
                principal_id=request.state.principal_id,
                plugin_id=plugin_id,
            )
        except PluginRegistryError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    @app.delete(
        "/v1/plugins/{plugin_id}/runtime-asset",
        response_model=FixedRuntimeAssetStatus,
        response_model_exclude_defaults=True,
    )
    async def remove_fixed_runtime_asset(request: Request, plugin_id: str) -> dict[str, Any]:
        registry: PluginRegistry = app.state.plugin_registry
        try:
            return await registry.remove_fixed_runtime_asset(
                principal_id=request.state.principal_id,
                plugin_id=plugin_id,
            )
        except PluginRegistryError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    @app.get(
        "/v1/plugins/{plugin_id}/readiness",
        response_model=PluginReadinessSnapshot,
    )
    async def inspect_plugin_readiness(request: Request, plugin_id: str) -> dict[str, Any]:
        if plugin_id != "org.shejane.computer-use":
            raise HTTPException(status_code=404, detail="plugin readiness is unavailable")
        registry: PluginRegistry = app.state.plugin_registry
        try:
            return await registry.computer_use_readiness(principal_id=request.state.principal_id)
        except PluginRegistryError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    @app.post(
        "/v1/commands",
        response_model=(
            CancelRunCommandReceipt
            | AnswerQuestionCommandReceipt
            | ResolvePermissionCommandReceipt
            | PlanResolveCommandReceipt
            | ToolReconcileCommandReceipt
            | PluginInstallCommandReceipt
            | PluginModelBindCommandReceipt
            | RuntimeAssetInstallCommandReceipt
            | PluginStateCommandReceipt
            | PluginVersionSwitchCommandReceipt
            | PluginRemoveCommandReceipt
            | PluginSetupAdvanceCommandReceipt
        ),
    )
    async def accept_command(
        request: Request,
        body: (
            CancelRunCommand
            | AnswerQuestionCommand
            | ResolvePermissionCommand
            | PlanResolveCommand
            | ToolReconcileCommand
            | PluginInstallCommand
            | PluginModelBindCommand
            | RuntimeAssetInstallCommand
            | PluginEnableCommand
            | PluginDisableCommand
            | PluginUpdateCommand
            | PluginRollbackCommand
            | PluginRemoveCommand
            | PluginSetupAdvanceCommand
        ),
    ) -> dict[str, Any]:
        store: LocalStore = app.state.store
        coordinator: RunCoordinator = app.state.coordinator
        if isinstance(body, PluginSetupAdvanceCommand):
            registry: PluginRegistry = app.state.plugin_registry
            try:
                return await registry.advance_computer_use_setup(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    expected_revision=body.expected_revision,
                    action_id=body.action_id,
                )
            except CommandConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except PluginRegistryError as exc:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={"code": exc.code, "message": str(exc)},
                ) from exc
        if isinstance(body, PluginModelBindCommand):
            registry: PluginRegistry = app.state.plugin_registry
            async with coordinator._model_admission(
                request.state.principal_id,
                body.model,
                ("image_inputs",),
            ) as (binding, error):
                if error is not None:
                    raise HTTPException(
                        status_code=409,
                        detail={"code": error.code, "message": str(error)},
                    )
                try:
                    return await registry.bind_model(
                        principal_id=request.state.principal_id,
                        command_id=body.command_id,
                        plugin_id=body.plugin_id,
                        binding_id=body.binding_id,
                        requested_model=body.model,
                        model_binding=binding,
                        expected_digest=body.expected_digest,
                    )
                except CommandConflictError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                except PluginRegistryError as exc:
                    raise HTTPException(
                        status_code=exc.status_code,
                        detail={"code": exc.code, "message": str(exc)},
                    ) from exc
        if isinstance(body, RuntimeAssetInstallCommand):
            registry: PluginRegistry = app.state.plugin_registry
            try:
                return await registry.install_runtime_asset(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    source_path=body.source_path,
                    expected_digest=body.expected_digest,
                )
            except CommandConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except PluginRegistryError as exc:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={"code": exc.code, "message": str(exc)},
                ) from exc
        if isinstance(body, PluginInstallCommand):
            registry: PluginRegistry = app.state.plugin_registry
            try:
                return await registry.install(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    source_path=body.source_path,
                    expected_digest=body.expected_digest,
                    allow_unsigned=body.allow_unsigned,
                )
            except CommandConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except PluginRegistryError as exc:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={"code": exc.code, "message": str(exc)},
                ) from exc
        if isinstance(body, PluginUpdateCommand):
            registry = app.state.plugin_registry
            try:
                return await registry.update(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    plugin_id=body.plugin_id,
                    source_path=body.source_path,
                    expected_digest=body.expected_digest,
                    allow_unsigned=body.allow_unsigned,
                )
            except CommandConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except PluginRegistryError as exc:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={"code": exc.code, "message": str(exc)},
                ) from exc
        if isinstance(body, PluginRollbackCommand):
            registry = app.state.plugin_registry
            try:
                return await registry.rollback(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    plugin_id=body.plugin_id,
                    target_digest=body.target_digest,
                    expected_digest=body.expected_digest,
                )
            except CommandConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except PluginRegistryError as exc:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={"code": exc.code, "message": str(exc)},
                ) from exc
        if isinstance(body, PluginRemoveCommand):
            registry = app.state.plugin_registry
            try:
                return await registry.remove(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    plugin_id=body.plugin_id,
                    expected_digest=body.expected_digest,
                )
            except CommandConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except PluginRegistryError as exc:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={"code": exc.code, "message": str(exc)},
                ) from exc
        if isinstance(body, (PluginEnableCommand, PluginDisableCommand)):
            registry = app.state.plugin_registry
            try:
                return await registry.set_enabled(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    plugin_id=body.plugin_id,
                    expected_digest=body.expected_digest,
                    enabled=isinstance(body, PluginEnableCommand),
                )
            except CommandConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except PluginRegistryError as exc:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={"code": exc.code, "message": str(exc)},
                ) from exc
        if isinstance(body, ToolReconcileCommand):
            command_payload = {
                "type": body.type,
                "operation_id": body.operation_id,
                "decision": body.decision,
            }
            try:
                replay = await store.accepted_command_receipt(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    command_type=body.type,
                    payload=command_payload,
                )
                if replay is not None:
                    if replay.get("resumed"):
                        coordinator.wake_jobs()
                    return replay
                reconciliation = await store.get_wait_candidate(body.operation_id)
                if reconciliation is None or reconciliation.get("kind") != "tool_reconciliation":
                    raise KeyError(body.operation_id)
                run = await _owned_run(
                    store,
                    principal_id=request.state.principal_id,
                    run_id=str(reconciliation["run_id"]),
                    not_found_detail="tool reconciliation not found",
                )
                await _authorized_workspace_path(
                    store,
                    principal_id=request.state.principal_id,
                    path=run.get("workspace_path"),
                )
                await coordinator.reconcile_resume_head(str(reconciliation["run_id"]))
                results = await _tool_reconciliation_results(
                    store,
                    operation_id=body.operation_id,
                    decision=body.decision,
                )
                receipt, _created = await store.request_tool_reconcile_command(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    operation_id=body.operation_id,
                    decision=body.decision,
                    **results,
                )
            except KeyError as exc:
                raise HTTPException(
                    status_code=404, detail="tool reconciliation not found"
                ) from exc
            except (CommandConflictError, WaitDecisionConflictError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except WorkspaceAdmissionError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if receipt["resumed"]:
                coordinator.wake_jobs()
            return receipt
        if isinstance(body, PlanResolveCommand):
            instructions = (body.instructions or "").strip() or None
            command_payload: dict[str, Any] = {
                "type": body.type,
                "approval_id": body.approval_id,
                "decision": body.decision,
            }
            if instructions is not None:
                command_payload["instructions"] = instructions
            try:
                replay = await store.accepted_command_receipt(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    command_type=body.type,
                    payload=command_payload,
                )
                if replay is not None:
                    if replay.get("resumed"):
                        coordinator.wake_jobs()
                    return replay
                approval = await store.get_plan_approval(body.approval_id)
                if approval is None:
                    raise KeyError(body.approval_id)
                run = await _owned_run(
                    store,
                    principal_id=request.state.principal_id,
                    run_id=str(approval["run_id"]),
                    not_found_detail="plan approval not found",
                )
                await _authorized_workspace_path(
                    store,
                    principal_id=request.state.principal_id,
                    path=run.get("workspace_path"),
                )
                await coordinator.reconcile_resume_head(str(approval["run_id"]))
                receipt, _created = await store.request_plan_resolve_command(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    approval_id=body.approval_id,
                    decision=body.decision,
                    instructions=instructions,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="plan approval not found") from exc
            except (CommandConflictError, WaitDecisionConflictError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except WorkspaceAdmissionError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if receipt["resumed"]:
                coordinator.wake_jobs()
            return receipt
        if isinstance(body, ResolvePermissionCommand):
            edited_action = body.edited_action.model_dump() if body.edited_action else None
            command_payload: dict[str, Any] = {
                "type": body.type,
                "permission_id": body.permission_id,
                "decision": body.decision,
                "scope": body.scope,
            }
            if edited_action is not None:
                command_payload["edited_action"] = edited_action
            try:
                replay = await store.accepted_command_receipt(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    command_type=body.type,
                    payload=command_payload,
                )
                if replay is not None:
                    if replay.get("resumed"):
                        coordinator.wake_jobs()
                    return replay
                permission = await store.get_permission(body.permission_id)
                if permission is None:
                    raise KeyError(body.permission_id)
                run = await _owned_run(
                    store,
                    principal_id=request.state.principal_id,
                    run_id=str(permission["run_id"]),
                    not_found_detail="permission not found",
                )
                await _authorized_workspace_path(
                    store,
                    principal_id=request.state.principal_id,
                    path=run.get("workspace_path"),
                )
                await coordinator.reconcile_resume_head(str(permission["run_id"]))
                receipt, _created = await store.request_permission_resolve_command(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    permission_id=body.permission_id,
                    decision=body.decision,
                    scope=body.scope,
                    edited_action=edited_action,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="permission not found") from exc
            except (CommandConflictError, WaitDecisionConflictError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except PermissionScopeNotAllowedError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except WorkspaceAdmissionError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if receipt["resumed"]:
                coordinator.wake_jobs()
            return receipt
        if isinstance(body, AnswerQuestionCommand):
            command_payload = {
                "type": body.type,
                "question_id": body.question_id,
                "answers": body.answers,
            }
            try:
                replay = await store.accepted_command_receipt(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    command_type=body.type,
                    payload=command_payload,
                )
                if replay is not None:
                    if replay.get("resumed"):
                        coordinator.wake_jobs()
                    return replay
                question = await store.get_question(body.question_id)
                if question is None:
                    raise KeyError(body.question_id)
                run = await _owned_run(
                    store,
                    principal_id=request.state.principal_id,
                    run_id=str(question["run_id"]),
                    not_found_detail="question not found",
                )
                await _authorized_workspace_path(
                    store,
                    principal_id=request.state.principal_id,
                    path=run.get("workspace_path"),
                )
                await coordinator.reconcile_resume_head(str(question["run_id"]))
                receipt, _created = await store.request_question_answer_command(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    question_id=body.question_id,
                    answers=body.answers,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="question not found") from exc
            except (CommandConflictError, WaitDecisionConflictError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except WorkspaceAdmissionError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if receipt["resumed"]:
                coordinator.wake_jobs()
            return receipt
        try:
            receipt, created = await store.request_run_cancel_command(
                principal_id=request.state.principal_id,
                command_id=body.command_id,
                run_id=body.run_id,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "run_not_found", "message": "run not found"},
            ) from exc
        except CommandConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if created and receipt["canceled"]:
            await coordinator.cancel_run(body.run_id)
        return receipt

    @app.post("/v1/runs", response_model=LocalRun)
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
        coordinator: RunCoordinator = app.state.coordinator
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
            return await _run_with_inputs(app.state.store, run)
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

    @app.get("/v1/schedules", response_model=ListScheduledRunsResponse)
    async def list_schedules(
        request: Request,
        status: str | None = Query(default=None),
        notify_pending: bool = Query(default=False),
    ) -> dict[str, Any]:
        store: LocalStore = app.state.store
        schedules = await store.list_scheduled_runs_for_principal(
            principal_id=request.state.principal_id,
            status=status,
            notify_pending=notify_pending,
        )
        return {"schedules": schedules}

    @app.post("/v1/schedules", response_model=LocalScheduledRun)
    async def create_schedule(request: Request, body: CreateScheduledRunRequest) -> dict[str, Any]:
        goal = body.goal.strip()
        if not goal:
            raise HTTPException(status_code=400, detail="goal required")
        store: LocalStore = app.state.store
        principal_id = request.state.principal_id
        workspace_path = (
            await _normalized_path(body.workspace_path) if body.workspace_path is not None else None
        )
        try:
            return await store.create_scheduled_run(
                principal_id=principal_id,
                goal=goal,
                run_at=_normalize_schedule_time(body.run_at),
                workspace_path=workspace_path,
                model=body.model.strip(),
                history=body.history or [],
                settings=freeze_run_settings(
                    app.state.settings,
                    {**(body.settings or {}), "permission_mode": body.permission_mode},
                ),
                metadata=sanitize_run_metadata(body.metadata),
            )
        except WorkspaceAdmissionError as exc:
            status_code = 409 if "no longer available" in str(exc) else 403
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    @app.delete("/v1/schedules/{schedule_id}", response_model=LocalScheduledRun)
    async def cancel_schedule(request: Request, schedule_id: str) -> dict[str, Any]:
        store: LocalStore = app.state.store
        schedule = await store.cancel_scheduled_run(
            principal_id=request.state.principal_id,
            schedule_id=schedule_id,
        )
        if schedule is None:
            raise HTTPException(status_code=404, detail="schedule not found")
        return schedule

    @app.post("/v1/schedules/{schedule_id}/notified", response_model=LocalScheduledRun)
    async def mark_schedule_notified(request: Request, schedule_id: str) -> dict[str, Any]:
        store: LocalStore = app.state.store
        schedule = await store.mark_scheduled_run_notified(
            principal_id=request.state.principal_id,
            schedule_id=schedule_id,
        )
        if schedule is None:
            raise HTTPException(status_code=404, detail="schedule not found")
        return schedule

    @app.post("/v1/runs/{run_id}/fork", response_model=LocalRun)
    async def fork_run(request: Request, run_id: str, body: ForkRunRequest) -> dict[str, Any]:
        checkpoint_id = body.checkpoint_id.strip()
        if not checkpoint_id:
            raise HTTPException(status_code=400, detail="checkpoint_id required")
        await _owned_run(
            app.state.store,
            principal_id=request.state.principal_id,
            run_id=run_id,
        )
        coordinator: RunCoordinator = app.state.coordinator
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
            return await _run_with_inputs(app.state.store, run)
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

    @app.get("/v1/runs/{run_id}", response_model=LocalRun)
    async def get_run(request: Request, run_id: str) -> dict[str, Any]:
        """Return the flat run record (same shape as POST /runs)."""
        store: LocalStore = app.state.store
        run = await _owned_run(
            store,
            principal_id=request.state.principal_id,
            run_id=run_id,
        )
        return await _run_with_inputs(store, run)

    @app.get("/v1/runs/{run_id}/children", response_model=ListChildRunsResponse)
    async def list_child_runs(request: Request, run_id: str) -> dict[str, Any]:
        store: LocalStore = app.state.store
        await _owned_run(
            store,
            principal_id=request.state.principal_id,
            run_id=run_id,
        )
        return {"children": await store.list_child_runs_for_run(run_id)}

    @app.get(
        "/v1/runs/{run_id}/collaboration",
        response_model=LocalCollaborationSnapshot,
    )
    async def get_collaboration_snapshot(request: Request, run_id: str) -> dict[str, Any]:
        await _owned_run(
            app.state.store,
            principal_id=request.state.principal_id,
            run_id=run_id,
        )
        try:
            return await app.state.coordinator.collaboration_snapshot(run_id)
        except RunAdmissionError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    @app.get("/v1/runs/{run_id}/mailbox", response_model=ListAgentMessagesResponse)
    async def list_agent_messages(
        request: Request,
        run_id: str,
        box: Literal["inbox", "outbox"] = Query(default="inbox"),
    ) -> dict[str, Any]:
        store: LocalStore = app.state.store
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

    @app.get("/v1/runs/{run_id}/events", response_model=ListRunEventsResponse)
    async def list_run_events(
        request: Request,
        run_id: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=1000, ge=1, le=5000),
    ) -> dict[str, Any]:
        store: LocalStore = app.state.store
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

    @app.get("/v1/runs/{run_id}/stream")
    async def stream_run(
        request: Request,
        run_id: str,
        after: int = Query(default=0, ge=0),
    ) -> EventSourceResponse:
        store: LocalStore = app.state.store
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
        coordinator: RunCoordinator = app.state.coordinator

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

    @app.post("/v1/runs/{run_id}/cancel", response_model=CancelRunResponse)
    async def cancel_run(request: Request, run_id: str) -> dict[str, Any]:
        await _owned_run(
            app.state.store,
            principal_id=request.state.principal_id,
            run_id=run_id,
        )
        coordinator: RunCoordinator = app.state.coordinator
        ok = await coordinator.cancel_run(run_id)
        return {"canceled": ok}

    @app.post(
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
        store: LocalStore = app.state.store
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

    @app.post("/v1/permissions/{permission_id}", response_model=PermissionResolution)
    async def resolve_permission(
        request: Request, permission_id: str, body: ResolvePermissionRequest
    ) -> dict[str, Any]:
        """Approve, edit, or deny a parameter-bound tool review.

        Translates the client's `{decision, scope}` body into the
        `{"decisions": [{"type": "approve"|"edit"|"reject", ...}]}` shape
        that `ToolReviewMiddleware` verifies on resume. One LangGraph
        interrupt can contain multiple action requests, so the run
        resumes only after every permission in the current pause batch is
        resolved, preserving the original `permission.required` order.

        `scope=run` is a durable grant for an eligible ordinary tool for the
        rest of the run, with the same version and risk class. Irreversible
        and unknown actions cannot receive this scope.
        """
        decision_text = body.decision
        scope = body.scope
        store: LocalStore = app.state.store
        record = await store.get_permission(permission_id)
        if record is None:
            raise HTTPException(status_code=404, detail="permission not found")
        run = await _owned_run(
            store,
            principal_id=request.state.principal_id,
            run_id=record["run_id"],
            not_found_detail="permission not found",
        )
        await _authorized_workspace_path(
            store,
            principal_id=request.state.principal_id,
            path=run.get("workspace_path"),
        )
        if decision_text == "approve":
            hitl_decision: dict[str, Any] = {"type": "approve"}
            persisted_status = "approved"
        elif decision_text == "edit":
            assert body.edited_action is not None
            if body.edited_action.name != record["tool_name"]:
                raise HTTPException(status_code=400, detail="tool name cannot be changed")
            hitl_decision = {
                "type": "edit",
                "edited_action": body.edited_action.model_dump(),
            }
            persisted_status = "approved"
        else:
            hitl_decision = {
                "type": "reject",
                "message": "Tool execution denied by user.",
            }
            persisted_status = "denied"
        already_resolved = record.get("status") != "pending"
        if not already_resolved and run.get("status") in _TERMINAL_RUN_STATUSES:
            raise HTTPException(status_code=409, detail="run is not awaiting a decision")
        resolution_event = {
            "request_id": permission_id,
            "tool": record["tool_name"],
            "tool_name": record["tool_name"],
            "operation_id": record.get("operation_id"),
            "decision": decision_text,
            "scope": str(scope),
        }
        try:
            await store.resolve_permission(
                permission_id,
                status=persisted_status,
                scope=str(scope),
                decision=hitl_decision,
                event_payload=None if already_resolved else resolution_event,
            )
        except PermissionScopeNotAllowedError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (PermissionDecisionConflictError, WaitDecisionConflictError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        coordinator: RunCoordinator = app.state.coordinator
        if not already_resolved:
            coordinator.wake_run(str(record["run_id"]))
        resume_payload = await store.wait_cycle_resume_payload(
            run_id=str(record["run_id"]),
            wait_cycle_id=str(record.get("wait_cycle_id") or record["id"]),
        )
        if resume_payload is None:
            ok = False
        else:
            ok = await _ensure_resume_job(
                store=store,
                coordinator=coordinator,
                run_id=record["run_id"],
                decision=resume_payload,
            )
        return {
            "permission_id": permission_id,
            "resolved": True,
            "decision": decision_text,
            "scope": scope,
            "resumed": ok,
        }

    @app.post("/v1/questions/{question_id}", response_model=QuestionAnswer)
    async def answer_question(
        request: Request, question_id: str, body: AnswerQuestionRequest
    ) -> dict[str, Any]:
        """Submit answers to a paused user.ask interrupt.

        Body shape (per `client.ts:answerLocalQuestion`):
        `{answers: Record<string, string[]>}`. We look up the question
        by id to find its run_id, persist the answers, then resume.
        """
        answers = body.answers
        store: LocalStore = app.state.store
        record = await store.get_question(question_id)
        if record is None:
            raise HTTPException(status_code=404, detail="question not found")
        run = await _owned_run(
            store,
            principal_id=request.state.principal_id,
            run_id=record["run_id"],
            not_found_detail="question not found",
        )
        await _authorized_workspace_path(
            store,
            principal_id=request.state.principal_id,
            path=run.get("workspace_path"),
        )
        already_answered = record.get("status") != "pending"
        if not already_answered and run.get("status") in _TERMINAL_RUN_STATUSES:
            raise HTTPException(status_code=409, detail="run is not awaiting a decision")
        answer_event = {"request_id": question_id, "answers": answers}
        try:
            await store.answer_question(
                question_id,
                answers=answers,
                event_payload=None if already_answered else answer_event,
            )
        except WaitDecisionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        coordinator: RunCoordinator = app.state.coordinator
        if not already_answered:
            coordinator.wake_run(str(record["run_id"]))
        resume_payload = await store.wait_cycle_resume_payload(
            run_id=str(record["run_id"]),
            wait_cycle_id=str(record.get("wait_cycle_id") or record["id"]),
        )
        if resume_payload is not None:
            ok = await _ensure_resume_job(
                store=store,
                coordinator=coordinator,
                run_id=record["run_id"],
                decision=resume_payload,
            )
        else:
            ok = False
        return {
            "question_id": question_id,
            "answered": True,
            "resumed": ok,
        }

    @app.post(
        "/v1/tool-reconciliations/{operation_id}",
        response_model=ToolReconciliationResolution,
    )
    async def reconcile_tool_operation(
        request: Request,
        operation_id: str,
        body: ReconcileToolRequest,
    ) -> dict[str, Any]:
        store: LocalStore = app.state.store
        command_id = f"legacy_reconcile_{operation_id}"
        try:
            replay = await store.accepted_command_receipt(
                principal_id=request.state.principal_id,
                command_id=command_id,
                command_type="tool.reconcile",
                payload={
                    "type": "tool.reconcile",
                    "operation_id": operation_id,
                    "decision": body.decision,
                },
            )
        except CommandConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if replay is not None:
            if replay.get("resumed"):
                app.state.coordinator.wake_jobs()
            return replay
        record = await store.get_wait_candidate(operation_id)
        if record is None or record.get("kind") != "tool_reconciliation":
            raise HTTPException(status_code=404, detail="tool reconciliation not found")
        run = await _owned_run(
            store,
            principal_id=request.state.principal_id,
            run_id=str(record["run_id"]),
            not_found_detail="tool reconciliation not found",
        )
        await _authorized_workspace_path(
            store,
            principal_id=request.state.principal_id,
            path=run.get("workspace_path"),
        )
        if run.get("status") in _TERMINAL_RUN_STATUSES:
            raise HTTPException(status_code=409, detail="run is not awaiting a decision")
        coordinator: RunCoordinator = app.state.coordinator
        await coordinator.reconcile_resume_head(str(record["run_id"]))
        try:
            results = await _tool_reconciliation_results(
                store,
                operation_id=operation_id,
                decision=body.decision,
            )
            receipt, _created = await store.request_tool_reconcile_command(
                principal_id=request.state.principal_id,
                command_id=command_id,
                operation_id=operation_id,
                decision=body.decision,
                **results,
            )
        except (CommandConflictError, WaitDecisionConflictError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except WorkspaceAdmissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if receipt["resumed"]:
            coordinator.wake_jobs()
        return receipt

    @app.post("/v1/plans/{approval_id}", response_model=PlanApprovalResolution)
    async def resolve_plan_approval(
        request: Request,
        approval_id: str,
        body: ResolvePlanApprovalRequest,
    ) -> dict[str, Any]:
        """Approve, revise, or reject a Plan Mode `write_todos` pause."""
        decision_text = body.decision
        instructions = (body.instructions or "").strip() or None
        if decision_text == "modify" and not instructions:
            raise HTTPException(status_code=400, detail="instructions required")

        store: LocalStore = app.state.store
        record = await store.get_plan_approval(approval_id)
        if record is None:
            raise HTTPException(status_code=404, detail="plan approval not found")
        run = await _owned_run(
            store,
            principal_id=request.state.principal_id,
            run_id=record["run_id"],
            not_found_detail="plan approval not found",
        )
        await _authorized_workspace_path(
            store,
            principal_id=request.state.principal_id,
            path=run.get("workspace_path"),
        )
        if run.get("status") in _TERMINAL_RUN_STATUSES:
            raise HTTPException(status_code=409, detail="run is not awaiting a decision")
        coordinator: RunCoordinator = app.state.coordinator
        await coordinator.reconcile_resume_head(str(record["run_id"]))
        try:
            receipt, _created = await store.request_plan_resolve_command(
                principal_id=request.state.principal_id,
                command_id=f"legacy_plan_{approval_id}",
                approval_id=approval_id,
                decision=decision_text,
                instructions=instructions,
            )
        except (CommandConflictError, WaitDecisionConflictError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except WorkspaceAdmissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if receipt["resumed"]:
            coordinator.wake_jobs()
        return receipt

    @app.post(
        "/v1/workspaces/diagnose",
        response_model=LocalWorkspaceDiagnosis,
        response_model_exclude_none=True,
    )
    async def diagnose_workspace(
        request: Request, body: DiagnoseWorkspaceRequest
    ) -> dict[str, Any]:
        """Inspect a candidate path against the authorization registry.

        Response matches the TS `LocalWorkspaceDiagnosis` shape — the
        `reason` enum drives the workspace-picker's "why disabled?"
        copy, keep it stable.
        """
        store: LocalStore = app.state.store
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

    @app.delete("/v1/memory", response_model=ClearMemoryResponse)
    async def clear_memory(request: Request) -> dict[str, Any]:
        """Wipe this authenticated principal's long-term memory namespaces.

        Backs the "清空记忆 / Clear memory" button in the agent settings
        dialog. Walks every ("notes", ...) namespace in pages of 200
        (matches the BaseStore default search limit ceiling for SQLite stores)
        and deletes each key. Returns the total count so the UI can render an
        accurate "cleared N memories" toast.

        Idempotent: calling it on an empty store returns
        `deleted_count: 0` without error.
        """
        from .tools.memory import NAMESPACE, memory_namespace_prefix

        agent_store = getattr(app.state, "agent_store", None)
        if agent_store is None:
            raise HTTPException(status_code=503, detail="memory store not initialized")
        deleted = 0
        # `asearch(query=None)` returns everything in the namespace
        # (no semantic ranking needed — we just want the keys).
        # Loop until a page comes back smaller than `limit` so we don't
        # over-fetch on a single-item store.
        page_size = 200
        principal_prefix = memory_namespace_prefix(request.state.principal_id)
        namespaces = [principal_prefix]
        if request.state.principal_id == LOCAL_OWNER_PRINCIPAL_ID:
            namespaces.insert(0, NAMESPACE)
        if hasattr(agent_store, "alist_namespaces"):
            namespaces = (
                [NAMESPACE] if request.state.principal_id == LOCAL_OWNER_PRINCIPAL_ID else []
            )
            offset = 0
            while True:
                page = await agent_store.alist_namespaces(
                    prefix=principal_prefix,
                    limit=page_size,
                    offset=offset,
                )
                if not page:
                    break
                namespaces.extend(page)
                if len(page) < page_size:
                    break
                offset += len(page)
        for namespace in namespaces:
            while True:
                items = await agent_store.asearch(namespace, limit=page_size)
                if not items:
                    break
                for item in items:
                    try:
                        await agent_store.adelete(namespace, item.key)
                        deleted += 1
                    except Exception as exc:
                        log.warning(
                            "memory delete failed namespace=%s key=%s: %s", namespace, item.key, exc
                        )
                if len(items) < page_size:
                    break
        return {"cleared": True, "deleted_count": deleted}

    app.include_router(model_service_router)
    app.include_router(diagnostics_router)
    app.include_router(content_router)
    app.include_router(catalog_router)
    return app


def _current_permission_batch(
    raw_events: list[dict[str, Any]],
    permissions: list[dict[str, Any]],
    fallback_permission_id: str,
) -> list[dict[str, Any]]:
    """Return permission rows for the currently paused HITL batch.

    deepagents/LangGraph can bundle several tool approvals into one interrupt
    and expects one ordered `decisions` list on resume. We derive the batch from
    `permission.required` events emitted since the latest run start/resume
    boundary; if old rows or a sparse event log confuse that lookup, fall back
    to the single permission the user just resolved.
    """
    permission_by_id = {
        permission_id: permission
        for permission in permissions
        if (permission_id := str(permission.get("id") or ""))
    }
    batch_ids = _current_permission_batch_ids(raw_events)
    if fallback_permission_id not in batch_ids:
        batch_ids = [fallback_permission_id]

    batch: list[dict[str, Any]] = []
    seen: set[str] = set()
    for permission_id in batch_ids:
        if permission_id in seen:
            continue
        record = permission_by_id.get(permission_id)
        if record is None:
            continue
        seen.add(permission_id)
        batch.append(record)
    fallback = permission_by_id.get(fallback_permission_id)
    if not batch and fallback is not None:
        return [fallback]
    return batch


async def _ensure_resume_job(
    *,
    store: LocalStore,
    coordinator: RunCoordinator,
    run_id: str,
    decision: dict[str, Any],
) -> bool:
    """Idempotently ensure a resolved wait has a durable resume owner.

    The decision and resume job are currently separate SQLite transactions.
    Replaying the same decision repairs the crash window between them instead
    of falsely acknowledging a run that remains permanently paused.
    """
    if await coordinator.resume_run(run_id=run_id, decision=decision):
        return True
    run = await store.get_run(run_id)
    if run is None:
        return False
    if run.get("status") in {"completed", "failed", "canceled"}:
        return True
    if run.get("status") not in {"waiting_permission", "waiting_input"}:
        return False
    active_job = await store.get_active_run_job(run_id)
    return bool(
        active_job
        and active_job.get("kind") == "resume"
        and active_job.get("status") in {"pending", "leased"}
    )


def _current_permission_batch_ids(raw_events: list[dict[str, Any]]) -> list[str]:
    boundary_index = -1
    for index, event in enumerate(raw_events):
        if event.get("event_type") in {"run.started", "run.resumed"}:
            boundary_index = index

    request_ids: list[str] = []
    seen: set[str] = set()
    for event in raw_events[boundary_index + 1 :]:
        if event.get("event_type") != "permission.required":
            continue
        payload = _event_payload(event)
        request_id = _first_string(payload.get("request_id"), payload.get("id"))
        if request_id is None or request_id in seen:
            continue
        seen.add(request_id)
        request_ids.append(request_id)
    return request_ids


def _thread_record_for_api(thread: dict[str, Any]) -> dict[str, Any]:
    try:
        metadata = json.loads(thread.get("metadata_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        metadata = {}
    return {**thread, "metadata": metadata if isinstance(metadata, dict) else {}}


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    if isinstance(payload, dict):
        return payload
    payload_json = event.get("payload_json")
    if isinstance(payload_json, str):
        try:
            parsed = json.loads(payload_json)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _tool_reconciliation_results(
    store: LocalStore,
    *,
    operation_id: str,
    decision: str,
) -> dict[str, str | None]:
    record = await store.get_wait_candidate(operation_id)
    if record is None or record.get("kind") != "tool_reconciliation":
        raise KeyError(operation_id)
    payload = _json_object(record.get("payload_json"))
    current_receipt = await store.get_tool_receipt(operation_id)
    prior_operation_id = str(payload.get("prior_operation_id") or operation_id)
    prior_receipt = await store.get_tool_receipt(prior_operation_id)
    if current_receipt is None or prior_receipt is None:
        raise WaitDecisionConflictError("tool reconciliation receipt is missing")
    current_result = (
        _tool_reconciliation_result(current_receipt, decision)
        if decision != "retry_not_executed"
        else None
    )
    prior_result = _tool_reconciliation_result(
        prior_receipt,
        "abort" if decision == "retry_not_executed" else decision,
    )
    return {
        "current_result_json": current_result,
        "current_result_hash": (
            hashlib.sha256(current_result.encode()).hexdigest()
            if current_result is not None
            else None
        ),
        "prior_result_json": prior_result,
        "prior_result_hash": hashlib.sha256(prior_result.encode()).hexdigest(),
    }


def _tool_reconciliation_result(receipt: dict[str, Any], decision: str) -> str:
    completed = decision == "confirmed_completed"
    return serialize_tool_result(
        ToolMessage(
            content=(
                "The user verified that the external action completed successfully."
                if completed
                else "The user verified that this uncertain action must not be retried automatically."
            ),
            name=str(receipt.get("tool_name") or ""),
            tool_call_id=str(receipt.get("tool_call_id") or ""),
            status="success" if completed else "error",
        )
    )


def _hitl_decision_for_permission(permission: dict[str, Any]) -> dict[str, Any]:
    raw = permission.get("decision_json")
    if isinstance(raw, str) and raw:
        try:
            decision = json.loads(raw)
        except json.JSONDecodeError:
            decision = None
        if isinstance(decision, dict):
            return decision
    # Compatibility for permission rows created before decision_json existed.
    if permission.get("status") == "approved":
        return {"type": "approve"}
    return {"type": "reject", "message": "Tool execution denied by user."}


app = create_app()
