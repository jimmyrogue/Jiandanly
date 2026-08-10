from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from langchain_core.load.dump import dumps as lc_dumps
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .failure_policy import classify_failure_payload
from .llm.errors import ModelServiceError
from .run_errors import ExecutionSettlementError


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
    config = payload.get("config")
    return _checkpoint_id_from_config(config)


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


def _repair_context_from_metadata(
    metadata: dict[str, Any],
    *,
    max_attempts: int,
) -> dict[str, Any] | None:
    if str(metadata.get("intent", "")).strip().lower() != "repair":
        return None
    return {
        "attempt": _positive_int(metadata.get("attempt"), default=1),
        "max_attempts": max(0, int(max_attempts)),
        "source_run_id": _non_empty_str(metadata.get("source_run_id")),
        "source_message_id": _non_empty_str(metadata.get("source_message_id")),
        "failure_category": _non_empty_str(metadata.get("failure_category")),
        "failure_action_kind": _non_empty_str(metadata.get("failure_action_kind")),
    }


def _retry_context_from_metadata(metadata: dict[str, Any]) -> dict[str, Any] | None:
    if str(metadata.get("intent", "")).strip().lower() != "retry":
        return None
    return {
        "attempt": _positive_int(metadata.get("attempt"), default=1),
        "source_run_id": _non_empty_str(metadata.get("source_run_id")),
        "source_message_id": _non_empty_str(metadata.get("source_message_id")),
        "failure_category": _non_empty_str(metadata.get("failure_category")),
        "failure_action_kind": _non_empty_str(metadata.get("failure_action_kind")),
    }


def _repair_context_rejected(context: dict[str, Any]) -> bool:
    max_attempts = int(context.get("max_attempts") or 0)
    attempt = int(context.get("attempt") or 1)
    return max_attempts <= 0 or attempt > max_attempts


def _repair_workflow_payload(
    context: dict[str, Any],
    *,
    status: str,
    reason: str | None = None,
) -> dict[str, Any]:
    payload = {
        "status": status,
        "attempt": int(context.get("attempt") or 1),
        "max_attempts": int(context.get("max_attempts") or 0),
        "source_run_id": context.get("source_run_id"),
        "source_message_id": context.get("source_message_id"),
        "failure_category": context.get("failure_category"),
        "failure_action_kind": context.get("failure_action_kind"),
        "reason": reason,
    }
    return {key: value for key, value in payload.items() if value is not None}


def _repair_rejected_failure_payload(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "error": "repair attempt limit exceeded",
        "type": "RepairWorkflowRejected",
        "category": "validation",
        "recoverable": True,
        "retryable": False,
        "action_kind": "repair",
        "suggested_action": (
            "Review the previous repair attempts and adjust the task or inputs before retrying."
        ),
        "attempt": int(context.get("attempt") or 1),
        "max_attempts": int(context.get("max_attempts") or 0),
    }


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _non_empty_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _run_failed_payload(
    exc: Exception,
    *,
    secrets: tuple[str, ...] = (),
) -> dict[str, Any]:
    if isinstance(exc, ModelServiceError):
        payload = exc.to_event_payload()
    else:
        payload = {"error": str(exc), "type": type(exc).__name__}
        code = getattr(exc, "code", None)
        retryable = getattr(exc, "retryable", None)
        if isinstance(code, str) and code:
            payload["code"] = code
        if isinstance(retryable, bool):
            payload["retryable"] = retryable
    payload = _redact_failure_value(payload, secrets=secrets)
    classification = classify_failure_payload("run.failed", payload)
    for key in (
        "category",
        "recoverable",
        "retryable",
        "action_kind",
        "recovery_action",
        "suggested_action",
    ):
        payload.setdefault(key, classification[key])
    if classification.get("code"):
        payload.setdefault("error_code", classification["code"])
    return payload


def _redact_failure_value(value: Any, *, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        return {key: _redact_failure_value(item, secrets=secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_failure_value(item, secrets=secrets) for item in value]
    if not isinstance(value, str):
        return value
    redacted = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _waiting_status_for_interrupts(interrupts: list[Any]) -> str:
    if not interrupts:
        raise ExecutionSettlementError("graph paused without a durable interrupt")
    if interrupts and all(_is_user_input_interrupt(item) for item in interrupts):
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


def _completion_failure_payload(
    state_values: Any,
    *,
    current_run_id: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(state_values, dict):
        route: dict[str, Any] = {}
    else:
        value = state_values.get("completion_route")
        route = value if isinstance(value, dict) else {}
    if current_run_id is not None and route.get("run_id") != current_run_id:
        route = {
            "decision": "failed",
            "reason": "completion_route_scope_mismatch",
            "message": "The graph ended without a completion decision owned by this run.",
            "recoverable": False,
            "run_id": current_run_id,
        }
    if route.get("decision") not in {"failed", "blocked"}:
        return None
    reason = str(route.get("reason") or "model_output_invalid")
    message = str(route.get("message") or "The model did not produce a valid result.")
    payload = {
        "error": message,
        "error_code": reason,
        "source": "completion_router",
        "failure_category": ("verification" if reason == "verification_failed" else "model_output"),
        "recoverable": bool(route.get("recoverable")),
        "retryable": False,
        "details": {
            key: route[key] for key in ("attempts", "max_attempts", "tool_call_id") if key in route
        },
    }
    classification = classify_failure_payload("run.failed", payload)
    for key in (
        "category",
        "action_kind",
        "recovery_action",
        "suggested_action",
    ):
        payload[key] = classification[key]
    return payload


def _assistant_draft_from_update(payload: Any) -> dict[str, Any] | None:
    """Extract the latest fully assembled top-level AI message from an update."""
    message = _complete_ai_message_from_update(payload)
    if message is None:
        return None
    content = _assistant_content_text(getattr(message, "content", None))
    tool_calls = [
        dict(item)
        for item in (getattr(message, "tool_calls", None) or [])
        if isinstance(item, dict)
    ]
    identity = {
        "id": getattr(message, "id", None),
        "content": content,
        "tool_calls": tool_calls,
    }
    message_key = hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
    return {
        "message_key": message_key,
        "content": content,
        "tool_calls": tool_calls,
    }


def _assistant_round_from_update(
    payload: Any,
    *,
    allow_reasoning_summary: bool = False,
) -> dict[str, Any] | None:
    """Extract one durable, display-safe progress round from a model update."""
    message = _complete_ai_message_from_update(payload)
    if message is None:
        return None
    additional_kwargs = getattr(message, "additional_kwargs", None)
    if not isinstance(additional_kwargs, dict):
        return None
    round_id = str(additional_kwargs.get("runtime_model_call_id") or "")
    tool_call_ids = [
        str(item.get("id") or "")
        for item in (getattr(message, "tool_calls", None) or [])
        if isinstance(item, dict) and item.get("id")
    ]
    if not round_id or not tool_call_ids:
        return None
    summary = _provider_reasoning_summary(message) if allow_reasoning_summary else None
    return {
        "round_id": round_id,
        "text": _assistant_content_text(getattr(message, "content", None)),
        "reasoning_summary": summary,
        "tool_call_ids": tool_call_ids,
    }


def _provider_reasoning_summary(message: Any) -> str | None:
    """Return provider-declared summaries, never generic/raw reasoning blocks."""
    response_metadata = getattr(message, "response_metadata", None)
    if (
        not isinstance(response_metadata, dict)
        or response_metadata.get("model_provider") != "openai"
    ):
        return None
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "reasoning":
            continue
        summary = block.get("summary")
        if not isinstance(summary, list):
            continue
        for item in summary:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts) or None


def _complete_ai_message_from_update(payload: Any) -> Any | None:
    if not isinstance(payload, dict):
        return None
    for delta in reversed(list(payload.values())):
        if not isinstance(delta, dict):
            continue
        messages = delta.get("messages")
        if not isinstance(messages, list):
            continue
        for message in reversed(messages):
            if getattr(message, "type", None) != "ai":
                continue
            return message
    return None


def _assistant_draft_from_state(state: Any, *, run_id: str) -> dict[str, Any] | None:
    """Build one user-visible answer from the current run's top-level model rounds."""
    if not isinstance(state, dict):
        return None
    messages = state.get("messages")
    if not isinstance(messages, list):
        return None

    start: int | None = None
    for index in range(len(messages) - 1, -1, -1):
        kwargs = getattr(messages[index], "additional_kwargs", None)
        if (
            isinstance(kwargs, dict)
            and kwargs.get("runtime_kind") == "task_input"
            and kwargs.get("runtime_run_id") == run_id
        ):
            start = index + 1
            break
    if start is None:
        return None

    assistant_messages = [
        message for message in messages[start:] if getattr(message, "type", None) == "ai"
    ]
    if not assistant_messages:
        return None

    final_message = assistant_messages[-1]
    visible_messages = [
        message
        for message in assistant_messages
        if message is final_message or getattr(message, "tool_calls", None)
    ]
    content_parts = [
        content.strip()
        for message in visible_messages
        if (content := _assistant_content_text(getattr(message, "content", None))).strip()
    ]
    tool_calls = [
        dict(item)
        for item in (getattr(final_message, "tool_calls", None) or [])
        if isinstance(item, dict)
    ]
    identity = [
        {
            "id": getattr(message, "id", None),
            "content": _assistant_content_text(getattr(message, "content", None)),
            "tool_calls": list(getattr(message, "tool_calls", None) or []),
        }
        for message in visible_messages
    ]
    return {
        "message_key": hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
        ).hexdigest(),
        "content": "\n\n".join(content_parts),
        "tool_calls": tool_calls,
    }


def _assistant_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
        elif isinstance(item, str):
            parts.append(item)
    return "".join(parts)


def _normalize_question_options(raw: Any) -> list[dict[str, str]]:
    """Coerce `user.ask` options into the {label, description?} shape the
    TS `AgentQuestionChoice` contract expects.

    The tool signature is `options: list[str]`, so the agent typically
    emits bare strings. Earlier behavior shipped these through unchanged,
    which the client's parseQuestionPayload silently filtered out
    (typeof option !== 'object' → undefined) leaving the question UI
    with zero options to render — the run looked stuck even though
    everything else was fine.

    Accepts:
        - a string         → {label: string}
        - a {label, ...}   → passed through, coerced to strings
        - anything else    → skipped
    """
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
