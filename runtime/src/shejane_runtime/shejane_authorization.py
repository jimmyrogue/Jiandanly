"""Runtime-owned native authorization for the fixed SheJane Cloud service."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import platform
import secrets
import sys
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

OFFICIAL_CLOUD_ORIGIN: Final = "https://app.shejane.com"
CALLBACK_PATH: Final = "/shejane/auth/callback"
CLIENT_ID: Final = "shejane-desktop"


class OfficialServiceUnavailable(RuntimeError):
    pass


@dataclass
class _Authorization:
    principal_id: str
    state: str
    verifier: str
    redirect_uri: str
    expires_at: datetime
    server: asyncio.AbstractServer
    expiry_task: asyncio.Task[None] | None = None
    work_task: asyncio.Task[None] | None = None
    status: str = "pending"
    connection: dict[str, Any] | None = None
    error_code: str | None = None
    callback_claimed: bool = False


class SheJaneAuthorizationManager:
    def __init__(
        self,
        *,
        complete: Callable[[str, str], Awaitable[dict[str, Any]]],
        app_version: str,
        cloud_origin: str = OFFICIAL_CLOUD_ORIGIN,
        ttl_seconds: float = 600,
    ) -> None:
        parsed = urlsplit(cloud_origin)
        self._cloud_origin = (
            cloud_origin.rstrip("/")
            if parsed.scheme == "https"
            and parsed.netloc
            and not parsed.path.rstrip("/")
            and not parsed.query
            and not parsed.fragment
            and parsed.username is None
            else ""
        )
        self._complete = complete
        self._app_version = app_version
        self._ttl_seconds = ttl_seconds
        self._authorizations: dict[str, _Authorization] = {}

    async def start(self, principal_id: str) -> dict[str, Any]:
        if not self._cloud_origin:
            raise OfficialServiceUnavailable("official SheJane Cloud origin is not configured")

        authorization_id = f"auth_{uuid.uuid4().hex}"
        state = _base64url(secrets.token_bytes(32))
        verifier = _base64url(secrets.token_bytes(32))

        async def receive_callback(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            await self._receive_callback(authorization_id, reader, writer)

        server = await asyncio.start_server(receive_callback, "127.0.0.1", 0, limit=8192)
        socket = server.sockets[0]
        port = int(socket.getsockname()[1])
        redirect_uri = f"http://127.0.0.1:{port}{CALLBACK_PATH}"
        expires_at = datetime.now(UTC) + timedelta(seconds=self._ttl_seconds)
        authorization = _Authorization(
            principal_id=principal_id,
            state=state,
            verifier=verifier,
            redirect_uri=redirect_uri,
            expires_at=expires_at,
            server=server,
        )
        self._authorizations[authorization_id] = authorization
        authorization.expiry_task = asyncio.create_task(self._expire(authorization_id))

        challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
        authorize_url = f"{self._cloud_origin}/shejane/authorize?" + urlencode(
            {
                "response_type": "code",
                "client_id": CLIENT_ID,
                "redirect_uri": redirect_uri,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
                "device_name": (platform.node().strip() or "SheJane Desktop")[:80],
                "platform": _platform_name(),
                "app_version": self._app_version,
            }
        )
        return {
            "authorization_id": authorization_id,
            "authorization_url": authorize_url,
            "expires_at": expires_at,
        }

    def status(self, authorization_id: str, principal_id: str) -> dict[str, Any]:
        authorization = self._authorizations.get(authorization_id)
        if authorization is None or authorization.principal_id != principal_id:
            raise KeyError(authorization_id)
        return {
            "authorization_id": authorization_id,
            "status": authorization.status,
            "connection": authorization.connection,
            "error_code": authorization.error_code,
        }

    async def close(self) -> None:
        tasks: list[asyncio.Task[None]] = []
        for authorization in self._authorizations.values():
            authorization.server.close()
            await authorization.server.wait_closed()
            for task in (authorization.expiry_task, authorization.work_task):
                if task is not None and not task.done():
                    task.cancel()
                    tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _expire(self, authorization_id: str) -> None:
        await asyncio.sleep(self._ttl_seconds)
        authorization = self._authorizations.get(authorization_id)
        if authorization is not None and authorization.status == "pending":
            authorization.status = "expired"
            authorization.error_code = "authorization_expired"
            authorization.state = ""
            authorization.verifier = ""
            authorization.server.close()
            await authorization.server.wait_closed()

    async def _receive_callback(
        self,
        authorization_id: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        authorization = self._authorizations.get(authorization_id)
        if (
            authorization is None
            or authorization.status != "pending"
            or authorization.callback_claimed
        ):
            await _reply(writer, 410, "This authorization request is no longer active.")
            return
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            if len(raw) > 8192:
                raise ValueError
            lines = raw.decode("ascii").split("\r\n")
            method, target, version = lines[0].split(" ")
            headers = {
                name.strip().lower(): value.strip()
                for line in lines[1:]
                if line and ":" in line
                for name, value in [line.split(":", 1)]
            }
            parsed = urlsplit(target)
            expected_host = urlsplit(authorization.redirect_uri).netloc
            if (
                method != "GET"
                or version not in {"HTTP/1.0", "HTTP/1.1"}
                or parsed.scheme
                or parsed.netloc
                or parsed.path != CALLBACK_PATH
                or parsed.fragment
                or headers.get("host") != expected_host
            ):
                raise ValueError
            pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
            if len(pairs) != 2 or len({key for key, _ in pairs}) != 2:
                raise ValueError
            if authorization.status != "pending" or authorization.callback_claimed:
                await _reply(writer, 410, "This authorization request is no longer active.")
                return
            authorization.callback_claimed = True
            values = dict(pairs)
            if values.get("state") != authorization.state:
                await self._fail(authorization, "state_mismatch")
                await _reply(writer, 400, "Authorization could not be completed.")
                return
            authorization.state = ""
            if authorization.expiry_task is not None:
                authorization.expiry_task.cancel()
            if set(values) == {"error", "state"} and values["error"] == "access_denied":
                authorization.status = "denied"
                authorization.error_code = "access_denied"
                authorization.verifier = ""
                authorization.server.close()
                await _reply(writer, 200, "Authorization was declined. You can return to SheJane.")
                return
            if set(values) != {"code", "state"} or not values["code"]:
                raise ValueError
        except (
            ValueError,
            UnicodeDecodeError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            if authorization.status != "pending" or authorization.callback_claimed:
                await _reply(writer, 410, "This authorization request is no longer active.")
                return
            await self._fail(authorization, "invalid_callback")
            await _reply(writer, 400, "Authorization could not be completed.")
            return
        except TimeoutError:
            if authorization.status != "pending" or authorization.callback_claimed:
                await _reply(writer, 410, "This authorization request is no longer active.")
                return
            await self._fail(authorization, "invalid_callback")
            await _reply(writer, 408, "Authorization callback timed out.")
            return

        authorization.server.close()
        authorization.work_task = asyncio.create_task(self._exchange(authorization, values["code"]))
        await _reply(
            writer,
            200,
            "授权已收到。你可以返回 SheJane。\n\n"
            "Authorization received. You can return to SheJane.",
        )

    async def _exchange(self, authorization: _Authorization, code: str) -> None:
        try:
            async with httpx.AsyncClient(
                timeout=15,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(
                    f"{self._cloud_origin}/api/shejane/token",
                    headers={"Accept": "application/json"},
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": authorization.redirect_uri,
                        "client_id": CLIENT_ID,
                        "code_verifier": authorization.verifier,
                    },
                )
            if response.status_code != 200:
                raise ValueError
            payload = response.json()
            token = payload.get("access_token") if isinstance(payload, dict) else None
            if not isinstance(token, str) or not token or payload.get("token_type") != "Bearer":
                raise ValueError
        except Exception:
            authorization.status = "failed"
            authorization.error_code = "token_exchange_failed"
            authorization.verifier = ""
            return
        try:
            authorization.connection = await self._complete(authorization.principal_id, token)
        except Exception:
            authorization.status = "failed"
            authorization.error_code = "service_connection_failed"
        else:
            authorization.status = "succeeded"
        finally:
            authorization.verifier = ""

    async def _fail(self, authorization: _Authorization, code: str) -> None:
        authorization.status = "failed"
        authorization.error_code = code
        authorization.state = ""
        authorization.verifier = ""
        authorization.server.close()


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _platform_name() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    return "linux"


async def _reply(writer: asyncio.StreamWriter, status: int, message: str) -> None:
    reason = {200: "OK", 400: "Bad Request", 408: "Request Timeout", 410: "Gone"}[status]
    body = message.encode("utf-8")
    writer.write(
        (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "Cache-Control: no-store\r\n"
            "X-Content-Type-Options: nosniff\r\n"
            "Connection: close\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode("ascii")
        + body
    )
    await writer.drain()
    writer.close()
    await writer.wait_closed()
