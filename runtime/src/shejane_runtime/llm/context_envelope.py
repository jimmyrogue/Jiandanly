"""Provider-safe model context budgeting and deterministic truncation."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.messages import BaseMessage, SystemMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.tools import BaseTool


def enforce_context_envelope(
    messages: list[BaseMessage],
    *,
    max_tokens: int,
) -> list[BaseMessage]:
    """Return a deterministic, explicit provider-safe message envelope."""
    if conservative_token_count(messages) <= max_tokens:
        return messages

    max_single_chars = max(1_000, int(max_tokens * 0.65))
    bounded = [truncate_large_message(message, max_chars=max_single_chars) for message in messages]
    if conservative_token_count(bounded) <= max_tokens:
        return bounded

    marker = SystemMessage(
        content=(
            "[Runtime context envelope: older model input was omitted to fit the "
            "selected model's declared context window. Do not assume omitted details.]"
        )
    )
    system_prefix: list[BaseMessage] = []
    for message in bounded:
        if not isinstance(message, SystemMessage):
            break
        system_prefix.append(message)
    turn_start = max(
        (index for index, message in enumerate(bounded) if message.type == "human"),
        default=len(system_prefix),
    )
    latest_turn = bounded[turn_start:]
    turn_budget = max_tokens - conservative_token_count([*system_prefix, marker])
    fitted_turn = _fit_message_contents(latest_turn, max_tokens=max(128, turn_budget))
    result = [*system_prefix, marker, *fitted_turn]
    if (fitted_turn or not latest_turn) and conservative_token_count(result) <= max_tokens:
        return result

    # ponytail: if fixed tool-call metadata alone exceeds the window, retain
    # the latest user input and let the model redo the batch instead of sending
    # an invalid orphan ToolMessage sequence.
    latest_human = next((message for message in latest_turn if message.type == "human"), None)
    fallback = (
        [truncate_large_message(latest_human, max_chars=max(128, turn_budget))]
        if latest_human is not None
        else []
    )
    return [*system_prefix, marker, *fallback]


def _fit_message_contents(
    messages: list[BaseMessage],
    *,
    max_tokens: int,
) -> list[BaseMessage]:
    if conservative_token_count(messages) <= max_tokens:
        return messages
    low = 128
    high = max(
        (len(message.content) for message in messages if isinstance(message.content, str)),
        default=low,
    )
    best: list[BaseMessage] = []
    while low <= high:
        limit = (low + high) // 2
        candidate = [truncate_large_message(message, max_chars=limit) for message in messages]
        if conservative_token_count(candidate) <= max_tokens:
            best = candidate
            low = limit + 1
        else:
            high = limit - 1
    return best


def truncate_large_message(message: BaseMessage, *, max_chars: int) -> BaseMessage:
    content = message.content
    if isinstance(content, str):
        return (
            message
            if len(content) <= max_chars
            else message.model_copy(update={"content": _truncate_text(content, max_chars)})
        )
    if not isinstance(content, list):
        return message

    text_parts: list[tuple[int, str, bool]] = []
    for index, block in enumerate(content):
        if isinstance(block, str) and block:
            text_parts.append((index, block, False))
        elif (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
            and block["text"]
        ):
            text_parts.append((index, block["text"], True))
    total_chars = sum(len(text) for _, text, _ in text_parts)
    if not text_parts and isinstance(message, ToolMessage):
        serialized_size = len(json.dumps(content, ensure_ascii=False, default=str))
        if serialized_size > max_chars:
            artifact_id = message.additional_kwargs.get("artifact_id")
            suffix = f" Artifact: {artifact_id}." if artifact_id else ""
            return message.model_copy(
                update={
                    "content": (
                        "[Runtime omitted oversized binary tool content; call the tool again "
                        f"if it is still needed.]{suffix}"
                    )
                }
            )
    if total_chars <= max_chars:
        return message

    low = 0
    high = max(len(text) for _, text, _ in text_parts)
    per_part_limit = 0
    while low <= high:
        candidate = (low + high) // 2
        if sum(min(len(text), candidate) for _, text, _ in text_parts) <= max_chars:
            per_part_limit = candidate
            low = candidate + 1
        else:
            high = candidate - 1

    bounded = list(content)
    for index, text, is_block in text_parts:
        replacement = text if len(text) <= per_part_limit else _truncate_text(text, per_part_limit)
        bounded[index] = {**bounded[index], "text": replacement} if is_block else replacement
    return message.model_copy(update={"content": bounded})


def _truncate_text(content: str, max_chars: int) -> str:
    marker = (
        f"\n\n[Runtime truncated {max(0, len(content) - max_chars)} "
        "characters from this message.]\n\n"
    )
    if max_chars <= len(marker):
        return marker[:max_chars]
    available = max_chars - len(marker)
    head = available // 2
    tail = available - head
    return content[:head] + marker + content[-tail:]


def estimate_tool_tokens(
    tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
) -> int:
    serializable: list[Any] = []
    for tool in tools:
        if isinstance(tool, dict):
            serializable.append(tool)
            continue
        schema: Any = {}
        args_schema = getattr(tool, "args_schema", None)
        if args_schema is not None:
            try:
                schema = args_schema.model_json_schema()
            except Exception:
                schema = str(args_schema)
        serializable.append(
            {
                "name": getattr(tool, "name", getattr(tool, "__name__", "")),
                "description": getattr(tool, "description", ""),
                "input_schema": schema,
            }
        )
    payload = json.dumps(serializable, ensure_ascii=False, default=str)
    # JSON is mostly ASCII (~4 bytes/token), while CJK approaches 3 bytes/token.
    # Two bytes/token stays conservative without downloading tokenizer data.
    return max(0, math.ceil(len(payload.encode("utf-8")) / 2))


def conservative_token_count(messages: list[BaseMessage]) -> int:
    return count_tokens_approximately(messages, chars_per_token=1.0)
