"""Persist LangGraph interrupts as Runtime-owned waiting state."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from .progress_ledger import build_handoff_snapshot
from .run_stream_state import (
    _normalize_question_options,
    normalize_todos,
    summarize_todos,
)
from .store.sqlite import LocalStore


async def build_waiting_handoff(store: LocalStore, run_id: str) -> dict[str, Any]:
    raw_events = await store.events_since(run_id, after_seq=0)
    events: list[dict[str, Any]] = []
    for event in raw_events:
        try:
            payload = json.loads(event.get("payload_json") or "{}")
        except json.JSONDecodeError:
            payload = {}
        events.append(
            {
                "id": event.get("id"),
                "run_id": event.get("run_id"),
                "seq": event.get("seq"),
                "event_type": event.get("event_type"),
                "payload": payload,
                "created_at": event.get("created_at"),
            }
        )
    artifacts = await store.list_artifacts_for_run(run_id)
    return build_handoff_snapshot(events, artifacts)


async def handle_run_interrupt(
    store: LocalStore,
    emit: Callable[[asyncio.Event | None, str, str, dict[str, Any]], Awaitable[None]],
    wakeup: asyncio.Event,
    run_id: str,
    snap_interrupt: Any,
    *,
    wait_cycle_id: str,
) -> None:
    """Persist an interrupt and emit the matching user-decision event."""
    value = getattr(snap_interrupt, "value", None)
    if isinstance(value, dict) and value.get("kind") == "tool_reconciliation":
        operation_id = str(value.get("operation_id") or "")
        interrupt_id = str(getattr(snap_interrupt, "id", None) or "")
        if not operation_id or not interrupt_id:
            raise RuntimeError("tool reconciliation is missing durable identity")
        record = await store.create_tool_reconciliation(
            run_id=run_id,
            operation_id=operation_id,
            wait_cycle_id=wait_cycle_id,
            interrupt_id=interrupt_id,
            payload=value,
        )
        await emit(
            wakeup,
            run_id,
            "tool.reconciliation_required",
            {
                "request_id": record["id"],
                "operation_id": operation_id,
                "tool_name": str(value.get("tool_name") or ""),
                "arguments_hash": str(value.get("arguments_hash") or ""),
                "risk": str(value.get("risk") or ""),
                "allowed_decisions": value.get("allowed_decisions") or [],
                "wait_cycle_id": wait_cycle_id,
                "interrupt_id": interrupt_id,
            },
        )
        return

    if isinstance(value, dict) and value.get("kind") == "plan_approval":
        todos = normalize_todos(value.get("todos"))
        tool_call_id = str(value.get("tool_call_id") or getattr(snap_interrupt, "id", None) or "")
        summary = str(value.get("summary") or summarize_todos(todos))
        record = await store.create_plan_approval(
            run_id=run_id,
            tool_call_id=tool_call_id,
            todos=todos,
            summary=summary,
            wait_cycle_id=wait_cycle_id,
            interrupt_id=str(getattr(snap_interrupt, "id", None) or ""),
        )
        await emit(
            wakeup,
            run_id,
            "plan.approval_required",
            {
                "request_id": record["id"],
                "tool_call_id": tool_call_id,
                "todos": record["todos"],
                "summary": record["summary"],
                "wait_cycle_id": wait_cycle_id,
                "interrupt_id": record["interrupt_id"],
            },
        )
        return

    if isinstance(value, dict) and value.get("kind") == "question":
        questions = [
            {
                "question": str(value.get("question", "")),
                "options": _normalize_question_options(value.get("options") or []),
            }
        ]
        record = await store.create_question(
            run_id=run_id,
            tool_call_id=getattr(snap_interrupt, "id", None),
            questions=questions,
            wait_cycle_id=wait_cycle_id,
            interrupt_id=str(getattr(snap_interrupt, "id", None) or ""),
        )
        questions[0]["id"] = record["id"]
        await emit(
            wakeup,
            run_id,
            "question.asked",
            {"request_id": record["id"], "questions": questions},
        )
        return

    action_requests: list[dict[str, Any]] = []
    if isinstance(value, dict) and isinstance(value.get("action_requests"), list):
        action_requests = [
            action for action in value["action_requests"] if isinstance(action, dict)
        ]
    if not action_requests:
        action_requests = [{"name": "", "args": {}}]

    interrupt_id = str(getattr(snap_interrupt, "id", None) or "")
    for action_index, action in enumerate(action_requests):
        tool_name = str(action.get("name", ""))
        args_raw = action.get("args") or {}
        arguments = args_raw if isinstance(args_raw, dict) else {"value": args_raw}
        record = await store.create_permission(
            run_id=run_id,
            tool_call_id=str(action.get("tool_call_id") or ""),
            tool_name=tool_name,
            tool_version=str(action.get("tool_version") or ""),
            arguments=arguments,
            operation_id=str(action.get("operation_id") or "") or None,
            arguments_hash=str(action.get("arguments_hash") or "") or None,
            risk=str(action.get("risk") or "") or None,
            wait_cycle_id=wait_cycle_id,
            interrupt_id=interrupt_id,
            action_index=action_index,
        )
        await emit(
            wakeup,
            run_id,
            "permission.required",
            {
                "request_id": record["id"],
                "tool": tool_name,
                "tool_name": tool_name,
                "arguments": arguments,
                "description": action.get("description") or "",
                "tool_call_id": record.get("tool_call_id"),
                "operation_id": record.get("operation_id"),
                "arguments_hash": record.get("arguments_hash"),
                "risk": record.get("risk"),
                "review_source": action.get("review_source"),
                "review_reason": action.get("review_reason"),
                "allowed_decisions": action.get("allowed_decisions") or ["approve", "reject"],
                "allow_run_scope": action.get("allow_run_scope") is True,
                "wait_cycle_id": wait_cycle_id,
                "interrupt_id": interrupt_id,
            },
        )
