"""Single source of truth for the HTTP API's request/response shapes.

Why this module exists:

  Before this file, every handler returned `dict[str, Any]`. That made
  `app.openapi()` output useless (just `additionalProperties: True`
  with no field names), so the client couldn't generate types from it
  and had to hand-maintain interfaces in `client/src/runtime/
  client.ts`. The 2026-05-22 contract repair session was the result —
  9+ silent shape drifts between hand-written TS interfaces and
  runtime dict returns.

  Now: every response gets a pydantic model declared here, handlers
  use `response_model=ModelName`, and FastAPI emits a real
  `components.schemas` section in openapi.json that `openapi-typescript`
  consumes. `make schemas` regenerates the TS .d.ts; CI rejects PRs
  where they drift.

Conventions:

  • Field names use snake_case (matches the wire + the original TS
    interfaces). Don't rename to camelCase — that's a breaking
    change for every renderer that reads these.
  • `Literal[...]` types over plain `str` whenever the value set is
    finite (e.g. `reason: Literal["authorized", ...]`) — they
    generate as TS string-literal unions, much better than bare
    `string`.
  • Optional fields use `| None = None` (NOT `Optional[T]`) for
    consistency with PEP 604 + the rest of the codebase.
  • SSE event payloads are NOT in here — discriminated unions over
    `event_type` don't roundtrip cleanly through openapi. The wire
    format is documented in `docs/runtime-protocol.md` and the
    TS `AgentRunEvent` interface stays hand-written in
    `runtime/sdk/src/client.ts`.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_LOCAL_REQUEST_BODY_BYTES = 1_048_576
RUNTIME_MODEL_PATTERN = r"^local:[^:\s]+:\S+$"
MAX_RUNTIME_MODEL_SPEC_LENGTH = 256

# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    """GET /v1/health — no auth required.

    Both `ok` (legacy smoke contract) and `status` (current TS client
    probe) are returned. Drop `ok` once smoke scripts are updated.
    """

    ok: bool = True
    status: Literal["ok"] = "ok"
    mode: str = "ready"
    worker: str = "python-langgraph"
    version: str
    pairing_configured: bool


class RuntimeInfo(BaseModel):
    """Authenticated Runtime protocol and capability discovery."""

    protocol_version: int
    runtime_version: str
    capabilities: list[str]
    model_service_configured: bool


class RuntimeSettingsResponse(BaseModel):
    """Persisted defaults used when accepting future runs."""

    version: int = 0
    max_model_calls: int = Field(default=100, ge=1, le=100)
    max_tool_retries: int = Field(default=2, ge=0, le=5)
    research_search_limit: int = Field(default=10, ge=1, le=20)
    unknown_model_max_input_tokens: int = Field(default=32_768, ge=8_192, le=10_000_000)
    unknown_model_max_output_tokens: int = Field(default=8_192, ge=128, le=1_000_000)
    model_request_timeout_seconds: float = Field(default=120.0, ge=5.0, le=900.0)
    browser_headless: bool = True
    subagents: bool = True
    input_guard: Literal["off", "observe", "block"] = "observe"
    plan_first: Literal["off", "auto", "always"] = "off"
    verification_repair_max: int = Field(default=1, ge=0, le=3)
    repair_workflow_max: int = Field(default=3, ge=0, le=5)
    pii_redact: str = Field(default="", max_length=200)


class UpdateRuntimeSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_model_calls: int | None = Field(default=None, ge=1, le=100)
    max_tool_retries: int | None = Field(default=None, ge=0, le=5)
    research_search_limit: int | None = Field(default=None, ge=1, le=20)
    unknown_model_max_input_tokens: int | None = Field(default=None, ge=8_192, le=10_000_000)
    unknown_model_max_output_tokens: int | None = Field(default=None, ge=128, le=1_000_000)
    model_request_timeout_seconds: float | None = Field(default=None, ge=5.0, le=900.0)
    browser_headless: bool | None = None
    subagents: bool | None = None
    input_guard: Literal["off", "observe", "block"] | None = None
    plan_first: Literal["off", "auto", "always"] | None = None
    verification_repair_max: int | None = Field(default=None, ge=0, le=3)
    repair_workflow_max: int | None = Field(default=None, ge=0, le=5)
    pii_redact: str | None = Field(default=None, max_length=200)


ModelCapabilityName = Literal[
    "agent_chat",
    "image_understanding",
    "image_generation",
    "image_editing",
]
ModelProtocol = Literal[
    "openai_chat_completions",
    "openai_responses",
    "anthropic_messages",
    "google_generate_content",
    "openai_images_generations",
    "openai_images_edits",
]
ModelVerification = Literal["verified", "unverified"]


class ModelCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: ModelCapabilityName
    protocol: ModelProtocol
    verification: ModelVerification = "unverified"

    @model_validator(mode="after")
    def validate_protocol(self) -> ModelCapability:
        valid = {
            "agent_chat": {
                "openai_chat_completions",
                "openai_responses",
                "anthropic_messages",
                "google_generate_content",
            },
            "image_understanding": {
                "openai_chat_completions",
                "openai_responses",
                "anthropic_messages",
                "google_generate_content",
            },
            "image_generation": {"openai_images_generations"},
            "image_editing": {"openai_images_edits"},
        }[self.capability]
        if self.protocol not in valid:
            raise ValueError("protocol does not support the selected model capability")
        return self


class ModelCapabilityProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str = Field(min_length=1, max_length=200, pattern=r"^\S+$")
    display_name: str = Field(min_length=1, max_length=100)
    capabilities: list[ModelCapability] = Field(default_factory=list, max_length=4)
    tool_calling: bool = True
    streaming: bool = True
    image_inputs: bool = False
    max_input_tokens: int | None = Field(default=None, ge=1, le=10_000_000)
    max_output_tokens: int | None = Field(default=None, ge=128, le=1_000_000)


class LocalRuntimeModel(BaseModel):
    spec: str
    model_id: str
    display_name: str
    connection_id: str
    service_name: str
    capabilities: list[ModelCapability]
    tool_calling: bool
    streaming: bool
    image_inputs: bool
    verification: Literal["verified", "unverified"]
    recommended: bool
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    available: bool


class LocalRuntimeModelCatalog(BaseModel):
    models: list[LocalRuntimeModel]


class ModelServiceRegion(BaseModel):
    id: Literal["cn", "intl"]
    name: str
    default: bool
    base_url: str


class ModelServicePreset(BaseModel):
    id: str
    name: str
    description: str
    connection_method: Literal["api_key", "browser_authorization"]
    api_key_url: str | None
    billing_url: str | None
    regions: list[ModelServiceRegion]


class ModelServicePresetCatalog(BaseModel):
    services: list[ModelServicePreset]


ModelAdapterID = Literal["openai_chat", "anthropic_messages", "google_genai"]
ModelCatalogStatus = Literal["ready", "stale", "unavailable"]
ModelSource = Literal["bundled", "discovered", "manual"]


class ModelServiceModel(ModelCapabilityProfile):
    source: ModelSource
    verification: ModelVerification
    recommended: bool = False


class ModelServiceConnection(BaseModel):
    id: str
    preset_id: str
    name: str
    region: Literal["cn", "intl", "custom", "official"]
    adapter_id: ModelAdapterID
    base_url: str
    credential_configured: bool
    catalog_status: ModelCatalogStatus
    models: list[ModelServiceModel]
    version: int
    created_at: str
    updated_at: str


class ListModelServiceConnectionsResponse(BaseModel):
    services: list[ModelServiceConnection]


class SheJaneAuthorizationStartResponse(BaseModel):
    authorization_id: str = Field(pattern=r"^auth_[a-f0-9]{32}$")
    authorization_url: str
    expires_at: datetime


class SheJaneAuthorizationStatusResponse(BaseModel):
    authorization_id: str = Field(pattern=r"^auth_[a-f0-9]{32}$")
    status: Literal["pending", "succeeded", "denied", "expired", "failed"]
    connection: ModelServiceConnection | None = None
    error_code: str | None = None


class CentralDiagnosticsStatusResponse(BaseModel):
    enabled: bool
    connection_id: str | None = Field(default=None, pattern=r"^conn_[a-f0-9]{32}$")
    success_sample_rate: float = Field(ge=0, le=1)
    credential_configured: bool


class UpdateCentralDiagnosticsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    connection_id: str | None = Field(
        default=None,
        pattern=r"^conn_[a-f0-9]{32}$",
    )
    success_sample_rate: float = Field(default=0, ge=0, le=1)


class ConnectModelServiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset_id: str = Field(min_length=1, max_length=32)
    region: Literal["cn", "intl", "custom"] | None = None
    api_key: str = Field(min_length=1, max_length=8192)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    adapter_id: ModelAdapterID | None = None


class ReconnectModelServiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(min_length=1, max_length=8192)
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)


class ImportModelServiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^conn_[a-f0-9]{32}$")
    preset_id: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=100)
    region: Literal["cn", "intl", "custom"]
    adapter_id: ModelAdapterID
    base_url: str = Field(min_length=1, max_length=2048)
    models: list[ModelServiceModel] = Field(default_factory=list, max_length=1000)


class AddModelServiceModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str = Field(min_length=1, max_length=200, pattern=r"^\S+$")
    display_name: str | None = Field(default=None, min_length=1, max_length=100)


class VerifyModelServiceModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: ModelCapabilityName
    protocol: ModelProtocol

    @model_validator(mode="after")
    def validate_protocol_for_capability(self) -> VerifyModelServiceModelRequest:
        ModelCapability(
            capability=self.capability,
            protocol=self.protocol,
            verification="unverified",
        )
        return self


BindableModelCapability = Literal["image_generation", "image_editing"]


class ModelCapabilityBinding(BaseModel):
    capability: BindableModelCapability
    model_spec: str = Field(pattern=RUNTIME_MODEL_PATTERN)
    connection_id: str
    connection_version: int = Field(ge=1)
    model_id: str
    protocol: ModelProtocol
    status: Literal["ready", "stale"]
    revision: int = Field(ge=1)
    updated_at: str


class ListModelCapabilityBindingsResponse(BaseModel):
    bindings: list[ModelCapabilityBinding]


class SetModelCapabilityBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_spec: str = Field(pattern=RUNTIME_MODEL_PATTERN)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


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


class PptxSlideOutline(BaseModel):
    index: int
    layout: str
    title: str
    bullets: list[str]
    notes: str
    shape_count: int
    image_count: int


class PptxOutlineResponse(BaseModel):
    slides: list[PptxSlideOutline]
    slide_count: int


class LocalThread(BaseModel):
    id: str
    title: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    version: int
    created_at: str
    updated_at: str
    archived_at: str | None = None
    deleted_at: str | None = None


class UpdateLocalThreadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=512)
    metadata: dict[str, Any] | None = None
    archived: bool | None = None


class DeleteLocalThreadResponse(BaseModel):
    id: str
    deleted: Literal[True] = True
    version: int


class LocalThreadItem(BaseModel):
    id: str
    thread_id: str
    run_id: str | None = None
    client_id: str | None = None
    item_type: str
    status: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    position: int
    version: int
    created_at: str
    updated_at: str
    completed_at: str | None = None


class LocalThreadChange(BaseModel):
    cursor: int
    thread_id: str
    thread_version: int
    change_type: str
    run_id: str | None = None
    created_at: str


class LocalThreadEvent(BaseModel):
    id: str
    run_id: str
    seq: int
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class ListRunEventsResponse(BaseModel):
    events: list[LocalThreadEvent]
    has_more: bool = False
    next_after: int = Field(ge=0)


class ListThreadsResponse(BaseModel):
    threads: list[LocalThread]
    cursor: int
    has_more: bool = False
    next_before_created_at: str | None = None
    next_before_id: str | None = None


class RunPresentationOrder(BaseModel):
    event_seq: int = Field(ge=1)
    slot: int = Field(default=0, ge=0)


class RunPresentationSource(BaseModel):
    kind: Literal["run_event", "tool_receipt", "wait_candidate", "artifact", "thread_item"]
    id: str


class RunPresentationProgressItem(BaseModel):
    id: str
    kind: Literal["progress"] = "progress"
    status: Literal["completed"] = "completed"
    order: RunPresentationOrder
    revision: int = Field(ge=1)
    source: RunPresentationSource
    text: str
    created_at: str


class RunPresentationToolItem(BaseModel):
    id: str
    kind: Literal["tool"] = "tool"
    status: Literal[
        "pending", "in_progress", "waiting", "completed", "failed", "canceled", "unknown"
    ]
    order: RunPresentationOrder
    revision: int = Field(ge=1)
    source: RunPresentationSource
    tool_call_id: str
    tool_name: str
    risk: str
    created_at: str
    updated_at: str
    completed_at: str | None = None


class RunPresentationReasoningSummaryItem(BaseModel):
    id: str
    kind: Literal["reasoning_summary"] = "reasoning_summary"
    status: Literal["completed"] = "completed"
    order: RunPresentationOrder
    revision: int = Field(ge=1)
    source: RunPresentationSource
    summary: str
    created_at: str


class RunPresentationSubagentItem(BaseModel):
    id: str
    kind: Literal["subagent"] = "subagent"
    status: Literal[
        "pending", "in_progress", "waiting", "completed", "failed", "canceled", "unknown"
    ]
    order: RunPresentationOrder
    revision: int = Field(ge=1)
    source: RunPresentationSource
    operation_id: str
    subagent_type: str
    description: str
    created_at: str
    updated_at: str
    completed_at: str | None = None


class RunPresentationVerificationItem(BaseModel):
    id: str
    kind: Literal["verification"] = "verification"
    status: Literal[
        "pending", "in_progress", "waiting", "completed", "failed", "canceled", "unknown"
    ]
    order: RunPresentationOrder
    revision: int = Field(ge=1)
    source: RunPresentationSource
    operation_id: str
    tool_name: str
    created_at: str
    updated_at: str
    completed_at: str | None = None


class RunPresentationArtifactItem(BaseModel):
    id: str
    kind: Literal["artifact"] = "artifact"
    status: Literal["completed"] = "completed"
    order: RunPresentationOrder
    revision: int = Field(ge=1)
    source: RunPresentationSource
    artifact_id: str
    title: str
    content_type: str
    created_at: str


class RunPresentationDecisionItem(BaseModel):
    id: str
    kind: Literal["approval", "question", "plan", "reconciliation"]
    status: Literal["waiting", "completed", "failed", "canceled"]
    order: RunPresentationOrder
    revision: int = Field(ge=1)
    source: RunPresentationSource
    request_id: str
    summary: str
    created_at: str
    updated_at: str
    completed_at: str | None = None


class RunPresentationNoticeItem(BaseModel):
    id: str
    kind: Literal["notice"] = "notice"
    status: Literal["failed", "canceled", "unknown"]
    order: RunPresentationOrder
    revision: int = Field(ge=1)
    source: RunPresentationSource
    severity: Literal["warning", "error"]
    message: str
    created_at: str


class RunPresentationFinalAnswerItem(BaseModel):
    id: str
    kind: Literal["final_answer"] = "final_answer"
    status: Literal["completed"] = "completed"
    order: RunPresentationOrder
    revision: int = Field(ge=1)
    source: RunPresentationSource
    content: str
    created_at: str
    completed_at: str


RunPresentationItem = Annotated[
    RunPresentationProgressItem
    | RunPresentationReasoningSummaryItem
    | RunPresentationToolItem
    | RunPresentationSubagentItem
    | RunPresentationVerificationItem
    | RunPresentationArtifactItem
    | RunPresentationDecisionItem
    | RunPresentationNoticeItem
    | RunPresentationFinalAnswerItem,
    Field(discriminator="kind"),
]


class RunPresentationSnapshot(BaseModel):
    schema_version: Literal[1] = 1
    run_id: str
    items: list[RunPresentationItem] = Field(default_factory=list)
    event_high_watermark: int = Field(ge=0)


class LocalThreadSnapshot(BaseModel):
    thread: LocalThread
    items: list[LocalThreadItem]
    runs: list[LocalRun]
    events: list[LocalThreadEvent]
    event_high_watermarks: dict[str, int] = Field(
        default_factory=dict,
        description="Highest event sequence included in this snapshot per Run; 0 means replay all.",
    )
    presentations: dict[str, RunPresentationSnapshot] = Field(default_factory=dict)
    cursor: int
    has_more_items: bool = False
    next_before_position: int | None = None
    events_truncated: bool = False


class ListThreadChangesResponse(BaseModel):
    changes: list[LocalThreadChange]
    cursor: int


def _has_invalid_capability_name(capabilities: list[str]) -> bool:
    return any(
        not item or len(item) > 64 or not all(char.isalnum() or char in "._-" for char in item)
        for item in capabilities
    )


class PluginReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plugin_id: str = Field(
        min_length=3,
        max_length=200,
        pattern=r"^[a-z0-9]+(?:[.-][a-z0-9]+)+$",
    )
    required: bool = True
    expected_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )


class PluginCommandReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plugin_id: str = Field(
        min_length=3,
        max_length=200,
        pattern=r"^[a-z0-9]+(?:[.-][a-z0-9]+)+$",
    )
    command_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$",
    )
    expected_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    client_message_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    thread_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    assistant_message_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    protocol_version: int = Field(ge=1, le=65_535)
    required_capabilities: list[str] = Field(max_length=32)
    required_tools: list[Literal["image.generate", "image.edit"]] = Field(
        default_factory=list,
        max_length=2,
    )
    goal: str = Field(max_length=131_072)
    user_input: str | None = Field(default=None, max_length=131_072)
    thread_title: str | None = Field(default=None, max_length=512)
    thread_metadata: dict[str, Any] | None = None
    user_item_metadata: dict[str, Any] | None = None
    replace_from_client_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    workspace_path: str | None = Field(default=None, max_length=4096)
    attachment_paths: list[str] = Field(default_factory=list, max_length=10)
    # Runtime model selection, normally `local:<connection>:<model>`.
    model: str = Field(
        min_length=1,
        max_length=MAX_RUNTIME_MODEL_SPEC_LENGTH,
        pattern=RUNTIME_MODEL_PATTERN,
    )
    permission_mode: PermissionMode = "ask"
    history: list[dict[str, str]] | None = Field(default=None, max_length=256)
    parent_run_id: str | None = Field(default=None, max_length=128)
    plugin_refs: list[PluginReference] = Field(default_factory=list, max_length=32)
    plugin_command: PluginCommandReference | None = None
    settings: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def persistent_payload_fits(self) -> CreateRunRequest:
        if self.assistant_message_id == self.client_message_id:
            raise ValueError("assistant_message_id must differ from client_message_id")
        if _has_invalid_capability_name(self.required_capabilities):
            raise ValueError("required_capabilities contains an invalid capability name")
        if len(self.required_tools) != len(set(self.required_tools)):
            raise ValueError("required_tools must contain unique tool names")
        plugin_ids = [reference.plugin_id for reference in self.plugin_refs]
        if len(plugin_ids) != len(set(plugin_ids)):
            raise ValueError("plugin_refs must contain unique plugin ids")
        if any(not path.strip() or len(path) > 4096 for path in self.attachment_paths):
            raise ValueError("attachment_paths contains an invalid path")
        for field_name, value in (("settings", self.settings), ("metadata", self.metadata)):
            nodes = 0
            stack: list[tuple[Any, int]] = [(value, 1)] if value is not None else []
            while stack:
                item, depth = stack.pop()
                nodes += 1
                if depth > 8 or nodes > 512:
                    raise ValueError(f"{field_name} exceeds the depth or node limit")
                children = (
                    item.values()
                    if isinstance(item, dict)
                    else item
                    if isinstance(item, list)
                    else ()
                )
                stack.extend((child, depth + 1) for child in children)
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_LOCAL_REQUEST_BODY_BYTES:
            raise ValueError("run command exceeds the 1 MiB persistence limit")
        return self


ScheduledRunStatus = Literal["scheduled", "running", "completed", "failed", "canceled"]


class LocalScheduledRun(BaseModel):
    id: str
    goal: str
    status: ScheduledRunStatus
    run_at: str
    workspace_path: str | None = None
    model: str = "auto"
    history_json: str = "[]"
    settings_json: str = "{}"
    metadata_json: str = "{}"
    run_id: str | None = None
    result_text: str | None = None
    error_message: str | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None
    notified_at: str | None = None


class ListScheduledRunsResponse(BaseModel):
    schedules: list[LocalScheduledRun]


class CreateScheduledRunRequest(BaseModel):
    goal: str
    run_at: str
    workspace_path: str | None = None
    model: str = Field(
        min_length=1,
        max_length=MAX_RUNTIME_MODEL_SPEC_LENGTH,
        pattern=RUNTIME_MODEL_PATTERN,
    )
    permission_mode: PermissionMode = "ask"
    history: list[dict[str, str]] | None = None
    settings: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


class ForkRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    client_message_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
    )
    assistant_message_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
    )
    thread_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    protocol_version: int = Field(ge=1, le=65_535)
    required_capabilities: list[str] = Field(max_length=32)
    checkpoint_id: str = Field(min_length=1, max_length=256)
    goal: str | None = Field(default=None, max_length=131_072)
    user_input: str = Field(max_length=131_072)
    thread_title: str | None = Field(default=None, max_length=512)
    thread_metadata: dict[str, Any] | None = None
    user_item_metadata: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_client_ids(self) -> ForkRunRequest:
        if self.client_message_id == self.assistant_message_id:
            raise ValueError("client_message_id and assistant_message_id must differ")
        if _has_invalid_capability_name(self.required_capabilities):
            raise ValueError("required_capabilities contains an invalid capability name")
        encoded = json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > MAX_LOCAL_REQUEST_BODY_BYTES:
            raise ValueError("fork command exceeds the 1 MiB persistence limit")
        return self


class InjectRunInstructionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    content: str = Field(min_length=1, max_length=131_072)


class InjectRunInstructionResponse(BaseModel):
    command_id: str
    run_id: str
    instruction_id: str
    queued: Literal[True] = True


PlanApprovalDecision = Literal["approve", "modify", "reject"]


class ResolvePlanApprovalRequest(BaseModel):
    decision: PlanApprovalDecision
    instructions: str | None = Field(default=None, max_length=8192)


class PlanApprovalResolution(BaseModel):
    approval_id: str
    resolved: Literal[True] = True
    decision: PlanApprovalDecision
    resumed: bool


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------


class LocalWorkspaceAuthorization(BaseModel):
    id: str
    path: str
    label: str
    created_at: str
    last_used_at: str


class ListWorkspacesResponse(BaseModel):
    workspaces: list[LocalWorkspaceAuthorization]


class CreateWorkspaceRequest(BaseModel):
    path: str
    label: str = ""


class DiagnoseWorkspaceRequest(BaseModel):
    path: str


class LocalWorkspaceDiagnosis(BaseModel):
    """Result of POST /v1/workspaces/diagnose. The `reason` field
    drives the workspace-picker UI's "why is this disabled?" copy —
    keep the enum stable."""

    path: str
    exists: bool
    is_directory: bool
    authorized: bool
    reason: Literal["authorized", "not_authorized", "not_found", "not_directory"]
    workspace: LocalWorkspaceAuthorization | None = None


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Permissions (HITL)
# ---------------------------------------------------------------------------


PermissionDecision = Literal["approve", "edit", "deny"]
PermissionScope = Literal["once", "run"]


class EditedToolAction(BaseModel):
    name: str = Field(min_length=1)
    args: dict[str, Any]


class ResolvePermissionRequest(BaseModel):
    decision: PermissionDecision
    scope: PermissionScope = "once"
    edited_action: EditedToolAction | None = None

    @model_validator(mode="after")
    def validate_edit(self) -> ResolvePermissionRequest:
        if self.decision == "edit" and self.edited_action is None:
            raise ValueError("edited_action is required when decision is edit")
        if self.decision != "edit" and self.edited_action is not None:
            raise ValueError("edited_action is only valid when decision is edit")
        if self.decision == "edit" and self.scope != "once":
            raise ValueError("edited tool calls can only be approved once")
        return self


class PermissionResolution(BaseModel):
    permission_id: str
    resolved: Literal[True] = True
    decision: PermissionDecision
    scope: PermissionScope
    resumed: bool


# ---------------------------------------------------------------------------
# Questions (user.ask)
# ---------------------------------------------------------------------------


class AnswerQuestionRequest(BaseModel):
    # answers: { <question_id>: [text, ...] }
    answers: dict[str, list[str]]


class QuestionAnswer(BaseModel):
    question_id: str
    answered: Literal[True] = True
    resumed: bool


class ReconcileToolRequest(BaseModel):
    decision: Literal["confirmed_completed", "retry_not_executed", "abort"]


class ToolReconciliationResolution(BaseModel):
    operation_id: str
    resolved: Literal[True] = True
    decision: Literal["confirmed_completed", "retry_not_executed", "abort"]
    resumed: bool


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


class LocalArtifact(BaseModel):
    """Authorized Artifact metadata; blob bodies use the separate content route."""

    id: str
    title: str
    content: str
    content_type: str
    bytes: int
    sha256: str | None = None
    storage_kind: Literal["inline_text", "blob"]
    tool_name: str | None = None
    created_at: str


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


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
    max_subagent_tasks: int = Field(ge=0)
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
    error_code: str | None = None
    created_at: str
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


class CancelRunResponse(BaseModel):
    canceled: bool


class CancelRunCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["run.cancel"]
    command_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    run_id: str = Field(min_length=1, max_length=128)


class CancelRunCommandReceipt(BaseModel):
    type: Literal["run.cancel"]
    command_id: str
    run_id: str
    canceled: bool


class AnswerQuestionCommand(AnswerQuestionRequest):
    model_config = ConfigDict(extra="forbid")

    type: Literal["question.answer"]
    command_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    question_id: str = Field(min_length=1, max_length=128)


class AnswerQuestionCommandReceipt(BaseModel):
    type: Literal["question.answer"]
    command_id: str
    question_id: str
    run_id: str
    answered: Literal[True] = True
    resumed: bool


class ResolvePermissionCommand(ResolvePermissionRequest):
    model_config = ConfigDict(extra="forbid")

    type: Literal["permission.resolve"]
    command_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    permission_id: str = Field(min_length=1, max_length=128)


class ResolvePermissionCommandReceipt(BaseModel):
    type: Literal["permission.resolve"]
    command_id: str
    permission_id: str
    run_id: str
    resolved: Literal[True] = True
    decision: PermissionDecision
    scope: PermissionScope
    resumed: bool


class PlanResolveCommand(ResolvePlanApprovalRequest):
    model_config = ConfigDict(extra="forbid")

    type: Literal["plan.resolve"]
    command_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    approval_id: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_instructions(self) -> PlanResolveCommand:
        instructions = (self.instructions or "").strip()
        if self.decision == "modify" and not instructions:
            raise ValueError("instructions are required when decision is modify")
        if self.decision != "modify" and instructions:
            raise ValueError("instructions are only valid when decision is modify")
        return self


class PlanResolveCommandReceipt(BaseModel):
    type: Literal["plan.resolve"]
    command_id: str
    approval_id: str
    run_id: str
    resolved: Literal[True] = True
    decision: PlanApprovalDecision
    instructions: str | None = None
    resumed: bool


class ToolReconcileCommand(ReconcileToolRequest):
    model_config = ConfigDict(extra="forbid")

    type: Literal["tool.reconcile"]
    command_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    operation_id: str = Field(min_length=1, max_length=128)


class ToolReconcileCommandReceipt(BaseModel):
    type: Literal["tool.reconcile"]
    command_id: str
    operation_id: str
    run_id: str
    resolved: Literal[True] = True
    decision: Literal["confirmed_completed", "retry_not_executed", "abort"]
    resumed: bool


class PluginInstallCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["plugin.install"]
    command_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    source_path: str = Field(min_length=1, max_length=4096)
    expected_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    allow_unsigned: bool = False


class PluginInstallCommandReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["plugin.install"]
    command_id: str
    plugin_id: str
    version: str
    digest: str
    installed: Literal[True] = True
    enabled: bool


class RuntimeAssetInstallCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["plugin.runtime_asset.install"]
    command_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    source_path: str = Field(min_length=1, max_length=4096)
    expected_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )


class RuntimeAssetInstallCommandReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["plugin.runtime_asset.install"]
    command_id: str
    asset_id: str
    version: str
    platform: str
    digest: str
    installed: Literal[True] = True


class FixedRuntimeAssetStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plugin_id: Literal["org.shejane.browser-qa", "org.shejane.ocr"]
    available: bool = True
    downloaded: bool
    downloading: bool | None = None
    download_progress: int | None = Field(default=None, ge=0, le=100)


class RuntimeAssetStorage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_bytes: int = Field(ge=0)
    history_bytes: int = Field(ge=0)
    asset_count: int = Field(ge=0)
    history_asset_count: int = Field(ge=0)


class RuntimeAssetCleanupResult(RuntimeAssetStorage):
    freed_bytes: int = Field(ge=0)


class _PluginStateCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    plugin_id: str = Field(
        min_length=3,
        max_length=200,
        pattern=r"^[a-z0-9]+(?:[.-][a-z0-9]+)+$",
    )
    expected_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )


class PluginEnableCommand(_PluginStateCommand):
    type: Literal["plugin.enable"]


class PluginDisableCommand(_PluginStateCommand):
    type: Literal["plugin.disable"]


class PluginUpdateCommand(_PluginStateCommand):
    type: Literal["plugin.update"]
    source_path: str = Field(min_length=1, max_length=4096)
    allow_unsigned: bool = False


class PluginRollbackCommand(_PluginStateCommand):
    type: Literal["plugin.rollback"]
    target_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class PluginRemoveCommand(_PluginStateCommand):
    type: Literal["plugin.remove"]


class PluginModelBindCommand(_PluginStateCommand):
    type: Literal["plugin.model.bind"]
    binding_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    model: str = Field(
        min_length=1,
        max_length=MAX_RUNTIME_MODEL_SPEC_LENGTH,
        pattern=RUNTIME_MODEL_PATTERN,
    )


class PluginStateCommandReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["plugin.enable", "plugin.disable"]
    command_id: str
    plugin_id: str
    digest: str
    enabled: bool


class PluginReadinessSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["ready", "action_required", "awaiting_user", "blocked"]
    revision: int = Field(ge=0)
    step: Literal["install_helper", "screen_recording", "accessibility"] | None = None
    action_id: (
        Literal[
            "install_helper",
            "request_screen_recording",
            "open_screen_recording_settings",
            "request_accessibility",
            "open_accessibility_settings",
        ]
        | None
    ) = None
    can_recheck: bool
    code: str | None = None


class PluginSetupAdvanceCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["plugin.setup.advance"]
    command_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    plugin_id: Literal["org.shejane.computer-use"]
    expected_revision: int = Field(ge=0)
    action_id: Literal[
        "install_helper",
        "request_screen_recording",
        "open_screen_recording_settings",
        "request_accessibility",
        "open_accessibility_settings",
        "recheck",
    ]


class PluginSetupAdvanceCommandReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["plugin.setup.advance"]
    command_id: str
    plugin_id: Literal["org.shejane.computer-use"]
    readiness: PluginReadinessSnapshot


class PluginVersionSwitchCommandReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["plugin.update", "plugin.rollback"]
    command_id: str
    plugin_id: str
    version: str
    previous_digest: str
    digest: str
    enabled: bool


class PluginRemoveCommandReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["plugin.remove"]
    command_id: str
    plugin_id: str
    digest: str
    retired: Literal[True] = True
    enabled: Literal[False] = False


class PluginModelBindingSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    requested_model: str
    connection_id: str
    connection_version: int
    model_id: str


class PluginModelBindCommandReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["plugin.model.bind"]
    command_id: str
    plugin_id: str
    digest: str
    model_binding_revision: int
    model_binding: PluginModelBindingSummary


class PluginPublisherSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str


class PluginSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str
    version: str
    digest: str
    publisher: PluginPublisherSummary
    execution_kind: Literal["wasi", "managed_worker", "builtin"]
    signature_status: Literal["unsigned", "verified"]
    compatibility: Literal["compatible", "incompatible"]
    enabled: bool
    retired: bool


class PluginActionLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_ms: int
    memory_mb: int
    output_mb: int


class PluginActionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    description: str
    consumes: list[str]
    produces: list[str]
    effects: list[Literal["read", "artifact", "external"]]
    determinism: Literal["pure", "input_stable", "nondeterministic"]
    capabilities: list[str]
    limits: PluginActionLimits


class PluginCommandSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    description: str
    required_actions: list[str]


class PluginPathContributionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    path: str


class PluginVersionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    digest: str
    signature_status: Literal["unsigned", "verified"]
    compatibility: Literal["compatible", "incompatible"]
    state: Literal["installed", "retired"]
    active: bool
    created_at: str


class PluginDetail(PluginSummary):
    license: str | None = None
    actions: list[PluginActionSummary]
    skills: list[PluginPathContributionSummary]
    commands: list[PluginCommandSummary]
    mcp_servers: list[PluginPathContributionSummary]
    versions: list[PluginVersionSummary]
    model_binding: PluginModelBindingSummary | None = None


class ListPluginsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plugins: list[PluginSummary]


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


class ClearMemoryResponse(BaseModel):
    """DELETE /v1/memory — wipes the agent's long-term notes.

    Reported back so the renderer can show "cleared N memories" toast
    instead of a generic "done" message.
    """

    cleared: Literal[True] = True
    deleted_count: int


# ---------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------


class McpServerInfo(BaseModel):
    """One MCP Server configured for this Runtime.

    `name` is the unique key the user (or installer tool) gave it.
    `transport` is the normalized transport — `stdio` / `streamable_http`
    / `sse` / `websocket`. `command` / `args` / `url` / `env_keys` are
    descriptive only — we never echo env *values* (could be secrets).
    `source` is one of `shejane` / `shejane-legacy` / `env` — used by
    the UI to group Runtime-owned configuration by provenance.
    `source_path` is the absolute path of the config file the entry was
    read from, displayed in the settings panel so the user knows where
    to go edit it.
    """

    name: str
    transport: str
    source: str
    source_path: str
    command: str | None = None
    args: list[str] = []
    url: str | None = None
    env_keys: list[str] = []
    cwd: str | None = None
    status: Literal["idle", "ready", "error"] = "idle"
    tool_count: int = 0
    error_type: str | None = None


class McpServerCatalog(BaseModel):
    """GET /v1/mcp-servers — Runtime-owned MCP Servers and sources."""

    servers: list[McpServerInfo]
    sources_scanned: list[str]


class McpServerWriteRequest(BaseModel):
    """Create/update one SheJane-managed MCP server.

    This writes only `~/.shejane/mcp-servers.json`.
    """

    name: str | None = None
    transport: str = "stdio"
    command: str | None = None
    args: list[str] = []
    url: str | None = None
    env: dict[str, str] = {}
    cwd: str | None = None


class McpServerWriteResponse(BaseModel):
    server: McpServerInfo


class McpServerDeleteResponse(BaseModel):
    deleted: bool = True
    name: str


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


class SkillWriteRequest(BaseModel):
    """Create/update one SheJane-managed skill under `~/.shejane/skills`."""

    name: str | None = None
    description: str = ""
    content: str | None = None


class SkillFile(BaseModel):
    name: str
    description: str = ""
    path: str
    root_path: str
    content: str


class SkillWriteResponse(BaseModel):
    skill: SkillFile


class SkillDeleteResponse(BaseModel):
    deleted: bool = True
    name: str
