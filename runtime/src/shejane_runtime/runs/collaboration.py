from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any, Literal

from ..agent.child_runs import ChildRunControl
from ..agent.mailbox import AgentMailboxControl, AgentMessageKind
from ..run_configuration import _execution_policy_snapshot
from ..store.sqlite import LocalStore
from .event_stream import RunEventStream
from .stream_state import _json_object

_CHILD_TERMINAL_STATUSES = {"completed", "failed", "canceled", "cleanup_required"}


def _child_wait_satisfied(
    children: Sequence[dict[str, Any]],
    condition: Literal["all", "any"],
) -> bool:
    terminal = [child.get("status") in _CHILD_TERMINAL_STATUSES for child in children]
    return all(terminal) if condition == "all" else any(terminal)


def _collaboration_completion_summary(
    children: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    active = [
        str(child["id"])
        for child in children
        if child.get("status") not in _CHILD_TERMINAL_STATUSES
    ]
    required = [child for child in children if child.get("completion_mode") == "required"]
    required_failed = [
        str(child["id"])
        for child in required
        if child.get("status") in _CHILD_TERMINAL_STATUSES and child.get("status") != "completed"
    ]
    required_waiting = [
        str(child["id"])
        for child in required
        if child.get("status") not in _CHILD_TERMINAL_STATUSES
    ]
    best_effort_active = [
        str(child["id"])
        for child in children
        if child.get("completion_mode") == "best_effort"
        and child.get("status") not in _CHILD_TERMINAL_STATUSES
    ]

    quorum_members: dict[str, list[dict[str, Any]]] = {}
    for child in children:
        if child.get("completion_mode") != "quorum":
            continue
        quorum_members.setdefault(str(child.get("quorum_group") or ""), []).append(child)
    quorum_groups: list[dict[str, Any]] = []
    quorum_waiting: list[str] = []
    quorum_cancel: list[str] = []
    quorum_impossible = False
    quorum_satisfied = True
    for group, members in sorted(quorum_members.items()):
        requirements = {int(member.get("quorum_required") or 0) for member in members}
        required_count = next(iter(requirements)) if len(requirements) == 1 else 0
        completed = sum(member.get("status") == "completed" for member in members)
        member_active = [
            str(member["id"])
            for member in members
            if member.get("status") not in _CHILD_TERMINAL_STATUSES
        ]
        failed = sum(
            member.get("status") in _CHILD_TERMINAL_STATUSES and member.get("status") != "completed"
            for member in members
        )
        satisfied = required_count > 0 and completed >= required_count
        impossible = (
            required_count <= 0
            or len(members) < required_count
            or completed + len(member_active) < required_count
        )
        if satisfied or impossible:
            quorum_cancel.extend(member_active)
        else:
            quorum_waiting.extend(member_active)
        quorum_satisfied = quorum_satisfied and satisfied
        quorum_impossible = quorum_impossible or impossible
        quorum_groups.append(
            {
                "group": group,
                "required": required_count,
                "completed": completed,
                "active": len(member_active),
                "failed": failed,
                "satisfied": satisfied,
                "impossible": impossible,
            }
        )

    impossible = bool(required_failed) or quorum_impossible
    required_satisfied = not required_failed and not required_waiting
    satisfied = required_satisfied and quorum_satisfied and not impossible
    wait_for = [] if impossible else [*required_waiting, *quorum_waiting]
    cancel = [*best_effort_active, *quorum_cancel]
    if impossible:
        cancel = active
    return {
        "satisfied": satisfied,
        "impossible": impossible,
        "required": {
            "total": len(required),
            "completed": sum(child.get("status") == "completed" for child in required),
            "failed": required_failed,
            "active": len(required_waiting),
        },
        "quorum_groups": quorum_groups,
        "best_effort_active": len(best_effort_active),
        "wait_for": list(dict.fromkeys(wait_for)),
        "cancel": list(dict.fromkeys(cancel)),
    }


class RunCollaboration:
    def __init__(
        self,
        store: LocalStore,
        *,
        start_run: Callable[..., Awaitable[dict[str, Any]]],
        cancel_run: Callable[[str], Awaitable[bool]],
        tasks: Callable[[], dict[str, asyncio.Task[Any]]],
        slots: Callable[[], asyncio.Semaphore],
        job_wakeup: Callable[[], asyncio.Event],
        event_stream: Callable[[], RunEventStream],
    ) -> None:
        self.store = store
        self._start_run = start_run
        self._cancel_run = cancel_run
        self._tasks = tasks
        self._slots = slots
        self._job_wakeup = job_wakeup
        self._event_stream = event_stream
        self._child_wait_locks: dict[str, asyncio.Lock] = {}

    def child_run_control(self) -> ChildRunControl:
        return ChildRunControl(
            spawn=self._spawn_child_run,
            list=self.store.list_child_runs_for_run,
            check=self.store.child_runs_for_parent,
            wait=self._wait_for_child_runs,
            cancel=self._cancel_child_runs,
        )

    def agent_mailbox_control(self) -> AgentMailboxControl:
        return AgentMailboxControl(
            send=self._send_agent_message,
            reply=self._reply_agent_message,
            inbox=self.store.list_agent_inbox,
            ack=self._ack_agent_messages,
        )

    async def collaboration_snapshot(self, root_run_id: str) -> dict[str, Any]:
        snapshot = await self.store.collaboration_snapshot(root_run_id)
        snapshot["completion"] = _collaboration_completion_summary(snapshot["children"])
        return snapshot

    async def _send_agent_message(
        self,
        sender_run_id: str,
        sender_operation_id: str,
        recipient_run_id: str,
        kind: AgentMessageKind,
        text: str,
        data: dict[str, Any],
        artifact_refs: Sequence[str],
        ttl_seconds: int,
    ) -> dict[str, Any]:
        message, _created = await self.store.send_agent_message(
            sender_run_id=sender_run_id,
            sender_operation_id=sender_operation_id,
            recipient_run_id=recipient_run_id,
            kind=kind,
            text=text,
            data=data,
            artifact_refs=artifact_refs,
            ttl_seconds=ttl_seconds,
        )
        return message

    async def _reply_agent_message(
        self,
        sender_run_id: str,
        sender_operation_id: str,
        in_reply_to: str,
        kind: AgentMessageKind,
        text: str,
        data: dict[str, Any],
        artifact_refs: Sequence[str],
        ttl_seconds: int,
    ) -> dict[str, Any]:
        message, _created = await self.store.reply_agent_message(
            sender_run_id=sender_run_id,
            sender_operation_id=sender_operation_id,
            in_reply_to=in_reply_to,
            kind=kind,
            text=text,
            data=data,
            artifact_refs=artifact_refs,
            ttl_seconds=ttl_seconds,
        )
        return message

    async def _ack_agent_messages(
        self,
        recipient_run_id: str,
        operation_id: str,
        message_ids: Sequence[str],
    ) -> list[dict[str, Any]]:
        return await self.store.ack_agent_messages(
            recipient_run_id=recipient_run_id,
            message_ids=message_ids,
            operation_id=operation_id,
        )

    async def _spawn_child_run(
        self,
        parent_run_id: str,
        spawn_operation_id: str,
        goal: str,
        agent_definition: dict[str, Any],
        coordination: dict[str, Any],
    ) -> dict[str, Any]:
        parent = await self.store.get_run(parent_run_id)
        parent_settings = (
            _json_object(parent.get("settings_json")) if isinstance(parent, dict) else {}
        )
        parent_settings.pop("_execution_policy", None)
        child_execution_policy = _execution_policy_snapshot(goal, parent_settings)
        child, created = await self.store.accept_child_run(
            parent_run_id=parent_run_id,
            spawn_operation_id=spawn_operation_id,
            goal=goal,
            agent_definition=agent_definition,
            coordination=coordination,
            execution_policy=child_execution_policy,
        )
        spawn_event = child.pop("_spawn_event", None)
        if isinstance(spawn_event, dict):
            event_stream = self._event_stream()
            async with event_stream.publication(parent_run_id):
                event_stream.publish_live(
                    parent_run_id,
                    event_stream.stored_event_envelope(spawn_event),
                )
        if created:
            self._job_wakeup().set()
        return (await self.store.child_runs_for_parent(parent_run_id, [str(child["id"])]))[0]

    async def _wait_for_child_runs(
        self,
        parent_run_id: str,
        child_run_ids: Sequence[str],
        condition: Literal["all", "any"],
        timeout_seconds: float,
    ) -> list[dict[str, Any]]:
        snapshots = await self.store.child_runs_for_parent(parent_run_id, child_run_ids)
        if _child_wait_satisfied(snapshots, condition) or timeout_seconds <= 0:
            return snapshots
        lock = self._child_wait_locks.setdefault(parent_run_id, asyncio.Lock())
        async with lock:
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            async with self._yield_execution_slot(parent_run_id):
                while True:
                    snapshots = await self.store.child_runs_for_parent(parent_run_id, child_run_ids)
                    if _child_wait_satisfied(snapshots, condition):
                        break
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(0.25, remaining))
        if not lock.locked():
            self._child_wait_locks.pop(parent_run_id, None)
        return snapshots

    async def _wait_for_child_runs_terminal(
        self,
        parent_run_id: str,
        child_run_ids: Sequence[str],
    ) -> list[dict[str, Any]]:
        if not child_run_ids:
            return []
        lock = self._child_wait_locks.setdefault(parent_run_id, asyncio.Lock())
        async with lock:
            async with self._yield_execution_slot(parent_run_id):
                while True:
                    snapshots = await self.store.child_runs_for_parent(parent_run_id, child_run_ids)
                    if _child_wait_satisfied(snapshots, "all"):
                        break
                    await asyncio.sleep(0.25)
        if not lock.locked():
            self._child_wait_locks.pop(parent_run_id, None)
        return snapshots

    async def _wait_for_child_status_change(
        self,
        parent_run_id: str,
        current: Sequence[dict[str, Any]],
        child_run_ids: Sequence[str],
    ) -> None:
        previous = {
            str(child["id"]): (str(child.get("status") or ""), str(child.get("updated_at") or ""))
            for child in current
            if str(child["id"]) in child_run_ids
        }
        lock = self._child_wait_locks.setdefault(parent_run_id, asyncio.Lock())
        async with lock:
            async with self._yield_execution_slot(parent_run_id):
                while True:
                    snapshots = await self.store.child_runs_for_parent(parent_run_id, child_run_ids)
                    if any(
                        previous.get(str(child["id"]))
                        != (str(child.get("status") or ""), str(child.get("updated_at") or ""))
                        for child in snapshots
                    ):
                        break
                    await asyncio.sleep(0.25)
        if not lock.locked():
            self._child_wait_locks.pop(parent_run_id, None)

    @asynccontextmanager
    async def _yield_execution_slot(self, parent_run_id: str):
        task = self._tasks().get(parent_run_id)
        should_yield = task is not None and not task.done()
        if not should_yield:
            yield
            return
        self._slots().release()
        self._job_wakeup().set()
        try:
            yield
        finally:
            acquire = asyncio.create_task(self._slots().acquire())
            try:
                await asyncio.shield(acquire)
            except asyncio.CancelledError:
                await acquire
                raise

    async def _cancel_child_runs(
        self,
        parent_run_id: str,
        child_run_ids: Sequence[str],
    ) -> list[dict[str, Any]]:
        await self.store.child_runs_for_parent(parent_run_id, child_run_ids)
        for child_run_id in child_run_ids:
            await self._cancel_run(str(child_run_id))
        return await self.store.child_runs_for_parent(parent_run_id, child_run_ids)
