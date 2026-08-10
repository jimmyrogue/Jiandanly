"""Durable Run event replay and ephemeral live subscriber delivery."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from ..dev_trace import trace_stream_event
from ..presentation import project_run_presentation
from ..store.sqlite import LocalStore

_LIVE_EVENT_QUEUE_SIZE = 256

_PRESENTATION_EVENT_TYPES = {
    "assistant.round.committed",
    "tool.requested",
    "tool.completed",
    "tool.failed",
    "tool.canceled",
    "subagent.spawned",
    "subagent.started",
    "subagent.waiting",
    "subagent.completed",
    "subagent.failed",
    "subagent.canceled",
    "subagent.outcome_unknown",
    "permission.required",
    "permission.resolved",
    "question.asked",
    "question.answered",
    "plan.approval_required",
    "plan.resolved",
    "tool.reconciliation_required",
    "tool.reconciliation_resolved",
    "artifact.created",
    "run.completed",
    "run.failed",
    "run.canceled",
    "run.cleanup_required",
}
_TERMINAL_EVENT_TYPES = {
    "run.completed",
    "run.failed",
    "run.canceled",
    "run.cleanup_required",
}


class RunEventStream:
    """Own live SSE subscribers and project durable events for delivery."""

    def __init__(
        self,
        store: LocalStore,
        *,
        run_is_active: Callable[[str], bool],
    ) -> None:
        self.store = store
        self._run_is_active = run_is_active
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._publication_locks: dict[str, asyncio.Lock] = {}
        self._publication_lock_users: dict[str, int] = {}

    async def stream(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield live events while replaying durable events after ``after_seq``."""
        live_events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_LIVE_EVENT_QUEUE_SIZE)
        registered = False
        try:
            async with self.publication(run_id):
                self._subscribers.setdefault(run_id, set()).add(live_events)
                registered = True
                events = await self.store.events_since(run_id, after_seq=after_seq)
            for event, envelope in zip(
                events,
                await self.event_envelopes(events),
                strict=True,
            ):
                trace_stream_event(envelope)
                yield envelope
                after_seq = int(event["seq"])

            while True:
                pending_live: list[dict[str, Any]] = []
                try:
                    pending_live.append(await asyncio.wait_for(live_events.get(), timeout=0.5))
                    while not live_events.empty():
                        pending_live.append(live_events.get_nowait())
                    durable_wake_seqs = [
                        int(event["seq"]) for event in pending_live if event.get("seq") is not None
                    ]
                    replay: list[dict[str, Any]] = []
                    envelopes: list[dict[str, Any]] = []
                    if durable_wake_seqs:
                        # Durable queue entries are wakeups, not payloads. One
                        # range read recovers gaps and avoids N full projections
                        # when several writes arrive before the consumer wakes.
                        wake_seq = max(durable_wake_seqs)
                        events = await self.store.events_since(run_id, after_seq=after_seq)
                        replay = [
                            stored_event
                            for stored_event in events
                            if int(stored_event["seq"]) <= wake_seq
                        ]
                        envelopes = await self.event_envelopes(replay)
                    replay_index = 0
                    for event in pending_live:
                        if event.get("seq") is None:
                            yield event
                            continue
                        wake_seq = int(event["seq"])
                        while (
                            replay_index < len(replay)
                            and int(replay[replay_index]["seq"]) <= wake_seq
                        ):
                            trace_stream_event(envelopes[replay_index])
                            yield envelopes[replay_index]
                            after_seq = int(replay[replay_index]["seq"])
                            replay_index += 1
                except TimeoutError:
                    pass

                # Durable polling recovers events published by another process
                # and any persistent notification dropped by a full live queue.
                async with self.publication(run_id):
                    if not live_events.empty():
                        continue
                    events = await self.store.events_since(run_id, after_seq=after_seq)
                for event, envelope in zip(
                    events,
                    await self.event_envelopes(events),
                    strict=True,
                ):
                    trace_stream_event(envelope)
                    yield envelope
                    after_seq = int(event["seq"])

                run = await self.store.get_run(run_id)
                if run is None:
                    return
                active_job = await self.store.get_active_run_job(run_id)
                if not live_events.empty():
                    continue
                if run.get("status") not in {"queued", "running"} and active_job is None:
                    return
        finally:
            if registered:
                async with self.publication(run_id):
                    subscribers = self._subscribers.get(run_id)
                    if subscribers is not None:
                        subscribers.discard(live_events)
                        if not subscribers:
                            self._subscribers.pop(run_id, None)

    @staticmethod
    def stored_event_envelope(event: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": event["id"],
            "run_id": event["run_id"],
            "seq": event["seq"],
            "event_type": event["event_type"],
            "payload": json.loads(event["payload_json"] or "{}"),
            "created_at": event["created_at"],
        }

    async def event_envelope(self, event: dict[str, Any]) -> dict[str, Any]:
        return (await self.event_envelopes([event]))[0]

    async def event_envelopes(
        self,
        events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Project one replay batch with a single consistent facts read."""
        envelopes = [self.stored_event_envelope(event) for event in events]
        if not events or not any(
            event["event_type"] in _PRESENTATION_EVENT_TYPES for event in events
        ):
            return envelopes
        facts = await self.store.get_run_presentation_facts(str(events[0]["run_id"]))
        if facts is None:
            return envelopes
        decoded_events = []
        for source_event in facts["events"]:
            try:
                payload = json.loads(source_event.get("payload_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                payload = {}
            decoded_events.append(
                {
                    **source_event,
                    "payload": payload if isinstance(payload, dict) else {},
                }
            )
        snapshot = project_run_presentation(
            run=facts["run"],
            assistant_item=facts["assistant_item"],
            events=decoded_events,
            tool_receipts=facts["tool_receipts"],
            wait_candidates=facts["wait_candidates"],
            artifacts=facts["artifacts"],
            event_high_watermark=int(facts["event_high_watermark"]),
        )
        for event, envelope in zip(events, envelopes, strict=True):
            if event["event_type"] not in _PRESENTATION_EVENT_TYPES:
                continue
            seq = int(event["seq"])
            items = [
                candidate
                for candidate in snapshot["items"]
                if candidate["revision"] == seq or candidate["order"]["event_seq"] == seq
            ]
            primary_item = items[-1] if items else None
            if event["event_type"] in _TERMINAL_EVENT_TYPES:
                ids = {item["id"] for item in items}
                items.extend(
                    item
                    for item in snapshot["items"]
                    if item["id"] not in ids
                    and item["kind"] in {"tool", "subagent", "verification"}
                )
            if not items:
                continue
            changes = [{"kind": "item.upsert", "item": item} for item in items]
            # Keep the singular field for Clients released during the schema rollout.
            envelope["presentation_change"] = {
                "kind": "item.upsert",
                "item": primary_item or items[-1],
            }
            if len(changes) > 1:
                envelope["presentation_changes"] = changes
        return envelopes

    def publish_live(self, run_id: str, event: dict[str, Any]) -> None:
        for subscriber in tuple(self._subscribers.get(run_id, ())):
            try:
                subscriber.put_nowait(event)
            except asyncio.QueueFull:
                # Temporary output is allowed to drop under backpressure;
                # durable events are recovered by the database poll above.
                pass

    @asynccontextmanager
    async def publication(self, run_id: str) -> AsyncIterator[None]:
        lock = self._publication_locks.setdefault(run_id, asyncio.Lock())
        self._publication_lock_users[run_id] = self._publication_lock_users.get(run_id, 0) + 1
        try:
            async with lock:
                yield
        finally:
            users = self._publication_lock_users.get(run_id, 1) - 1
            if users > 0:
                self._publication_lock_users[run_id] = users
            else:
                self._publication_lock_users.pop(run_id, None)
            self.discard_if_idle(run_id)

    def discard_if_idle(self, run_id: str) -> None:
        if (
            self._publication_lock_users.get(run_id, 0) == 0
            and not self._subscribers.get(run_id)
            and not self._run_is_active(run_id)
        ):
            self._publication_locks.pop(run_id, None)
