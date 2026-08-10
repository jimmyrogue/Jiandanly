"""LangGraph checkpoint, interrupt, and compatibility projection helpers."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.load.dump import dumps as lc_dumps
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .run_assistant_projection import (
    _assistant_draft_from_state as _assistant_draft_from_state,
)
from .run_assistant_projection import (
    _assistant_draft_from_update as _assistant_draft_from_update,
)
from .run_assistant_projection import (
    _assistant_round_from_update as _assistant_round_from_update,
)
from .run_errors import ExecutionSettlementError
from .run_failure_projection import (
    _completion_failure_payload as _completion_failure_payload,
)
from .run_failure_projection import (
    _repair_context_from_metadata as _repair_context_from_metadata,
)
from .run_failure_projection import (
    _repair_context_rejected as _repair_context_rejected,
)
from .run_failure_projection import (
    _repair_rejected_failure_payload as _repair_rejected_failure_payload,
)
from .run_failure_projection import (
    _repair_workflow_payload as _repair_workflow_payload,
)
from .run_failure_projection import (
    _retry_context_from_metadata as _retry_context_from_metadata,
)
from .run_failure_projection import _run_failed_payload as _run_failed_payload


def _serialize_payload(payload: Any) -> dict[str, Any]:
    """Best-effort conversion of LangGraph stream payloads into JSON-safe dicts."""
    try:
        return json.loads(lc_dumps(payload))
    except Exception:
        try:
            return json.loads(json.dumps(payload, default=str))
        except Exception:
            return {"repr": str(payload)}


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _checkpoint_id_from_stream(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    return _checkpoint_id_from_config(payload.get("config"))


def _checkpoint_id_from_config(config: Any) -> str | None:
    if not isinstance(config, dict):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    checkpoint_id = configurable.get("checkpoint_id")
    return checkpoint_id if isinstance(checkpoint_id, str) and checkpoint_id else None


def _task_interrupts(task: Any) -> tuple[Any, ...] | list[Any]:
    if isinstance(task, dict):
        return task.get("interrupts") or ()
    return getattr(task, "interrupts", ()) or ()


async def _checkpoint_is_ancestor(
    checkpointer: AsyncSqliteSaver,
    *,
    graph_thread_id: str,
    head_checkpoint_id: str,
    candidate_checkpoint_id: str,
) -> bool:
    """Follow public parent configs; sibling branch checkpoints are not valid heads."""
    current = head_checkpoint_id
    seen: set[str] = set()
    while current and current not in seen:
        if current == candidate_checkpoint_id:
            return True
        seen.add(current)
        item = await checkpointer.aget_tuple(
            {
                "configurable": {
                    "thread_id": graph_thread_id,
                    "checkpoint_ns": "",
                    "checkpoint_id": current,
                }
            }
        )
        if item is None or not isinstance(item.parent_config, dict):
            return False
        parent = item.parent_config.get("configurable")
        current = parent.get("checkpoint_id") if isinstance(parent, dict) else None
    return False


def _waiting_status_for_interrupts(interrupts: list[Any]) -> str:
    if not interrupts:
        raise ExecutionSettlementError("graph paused without a durable interrupt")
    if all(_is_user_input_interrupt(item) for item in interrupts):
        return "waiting_input"
    return "waiting_permission"


def _is_user_input_interrupt(interrupt: Any) -> bool:
    return _is_question_interrupt(interrupt) or _is_plan_approval_interrupt(interrupt)


def _is_question_interrupt(interrupt: Any) -> bool:
    value = getattr(interrupt, "value", None)
    return isinstance(value, dict) and value.get("kind") == "question"


def _is_plan_approval_interrupt(interrupt: Any) -> bool:
    value = getattr(interrupt, "value", None)
    return isinstance(value, dict) and value.get("kind") == "plan_approval"


def normalize_todos(value: Any) -> list[dict[str, str]]:
    """Decode legacy plan-approval payloads kept for old persisted events."""
    if not isinstance(value, list):
        return []
    todos: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, dict):
            content = str(item.get("content") or "").strip()
            status = str(item.get("status") or "pending").strip()
        else:
            content = str(item).strip()
            status = "pending"
        if content:
            todos.append(
                {
                    "content": content,
                    "status": (
                        status if status in {"pending", "in_progress", "completed"} else "pending"
                    ),
                }
            )
    return todos


def summarize_todos(todos: list[dict[str, str]]) -> str:
    return "; ".join(item["content"] for item in todos[:5])


def _normalize_question_options(raw: Any) -> list[dict[str, str]]:
    """Coerce `user.ask` options into the AgentQuestionChoice shape."""
    if not isinstance(raw, list):
        return []
    options: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, str):
            label = item.strip()
            if label:
                options.append({"label": label})
            continue
        if isinstance(item, dict):
            label = str(item.get("label", "")).strip()
            if not label:
                continue
            entry: dict[str, str] = {"label": label}
            description = item.get("description")
            if isinstance(description, str) and description.strip():
                entry["description"] = description.strip()
            options.append(entry)
    return options
