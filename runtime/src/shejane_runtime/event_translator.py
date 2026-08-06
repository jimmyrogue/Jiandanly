"""Translate LangGraph stream events into client-friendly SSE event types.

LangGraph's v2 stream returns typed parts whose `data` shape depends on mode:

  * messages → (AIMessageChunk | ToolMessageChunk | …, metadata: dict)
  * updates  → {node_name: state_delta}
  * custom   → whatever the writer pushed

For the SSE protocol the client consumes we want stable, narrow event
names that don't leak LangGraph internals. This module is the single
place that translation happens.

Client SSE event types — names MUST match what the TypeScript
streamTransport.ts and chatStore.ts switch on (see
client/src/shared/streaming/streamTransport.ts:61 and
client/src/features/chat/chatStore.ts):

  llm.delta            — one streamed token of assistant content (was
                         `llm.token` pre-Block-3 — client looks for
                         `llm.delta`)
  llm.tool_call_chunk  — partial JSON args for a tool call being assembled
  tool.requested       — tool about to run (emitted when the model's
                         tool_call_chunk finalizes with a name)
  tool.completed       — tool result observed (was `tool.end` pre-Block-3
                         — client looks for `tool.completed`)
  tool.failed          — tool result observed with status="error"
  agent.custom         — middleware-emitted custom payload

Subagent lifecycle is deliberately absent here. Runtime projects it from the
durable ``task`` Tool Receipt transitions; stream chunks are neither complete
nor replayable enough to own lifecycle state.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from langchain_core.load.dump import dumps as lc_dumps
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from .failure_policy import classify_failure_payload
from .tool_outcomes import tool_result_envelope, tool_result_envelope_failed


def translate(kind: str, payload: Any) -> list[dict[str, Any]]:
    """Return zero or more `{event, data}` dicts for a single stream tuple.

    Unknown graph internals stay in checkpoints and traces instead of the
    stable product event stream.
    """
    if kind == "messages":
        return _translate_messages(payload)
    if kind == "updates":
        return _translate_updates(payload)
    if kind == "custom":
        if (
            isinstance(payload, dict)
            and payload.get("event") in {"tool.progress", "permission.auto_approved"}
            and isinstance(payload.get("data"), dict)
        ):
            return [{"event": payload["event"], "data": _safe_dump(payload["data"])}]
        return [
            {"event": "agent.custom", "data": _safe_dump(payload)},
        ]
    return []


# ---- messages mode ----


def _translate_messages(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, tuple) or len(payload) != 2:
        return []
    chunk, _metadata = payload

    if isinstance(chunk, AIMessageChunk):
        out: list[dict[str, Any]] = []
        content = chunk.content
        if isinstance(content, str) and content:
            # `llm.delta` is the canonical content-stream event; the
            # client's streamAgentSSE() reads `payload.content` to
            # append to the in-flight assistant message bubble.
            out.append({"event": "llm.delta", "data": {"content": content}})

        usage = chunk.additional_kwargs.get("usage")
        if not isinstance(usage, dict):
            usage = chunk.usage_metadata
        if isinstance(usage, dict):
            # Ephemeral per-call usage for the live chip. The authoritative
            # run total comes from the durable model-call ledger, not SSE.
            out.append(
                {
                    "event": "llm.usage",
                    "data": {
                        "input_tokens": usage.get("input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0),
                    },
                }
            )

        for tc in chunk.tool_call_chunks or []:
            tool_name = tc.get("name")
            out.append(
                {
                    "event": "llm.tool_call_chunk",
                    "data": {
                        "id": tc.get("id"),
                        "name": tool_name,
                        "args_delta": tc.get("args"),
                        "index": tc.get("index"),
                    },
                }
            )

        # `tool.requested` (pre-call signal) is best emitted from a
        # `wrap_tool_call` middleware hook in `agent/builder.py` so we
        # can distinguish the truly final tool_call from partial
        # streaming chunks — AIMessageChunk surfaces `tool_calls` for
        # any chunk where it can parse a name, which floods the wire
        # with spam mid-args. Deferred to a follow-up block; the client
        # falls back to `tool.completed` for the "tool ran" headline.

        backend_error = chunk.additional_kwargs.get("backend_error")
        if backend_error:
            out.append({"event": "llm.error", "data": {"message": str(backend_error)}})

        return out

    if isinstance(chunk, ToolMessage):
        # `tool.completed` is the canonical name the client expects
        # (chatStore.ts:155). Erroring tool messages get split to
        # `tool.failed` so the UI can render them differently. Some tools
        # return a structured `{ok:false, ...}` envelope without setting
        # ToolMessage.status="error"; treat those as failures too so
        # diagnostics and retry policy see the real outcome.
        status = getattr(chunk, "status", None)
        envelope = tool_result_envelope(chunk.content)
        is_failed = status == "error" or tool_result_envelope_failed(envelope)
        if is_failed:
            event_name = "tool.failed"
        else:
            event_name = "tool.completed"
        data = {
            "tool_call_id": chunk.tool_call_id,
            "name": chunk.name,
            "tool": chunk.name,  # client uses `tool` key for headline
            "content": _stringify(chunk.content),
            "status": "error" if is_failed else (status or "ok"),
        }
        if isinstance(envelope, dict):
            _merge_tool_failure_envelope(data, envelope)
        events = [
            {
                "event": event_name,
                "data": data,
            }
        ]
        if not is_failed and isinstance(envelope, dict):
            events.extend(_artifact_events(envelope, tool_name=chunk.name))
        return events

    return []


# ---- updates mode ----

#  Updates payload is roughly `{node_name: {state_field: value, …}}`.


def _translate_updates(payload: Any) -> list[dict[str, Any]]:
    """Extract stable tool requests and discard raw node deltas."""
    if not isinstance(payload, dict):
        return []

    out: list[dict[str, Any]] = []
    for delta in payload.values():
        # When the model node finishes a turn, its delta carries an
        # AIMessage with the fully assembled `tool_calls` list. Emit one
        # `tool.requested` event per tool_call so the client renderer can
        # show rich per-tool info ("搜索 · 普吉岛雨季天气", "读取 · App.tsx").
        #
        # Why here and not in `_translate_messages`: AIMessageChunk
        # surfaces tool_calls for any chunk where it can parse a name,
        # which floods the wire with spam mid-args. The updates-mode
        # delta fires once per model turn with the assembled tool_calls
        # — a clean single-emit point. Same "single source of truth"
        # discipline that fixed the duplicate-tool.completed bug.
        out.extend(_tool_requested_events_from_update(delta))
    return out


def _tool_requested_events_from_update(delta: Any) -> list[dict[str, Any]]:
    """Pull `tool.requested` events out of an update delta. Each event
    carries `{tool_call_id, name, tool, args}` for one finalized tool
    call. Returns [] when the delta has no AIMessage with tool_calls."""
    if not isinstance(delta, dict):
        return []
    messages = delta.get("messages")
    if not isinstance(messages, list):
        return []
    out: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for tool_call in getattr(message, "tool_calls", None) or []:
            if not isinstance(tool_call, dict):
                continue
            name = str(tool_call.get("name") or "")
            if not name:
                continue
            args = tool_call.get("args")
            if not isinstance(args, dict):
                args = {}
            out.append(
                {
                    "event": "tool.requested",
                    # Field name `arguments` (not `args`) matches the wire
                    # convention used by the existing HITL permission flow
                    # — the client renderer reads payload.arguments in
                    # chatStore.ts:toolDetail. Keeping one field name
                    # across paths saves the client from having to
                    # normalize.
                    "data": {
                        "tool_call_id": str(tool_call.get("id") or ""),
                        "name": name,
                        "tool": name,  # alias the renderer uses for headlines
                        "arguments": args,
                    },
                }
            )
    return out


# ---- helpers ----


def _safe_dump(payload: Any) -> Any:
    try:
        return json.loads(lc_dumps(payload))
    except Exception:
        try:
            return json.loads(json.dumps(payload, default=str))
        except Exception:
            return {"repr": str(payload)}


def _stringify(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
            else:
                parts.append(str(part))
        return "".join(parts)
    return str(content)


def _artifact_events(envelope: dict[str, Any], *, tool_name: str | None) -> list[dict[str, Any]]:
    artifacts = envelope.get("artifacts")
    if not isinstance(artifacts, list):
        return []
    events: list[dict[str, Any]] = []
    for artifact in artifacts[:128]:
        if not isinstance(artifact, dict):
            continue
        artifact_id = artifact.get("artifact_id")
        title = artifact.get("name")
        if not isinstance(artifact_id, str) or not artifact_id:
            continue
        data: dict[str, Any] = {
            "artifact_id": artifact_id,
            "title": title if isinstance(title, str) and title else artifact_id,
            "tool": tool_name,
        }
        for key in ("media_type", "size_bytes", "sha256"):
            value = artifact.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                data[key] = value
        events.append({"event": "artifact.created", "data": data})
    return events


def _merge_tool_failure_envelope(data: dict[str, Any], envelope: dict[str, Any]) -> None:
    error_code = envelope.get("errorCode") or envelope.get("error_code") or envelope.get("code")
    if error_code:
        data["error_code"] = str(error_code)
    recoverable = envelope.get("recoverable")
    if isinstance(recoverable, bool):
        data["recoverable"] = recoverable
    retryable = envelope.get("retryable")
    if isinstance(retryable, bool):
        data["retryable"] = retryable
    elif isinstance(recoverable, bool):
        data["retryable"] = (
            recoverable
            and classify_failure_payload(
                "tool.failed",
                {
                    "error_code": str(error_code or ""),
                    "content": data["content"],
                    "recoverable": recoverable,
                },
            )["retryable"]
        )
    message = envelope.get("message") or envelope.get("error") or envelope.get("content")
    if isinstance(message, str) and message.strip():
        data["message"] = message.strip()


def translate_many(events: Iterable[tuple[str, Any]]) -> list[dict[str, Any]]:
    """Convenience: translate a batch of (mode, payload) tuples."""
    out: list[dict[str, Any]] = []
    for kind, payload in events:
        out.extend(translate(kind, payload))
    return out
