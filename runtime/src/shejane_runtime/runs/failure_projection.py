"""Run repair metadata and public failure-event projection."""

from __future__ import annotations

import re
from typing import Any

from ..failure_policy import classify_failure_payload
from ..llm.errors import ModelServiceError


def _repair_context_from_metadata(
    metadata: dict[str, Any],
    *,
    max_attempts: int,
) -> dict[str, Any] | None:
    if str(metadata.get("intent", "")).strip().lower() != "repair":
        return None
    return {
        "attempt": _positive_int(metadata.get("attempt"), default=1),
        "max_attempts": max(0, int(max_attempts)),
        "source_run_id": _non_empty_str(metadata.get("source_run_id")),
        "source_message_id": _non_empty_str(metadata.get("source_message_id")),
        "failure_category": _non_empty_str(metadata.get("failure_category")),
        "failure_action_kind": _non_empty_str(metadata.get("failure_action_kind")),
    }


def _retry_context_from_metadata(metadata: dict[str, Any]) -> dict[str, Any] | None:
    if str(metadata.get("intent", "")).strip().lower() != "retry":
        return None
    return {
        "attempt": _positive_int(metadata.get("attempt"), default=1),
        "source_run_id": _non_empty_str(metadata.get("source_run_id")),
        "source_message_id": _non_empty_str(metadata.get("source_message_id")),
        "failure_category": _non_empty_str(metadata.get("failure_category")),
        "failure_action_kind": _non_empty_str(metadata.get("failure_action_kind")),
    }


def _repair_context_rejected(context: dict[str, Any]) -> bool:
    max_attempts = int(context.get("max_attempts") or 0)
    attempt = int(context.get("attempt") or 1)
    return max_attempts <= 0 or attempt > max_attempts


def _repair_workflow_payload(
    context: dict[str, Any],
    *,
    status: str,
    reason: str | None = None,
) -> dict[str, Any]:
    payload = {
        "status": status,
        "attempt": int(context.get("attempt") or 1),
        "max_attempts": int(context.get("max_attempts") or 0),
        "source_run_id": context.get("source_run_id"),
        "source_message_id": context.get("source_message_id"),
        "failure_category": context.get("failure_category"),
        "failure_action_kind": context.get("failure_action_kind"),
        "reason": reason,
    }
    return {key: value for key, value in payload.items() if value is not None}


def _repair_rejected_failure_payload(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "error": "repair attempt limit exceeded",
        "type": "RepairWorkflowRejected",
        "category": "validation",
        "recoverable": True,
        "retryable": False,
        "action_kind": "repair",
        "suggested_action": (
            "Review the previous repair attempts and adjust the task or inputs before retrying."
        ),
        "attempt": int(context.get("attempt") or 1),
        "max_attempts": int(context.get("max_attempts") or 0),
    }


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _non_empty_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _run_failed_payload(
    exc: Exception,
    *,
    secrets: tuple[str, ...] = (),
) -> dict[str, Any]:
    if isinstance(exc, ModelServiceError):
        payload = exc.to_event_payload()
    else:
        payload = {"error": str(exc), "type": type(exc).__name__}
        code = getattr(exc, "code", None)
        retryable = getattr(exc, "retryable", None)
        if isinstance(code, str) and code:
            payload["code"] = code
        if isinstance(retryable, bool):
            payload["retryable"] = retryable
    payload = _redact_failure_value(payload, secrets=secrets)
    classification = classify_failure_payload("run.failed", payload)
    for key in (
        "category",
        "recoverable",
        "retryable",
        "action_kind",
        "recovery_action",
        "suggested_action",
    ):
        payload.setdefault(key, classification[key])
    if classification.get("code"):
        payload.setdefault("error_code", classification["code"])
    return payload


def _redact_failure_value(value: Any, *, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        return {key: _redact_failure_value(item, secrets=secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_failure_value(item, secrets=secrets) for item in value]
    if not isinstance(value, str):
        return value
    redacted = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _completion_failure_payload(
    state_values: Any,
    *,
    current_run_id: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(state_values, dict):
        route: dict[str, Any] = {}
    else:
        value = state_values.get("completion_route")
        route = value if isinstance(value, dict) else {}
    if current_run_id is not None and route.get("run_id") != current_run_id:
        route = {
            "decision": "failed",
            "reason": "completion_route_scope_mismatch",
            "message": "The graph ended without a completion decision owned by this run.",
            "recoverable": False,
            "run_id": current_run_id,
        }
    if route.get("decision") not in {"failed", "blocked"}:
        return None
    reason = str(route.get("reason") or "model_output_invalid")
    message = str(route.get("message") or "The model did not produce a valid result.")
    payload = {
        "error": message,
        "error_code": reason,
        "source": "completion_router",
        "failure_category": ("verification" if reason == "verification_failed" else "model_output"),
        "recoverable": bool(route.get("recoverable")),
        "retryable": False,
        "details": {
            key: route[key] for key in ("attempts", "max_attempts", "tool_call_id") if key in route
        },
    }
    classification = classify_failure_payload("run.failed", payload)
    for key in (
        "category",
        "action_kind",
        "recovery_action",
        "suggested_action",
    ):
        payload[key] = classification[key]
    return payload
