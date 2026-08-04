"""Shared failure classification and retry policy.

Diagnostics, model retry, and future user-action policy should agree on the
same category/retry semantics. Keep this module free of FastAPI/LangGraph
imports so it can be used from both HTTP surfaces and middleware assembly.
"""

from __future__ import annotations

from typing import Any


def classify_failure_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    code = _first_string(
        payload.get("error_code"),
        payload.get("errorCode"),
        payload.get("code"),
        payload.get("type"),
    )
    message = _first_string(
        payload.get("message"),
        payload.get("error"),
        payload.get("content"),
        payload.get("detail"),
        code,
        event_type,
    )
    tool = _first_string(payload.get("tool"), payload.get("name"))
    haystack = f"{code or ''} {message}".lower()
    explicit_recoverable = payload.get("recoverable")
    if not isinstance(explicit_recoverable, bool):
        explicit_recoverable = None
    explicit_retryable = payload.get("retryable")
    if not isinstance(explicit_retryable, bool):
        explicit_retryable = None

    category = "unknown"
    recoverable = False
    retryable = False
    suggested_action = "Inspect the diagnostic events and logs before retrying."

    if _contains_any(haystack, "memory_fact_not_authorized"):
        category = "permission"
        recoverable = True
        suggested_action = "Confirm the exact fact to save to memory, then try again."
    elif _contains_any(haystack, "model_call_budget_exhausted"):
        category = "fatal"
        suggested_action = (
            "Inspect repeated agent steps or increase the model-call budget only when the task "
            "legitimately needs more rounds."
        )
    elif _contains_any(
        haystack,
        "provider_quota_exceeded",
        "insufficient_quota",
        "quota exceeded",
        "quota exhausted",
    ):
        category = "quota"
        recoverable = True
        suggested_action = "Check the model-service quota or choose another Runtime model."
    elif _contains_any(
        haystack,
        "unauthorized",
        "401",
        "token",
        "sign in",
        "login",
        "bearer",
    ):
        category = "auth"
        recoverable = True
        suggested_action = "Check the Runtime model-service API Key, then retry."
    elif _contains_any(
        haystack,
        "not_configured",
        "missing_api_key",
        "model_unavailable",
        "api key",
        "configuration",
    ):
        category = "configuration"
        recoverable = True
        suggested_action = "Connect the missing model service or API Key, then retry."
    elif _contains_any(
        haystack,
        "timeout",
        "timed out",
        "temporarily",
        "rate_limit",
        "rate limit",
        "429",
        "500",
        "502",
        "503",
        "504",
        "apiconnectionerror",
        "connection error",
        "connection reset",
        "network",
        "unreachable",
        "transient",
    ):
        category = "transient"
        recoverable = True
        retryable = True
        suggested_action = (
            "Retry after a short backoff; if it repeats, inspect model-service or network status."
        )
    elif _contains_any(
        haystack, "path_outside_workspace", "workspace", "not inside any authorized"
    ):
        category = "workspace"
        recoverable = True
        suggested_action = (
            "Authorize the correct workspace or choose a path inside an authorized workspace."
        )
    elif _contains_any(haystack, "permissionerror", "[errno 13]"):
        category = "fatal"
        suggested_action = (
            "Inspect the local executable or file permissions and fix the Runtime installation."
        )
    elif _contains_any(haystack, "permission", "denied", "approval", "approve"):
        category = "permission"
        recoverable = True
        suggested_action = "Approve or deny the pending permission request before retrying."
    elif _contains_any(
        haystack,
        "validation",
        "verification_failed",
        "verification failed",
        "repeated_tool_failure",
        "already exists",
        "invalid",
        "bad request",
        "400",
    ):
        category = "validation"
        recoverable = True
        suggested_action = "Fix the invalid request arguments before retrying."
    elif _contains_any(haystack, "typeerror", "valueerror", "keyerror", "runtimeerror"):
        category = "fatal"
        suggested_action = (
            "Inspect the stack/logs and fix the local implementation before retrying."
        )

    if explicit_recoverable is not None:
        recoverable = explicit_recoverable
        if explicit_recoverable and category == "unknown":
            suggested_action = "Resolve the reported failure condition, then retry."
    if explicit_retryable is not None:
        if explicit_retryable and explicit_recoverable is False:
            retryable = False
            suggested_action = "Inspect the diagnostic events and logs before retrying."
        elif explicit_retryable and _allows_explicit_retry(category):
            retryable = True
            if explicit_recoverable is None:
                recoverable = True
            if category == "unknown":
                category = "transient"
            suggested_action = "Retry after a short backoff; if it repeats, inspect model-service or network status."
        elif not explicit_retryable and category == "transient":
            retryable = False
            suggested_action = "Resolve the reported transient failure before retrying."

    action_kind = _action_kind(category, retryable=retryable)

    return {
        "category": category,
        "recoverable": recoverable,
        "retryable": retryable,
        "action_kind": action_kind,
        "recovery_action": _recovery_action(category, action_kind),
        "code": code,
        "message": message,
        "source_event_type": event_type,
        "tool": tool,
        "suggested_action": suggested_action,
    }


def should_retry_failure_payload(event_type: str, payload: dict[str, Any]) -> bool:
    return bool(classify_failure_payload(event_type, payload).get("retryable"))


def build_retry_decision(
    event_type: str,
    payload: dict[str, Any],
    *,
    attempt: int,
    max_attempts: int,
    initial_delay: float = 0.25,
    backoff_factor: float = 2.0,
    max_delay: float = 2.0,
) -> dict[str, Any]:
    """Return one bounded retry/backoff decision for a classified failure.

    `attempt` is zero-based and `max_attempts` is the retry budget. For
    example, attempt=0/max_attempts=2 is the first retry opportunity; attempt=2
    means the budget is exhausted.
    """
    classification = classify_failure_payload(event_type, payload)
    attempt_index = max(0, int(attempt))
    budget = max(0, int(max_attempts))
    action_kind = str(classification.get("action_kind") or "inspect")
    retryable = bool(classification.get("retryable"))

    should_retry = False
    reason = "not_retryable"
    if action_kind != "retry":
        reason = f"action_kind_{action_kind}"
    elif not retryable:
        reason = "not_retryable"
    elif attempt_index >= budget:
        reason = "retry_budget_exhausted"
    else:
        should_retry = True
        reason = "retryable_transient"

    delay_s = 0.0
    if should_retry:
        delay_s = _bounded_backoff_seconds(
            attempt_index,
            initial_delay=initial_delay,
            backoff_factor=backoff_factor,
            max_delay=max_delay,
        )

    return {
        **classification,
        "should_retry": should_retry,
        "attempt": attempt_index,
        "max_attempts": budget,
        "delay_s": delay_s,
        "reason": reason,
    }


def _contains_any(value: str, *needles: str) -> bool:
    return any(needle in value for needle in needles)


def _allows_explicit_retry(category: str) -> bool:
    return category in {"unknown", "transient"}


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _action_kind(category: str, *, retryable: bool) -> str:
    if retryable:
        return "retry"
    if category in {"auth", "quota", "permission", "configuration", "workspace"}:
        return "user_action"
    if category == "validation":
        return "repair"
    if category == "fatal":
        return "operator_action"
    return "inspect"


def _recovery_action(category: str, action_kind: str) -> str:
    if action_kind == "retry":
        return "retry"
    if action_kind == "repair":
        return "repair"
    if category in {"quota", "auth"}:
        return "diagnostics"
    if category == "workspace":
        return "workspace"
    if category == "permission":
        return "retry"
    return "diagnostics"


def _bounded_backoff_seconds(
    attempt: int,
    *,
    initial_delay: float,
    backoff_factor: float,
    max_delay: float,
) -> float:
    initial = max(0.0, float(initial_delay))
    factor = max(0.0, float(backoff_factor))
    ceiling = max(0.0, float(max_delay))
    if initial <= 0 or ceiling <= 0:
        return 0.0
    return min(initial * (factor**attempt), ceiling)
