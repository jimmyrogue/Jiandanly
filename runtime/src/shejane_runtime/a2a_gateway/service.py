from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from a2a.types.a2a_pb2 import (
    ROLE_USER,
    ListTasksRequest,
    ListTasksResponse,
    Message,
    SendMessageRequest,
    StreamResponse,
    Task,
    TaskArtifactUpdateEvent,
    TaskStatusUpdateEvent,
)
from a2a.utils.errors import (
    ContentTypeNotSupportedError,
    InternalError,
    InvalidParamsError,
    TaskNotCancelableError,
    TaskNotFoundError,
    UnsupportedOperationError,
)
from google.protobuf import json_format

from .delivery import TaskDeliveryMixin
from .input_files import (
    InboundFileError,
    InboundFileStore,
    InboundFileTemporaryError,
    InboundFileUnsupportedMediaError,
    has_file_parts,
    validate_file_part,
)
from .projection import (
    _SETTLED_STATES,
    _TERMINAL_STATES,
    TaskProjection,
    _stable_id,
)
from .push import PushConfigMixin
from .runtime_client import RuntimeHTTPError
from .secrets import PushSecretBox
from .store import (
    A2AGatewayStore,
    A2AMessageConflictError,
)

_SUPPORTED_OUTPUT_MODES = ("text/plain", "application/json")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _message_content(message: Message) -> str:
    if message.role != ROLE_USER:
        raise InvalidParamsError(message="inbound messages must use ROLE_USER")
    if not message.message_id or len(message.message_id) > 128:
        raise InvalidParamsError(message="messageId must contain 1 to 128 characters")
    if not 1 <= len(message.parts) <= 16:
        raise InvalidParamsError(message="message parts must contain 1 to 16 entries")
    if len(message.reference_task_ids) > 16:
        raise InvalidParamsError(message="referenceTaskIds exceeds 16 entries")
    if message.extensions:
        raise UnsupportedOperationError(message="message extensions are not supported")

    rendered: list[str] = []
    total_bytes = 0
    for part in message.parts:
        content = part.WhichOneof("content")
        if content == "text":
            if part.media_type and part.media_type != "text/plain":
                raise ContentTypeNotSupportedError(message="text parts must use text/plain")
            value = part.text
        elif content == "data":
            if part.media_type and part.media_type != "application/json":
                raise ContentTypeNotSupportedError(message="data parts must use application/json")
            value = json.dumps(
                json_format.MessageToDict(part.data),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        elif content in {"raw", "url"}:
            filename, media_type = validate_file_part(part, index=len(rendered))
            value = f"[Attachment: {filename} ({media_type})]"
        else:
            raise InvalidParamsError(message="each message part must set exactly one content field")
        encoded = value.encode("utf-8")
        total_bytes += len(encoded)
        if total_bytes > 131_072:
            raise InvalidParamsError(message="message content exceeds 128 KiB")
        rendered.append(value)
    content = "\n\n".join(rendered).strip()
    if not content:
        raise InvalidParamsError(message="message content must not be empty")
    if message.ByteSize() > 512 * 1024:
        raise InvalidParamsError(message="message exceeds the persistence limit")
    return content


def _output_mode(params: SendMessageRequest) -> str:
    accepted = list(params.configuration.accepted_output_modes)
    if not accepted or "text/plain" in accepted:
        return "text/plain"
    if "application/json" in accepted:
        return "application/json"
    raise ContentTypeNotSupportedError(message="no accepted output mode is supported")


def _history_length(params: SendMessageRequest) -> int | None:
    if not params.configuration.HasField("history_length"):
        return None
    value = int(params.configuration.history_length)
    if not 0 <= value <= 100:
        raise InvalidParamsError(message="historyLength must be between 0 and 100")
    return value


class GatewayService(PushConfigMixin, TaskDeliveryMixin):
    def __init__(
        self,
        store: A2AGatewayStore,
        runtime: Any,
        *,
        public_base_url: str,
        push_secret_box: PushSecretBox,
        input_files: InboundFileStore,
    ) -> None:
        self.store = store
        self.runtime = runtime
        self.public_base_url = public_base_url.rstrip("/")
        self.push_secret_box = push_secret_box
        self.input_files = input_files
        self.projection = TaskProjection(
            store,
            runtime,
            public_base_url=self.public_base_url,
            push_secret_box=push_secret_box,
        )
        self.wake_push: Any = lambda: None

    async def accept_message(
        self,
        params: SendMessageRequest,
        peer: dict[str, Any],
        *,
        wait_for_completion: bool = True,
    ) -> Task:
        if params.tenant and params.tenant != peer["tenant"]:
            raise InvalidParamsError(message="tenant does not match the authenticated peer")
        if "tasks.create" not in peer["scopes"]:
            raise UnsupportedOperationError(message="A2A peer scope is required: tasks.create")
        try:
            content = _message_content(params.message)
        except InboundFileUnsupportedMediaError as exc:
            raise ContentTypeNotSupportedError(message=str(exc)) from exc
        except InboundFileError as exc:
            raise InvalidParamsError(message=str(exc)) from exc
        if params.message.task_id and has_file_parts(params.message):
            raise ContentTypeNotSupportedError(
                message="attachments on follow-up messages are not supported"
            )
        output_mode = _output_mode(params)
        history_length = _history_length(params)
        fingerprint = (
            "sha256:"
            + hashlib.sha256(params.message.SerializeToString(deterministic=True)).hexdigest()
        )
        message_json = json_format.MessageToDict(params.message)
        peer_id = str(peer["id"])
        try:
            attachment_paths = await self.input_files.materialize(
                peer_id=peer_id, message=params.message
            )
        except InboundFileUnsupportedMediaError as exc:
            raise ContentTypeNotSupportedError(message=str(exc)) from exc
        except InboundFileError as exc:
            raise InvalidParamsError(message=str(exc)) from exc
        except InboundFileTemporaryError as exc:
            raise InternalError(message="attachment import is temporarily unavailable") from exc
        stable = _stable_id("a2a", peer_id, params.message.message_id)
        try:
            task, stored_message, _created = await self.store.prepare_message(
                peer_id=peer_id,
                tenant=str(peer["tenant"]),
                message_id=params.message.message_id,
                task_id=params.message.task_id or None,
                context_id=params.message.context_id or None,
                reference_task_ids=list(params.message.reference_task_ids),
                request_fingerprint=fingerprint,
                message=message_json,
                new_task_id=_new_id("task"),
                new_context_id=_new_id("context"),
                runtime_thread_id=_stable_id("a2a_thread", stable),
                runtime_command_id=_stable_id("a2a_command", stable),
                runtime_client_message_id=_stable_id("a2a_message", stable),
                output_mode=output_mode,
            )
        except A2AMessageConflictError as exc:
            raise InvalidParamsError(message=str(exc)) from exc
        except KeyError as exc:
            raise TaskNotFoundError from exc
        except ValueError as exc:
            raise InvalidParamsError(message=str(exc)) from exc

        initial = stored_message["runtime_command_id"] == task["create_command_id"]
        if initial:
            task = await self._ensure_task_admitted(
                task, stored_message, content, attachment_paths, peer
            )
        else:
            await self._ensure_followup_delivered(task, stored_message, content, peer)
            task = await self._owned_task(peer, str(task["id"]))
        if params.configuration.HasField("task_push_notification_config"):
            if "push.manage" not in peer["scopes"]:
                raise UnsupportedOperationError(message="A2A peer scope is required: push.manage")
            await self.create_push_config(
                params.configuration.task_push_notification_config,
                peer,
                task_id=str(task["id"]),
                default_id=_stable_id("push", str(peer["id"]), params.message.message_id),
            )
        projected = await self.projection.project_task(
            task,
            peer,
            history_length=history_length,
            include_artifacts=True,
        )
        if (
            wait_for_completion
            and not params.configuration.return_immediately
            and projected.status.state not in _SETTLED_STATES
        ):
            projected = await self.wait_until_settled(
                task,
                peer,
                history_length=history_length,
            )
        return projected

    async def stream_message(
        self, params: SendMessageRequest, peer: dict[str, Any]
    ) -> AsyncGenerator[Task | TaskStatusUpdateEvent | TaskArtifactUpdateEvent, None]:
        task = await self.accept_message(
            params,
            peer,
            wait_for_completion=False,
        )
        async for event in self.stream_task(
            task_id=task.id,
            peer=peer,
            reject_terminal=False,
        ):
            yield event

    async def stream_task(
        self,
        *,
        task_id: str,
        peer: dict[str, Any],
        reject_terminal: bool,
    ) -> AsyncGenerator[Task | TaskStatusUpdateEvent | TaskArtifactUpdateEvent, None]:
        task = await self._owned_task(peer, task_id)
        first, after = await self.projection.snapshot(task, peer)
        if reject_terminal and first.status.state in _TERMINAL_STATES:
            raise UnsupportedOperationError(message="a terminal task cannot be subscribed")
        yield first
        if first.status.state in _TERMINAL_STATES:
            return
        run_id = str(task["runtime_run_id"])
        try:
            async for event in self.runtime.stream_events(run_id=run_id, after=after):
                for projected in await self.projection.project_stream_event(task, peer, event):
                    yield projected
                    if (
                        isinstance(projected, TaskStatusUpdateEvent)
                        and projected.status.state in _TERMINAL_STATES
                    ):
                        return
        except (RuntimeHTTPError, httpx.TransportError) as exc:
            raise InternalError(message="Runtime task stream was interrupted") from exc

    async def get_task(
        self,
        *,
        task_id: str,
        peer: dict[str, Any],
        history_length: int | None,
    ) -> Task:
        task = await self._owned_task(peer, task_id)
        return await self.projection.project_task(
            task,
            peer,
            history_length=history_length,
            include_artifacts=True,
        )

    async def list_tasks(self, params: ListTasksRequest, peer: dict[str, Any]) -> ListTasksResponse:
        page_size = int(params.page_size) if params.HasField("page_size") else 50
        if not 1 <= page_size <= 100:
            raise InvalidParamsError(message="pageSize must be between 1 and 100")
        history_length = int(params.history_length) if params.HasField("history_length") else 0
        if not 0 <= history_length <= 100:
            raise InvalidParamsError(message="historyLength must be between 0 and 100")
        rows = await self.store.list_tasks(peer_id=str(peer["id"]), tenant=str(peer["tenant"]))
        projected: list[Task] = []
        for row in rows:
            task = await self.projection.project_task(
                row,
                peer,
                history_length=history_length,
                include_artifacts=bool(params.include_artifacts),
            )
            if params.context_id and task.context_id != params.context_id:
                continue
            if params.status and task.status.state != params.status:
                continue
            if params.HasField("status_timestamp_after"):
                task_position = (task.status.timestamp.seconds, task.status.timestamp.nanos)
                requested_position = (
                    params.status_timestamp_after.seconds,
                    params.status_timestamp_after.nanos,
                )
                if task_position <= requested_position:
                    continue
            projected.append(task)

        start = 0
        if params.page_token:
            matching = [
                index for index, task in enumerate(projected) if task.id == params.page_token
            ]
            if not matching:
                raise InvalidParamsError(message="pageToken is invalid for this peer")
            start = matching[0] + 1
        page = projected[start : start + page_size]
        has_more = start + len(page) < len(projected)
        return ListTasksResponse(
            tasks=page,
            next_page_token=page[-1].id if has_more and page else "",
            page_size=len(page),
            total_size=len(projected),
        )

    async def cancel_task(self, *, task_id: str, peer: dict[str, Any]) -> Task:
        task = await self._owned_task(peer, task_id)
        projected = await self.projection.project_task(
            task, peer, history_length=None, include_artifacts=True
        )
        if projected.status.state in _TERMINAL_STATES:
            raise TaskNotCancelableError
        run_id = task.get("runtime_run_id")
        if not isinstance(run_id, str) or not run_id:
            raise TaskNotCancelableError
        try:
            await self.runtime.cancel(
                run_id=run_id,
                command_id=_stable_id("a2a_cancel", str(peer["id"]), task_id),
            )
        except RuntimeHTTPError as exc:
            if exc.status_code in {404, 409}:
                raise TaskNotCancelableError from exc
            raise InternalError(message="Runtime cancellation failed") from exc
        except httpx.TransportError as exc:
            raise InternalError(message="Runtime cancellation failed") from exc
        refreshed = await self._owned_task(peer, task_id)
        return await self.projection.project_task(
            refreshed, peer, history_length=None, include_artifacts=True
        )

    @staticmethod
    def stream_response_dict(
        event: Task | Message | TaskStatusUpdateEvent | TaskArtifactUpdateEvent,
    ) -> dict[str, Any]:
        response = StreamResponse()
        if isinstance(event, Task):
            response.task.CopyFrom(event)
        elif isinstance(event, Message):
            response.message.CopyFrom(event)
        elif isinstance(event, TaskStatusUpdateEvent):
            response.status_update.CopyFrom(event)
        else:
            response.artifact_update.CopyFrom(event)
        return json_format.MessageToDict(response)
