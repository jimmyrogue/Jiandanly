"""Run admission, scheduling, fork, injection, and plan schemas."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .run_state import PermissionMode
from .runtime import (
    MAX_LOCAL_REQUEST_BODY_BYTES,
    MAX_RUNTIME_MODEL_SPEC_LENGTH,
    RUNTIME_MODEL_PATTERN,
    ReasoningMode,
)


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
    reasoning_mode: ReasoningMode | None = None
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
    reasoning_mode: ReasoningMode | None = None
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
