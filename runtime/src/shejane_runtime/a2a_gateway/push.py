from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx

from shejane_runtime.tools.web import _pinned_transport

from .runtime_client import RuntimeHTTPError
from .secrets import PushSecretBox
from .store import A2AGatewayStore, _normalize_push_origin
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
                updates = await self.service._project_stream_event(task, peer, event)
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
