"""Build a redacted Trace projection from Runtime-owned durable records."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def build_run_trace(
    run: dict[str, Any],
    *,
    model_calls: list[dict[str, Any]],
    tool_receipts: list[dict[str, Any]],
    child_runs: list[dict[str, Any]],
    checkpoint: dict[str, Any] | None,
    event_count: int,
) -> dict[str, Any]:
    run_id = str(run["id"])
    root_id = f"span:run:{run_id}"
    terminal = str(run.get("status") or "") in {"completed", "failed", "canceled"}
    run_end = (
        run.get("completed_at") or run.get("canceled_at") or run.get("updated_at")
        if terminal
        else None
    )
    spans: list[dict[str, Any]] = [
        _span(
            span_id=root_id,
            parent_id=None,
            kind="run",
            name="agent.run",
            status=str(run.get("status") or "unknown"),
            started_at=str(run["created_at"]),
            ended_at=run_end,
            attributes={
                "event_count": event_count,
                "model_call_count": len(model_calls),
                "tool_call_count": len(tool_receipts),
                "child_run_count": len(child_runs),
            },
        )
    ]
    model_spans: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for call in model_calls:
        span = _span(
            span_id=f"span:model:{call['id']}",
            parent_id=root_id,
            kind="model",
            name=str(call.get("purpose") or "model"),
            status=str(call.get("status") or "unknown"),
            started_at=str(call["created_at"]),
            ended_at=call.get("completed_at"),
            attributes={
                "model": str(call.get("model") or ""),
                "call_index": int(call.get("call_index") or 0),
                "input_tokens": call.get("input_tokens"),
                "output_tokens": call.get("output_tokens"),
                "output_started": bool(call.get("output_started")),
                "error_code": call.get("error_code"),
            },
        )
        spans.append(span)
        model_spans.append((call, span))
    for receipt in tool_receipts:
        parent_id = root_id
        candidates = [
            item
            for item in model_spans
            if item[0].get("execution_attempt_id") == receipt.get("execution_attempt_id")
            and str(item[0].get("created_at") or "") <= str(receipt.get("created_at") or "")
        ]
        if candidates:
            parent_id = max(candidates, key=lambda item: str(item[0].get("created_at") or ""))[1][
                "id"
            ]
        tool_name = str(receipt.get("tool_name") or "unknown")
        spans.append(
            _span(
                span_id=f"span:tool:{receipt['operation_id']}",
                parent_id=parent_id,
                kind="subagent" if tool_name == "task" else "tool",
                name=tool_name,
                status=str(receipt.get("status") or "unknown"),
                started_at=str(receipt.get("started_at") or receipt["created_at"]),
                ended_at=receipt.get("completed_at"),
                attributes={
                    "tool_call_id": str(receipt.get("tool_call_id") or ""),
                    "tool_version": str(receipt.get("tool_version") or ""),
                    "arguments_hash": str(receipt.get("arguments_hash") or ""),
                    "result_hash": receipt.get("result_hash"),
                    "risk": str(receipt.get("risk") or ""),
                    "attempt_count": int(receipt.get("attempt_count") or 0),
                    "error_type": receipt.get("error_type"),
                },
            )
        )
    for child in child_runs:
        child_terminal = str(child.get("status") or "") in {"completed", "failed", "canceled"}
        spans.append(
            _span(
                span_id=f"span:child-run:{child['id']}",
                parent_id=root_id,
                kind="subagent",
                name="child_run",
                status=str(child.get("status") or "unknown"),
                started_at=str(child["created_at"]),
                ended_at=(
                    child.get("completed_at") or child.get("updated_at") if child_terminal else None
                ),
                attributes={"run_id": str(child["id"])},
            )
        )
    if checkpoint:
        spans.append(
            _span(
                span_id=f"span:checkpoint:{checkpoint['id']}",
                parent_id=root_id,
                kind="checkpoint",
                name=str(checkpoint.get("reason") or "checkpoint"),
                status="completed",
                started_at=checkpoint.get("created_at"),
                ended_at=checkpoint.get("created_at"),
                attributes={
                    "step": int(checkpoint.get("step") or 0),
                    "messages_count": int(checkpoint.get("messages_count") or 0),
                },
            )
        )
    if terminal:
        spans.append(
            _span(
                span_id=f"span:terminal:{run_id}",
                parent_id=root_id,
                kind="terminal",
                name=f"run.{run['status']}",
                status=str(run["status"]),
                started_at=run_end,
                ended_at=run_end,
                attributes={},
            )
        )
    return {"root_span_id": root_id, "spans": spans}


def _span(
    *,
    span_id: str,
    parent_id: str | None,
    kind: str,
    name: str,
    status: str,
    started_at: str | None,
    ended_at: str | None,
    attributes: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": span_id,
        "parent_id": parent_id,
        "kind": kind,
        "name": name,
        "status": status,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": _duration_ms(started_at, ended_at),
        "attributes": {key: value for key, value in attributes.items() if value is not None},
    }


def _duration_ms(started_at: str | None, ended_at: str | None) -> float | None:
    if not started_at or not ended_at:
        return None
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        ended = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return round(max(0.0, (ended - started).total_seconds() * 1000), 3)
