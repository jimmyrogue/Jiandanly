"""A2A peer authentication, rate limiting, tracing, and request audit middleware."""

from __future__ import annotations

import time
from typing import Any

from a2a.server.request_handlers.response_helpers import build_error_response
from a2a.utils.errors import ContentTypeNotSupportedError
from fastapi import FastAPI
from starlette.authentication import AuthCredentials, SimpleUser
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .oidc import OIDCAuthenticator, OIDCUnavailableError
from .store import A2AGatewayStore
from .trace_context import bind_trace, new_server_trace, trace_id


class A2APeerAuthMiddleware:
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
