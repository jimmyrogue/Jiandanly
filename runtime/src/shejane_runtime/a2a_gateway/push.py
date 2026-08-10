from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from a2a.types.a2a_pb2 import TaskPushNotificationConfig
from a2a.utils.errors import InvalidParamsError, TaskNotFoundError

from shejane_runtime.tools.web import _pinned_transport

from .runtime_client import RuntimeHTTPError
from .secrets import PushSecretBox
from .store import A2AGatewayStore, A2APushConfigConflictError, _normalize_push_origin
from .trace_context import outbound_trace_headers

log = logging.getLogger("shejane_runtime.a2a_gateway.push")

PushSender = Callable[[str, dict[str, str], bytes], Awaitable[int]]


def validate_push_url(value: str, allowed_origins: list[str]) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2048
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError("push URL must contain 1 to 2048 valid characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("push URL is invalid") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("push URL must be an HTTPS URL without credentials or a fragment")
    authority = parsed.hostname
    if ":" in authority and not authority.startswith("["):
        authority = f"[{authority}]"
    if port is not None:
        authority = f"{authority}:{port}"
    origin = _normalize_push_origin(f"https://{authority}")
    if origin not in allowed_origins:
        raise ValueError("push URL origin is not allowed for this peer")
    return value


async def _send_push(url: str, headers: dict[str, str], body: bytes) -> int:
    transport, reason = _pinned_transport(url, allow_fake_ip=False)
    if transport is None:
        raise httpx.ConnectError(reason)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(20, connect=5),
        follow_redirects=False,
        transport=transport,
    ) as client:
        response = await client.post(url, headers=headers, content=body)
        return response.status_code


_EXTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_AUTH_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9!#$%&'*+.^_`|~-]{0,31}$")


def _new_push_id() -> str:
    return f"push_{uuid.uuid4().hex}"


class PushConfigMixin:
    async def create_push_config(
        self,
        params: TaskPushNotificationConfig,
        peer: dict[str, Any],
        *,
        task_id: str | None = None,
        default_id: str | None = None,
    ) -> TaskPushNotificationConfig:
        resolved_task_id = task_id or params.task_id
        if not resolved_task_id:
            raise InvalidParamsError(message="taskId is required")
        task = await self._owned_task(peer, resolved_task_id)
        if params.tenant and params.tenant != peer["tenant"]:
            raise InvalidParamsError(message="tenant does not match the authenticated peer")
        if params.task_id and params.task_id != resolved_task_id:
            raise InvalidParamsError(message="push config taskId does not match its task")
        config_id = params.id or default_id or _new_push_id()
        if _EXTERNAL_ID_RE.fullmatch(config_id) is None:
            raise InvalidParamsError(message="push config id is invalid")
        try:
            url = validate_push_url(params.url, list(peer["push_origins"]))
        except ValueError as exc:
            raise InvalidParamsError(message=str(exc)) from exc
        token = params.token or None
        if token is not None and (
            len(token) > 4096 or "\x00" in token or "\r" in token or "\n" in token
        ):
            raise InvalidParamsError(message="push config token is invalid")
        scheme: str | None = None
        credentials: str | None = None
        if params.HasField("authentication"):
            scheme = params.authentication.scheme
            credentials = params.authentication.credentials
            if (
                _AUTH_SCHEME_RE.fullmatch(scheme) is None
                or not credentials
                or len(credentials) > 4096
                or any(character in credentials for character in "\x00\r\n")
            ):
                raise InvalidParamsError(message="push authentication is invalid")

        normalized = TaskPushNotificationConfig(
            tenant=str(peer["tenant"]),
            id=config_id,
            task_id=resolved_task_id,
            url=url,
            token=token or "",
        )
        if scheme is not None and credentials is not None:
            normalized.authentication.scheme = scheme
            normalized.authentication.credentials = credentials
        fingerprint = (
            "sha256:" + hashlib.sha256(normalized.SerializeToString(deterministic=True)).hexdigest()
        )
        existing = await self.store.get_push_config(
            peer_id=str(peer["id"]),
            tenant=str(peer["tenant"]),
            task_id=resolved_task_id,
            config_id=config_id,
        )
        if existing is not None:
            if existing["request_fingerprint"] != fingerprint:
                raise InvalidParamsError(
                    message=f"push config {config_id} already exists with different content"
                )
            return self._push_config_proto(existing)
        snapshot, start_after = await self.projection.snapshot(task, peer)
        try:
            stored, _created = await self.store.create_push_config(
                config_id=config_id,
                peer_id=str(peer["id"]),
                tenant=str(peer["tenant"]),
                task_id=resolved_task_id,
                request_fingerprint=fingerprint,
                url=url,
                token_ciphertext=(
                    self.push_secret_box.encrypt(token, config_id=config_id, field="token")
                    if token is not None
                    else None
                ),
                auth_scheme=scheme,
                credentials_ciphertext=(
                    self.push_secret_box.encrypt(
                        credentials, config_id=config_id, field="credentials"
                    )
                    if credentials is not None
                    else None
                ),
                start_after=start_after,
                snapshot_payload=self.stream_response_dict(snapshot),
            )
        except A2APushConfigConflictError as exc:
            raise InvalidParamsError(message=str(exc)) from exc
        except ValueError as exc:
            raise InvalidParamsError(message=str(exc)) from exc
        self.wake_push()
        return self._push_config_proto(stored)

    async def get_push_config(
        self, *, task_id: str, config_id: str, peer: dict[str, Any]
    ) -> TaskPushNotificationConfig:
        await self._owned_task(peer, task_id)
        stored = await self.store.get_push_config(
            peer_id=str(peer["id"]),
            tenant=str(peer["tenant"]),
            task_id=task_id,
            config_id=config_id,
        )
        if stored is None:
            raise TaskNotFoundError(message="push configuration not found")
        return self._push_config_proto(stored)

    async def list_push_configs(
        self,
        *,
        task_id: str,
        peer: dict[str, Any],
        page_size: int,
        page_token: str,
    ) -> tuple[list[TaskPushNotificationConfig], str]:
        await self._owned_task(peer, task_id)
        if not 1 <= page_size <= 100:
            raise InvalidParamsError(message="pageSize must be between 1 and 100")
        rows = await self.store.list_push_configs(
            peer_id=str(peer["id"]), tenant=str(peer["tenant"]), task_id=task_id
        )
        start = 0
        if page_token:
            matches = [index for index, row in enumerate(rows) if row["id"] == page_token]
            if not matches:
                raise InvalidParamsError(message="pageToken is invalid for this peer")
            start = matches[0] + 1
        page = rows[start : start + page_size]
        has_more = start + len(page) < len(rows)
        return (
            [self._push_config_proto(row) for row in page],
            str(page[-1]["id"]) if has_more and page else "",
        )

    async def delete_push_config(
        self, *, task_id: str, config_id: str, peer: dict[str, Any]
    ) -> None:
        await self._owned_task(peer, task_id)
        deleted = await self.store.delete_push_config(
            peer_id=str(peer["id"]),
            tenant=str(peer["tenant"]),
            task_id=task_id,
            config_id=config_id,
        )
        if not deleted:
            raise TaskNotFoundError(message="push configuration not found")

    def _push_config_proto(self, stored: dict[str, Any]) -> TaskPushNotificationConfig:
        config_id = str(stored["id"])
        result = TaskPushNotificationConfig(
            tenant=str(stored["tenant"]),
            id=config_id,
            task_id=str(stored["task_id"]),
            url=str(stored["url"]),
        )
        encrypted_token = stored.get("token_ciphertext")
        if isinstance(encrypted_token, str):
            result.token = self.push_secret_box.decrypt(
                encrypted_token, config_id=config_id, field="token"
            )
        scheme = stored.get("auth_scheme")
        encrypted_credentials = stored.get("credentials_ciphertext")
        if isinstance(scheme, str) and isinstance(encrypted_credentials, str):
            result.authentication.scheme = scheme
            result.authentication.credentials = self.push_secret_box.decrypt(
                encrypted_credentials, config_id=config_id, field="credentials"
            )
        return result


class PushCoordinator:
    def __init__(
        self,
        *,
        store: A2AGatewayStore,
        runtime: Any,
        service: Any,
        secret_box: PushSecretBox,
        sender: PushSender | None = None,
    ) -> None:
        self.store = store
        self.runtime = runtime
        self.service = service
        self.secret_box = secret_box
        self.sender = sender or _send_push
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="a2a-push-outbox")

    def wake(self) -> None:
        self._wakeup.set()

    async def close(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.produce_once()
                while await self.deliver_once():
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("A2A push coordinator iteration failed")
            self._wakeup.clear()
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=0.5)
            except TimeoutError:
                pass

    async def produce_once(self) -> None:
        for task in await self.store.list_push_watch_tasks():
            run_id = task.get("runtime_run_id")
            if not isinstance(run_id, str) or not run_id:
                continue
            after = int(task["runtime_after"])
            try:
                events = await self.runtime.list_events(run_id, after=after)
            except (RuntimeHTTPError, httpx.TransportError):
                log.warning("A2A push event poll failed", extra={"task_id": task["id"]})
                continue
            peer = await self.store.get_peer(str(task["peer_id"]))
            if peer is None:
                continue
            for event in events:
                seq = event.get("seq")
                if not isinstance(seq, int) or isinstance(seq, bool) or seq <= after:
                    continue
                updates = await self.service.projection.project_stream_event(task, peer, event)
                payloads = [self.service.stream_response_dict(update) for update in updates]
                await self.store.record_push_event(
                    peer_id=str(task["peer_id"]),
                    task_id=str(task["id"]),
                    event_seq=seq,
                    payloads=payloads,
                )
                after = seq

    async def deliver_once(self) -> bool:
        delivery = await self.store.claim_push_delivery()
        if delivery is None:
            return False
        delivery_id = str(delivery["id"])
        config_id = str(delivery["config_id"])
        headers = {
            "A2A-Version": "1.0",
            "Content-Type": "application/a2a+json",
            "Idempotency-Key": delivery_id,
            "User-Agent": "SheJane-A2A-Gateway/1.0",
        }
        headers.update(outbound_trace_headers())
        scheme = delivery.get("auth_scheme")
        encrypted = delivery.get("credentials_ciphertext")
        if isinstance(scheme, str) and isinstance(encrypted, str):
            try:
                credentials = self.secret_box.decrypt(
                    encrypted, config_id=config_id, field="credentials"
                )
            except ValueError as exc:
                await self._fail(delivery, error=str(exc), retryable=False)
                return True
            headers["Authorization"] = f"{scheme} {credentials}"
        try:
            body = str(delivery["payload_json"]).encode("utf-8")
            status = await self.sender(str(delivery["url"]), headers, body)
        except (httpx.HTTPError, OSError, TimeoutError) as exc:
            await self._fail(delivery, error=type(exc).__name__, retryable=True)
            return True
        if 200 <= status < 300:
            await self.store.settle_push_delivery(delivery_id)
        else:
            retryable = status in {408, 425, 429} or status >= 500
            await self._fail(
                delivery,
                error=f"callback returned HTTP {status}",
                retryable=retryable,
            )
        return True

    async def _fail(self, delivery: dict[str, Any], *, error: str, retryable: bool) -> None:
        attempts = int(delivery["attempts"])
        dead = not retryable or attempts >= 12
        seed = int(hashlib.sha256(str(delivery["id"]).encode()).hexdigest()[:4], 16)
        delay = min(3600, 2 ** min(attempts, 11)) + seed % 3
        available = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
        await self.store.retry_push_delivery(
            str(delivery["id"]),
            available_at=available,
            error=error,
            dead=dead,
        )
