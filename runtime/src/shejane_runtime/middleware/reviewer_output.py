"""Shared strict-JSON invocation for internal model reviewers."""

from __future__ import annotations

import json
from typing import Any


async def invoke_json_review(
    model: Any,
    messages: list[Any],
    *,
    schema_name: str,
    schema: dict[str, Any],
) -> dict[str, Any]:
    kwargs = {}
    if getattr(model, "supports_json_schema_output", False):
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": schema,
            },
        }
    response = await model.ainvoke(messages, **kwargs)
    value = _message_text(getattr(response, "content", "")).strip()
    if value.startswith("```json") and value.endswith("```"):
        value = value[len("```json") : -len("```")].strip()
    elif value.startswith("```") and value.endswith("```"):
        value = value[len("```") : -len("```")].strip()
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("reviewer response must be a JSON object")
    return parsed


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text") or "") if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content)
