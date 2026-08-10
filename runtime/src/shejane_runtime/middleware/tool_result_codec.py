"""Provider-safe, replayable, and size-bounded tool results."""

from __future__ import annotations

import json
from typing import Any

from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import BaseMessage, ToolMessage, message_to_dict, messages_from_dict
from langgraph.types import Command

from ..store.sqlite import LocalStore, ToolReceiptStateError

MAX_MODEL_TOOL_RESULT_BYTES = 64 * 1024
MAX_TOOL_ARTIFACT_BYTES = 16 * 1024 * 1024


def _provider_safe_tool_result(
    request: ToolCallRequest,
    result: ToolMessage | Command[Any],
) -> ToolMessage | Command[Any]:
    """Keep file results compatible with the selected model and provider.

    Text-only models receive an explicit limitation instead of image bytes.
    Runtime-extracted PDF text is also unwrapped from Deep Agents' synthetic
    file block before it reaches OpenAI-compatible providers.
    """
    if not isinstance(result, ToolMessage):
        return result
    blocks = result.content
    call = request.tool_call
    context = getattr(request.runtime, "context", None)
    model_profile = getattr(getattr(context, "model", None), "profile", None)
    if (
        isinstance(blocks, list)
        and any(isinstance(block, dict) and block.get("type") == "image" for block in blocks)
        and isinstance(model_profile, dict)
        and model_profile.get("image_inputs") is False
    ):
        limitation = (
            "Image content was not provided because the selected model is text-only. "
            "Choose a model marked as supporting images before describing this file."
        )
        remaining = [
            block
            for block in blocks
            if not (isinstance(block, dict) and block.get("type") == "image")
        ]
        return result.model_copy(
            update={
                "content": (
                    [*remaining, {"type": "text", "text": limitation}] if remaining else limitation
                ),
                "additional_kwargs": {
                    **result.additional_kwargs,
                    "runtime_image_omitted": True,
                },
            }
        )
    if str(call.get("name") or "") != "read_file":
        return result
    arguments = call.get("args")
    requested_path = arguments.get("file_path") if isinstance(arguments, dict) else None
    if not isinstance(requested_path, str) or not requested_path.lower().endswith(".pdf"):
        return result
    attachments = getattr(context, "attachments", ())
    if requested_path not in attachments:
        return result
    blocks = result.content
    if not isinstance(blocks, list) or len(blocks) != 1:
        return result
    block = blocks[0]
    if not isinstance(block, dict) or block.get("type") != "file":
        return result
    if block.get("mime_type") != "application/pdf" or not isinstance(block.get("base64"), str):
        return result
    return result.model_copy(
        update={
            "content": block["base64"],
            "additional_kwargs": {
                **result.additional_kwargs,
                "runtime_extracted_text_from": "application/pdf",
            },
        }
    )


def serialize_tool_result(result: ToolMessage | Command[Any]) -> str:
    if isinstance(result, ToolMessage):
        payload = {"kind": "tool_message", "value": message_to_dict(result)}
    elif isinstance(result, Command):
        payload = {
            "kind": "command",
            "graph": result.graph,
            "update": _encode_json_value(result.update),
            "resume": _encode_json_value(result.resume),
            "goto": _encode_json_value(result.goto),
        }
    else:  # pragma: no cover - enforced by LangChain's wrapper contract
        raise ToolReceiptStateError("tool handler returned an unsupported result type")
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


async def _bound_tool_result(
    *,
    result: ToolMessage | Command[Any],
    store: LocalStore,
    run_id: str,
    operation_id: str,
    tool_call: dict[str, Any],
) -> ToolMessage | Command[Any]:
    if isinstance(result, ToolMessage):
        raw = _message_content_text(result.content)
        content_type = "text/plain"
        if len(raw.encode("utf-8")) <= MAX_MODEL_TOOL_RESULT_BYTES:
            serialized = serialize_tool_result(result)
            if len(serialized.encode("utf-8")) <= MAX_MODEL_TOOL_RESULT_BYTES:
                return result
    else:
        raw = serialize_tool_result(result)
        content_type = "application/json"
    raw_bytes = raw.encode("utf-8")
    if not isinstance(result, ToolMessage) and len(raw_bytes) <= MAX_MODEL_TOOL_RESULT_BYTES:
        return result
    # ponytail: SQLite artifacts cap at 16 MiB; move oversized bodies to a
    # content-addressed file/blob store when real workloads exceed this ceiling.
    artifact_content = _head_tail_bytes(raw_bytes, MAX_TOOL_ARTIFACT_BYTES)
    artifact = await store.create_artifact(
        artifact_id=f"art_{operation_id.removeprefix('toolop_')}",
        run_id=run_id,
        kind="tool_output",
        title=f"{tool_call.get('name') or 'tool'} full output",
        content=artifact_content,
        content_type=content_type,
        tool_call_id=str(tool_call.get("id") or ""),
        tool_name=str(tool_call.get("name") or ""),
        metadata={
            "operation_id": operation_id,
            "original_bytes": len(raw_bytes),
            "artifact_truncated": len(raw_bytes) > MAX_TOOL_ARTIFACT_BYTES,
        },
    )
    source = raw
    preview = _head_tail_bytes(source.encode("utf-8"), 32 * 1024)
    artifact_is_complete = len(raw_bytes) <= MAX_TOOL_ARTIFACT_BYTES
    summary = ToolMessage(
        content=(
            f"{preview}\n\n[{'Full' if artifact_is_complete else 'Truncated'} tool output "
            f"stored as artifact {artifact['id']}; "
            f"original size {len(raw_bytes)} bytes.]"
        ),
        name=str(tool_call.get("name") or ""),
        tool_call_id=str(tool_call.get("id") or ""),
        status=result.status if isinstance(result, ToolMessage) else "success",
        additional_kwargs={
            "artifact_id": artifact["id"],
            "original_bytes": len(raw_bytes),
        },
    )
    if not isinstance(result, Command):
        return summary
    update = result.update
    if isinstance(update, dict):
        bounded_update = {**update, "messages": [summary]}
        candidate = Command(
            graph=result.graph,
            update=bounded_update,
            resume=result.resume,
            goto=result.goto,
        )
        if len(serialize_tool_result(candidate).encode("utf-8")) <= MAX_MODEL_TOOL_RESULT_BYTES:
            return candidate
    raise ToolReceiptStateError(
        "oversized Command contains non-message state that cannot be safely compacted"
    )


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def _head_tail_bytes(value: bytes, limit: int) -> str:
    if len(value) <= limit:
        return value.decode("utf-8", errors="replace")
    marker = f"\n…[{len(value) - limit} bytes omitted]…\n".encode()
    available = max(0, limit - len(marker))
    head = available * 3 // 4
    tail = available - head
    return (value[:head] + marker + value[-tail:]).decode("utf-8", errors="replace")


def _deserialize_tool_result(value: str) -> ToolMessage | Command[Any]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ToolReceiptStateError("stored tool result is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ToolReceiptStateError("stored tool result is not an object")
    if payload.get("kind") == "tool_message":
        raw = payload.get("value")
        if not isinstance(raw, dict):
            raise ToolReceiptStateError("stored ToolMessage is invalid")
        messages = messages_from_dict([raw])
        if len(messages) != 1 or not isinstance(messages[0], ToolMessage):
            raise ToolReceiptStateError("stored result is not a ToolMessage")
        return messages[0]
    if payload.get("kind") == "command":
        return Command(
            graph=payload.get("graph"),
            update=_decode_json_value(payload.get("update")),
            resume=_decode_json_value(payload.get("resume")),
            goto=_decode_json_value(payload.get("goto")),
        )
    raise ToolReceiptStateError("stored tool result kind is unsupported")


def _encode_json_value(value: Any) -> Any:
    if isinstance(value, BaseMessage):
        return {"__runtime_type__": "message", "value": message_to_dict(value)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ToolReceiptStateError("tool result contains a non-string mapping key")
        return {key: _encode_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode_json_value(item) for item in value]
    raise ToolReceiptStateError(f"tool result contains unsupported value {type(value).__name__}")


def _decode_json_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode_json_value(item) for item in value]
    if isinstance(value, dict):
        if value.get("__runtime_type__") == "message":
            raw = value.get("value")
            if not isinstance(raw, dict):
                raise ToolReceiptStateError("stored message value is invalid")
            return messages_from_dict([raw])[0]
        return {key: _decode_json_value(item) for key, item in value.items()}
    return value
