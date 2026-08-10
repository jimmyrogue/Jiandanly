"""Thread snapshots and Run presentation projection schemas."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .run_state import LocalRun


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
