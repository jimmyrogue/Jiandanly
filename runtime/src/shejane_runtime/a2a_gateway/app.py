from __future__ import annotations

import hashlib
import json
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from a2a.server.context import ServerCallContext
from a2a.server.request_handlers.request_handler import RequestHandler
from a2a.server.request_handlers.response_helpers import (
    agent_card_to_dict,
    build_error_response,
)
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    CancelTaskRequest,
    DeleteTaskPushNotificationConfigRequest,
    GetExtendedAgentCardRequest,
    GetTaskPushNotificationConfigRequest,
    GetTaskRequest,
    HTTPAuthSecurityScheme,
    ListTaskPushNotificationConfigsRequest,
    ListTaskPushNotificationConfigsResponse,
    ListTasksRequest,
    ListTasksResponse,
    Message,
    MutualTlsSecurityScheme,
    OpenIdConnectSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    SendMessageRequest,
    StringList,
    SubscribeToTaskRequest,
    Task,
    TaskPushNotificationConfig,
)
from a2a.utils.errors import (
    ContentTypeNotSupportedError,
    InvalidParamsError,
    UnsupportedOperationError,
    VersionNotSupportedError,
)
from fastapi import FastAPI, HTTPException, Request
from starlette.authentication import AuthCredentials, SimpleUser
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from shejane_runtime.http_body_limit import RequestBodyLimitMiddleware

from .input_files import SUPPORTED_INPUT_MEDIA_TYPES, InboundFileStore
from .oidc import OIDCAuthenticator, OIDCUnavailableError, validate_oidc_configuration
from .push import PushCoordinator, PushSender
from .runtime_client import RuntimeHTTPClient
from .secrets import PushSecretBox
from .service import GatewayService
from .store import A2AGatewayStore
from .trace_context import bind_trace, new_server_trace, trace_id


@dataclass(frozen=True)
class GatewayConfig:
    db_path: Path
    runtime_base_url: str
    runtime_token: str
    public_base_url: str
    push_credential_key: bytes
    requests_per_minute: int = 120
    require_mtls: bool = False
    oidc_issuer: str | None = None
    oidc_discovery_url: str | None = None
    oidc_audience: str | None = None


def _agent_card(
    public_base_url: str,
    *,
    tenant: str = "",
    require_mtls: bool = False,
    oidc_discovery_url: str | None = None,
) -> AgentCard:
    interface = AgentInterface(
        url=f"{public_base_url.rstrip('/')}/a2a",
        protocol_binding="JSONRPC",
        protocol_version="1.0",
    )
    if tenant:
        interface.tenant = tenant
    security_schemes = {
        "bearer": SecurityScheme(
            http_auth_security_scheme=HTTPAuthSecurityScheme(
                scheme="bearer",
                bearer_format="opaque",
                description="Gateway-issued A2A peer token.",
            )
        )
    }
    required_schemes = [{"bearer": StringList()}]
    if oidc_discovery_url is not None:
        security_schemes["oidc"] = SecurityScheme(
            open_id_connect_security_scheme=OpenIdConnectSecurityScheme(
                description="OIDC-issued OAuth 2.0 JWT access token.",
                open_id_connect_url=oidc_discovery_url,
            )
        )
        required_schemes.append({"oidc": StringList()})
    if require_mtls:
        security_schemes["mtls"] = SecurityScheme(
            mtls_security_scheme=MutualTlsSecurityScheme(
                description="A client certificate issued by the configured gateway CA."
            )
        )
        for requirement in required_schemes:
            requirement["mtls"] = StringList()
    return AgentCard(
        name="SheJane A2A Gateway",
        description="A2A 1.0 adapter for a SheJane Harness Runtime.",
        supported_interfaces=[interface],
        provider=AgentProvider(organization="SheJane", url="https://shejane.com"),
        version="1.0.0",
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=True,
            extended_agent_card=True,
        ),
        security_schemes=security_schemes,
        security_requirements=[
            SecurityRequirement(schemes=requirement) for requirement in required_schemes
        ],
        default_input_modes=list(SUPPORTED_INPUT_MEDIA_TYPES),
        default_output_modes=["text/plain", "application/json"],
        skills=[
            AgentSkill(
                id="agent.run",
                name="Run an agent task",
                description="Create and observe a durable SheJane agent task.",
                tags=["agent", "task", "multi-agent"],
            )
        ],
    )


class _GatewayContextBuilder(DefaultServerCallContextBuilder):
    def build(self, request: Request) -> ServerCallContext:
        context = super().build(request)
        context.state["peer"] = request.scope.get("state", {}).get("a2a_peer")
        return context


def _peer(context: ServerCallContext, *, scope: str | None = None) -> dict[str, Any]:
    _require_version(context)
    peer = context.state.get("peer")
    if not isinstance(peer, dict):
        raise InvalidParamsError(message="authenticated A2A peer is required")
    if context.tenant and context.tenant != peer["tenant"]:
        raise InvalidParamsError(message="tenant does not match the authenticated peer")
    if scope is not None and scope not in peer["scopes"]:
        raise UnsupportedOperationError(message=f"A2A peer scope is required: {scope}")
    return peer


def _require_version(context: ServerCallContext) -> None:
    headers = context.state.get("headers")
    version = headers.get("a2a-version", "") if isinstance(headers, dict) else ""
    if version != "1.0":
        raise VersionNotSupportedError(
            message=f"A2A protocol version {version or '0.3'} is not supported"
        )


class _GatewayHandler(RequestHandler):
    def __init__(self, gateway_app: FastAPI, config: GatewayConfig) -> None:
        self.gateway_app = gateway_app
        self.config = config

    @property
    def service(self) -> GatewayService:
        return self.gateway_app.state.gateway_service

    async def on_list_tasks(
        self, params: ListTasksRequest, context: ServerCallContext
    ) -> ListTasksResponse:
        peer = _peer(context, scope="tasks.read")
        return await self.service.list_tasks(params, peer)

    async def on_get_extended_agent_card(
        self, params: GetExtendedAgentCardRequest, context: ServerCallContext
    ) -> AgentCard:
        _require_version(context)
        peer = context.state.get("peer")
        if not isinstance(peer, dict):
            raise InvalidParamsError(message="authenticated A2A peer is required")
        return _agent_card(
            self.config.public_base_url,
            tenant=str(peer["tenant"]),
            require_mtls=self.config.require_mtls,
            oidc_discovery_url=self.config.oidc_discovery_url,
        )

    async def on_get_task(self, params: GetTaskRequest, context: ServerCallContext) -> Task | None:
        peer = _peer(context, scope="tasks.read")
        history_length = int(params.history_length) if params.HasField("history_length") else None
        if history_length is not None and not 0 <= history_length <= 100:
            raise InvalidParamsError(message="historyLength must be between 0 and 100")
        return await self.service.get_task(
            task_id=params.id,
            peer=peer,
            history_length=history_length,
        )

    async def on_cancel_task(
        self, params: CancelTaskRequest, context: ServerCallContext
    ) -> Task | None:
        peer = _peer(context, scope="tasks.cancel")
        return await self.service.cancel_task(task_id=params.id, peer=peer)

    async def on_message_send(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> Task | Message:
        peer = _peer(context, scope="tasks.create")
        return await self.service.accept_message(params, peer)

    async def on_message_send_stream(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> AsyncGenerator[Any, None]:
        peer = _peer(context, scope="tasks.create")
        async for event in self.service.stream_message(params, peer):
            yield event

    async def on_create_task_push_notification_config(
        self, params: TaskPushNotificationConfig, context: ServerCallContext
    ) -> TaskPushNotificationConfig:
        peer = _peer(context, scope="push.manage")
        return await self.service.create_push_config(params, peer)

    async def on_get_task_push_notification_config(
        self, params: GetTaskPushNotificationConfigRequest, context: ServerCallContext
    ) -> TaskPushNotificationConfig:
        peer = _peer(context, scope="push.manage")
        return await self.service.get_push_config(
            task_id=params.task_id, config_id=params.id, peer=peer
        )

    async def on_subscribe_to_task(
        self, params: SubscribeToTaskRequest, context: ServerCallContext
    ) -> AsyncGenerator[Any, None]:
        peer = _peer(context, scope="tasks.read")
        async for event in self.service.stream_task(
            task_id=params.id,
            peer=peer,
            reject_terminal=True,
        ):
            yield event

    async def on_list_task_push_notification_configs(
        self, params: ListTaskPushNotificationConfigsRequest, context: ServerCallContext
    ) -> ListTaskPushNotificationConfigsResponse:
        peer = _peer(context, scope="push.manage")
        page_size = int(params.page_size) or 50
        configs, next_page_token = await self.service.list_push_configs(
            task_id=params.task_id,
            peer=peer,
            page_size=page_size,
            page_token=params.page_token,
        )
        return ListTaskPushNotificationConfigsResponse(
            configs=configs, next_page_token=next_page_token
        )

    async def on_delete_task_push_notification_config(
        self, params: DeleteTaskPushNotificationConfigRequest, context: ServerCallContext
    ) -> None:
        peer = _peer(context, scope="push.manage")
        await self.service.delete_push_config(
            task_id=params.task_id, config_id=params.id, peer=peer
        )


class _A2APeerAuthMiddleware:
    def __init__(self, app: ASGIApp, gateway_app: FastAPI) -> None:
        self.app = app
        self.gateway_app = gateway_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path") or "")
        if scope["type"] != "http" or not (path == "/a2a" or path.startswith("/a2a/")):
            await self.app(scope, receive, send)
            return
        raw_headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        server_traceparent, tracestate = new_server_trace(
            raw_headers.get("traceparent", ""),
            raw_headers.get("tracestate", ""),
        )
        scope.setdefault("state", {})["traceparent"] = server_traceparent
        scope["state"]["tracestate"] = tracestate
        status_code = 500
        started = time.monotonic()
        peer: dict[str, Any] | None = None

        async def traced_send(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() not in {b"traceparent", b"tracestate"}
                ]
                headers.append((b"traceparent", server_traceparent.encode("ascii")))
                if tracestate:
                    headers.append((b"tracestate", tracestate.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        store: A2AGatewayStore | None = getattr(self.gateway_app.state, "gateway_store", None)
        with bind_trace(server_traceparent, tracestate):
            try:
                await self._authenticated_call(
                    scope,
                    receive,
                    traced_send,
                    raw_headers=raw_headers,
                    store=store,
                )
            finally:
                if store is not None:
                    peer = scope.get("state", {}).get("a2a_peer")
                    raw_length = raw_headers.get("content-length", "")
                    try:
                        content_length = int(raw_length) if raw_length else None
                    except ValueError:
                        content_length = None
                    await store.append_audit_event(
                        peer_id=str(peer["id"]) if isinstance(peer, dict) else None,
                        tenant=str(peer["tenant"]) if isinstance(peer, dict) else None,
                        trace_id=trace_id(server_traceparent),
                        http_method=str(scope.get("method") or ""),
                        path=path,
                        http_status=status_code,
                        duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                        content_length=content_length,
                    )

    async def _authenticated_call(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        raw_headers: dict[str, str],
        store: A2AGatewayStore | None,
    ) -> None:
        authorization = raw_headers.get("authorization", "")
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        peer = None
        if store is not None and token.startswith("sj_a2a."):
            peer = await store.authenticate_peer(token)
        elif store is not None and token:
            authenticator: OIDCAuthenticator | None = getattr(
                self.gateway_app.state, "gateway_oidc_authenticator", None
            )
            if authenticator is not None:
                try:
                    identity = await authenticator.authenticate(token)
                except OIDCUnavailableError:
                    response = JSONResponse(
                        {"error": "OIDC identity provider is temporarily unavailable"},
                        status_code=503,
                        headers={"Retry-After": "30"},
                    )
                    await response(scope, receive, send)
                    return
                if identity is not None:
                    peer = await store.authenticate_oidc_peer(
                        issuer=identity[0], subject=identity[1]
                    )
        if peer is None:
            response = JSONResponse(
                {"error": "invalid A2A peer token"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="shejane-a2a"'},
            )
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})["a2a_peer"] = peer
        scope["user"] = SimpleUser(str(peer["id"]))
        scope["auth"] = AuthCredentials(list(peer["scopes"]))
        allowed, retry_after = await store.consume_request_rate(
            peer_id=str(peer["id"]),
            limit_per_minute=self.gateway_app.state.gateway_config.requests_per_minute,
            epoch_seconds=int(time.time()),
        )
        if not allowed:
            response = JSONResponse(
                {"error": "A2A peer request rate exceeded"},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
            await response(scope, receive, send)
            return
        content_type = raw_headers.get("content-type", "").partition(";")[0].strip().lower()
        if scope.get("method") == "POST" and content_type != "application/json":
            response = JSONResponse(
                build_error_response(
                    None,
                    ContentTypeNotSupportedError(
                        message="A2A JSON-RPC requests must use application/json"
                    ),
                )
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_gateway_app(
    config: GatewayConfig,
    *,
    runtime_client: Any | None = None,
    push_sender: PushSender | None = None,
    oidc_authenticator: OIDCAuthenticator | None = None,
) -> FastAPI:
    oidc_config = validate_oidc_configuration(
        issuer=config.oidc_issuer,
        discovery_url=config.oidc_discovery_url,
        audience=config.oidc_audience,
    )
    if oidc_authenticator is not None and oidc_config is None:
        raise ValueError("an OIDC authenticator requires OIDC gateway configuration")
    if oidc_authenticator is None and oidc_config is not None:
        oidc_authenticator = OIDCAuthenticator(
            issuer=oidc_config[0],
            discovery_url=oidc_config[1],
            audience=oidc_config[2],
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = await A2AGatewayStore.open(config.db_path)
        runtime = runtime_client or RuntimeHTTPClient(
            base_url=config.runtime_base_url,
            token=config.runtime_token,
        )
        app.state.gateway_store = store
        app.state.gateway_runtime = runtime
        secret_box = PushSecretBox(config.push_credential_key)
        input_files = InboundFileStore(config.db_path.parent / "inputs")
        service = GatewayService(
            store,
            runtime,
            public_base_url=config.public_base_url,
            push_secret_box=secret_box,
            input_files=input_files,
        )
        push = PushCoordinator(
            store=store,
            runtime=runtime,
            service=service,
            secret_box=secret_box,
            sender=push_sender,
        )
        service.wake_push = push.wake
        app.state.gateway_service = service
        app.state.gateway_secret_box = secret_box
        app.state.gateway_push = push
        push.start()
        try:
            yield
        finally:
            await push.close()
            await store.close()
            if runtime_client is None:
                await runtime.close()

    app = FastAPI(title="SheJane A2A Gateway", version="1.0.0", lifespan=lifespan)
    app.state.gateway_config = config
    app.state.gateway_oidc_authenticator = oidc_authenticator
    app.add_middleware(_A2APeerAuthMiddleware, gateway_app=app)
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=1024 * 1024,
        detail="A2A request body exceeds the 1 MiB limit",
    )

    public_card = _agent_card(
        config.public_base_url,
        require_mtls=config.require_mtls,
        oidc_discovery_url=config.oidc_discovery_url,
    )
    public_card_json = agent_card_to_dict(public_card)
    canonical = json.dumps(public_card_json, sort_keys=True, separators=(",", ":"))
    card_etag = '"' + hashlib.sha256(canonical.encode("utf-8")).hexdigest() + '"'
    card_last_modified_at = datetime.now(UTC).replace(microsecond=0)
    card_last_modified = format_datetime(card_last_modified_at, usegmt=True)

    @app.get("/.well-known/agent-card.json", include_in_schema=False)
    async def get_agent_card(request: Request) -> Response:
        headers = {
            "Cache-Control": "public, max-age=300",
            "ETag": card_etag,
            "Last-Modified": card_last_modified,
        }
        if_none_match = request.headers.get("If-None-Match")
        not_modified = if_none_match == card_etag
        if if_none_match is None and (
            if_modified_since := request.headers.get("If-Modified-Since")
        ):
            try:
                not_modified = parsedate_to_datetime(if_modified_since) >= card_last_modified_at
            except (TypeError, ValueError):
                pass
        if not_modified:
            return Response(status_code=304, headers=headers)
        return JSONResponse(public_card_json, headers=headers)

    @app.get("/a2a/artifacts/{artifact_id}", include_in_schema=False)
    async def get_a2a_artifact(request: Request, artifact_id: str) -> Response:
        peer = getattr(request.state, "a2a_peer", None)
        if not isinstance(peer, dict) or "tasks.read" not in peer["scopes"]:
            raise HTTPException(status_code=404, detail="artifact not found")
        try:
            expires = int(request.query_params.get("expires", ""))
        except ValueError:
            raise HTTPException(status_code=404, detail="artifact not found") from None
        signature = request.query_params.get("signature", "")
        if (
            expires < int(time.time())
            or expires > int(time.time()) + 3600
            or not request.app.state.gateway_secret_box.verify_artifact(
                signature,
                peer_id=str(peer["id"]),
                artifact_id=artifact_id,
                expires=expires,
            )
        ):
            raise HTTPException(status_code=404, detail="artifact not found")
        record = await request.app.state.gateway_store.get_artifact(
            peer_id=str(peer["id"]),
            tenant=str(peer["tenant"]),
            artifact_id=artifact_id,
        )
        if record is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        try:
            upstream = await request.app.state.gateway_runtime.open_artifact_content(
                str(record["runtime_artifact_id"])
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail="artifact body is unavailable") from exc
        filename = quote(str(record["title"]), safe="")
        return StreamingResponse(
            upstream.aiter_bytes(),
            media_type=str(record["media_type"]),
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
                "Content-Length": str(record["size_bytes"]),
                "X-Content-Type-Options": "nosniff",
            },
            background=BackgroundTask(upstream.aclose),
        )

    handler = _GatewayHandler(app, config)
    for rpc_path in ("/a2a", "/a2a/"):
        app.routes.extend(
            create_jsonrpc_routes(
                handler,
                rpc_path,
                context_builder=_GatewayContextBuilder(),
            )
        )
    return app
