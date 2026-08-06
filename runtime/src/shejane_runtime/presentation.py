"""Read-time projection of durable execution facts into user-facing items."""

from __future__ import annotations

import json
from typing import Any

_TOOL_STATUS = {
    "prepared": "pending",
    "running": "in_progress",
    "paused": "waiting",
    "completed": "completed",
    "failed": "failed",
    "rejected": "failed",
    "canceled": "canceled",
    "outcome_unknown": "unknown",
}


def project_run_presentation(
    *,
    run: dict[str, Any],
    assistant_item: dict[str, Any] | None,
    events: list[dict[str, Any]],
    tool_receipts: list[dict[str, Any]],
    event_high_watermark: int,
    wait_candidates: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a deterministic projection without persisting a second source of truth."""
    items: list[dict[str, Any]] = []
    events_by_tool_call: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if str(payload.get("execution_namespace") or "main") != "main":
            continue
        tool_call_id = str(payload.get("tool_call_id") or "")
        if tool_call_id:
            events_by_tool_call.setdefault(tool_call_id, []).append(event)
        if event.get("event_type") != "assistant.round.committed":
            continue
        round_id = str(payload.get("round_id") or "")
        text = str(payload.get("text") or "").strip()
        tool_call_ids = payload.get("tool_call_ids")
        if not round_id or not isinstance(tool_call_ids, list) or not tool_call_ids:
            continue
        seq = int(event["seq"])
        reasoning_summary = str(payload.get("reasoning_summary") or "").strip()
        if reasoning_summary:
            items.append(
                {
                    "id": f"round:{round_id}:reasoning",
                    "kind": "reasoning_summary",
                    "status": "completed",
                    "order": {"event_seq": seq, "slot": 0},
                    "revision": seq,
                    "source": {"kind": "run_event", "id": str(event["id"])},
                    "summary": reasoning_summary,
                    "created_at": str(event["created_at"]),
                }
            )
        if text:
            items.append(
                {
                    "id": f"round:{round_id}:progress",
                    "kind": "progress",
                    "status": "completed",
                    "order": {"event_seq": seq, "slot": 1 if reasoning_summary else 0},
                    "revision": seq,
                    "source": {"kind": "run_event", "id": str(event["id"])},
                    "text": text,
                    "created_at": str(event["created_at"]),
                }
            )

    receipted_tool_calls: set[str] = set()
    for receipt in tool_receipts:
        if str(receipt.get("execution_namespace") or "main") != "main":
            continue
        tool_call_id = str(receipt.get("tool_call_id") or "")
        matching = events_by_tool_call.get(tool_call_id, [])
        tool_name = str(receipt.get("tool_name") or "tool")
        requested = next(
            (
                event
                for event in matching
                if event.get("event_type")
                == ("subagent.spawned" if tool_name == "task" else "tool.requested")
            ),
            None,
        )
        if requested is None:
            continue
        receipted_tool_calls.add(tool_call_id)
        revision = max(int(event["seq"]) for event in matching)
        status = _TOOL_STATUS.get(str(receipt.get("status")), "unknown")
        common = {
            "id": f"tool-call:{tool_call_id}",
            "status": status,
            "order": {"event_seq": int(requested["seq"]), "slot": 0},
            "revision": revision,
            "source": {"kind": "tool_receipt", "id": str(receipt["operation_id"])},
            "operation_id": str(receipt["operation_id"]),
            "created_at": str(receipt["created_at"]),
            "updated_at": str(receipt["updated_at"]),
            "completed_at": receipt.get("completed_at"),
        }
        if tool_name == "task":
            arguments = _json_object(receipt.get("arguments_json"))
            requested_payload = requested.get("payload") or {}
            items.append(
                {
                    **common,
                    "kind": "subagent",
                    "subagent_type": str(arguments.get("subagent_type") or ""),
                    "description": str(requested_payload.get("description") or "")[:240],
                }
            )
            continue
        if tool_name == "task.verify":
            items.append({**common, "kind": "verification", "tool_name": tool_name})
            continue
        items.append(
            {
                "id": common["id"],
                "kind": "tool",
                "status": status,
                "order": common["order"],
                "revision": revision,
                "source": common["source"],
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "risk": str(receipt.get("risk") or "unknown"),
                "created_at": common["created_at"],
                "updated_at": common["updated_at"],
                "completed_at": common["completed_at"],
            }
        )

    legacy_status = {
        "tool.completed": "completed",
        "tool.failed": "failed",
        "tool.canceled": "canceled",
    }
    for tool_call_id, matching in events_by_tool_call.items():
        if tool_call_id in receipted_tool_calls:
            continue
        requested = next(
            (event for event in matching if event.get("event_type") == "tool.requested"),
            None,
        )
        if requested is None:
            continue
        latest = matching[-1]
        payload = requested.get("payload") or {}
        items.append(
            {
                "id": f"tool-call:{tool_call_id}",
                "kind": "tool",
                "status": legacy_status.get(str(latest.get("event_type")), "in_progress"),
                "order": {"event_seq": int(requested["seq"]), "slot": 0},
                "revision": int(latest["seq"]),
                "source": {"kind": "run_event", "id": str(requested["id"])},
                "tool_call_id": tool_call_id,
                "tool_name": str(payload.get("name") or payload.get("tool") or "tool"),
                "risk": "unknown",
                "created_at": str(requested["created_at"]),
                "updated_at": str(latest["created_at"]),
                "completed_at": (
                    str(latest["created_at"]) if latest.get("event_type") in legacy_status else None
                ),
            }
        )

    for candidate in wait_candidates or []:
        request_id = str(candidate.get("id") or "")
        matching = [
            event
            for event in events
            if isinstance(event.get("payload"), dict)
            and str(event["payload"].get("request_id") or "") == request_id
        ]
        if not request_id or not matching:
            continue
        first = matching[0]
        latest = matching[-1]
        kind = {
            "tool_review": "approval",
            "question": "question",
            "plan": "plan",
            "tool_reconciliation": "reconciliation",
        }.get(str(candidate.get("kind") or ""))
        if kind is None:
            continue
        payload = _json_object(candidate.get("payload_json"))
        event_payload = first.get("payload") or {}
        summary = str(
            payload.get("tool_name")
            or payload.get("summary")
            or event_payload.get("tool")
            or event_payload.get("summary")
            or kind
        )
        resolved = str(candidate.get("status") or "") == "resolved"
        items.append(
            {
                "id": f"wait:{request_id}",
                "kind": kind,
                "status": "completed" if resolved else "waiting",
                "order": {"event_seq": int(first["seq"]), "slot": 0},
                "revision": int(latest["seq"]),
                "source": {"kind": "wait_candidate", "id": request_id},
                "request_id": request_id,
                "summary": summary,
                "created_at": str(candidate["created_at"]),
                "updated_at": str(candidate.get("resolved_at") or latest["created_at"]),
                "completed_at": candidate.get("resolved_at"),
            }
        )

    events_by_artifact = {
        str(event["payload"].get("artifact_id")): event
        for event in events
        if event.get("event_type") == "artifact.created"
        and isinstance(event.get("payload"), dict)
        and event["payload"].get("artifact_id")
    }
    for artifact in artifacts or []:
        artifact_id = str(artifact.get("id") or "")
        event = events_by_artifact.get(artifact_id)
        if not artifact_id or event is None:
            continue
        seq = int(event["seq"])
        items.append(
            {
                "id": f"artifact:{artifact_id}",
                "kind": "artifact",
                "status": "completed",
                "order": {"event_seq": seq, "slot": 0},
                "revision": seq,
                "source": {"kind": "artifact", "id": artifact_id},
                "artifact_id": artifact_id,
                "title": str(artifact.get("title") or artifact_id),
                "content_type": str(artifact.get("content_type") or "application/octet-stream"),
                "created_at": str(artifact["created_at"]),
            }
        )

    terminal = next(
        (event for event in reversed(events) if event.get("event_type") == "run.completed"),
        None,
    )
    if terminal is not None and assistant_item is not None:
        content = str(assistant_item.get("content") or "").strip()
        if content:
            seq = int(terminal["seq"])
            items.append(
                {
                    "id": f"answer:{assistant_item['id']}",
                    "kind": "final_answer",
                    "status": "completed",
                    "order": {"event_seq": seq, "slot": 0},
                    "revision": seq,
                    "source": {"kind": "thread_item", "id": str(assistant_item["id"])},
                    "content": content,
                    "created_at": str(assistant_item["created_at"]),
                    "completed_at": str(
                        assistant_item.get("completed_at") or terminal["created_at"]
                    ),
                }
            )

    terminal_notice = next(
        (
            event
            for event in reversed(events)
            if event.get("event_type") in {"run.failed", "run.canceled", "run.cleanup_required"}
        ),
        None,
    )
    if terminal_notice is not None:
        payload = terminal_notice.get("payload") or {}
        event_type = str(terminal_notice["event_type"])
        status = {
            "run.failed": "failed",
            "run.canceled": "canceled",
            "run.cleanup_required": "unknown",
        }[event_type]
        seq = int(terminal_notice["seq"])
        items.append(
            {
                "id": f"notice:{terminal_notice['id']}",
                "kind": "notice",
                "status": status,
                "order": {"event_seq": seq, "slot": 0},
                "revision": seq,
                "source": {"kind": "run_event", "id": str(terminal_notice["id"])},
                "severity": "warning" if status == "canceled" else "error",
                "message": {
                    "run.failed": "Run failed",
                    "run.canceled": "Run canceled",
                    "run.cleanup_required": "Cleanup required",
                }[event_type],
                "created_at": str(terminal_notice["created_at"]),
            }
        )

    items.sort(key=lambda item: (item["order"]["event_seq"], item["order"]["slot"], item["id"]))
    return {
        "schema_version": 1,
        "run_id": str(run["id"]),
        "items": items,
        "event_high_watermark": event_high_watermark,
    }


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return value if isinstance(value, dict) else {}
