"""Run event publication, terminal result commit, and callback delivery."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from ..store.sqlite import TRANSIENT_RUN_EVENT_TYPES

log = logging.getLogger("shejane_runtime.runs")


class RunEventPublisherMixin:
    async def _enqueue(
        self,
        wakeup: asyncio.Event | None,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Publish transient output or persist an authoritative event."""
        parent_event: dict[str, Any] | None = None
        async with self._event_stream.publication(run_id):
            if event_type in TRANSIENT_RUN_EVENT_TYPES:
                event = {
                    "id": f"transient_{uuid.uuid4().hex}",
                    "run_id": run_id,
                    "event_type": event_type,
                    "payload": payload,
                    "created_at": datetime.now(UTC).isoformat(),
                }
                round_id = str(payload.get("round_id") or "")
                if event_type == "llm.delta" and round_id:
                    event["presentation_change"] = {
                        "kind": "draft.delta",
                        "round_id": round_id,
                        "content": str(payload.get("content") or ""),
                    }
                elif event_type == "llm.round.closed" and round_id:
                    event["presentation_change"] = {
                        "kind": "draft.closed",
                        "round_id": round_id,
                        "committed_item_ids": payload.get("committed_item_ids") or [],
                    }
            else:
                stored_event = await self.store.append_event(run_id, event_type, payload)
                candidate = stored_event.get("_parent_event")
                if isinstance(candidate, dict):
                    parent_event = candidate
                event = self._event_stream.stored_event_envelope(stored_event)
            self._event_stream.publish_live(run_id, event)
        self._trace_stream_event(event)
        if parent_event is not None:
            await self._publish_derived_parent_event(parent_event)
        if wakeup is not None:
            wakeup.set()

    async def _commit_run_result(
        self,
        wakeup: asyncio.Event,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        status: str,
    ) -> None:
        """Persist the authoritative result before notifying live subscribers."""
        parent_event: dict[str, Any] | None = None
        envelope: dict[str, Any] | None = None
        async with self._event_stream.publication(run_id):
            event, created = await self.store.commit_run_result(
                run_id,
                status=status,
                event_type=event_type,
                payload=payload,
            )
            if created:
                envelope = self._event_stream.stored_event_envelope(event)
                self._event_stream.publish_live(run_id, envelope)
                candidate = event.get("_parent_event")
                if isinstance(candidate, dict):
                    parent_event = candidate
        if created and envelope is not None:
            self._trace_stream_event(envelope)
        if parent_event is not None:
            await self._publish_derived_parent_event(parent_event)
        if not created:
            return
        wakeup.set()
        if self._terminal_callback is not None and status in {
            "completed",
            "failed",
            "canceled",
            "cleanup_required",
        }:
            task = asyncio.create_task(
                self._terminal_callback(run_id, status, payload),
                name=f"central-diagnostics:{run_id}",
            )
            self._terminal_callback_tasks.add(task)
            task.add_done_callback(
                lambda completed: self._terminal_callback_finished(
                    completed,
                    run_id=run_id,
                    status=status,
                )
            )
        if status in {"waiting_permission", "waiting_input"}:
            resume_payload = await self.store.latest_resolved_wait_cycle_payload(run_id)
            if resume_payload is not None:
                await self.resume_run(run_id=run_id, decision=resume_payload)

    async def _publish_derived_parent_event(self, event: dict[str, Any]) -> None:
        parent_run_id = str(event["run_id"])
        async with self._event_stream.publication(parent_run_id):
            self._event_stream.publish_live(
                parent_run_id,
                self._event_stream.stored_event_envelope(event),
            )
        wakeup = self._wakeups.get(parent_run_id)
        if wakeup is not None:
            wakeup.set()

    def _terminal_callback_finished(
        self,
        task: asyncio.Task[None],
        *,
        run_id: str,
        status: str,
    ) -> None:
        self._terminal_callback_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.warning(
                "central diagnostics upload failed run_id=%s status=%s error_type=%s",
                run_id,
                status,
                type(exc).__name__,
            )
