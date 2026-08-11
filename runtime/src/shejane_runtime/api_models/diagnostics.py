"""Redacted Runtime diagnostics export schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .decisions import PermissionScope
from .run_state import LocalRun


class DiagnosticsPermission(BaseModel):
    id: str
    run_id: str
    tool_call_id: str | None = None
    tool_name: str
    arguments: dict[str, Any]
    status: str
    scope: PermissionScope = "once"
    created_at: str
    resolved_at: str | None = None


class DiagnosticsArtifact(BaseModel):
    id: str
    run_id: str
    kind: str
    title: str
    content_type: str
    bytes: int
    sha256: str | None = None
    storage_kind: Literal["inline_text", "blob"] = "inline_text"
    tool_call_id: str | None = None
    tool_name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class DiagnosticsEvent(BaseModel):
    """One row from the `local_events` table after payload parsing.

    Matches the AgentRunEvent envelope the client's
    `parseAgentSSEChunk` produces (intentional, so the diagnostics
    panel can reuse the live-stream renderer).
    """

    id: str
    run_id: str
    seq: int
    event_type: str
    payload: dict[str, Any]
    created_at: str


class LatestCheckpoint(BaseModel):
    """Slim summary of the agent run's last persisted superstep — used
    by the diagnostics panel to render the "where the run is paused"
    headline. The full LangGraph checkpoint is much larger; we just
    expose the fields the UI reads (`id`, `reason`, `messages_count`)."""

    id: str
    run_id: str | None = None
    step: int
    reason: str
    messages_count: int
    created_at: str | None = None


class DiagnosticsFailure(BaseModel):
    """Structured classification for the latest failed run/tool event."""

    category: Literal[
        "transient",
        "auth",
        "quota",
        "permission",
        "configuration",
        "workspace",
        "validation",
        "execution_invariant",
        "fatal",
        "unknown",
    ]
    recoverable: bool
    retryable: bool
    action_kind: Literal["retry", "user_action", "repair", "operator_action", "inspect"]
    recovery_action: Literal[
        "retry", "repair", "recharge", "refresh_session", "workspace", "diagnostics"
    ]
    code: str | None = None
    message: str
    source_event_type: str
    tool: str | None = None
    suggested_action: str


class DiagnosticsVerification(BaseModel):
    """Latest machine-readable task.verify result, if any."""

    status: Literal["passed", "failed"]
    reason: str | None = None
    pass_count: int | None = None
    fail_count: int | None = None
    source_event_type: str


class DiagnosticsReflectionCritic(BaseModel):
    """Compact final-answer critic output, if reflection ran."""

    coverage: int | None = None
    clarity: int | None = None
    grounding: int | None = None
    notes: list[str] = Field(default_factory=list)
    raw: str | None = None


class DiagnosticsReflection(BaseModel):
    """Safe reflection summary from the latest checkpoint.

    This deliberately excludes checkpoint messages and raw tool output.
    """

    ai_messages: int | None = None
    tool_results: int | None = None
    final_answer_chars: int | None = None
    critic: DiagnosticsReflectionCritic | None = None


class DiagnosticsToolReceipt(BaseModel):
    """Safe execution identity/status without raw tool arguments or output."""

    operation_id: str
    execution_namespace: str
    parent_operation_id: str | None = None
    tool_call_id: str
    tool_name: str
    tool_version: str
    arguments_hash: str
    risk: str
    status: Literal[
        "prepared",
        "running",
        "paused",
        "completed",
        "failed",
        "outcome_unknown",
        "rejected",
        "canceled",
    ]
    attempt_count: int
    result_hash: str | None = None
    error_type: str | None = None
    review_decision: str | None = None
    review_source: str | None = None
    review_reason_hash: str | None = None
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    updated_at: str


class DiagnosticsWaitCandidate(BaseModel):
    id: str
    kind: Literal["tool_review", "question", "plan", "tool_reconciliation"]
    status: Literal["pending", "resolved"]
    created_at: str
    resolved_at: str | None = None


class DiagnosticsBuildIdentity(BaseModel):
    runtime_version: str
    client_release: str | None = None
    build_commit: str | None = None
    build_id: str | None = None
    platform: str
    arch: str
    packaging_mode: str
    protocol_version: int = Field(ge=1)


class DiagnosticsExecutionPolicy(BaseModel):
    complexity: Literal["simple", "complex"]
    plan_mode: Literal["off", "auto", "always"]
    plan_required: bool
    subagent_allowed: bool
    reason: str
    max_model_calls: int = Field(ge=1)
    soft_model_call_limit: int = Field(ge=1)
    final_model_call_reserve: int = Field(ge=1)
    subagent_budget_mode: Literal["shared_model_budget"]
    max_subagent_tasks: int | None = Field(default=None, ge=0)
    preferred_subagent_concurrency: int = Field(ge=0)
    max_concurrent_subagent_tasks: int = Field(ge=0)
    max_subagent_model_calls: int = Field(ge=0)


class DiagnosticsModelCall(BaseModel):
    id: str
    logical_call_id: str
    retry_attempt: int = Field(ge=0)
    execution_attempt_id: str
    parent_tool_operation_id: str | None = None
    call_index: int = Field(ge=1)
    model: str
    purpose: str
    status: str
    output_started: bool
    outcome_unknown: bool
    provider_request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    phase: Literal[
        "waiting_provider",
        "reasoning",
        "answering",
        "tool_calling",
        "completed",
    ]
    error_code: str | None = None
    created_at: str
    request_started_at: str
    response_headers_at: str | None = None
    first_raw_chunk_at: str | None = None
    reasoning_started_at: str | None = None
    first_visible_output_at: str | None = None
    phase_started_at: str
    first_output_at: str | None = None
    completed_at: str | None = None


class DiagnosticsHandoff(BaseModel):
    """Compact handoff summary for long-running or resumed work.

    This is derived from redacted run metadata and event types. It deliberately
    does not include full checkpoint messages, artifact bodies, or raw tool
    output.
    """

    status: str
    headline: str
    next_actions: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    recent_event_types: list[str] = Field(default_factory=list)
    ledger_state: Literal["not_required", "missing", "fresh", "stale"] = "not_required"
    ledger_message: str | None = None
    failure: DiagnosticsFailure | None = None
    verification: DiagnosticsVerification | None = None


class DiagnosticsTraceSpan(BaseModel):
    id: str
    parent_id: str | None = None
    kind: Literal["run", "model", "tool", "subagent", "checkpoint", "terminal"]
    name: str
    status: str
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: float | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class DiagnosticsTrace(BaseModel):
    root_span_id: str
    spans: list[DiagnosticsTraceSpan] = Field(default_factory=list)


class FeatureLedger(BaseModel):
    """Latest durable progress ledger entry for the run."""

    summary: str
    status: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    files_touched: list[str] = Field(default_factory=list)
    validation_commands: list[str] = Field(default_factory=list)
    unresolved_risks: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    artifact_id: str | None = None
    created_at: str | None = None


class LocalRunDiagnostics(BaseModel):
    schema_version: Literal[2] = 2
    exported_at: str
    runtime_version: str | None = None
    build: DiagnosticsBuildIdentity
    execution_policy: DiagnosticsExecutionPolicy
    run: LocalRun
    events: list[DiagnosticsEvent]
    permissions: list[DiagnosticsPermission]
    model_calls: list[DiagnosticsModelCall] = Field(default_factory=list)
    tool_receipts: list[DiagnosticsToolReceipt] = Field(default_factory=list)
    wait_candidates: list[DiagnosticsWaitCandidate] = Field(default_factory=list)
    artifacts: list[DiagnosticsArtifact]
    latest_checkpoint: LatestCheckpoint | None = None
    handoff: DiagnosticsHandoff
    feature_ledger: FeatureLedger | None = None
    reflection: DiagnosticsReflection | None = None
    trace: DiagnosticsTrace


# ---------------------------------------------------------------------------
# Simple ack envelopes — used by handlers that don't have a richer return.
# ---------------------------------------------------------------------------
