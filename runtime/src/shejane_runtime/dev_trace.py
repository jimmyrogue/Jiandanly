"""Terminal-only development trace for Client-visible Agent progress."""

from __future__ import annotations

import os
import re
from typing import Any

_MAX_TEXT_CHARS = 8_000
_TRUE_VALUES = {"1", "true", "yes"}
_TOOL_EVENTS = {"tool.requested", "tool.completed", "tool.failed", "tool.canceled"}
_RUN_EVENTS = {
    "run.started",
    "run.waiting",
    "run.completed",
    "run.failed",
    "run.canceled",
    "run.cleanup_required",
    "repair.workflow",
}
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
_URL_CREDENTIAL_RE = re.compile(r"(?i)(https?://)[^/@\s]+@")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_seen_event_ids: set[str] = set()


def is_dev_trace_enabled() -> bool:
    return os.environ.get("SHEJANE_DEV_TRACE", "").lower() in _TRUE_VALUES


def trace_stream_event(event: dict[str, Any]) -> None:
    """Mirror each printable P4 event once, even with multiple SSE subscribers."""
    if not is_dev_trace_enabled():
        return
    run_id = str(event.get("run_id") or "unknown")
    event_type = str(event.get("event_type") or "")
    payload = event.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    if event_type != "assistant.round.committed" and event_type not in (
        _TOOL_EVENTS | _RUN_EVENTS | {"llm.error"}
    ):
        return
    event_id = str(event.get("id") or "")
    if event_id and event_id in _seen_event_ids:
        return
    if event_id:
        _seen_event_ids.add(event_id)
    if event_type == "assistant.round.committed":
        trace_assistant_round(run_id, payload)
    else:
        trace_run_event(run_id, event_type, payload)


def trace_assistant_round(run_id: str, payload: dict[str, Any]) -> None:
    """Print only summaries and text already eligible for Client display."""
    if not is_dev_trace_enabled():
        return
    reasoning = _safe_text(payload.get("reasoning_summary"))
    text = _safe_text(payload.get("text"))
    if reasoning:
        _write_line(f"[agent][{run_id}] reasoning: {reasoning}")
    if text:
        _write_line(f"[agent][{run_id}] assistant: {text}")


def trace_run_event(run_id: str, event_type: str, payload: dict[str, Any]) -> None:
    """Print bounded metadata without tool arguments, results, prompts, or raw errors."""
    if not is_dev_trace_enabled():
        return
    if event_type not in _TOOL_EVENTS | _RUN_EVENTS | {"llm.error"}:
        return
    fields = _event_fields(payload)
    details = " ".join(f"{key}={value}" for key, value in fields.items())
    suffix = f" {details}" if details else ""
    _write_line(f"[agent][{run_id}] {event_type}{suffix}")


def _event_fields(payload: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    tool = payload.get("tool") or payload.get("name") or payload.get("tool_name")
    if tool:
        fields["tool"] = _safe_text(tool)
    for key in (
        "status",
        "error_code",
        "code",
        "category",
        "failure_category",
        "recoverable",
        "retryable",
        "action_kind",
        "recovery_action",
        "attempt",
        "max_attempts",
    ):
        value = payload.get(key)
        if value is None:
            continue
        fields[key] = _safe_text(value) if isinstance(value, str) else value
    return fields


def _write_line(line: str) -> None:
    fd_value = os.environ.get("SHEJANE_DEV_TRACE_FD", "")
    try:
        fd = int(fd_value)
    except ValueError:
        return
    try:
        os.write(fd, f"{line}\n".encode("utf-8", errors="replace"))
    except OSError:
        pass


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _URL_CREDENTIAL_RE.sub(r"\1[REDACTED]@", text)
    text = _CONTROL_RE.sub(_escape_control, text)
    if len(text) > _MAX_TEXT_CHARS:
        return f"{text[:_MAX_TEXT_CHARS]}... [truncated]"
    return text


def _escape_control(match: re.Match[str]) -> str:
    char = match.group(0)
    return {"\n": r"\n", "\r": r"\r", "\t": r"\t"}.get(char, f"\\x{ord(char):02x}")
