"""Durable A2A message delivery into Runtime-owned Runs."""

from __future__ import annotations

from typing import Any

import httpx
from a2a.types.a2a_pb2 import Task
from a2a.utils.errors import InternalError, TaskNotFoundError, UnsupportedOperationError

from .projection import _TERMINAL_STATES, _stable_id, _status_state
from .runtime_client import RuntimeHTTPError


class TaskDeliveryMixin:
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

    async def wait_until_settled(
        self,
        task: dict[str, Any],
        peer: dict[str, Any],
        *,
        history_length: int | None,
    ) -> Task:
        run_id = task.get("runtime_run_id")
        if not isinstance(run_id, str) or not run_id:
            return await self.projection.project_task(
                task, peer, history_length=history_length, include_artifacts=True
            )
        try:
            async for _event in self.runtime.stream_events(run_id=run_id, after=0):
                pass
        except (RuntimeHTTPError, httpx.TransportError) as exc:
            raise InternalError(message="Runtime task stream was interrupted") from exc
        refreshed = await self._owned_task(peer, str(task["id"]))
        return await self.projection.project_task(
            refreshed, peer, history_length=history_length, include_artifacts=True
        )
