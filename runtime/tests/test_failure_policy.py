from __future__ import annotations

from shejane_runtime.failure_policy import (
    build_retry_decision,
    classify_failure_payload,
    should_retry_failure_payload,
)


def test_quota_and_configuration_failures_are_not_retryable_even_with_429_text() -> None:
    quota = classify_failure_payload(
        "run.failed",
        {"error_code": "provider_quota_exceeded", "message": "Provider quota exhausted"},
    )
    assert quota["category"] == "quota"
    assert quota["recoverable"] is True
    assert quota["retryable"] is False
    assert quota["action_kind"] == "user_action"
    assert should_retry_failure_payload("run.failed", quota) is False

    configuration = classify_failure_payload(
        "run.failed",
        {"error_code": "missing_api_key", "message": "HTTP 429: missing API key"},
    )
    assert configuration["category"] == "configuration"
    assert configuration["recoverable"] is True
    assert configuration["retryable"] is False
    assert configuration["action_kind"] == "user_action"


def test_transient_failures_remain_retryable() -> None:
    failure = classify_failure_payload(
        "run.failed",
        {"error_code": "rate_limit", "message": "provider returned 429"},
    )
    assert failure["category"] == "transient"
    assert failure["recoverable"] is True
    assert failure["retryable"] is True
    assert failure["action_kind"] == "retry"
    assert should_retry_failure_payload("run.failed", failure) is True


def test_execution_settlement_failure_is_classified_as_an_invariant_bug() -> None:
    failure = classify_failure_payload(
        "run.failed",
        {
            "type": "ExecutionSettlementError",
            "message": "completed run has unsettled tool receipts",
        },
    )

    assert failure["category"] == "execution_invariant"
    assert failure["retryable"] is False
    assert failure["action_kind"] == "operator_action"
    assert failure["recovery_action"] == "diagnostics"


def test_api_connection_error_is_a_retryable_transient_failure() -> None:
    failure = classify_failure_payload(
        "run.failed",
        {"error_code": "APIConnectionError", "error": "Connection error."},
    )

    assert failure["category"] == "transient"
    assert failure["recoverable"] is True
    assert failure["retryable"] is True
    assert failure["action_kind"] == "retry"


def test_model_call_budget_exhaustion_is_a_distinct_user_recoverable_failure() -> None:
    failure = classify_failure_payload(
        "run.failed",
        {
            "error_code": "model_call_budget_exhausted",
            "message": "agent model call budget exhausted for run run-1: 20",
            "retryable": False,
        },
    )

    assert failure["category"] == "budget"
    assert failure["recoverable"] is True
    assert failure["retryable"] is False
    assert failure["action_kind"] == "user_action"
    assert failure["recovery_action"] == "diagnostics"


def test_explicit_nonrecoverable_blocks_retryable_true() -> None:
    failure = classify_failure_payload(
        "run.failed",
        {
            "error_code": "provider_busy",
            "message": "provider says retry but not recoverable",
            "recoverable": False,
            "retryable": True,
        },
    )

    assert failure["recoverable"] is False
    assert failure["retryable"] is False
    assert failure["action_kind"] != "retry"
    assert should_retry_failure_payload("run.failed", failure) is False


def test_explicit_retryable_true_cannot_override_user_or_operator_action_categories() -> None:
    cases = [
        ("quota", {"error_code": "provider_quota_exceeded", "message": "quota exhausted"}),
        ("auth", {"error_code": "unauthorized", "message": "provider token rejected"}),
        ("configuration", {"error_code": "missing_api_key", "message": "api key missing"}),
        ("workspace", {"error_code": "path_outside_workspace", "message": "workspace denied"}),
        ("validation", {"error_code": "validation_failed", "message": "invalid arguments"}),
        ("fatal", {"error_code": "RuntimeError", "message": "RuntimeError: boom"}),
    ]

    for expected_category, payload in cases:
        failure = classify_failure_payload("run.failed", {**payload, "retryable": True})
        assert failure["category"] == expected_category
        assert failure["retryable"] is False
        assert failure["action_kind"] != "retry"
        assert should_retry_failure_payload("run.failed", failure) is False

    unknown_retry = classify_failure_payload(
        "run.failed",
        {"error_code": "provider_busy", "message": "provider busy", "retryable": True},
    )
    assert unknown_retry["category"] == "transient"
    assert unknown_retry["retryable"] is True
    assert unknown_retry["action_kind"] == "retry"


def test_failure_policy_exposes_action_kind_for_policy_layers() -> None:
    cases = [
        ("user_action", {"error_code": "unauthorized", "message": "provider token rejected"}),
        ("user_action", {"error_code": "path_outside_workspace", "message": "workspace denied"}),
        ("repair", {"error_code": "validation_failed", "message": "invalid tool arguments"}),
        ("operator_action", {"error_code": "RuntimeError", "message": "RuntimeError: boom"}),
        ("inspect", {"error_code": "unknown_failure", "message": "unexpected failure"}),
    ]

    for expected, payload in cases:
        failure = classify_failure_payload("run.failed", payload)
        assert failure["action_kind"] == expected


def test_failure_policy_exposes_recovery_action_for_ui() -> None:
    cases = [
        ("retry", {"error_code": "rate_limit", "message": "provider returned 429"}),
        (
            "diagnostics",
            {"error_code": "provider_quota_exceeded", "message": "quota exhausted"},
        ),
        ("diagnostics", {"error_code": "unauthorized", "message": "provider token rejected"}),
        ("workspace", {"error_code": "path_outside_workspace", "message": "workspace denied"}),
        ("retry", {"error_code": "permission_denied", "message": "permission denied"}),
        ("repair", {"error_code": "validation_failed", "message": "invalid tool arguments"}),
        ("diagnostics", {"error_code": "missing_api_key", "message": "api key missing"}),
        ("diagnostics", {"error_code": "RuntimeError", "message": "RuntimeError: boom"}),
        ("diagnostics", {"error_code": "unknown_failure", "message": "unexpected failure"}),
    ]

    for expected, payload in cases:
        failure = classify_failure_payload("run.failed", payload)
        assert failure["recovery_action"] == expected


def test_workspace_failures_take_precedence_over_generic_permission_words() -> None:
    workspace = classify_failure_payload(
        "tool.failed",
        {"error_code": "path_outside_workspace", "message": "workspace denied"},
    )
    assert workspace["category"] == "workspace"
    assert workspace["recoverable"] is True
    assert workspace["retryable"] is False
    assert workspace["action_kind"] == "user_action"
    assert "workspace" in workspace["suggested_action"].lower()

    permission = classify_failure_payload(
        "tool.failed",
        {"error_code": "permission_denied", "message": "permission denied by user"},
    )
    assert permission["category"] == "permission"


def test_os_permission_error_is_not_a_pending_user_approval() -> None:
    failure = classify_failure_payload(
        "run.failed",
        {"error_code": "PermissionError", "message": "[Errno 13] Permission denied"},
    )

    assert failure["category"] == "fatal"
    assert failure["action_kind"] == "operator_action"
    assert failure["recovery_action"] == "diagnostics"
    assert "approve" not in failure["suggested_action"].lower()


def test_memory_confirmation_failure_requires_user_action_without_retry() -> None:
    failure = classify_failure_payload(
        "tool.failed",
        {
            "error_code": "memory_fact_not_authorized",
            "message": "fact was not authorized by the current user input",
            "recoverable": True,
            "retryable": False,
        },
    )

    assert failure["category"] == "permission"
    assert failure["action_kind"] == "user_action"
    assert failure["retryable"] is False
    assert "memory" in failure["suggested_action"].lower()


def test_retry_decision_applies_bounded_backoff_for_retry_action() -> None:
    decision = build_retry_decision(
        "run.failed",
        {"error_code": "rate_limit", "message": "provider returned 429"},
        attempt=1,
        max_attempts=3,
        initial_delay=0.5,
        backoff_factor=2,
        max_delay=2,
    )

    assert decision["should_retry"] is True
    assert decision["action_kind"] == "retry"
    assert decision["category"] == "transient"
    assert decision["attempt"] == 1
    assert decision["max_attempts"] == 3
    assert decision["delay_s"] == 1.0
    assert decision["reason"] == "retryable_transient"


def test_retry_decision_fails_fast_for_user_action_even_when_retryable_is_set() -> None:
    decision = build_retry_decision(
        "tool.failed",
        {
            "error_code": "provider_quota_exceeded",
            "message": "Provider quota exhausted",
            "retryable": True,
        },
        attempt=0,
        max_attempts=3,
    )

    assert decision["should_retry"] is False
    assert decision["action_kind"] == "user_action"
    assert decision["category"] == "quota"
    assert decision["delay_s"] == 0.0
    assert decision["reason"] == "action_kind_user_action"


def test_retry_decision_stops_when_retry_budget_is_exhausted() -> None:
    decision = build_retry_decision(
        "run.failed",
        {"error_code": "timeout", "message": "provider timed out"},
        attempt=2,
        max_attempts=2,
    )

    assert decision["should_retry"] is False
    assert decision["action_kind"] == "retry"
    assert decision["delay_s"] == 0.0
    assert decision["reason"] == "retry_budget_exhausted"
