"""Durable Run and Plugin command schemas and receipts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .decisions import (
    AnswerQuestionRequest,
    PermissionDecision,
    PermissionScope,
    ReconcileToolRequest,
    ResolvePermissionRequest,
)
from .run_requests import PlanApprovalDecision, ResolvePlanApprovalRequest
from .runtime import MAX_RUNTIME_MODEL_SPEC_LENGTH, RUNTIME_MODEL_PATTERN


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
