"""Runtime discovery, settings, and model-service schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

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
    recommended_for: list[ModelCapabilityName] = Field(default_factory=list, max_length=4)


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
