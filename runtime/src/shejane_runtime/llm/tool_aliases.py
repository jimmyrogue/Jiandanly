"""Stable Runtime tool names at provider protocol boundaries."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

_SAFE_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def provider_tools(
    tools: Sequence[Any],
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, str]]:
    converted = [convert_to_openai_tool(tool) for tool in tools]
    reserved_names = {
        function["name"]
        for schema in converted
        if isinstance((function := schema.get("function")), dict)
        and isinstance(function.get("name"), str)
        and _SAFE_TOOL_NAME.fullmatch(function["name"])
    }
    used_names: set[str] = set()
    schemas: list[dict[str, Any]] = []
    aliases: dict[str, str] = {}
    choices: dict[str, str] = {}
    for schema in converted:
        function = schema.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            schemas.append(schema)
            continue
        original = function["name"]
        if _SAFE_TOOL_NAME.fullmatch(original):
            wire_name = original
            legacy_wire_name = None
        else:
            stem = re.sub(r"[^a-zA-Z0-9_-]+", "_", original).strip("_") or "tool"
            legacy_wire_name = f"{stem[:55]}_{hashlib.sha256(original.encode()).hexdigest()[:8]}"
            wire_name = stem
            if len(wire_name) > 64 or wire_name in reserved_names or wire_name in used_names:
                wire_name = legacy_wire_name
        used_names.add(wire_name)
        schemas.append({**schema, "function": {**function, "name": wire_name}})
        aliases[wire_name] = original
        if legacy_wire_name is not None and legacy_wire_name not in reserved_names:
            aliases.setdefault(legacy_wire_name, original)
        choices[original] = wire_name
    return schemas, aliases, choices


def rewrite_tool_names(message: BaseMessage, aliases: dict[str, str]) -> BaseMessage:
    if not aliases:
        return message
    if isinstance(message, ToolMessage):
        additional_kwargs = message.additional_kwargs
        raw_name = additional_kwargs.get("name")
        return message.model_copy(
            update={
                "name": aliases.get(message.name, message.name),
                "additional_kwargs": (
                    {**additional_kwargs, "name": aliases.get(raw_name, raw_name)}
                    if isinstance(raw_name, str)
                    else additional_kwargs
                ),
            }
        )
    if not isinstance(message, (AIMessage, AIMessageChunk)):
        return message
    updates: dict[str, Any] = {}
    if isinstance(message.content, list):
        updates["content"] = [
            {
                **block,
                "name": aliases.get(block.get("name"), block.get("name")),
            }
            if isinstance(block, dict)
            and block.get("type") in {"tool_call", "function_call", "tool_use"}
            and isinstance(block.get("name"), str)
            else block
            for block in message.content
        ]
    for field in ("tool_calls", "invalid_tool_calls", "tool_call_chunks"):
        calls = getattr(message, field, None)
        if calls:
            updates[field] = [
                {**call, "name": aliases.get(call.get("name"), call.get("name"))} for call in calls
            ]
    raw_calls = message.additional_kwargs.get("tool_calls")
    if isinstance(raw_calls, list):
        normalized = []
        for call in raw_calls:
            function = call.get("function") if isinstance(call, dict) else None
            normalized.append(
                {
                    **call,
                    "function": {
                        **function,
                        "name": aliases.get(function.get("name"), function.get("name")),
                    },
                }
                if isinstance(call, dict) and isinstance(function, dict)
                else call
            )
        updates["additional_kwargs"] = {**message.additional_kwargs, "tool_calls": normalized}
    return message.model_copy(update=updates)


_provider_tools = provider_tools
_rewrite_tool_names = rewrite_tool_names
