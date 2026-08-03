from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import httpx
from a2a.types.a2a_pb2 import (
    ROLE_AGENT,
    ROLE_USER,
    TASK_STATE_AUTH_REQUIRED,
    TASK_STATE_CANCELED,
    TASK_STATE_COMPLETED,
    TASK_STATE_FAILED,
    TASK_STATE_INPUT_REQUIRED,
    TASK_STATE_REJECTED,
    TASK_STATE_SUBMITTED,
    TASK_STATE_WORKING,
    Artifact,
    ListTasksRequest,
    ListTasksResponse,
    Message,
    Part,
    SendMessageRequest,
    StreamResponse,
    Task,
    TaskArtifactUpdateEvent,
    TaskPushNotificationConfig,
    TaskStatus,
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
from google.protobuf.struct_pb2 import Value
from google.protobuf.timestamp_pb2 import Timestamp

from .input_files import (
    InboundFileError,
    InboundFileStore,
    InboundFileTemporaryError,
    InboundFileUnsupportedMediaError,
    has_file_parts,
    validate_file_part,
)
from .push import validate_push_url
from .runtime_client import RuntimeHTTPError
from .secrets import PushSecretBox
from .store import (
    A2AGatewayStore,
    A2AMessageConflictError,
    A2APushConfigConflictError,
)

_SUPPORTED_OUTPUT_MODES = ("text/plain", "application/json")
_SETTLED_STATES = {
    TASK_STATE_COMPLETED,
    TASK_STATE_FAILED,
    TASK_STATE_CANCELED,
    TASK_STATE_REJECTED,
    TASK_STATE_INPUT_REQUIRED,
    TASK_STATE_AUTH_REQUIRED,
}
_TERMINAL_STATES = {
    TASK_STATE_COMPLETED,
    TASK_STATE_FAILED,
    TASK_STATE_CANCELED,
    TASK_STATE_REJECTED,
}
_EXTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_AUTH_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9!#$%&'*+.^_`|~-]{0,31}$")


def _stable_id(prefix: str, *values: str) -> str:
    digest = hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _timestamp(value: str | None) -> Timestamp:
    parsed = datetime.now(UTC)
    if value:
        try:
            candidate = datetime.fromisoformat(value)
            if candidate.tzinfo is not None:
                parsed = candidate.astimezone(UTC)
        except ValueError:
            pass
    result = Timestamp()
    result.FromDatetime(parsed)
    return result


def _status_state(runtime_status: str) -> int:
    return {
        "queued": TASK_STATE_SUBMITTED,
        "running": TASK_STATE_WORKING,
        "waiting_input": TASK_STATE_INPUT_REQUIRED,
        "waiting_permission": TASK_STATE_AUTH_REQUIRED,
        "completed": TASK_STATE_COMPLETED,
        "failed": TASK_STATE_FAILED,
        "cleanup_required": TASK_STATE_FAILED,
        "canceled": TASK_STATE_CANCELED,
    }.get(runtime_status, TASK_STATE_SUBMITTED)


def _status_message(task_id: str, context_id: str, state: int, text: str) -> Message:
    return Message(
        message_id=_stable_id("message_status", task_id, str(state)),
        task_id=task_id,
        context_id=context_id,
        role=ROLE_AGENT,
        parts=[Part(text=text, media_type="text/plain")],
    )


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


class GatewayService:
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
        projected = await self.project_task(
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
        first, after = await self._snapshot_projection(task, peer)
        if reject_terminal and first.status.state in _TERMINAL_STATES:
            raise UnsupportedOperationError(message="a terminal task cannot be subscribed")
        yield first
        if first.status.state in _TERMINAL_STATES:
            return
        run_id = str(task["runtime_run_id"])
        try:
            async for event in self.runtime.stream_events(run_id=run_id, after=after):
                for projected in await self._project_stream_event(task, peer, event):
                    yield projected
                    if (
                        isinstance(projected, TaskStatusUpdateEvent)
                        and projected.status.state in _TERMINAL_STATES
                    ):
                        return
        except (RuntimeHTTPError, httpx.TransportError) as exc:
            raise InternalError(message="Runtime task stream was interrupted") from exc

    async def _snapshot_projection(
        self, task: dict[str, Any], peer: dict[str, Any]
    ) -> tuple[Task, int]:
        run_id = task.get("runtime_run_id")
        if not isinstance(run_id, str) or not run_id:
            return (
                await self.project_task(task, peer, history_length=None, include_artifacts=True),
                0,
            )
        try:
            snapshot = await self.runtime.get_thread_snapshot(str(task["runtime_thread_id"]))
        except (RuntimeHTTPError, httpx.TransportError) as exc:
            raise InternalError(message="Runtime task snapshot is unavailable") from exc
        high_watermarks = snapshot.get("event_high_watermarks", {})
        after = int(high_watermarks.get(run_id, 0)) if isinstance(high_watermarks, dict) else 0
        snapshot_runs = snapshot.get("runs")
        snapshot_run = (
            next(
                (
                    item
                    for item in snapshot_runs
                    if isinstance(item, dict) and item.get("id") == run_id
                ),
                None,
            )
            if isinstance(snapshot_runs, list)
            else None
        )
        snapshot_events = snapshot.get("events")
        run_events = (
            [
                event
                for event in snapshot_events
                if isinstance(event, dict) and event.get("run_id", run_id) == run_id
            ]
            if isinstance(snapshot_events, list)
            else []
        )
        if snapshot_run is None:
            raise InternalError(message="Runtime task snapshot is inconsistent")
        return (
            await self.project_task(
                task,
                peer,
                history_length=None,
                include_artifacts=True,
                runtime_run=snapshot_run,
                runtime_events=run_events,
            ),
            after,
        )

    async def get_task(
        self,
        *,
        task_id: str,
        peer: dict[str, Any],
        history_length: int | None,
    ) -> Task:
        task = await self._owned_task(peer, task_id)
        return await self.project_task(
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
            task = await self.project_task(
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
        projected = await self.project_task(task, peer, history_length=None, include_artifacts=True)
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
        return await self.project_task(refreshed, peer, history_length=None, include_artifacts=True)

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
        config_id = params.id or default_id or _new_id("push")
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
        snapshot, start_after = await self._snapshot_projection(task, peer)
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

    async def _ensure_task_admitted(
        self,
        task: dict[str, Any],
        message: dict[str, Any],
        content: str,
        attachment_paths: list[str],
        peer: dict[str, Any],
    ) -> dict[str, Any]:
        if task["admission_status"] in {"accepted", "rejected"}:
            return task
        capabilities = ["agent.run", "agent.stream", "hitl"]
        if peer.get("runtime_workspace_path"):
            capabilities.append("workspace.files")
        if attachment_paths:
            capabilities.append("attachments")
        payload = {
            "command_id": task["create_command_id"],
            "client_message_id": task["create_client_message_id"],
            "thread_id": task["runtime_thread_id"],
            "assistant_message_id": _stable_id("a2a_assistant", str(task["id"])),
            "protocol_version": 1,
            "required_capabilities": sorted(capabilities),
            "goal": content,
            "user_input": content,
            "workspace_path": peer.get("runtime_workspace_path"),
            "attachment_paths": attachment_paths,
            "model": peer["runtime_model"],
            "permission_mode": peer["permission_mode"],
            "history": [],
            "metadata": {
                "a2a_task_id": task["id"],
                "a2a_context_id": task["context_id"],
                "a2a_peer_id": peer["id"],
                "a2a_message_id": message["message_id"],
            },
        }
        try:
            run = await self.runtime.create_run(payload)
        except RuntimeHTTPError as exc:
            if exc.retryable:
                raise InternalError(message="Runtime is temporarily unavailable") from exc
            reason = "Runtime rejected the task"
            await self.store.reject_task_admission(
                peer_id=str(peer["id"]), task_id=str(task["id"]), reason=reason
            )
            await self.store.reject_message_delivery(
                peer_id=str(peer["id"]),
                message_id=str(message["message_id"]),
                reason=reason,
            )
            return await self._owned_task(peer, str(task["id"]))
        except httpx.TransportError as exc:
            raise InternalError(message="Runtime is temporarily unavailable") from exc
        run_id = run.get("id") if isinstance(run, dict) else None
        if not isinstance(run_id, str) or not run_id:
            raise InternalError(message="Runtime returned an invalid Run")
        task = await self.store.settle_task_admission(
            peer_id=str(peer["id"]), task_id=str(task["id"]), runtime_run_id=run_id
        )
        await self.store.settle_message_delivery(
            peer_id=str(peer["id"]), message_id=str(message["message_id"])
        )
        return task

    async def _ensure_followup_delivered(
        self,
        task: dict[str, Any],
        message: dict[str, Any],
        content: str,
        peer: dict[str, Any],
    ) -> None:
        if message["delivery_status"] == "accepted":
            return
        if message["delivery_status"] == "rejected" or task["admission_status"] == "rejected":
            raise UnsupportedOperationError(message="the task cannot accept another message")
        if task["admission_status"] != "accepted" or not task["runtime_run_id"]:
            raise InternalError(message="the task is still being admitted")
        try:
            run = await self.runtime.get_run(str(task["runtime_run_id"]))
            if _status_state(str(run.get("status"))) in _TERMINAL_STATES:
                await self.store.reject_message_delivery(
                    peer_id=str(peer["id"]),
                    message_id=str(message["message_id"]),
                    reason="task is terminal",
                )
                raise UnsupportedOperationError(message="a terminal task cannot be restarted")
            receipt = await self.runtime.inject(
                run_id=str(task["runtime_run_id"]),
                command_id=str(message["runtime_command_id"]),
                content=content,
            )
        except RuntimeHTTPError as exc:
            if exc.status_code == 409:
                await self.store.reject_message_delivery(
                    peer_id=str(peer["id"]),
                    message_id=str(message["message_id"]),
                    reason="task is terminal",
                )
                raise UnsupportedOperationError(
                    message="a terminal task cannot be restarted"
                ) from exc
            if exc.retryable:
                raise InternalError(message="Runtime is temporarily unavailable") from exc
            raise InternalError(message="Runtime rejected the follow-up") from exc
        except httpx.TransportError as exc:
            raise InternalError(message="Runtime is temporarily unavailable") from exc
        instruction_id = receipt.get("instruction_id") if isinstance(receipt, dict) else None
        if not isinstance(instruction_id, str) or not instruction_id:
            raise InternalError(message="Runtime returned an invalid instruction receipt")
        await self.store.settle_message_delivery(
            peer_id=str(peer["id"]),
            message_id=str(message["message_id"]),
            runtime_instruction_id=instruction_id,
        )

    async def _owned_task(self, peer: dict[str, Any], task_id: str) -> dict[str, Any]:
        task = await self.store.get_task(
            peer_id=str(peer["id"]), tenant=str(peer["tenant"]), task_id=task_id
        )
        if task is None:
            raise TaskNotFoundError
        return task

    async def project_task(
        self,
        task: dict[str, Any],
        peer: dict[str, Any],
        *,
        history_length: int | None,
        include_artifacts: bool,
        runtime_run: dict[str, Any] | None = None,
        runtime_events: list[dict[str, Any]] | None = None,
    ) -> Task:
        events: list[dict[str, Any]] = []
        status_message: Message | None = None
        if task["admission_status"] == "rejected":
            state = TASK_STATE_REJECTED
            status_message = _status_message(
                str(task["id"]),
                str(task["context_id"]),
                state,
                str(task.get("rejection_reason") or "Task rejected"),
            )
            updated_at = str(task["updated_at"])
        elif task["admission_status"] != "accepted" or not task["runtime_run_id"]:
            state = TASK_STATE_SUBMITTED
            updated_at = str(task["updated_at"])
        else:
            try:
                run = runtime_run or await self.runtime.get_run(str(task["runtime_run_id"]))
                state = _status_state(str(run.get("status")))
                updated_at = str(run.get("updated_at") or task["updated_at"])
                if include_artifacts or state in {
                    TASK_STATE_COMPLETED,
                    TASK_STATE_FAILED,
                    TASK_STATE_CANCELED,
                    TASK_STATE_INPUT_REQUIRED,
                    TASK_STATE_AUTH_REQUIRED,
                }:
                    events = (
                        runtime_events
                        if runtime_events is not None
                        else await self.runtime.list_events(str(task["runtime_run_id"]))
                    )
            except (RuntimeHTTPError, httpx.TransportError) as exc:
                raise InternalError(message="Runtime task state is unavailable") from exc
            message_text = self._state_message_text(state, events)
            if message_text:
                status_message = _status_message(
                    str(task["id"]), str(task["context_id"]), state, message_text
                )

        messages = await self.store.list_task_messages(
            peer_id=str(peer["id"]), task_id=str(task["id"])
        )
        history = [
            json_format.ParseDict(item["message"], Message())
            for item in messages
            if item["delivery_status"] == "accepted"
        ]
        if history_length is not None:
            history = history[-history_length:] if history_length else []
        else:
            history = history[-100:]

        artifacts = (
            await self._project_runtime_artifacts(task, peer, events) if include_artifacts else []
        )
        if include_artifacts and state == TASK_STATE_COMPLETED:
            final_text = self._final_text(events)
            if final_text:
                artifacts.append(self._result_artifact(task, final_text))
        status = TaskStatus(state=state, timestamp=_timestamp(updated_at))
        if status_message is not None:
            status.message.CopyFrom(status_message)
        return Task(
            id=str(task["id"]),
            context_id=str(task["context_id"]),
            status=status,
            artifacts=artifacts,
            history=history,
        )

    async def wait_until_settled(
        self,
        task: dict[str, Any],
        peer: dict[str, Any],
        *,
        history_length: int | None,
    ) -> Task:
        run_id = task.get("runtime_run_id")
        if not isinstance(run_id, str) or not run_id:
            return await self.project_task(
                task, peer, history_length=history_length, include_artifacts=True
            )
        try:
            async for _event in self.runtime.stream_events(run_id=run_id, after=0):
                pass
        except (RuntimeHTTPError, httpx.TransportError) as exc:
            raise InternalError(message="Runtime task stream was interrupted") from exc
        refreshed = await self._owned_task(peer, str(task["id"]))
        return await self.project_task(
            refreshed, peer, history_length=history_length, include_artifacts=True
        )

    @staticmethod
    def _final_text(events: list[dict[str, Any]]) -> str:
        for event in reversed(events):
            if event.get("event_type") != "run.completed":
                continue
            payload = event.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("final_text"), str):
                return str(payload["final_text"])
        return ""

    @staticmethod
    def _state_message_text(state: int, events: list[dict[str, Any]]) -> str:
        if state == TASK_STATE_INPUT_REQUIRED:
            return "Task requires additional input."
        if state == TASK_STATE_AUTH_REQUIRED:
            return "Task requires an authorization decision."
        if state == TASK_STATE_FAILED:
            for event in reversed(events):
                if event.get("event_type") not in {"run.failed", "run.cleanup_required"}:
                    continue
                payload = event.get("payload")
                if isinstance(payload, dict):
                    error = payload.get("error")
                    if isinstance(error, str) and error:
                        return error[:2048]
            return "Task failed."
        if state == TASK_STATE_CANCELED:
            return "Task was canceled."
        return ""

    @staticmethod
    def _result_artifact(task: dict[str, Any], final_text: str) -> Artifact:
        if task.get("output_mode") == "application/json":
            data = Value()
            json_format.ParseDict({"text": final_text}, data)
            part = Part(data=data, media_type="application/json")
        else:
            part = Part(text=final_text, media_type="text/plain")
        return Artifact(
            artifact_id=_stable_id("artifact_result", str(task["id"])),
            name="result",
            description="Final SheJane task result.",
            parts=[part],
        )

    async def _project_stream_event(
        self, task: dict[str, Any], peer: dict[str, Any], event: dict[str, Any]
    ) -> list[TaskStatusUpdateEvent | TaskArtifactUpdateEvent]:
        event_type = str(event.get("event_type") or "")
        if event_type == "artifact.created":
            artifacts = await self._project_runtime_artifacts(task, peer, [event])
            return [
                TaskArtifactUpdateEvent(
                    task_id=str(task["id"]),
                    context_id=str(task["context_id"]),
                    artifact=artifact,
                    append=False,
                    last_chunk=True,
                )
                for artifact in artifacts
            ]
        state = {
            "run.started": TASK_STATE_WORKING,
            "run.resumed": TASK_STATE_WORKING,
            "run.completed": TASK_STATE_COMPLETED,
            "run.failed": TASK_STATE_FAILED,
            "run.cleanup_required": TASK_STATE_FAILED,
            "run.canceled": TASK_STATE_CANCELED,
        }.get(event_type)
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if event_type == "run.waiting":
            waiting = str(payload.get("status") or payload.get("kind") or "")
            state = (
                TASK_STATE_AUTH_REQUIRED
                if "permission" in waiting or "auth" in waiting
                else TASK_STATE_INPUT_REQUIRED
            )
        if state is None:
            return []

        updates: list[TaskStatusUpdateEvent | TaskArtifactUpdateEvent] = []
        if state == TASK_STATE_COMPLETED:
            final_text = payload.get("final_text")
            if isinstance(final_text, str) and final_text:
                updates.append(
                    TaskArtifactUpdateEvent(
                        task_id=str(task["id"]),
                        context_id=str(task["context_id"]),
                        artifact=self._result_artifact(task, final_text),
                        append=False,
                        last_chunk=True,
                    )
                )
        status = TaskStatus(state=state, timestamp=_timestamp(event.get("created_at")))
        message_text = self._state_message_text(state, [event])
        if message_text:
            status.message.CopyFrom(
                _status_message(str(task["id"]), str(task["context_id"]), state, message_text)
            )
        updates.append(
            TaskStatusUpdateEvent(
                task_id=str(task["id"]),
                context_id=str(task["context_id"]),
                status=status,
            )
        )
        return updates

    async def _project_runtime_artifacts(
        self,
        task: dict[str, Any],
        peer: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> list[Artifact]:
        existing = await self.store.list_task_artifacts(
            peer_id=str(peer["id"]), task_id=str(task["id"])
        )
        by_runtime_id = {str(item["runtime_artifact_id"]): item for item in existing}
        for event in events:
            if event.get("event_type") != "artifact.created":
                continue
            payload = event.get("payload")
            runtime_artifact_id = payload.get("artifact_id") if isinstance(payload, dict) else None
            if not isinstance(runtime_artifact_id, str) or not runtime_artifact_id:
                continue
            if runtime_artifact_id in by_runtime_id:
                continue
            try:
                metadata = await self.runtime.get_artifact(runtime_artifact_id)
            except (RuntimeHTTPError, httpx.TransportError) as exc:
                raise InternalError(message="Runtime artifact metadata is unavailable") from exc
            title = metadata.get("title")
            media_type = metadata.get("content_type")
            size_bytes = metadata.get("bytes")
            storage_kind = metadata.get("storage_kind")
            if (
                not isinstance(title, str)
                or not title
                or not isinstance(media_type, str)
                or not media_type
                or not isinstance(size_bytes, int)
                or isinstance(size_bytes, bool)
                or size_bytes < 0
                or storage_kind not in {"inline_text", "blob"}
            ):
                raise InternalError(message="Runtime returned invalid artifact metadata")
            content = metadata.get("content") if storage_kind == "inline_text" else None
            if content is not None and not isinstance(content, str):
                raise InternalError(message="Runtime returned invalid artifact content")
            sha256 = metadata.get("sha256")
            if sha256 is not None and not isinstance(sha256, str):
                sha256 = None
            record = await self.store.register_artifact(
                artifact_id=_stable_id(
                    "artifact", str(peer["id"]), str(task["id"]), runtime_artifact_id
                ),
                peer_id=str(peer["id"]),
                tenant=str(peer["tenant"]),
                task_id=str(task["id"]),
                runtime_artifact_id=runtime_artifact_id,
                title=title[:512],
                media_type=media_type[:255],
                size_bytes=size_bytes,
                sha256=sha256,
                storage_kind=str(storage_kind),
                inline_content=content,
                created_at=str(
                    metadata.get("created_at")
                    or event.get("created_at")
                    or datetime.now(UTC).isoformat()
                ),
            )
            by_runtime_id[runtime_artifact_id] = record
        return [self._a2a_artifact(item) for item in by_runtime_id.values()]

    def _a2a_artifact(self, record: dict[str, Any]) -> Artifact:
        media_type = str(record["media_type"])
        content = record.get("inline_content")
        if record["storage_kind"] == "inline_text" and isinstance(content, str):
            if media_type == "application/json":
                try:
                    decoded = json.loads(content)
                except json.JSONDecodeError:
                    part = Part(raw=content.encode(), media_type=media_type)
                else:
                    data = Value()
                    json_format.ParseDict(decoded, data)
                    part = Part(data=data, media_type=media_type)
            elif media_type.startswith("text/"):
                part = Part(text=content, media_type=media_type)
            else:
                part = Part(raw=content.encode(), media_type=media_type)
        else:
            expires = int(datetime.now(UTC).timestamp()) + 900
            signature = self.push_secret_box.sign_artifact(
                peer_id=str(record["peer_id"]),
                artifact_id=str(record["id"]),
                expires=expires,
            )
            part = Part(
                url=(
                    f"{self.public_base_url}/a2a/artifacts/{record['id']}"
                    f"?expires={expires}&signature={signature}"
                ),
                filename=str(record["title"]),
                media_type=media_type,
            )
        return Artifact(
            artifact_id=str(record["id"]),
            name=str(record["title"]),
            parts=[part],
        )
