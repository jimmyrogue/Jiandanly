"""Official A2A ITK test adapter around the production SheJane gateway.

The ITK reference agents intentionally run over loopback HTTP and exchange a
private protobuf instruction media type.  Production SheJane keeps its HTTPS,
authentication, media-type, Artifact, and SSRF rules unchanged; this executable
adapts only the protocol test fixture at the process boundary.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import importlib
import json
import logging
import sys
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import uvicorn
from a2a.client import Client, ClientConfig, create_client
from a2a.types.a2a_pb2 import (
    ROLE_USER,
    CancelTaskRequest,
    Message,
    Part,
    SendMessageRequest,
    SubscribeToTaskRequest,
    TaskPushNotificationConfig,
)
from a2a_tck_sut import TCKAdapter
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.types import Message as ASGIMessage

from shejane_runtime.a2a_gateway.app import GatewayConfig, create_gateway_app
from shejane_runtime.a2a_gateway.outbound import connect_a2a_agent
from shejane_runtime.a2a_gateway.runtime_client import RuntimeHTTPError
from shejane_runtime.a2a_gateway.store import A2AGatewayStore

_PROTO_MEDIA_TYPE = "application/x-protobuf"
_TEST_MEDIA_TYPE = "application/json"
_WEBHOOK_MARKER = "https://tck-webhook.invalid/callback/"
log = logging.getLogger("shejane.a2a.itk")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _message(text: str, *, task_id: str = "", context_id: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {
        "messageId": f"itk-message-{uuid.uuid4().hex}",
        "role": "ROLE_AGENT",
        "parts": [{"text": text, "mediaType": "text/plain"}],
    }
    if task_id:
        result["taskId"] = task_id
    if context_id:
        result["contextId"] = context_id
    return result


def _artifact_text(artifact: object) -> str:
    if not isinstance(artifact, dict):
        return ""
    rendered: list[str] = []
    for part in artifact.get("parts", []):
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("text"), str):
            rendered.append(str(part["text"]))
        elif "data" in part:
            rendered.append(json.dumps(part["data"], ensure_ascii=False, separators=(",", ":")))
    return "\n".join(rendered)


def _adapt_itk_result(value: object) -> object:
    """Expose spec-compliant Artifacts to the ITK's message-only verifier."""
    if not isinstance(value, dict):
        return value
    result = value.get("result")
    if isinstance(result, dict):
        _adapt_itk_result(result)

    task = value.get("task")
    if isinstance(task, dict):
        texts = [_artifact_text(item) for item in task.get("artifacts", [])]
        text = "\n".join(item for item in texts if item)
        status = task.get("status")
        if text and isinstance(status, dict):
            status["message"] = _message(
                text,
                task_id=str(task.get("id") or ""),
                context_id=str(task.get("contextId") or ""),
            )

    update = value.get("artifactUpdate")
    if isinstance(update, dict):
        text = _artifact_text(update.get("artifact"))
        if text:
            value.pop("artifactUpdate", None)
            value["statusUpdate"] = {
                "taskId": str(update.get("taskId") or ""),
                "contextId": str(update.get("contextId") or ""),
                "status": {
                    "state": "TASK_STATE_WORKING",
                    "timestamp": _now(),
                    "message": _message(
                        text,
                        task_id=str(update.get("taskId") or ""),
                        context_id=str(update.get("contextId") or ""),
                    ),
                },
            }
    return value


def _adapt_itk_json(body: bytes) -> bytes:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    return json.dumps(
        _adapt_itk_result(payload), ensure_ascii=False, separators=(",", ":")
    ).encode()


def _adapt_itk_card(body: bytes) -> bytes:
    try:
        card = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    for requirement in card.get("securityRequirements", []):
        schemes = requirement.get("schemes") if isinstance(requirement, dict) else None
        if not isinstance(schemes, dict):
            continue
        for name, scopes in schemes.items():
            if isinstance(scopes, dict):
                values = scopes.get("list", [])
                schemes[name] = values if isinstance(values, list) else []
    return json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode()


def _adapt_itk_sse(body: bytes) -> bytes:
    lines = body.splitlines(keepends=True)
    rendered: list[bytes] = []
    for line in lines:
        if line.startswith(b"data:"):
            prefix, payload = line.split(b":", 1)
            if payload.endswith(b"\r\n"):
                ending = b"\r\n"
                payload = payload[:-2]
            elif payload.endswith(b"\n"):
                ending = b"\n"
                payload = payload[:-1]
            else:
                ending = b""
            payload = payload.lstrip()
            line = prefix + b": " + _adapt_itk_json(payload) + ending
        rendered.append(line)
    return b"".join(rendered)


class ITKAdapter:
    """Translate only the official ITK fixture's known wire mismatches."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        cleanup: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.app = app
        self.cleanup = cleanup

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            try:
                await self.app(scope, receive, send)
            finally:
                if self.cleanup is not None:
                    await self.cleanup()
            return
        path = str(scope.get("path") or "")
        if scope["type"] == "http" and path == "/.well-known/agent-card.json":
            await self._card(scope, receive, send)
            return
        if scope["type"] != "http" or not (
            path in {"/a2a", "/jsonrpc", "/jsonrpc/"} or path.startswith("/a2a/")
        ):
            await self.app(scope, receive, send)
            return
        if path in {"/jsonrpc", "/jsonrpc/"}:
            scope = {**scope, "path": "/a2a", "raw_path": b"/a2a"}

        body = bytearray()
        while True:
            message = await receive()
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            params = payload.get("params")
            inbound = params.get("message") if isinstance(params, dict) else None
            if isinstance(inbound, dict):
                for part in inbound.get("parts", []):
                    if isinstance(part, dict) and part.get("mediaType") == _PROTO_MEDIA_TYPE:
                        part["mediaType"] = _TEST_MEDIA_TYPE
            body = bytearray(json.dumps(payload, separators=(",", ":")).encode())

        start: ASGIMessage | None = None
        streaming = False
        response_body = bytearray()
        sse_buffer = bytearray()

        async def transformed_send(message: ASGIMessage) -> None:
            nonlocal start, streaming
            if message["type"] == "http.response.start":
                start = message
                content_type = next(
                    (
                        value.decode("latin-1")
                        for name, value in message.get("headers", [])
                        if name.lower() == b"content-type"
                    ),
                    "",
                )
                streaming = content_type.startswith("text/event-stream")
                if streaming:
                    await send(self._response_start(message, content_length=None))
                return
            if message["type"] != "http.response.body":
                await send(message)
                return
            chunk = message.get("body", b"")
            if streaming:
                sse_buffer.extend(chunk)
                while (boundary := self._sse_boundary(sse_buffer)) is not None:
                    offset, length = boundary
                    event = bytes(sse_buffer[: offset + length])
                    del sse_buffer[: offset + length]
                    await send(
                        {
                            "type": "http.response.body",
                            "body": _adapt_itk_sse(event),
                            "more_body": True,
                        }
                    )
                if not message.get("more_body", False):
                    if sse_buffer:
                        await send(
                            {
                                "type": "http.response.body",
                                "body": _adapt_itk_sse(bytes(sse_buffer)),
                                "more_body": True,
                            }
                        )
                    await send({"type": "http.response.body", "body": b""})
                return
            response_body.extend(chunk)
            if message.get("more_body", False):
                return
            adapted = _adapt_itk_json(bytes(response_body))
            if start is None:
                raise RuntimeError("ITK adapter received a response body without headers")
            await send(self._response_start(start, content_length=len(adapted)))
            await send({"type": "http.response.body", "body": adapted})

        await self.app(
            scope,
            TCKAdapter._receive(bytes(body), receive),
            transformed_send,
        )

    async def _card(self, scope: Scope, receive: Receive, send: Send) -> None:
        start: ASGIMessage | None = None
        body = bytearray()

        async def capture(message: ASGIMessage) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
            elif message["type"] == "http.response.body":
                body.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    adapted = _adapt_itk_card(bytes(body))
                    if start is None:
                        raise RuntimeError("ITK adapter received a Card body without headers")
                    await send(self._response_start(start, content_length=len(adapted)))
                    await send({"type": "http.response.body", "body": adapted})
            else:
                await send(message)

        await self.app(scope, receive, capture)

    @staticmethod
    def _sse_boundary(buffer: bytearray) -> tuple[int, int] | None:
        candidates = [
            (offset, len(marker))
            for marker in (b"\r\n\r\n", b"\n\n")
            if (offset := buffer.find(marker)) >= 0
        ]
        return min(candidates) if candidates else None

    @staticmethod
    def _response_start(message: ASGIMessage, *, content_length: int | None) -> ASGIMessage:
        headers = [
            (name, value)
            for name, value in message.get("headers", [])
            if name.lower() != b"content-length"
        ]
        if content_length is not None:
            headers.append((b"content-length", str(content_length).encode("ascii")))
        return {**message, "headers": headers}


class LoopbackAgentTransport(httpx.AsyncBaseTransport):
    """Test-only HTTPS facade for an official ITK loopback reference agent."""

    def __init__(self, actual_base_url: str, facade_base_url: str) -> None:
        actual = urlsplit(actual_base_url)
        facade = urlsplit(facade_base_url)
        if actual.scheme != "http" or actual.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("ITK reference agent must use loopback HTTP")
        if facade.scheme != "https" or not facade.hostname:
            raise ValueError("ITK facade must use HTTPS")
        self.actual = actual
        self.facade = facade
        self.transport = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        content = await request.aread()

        async def forward(path: str) -> httpx.Response:
            target = urlunsplit(
                (
                    self.actual.scheme,
                    self.actual.netloc,
                    path,
                    request.url.query.decode()
                    if isinstance(request.url.query, bytes)
                    else request.url.query,
                    "",
                )
            )
            headers = request.headers.copy()
            headers["host"] = self.actual.netloc
            return await self.transport.handle_async_request(
                httpx.Request(
                    request.method,
                    target,
                    headers=headers,
                    content=content,
                    extensions=request.extensions,
                )
            )

        response = await forward(request.url.path)
        if response.status_code in {307, 308}:
            location = response.headers.get("location")
            redirected = urlsplit(location) if location else None
            if redirected is not None and redirected.path == f"{request.url.path}/":
                await response.aread()
                await response.aclose()
                response = await forward(redirected.path)
        if request.url.path == "/.well-known/agent-card.json":
            body = await response.aread()
            await response.aclose()
            payload = json.loads(body)
            interfaces = payload.get("supportedInterfaces", [])
            rewritten: list[dict[str, Any]] = []
            for interface in interfaces:
                if not isinstance(interface, dict):
                    continue
                if interface.get("protocolBinding") != "JSONRPC":
                    continue
                if interface.get("protocolVersion") != "1.0":
                    continue
                original = urlsplit(str(interface.get("url") or ""))
                if original.hostname not in {"127.0.0.1", "localhost"}:
                    continue
                rewritten.append(
                    {
                        **interface,
                        "url": urlunsplit(
                            (
                                self.facade.scheme,
                                self.facade.netloc,
                                original.path or "/",
                                "",
                                "",
                            )
                        ),
                    }
                )
            payload["supportedInterfaces"] = rewritten
            content = json.dumps(payload, separators=(",", ":")).encode()
            response_headers = [
                (name, value)
                for name, value in response.headers.raw
                if name.lower() not in {b"content-length", b"content-encoding"}
            ]
            return httpx.Response(
                response.status_code,
                headers=response_headers,
                content=content,
                request=request,
            )
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=response.stream,
            extensions=response.extensions,
            request=request,
        )

    async def aclose(self) -> None:
        await self.transport.aclose()


def _event_texts(event: object) -> list[str]:
    if isinstance(event, tuple):
        result: list[str] = []
        for item in event:
            result.extend(_event_texts(item))
        return result
    messages: list[Any] = []
    artifacts: list[Any] = []
    if isinstance(event, Message):
        messages.append(event)
    if hasattr(event, "HasField"):
        for field in ("message",):
            try:
                if event.HasField(field):
                    messages.append(getattr(event, field))
            except ValueError:
                pass
        try:
            if event.HasField("task"):
                task = event.task
                if task.status.HasField("message"):
                    messages.append(task.status.message)
                messages.extend(task.history)
                artifacts.extend(task.artifacts)
        except ValueError:
            pass
        try:
            if event.HasField("status_update") and event.status_update.status.HasField("message"):
                messages.append(event.status_update.status.message)
        except ValueError:
            pass
        try:
            if event.HasField("artifact_update"):
                artifacts.append(event.artifact_update.artifact)
        except ValueError:
            pass
    result = [part.text for message in messages for part in message.parts if part.text]
    result.extend(part.text for artifact in artifacts for part in artifact.parts if part.text)
    return result


def _event_task_id(event: object) -> str:
    if isinstance(event, tuple):
        for item in event:
            if task_id := _event_task_id(item):
                return task_id
        return ""
    if hasattr(event, "HasField"):
        for field in ("task", "status_update", "artifact_update"):
            try:
                if event.HasField(field):
                    value = getattr(event, field)
                    return str(value.id if field == "task" else value.task_id)
            except ValueError:
                pass
    return ""


class ITKRuntime:
    def __init__(self, itk_root: Path) -> None:
        sys.path.insert(0, str(itk_root))
        self.instruction_pb2 = importlib.import_module("agents.python.v10.pyproto.instruction_pb2")
        self.runs: dict[str, dict[str, Any]] = {}
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.artifacts: dict[str, dict[str, Any]] = {}
        self.instructions: dict[str, Any] = {}
        self.execution: dict[str, asyncio.Task[None]] = {}
        self.changes: dict[str, asyncio.Event] = {}

    async def close(self) -> None:
        tasks = list(self.execution.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _append(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self.events[run_id].append(
            {
                "seq": len(self.events[run_id]) + 1,
                "run_id": run_id,
                "event_type": event_type,
                "payload": payload,
                "created_at": _now(),
            }
        )
        self.changes[run_id].set()

    async def create_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        paths = payload.get("attachment_paths")
        if not isinstance(paths, list) or len(paths) != 1:
            raise RuntimeHTTPError(422, "ITK instruction attachment is required")
        try:
            raw = Path(str(paths[0])).read_bytes()
            instruction = self.instruction_pb2.Instruction()
            instruction.ParseFromString(raw)
        except Exception as exc:
            raise RuntimeHTTPError(422, "ITK instruction is invalid") from exc
        run_id = f"run_itk_{uuid.uuid4().hex}"
        run = {
            "id": run_id,
            "thread_id": payload["thread_id"],
            "status": "queued",
            "updated_at": _now(),
        }
        self.runs[run_id] = run
        self.events[run_id] = []
        self.instructions[run_id] = instruction
        self.changes[run_id] = asyncio.Event()
        return run

    async def inject(self, *, run_id: str, command_id: str, content: str) -> dict[str, Any]:
        raise RuntimeHTTPError(409, "ITK task does not accept follow-up messages")

    async def get_run(self, run_id: str) -> dict[str, Any]:
        return self.runs[run_id]

    async def list_events(self, run_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        return [event for event in self.events[run_id] if int(event["seq"]) > after]

    async def get_thread_snapshot(self, thread_id: str) -> dict[str, Any]:
        run = next(item for item in self.runs.values() if item["thread_id"] == thread_id)
        run_id = str(run["id"])
        events = self.events[run_id]
        return {
            "runs": [run],
            "events": events,
            "event_high_watermarks": {
                run_id: max((int(event["seq"]) for event in events), default=0)
            },
        }

    async def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        return self.artifacts[artifact_id]

    async def open_artifact_content(self, artifact_id: str) -> httpx.Response:
        raise RuntimeHTTPError(404, f"artifact not found: {artifact_id}")

    async def cancel(self, *, run_id: str, command_id: str) -> dict[str, Any]:
        run = self.runs[run_id]
        if run["status"] in {"completed", "failed", "canceled"}:
            raise RuntimeHTTPError(409, "task is terminal")
        run["status"] = "canceled"
        run["updated_at"] = _now()
        self._append(run_id, "run.canceled", {"command_id": command_id})
        task = self.execution.get(run_id)
        if task is not None:
            task.cancel()
        return {"canceled": True}

    async def stream_events(
        self, *, run_id: str, after: int
    ) -> AsyncGenerator[dict[str, Any], None]:
        if run_id not in self.execution:
            self.execution[run_id] = asyncio.create_task(
                self._execute(run_id), name=f"itk-run-{run_id}"
            )
        while True:
            for event in await self.list_events(run_id, after=after):
                after = int(event["seq"])
                yield event
            if self.runs[run_id]["status"] in {"completed", "failed", "canceled"}:
                return
            changed = self.changes[run_id]
            changed.clear()
            if any(int(event["seq"]) > after for event in self.events[run_id]):
                continue
            await changed.wait()

    async def _execute(self, run_id: str) -> None:
        run = self.runs[run_id]
        try:
            run["status"] = "running"
            run["updated_at"] = _now()
            self._append(run_id, "run.started", {})
            instruction = self.instructions[run_id]
            text = "\n".join(await self._handle_instruction(instruction))
            if self._should_hold(instruction):
                artifact_id = f"artifact_itk_{uuid.uuid4().hex}"
                content = f"{text}\ntask-finished"
                self.artifacts[artifact_id] = {
                    "id": artifact_id,
                    "title": "itk-result",
                    "content_type": "text/plain",
                    "bytes": len(content.encode()),
                    "storage_kind": "inline_text",
                    "content": content,
                    "sha256": None,
                    "created_at": _now(),
                }
                self._append(run_id, "artifact.created", {"artifact_id": artifact_id})
                await asyncio.Event().wait()
            run["status"] = "completed"
            run["updated_at"] = _now()
            self._append(run_id, "run.completed", {"final_text": text})
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.exception("ITK Runtime execution failed", extra={"run_id": run_id})
            run["status"] = "failed"
            run["updated_at"] = _now()
            self._append(run_id, "run.failed", {"error": str(exc)[:2048]})

    def _should_hold(self, instruction: Any) -> bool:
        if instruction.HasField("return_response"):
            return bool(instruction.return_response.hold_task)
        if instruction.HasField("steps"):
            return any(self._should_hold(item) for item in instruction.steps.instructions)
        return False

    async def _handle_instruction(self, instruction: Any) -> list[str]:
        if instruction.HasField("return_response"):
            return [str(instruction.return_response.response)]
        if instruction.HasField("call_agent"):
            return await self._call_agent(instruction.call_agent)
        if instruction.HasField("steps"):
            result: list[str] = []
            for item in instruction.steps.instructions:
                result.extend(await self._handle_instruction(item))
            return result
        raise ValueError("unknown ITK instruction")

    async def _call_agent(self, call: Any) -> list[str]:
        if str(call.transport).upper() != "JSONRPC":
            raise ValueError(f"unsupported ITK transport: {call.transport}")
        message = Message(
            message_id=f"itk-{uuid.uuid4().hex}",
            role=ROLE_USER,
            parts=[
                Part(
                    raw=call.instruction.SerializeToString(),
                    media_type=_PROTO_MEDIA_TYPE,
                    filename="instruction.bin",
                )
            ],
        )
        request = SendMessageRequest(message=message)
        actual = str(call.agent_card_uri).rstrip("/")
        parsed = urlsplit(actual)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("ITK target must be a loopback HTTP reference agent")

        if call.HasField("push_notification"):
            config = ClientConfig(
                streaming=bool(call.streaming),
                push_notification_config=TaskPushNotificationConfig(
                    url=f"{str(call.push_notification.url).rstrip('/')}/notifications",
                    token="itk-token",
                ),
                supported_protocol_bindings=["JSONRPC"],
            )
            async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=120)) as client:
                config.httpx_client = client
                direct = await create_client(actual, client_config=config)
                return await self._consume(direct, request, resubscribe=False)

        facade = f"https://agent-{parsed.port or 80}.itk.invalid"
        connection = await connect_a2a_agent(
            facade,
            bearer_token=None,
            streaming=bool(call.streaming) or call.HasField("resubscribe"),
            transport_factory=lambda _url: LoopbackAgentTransport(actual, facade),
        )
        try:
            return await self._consume(
                connection.client,
                request,
                resubscribe=call.HasField("resubscribe"),
            )
        finally:
            await connection.close()

    async def _consume(
        self, client: Client, request: SendMessageRequest, *, resubscribe: bool
    ) -> list[str]:
        stream = client.send_message(request)
        if not resubscribe:
            result: list[str] = []
            async for event in stream:
                result.extend(_event_texts(event))
            return result

        task_id = ""
        async for event in stream:
            task_id = _event_task_id(event)
            if task_id:
                break
        await stream.aclose()
        if not task_id:
            raise RuntimeError("ITK resubscribe did not return a task ID")
        result = []
        subscription = client.subscribe(SubscribeToTaskRequest(id=task_id))
        async for event in subscription:
            texts = _event_texts(event)
            result.extend(text.replace("task-finished", "").strip() for text in texts)
            if any("task-finished" in text for text in texts):
                break
        await subscription.aclose()
        await client.cancel_task(CancelTaskRequest(id=task_id))
        return [item for item in result if item]


async def _peer_token(db_path: Path) -> str:
    store = await A2AGatewayStore.open(db_path)
    try:
        peer, token = await store.create_peer(
            name="Official A2A ITK",
            tenant="itk",
            scopes=["tasks.create", "tasks.read", "tasks.cancel", "push.manage"],
            runtime_model="local:itk:model",
            runtime_workspace_path=None,
            permission_mode="ask",
            push_origins=["https://tck-webhook.invalid"],
            expires_at=None,
        )
        assert peer["tenant"] == "itk"
        return token
    finally:
        await store.close()


async def _push_sender(url: str, headers: dict[str, str], body: bytes) -> int:
    if not url.startswith(_WEBHOOK_MARKER):
        return 204
    encoded = url.removeprefix(_WEBHOOK_MARKER)
    target = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    adapted = _adapt_itk_json(body)
    forwarded_headers = {
        key: value for key, value in headers.items() if key.lower() != "content-length"
    }
    forwarded_headers["content-length"] = str(len(adapted))
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(5, connect=2), follow_redirects=False
    ) as client:
        response = await client.post(target, headers=forwarded_headers, content=adapted)
        return response.status_code


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--itk-root", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--httpPort", "--port", dest="port", type=int, required=True)
    parser.add_argument("--grpcPort", type=int)
    args = parser.parse_args(argv)

    token = asyncio.run(_peer_token(args.db))
    public_url = f"http://{args.host}:{args.port}"
    runtime = ITKRuntime(args.itk_root.resolve())
    gateway = create_gateway_app(
        GatewayConfig(
            db_path=args.db,
            runtime_base_url="http://127.0.0.1:1",
            runtime_token="itk-runtime-token",
            public_base_url=public_url,
            push_credential_key=b"k" * 32,
            requests_per_minute=10_000,
        ),
        runtime_client=runtime,
        push_sender=_push_sender,
    )
    app = ITKAdapter(TCKAdapter(gateway, token), cleanup=runtime.close)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="warning",
        access_log=False,
        loop="asyncio",
        http="h11",
        ws="none",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
