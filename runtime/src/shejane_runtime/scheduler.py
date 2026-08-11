"""Lightweight local scheduler for delayed runtime runs."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from .run_configuration import RUNTIME_PROTOCOL_VERSION, sanitize_run_metadata
from .store.sqlite import LocalStore

log = logging.getLogger("shejane_runtime.scheduler")

TERMINAL_RUN_STATUSES = {"completed", "failed", "canceled"}


class ScheduledRunDispatcher:
    """Poll SQLite for due schedules and run them through RunCoordinator.

    The dispatcher consumes the run stream itself. That matters because
    RunCoordinator persists each event and also mirrors it into a live queue;
    a scheduled run has no foreground renderer stream to drain that queue.
    """

    def __init__(
        self,
        *,
        store: LocalStore,
        coordinator: Any,
        poll_interval: float = 5.0,
    ) -> None:
        self.store = store
        self.coordinator = coordinator
        self.poll_interval = poll_interval
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop(), name="scheduled-run-dispatcher")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def recover_running(self) -> None:
        """Reconcile schedule rows left running across runtime restarts."""
        rows = await self.store.list_scheduled_runs(status="running", limit=100)
        for schedule in rows:
            run_id = schedule.get("run_id")
            if not run_id:
                await self._run_schedule(schedule)
                continue
            run = await self.store.get_run(str(run_id))
            if run is None:
                await self.store.complete_scheduled_run(
                    schedule["id"],
                    status="failed",
                    error_message="Scheduled run record is missing.",
                )
                continue
            await self._finalize_schedule_from_run(schedule["id"], run)

    async def tick(self, *, now: datetime | None = None) -> None:
        due = await self.store.claim_due_scheduled_runs(
            now=(now or datetime.now(UTC)).astimezone(UTC).isoformat()
        )
        if not due:
            return
        await asyncio.gather(*(self._run_schedule(schedule) for schedule in due))

    async def _run_loop(self) -> None:
        try:
            while True:
                try:
                    await self.tick()
                except Exception:
                    log.exception("scheduled run tick failed")
                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            raise

    async def _run_schedule(self, schedule: dict[str, Any]) -> None:
        schedule_id = str(schedule["id"])
        try:
            workspace_error = await self.store.workspace_admission_error(
                principal_id=str(schedule["principal_id"]),
                path=schedule.get("workspace_path"),
            )
            if workspace_error is not None:
                await self.store.complete_scheduled_run(
                    schedule_id,
                    status="failed",
                    error_message=workspace_error,
                )
                return
            metadata = sanitize_run_metadata(_json_object(schedule.get("metadata_json")))
            metadata.update({"intent": "scheduled_run", "scheduled_run_id": schedule_id})
            schedule_settings = _json_object(schedule.get("settings_json"))
            run = await self.coordinator.start_run(
                principal_id=str(schedule["principal_id"]),
                command_id=f"cmd_schedule:{schedule_id}",
                client_message_id=f"msg_schedule:{schedule_id}",
                protocol_version=RUNTIME_PROTOCOL_VERSION,
                required_capabilities=["agent.run", "agent.stream"],
                goal=str(schedule.get("goal") or ""),
                workspace_path=schedule.get("workspace_path"),
                mode=str(schedule.get("model") or "auto"),
                reasoning_mode=(
                    str(schedule_settings["reasoning_mode"])
                    if schedule_settings.get("reasoning_mode")
                    else None
                ),
                history=_json_list(schedule.get("history_json")),
                settings=schedule_settings,
                metadata=metadata,
                settings_are_frozen="_snapshot_version" in schedule_settings,
                metadata_is_trusted=True,
            )
            run_id = str(run["id"])
            bound = await self.store.mark_scheduled_run_started(schedule_id, run_id)
            if bound is None:
                raise RuntimeError("scheduled run disappeared before it could be bound")
            if bound.get("run_id") != run_id:
                return
            if bound.get("status") != "running":
                return
            async for _event in self.coordinator.stream(run_id):
                pass
            fresh_run = await self.store.get_run(run_id)
            if fresh_run is None:
                await self.store.complete_scheduled_run(
                    schedule_id,
                    status="failed",
                    error_message="Scheduled run disappeared before completion.",
                )
                return
            await self._finalize_schedule_from_run(schedule_id, fresh_run)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("scheduled run %s failed to start", schedule_id)
            await self.store.complete_scheduled_run(
                schedule_id,
                status="failed",
                error_message=str(exc),
            )

    async def _finalize_schedule_from_run(
        self,
        schedule_id: str,
        run: dict[str, Any],
    ) -> None:
        run_status = str(run.get("status") or "")
        if run_status == "completed":
            await self.store.complete_scheduled_run(
                schedule_id,
                status="completed",
                result_text=await self._latest_event_text(
                    str(run["id"]),
                    event_type="run.completed",
                    fields=("final_text", "final", "content", "message"),
                ),
            )
            return
        if run_status == "canceled":
            await self.store.complete_scheduled_run(
                schedule_id,
                status="canceled",
                error_message="Scheduled run was canceled.",
            )
            return
        if run_status == "failed":
            await self.store.complete_scheduled_run(
                schedule_id,
                status="failed",
                error_message=await self._latest_event_text(
                    str(run["id"]),
                    event_type="run.failed",
                    fields=("message", "error", "detail"),
                )
                or "Scheduled run failed.",
            )
            return
        await self.store.complete_scheduled_run(
            schedule_id,
            status="failed",
            error_message="Scheduled run paused for user input or permission.",
        )

    async def _latest_event_text(
        self,
        run_id: str,
        *,
        event_type: str,
        fields: tuple[str, ...],
    ) -> str:
        events = await self.store.events_since(run_id, after_seq=0)
        for event in reversed(events):
            if event.get("event_type") != event_type:
                continue
            payload = _json_object(event.get("payload_json"))
            for field in fields:
                value = payload.get(field)
                if value is not None:
                    return str(value)
        return ""


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(raw: Any) -> list[dict[str, str]]:
    if isinstance(raw, list):
        parsed = raw
    else:
        try:
            parsed = json.loads(str(raw or "[]"))
        except json.JSONDecodeError:
            parsed = []
    if not isinstance(parsed, list):
        return []
    out: list[dict[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if content is None:
            continue
        out.append({"role": str(item.get("role") or "user"), "content": str(content)})
    return out
