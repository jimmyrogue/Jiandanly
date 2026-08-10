"""FastAPI application + HTTP route surface.

Phase 2' deliverables:
- `/v1/health` (no auth)
- `/v1/tools` (list available tools — placeholder for now)
- `/v1/workspaces` (CRUD authorization records)
- `/v1/runs` (placeholder: real impl lands in Phase 3')
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from urllib.parse import urljoin

from fastapi import FastAPI, Request
from fastapi import HTTPException as HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .agent.builder import (
    open_checkpointer,
    open_store,
)
from .auth import LOCAL_OWNER_PRINCIPAL_ID, PairingTokenAuthMiddleware
from .catalog_routes import catalog_router
from .central_diagnostics import (
    CentralDiagnosticsManager,
)
from .config import Settings, get_settings
from .content_routes import content_router
from .diagnostics_routes import diagnostics_router
from .http_body_limit import RequestBodyLimitMiddleware
from .model_service_authorization import _complete_shejane_authorization
from .model_service_routes import model_service_router
from .plugin_routes import plugin_router
from .plugins.browser_qa import BROWSER_QA_PLUGIN_ID
from .plugins.catalog import PluginCatalog
from .plugins.platforms import current_managed_worker_platform
from .plugins.registry import PluginRegistry
from .run_decision_routes import run_decision_router
from .run_routes import run_router
from .runs import (
    RunCoordinator,
)
from .runtime_routes import _apply_runtime_settings, runtime_router
from .scheduler import ScheduledRunDispatcher
from .shejane_authorization import (
    OFFICIAL_CLOUD_ORIGIN,
    SheJaneAuthorizationManager,
)
from .store.sqlite import (
    LocalStore,
)
from .thread_routes import thread_router
from .workspace_routes import workspace_router

log = logging.getLogger("shejane_runtime.server")


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


_TERMINAL_RUN_STATUSES = {"completed", "failed", "canceled", "cleanup_required"}


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

    app.include_router(run_router)
    app.include_router(run_decision_router)
    app.include_router(runtime_router)
    app.include_router(workspace_router)
    app.include_router(model_service_router)
    app.include_router(diagnostics_router)
    app.include_router(content_router)
    app.include_router(thread_router)
    app.include_router(plugin_router)
    app.include_router(catalog_router)
    return app


app = create_app()
