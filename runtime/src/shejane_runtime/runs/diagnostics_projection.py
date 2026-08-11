"""Project durable Run state into diagnostics policy, checkpoint, and handoff summaries."""

from __future__ import annotations

import json
import logging
from typing import Any

from langgraph.graph import add_messages

from ..agent.subagents import SUBAGENT_MODEL_CALL_LIMIT
from ..build_info import runtime_build_identity
from ..failure_policy import classify_failure_payload
from ..progress_ledger import latest_feature_ledger as latest_feature_ledger
from ..progress_ledger import progress_ledger_state
from ..run_configuration import (
    RUNTIME_PROTOCOL_VERSION,
    _execution_policy_snapshot,
)

log = logging.getLogger("shejane_runtime.server")

_HANDOFF_STATUSES = {"completed", "failed", "canceled", "waiting_permission", "waiting_input"}


def diagnostics_execution_policy(run: dict[str, Any]) -> dict[str, Any]:
    try:
        settings = json.loads(str(run.get("settings_json") or "{}"))
    except json.JSONDecodeError:
        settings = {}
    if not isinstance(settings, dict):
        settings = {}
    policy = _execution_policy_snapshot(str(run.get("goal") or ""), settings)
    plan_mode = str(settings.get("plan_first") or "off")
    if plan_mode not in {"off", "auto", "always"}:
        plan_mode = "off"
    subagents_enabled = settings.get("subagents") is not False
    subagent_allowed = bool(policy["subagent_allowed"] and subagents_enabled)
    return {
        "complexity": policy["complexity"],
        "plan_mode": plan_mode,
        "plan_required": plan_mode == "always"
        or (plan_mode == "auto" and policy["complexity"] == "complex"),
        "subagent_allowed": subagent_allowed,
        "reason": "subagents_disabled" if not subagents_enabled else policy["reason"],
        "max_model_calls": policy["max_model_calls"],
        "soft_model_call_limit": policy["soft_model_call_limit"],
        "final_model_call_reserve": policy["final_model_call_reserve"],
        "subagent_budget_mode": policy["subagent_budget_mode"],
        "max_subagent_tasks": None,
        "preferred_subagent_concurrency": (
            policy["preferred_subagent_concurrency"] if subagent_allowed else 0
        ),
        "max_concurrent_subagent_tasks": (
            policy["max_concurrent_subagent_tasks"] if subagent_allowed else 0
        ),
        "max_subagent_model_calls": SUBAGENT_MODEL_CALL_LIMIT if subagent_allowed else 0,
    }


def diagnostics_build(run: dict[str, Any]) -> dict[str, Any]:
    try:
        settings = json.loads(str(run.get("settings_json") or "{}"))
    except json.JSONDecodeError:
        settings = {}
    stored = settings.get("_diagnostics_build") if isinstance(settings, dict) else None
    return (
        stored
        if isinstance(stored, dict)
        else runtime_build_identity(protocol_version=RUNTIME_PROTOCOL_VERSION)
    )


def build_diagnostics_handoff(
    run: dict[str, Any],
    events: list[dict[str, Any]],
    permissions: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    status = str(run.get("status") or "unknown")
    event_count = len(events)
    artifact_count = len(artifacts)
    pending_permissions = [p for p in permissions if p.get("status") == "pending"]
    recent_event_types = [str(e.get("event_type") or "") for e in events[-8:]]
    recent_event_types = [e for e in recent_event_types if e]
    ledger_state, ledger_message = progress_ledger_state(events, artifacts)
    verification = latest_task_verification(events)
    failure = latest_failure_classification(events, run_status=status, verification=verification)

    blockers: list[str] = []
    if pending_permissions:
        names = sorted({str(p.get("tool_name") or "tool") for p in pending_permissions})
        blockers.append(f"Waiting for permission: {', '.join(names)}")

    if failure:
        blockers.append(failure_blocker(failure))
    if verification and verification["status"] == "failed":
        blockers.append(f"Latest task.verify failed: {verification.get('reason') or 'unknown'}")

    if status == "completed":
        headline = f"Run completed with {event_count} events and {artifact_count} artifacts."
        next_actions = ["Review the final answer and any listed artifacts."]
    elif status == "waiting_permission" or pending_permissions:
        headline = f"Run is waiting on {len(pending_permissions)} permission request(s)."
        next_actions = ["Approve or deny pending permission requests to continue the run."]
    elif status == "waiting_input":
        headline = "Run is waiting for user input."
        next_actions = ["Answer the pending question to continue the run."]
    elif status in {"queued", "running"}:
        headline = f"Run is {status} with {event_count} persisted events."
        next_actions = ["Reconnect to the stream or wait for the run to reach a terminal state."]
    elif status == "cleanup_required":
        headline = "Run is quarantined because execution cleanup could not yet be confirmed."
        blockers.append("The Runtime has not released this execution generation.")
        next_actions = [
            "Do not retry automatically; inspect Runtime diagnostics and cleanup state."
        ]
    elif status == "failed":
        headline = f"Run failed after {event_count} events."
        next_actions = ["Inspect blockers and recent failed events before retrying."]
    elif status == "canceled":
        headline = f"Run was canceled after {event_count} events."
        next_actions = ["Start a new run if the goal still needs work."]
    else:
        headline = f"Run status is {status} with {event_count} events."
        next_actions = ["Inspect recent events before resuming work."]

    if status in _HANDOFF_STATUSES and ledger_state != "fresh":
        if ledger_message:
            blockers.append(ledger_message)
        if ledger_state == "missing":
            next_actions.append(
                "Call task.progress with current acceptance criteria, decisions, risks, and next actions."
            )
        elif ledger_state == "stale":
            next_actions.append("Refresh task.progress before handing off or resuming this run.")

    if failure and failure["suggested_action"] not in next_actions:
        next_actions.append(failure["suggested_action"])
    if verification and verification["status"] == "failed":
        action = "Fix the failing verification, then rerun task.verify before final handoff."
        if action not in next_actions:
            next_actions.append(action)

    return {
        "status": status,
        "headline": headline,
        "next_actions": next_actions,
        "blockers": blockers,
        "recent_event_types": recent_event_types,
        "ledger_state": ledger_state,
        "ledger_message": ledger_message,
        "failure": failure,
        "verification": verification,
    }


def run_checkpoint_config(run: dict[str, Any]) -> dict[str, Any]:
    configurable = {"thread_id": str(run.get("graph_thread_id") or run["id"])}
    checkpoint_id = run.get("graph_checkpoint_id")
    if isinstance(checkpoint_id, str) and checkpoint_id:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


async def latest_checkpoint_summary(
    checkpointer: Any, run: dict[str, Any]
) -> dict[str, Any] | None:
    if checkpointer is None:
        return None
    run_id = str(run["id"])
    try:
        item = await run_checkpoint_tuple(checkpointer, run)
        if item is None:
            return None
        checkpoint = item.checkpoint if isinstance(item.checkpoint, dict) else {}
        metadata = item.metadata if isinstance(item.metadata, dict) else {}
        configurable = item.config.get("configurable", {})
        checkpoint_id = first_string(checkpoint.get("id"), configurable.get("checkpoint_id"))
        if not checkpoint_id:
            return None
        step = int_or_none(metadata.get("step"))
        reason = first_string(metadata.get("source"), metadata.get("reason"), "checkpoint")
        return {
            "id": checkpoint_id,
            "run_id": run_id,
            "step": step if step is not None else 0,
            "reason": reason or "checkpoint",
            "messages_count": await checkpoint_messages_count(checkpointer, item),
            "created_at": first_string(checkpoint.get("ts"), metadata.get("created_at")),
        }
    except Exception as exc:
        log.warning("latest checkpoint summary failed run_id=%s: %s", run_id, exc)
    return None


async def latest_checkpoint_reflection(
    checkpointer: Any, run: dict[str, Any]
) -> dict[str, Any] | None:
    if checkpointer is None:
        return None
    run_id = str(run["id"])
    try:
        item = await run_checkpoint_tuple(checkpointer, run)
        checkpoint = item.checkpoint if item is not None else None
        if not isinstance(checkpoint, dict):
            return None
        channel_values = checkpoint.get("channel_values")
        if not isinstance(channel_values, dict):
            return None
        return diagnostics_reflection(channel_values.get("reflection"))
    except Exception as exc:
        log.warning("latest checkpoint reflection failed run_id=%s: %s", run_id, exc)
    return None


async def run_checkpoint_tuple(checkpointer: Any, run: dict[str, Any]) -> Any | None:
    config = run_checkpoint_config(run)
    if run.get("graph_checkpoint_id") and hasattr(checkpointer, "aget_tuple"):
        return await checkpointer.aget_tuple(config)
    if hasattr(checkpointer, "alist"):
        async for item in checkpointer.alist(config, limit=1):
            return item
    return None


async def checkpoint_messages_count(checkpointer: Any, item: Any) -> int:
    checkpoint = item.checkpoint if isinstance(item.checkpoint, dict) else {}
    channel_values = checkpoint.get("channel_values")
    if not isinstance(channel_values, dict):
        return 0
    messages = channel_values.get("messages")
    if isinstance(messages, list):
        return len(messages)

    get_history = getattr(checkpointer, "aget_delta_channel_history", None)
    if not callable(get_history) or "messages" not in checkpoint.get("channel_versions", {}):
        return 0
    history = await get_history(config=item.config, channels=["messages"])
    entry = history.get("messages") if isinstance(history, dict) else None
    if not isinstance(entry, dict):
        return 0
    seed = entry.get("seed", [])
    current = getattr(seed, "value", seed)
    if not isinstance(current, list):
        return 0
    for write in entry.get("writes", []):
        if isinstance(write, (list, tuple)) and len(write) == 3:
            current = add_messages(current, write[2])
    return len(current)


def diagnostics_reflection(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for key in ("ai_messages", "tool_results", "final_answer_chars"):
        parsed = int_or_none(value.get(key))
        if parsed is not None:
            out[key] = parsed
    critic = diagnostics_reflection_critic(value.get("critic"))
    if critic:
        out["critic"] = critic
    return out or None


def diagnostics_reflection_critic(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for key in ("coverage", "clarity", "grounding"):
        parsed = int_or_none(value.get(key))
        if parsed is not None:
            out[key] = parsed
    notes = value.get("notes")
    if isinstance(notes, list):
        compact_notes = [
            note.strip()[:300] for note in notes[:3] if isinstance(note, str) and note.strip()
        ]
        if compact_notes:
            out["notes"] = compact_notes
    raw = first_string(value.get("raw"))
    if raw:
        out["raw"] = raw[:1000]
    return out or None


def latest_failure_classification(
    events: list[dict[str, Any]],
    *,
    run_status: str | None = None,
    verification: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if run_status == "completed" and (not verification or verification.get("status") != "failed"):
        return None
    for event in reversed(events):
        event_type = event.get("event_type")
        if event_type not in {"run.failed", "tool.failed"}:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        if (
            event_type == "tool.failed"
            and is_task_verify_payload(payload)
            and verification
            and verification.get("status") == "passed"
        ):
            continue
        return classify_failure_payload(str(event_type), payload)
    return None


def latest_task_verification(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        event_type = event.get("event_type")
        if event_type not in {"tool.completed", "tool.failed"}:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or not is_task_verify_payload(payload):
            continue
        parsed = parse_tool_content(payload.get("content"))
        if not isinstance(parsed, dict):
            parsed = {}
        status = (
            "passed" if event_type == "tool.completed" and truthy(parsed.get("ok")) else "failed"
        )
        return {
            "status": status,
            "reason": verification_reason(parsed),
            "pass_count": int_or_none(parsed.get("pass_count")),
            "fail_count": int_or_none(parsed.get("fail_count")),
            "source_event_type": str(event_type),
        }
    return None


def is_task_verify_payload(payload: dict[str, Any]) -> bool:
    return str(payload.get("tool") or payload.get("name") or "") == "task.verify"


def parse_tool_content(content: Any) -> Any:
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return None
    return None


def verification_reason(payload: dict[str, Any]) -> str | None:
    results = payload.get("results")
    if isinstance(results, list):
        failed_details: list[str] = []
        passed_details: list[str] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            detail = item.get("detail")
            if not isinstance(detail, str) or not detail.strip():
                continue
            if truthy(item.get("ok")):
                passed_details.append(detail.strip())
            else:
                failed_details.append(detail.strip())
        if failed_details:
            return failed_details[0]
        if passed_details:
            return passed_details[0]
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        return error.strip()
    return None


def failure_blocker(failure: dict[str, Any]) -> str:
    code = failure.get("code")
    label = f"{failure.get('category')}: {code}" if code else str(failure.get("category"))
    tool = failure.get("tool")
    if tool:
        return f"{tool}: {label}"
    return label


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "ok", "passed"}
    if isinstance(value, (int, float)):
        return value != 0
    return bool(value)


def int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
