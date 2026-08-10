"""Display-safe assistant draft and progress projection from graph state."""

from __future__ import annotations

import hashlib
import json
from typing import Any


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
            if getattr(message, "type", None) == "ai":
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
