"""Runtime-owned Run, child, mailbox, and collaboration schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .runtime import ReasoningMode

RunStatus = Literal[
    "queued",
    "running",
    "waiting_permission",
    "waiting_input",
    "cleanup_required",
    "completed",
    "canceled",
    "failed",
]
RunKind = Literal["turn", "fork", "child"]
PermissionMode = Literal["ask", "auto", "full_access"]


class LocalRunInputRef(BaseModel):
    client_index: int = Field(ge=0)
    input_id: str
    virtual_path: str
    original_name: str
    media_type: str
    bytes: int = Field(ge=0)
    sha256: str


SubagentInvocationStatus = Literal[
    "queued",
    "running",
    "waiting",
    "completed",
    "failed",
    "canceled",
    "unknown",
]
SubagentReceiptStatus = Literal[
    "prepared",
    "running",
    "paused",
    "completed",
    "failed",
    "outcome_unknown",
    "rejected",
    "canceled",
]


class LocalSubagentUsage(BaseModel):
    model_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    unmetered_calls: int = Field(default=0, ge=0)
    outcome_unknown_calls: int = Field(default=0, ge=0)


class LocalSubagentInvocation(BaseModel):
    """Current projection of one synchronous ``task`` tool invocation.

    This is not an independently addressable child Run. ``operation_id`` is
    the durable tool-operation identity owned by Runtime.
    """

    operation_id: str
    parent_run_id: str
    parent_operation_id: str | None
    tool_call_id: str
    subagent_type: str
    description: str
    status: SubagentInvocationStatus
    receipt_status: SubagentReceiptStatus
    attempt_count: int = Field(ge=0)
    usage: LocalSubagentUsage
    error_type: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None
    updated_at: str


class LocalChildRun(BaseModel):
    """Addressable Runtime-owned child with its own job and checkpoint."""

    id: str
    parent_run_id: str
    root_run_id: str
    run_kind: Literal["child"] = "child"
    goal: str
    status: RunStatus
    agent_definition_id: str
    agent_definition_version: str
    collaboration_depth: int = Field(ge=1)
    collaboration_policy: dict[str, Any]
    completion_mode: Literal["required", "best_effort", "quorum"] = "required"
    depends_on: list[str] = Field(default_factory=list, max_length=7)
    resource_claims: list[str] = Field(default_factory=list, max_length=16)
    quorum_group: str | None = None
    quorum_required: int | None = Field(default=None, ge=1, le=8)
    spawn_operation_id: str
    graph_thread_id: str
    graph_checkpoint_id: str | None = None
    result: str | None = None
    error: str | None = None
    error_type: str | None = None
    retryable: bool | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    events_count: int = Field(default=0, ge=0)
    created_at: str
    updated_at: str
    completed_at: str | None = None


class LocalRun(BaseModel):
    """One row of the `local_runs` table, surfaced over HTTP.

    `history_json` / `settings_json` are stringified JSON because
    SQLite stores them that way — the client parses on demand. Kept
    as strings here (not dict) to keep the wire format honest.
    """

    id: str
    run_kind: RunKind = "turn"
    root_run_id: str
    agent_definition_id: str = "shejane.default"
    agent_definition_version: str = "1"
    collaboration_depth: int = Field(default=0, ge=0)
    collaboration_policy_json: str = "{}"
    spawn_operation_id: str | None = None
    goal: str
    status: RunStatus
    workspace_path: str | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None
    canceled_at: str | None = None
    history_json: str = "[]"
    parent_run_id: str | None = None
    settings_json: str = "{}"
    metadata_json: str = "{}"
    reasoning_mode: ReasoningMode | None = None
    model_phase: (
        Literal[
            "waiting_provider",
            "reasoning",
            "answering",
            "tool_calling",
            "completed",
        ]
        | None
    ) = None
    model_phase_started_at: str | None = None
    events_count: int | None = None
    command_id: str | None = None
    client_message_id: str | None = None
    graph_thread_id: str | None = None
    graph_checkpoint_id: str | None = None
    thread_id: str | None = None
    assistant_item_id: str | None = None
    user_input: str | None = None
    inputs: list[LocalRunInputRef]
    subagent_invocations: list[LocalSubagentInvocation] = Field(default_factory=list)
    child_runs: list[LocalChildRun] = Field(default_factory=list)

    @model_validator(mode="after")
    def input_order_is_dense(self) -> LocalRun:
        if sorted(item.client_index for item in self.inputs) != list(range(len(self.inputs))):
            raise ValueError("inputs must have unique dense client_index values")
        return self


class ListRunsResponse(BaseModel):
    runs: list[LocalRun]


class ListChildRunsResponse(BaseModel):
    children: list[LocalChildRun]


class LocalAgentMessage(BaseModel):
    """Durable typed envelope exchanged inside one collaboration root."""

    id: str
    root_run_id: str
    sender_run_id: str
    recipient_run_id: str
    sender_operation_id: str
    kind: Literal["request", "question", "update", "result", "cancel"]
    text: str
    data: dict[str, Any]
    artifact_refs: list[str]
    correlation_id: str
    in_reply_to: str | None = None
    sequence: int = Field(ge=1)
    hop_count: int = Field(ge=0)
    status: Literal["queued", "delivered", "acknowledged", "expired"]
    ttl_seconds: int = Field(ge=60, le=86400)
    deadline_at: str
    created_at: str
    delivered_at: str | None = None
    acknowledged_at: str | None = None


class ListAgentMessagesResponse(BaseModel):
    messages: list[LocalAgentMessage]


class LocalCollaborationRoot(BaseModel):
    id: str
    parent_run_id: str | None = None
    root_run_id: str
    run_kind: Literal["turn", "fork"]
    goal: str
    status: RunStatus
    agent_definition_id: str
    agent_definition_version: str
    graph_thread_id: str
    graph_checkpoint_id: str | None = None
    result: str | None = None
    error: str | None = None
    error_type: str | None = None
    retryable: bool | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    created_at: str
    updated_at: str
    completed_at: str | None = None


class LocalCollaborationWait(BaseModel):
    id: str
    run_id: str
    kind: Literal["tool_review", "question", "plan", "tool_reconciliation"]
    wait_cycle_id: str
    interrupt_id: str
    position: int = Field(ge=0)
    status: Literal["pending"]
    payload: dict[str, Any]
    decision: dict[str, Any] | None = None
    created_at: str
    resolved_at: str | None = None


class LocalCollaborationResourceOwner(BaseModel):
    resource_key: str
    owner_run_id: str
    created_at: str


class LocalCollaborationDependency(BaseModel):
    child_run_id: str
    dependency_run_id: str


class LocalCollaborationArtifact(BaseModel):
    id: str
    run_id: str
    kind: str
    title: str
    content_type: str
    bytes: int = Field(ge=0)
    sha256: str | None = None
    storage_kind: Literal["inline_text", "blob"]
    tool_name: str | None = None
    created_at: str


class LocalCollaborationRequiredSummary(BaseModel):
    total: int = Field(ge=0)
    completed: int = Field(ge=0)
    failed: list[str]
    active: int = Field(ge=0)


class LocalCollaborationQuorumSummary(BaseModel):
    group: str
    required: int = Field(ge=0)
    completed: int = Field(ge=0)
    active: int = Field(ge=0)
    failed: int = Field(ge=0)
    satisfied: bool
    impossible: bool


class LocalCollaborationCompletionSummary(BaseModel):
    satisfied: bool
    impossible: bool
    required: LocalCollaborationRequiredSummary
    quorum_groups: list[LocalCollaborationQuorumSummary]
    best_effort_active: int = Field(ge=0)
    wait_for: list[str]
    cancel: list[str]


class LocalCollaborationSnapshot(BaseModel):
    schema_version: Literal[1] = 1
    captured_at: str
    root: LocalCollaborationRoot
    children: list[LocalChildRun]
    messages: list[LocalAgentMessage]
    pending_waits: list[LocalCollaborationWait]
    resource_owners: list[LocalCollaborationResourceOwner]
    dependencies: list[LocalCollaborationDependency]
    artifacts: list[LocalCollaborationArtifact]
    event_high_watermarks: dict[str, int]
    completion: LocalCollaborationCompletionSummary
