"""Evidence extraction and shared route state for P9 completion acceptance."""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import ToolMessage

from ..failure_policy import classify_failure_payload
from ..tool_outcomes import tool_result_envelope, tool_result_envelope_failed


def completion_repair_instruction(state: Any, *, run_id: str | None = None) -> str | None:
    if not isinstance(state, dict):
        return None
    route = state.get("completion_route")
    if not isinstance(route, dict) or route.get("decision") != "repair_requested":
        return None
    if run_id is not None and route.get("run_id") != run_id:
        return None
    value = route.get("instruction")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _terminal_route(
    decision: str,
    reason: str,
    message: str,
    *,
    recoverable: bool,
    run_id: str,
) -> dict[str, Any]:
    return {
        "completion_route": {
            "decision": decision,
            "reason": reason,
            "message": message,
            "recoverable": recoverable,
            "run_id": run_id,
        },
        "jump_to": "end",
    }


def _assistant_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            value = block.get("text")
            if isinstance(value, str):
                parts.append(value)
    return "".join(parts)


def _repeated_deterministic_tool_failure(messages: list[Any]) -> tuple[str, str] | None:
    latest: tuple[str, str] | None = None
    for message in reversed(messages):
        message_type = getattr(message, "type", None)
        if message_type == "human":
            return None
        if message_type != "tool":
            continue
        if str(getattr(message, "status", "") or "").lower() != "error":
            return None
        tool = str(getattr(message, "name", "") or "unknown")
        error = " ".join(str(getattr(message, "content", "") or "").split())
        classification = classify_failure_payload(
            "tool.failed",
            {"tool": tool, "content": error, "retryable": False},
        )
        if classification["category"] not in {
            "auth",
            "configuration",
            "fatal",
            "permission",
            "quota",
            "validation",
            "workspace",
        }:
            return None
        signature = (tool, error)
        if latest is None:
            latest = signature
            continue
        return signature if signature == latest else None
    return None


_PROSE_CLARIFICATION = re.compile(
    r"(?:你|您)(?:指的是|希望(?:按|用|选择|采用)|想(?:要|选择|使用)|需要(?:提供|选择|确认)|偏好)"
    r"|请(?:提供|告诉|选择|确认|说明|指定|补充)"
    r"|\b(?:which|what|how|where|when|would)\b.{0,80}\b(?:you|your)\b"
    r"|\bplease\s+(?:provide|choose|confirm|specify|tell)\b",
    re.IGNORECASE,
)
_NON_BLOCKING_OFFER = re.compile(
    r"\b(?:how|what)\s+can\s+i\s+(?:help|assist)(?:\s+you)?(?:\s+with)?\b",
    re.IGNORECASE,
)


def _is_prose_clarification(text: str) -> bool:
    value = _NON_BLOCKING_OFFER.sub("", " ".join(text.split()))
    return ("?" in value or "？" in value) and _PROSE_CLARIFICATION.search(value) is not None


def _finish_reason(message: Any) -> str:
    metadata = getattr(message, "response_metadata", None)
    if not isinstance(metadata, dict):
        return ""
    value = metadata.get("finish_reason") or metadata.get("stop_reason")
    return str(value or "").strip().lower()


def _latest_task_verification(messages: list[Any]) -> dict[str, Any] | None:
    # A graph fork may inherit old ToolMessages. Verification belongs to the
    # current user turn only; otherwise a failed check from an ancestor Run can
    # permanently block an unrelated follow-up.
    explicit_turn_start = [
        index
        for index, message in enumerate(messages)
        if getattr(message, "type", None) == "human"
        and isinstance(getattr(message, "additional_kwargs", None), dict)
        and message.additional_kwargs.get("runtime_kind") == "task_input"
    ]
    fallback_turn_start = max(
        (
            index
            for index, message in enumerate(messages)
            if getattr(message, "type", None) == "human"
            and not (
                isinstance(getattr(message, "additional_kwargs", None), dict)
                and message.additional_kwargs.get("runtime_kind") == "steering"
            )
        ),
        default=0,
    )
    turn_start = explicit_turn_start[-1] if explicit_turn_start else fallback_turn_start
    scoped = messages[turn_start:]
    for reverse_index, message in enumerate(reversed(scoped)):
        if getattr(message, "type", None) != "tool":
            continue
        if getattr(message, "name", "") != "task.verify":
            continue
        payload = _parse_tool_content(getattr(message, "content", ""))
        if not isinstance(payload, dict):
            return {
                "ok": False,
                "reason": "task.verify returned an unreadable result",
                "tool_call_id": str(getattr(message, "tool_call_id", "")),
            }
        ok = _truthy(payload.get("ok"))
        verification_index = len(scoped) - reverse_index - 1
        if ok:
            stale_tool = _verification_invalidating_tool(scoped[verification_index + 1 :])
            if stale_tool is not None:
                return {
                    "ok": False,
                    "reason": f"verification became stale after tool {stale_tool}",
                    "tool_call_id": str(getattr(message, "tool_call_id", "")),
                }
        return {
            "ok": ok,
            "reason": "verification passed" if ok else _verification_reason(payload),
            "tool_call_id": str(getattr(message, "tool_call_id", "")),
        }
    return None


_VERIFICATION_PRESERVING_TOOLS = {
    "clipboard.read",
    "environment.observe",
    "glob",
    "grep",
    "ls",
    "memory.search",
    "office.outline",
    "office.read",
    "office.read_range",
    "office.read_slides",
    "open.file",
    "open.url",
    "pdf.inspect",
    "read_file",
    "task.progress",
    "time.now",
    "web.fetch",
    "web.search",
}


def _verification_invalidating_tool(messages: list[Any]) -> str | None:
    for message in messages:
        if getattr(message, "type", None) != "tool":
            continue
        name = str(getattr(message, "name", "") or "unknown")
        status = str(getattr(message, "status", "") or "").lower()
        if status == "error" or name not in _VERIFICATION_PRESERVING_TOOLS:
            return name
    return None


def _has_current_tool_evidence(messages: list[Any], run_id: str) -> bool:
    """Limit semantic review to tool evidence produced for the current task."""
    start = 0
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        kwargs = getattr(message, "additional_kwargs", None)
        if not isinstance(kwargs, dict) or kwargs.get("runtime_kind") != "task_input":
            continue
        message_run_id = str(kwargs.get("runtime_run_id") or "")
        if not run_id or not message_run_id or message_run_id == run_id:
            start = index
            break
    return any(getattr(message, "type", None) == "tool" for message in messages[start:-1])


def _latest_memory_write_failed(messages: list[Any], run_id: str) -> bool:
    """Trust the latest memory.write receipt, never the model's success claim."""
    start = 0
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        kwargs = getattr(message, "additional_kwargs", None)
        if not isinstance(kwargs, dict) or kwargs.get("runtime_kind") != "task_input":
            continue
        message_run_id = str(kwargs.get("runtime_run_id") or "")
        if not run_id or not message_run_id or message_run_id == run_id:
            start = index
            break
    for message in reversed(messages[start:-1]):
        if getattr(message, "type", None) != "tool":
            continue
        if str(getattr(message, "name", "") or "") != "memory.write":
            continue
        status = str(getattr(message, "status", "") or "").lower()
        envelope = tool_result_envelope(getattr(message, "content", None))
        return status == "error" or tool_result_envelope_failed(envelope)
    return False


def _current_run_id(runtime: Any, messages: list[Any]) -> str:
    context = getattr(runtime, "context", None)
    value = getattr(context, "run_id", None)
    if isinstance(value, str) and value:
        return value
    for message in reversed(messages):
        kwargs = getattr(message, "additional_kwargs", None)
        if isinstance(kwargs, dict) and kwargs.get("runtime_kind") == "task_input":
            candidate = kwargs.get("runtime_run_id")
            if isinstance(candidate, str):
                return candidate
    return ""


def _repair_attempts_for_run(state: Any, run_id: str) -> int:
    repair_state = state.get("verification_repair_state") if isinstance(state, dict) else None
    if not isinstance(repair_state, dict) or repair_state.get("run_id") != run_id:
        return 0
    return _int_state(repair_state.get("attempts"))


def _route_attempts_for_run(state: Any, run_id: str, reason: str) -> int:
    route = state.get("completion_route") if isinstance(state, dict) else None
    if not isinstance(route, dict):
        return 0
    if route.get("run_id") != run_id or route.get("reason") != reason:
        return 0
    return _int_state(route.get("attempts"))


def _missing_required_tools(
    messages: list[Any],
    required_tools: tuple[str, ...],
    run_id: str,
) -> list[str]:
    if not required_tools:
        return []
    turn_messages = messages
    for index in range(len(messages) - 1, -1, -1):
        kwargs = getattr(messages[index], "additional_kwargs", None)
        if (
            isinstance(kwargs, dict)
            and kwargs.get("runtime_kind") == "task_input"
            and (not run_id or kwargs.get("runtime_run_id") == run_id)
        ):
            turn_messages = messages[index + 1 :]
            break
    completed: set[str] = set()
    for message in turn_messages:
        if not isinstance(message, ToolMessage):
            continue
        name = str(getattr(message, "name", "") or "")
        # A denied or failed paid image request still satisfies the requirement
        # to attempt the selected tool. Other completion checks prevent a false
        # success claim without automatically repeating a paid side effect.
        if name in required_tools:
            completed.add(name)
    return [name for name in required_tools if name not in completed]


def _parse_tool_content(content: Any) -> Any:
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return None
    return None


def _verification_reason(payload: dict[str, Any]) -> str:
    results = payload.get("results")
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict) or _truthy(item.get("ok")):
                continue
            detail = item.get("detail")
            if isinstance(detail, str) and detail.strip():
                return detail.strip()
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        return error.strip()
    return "task.verify returned ok=false"


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "ok", "passed"}
    if isinstance(value, (int, float)):
        return value != 0
    return bool(value)


def _int_state(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
