"""Projection from Runtime-owned Run state into A2A Task protocol objects."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import httpx
from a2a.types.a2a_pb2 import (
    ROLE_AGENT,
    TASK_STATE_AUTH_REQUIRED,
    TASK_STATE_CANCELED,
    TASK_STATE_COMPLETED,
    TASK_STATE_FAILED,
    TASK_STATE_INPUT_REQUIRED,
    TASK_STATE_REJECTED,
    TASK_STATE_SUBMITTED,
    TASK_STATE_WORKING,
    Artifact,
    Message,
    Part,
    Task,
    TaskArtifactUpdateEvent,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from a2a.utils.errors import InternalError
from google.protobuf import json_format
from google.protobuf.struct_pb2 import Value
from google.protobuf.timestamp_pb2 import Timestamp

from .runtime_client import RuntimeHTTPError
from .secrets import PushSecretBox
from .store import A2AGatewayStore

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


def _stable_id(prefix: str, *values: str) -> str:
    digest = hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


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


class TaskProjection:
    def __init__(
        self,
        store: A2AGatewayStore,
        runtime: Any,
        *,
        public_base_url: str,
        push_secret_box: PushSecretBox,
    ) -> None:
        self.store = store
        self.runtime = runtime
        self.public_base_url = public_base_url
        self.push_secret_box = push_secret_box

    async def snapshot(self, task: dict[str, Any], peer: dict[str, Any]) -> tuple[Task, int]:
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

    async def project_stream_event(
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
