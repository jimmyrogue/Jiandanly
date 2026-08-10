"""Terminal-only development trace for Client-visible Agent progress."""

from __future__ import annotations

import os
import queue
import re
import threading
from collections import OrderedDict
from typing import Any

_MAX_TEXT_CHARS = 8_000
_MAX_DELTA_BUFFER_CHARS = 64_000
_MAX_SEEN_DURABLE_EVENT_IDS = 8_192
_WRITE_QUEUE_SIZE = 256
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
_seen_event_ids: OrderedDict[str, None] = OrderedDict()
_delta_buffers: dict[tuple[str, str], str] = {}
_rounds_with_deltas: set[tuple[str, str]] = set()
_write_queue: queue.Queue[tuple[int, bytes]] = queue.Queue(maxsize=_WRITE_QUEUE_SIZE)
_writer_lock = threading.Lock()
_writer_started = False


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
    if event_type not in (
        _TOOL_EVENTS
        | _RUN_EVENTS
        | {"assistant.round.committed", "llm.delta", "llm.error", "llm.round.closed"}
    ):
        return
    event_id = str(event.get("id") or "")
    dedupe_event = event.get("seq") is not None
    if dedupe_event and event_id and event_id in _seen_event_ids:
        _seen_event_ids.move_to_end(event_id)
        return
    if dedupe_event and event_id:
        _seen_event_ids[event_id] = None
        if len(_seen_event_ids) > _MAX_SEEN_DURABLE_EVENT_IDS:
            _seen_event_ids.popitem(last=False)
    if event_type == "llm.delta":
        _trace_delta(run_id, payload)
    elif event_type == "llm.round.closed":
        _close_round(run_id, payload)
    elif event_type == "assistant.round.committed":
        trace_assistant_round(run_id, payload)
    else:
        if event_type in {"run.completed", "run.failed", "run.canceled", "run.cleanup_required"}:
            _flush_run(run_id, clear_rounds=True)
        elif event_type in {"run.waiting", "repair.workflow"}:
            _flush_run(run_id, clear_rounds=False)
        trace_run_event(run_id, event_type, payload)


def trace_assistant_round(run_id: str, payload: dict[str, Any]) -> None:
    """Print only summaries and text already eligible for Client display."""
    if not is_dev_trace_enabled():
        return
    key = _round_key(run_id, payload)
    had_deltas = key in _rounds_with_deltas
    if had_deltas:
        _flush_delta(key)
    reasoning = _safe_text(payload.get("reasoning_summary"))
    text = _safe_text(payload.get("text"))
    if reasoning:
        _write_line(f"[agent][{run_id}] reasoning: {reasoning}")
    if text and not had_deltas:
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
        "error_type",
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


def _trace_delta(run_id: str, payload: dict[str, Any]) -> None:
    content = str(payload.get("content") or "")
    if not content:
        return
    key = _round_key(run_id, payload)
    _rounds_with_deltas.add(key)
    buffered = _delta_buffers.get(key, "")
    remaining = _MAX_DELTA_BUFFER_CHARS - len(buffered)
    if remaining > 0:
        _delta_buffers[key] = f"{buffered}{content[:remaining]}"


def _close_round(run_id: str, payload: dict[str, Any]) -> None:
    key = _round_key(run_id, payload)
    _flush_delta(key)
    _rounds_with_deltas.discard(key)


def _flush_run(run_id: str, *, clear_rounds: bool) -> None:
    keys = {candidate for candidate in _rounds_with_deltas if candidate[0] == run_id}
    keys.update(candidate for candidate in _delta_buffers if candidate[0] == run_id)
    for key in keys:
        _flush_delta(key)
        if clear_rounds:
            _rounds_with_deltas.discard(key)


def _flush_delta(key: tuple[str, str]) -> None:
    text = _safe_text(_delta_buffers.pop(key, ""))
    if text:
        _write_line(f"[agent][{key[0]}] assistant: {text}")


def _round_key(run_id: str, payload: dict[str, Any]) -> tuple[str, str]:
    return run_id, str(payload.get("round_id") or "unscoped")


def _write_line(line: str) -> None:
    fd_value = os.environ.get("SHEJANE_DEV_TRACE_FD", "")
    try:
        fd = int(fd_value)
    except ValueError:
        return
    payload = f"{line}\n".encode("utf-8", errors="replace")
    if os.environ.get("SHEJANE_DEV_TRACE_SYNC") == "1":
        _write_payload(fd, payload)
        return
    _ensure_writer()
    try:
        _write_queue.put_nowait((fd, payload))
    except queue.Full:
        pass


def _ensure_writer() -> None:
    global _writer_started
    if _writer_started:
        return
    with _writer_lock:
        if _writer_started:
            return
        threading.Thread(target=_writer_loop, name="dev-trace-writer", daemon=True).start()
        _writer_started = True


def _writer_loop() -> None:
    while True:
        fd, payload = _write_queue.get()
        try:
            _write_payload(fd, payload)
        finally:
            _write_queue.task_done()


def _write_payload(fd: int, payload: bytes) -> None:
    try:
        os.write(fd, payload)
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
