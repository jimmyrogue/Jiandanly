"""FastAPI application + HTTP route surface.

Phase 2' deliverables:
- `/v1/health` (no auth)
- `/v1/tools` (list available tools — placeholder for now)
- `/v1/workspaces` (CRUD authorization records)
- `/v1/runs` (placeholder: real impl lands in Phase 3')
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import add_messages
from sse_starlette.sse import EventSourceResponse

from . import __version__
from .agent.builder import _build_byok_chat_model, open_checkpointer, open_store
from .api_schemas import (
    AddModelServiceModelRequest,
    AnswerQuestionCommand,
    AnswerQuestionCommandReceipt,
    AnswerQuestionRequest,
    CancelRunCommand,
    CancelRunCommandReceipt,
    CancelRunResponse,
    CentralDiagnosticsStatusResponse,
    ClearMemoryResponse,
    ConnectModelServiceRequest,
    CreateRunRequest,
    CreateScheduledRunRequest,
    CreateWorkspaceRequest,
    DeleteLocalThreadResponse,
    DiagnoseWorkspaceRequest,
    FixedRuntimeAssetStatus,
    ForkRunRequest,
    HealthResponse,
    ImportModelServiceRequest,
    InjectRunInstructionRequest,
    InjectRunInstructionResponse,
    ListAgentMessagesResponse,
    ListChildRunsResponse,
    ListModelCapabilityBindingsResponse,
    ListModelServiceConnectionsResponse,
    ListPluginsResponse,
    ListRunEventsResponse,
    ListRunsResponse,
    ListScheduledRunsResponse,
    ListThreadChangesResponse,
    ListThreadsResponse,
    ListWorkspacesResponse,
    LocalArtifact,
    LocalCollaborationSnapshot,
    LocalRun,
    LocalRunDiagnostics,
    LocalRuntimeModelCatalog,
    LocalScheduledRun,
    LocalThread,
    LocalThreadSnapshot,
    LocalWorkspaceAuthorization,
    LocalWorkspaceDiagnosis,
    McpServerCatalog,
    McpServerDeleteResponse,
    McpServerInfo,
    McpServerWriteRequest,
    McpServerWriteResponse,
    ModelCapabilityBinding,
    ModelServiceConnection,
    ModelServiceModel,
    ModelServicePresetCatalog,
    PermissionResolution,
    PlanApprovalResolution,
    PlanResolveCommand,
    PlanResolveCommandReceipt,
    PluginDetail,
    PluginDisableCommand,
    PluginEnableCommand,
    PluginInstallCommand,
    PluginInstallCommandReceipt,
    PluginModelBindCommand,
    PluginModelBindCommandReceipt,
    PluginReadinessSnapshot,
    PluginRemoveCommand,
    PluginRemoveCommandReceipt,
    PluginRollbackCommand,
    PluginSetupAdvanceCommand,
    PluginSetupAdvanceCommandReceipt,
    PluginStateCommandReceipt,
    PluginUpdateCommand,
    PluginVersionSwitchCommandReceipt,
    PptxOutlineResponse,
    QuestionAnswer,
    ReconcileToolRequest,
    ReconnectModelServiceRequest,
    ResolvePermissionCommand,
    ResolvePermissionCommandReceipt,
    ResolvePermissionRequest,
    ResolvePlanApprovalRequest,
    RuntimeAssetCleanupResult,
    RuntimeAssetInstallCommand,
    RuntimeAssetInstallCommandReceipt,
    RuntimeAssetStorage,
    RuntimeInfo,
    RuntimeSettingsResponse,
    SetModelCapabilityBindingRequest,
    SheJaneAuthorizationStartResponse,
    SheJaneAuthorizationStatusResponse,
    SkillDeleteResponse,
    SkillFile,
    SkillWriteRequest,
    SkillWriteResponse,
    ToolReconcileCommand,
    ToolReconcileCommandReceipt,
    ToolReconciliationResolution,
    UpdateCentralDiagnosticsRequest,
    UpdateLocalThreadRequest,
    UpdateRuntimeSettingsRequest,
    VerifyModelServiceModelRequest,
)
from .auth import LOCAL_OWNER_PRINCIPAL_ID, PairingTokenAuthMiddleware
from .central_diagnostics import (
    CentralDiagnosticsConfigurationError,
    CentralDiagnosticsManager,
    CentralDiagnosticsUnavailable,
)
from .config import Settings, get_settings
from .diagnostics_trace import build_run_trace
from .failure_policy import classify_failure_payload
from .http_body_limit import RequestBodyLimitMiddleware
from .llm.ledger import _provider_tools, _rewrite_tool_names
from .middleware.tool_execution import serialize_tool_result
from .model_credentials import (
    CredentialStoreError,
    credential_ref,
    delete_model_api_key,
    get_model_api_key,
    new_credential_ref,
    set_model_api_key,
)
from .model_profiles import (
    MODEL_CAPABILITY_ORDER,
    apply_known_model_profile_defaults,
    default_model_protocol,
    discovered_model_profile,
    model_capability,
    normalized_model_capabilities,
)
from .model_services import (
    adapter_for_custom_service,
    list_model_service_presets,
    model_service_preset,
    openai_compatible_endpoint,
)
from .permission_policy import PermissionScopeNotAllowedError
from .plugins.browser_qa import BROWSER_QA_PLUGIN_ID
from .plugins.catalog import PluginCatalog
from .plugins.platforms import current_managed_worker_platform
from .plugins.registry import PluginRegistry, PluginRegistryError
from .progress_ledger import (
    latest_feature_ledger as _latest_feature_ledger,
)
from .progress_ledger import (
    progress_ledger_state as _progress_ledger_state,
)
from .runs import (
    RUNTIME_PROTOCOL_VERSION,
    CheckpointNotFoundError,
    RunCoordinator,
    RunNotFoundError,
    freeze_run_settings,
    runtime_capabilities,
    sanitize_run_metadata,
)
from .scheduler import ScheduledRunDispatcher
from .shejane_authorization import (
    OFFICIAL_CLOUD_ORIGIN,
    OfficialServiceUnavailable,
    SheJaneAuthorizationManager,
)
from .store.sqlite import (
    ArtifactConflictError,
    CommandConflictError,
    LocalStore,
    ParentRunAdmissionError,
    PermissionDecisionConflictError,
    RunAdmissionError,
    RunInputSnapshotError,
    RunResultConflictError,
    ThreadAdmissionError,
    WaitDecisionConflictError,
    WorkspaceAdmissionError,
)

log = logging.getLogger("shejane_runtime.server")

_RUNTIME_SETTINGS_TO_FIELDS = {
    "max_model_calls": "max_model_calls",
    "max_tool_retries": "max_tool_retries",
    "research_search_limit": "research_search_limit",
    "unknown_model_max_input_tokens": "unknown_model_max_input_tokens",
    "unknown_model_max_output_tokens": "unknown_model_max_output_tokens",
    "model_request_timeout_seconds": "model_request_timeout_seconds",
    "browser_headless": "browser_headless",
    "subagents": "enable_subagents",
    "input_guard": "input_guard_mode",
    "plan_first": "plan_first_mode",
    "verification_repair_max": "verification_repair_max",
    "repair_workflow_max": "repair_workflow_max",
    "pii_redact": "pii_redact_types",
}


def _fixed_runtime_asset_sources(settings: Settings) -> dict[str, Path | str]:
    sources: dict[str, Path | str] = {}
    if settings.browser_qa_runtime_asset is not None:
        sources[BROWSER_QA_PLUGIN_ID + ".runtime"] = settings.browser_qa_runtime_asset
    if settings.ocr_runtime_asset is not None:
        sources["org.rapidocr.runtime"] = settings.ocr_runtime_asset
    if settings.fixed_runtime_asset_base_url is None:
        return sources
    platform = current_managed_worker_platform()
    target = {
        "darwin/arm64": "darwin-arm64",
        "windows/amd64": "windows-amd64",
    }.get(platform)
    if target is None:
        return sources
    filenames = {
        BROWSER_QA_PLUGIN_ID + ".runtime": (
            f"browser-qa-runtime-1.61.1-{target}.shejane-runtime-asset"
        ),
        "org.rapidocr.runtime": f"rapidocr-runtime-3.9.1-{target}.shejane-runtime-asset",
    }
    for asset_id, filename in filenames.items():
        sources.setdefault(
            asset_id,
            urljoin(settings.fixed_runtime_asset_base_url + "/", filename),
        )
    return sources


def _runtime_settings_payload(settings: Settings, *, version: int) -> dict[str, Any]:
    return {
        "version": version,
        **{
            public_name: getattr(settings, field_name)
            for public_name, field_name in _RUNTIME_SETTINGS_TO_FIELDS.items()
        },
    }


def _apply_runtime_settings(settings: Settings, values: dict[str, Any]) -> Settings:
    updates = {
        field_name: values[public_name]
        for public_name, field_name in _RUNTIME_SETTINGS_TO_FIELDS.items()
        if public_name in values
    }
    return settings.model_copy(update=updates)


_HANDOFF_STATUSES = {"completed", "failed", "canceled", "waiting_permission", "waiting_input"}
_TERMINAL_RUN_STATUSES = {"completed", "failed", "canceled", "cleanup_required"}


def _model_service_base_url(raw: str) -> str:
    value = raw.strip().rstrip("/")
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        raise HTTPException(status_code=400, detail="model service address is invalid")
    return value


def _model_connection_models(row: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        models = json.loads(row.get("models_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(models, list):
        return []
    normalized_models: list[dict[str, Any]] = []
    for model in models:
        if not isinstance(model, dict):
            continue
        normalized = apply_known_model_profile_defaults(
            model,
            service_base_url=str(row.get("base_url") or ""),
        )
        normalized["capabilities"] = normalized_model_capabilities(
            normalized,
            adapter_id=str(row.get("adapter_id") or "openai_chat"),
        )
        normalized.pop("purpose", None)
        normalized.pop("protocol", None)
        normalized["verification"] = (
            "verified"
            if any(item["verification"] == "verified" for item in normalized["capabilities"])
            else "unverified"
        )
        normalized_models.append(normalized)
    return normalized_models


def _merge_refreshed_model_catalog(
    current: list[dict[str, Any]],
    refreshed: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    current_by_id = {str(model["model_id"]): model for model in current}
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for model in refreshed:
        model_id = str(model["model_id"])
        previous = current_by_id.get(model_id)
        if previous and previous.get("capabilities"):
            model = {
                **model,
                "capabilities": list(previous.get("capabilities") or []),
                "verification": previous.get("verification", "unverified"),
                "streaming": bool(previous.get("streaming")),
                "tool_calling": bool(previous.get("tool_calling")),
                "image_inputs": bool(previous.get("image_inputs")),
            }
        merged.append(model)
        seen.add(model_id)
    merged.extend(
        model
        for model in current
        if model.get("source") == "manual" and str(model["model_id"]) not in seen
    )
    return merged


async def _model_service_response(
    row: dict[str, Any],
    *,
    credential_configured: bool | None = None,
) -> ModelServiceConnection:
    configured = credential_configured
    if configured is None:
        try:
            configured = bool(
                await get_model_api_key(
                    str(row["principal_id"]),
                    str(row["id"]),
                    str(row["credential_ref"]),
                )
            )
        except CredentialStoreError:
            configured = False
    return ModelServiceConnection(
        id=str(row["id"]),
        preset_id=str(row["preset_id"]),
        name=str(row["name"]),
        region=str(row["region"]),
        adapter_id=str(row["adapter_id"]),
        base_url=str(row["base_url"]),
        credential_configured=configured,
        catalog_status=str(row["catalog_status"]),
        models=_model_connection_models(row),
        version=int(row.get("version") or 1),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


async def _model_capability_binding_response(
    store: LocalStore,
    *,
    principal_id: str,
    row: dict[str, Any],
) -> dict[str, Any]:
    connection = await store.get_model_connection(
        principal_id=principal_id,
        connection_id=str(row["connection_id"]),
    )
    capability = None
    if connection is not None:
        model = next(
            (
                item
                for item in _model_connection_models(connection)
                if item.get("model_id") == row["model_id"]
            ),
            None,
        )
        if model is not None:
            capability = model_capability(model, str(row["capability"]))
    ready = bool(
        connection is not None
        and int(connection.get("version") or 1) == int(row["connection_version"])
        and capability is not None
        and capability.get("verification") == "verified"
        and capability.get("protocol") == row["protocol"]
    )
    return {
        "capability": row["capability"],
        "model_spec": f"local:{row['connection_id']}:{row['model_id']}",
        "connection_id": row["connection_id"],
        "connection_version": row["connection_version"],
        "model_id": row["model_id"],
        "protocol": row["protocol"],
        "status": "ready" if ready else "stale",
        "revision": row["revision"],
        "updated_at": row["updated_at"],
    }


async def _refresh_model_service_models(
    *,
    preset: dict[str, Any],
    base_url: str,
    adapter_id: str,
    api_key: str,
) -> tuple[list[dict[str, Any]], str]:
    bundled = [dict(model) for model in preset.get("models", ())]
    headers = {"Accept": "application/json"}
    discovery_url = openai_compatible_endpoint(base_url, "models")
    if adapter_id == "anthropic_messages":
        discovery_url = (
            f"{base_url}/models" if base_url.endswith("/v1") else f"{base_url}/v1/models"
        )
        headers["anthropic-version"] = "2023-06-01"
        headers["x-api-key"] = api_key
    elif adapter_id == "google_genai":
        discovery_url = f"{base_url.rstrip('/')}/v1beta/models?pageSize=1000"
        headers["x-goog-api-key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(
            timeout=8.0,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.get(discovery_url, headers=headers)
    except httpx.RequestError:
        return bundled, "stale" if bundled else "unavailable"

    if response.status_code == 401:
        raise HTTPException(status_code=401, detail="model service API key is invalid")
    if not response.is_success:
        return bundled, "stale" if bundled else "unavailable"
    try:
        payload = response.json()
    except ValueError:
        return bundled, "stale" if bundled else "unavailable"
    candidates = (
        payload.get("models")
        if adapter_id == "google_genai" and isinstance(payload, dict)
        else payload.get("data")
        if isinstance(payload, dict)
        else None
    )
    if not isinstance(candidates, list):
        return bundled, "stale" if bundled else "unavailable"

    bundled_by_id = {str(model["model_id"]): model for model in bundled}
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        model_id = (
            candidate.get("baseModelId") or str(candidate.get("name") or "").removeprefix("models/")
            if adapter_id == "google_genai"
            else candidate.get("id")
        )
        if (
            not isinstance(model_id, str)
            or not model_id
            or len(model_id) > 200
            or any(character.isspace() for character in model_id)
            or model_id in seen
        ):
            continue
        seen.add(model_id)
        raw_name = (
            candidate.get("displayName")
            if adapter_id == "google_genai"
            else candidate.get("name") or candidate.get("display_name")
        )
        display_name = raw_name.strip() if isinstance(raw_name, str) else model_id
        known = bundled_by_id.get(model_id)
        normalized_candidate = (
            {
                **candidate,
                "context_length": candidate.get("inputTokenLimit"),
                "max_output_tokens": candidate.get("outputTokenLimit"),
            }
            if adapter_id == "google_genai"
            else candidate
        )
        profile = discovered_model_profile(
            normalized_candidate,
            model_id=model_id,
            display_name=display_name[:100] or model_id[:100],
            service_base_url=base_url,
        )
        profile.update(
            {
                "source": "bundled" if known else "discovered",
                "verification": "unverified",
                "recommended": bool(known and known.get("recommended")),
            }
        )
        if known:
            profile.update(
                {
                    key: value
                    for key, value in known.items()
                    if key != "verification" and value is not None
                }
            )
            profile["capabilities"] = normalized_model_capabilities(
                {**known, "source": "bundled"},
                adapter_id=adapter_id,
            )
        if preset.get("id") == "shejane-official":
            declared_capabilities = candidate.get("capabilities")
            if isinstance(declared_capabilities, list):
                profile["capabilities"] = normalized_model_capabilities(
                    {
                        "capabilities": [
                            {
                                "capability": capability,
                                "protocol": default_model_protocol(adapter_id, capability),
                                "verification": "verified",
                            }
                            for capability in declared_capabilities
                            if isinstance(capability, str) and capability in MODEL_CAPABILITY_ORDER
                        ]
                    },
                    adapter_id=adapter_id,
                )
            recommended_for = candidate.get("recommended_for")
            if isinstance(recommended_for, list):
                profile["recommended"] = any(
                    isinstance(capability, str)
                    and model_capability(profile, capability) is not None
                    for capability in recommended_for
                )
        models.append(profile)
        if len(models) >= 1000:
            break
    for model in bundled:
        if model["model_id"] not in seen:
            models.append(model)
    return models, "ready"


async def _verify_model_service_compatibility(
    *,
    settings: Settings,
    base_url: str,
    adapter_id: str,
    protocol: str | None = None,
    api_key: str,
    model_id: str,
) -> None:
    # ponytail: bound this paid probe; use per-model budgets if 4K truncates real tool calls.
    probe_max_tokens = 4_096
    success_signal = "SHEJANE_MODEL_TOOL_LOOP_OK"
    ping = StructuredTool.from_function(
        lambda: success_signal,
        name="shejane.ping",
        description="Return a compatibility signal.",
    )
    try:
        model = _build_byok_chat_model(
            settings=settings,
            model_binding={
                "adapter_id": adapter_id,
                "protocol": protocol or default_model_protocol(adapter_id, "agent_chat"),
                "base_url": base_url,
                "model_id": model_id,
                "profile": {
                    "tool_calling": True,
                    "image_inputs": False,
                    "max_output_tokens": probe_max_tokens,
                },
            },
            model_api_key=api_key,
        ).bind(max_tokens=probe_max_tokens)
        prompt = HumanMessage(
            content=(
                "Call the ping tool exactly once. After receiving its result, "
                f"answer exactly {success_signal}."
            )
        )
        provider_tools, aliases, choices = _provider_tools([ping])
        bound = model.bind_tools(provider_tools)
        tool_request = _rewrite_tool_names(await bound.ainvoke([prompt]), aliases)
        calls = list(getattr(tool_request, "tool_calls", ()) or ())
        if len(calls) != 1 or calls[0].get("name") != "shejane.ping" or not calls[0].get("id"):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "incompatible_model",
                    "message": "模型没有完成工具调用，无法用于 Agent 任务。",
                },
            )
        result = ping.invoke(calls[0].get("args") or {})
        provider_tool_request = _rewrite_tool_names(tool_request, choices)
        provider_result = _rewrite_tool_names(
            ToolMessage(
                content=result,
                name="shejane.ping",
                tool_call_id=str(calls[0]["id"]),
            ),
            choices,
        )
        final = _rewrite_tool_names(
            await bound.ainvoke(
                [
                    prompt,
                    provider_tool_request,
                    provider_result,
                ]
            ),
            aliases,
        )
    except HTTPException:
        raise
    except Exception as exc:
        status_code = getattr(exc, "status_code", None)
        if status_code == 401:
            raise HTTPException(
                status_code=401,
                detail={"code": "invalid_api_key", "message": "API Key 无效，请检查后重试。"},
            ) from exc
        if status_code == 403:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "provider_permission_denied",
                    "message": "当前账户或 API Key 没有调用该模型的权限。",
                },
            ) from exc
        if status_code == 402:
            raise HTTPException(
                status_code=402,
                detail={"code": "billing_required", "message": "模型服务账户余额或额度不足。"},
            ) from exc
        if status_code == 429:
            raise HTTPException(
                status_code=429,
                detail={"code": "rate_limited", "message": "模型服务请求过于频繁，请稍后重试。"},
            ) from exc
        if status_code == 408 or (isinstance(status_code, int) and status_code >= 500):
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "provider_unavailable",
                    "message": "模型服务暂时无法连接，请稍后重试。",
                },
            ) from exc
        upstream = re.sub(r"\s+", " ", str(exc).replace(api_key, "[redacted]")).strip()[:240]
        raise HTTPException(
            status_code=409,
            detail={
                "code": "incompatible_model",
                "message": (
                    "模型服务拒绝了完整工具调用测试，请检查模型与接口格式。"
                    + (f" 服务返回：{upstream}" if upstream else "")
                ),
            },
        ) from exc
    content = final.content
    final_text = (
        content
        if isinstance(content, str)
        else "".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
        if isinstance(content, list)
        else ""
    )
    if getattr(final, "tool_calls", None) or final_text.strip() != success_signal:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "incompatible_model",
                "message": "模型没有在工具执行后返回最终答案。",
            },
        )


def _adapter_for_model_protocol(protocol: str) -> str:
    if protocol in {"openai_chat_completions", "openai_responses"}:
        return "openai_chat"
    if protocol == "anthropic_messages":
        return "anthropic_messages"
    if protocol == "google_generate_content":
        return "google_genai"
    raise HTTPException(status_code=422, detail="selected protocol is not a chat protocol")


def _model_probe_failure(exc: Exception, *, api_key: str, message: str) -> HTTPException:
    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status_code is None:
        status_code = getattr(response, "status_code", None)
    if response is not None:
        try:
            payload = response.json()
        except (ValueError, TypeError):
            payload = None
        provider_error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(provider_error, dict) and provider_error.get("code") == "get_channel_failed":
            upstream = re.sub(
                r"\s+",
                " ",
                str(provider_error.get("message") or "").replace(api_key, "[redacted]"),
            ).strip()[:240]
            return HTTPException(
                status_code=409,
                detail={
                    "code": "model_unavailable",
                    "message": (
                        "当前 API Key 所在分组没有此模型的可用渠道，请更换模型、分组或 API Key。"
                        + (f" 服务返回：{upstream}" if upstream else "")
                    ),
                },
            )
    if status_code == 401:
        return HTTPException(
            status_code=401,
            detail={"code": "invalid_api_key", "message": "API Key 无效，请检查后重试。"},
        )
    if status_code == 403:
        return HTTPException(
            status_code=403,
            detail={
                "code": "provider_permission_denied",
                "message": "当前账户或 API Key 没有调用该模型的权限。",
            },
        )
    if status_code == 402:
        return HTTPException(
            status_code=402,
            detail={"code": "billing_required", "message": "模型服务账户余额或额度不足。"},
        )
    if status_code == 429:
        return HTTPException(
            status_code=429,
            detail={"code": "rate_limited", "message": "模型服务请求过于频繁，请稍后重试。"},
        )
    if (
        isinstance(exc, httpx.RequestError)
        or status_code == 408
        or (isinstance(status_code, int) and status_code >= 500)
    ):
        return HTTPException(
            status_code=503,
            detail={
                "code": "provider_unavailable",
                "message": "模型服务暂时无法连接，请稍后重试。",
            },
        )
    upstream = re.sub(r"\s+", " ", str(exc).replace(api_key, "[redacted]")).strip()[:240]
    return HTTPException(
        status_code=409,
        detail={
            "code": "incompatible_model",
            "message": message + (f" 服务返回：{upstream}" if upstream else ""),
        },
    )


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return ""


async def _verify_model_image_understanding(
    *,
    settings: Settings,
    base_url: str,
    protocol: str,
    api_key: str,
    model_id: str,
) -> None:
    success_signal = "RED"
    image_base64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAEUlEQVR42mP8z4AdMDEMKQkA"
        "zUEBD7t4NqoAAAAASUVORK5CYII="
    )
    image_block: dict[str, Any]
    if protocol == "anthropic_messages":
        image_block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": image_base64,
            },
        }
    else:
        image_block = {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{image_base64}"},
        }
    try:
        model = _build_byok_chat_model(
            settings=settings,
            model_binding={
                "adapter_id": _adapter_for_model_protocol(protocol),
                "protocol": protocol,
                "base_url": base_url,
                "model_id": model_id,
                "profile": {"image_inputs": True, "max_output_tokens": 64},
            },
            model_api_key=api_key,
        ).bind(max_tokens=64)
        response = await model.ainvoke(
            [
                HumanMessage(
                    content=[
                        {
                            "type": "text",
                            "text": (
                                "What is the dominant color in this image? "
                                "Reply with one uppercase English word only."
                            ),
                        },
                        image_block,
                    ]
                )
            ]
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _model_probe_failure(
            exc,
            api_key=api_key,
            message="模型服务拒绝了图片输入测试，请检查模型与接口格式。",
        ) from exc
    if _message_text(response.content).strip() != success_signal:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "incompatible_model",
                "message": "模型没有正确响应图片输入测试。",
            },
        )


async def _verify_model_image_generation(
    *,
    settings: Settings,
    base_url: str,
    api_key: str,
    model_id: str,
) -> None:
    try:
        async with httpx.AsyncClient(
            timeout=settings.model_request_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(
                openai_compatible_endpoint(base_url, "images/generations"),
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model_id,
                    "prompt": "A plain white square on a plain white background.",
                    "n": 1,
                },
            )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise _model_probe_failure(
            exc,
            api_key=api_key,
            message="模型服务拒绝了图片生成测试，请检查模型与接口格式。",
        ) from exc
    raw_results = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(raw_results, dict):
        raw_results = raw_results.get("images", [raw_results])
    if raw_results is None and isinstance(payload, dict):
        raw_results = payload.get("images")
    valid_result = isinstance(raw_results, list) and any(
        isinstance(item, dict)
        and bool(item.get("url") or item.get("b64_json") or item.get("base64"))
        for item in raw_results
    )
    if not valid_result:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "incompatible_model",
                "message": "图片生成接口没有返回可用的图片结果。",
            },
        )


async def _verify_model_image_editing(
    *,
    settings: Settings,
    base_url: str,
    api_key: str,
    model_id: str,
) -> None:
    test_png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAEUlEQVR42mP8z4AdMDEMKQkA"
        "zUEBD7t4NqoAAAAASUVORK5CYII="
    )
    try:
        async with httpx.AsyncClient(
            timeout=settings.model_request_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(
                openai_compatible_endpoint(base_url, "images/edits"),
                headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
                data={
                    "model": model_id,
                    "prompt": "Keep this plain red square unchanged.",
                    "n": "1",
                },
                files={"image": ("probe.png", test_png, "image/png")},
            )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise _model_probe_failure(
            exc,
            api_key=api_key,
            message="模型服务拒绝了图片编辑测试，请检查模型与接口格式。",
        ) from exc
    raw_results = payload.get("data") if isinstance(payload, dict) else None
    if not (
        isinstance(raw_results, list)
        and any(
            isinstance(item, dict)
            and bool(item.get("url") or item.get("b64_json") or item.get("base64"))
            for item in raw_results
        )
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "incompatible_model",
                "message": "图片编辑接口没有返回可用的图片结果。",
            },
        )


async def _verify_model_service_capability(
    *,
    settings: Settings,
    base_url: str,
    capability: str,
    protocol: str,
    api_key: str,
    model_id: str,
) -> None:
    if capability == "agent_chat":
        await _verify_model_service_compatibility(
            settings=settings,
            base_url=base_url,
            adapter_id=_adapter_for_model_protocol(protocol),
            protocol=protocol,
            api_key=api_key,
            model_id=model_id,
        )
        return
    if capability == "image_understanding":
        await _verify_model_image_understanding(
            settings=settings,
            base_url=base_url,
            protocol=protocol,
            api_key=api_key,
            model_id=model_id,
        )
        return
    if capability == "image_generation":
        await _verify_model_image_generation(
            settings=settings,
            base_url=base_url,
            api_key=api_key,
            model_id=model_id,
        )
        return
    await _verify_model_image_editing(
        settings=settings,
        base_url=base_url,
        api_key=api_key,
        model_id=model_id,
    )


async def _verify_bundled_model_catalog(
    *,
    settings: Settings,
    base_url: str,
    adapter_id: str,
    api_key: str,
    models: list[dict[str, Any]],
    include_discovered: bool = False,
) -> list[dict[str, Any]]:
    verified_models: list[dict[str, Any]] = []
    for raw_model in models:
        model = {**raw_model, "verification": "unverified"}
        if model.get("source") == "bundled" or (
            include_discovered and model.get("source") == "discovered"
        ):
            model["capabilities"] = normalized_model_capabilities(
                model,
                adapter_id=adapter_id,
            )
            if model["capabilities"] and model_capability(model, "agent_chat") is None:
                verified_models.append(model)
                continue
            try:
                await _verify_model_service_compatibility(
                    settings=settings,
                    base_url=base_url,
                    adapter_id=adapter_id,
                    api_key=api_key,
                    model_id=str(model["model_id"]),
                )
            except HTTPException as exc:
                if exc.status_code in {401, 402, 403}:
                    raise
                log.info(
                    "bundled model compatibility probe failed model=%s status=%s",
                    model["model_id"],
                    exc.status_code,
                )
            else:
                for item in model["capabilities"]:
                    if item["capability"] in {"agent_chat", "image_understanding"}:
                        item["verification"] = "verified"
                model.update({"verification": "verified", "tool_calling": True, "streaming": True})
        verified_models.append(model)
    return verified_models


async def _complete_shejane_authorization(
    app: FastAPI,
    principal_id: str,
    token: str,
) -> dict[str, Any]:
    preset = model_service_preset("shejane-official")
    assert preset is not None
    official_api_base_url = openai_compatible_endpoint(OFFICIAL_CLOUD_ORIGIN, "").rstrip("/")
    connection_id = f"conn_{uuid.uuid4().hex}"
    next_credential_ref = credential_ref(connection_id)
    try:
        await set_model_api_key(
            principal_id,
            connection_id,
            token,
            next_credential_ref,
        )
    except CredentialStoreError as exc:
        raise RuntimeError("system credential store is unavailable") from exc

    try:
        models, catalog_status = await _refresh_model_service_models(
            preset=preset,
            base_url=official_api_base_url,
            adapter_id="openai_chat",
            api_key=token,
        )
        models = await _verify_bundled_model_catalog(
            settings=app.state.settings,
            base_url=official_api_base_url,
            adapter_id="openai_chat",
            api_key=token,
            models=models,
            include_discovered=True,
        )
        row = await app.state.store.create_model_connection(
            principal_id=principal_id,
            connection_id=connection_id,
            preset_id="shejane-official",
            name=str(preset["name"]),
            region="official",
            adapter_id="openai_chat",
            base_url=official_api_base_url,
            requires_api_key=True,
            credential_ref=next_credential_ref,
            models=models,
            catalog_status=catalog_status,
        )
    except BaseException:
        await delete_model_api_key(principal_id, connection_id, next_credential_ref)
        raise
    return (await _model_service_response(row, credential_configured=True)).model_dump()


def _list_skill_files() -> list[dict[str, str]]:
    """Lightweight skill catalog for the HTTP layer — independent of any
    running agent. Walks every roots `_resolve_skills_dirs` returns and
    finds skills in the Anthropic / skills.sh format: each skill is a
    directory containing `SKILL.md` with YAML frontmatter. Returns
    `{name, title, description, path, source}` where `source` is the
    last component of the root (`shejane`, `claude`, …) so the UI can
    group entries by provenance.

    Skill *invocation* (loading full content into prompts) happens via
    deepagents SkillsMiddleware inside a run; this endpoint just answers
    "what's available?". Only the SKILL.md directory format is listed
    because deepagents only loads that format — a flat `.md` would show
    up here but never reach the model.
    """
    from .agent.builder import _resolve_skills_dirs

    out: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for root in _resolve_skills_dirs():
        # `source` is the parent's name stripped of any leading dot, so
        # `~/.shejane/skills/` reports `shejane`, `~/.claude/skills/`
        # reports `claude`, and a custom env override like
        # `/abs/foo/skills/` reports `foo`. This is what the renderer
        # groups by — `root.name` itself is always literally "skills"
        # so it's useless as a label.
        source = (root.parent.name or root.name).lstrip(".")
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name.startswith(("_", ".")):
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.is_file():
                continue
            try:
                text = skill_md.read_text(encoding="utf-8")
            except OSError:
                continue
            # Use frontmatter `name` over directory name when present;
            # fall back to dir name. Dedupe across roots — first source
            # wins, matching deepagents' "later sources override earlier"
            # convention in reverse (we list shejane first so it's the
            # canonical name when both roots have the same skill).
            title, description = _parse_frontmatter_minimal(text)
            display_name = entry.name
            if display_name in seen_names:
                continue
            seen_names.add(display_name)
            out.append(
                {
                    "name": display_name,
                    "title": title or display_name,
                    "description": description,
                    "path": str(skill_md),
                    "source": source,
                    "root_path": str(root),
                }
            )
    return out


def _parse_frontmatter_minimal(text: str) -> tuple[str, str]:
    """Extract display metadata from Skill YAML frontmatter."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.DOTALL)
    if match is None:
        return "", ""
    try:
        metadata = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return "", ""
    if not isinstance(metadata, dict):
        return "", ""
    title = metadata.get("title") or metadata.get("name") or ""
    description = metadata.get("description") or ""
    return str(title), str(description)


_SAFE_CATALOG_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


def _safe_catalog_name(raw: str | None) -> str:
    name = (raw or "").strip()
    if not _SAFE_CATALOG_NAME_RE.fullmatch(name):
        raise HTTPException(
            status_code=400,
            detail="name must start with a letter or number and contain only letters, numbers, '.', '_' or '-'",
        )
    return name


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    _write_text_atomic(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _normalize_schedule_time(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise HTTPException(status_code=400, detail="run_at required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="run_at must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


async def _owned_run(
    store: LocalStore,
    *,
    principal_id: str,
    run_id: str,
    not_found_detail: str = "run not found",
) -> dict[str, Any]:
    run = await store.get_run_for_principal(principal_id=principal_id, run_id=run_id)
    if run is None:
        detail: str | dict[str, str] = not_found_detail
        if not_found_detail == "run not found":
            detail = {"code": "run_not_found", "message": not_found_detail}
        raise HTTPException(status_code=404, detail=detail)
    return run


async def _run_with_inputs(store: LocalStore, run: dict[str, Any]) -> dict[str, Any]:
    return (await _runs_with_inputs(store, [run]))[0]


async def _runs_with_inputs(store: LocalStore, runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    run_ids = [str(run["id"]) for run in runs]
    missing_subagent_ids = [str(run["id"]) for run in runs if "subagent_invocations" not in run]
    rows, subagent_rows, child_rows = await asyncio.gather(
        store.list_run_inputs_for_runs(run_ids),
        store.list_subagent_invocations_for_runs(missing_subagent_ids),
        store.list_child_runs_for_runs(run_ids),
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["run_id"]), []).append(row)
    grouped_subagents: dict[str, list[dict[str, Any]]] = {}
    for row in subagent_rows:
        grouped_subagents.setdefault(str(row["parent_run_id"]), []).append(row)
    grouped_children: dict[str, list[dict[str, Any]]] = {}
    for row in child_rows:
        grouped_children.setdefault(str(row["parent_run_id"]), []).append(row)
    return [
        {
            **run,
            "inputs": [
                {
                    "client_index": index,
                    **{
                        key: item[key]
                        for key in (
                            "input_id",
                            "virtual_path",
                            "original_name",
                            "media_type",
                            "bytes",
                            "sha256",
                        )
                    },
                }
                for index, item in enumerate(grouped.get(str(run["id"]), []))
            ],
            "subagent_invocations": run.get(
                "subagent_invocations",
                grouped_subagents.get(str(run["id"]), []),
            ),
            "child_runs": run.get(
                "child_runs",
                grouped_children.get(str(run["id"]), []),
            ),
        }
        for run in runs
    ]


async def _normalized_path(raw: str) -> str:
    return await asyncio.to_thread(
        lambda: str(Path(os.path.abspath(os.path.expanduser(raw))).resolve())
    )


async def _authorized_workspace_path(
    store: LocalStore, *, principal_id: str, path: str | None
) -> str | None:
    if path is None:
        return None
    resolved = await _normalized_path(path)
    workspace = await store.workspace_by_path(principal_id=principal_id, path=resolved)
    if workspace is None:
        raise HTTPException(status_code=403, detail="workspace is not authorized")
    workspace_error = await store.workspace_admission_error(
        principal_id=principal_id,
        path=resolved,
    )
    if workspace_error is not None:
        raise HTTPException(status_code=409, detail=workspace_error)
    return resolved


def _shejane_mcp_config_path() -> Path:
    return Path.home() / ".shejane" / "mcp-servers.json"


def _read_shejane_mcp_config() -> dict[str, Any]:
    path = _shejane_mcp_config_path()
    if not path.exists():
        return {"mcpServers": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=400, detail=f"shejane MCP config is not readable JSON: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        return {"mcpServers": {}}
    servers = raw.get("mcpServers")
    if isinstance(servers, dict):
        return raw
    if all(isinstance(v, dict) and ("command" in v or "url" in v) for v in raw.values()):
        return {"mcpServers": raw}
    raw["mcpServers"] = {}
    return raw


def _mcp_info_from_config(name: str, config: dict[str, Any]) -> McpServerInfo:
    path = _shejane_mcp_config_path()
    return McpServerInfo(
        name=name,
        transport=str(config.get("transport") or "stdio"),
        source="shejane",
        source_path=str(path),
        command=config.get("command") if isinstance(config.get("command"), str) else None,
        args=[str(arg) for arg in config.get("args", []) or []],
        url=config.get("url") if isinstance(config.get("url"), str) else None,
        env_keys=sorted(str(key) for key in (config.get("env") or {}).keys()),
        cwd=config.get("cwd") if isinstance(config.get("cwd"), str) else None,
    )


def _personal_skills_root() -> Path:
    root = Path.home() / ".shejane" / "skills"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _skill_md_path(name: str) -> Path:
    root = _personal_skills_root().resolve()
    skill_dir = (root / name).resolve()
    if root not in skill_dir.parents:
        raise HTTPException(status_code=400, detail="skill path escapes personal skills root")
    return skill_dir / "SKILL.md"


def _default_skill_content(name: str, description: str) -> str:
    lines = ["---", f"name: {name}"]
    description = description.strip()
    if description:
        lines.append(f"description: {description}")
    lines.extend(["---", "", f"# {name}", ""])
    if description:
        lines.append(description)
        lines.append("")
    return "\n".join(lines)


def _skill_file_from_path(name: str, path: Path) -> SkillFile:
    if not path.is_file():
        raise HTTPException(status_code=404, detail="skill not found")
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"failed to read skill: {exc}") from exc
    _, description = _parse_frontmatter_minimal(content)
    return SkillFile(
        name=name,
        description=description,
        path=str(path),
        root_path=str(_personal_skills_root()),
        content=content,
    )


def _write_mcp_server(
    route_name: str | None, request: McpServerWriteRequest
) -> McpServerWriteResponse:
    from .tools.mcp import _normalize_entry

    name = _safe_catalog_name(route_name or request.name)
    raw: dict[str, Any] = {
        "transport": request.transport,
    }
    if request.command is not None:
        raw["command"] = request.command
    if request.args:
        raw["args"] = request.args
    if request.url is not None:
        raw["url"] = request.url
    if request.env:
        raw["env"] = request.env
    if request.cwd is not None:
        raw["cwd"] = request.cwd

    normalized = _normalize_entry(name, raw)
    if normalized is None:
        raise HTTPException(status_code=400, detail="MCP server must include command or url")

    config = _read_shejane_mcp_config()
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        servers = {}
        config["mcpServers"] = servers
    servers[name] = normalized
    _write_json_atomic(_shejane_mcp_config_path(), config)
    return McpServerWriteResponse(server=_mcp_info_from_config(name, normalized))


def _write_local_skill(route_name: str | None, request: SkillWriteRequest) -> SkillWriteResponse:
    name = _safe_catalog_name(route_name or request.name)
    path = _skill_md_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = request.content
    if content is None:
        content = _default_skill_content(name, request.description)
    content = _normalize_local_skill_content(name, request.description, content)
    try:
        _write_text_atomic(path, content)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"failed to write skill: {exc}") from exc
    return SkillWriteResponse(skill=_skill_file_from_path(name, path))


def _normalize_local_skill_content(name: str, description: str, content: str) -> str:
    match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", content, re.DOTALL)
    body = content
    metadata: dict[str, Any] = {}
    if match is not None:
        try:
            parsed = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            raise HTTPException(status_code=422, detail="invalid Skill YAML frontmatter") from exc
        if parsed is not None and not isinstance(parsed, dict):
            raise HTTPException(status_code=422, detail="Skill frontmatter must be an object")
        metadata = dict(parsed or {})
        body = content[match.end() :]
    metadata["name"] = name
    requested_description = description.strip()
    if requested_description:
        metadata["description"] = requested_description
    elif not str(metadata.get("description") or "").strip():
        metadata["description"] = name
    header = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).rstrip()
    body = body.lstrip("\n")
    return f"---\n{header}\n---\n{body}".rstrip() + "\n"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.bootstrap_settings
    settings.ensure_data_dir()
    # Make sure the canonical user-managed skills dir exists from boot —
    # otherwise it's invisible to the UI until the user manually creates
    # it, and the "Personal" section silently disappears from the list.
    (Path.home() / ".shejane" / "skills").mkdir(parents=True, exist_ok=True)
    store = await LocalStore.open(settings.runtime_db_path)
    persisted_settings = await store.get_runtime_settings()
    if persisted_settings is not None:
        old_defaults = persisted_settings["settings"]
        migration = {}
        if old_defaults.get("max_model_calls") == 20:
            migration["max_model_calls"] = 100
        if old_defaults.get("research_search_limit") == 3:
            migration["research_search_limit"] = 10
        if migration:
            persisted_settings = await store.patch_runtime_settings(
                migration,
                initial_settings=old_defaults,
            )
        settings = _apply_runtime_settings(settings, persisted_settings["settings"])
    checkpointer, ck_stack = await open_checkpointer(settings)
    agent_store, store_stack = await open_store(settings)
    central_diagnostics = CentralDiagnosticsManager(
        store=store,
        cloud_origin=OFFICIAL_CLOUD_ORIGIN,
        app_version=__version__,
    )
    plugin_catalog = PluginCatalog(
        settings.data_dir,
        runtime_asset_sources=_fixed_runtime_asset_sources(settings),
    )
    coordinator = RunCoordinator(
        store=store,
        checkpointer=checkpointer,
        agent_store=agent_store,
        settings=settings,
        plugin_catalog=plugin_catalog,
        terminal_callback=lambda run_id, status, payload: central_diagnostics.submit_terminal(
            run_id=run_id,
            status=status,
            payload=payload,
        ),
    )
    await coordinator.mcp_catalog.hydrate()
    coordinator.mcp_catalog.request_refresh()
    scheduler = ScheduledRunDispatcher(store=store, coordinator=coordinator)
    plugin_registry = PluginRegistry(
        store=store,
        data_dir=settings.data_dir,
        runtime_version=__version__,
        plugin_catalog=plugin_catalog,
        computer_use_package=settings.computer_use_package,
        browser_qa_package=settings.browser_qa_package,
        ocr_package=settings.ocr_package,
    )
    await plugin_registry.initialize_fixed_capabilities(LOCAL_OWNER_PRINCIPAL_ID)
    app.state.store = store
    app.state.plugin_registry = plugin_registry
    app.state.settings = settings
    app.state.checkpointer = checkpointer
    app.state.agent_store = agent_store
    app.state.coordinator = coordinator
    app.state.mcp_catalog = coordinator.mcp_catalog
    app.state.scheduler = scheduler
    app.state.shejane_authorization = SheJaneAuthorizationManager(
        cloud_origin=OFFICIAL_CLOUD_ORIGIN,
        app_version=__version__,
        complete=partial(_complete_shejane_authorization, app),
    )
    app.state.central_diagnostics = central_diagnostics
    app.state.runtime_settings_lock = asyncio.Lock()
    app.state.runtime_settings_version = int(
        persisted_settings["version"] if persisted_settings is not None else 0
    )
    # Reconcile runs the previous process left non-terminal (the runtime is
    # SIGKILLed on every `make dev` restart): fail dead queued/running
    # runs, leave waiting_permission runs resumable. Without this they sit
    # `running` forever and the client never sees a terminal state.
    await coordinator.recover_orphans()
    coordinator.start()
    await scheduler.recover_running()
    scheduler.start()
    log.info(
        "runtime started host=%s port=%s data=%s",
        settings.host,
        settings.port,
        settings.data_dir,
    )
    try:
        yield
    finally:
        await app.state.shejane_authorization.close()
        await scheduler.stop()
        await coordinator.stop()
        await coordinator.mcp_catalog.close()
        await store_stack.aclose()
        await ck_stack.aclose()
        await store.close()
        log.info("runtime shutdown clean")


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = get_settings()

    app = FastAPI(
        title="SheJane Runtime",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.bootstrap_settings = settings

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        # FastAPI normally includes the rejected input in its 422 payload.
        # Local requests can contain model-service credentials, so return only
        # the location, message, and error type across the entire API.
        errors = [
            {key: value for key, value in error.items() if key not in {"input", "ctx"}}
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": errors})

    app.add_middleware(RequestBodyLimitMiddleware)

    # Order matters: middleware added LAST runs FIRST on the request path
    # (Starlette wraps outward). PairingTokenAuthMiddleware must sit
    # behind CORSMiddleware so that:
    #   1. CORS preflight (OPTIONS) is answered by CORSMiddleware without
    #      ever hitting auth — preflight by spec carries no credentials,
    #      so rejecting it with 401 makes every browser fetch fail.
    #   2. Even authenticated-but-401 responses still ship the
    #      Access-Control-Allow-Origin header, otherwise the browser
    #      hides the error body from the JS layer.
    app.add_middleware(PairingTokenAuthMiddleware, token=settings.pairing_token)

    # CORS — the runtime binds loopback only, but the Vite dev server (and
    # the production Electron renderer when loaded over file://) live on a
    # different origin than `:17371`. Without these headers, every
    # browser-side fetch fails preflight. Bearer-token auth
    # (PairingTokenAuthMiddleware above) is the real gate; CORS is just
    # plumbing.
    #
    # Override via env if you front the runtime with a custom reverse proxy.
    cors_origins_env = os.environ.get("SHEJANE_RUNTIME_CORS_ORIGINS", "").strip()
    if cors_origins_env:
        allow_origins = [o.strip() for o in cors_origins_env.split(",") if o.strip()]
        allow_origin_regex = None
    else:
        # Permit any localhost/loopback origin (dev Vite at 55173, prod
        # Electron file:// shows up as `null`, plus any 5173/5174/etc.).
        allow_origins = ["null"]
        allow_origin_regex = r"^(?:https?://)?(?:127\.0\.0\.1|localhost)(?::\d+)?$"
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_origin_regex=allow_origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/v1/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        # Two consumers, two contracts — both must be satisfied:
        #   • the live Client contract test checks `ok == true`
        #   • runtime/sdk/src/client.ts:probeRuntime
        #     checks `status === "ok"` and reads mode/worker
        # The HealthResponse defaults already encode `ok=True status="ok"`
        # mode="ready" worker="python-langgraph" — only `version` and
        # `pairing_configured` need to be filled per-request.
        return HealthResponse(
            version=__version__,
            pairing_configured=bool(settings.pairing_token),
        )

    @app.get("/v1/runtime", response_model=RuntimeInfo)
    async def runtime_info(request: Request) -> RuntimeInfo:
        runtime_settings: Settings = app.state.settings
        service_configured = False
        store: LocalStore = app.state.store
        try:
            connections = await store.list_model_connections(
                principal_id=request.state.principal_id
            )
            for connection in connections:
                if await get_model_api_key(
                    request.state.principal_id,
                    str(connection["id"]),
                    str(connection["credential_ref"]),
                ):
                    service_configured = True
                    break
        except CredentialStoreError:
            service_configured = False
        return RuntimeInfo(
            protocol_version=RUNTIME_PROTOCOL_VERSION,
            runtime_version=__version__,
            capabilities=sorted(runtime_capabilities(runtime_settings)),
            model_service_configured=service_configured,
        )

    @app.get("/v1/settings", response_model=RuntimeSettingsResponse)
    async def get_runtime_settings() -> dict[str, Any]:
        return _runtime_settings_payload(
            app.state.settings,
            version=app.state.runtime_settings_version,
        )

    @app.put("/v1/settings", response_model=RuntimeSettingsResponse)
    async def update_runtime_settings(body: UpdateRuntimeSettingsRequest) -> dict[str, Any]:
        patch = body.model_dump(exclude_none=True)
        async with app.state.runtime_settings_lock:
            current = _runtime_settings_payload(
                app.state.settings,
                version=app.state.runtime_settings_version,
            )
            current.update(patch)
            validated = RuntimeSettingsResponse(**current)
            candidate_values = validated.model_dump(exclude={"version"})
            store: LocalStore = app.state.store
            stored = await store.patch_runtime_settings(
                patch,
                initial_settings=candidate_values,
            )
            persisted = RuntimeSettingsResponse(
                **{**candidate_values, **stored["settings"]},
                version=int(stored["version"]),
            )
            values = persisted.model_dump(exclude={"version"})
            updated = _apply_runtime_settings(app.state.settings, values)
            app.state.settings = updated
            app.state.coordinator.settings = updated
            app.state.runtime_settings_version = int(stored["version"])
            return _runtime_settings_payload(updated, version=int(stored["version"]))

    @app.get(
        "/v1/shejane/diagnostics",
        response_model=CentralDiagnosticsStatusResponse,
    )
    async def get_central_diagnostics(request: Request) -> dict[str, Any]:
        try:
            return await app.state.central_diagnostics.status(request.state.principal_id)
        except CredentialStoreError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.put(
        "/v1/shejane/diagnostics",
        response_model=CentralDiagnosticsStatusResponse,
    )
    async def update_central_diagnostics(
        request: Request,
        body: UpdateCentralDiagnosticsRequest,
    ) -> dict[str, Any]:
        try:
            return await app.state.central_diagnostics.configure(
                principal_id=request.state.principal_id,
                enabled=body.enabled,
                connection_id=body.connection_id,
                success_sample_rate=body.success_sample_rate,
            )
        except CentralDiagnosticsConfigurationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (CentralDiagnosticsUnavailable, CredentialStoreError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get(
        "/v1/model-services/presets",
        response_model=ModelServicePresetCatalog,
    )
    async def list_model_services_presets() -> dict[str, Any]:
        return {"services": list_model_service_presets()}

    @app.post(
        "/v1/model-services/shejane/authorization",
        response_model=SheJaneAuthorizationStartResponse,
        status_code=201,
    )
    async def start_shejane_authorization(request: Request) -> dict[str, Any]:
        if await request.body():
            raise HTTPException(
                status_code=400,
                detail="authorization start does not accept configuration",
            )
        try:
            return await app.state.shejane_authorization.start(request.state.principal_id)
        except OfficialServiceUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "official_service_unconfigured",
                    "message": "SheJane 官方服务尚未配置。",
                },
            ) from exc

    @app.get(
        "/v1/model-services/shejane/authorization/{authorization_id}",
        response_model=SheJaneAuthorizationStatusResponse,
    )
    async def get_shejane_authorization(
        request: Request,
        authorization_id: str,
    ) -> dict[str, Any]:
        try:
            return app.state.shejane_authorization.status(
                authorization_id,
                request.state.principal_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="authorization not found") from exc

    @app.get(
        "/v1/model-services",
        response_model=ListModelServiceConnectionsResponse,
    )
    async def list_model_services(request: Request) -> dict[str, Any]:
        store: LocalStore = app.state.store
        try:
            rows = await store.list_model_connections(principal_id=request.state.principal_id)
            services = await asyncio.gather(*(_model_service_response(row) for row in rows))
        except CredentialStoreError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"services": services}

    @app.get(
        "/v1/model-capability-bindings",
        response_model=ListModelCapabilityBindingsResponse,
    )
    async def list_model_capability_bindings(request: Request) -> dict[str, Any]:
        principal_id = request.state.principal_id
        store: LocalStore = app.state.store
        rows = await store.list_model_capability_bindings(principal_id=principal_id)
        return {
            "bindings": [
                await _model_capability_binding_response(
                    store,
                    principal_id=principal_id,
                    row=row,
                )
                for row in rows
            ]
        }

    @app.put(
        "/v1/model-capability-bindings/{capability}",
        response_model=ModelCapabilityBinding,
    )
    async def set_model_capability_binding(
        request: Request,
        capability: str,
        body: SetModelCapabilityBindingRequest,
    ) -> dict[str, Any]:
        if capability not in {"image_generation", "image_editing"}:
            raise HTTPException(status_code=404, detail="model capability is not bindable")
        parts = body.model_spec.split(":", 2)
        connection_id, model_id = parts[1], parts[2]
        principal_id = request.state.principal_id
        store: LocalStore = app.state.store
        connection = await store.get_model_connection(
            principal_id=principal_id,
            connection_id=connection_id,
        )
        if connection is None:
            raise HTTPException(status_code=404, detail="model service not found")
        model = next(
            (
                item
                for item in _model_connection_models(connection)
                if item.get("model_id") == model_id
            ),
            None,
        )
        selected = model_capability(model, capability) if model is not None else None
        if selected is None or selected.get("verification") != "verified":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "model_capability_unverified",
                    "message": "请先验证这个模型的对应能力。",
                },
            )
        row = await store.set_model_capability_binding(
            principal_id=principal_id,
            capability=capability,
            connection_id=connection_id,
            connection_version=int(connection.get("version") or 1),
            model_id=model_id,
            protocol=str(selected["protocol"]),
        )
        return await _model_capability_binding_response(
            store,
            principal_id=principal_id,
            row=row,
        )

    @app.delete(
        "/v1/model-capability-bindings/{capability}",
        status_code=204,
        response_class=Response,
    )
    async def delete_model_capability_binding(
        request: Request,
        capability: str,
    ) -> Response:
        if capability not in {"image_generation", "image_editing"}:
            raise HTTPException(status_code=404, detail="model capability is not bindable")
        await app.state.store.delete_model_capability_binding(
            principal_id=request.state.principal_id,
            capability=capability,
        )
        return Response(status_code=204)

    @app.post(
        "/v1/model-services",
        response_model=ModelServiceConnection,
        status_code=201,
    )
    async def connect_model_service(
        request: Request,
        body: ConnectModelServiceRequest,
    ) -> ModelServiceConnection:
        preset = model_service_preset(body.preset_id)
        if preset is None:
            raise HTTPException(status_code=400, detail="model service is not supported")
        if preset["connection_method"] == "browser_authorization":
            raise HTTPException(
                status_code=400,
                detail="model service requires browser authorization",
            )
        api_key = body.api_key.strip()
        if not api_key:
            raise HTTPException(status_code=400, detail="API key is required")
        if body.preset_id == "custom":
            if not body.name or not body.base_url:
                raise HTTPException(
                    status_code=400,
                    detail="custom model service needs a name and address",
                )
            name = body.name.strip()
            region = "custom"
            base_url = _model_service_base_url(body.base_url)
            if body.adapter_id == "google_genai":
                models, catalog_status = await _refresh_model_service_models(
                    preset=preset,
                    base_url=base_url,
                    adapter_id="google_genai",
                    api_key=api_key,
                )
                if catalog_status != "ready":
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "code": "adapter_detection_failed",
                            "message": "无法通过 Google GenerateContent 接口读取模型列表。",
                        },
                    )
                adapter_id = "google_genai"
            else:
                detected: dict[str, tuple[list[dict[str, Any]], str]] = {}
                credential_error: HTTPException | None = None
                for candidate in ("openai_chat", "anthropic_messages"):
                    try:
                        candidate_models, candidate_status = await _refresh_model_service_models(
                            preset=preset,
                            base_url=base_url,
                            adapter_id=candidate,
                            api_key=api_key,
                        )
                    except HTTPException as exc:
                        if exc.status_code == 401:
                            credential_error = exc
                        continue
                    if candidate_status == "ready":
                        detected[candidate] = (candidate_models, candidate_status)
                adapter_id = body.adapter_id or adapter_for_custom_service(
                    openai_chat_available="openai_chat" in detected,
                    anthropic_messages_available="anthropic_messages" in detected,
                )
                if adapter_id is None:
                    if credential_error is not None:
                        raise credential_error
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "code": "adapter_detection_failed",
                            "message": "无法自动识别接口格式，请在高级设置中选择接口格式。",
                        },
                    )
                if (
                    body.adapter_id is not None
                    and adapter_id not in detected
                    and credential_error is not None
                ):
                    raise credential_error
                models, catalog_status = detected.get(adapter_id, ([], "unavailable"))
        else:
            if body.name is not None or body.adapter_id is not None:
                raise HTTPException(
                    status_code=400,
                    detail="official model service transport cannot be overridden",
                )
            regions = list(preset["regions"])
            region_id = body.region or next(str(item["id"]) for item in regions if item["default"])
            region_config = next(
                (item for item in regions if item["id"] == region_id),
                None,
            )
            if region_config is None:
                raise HTTPException(
                    status_code=400,
                    detail="model service region is not supported",
                )
            name = str(preset["name"])
            region = str(region_config["id"])
            base_url = _model_service_base_url(body.base_url or str(region_config["base_url"]))
            adapter_id = str(preset["adapter_id"])
            models, catalog_status = await _refresh_model_service_models(
                preset=preset,
                base_url=base_url,
                adapter_id=adapter_id,
                api_key=api_key,
            )
            models = await _verify_bundled_model_catalog(
                settings=app.state.settings,
                base_url=base_url,
                adapter_id=adapter_id,
                api_key=api_key,
                models=models,
            )
        connection_id = f"conn_{uuid.uuid4().hex}"
        next_credential_ref = credential_ref(connection_id)
        principal_id = request.state.principal_id
        try:
            await set_model_api_key(
                principal_id,
                connection_id,
                api_key,
                next_credential_ref,
            )
            try:
                row = await app.state.store.create_model_connection(
                    principal_id=principal_id,
                    connection_id=connection_id,
                    preset_id=body.preset_id,
                    name=name,
                    region=region,
                    adapter_id=adapter_id,
                    base_url=base_url,
                    requires_api_key=True,
                    credential_ref=next_credential_ref,
                    models=models,
                    catalog_status=catalog_status,
                )
            except BaseException:
                await delete_model_api_key(
                    principal_id,
                    connection_id,
                    next_credential_ref,
                )
                raise
        except CredentialStoreError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return await _model_service_response(row, credential_configured=True)

    @app.post(
        "/v1/model-services/import",
        response_model=ModelServiceConnection,
        status_code=201,
    )
    async def import_model_service(
        request: Request,
        body: ImportModelServiceRequest,
    ) -> ModelServiceConnection:
        principal_id = request.state.principal_id
        store: LocalStore = app.state.store
        if await store.get_model_connection(
            principal_id=principal_id,
            connection_id=body.id,
        ):
            raise HTTPException(status_code=409, detail="model service already exists")
        preset = model_service_preset(body.preset_id)
        if preset is None:
            raise HTTPException(status_code=400, detail="model service is not supported")
        if preset["connection_method"] == "browser_authorization":
            raise HTTPException(
                status_code=400,
                detail="official service must be authorized again after import",
            )
        if body.preset_id == "custom":
            raise HTTPException(
                status_code=400,
                detail="custom model services must be reconnected manually",
            )
        region_config = next(
            (item for item in preset["regions"] if item["id"] == body.region),
            None,
        )
        if region_config is None:
            raise HTTPException(
                status_code=400,
                detail="model service region is not supported",
            )
        name = str(preset["name"])
        region = str(region_config["id"])
        adapter_id = str(preset["adapter_id"])
        base_url = str(region_config["base_url"])
        models = [dict(model) for model in preset["models"]]
        row = await store.create_model_connection(
            principal_id=principal_id,
            connection_id=body.id,
            preset_id=body.preset_id,
            name=name,
            region=region,
            adapter_id=adapter_id,
            base_url=base_url,
            requires_api_key=True,
            credential_ref=credential_ref(body.id),
            models=models,
            catalog_status="stale" if models else "unavailable",
        )
        return await _model_service_response(row, credential_configured=False)

    @app.put(
        "/v1/model-services/{connection_id}/credential",
        response_model=ModelServiceConnection,
    )
    async def reconnect_model_service(
        request: Request,
        connection_id: str,
        body: ReconnectModelServiceRequest,
    ) -> ModelServiceConnection:
        principal_id = request.state.principal_id
        store: LocalStore = app.state.store
        row = await store.get_model_connection(
            principal_id=principal_id,
            connection_id=connection_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="model service not found")
        if row["preset_id"] == "shejane-official":
            raise HTTPException(
                status_code=400,
                detail="managed official credentials cannot be replaced",
            )
        api_key = body.api_key.strip()
        if not api_key:
            raise HTTPException(status_code=400, detail="API key is required")
        base_url = _model_service_base_url(body.base_url or str(row["base_url"]))
        preset = model_service_preset(str(row["preset_id"])) or {"models": ()}
        models, catalog_status = await _refresh_model_service_models(
            preset=preset,
            base_url=base_url,
            adapter_id=str(row["adapter_id"]),
            api_key=api_key,
        )
        if catalog_status != "ready":
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "provider_unavailable",
                    "message": "暂时无法验证新的 API Key，旧 Key 已保留。",
                },
            )
        models = await _verify_bundled_model_catalog(
            settings=app.state.settings,
            base_url=base_url,
            adapter_id=str(row["adapter_id"]),
            api_key=api_key,
            models=models,
        )
        next_credential_ref = new_credential_ref(connection_id)
        credential_swapped = False
        try:
            await set_model_api_key(
                principal_id,
                connection_id,
                api_key,
                next_credential_ref,
            )
            try:
                async with app.state.coordinator.model_connection_mutation(
                    principal_id=principal_id,
                    connection_id=connection_id,
                ):
                    current = await store.get_model_connection(
                        principal_id=principal_id,
                        connection_id=connection_id,
                    )
                    if current is None:
                        raise HTTPException(status_code=404, detail="model service not found")
                    previous_credential_ref = str(current["credential_ref"])
                    updated = await store.replace_model_connection_credential(
                        principal_id=principal_id,
                        connection_id=connection_id,
                        credential_ref=next_credential_ref,
                        base_url=base_url,
                        models=models,
                        catalog_status=catalog_status,
                    )
                    assert updated is not None
                    credential_swapped = True
                    try:
                        await delete_model_api_key(
                            principal_id,
                            connection_id,
                            previous_credential_ref,
                        )
                    except CredentialStoreError:
                        log.warning(
                            "old model-service credential remains after reconnect",
                            extra={"connection_id": connection_id},
                        )
            except BaseException:
                if not credential_swapped:
                    await delete_model_api_key(
                        principal_id,
                        connection_id,
                        next_credential_ref,
                    )
                raise
        except CredentialStoreError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return await _model_service_response(updated, credential_configured=True)

    @app.post(
        "/v1/model-services/{connection_id}/refresh",
        response_model=ModelServiceConnection,
    )
    async def refresh_model_service(
        request: Request,
        connection_id: str,
    ) -> ModelServiceConnection:
        principal_id = request.state.principal_id
        store: LocalStore = app.state.store
        row = await store.get_model_connection(
            principal_id=principal_id,
            connection_id=connection_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="model service not found")
        try:
            api_key = await get_model_api_key(
                principal_id,
                connection_id,
                str(row["credential_ref"]),
            )
        except CredentialStoreError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not api_key:
            raise HTTPException(status_code=409, detail="model service needs an API key")
        preset = model_service_preset(str(row["preset_id"])) or {
            "models": tuple(
                model for model in _model_connection_models(row) if model.get("source") == "bundled"
            )
        }
        models, catalog_status = await _refresh_model_service_models(
            preset=preset,
            base_url=str(row["base_url"]),
            adapter_id=str(row["adapter_id"]),
            api_key=api_key,
        )
        if row["preset_id"] == "shejane-official":
            models = await _verify_bundled_model_catalog(
                settings=app.state.settings,
                base_url=str(row["base_url"]),
                adapter_id=str(row["adapter_id"]),
                api_key=api_key,
                models=models,
                include_discovered=True,
            )
        async with app.state.coordinator.model_connection_catalog_update(
            principal_id=principal_id,
            connection_id=connection_id,
        ):
            current = await store.get_model_connection(
                principal_id=principal_id,
                connection_id=connection_id,
            )
            if current is None:
                raise HTTPException(status_code=404, detail="model service not found")
            cached = _model_connection_models(current)
            models = (
                models
                if catalog_status == "ready" and row["preset_id"] == "shejane-official"
                else _merge_refreshed_model_catalog(cached, models)
                if catalog_status == "ready"
                else cached or models
            )
            updated = await store.update_model_connection_catalog(
                principal_id=principal_id,
                connection_id=connection_id,
                models=models,
                catalog_status=catalog_status,
            )
        assert updated is not None
        return await _model_service_response(updated, credential_configured=True)

    @app.post(
        "/v1/model-services/{connection_id}/models",
        response_model=ModelServiceModel,
        status_code=201,
    )
    async def add_model_service_model(
        request: Request,
        connection_id: str,
        body: AddModelServiceModelRequest,
    ) -> dict[str, Any]:
        principal_id = request.state.principal_id
        store: LocalStore = app.state.store
        model = {
            "model_id": body.model_id,
            "display_name": body.display_name or body.model_id,
            "capabilities": [],
            "source": "manual",
            "verification": "unverified",
            "recommended": False,
            "tool_calling": False,
            "streaming": False,
            "image_inputs": False,
            "max_input_tokens": None,
            "max_output_tokens": None,
        }
        async with app.state.coordinator.model_connection_catalog_update(
            principal_id=principal_id,
            connection_id=connection_id,
        ):
            current = await store.get_model_connection(
                principal_id=principal_id,
                connection_id=connection_id,
            )
            if current is None:
                raise HTTPException(status_code=404, detail="model service not found")
            models = _model_connection_models(current)
            if any(item["model_id"] == body.model_id for item in models):
                raise HTTPException(status_code=409, detail="model already exists")
            models.append(model)
            await store.update_model_connection_catalog(
                principal_id=principal_id,
                connection_id=connection_id,
                models=models,
                catalog_status=str(current["catalog_status"]),
            )
        return model

    @app.post(
        "/v1/model-services/{connection_id}/models/{model_id:path}/verify",
        response_model=ModelServiceModel,
    )
    async def verify_model_service_model(
        request: Request,
        connection_id: str,
        model_id: str,
        body: VerifyModelServiceModelRequest | None = None,
    ) -> dict[str, Any]:
        principal_id = request.state.principal_id
        store: LocalStore = app.state.store
        row = await store.get_model_connection(
            principal_id=principal_id,
            connection_id=connection_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="model service not found")
        expected_connection = (
            int(row.get("version") or 1),
            str(row["credential_ref"]),
            str(row["base_url"]),
        )
        models = _model_connection_models(row)
        model = next((item for item in models if item["model_id"] == model_id), None)
        if model is None:
            raise HTTPException(status_code=404, detail="model not found")
        if body is None:
            capability = model_capability(model, "agent_chat")
            body = VerifyModelServiceModelRequest(
                capability="agent_chat",
                protocol=(
                    str(capability["protocol"])
                    if capability is not None
                    else "openai_responses"
                    if row.get("preset_id") == "openai"
                    else default_model_protocol(str(row.get("adapter_id")), "agent_chat")
                ),
            )
        try:
            api_key = await get_model_api_key(
                principal_id,
                connection_id,
                str(row["credential_ref"]),
            )
        except CredentialStoreError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not api_key:
            raise HTTPException(status_code=409, detail="model service needs an API key")
        await _verify_model_service_capability(
            settings=app.state.settings,
            base_url=str(row["base_url"]),
            capability=body.capability,
            protocol=body.protocol,
            api_key=api_key,
            model_id=model_id,
        )
        async with app.state.coordinator.model_connection_catalog_update(
            principal_id=principal_id,
            connection_id=connection_id,
        ):
            current = await store.get_model_connection(
                principal_id=principal_id,
                connection_id=connection_id,
            )
            if current is None:
                raise HTTPException(status_code=404, detail="model service not found")
            if (
                int(current.get("version") or 1),
                str(current["credential_ref"]),
                str(current["base_url"]),
            ) != expected_connection:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "model_service_changed",
                        "message": "模型服务在验证期间已更新，请重新验证。",
                    },
                )
            models = _model_connection_models(current)
            model = next((item for item in models if item["model_id"] == model_id), None)
            if model is None:
                raise HTTPException(status_code=404, detail="model not found")
            capabilities = {
                item["capability"]: dict(item)
                for item in model.get("capabilities", [])
                if isinstance(item, dict) and item.get("capability")
            }
            capabilities[body.capability] = {
                "capability": body.capability,
                "protocol": body.protocol,
                "verification": "verified",
            }
            model["capabilities"] = sorted(
                capabilities.values(),
                key=lambda item: MODEL_CAPABILITY_ORDER[str(item["capability"])],
            )
            model["verification"] = "verified"
            if body.capability == "agent_chat":
                model["tool_calling"] = True
                model["streaming"] = True
            if body.capability == "image_understanding":
                model["image_inputs"] = True
            await store.update_model_connection_catalog(
                principal_id=principal_id,
                connection_id=connection_id,
                models=models,
                catalog_status=str(current["catalog_status"]),
            )
        return model

    @app.delete(
        "/v1/model-services/{connection_id}",
        status_code=204,
        response_class=Response,
    )
    async def delete_model_service(
        request: Request,
        connection_id: str,
    ) -> Response:
        principal_id = request.state.principal_id
        store: LocalStore = app.state.store
        row = await store.get_model_connection(
            principal_id=principal_id,
            connection_id=connection_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="model service not found")
        try:
            async with app.state.coordinator.model_connection_mutation(
                principal_id=principal_id,
                connection_id=connection_id,
            ):
                credential_reference = str(row["credential_ref"])
                current_key = await get_model_api_key(
                    principal_id,
                    connection_id,
                    credential_reference,
                )
                await delete_model_api_key(
                    principal_id,
                    connection_id,
                    credential_reference,
                )
                try:
                    deleted = await store.delete_model_connection(
                        principal_id=principal_id,
                        connection_id=connection_id,
                    )
                    assert deleted is not None
                except BaseException:
                    if current_key:
                        await set_model_api_key(
                            principal_id,
                            connection_id,
                            current_key,
                            credential_reference,
                        )
                    raise
        except CredentialStoreError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return Response(status_code=204)

    @app.get("/v1/models", response_model=LocalRuntimeModelCatalog)
    async def list_runtime_models(request: Request) -> dict[str, Any]:
        store: LocalStore = app.state.store
        models: list[dict[str, Any]] = []
        if settings.fake_llm:
            models.append(
                {
                    "spec": "local:test:model",
                    "model_id": "model",
                    "display_name": "Test model",
                    "connection_id": "test",
                    "service_name": "Test",
                    "capabilities": [
                        {
                            "capability": "agent_chat",
                            "protocol": "openai_chat_completions",
                            "verification": "verified",
                        }
                    ],
                    "tool_calling": True,
                    "streaming": True,
                    "image_inputs": False,
                    "verification": "verified",
                    "recommended": True,
                    "max_input_tokens": 128_000,
                    "max_output_tokens": 8_192,
                    "available": True,
                }
            )
        try:
            rows = await store.list_model_connections(principal_id=request.state.principal_id)
            configured_connections = await asyncio.gather(
                *(
                    get_model_api_key(
                        request.state.principal_id,
                        str(row["id"]),
                        str(row["credential_ref"]),
                    )
                    for row in rows
                )
            )
            for row, api_key in zip(rows, configured_connections, strict=True):
                configured = bool(api_key)
                for model in _model_connection_models(row):
                    agent_capability = model_capability(model, "agent_chat")
                    models.append(
                        {
                            "spec": f"local:{row['id']}:{model['model_id']}",
                            "model_id": model["model_id"],
                            "display_name": model["display_name"],
                            "connection_id": row["id"],
                            "service_name": row["name"],
                            "capabilities": model.get("capabilities", []),
                            "tool_calling": bool(model.get("tool_calling")),
                            "streaming": bool(model.get("streaming")),
                            "image_inputs": bool(model.get("image_inputs")),
                            "verification": model.get("verification", "unverified"),
                            "recommended": bool(model.get("recommended")),
                            "max_input_tokens": model.get("max_input_tokens"),
                            "max_output_tokens": model.get("max_output_tokens"),
                            "available": configured
                            and agent_capability is not None
                            and agent_capability.get("verification") == "verified"
                            and bool(model.get("tool_calling"))
                            and bool(model.get("streaming")),
                        }
                    )
        except CredentialStoreError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"models": models}

    @app.get("/v1/tools")
    async def list_tools() -> dict[str, Any]:
        from .tools.registry import describe_tools

        # Phase 2': describe with the current store. workspace_root is
        # None at this layer because fs tools are bound per-run by the
        # agent builder (Phase 3'). Callers wanting the per-run view will
        # use a different endpoint then.
        store = getattr(app.state, "store", None)
        return {"tools": describe_tools(store=store, workspace_root=None)}

    @app.get("/v1/workspaces", response_model=ListWorkspacesResponse)
    async def list_workspaces(request: Request) -> dict[str, Any]:
        store: LocalStore = app.state.store
        return {"workspaces": await store.list_workspaces(principal_id=request.state.principal_id)}

    @app.post("/v1/workspaces", response_model=LocalWorkspaceAuthorization)
    async def add_workspace(request: Request, body: CreateWorkspaceRequest) -> dict[str, Any]:
        """Authorize a workspace path. Returns the flat row — the TS
        `authorizeLocalWorkspace` reads `.id / .path / .label` directly
        (no wrapper)."""
        store: LocalStore = app.state.store
        raw_path = body.path.strip()
        if not raw_path:
            raise HTTPException(status_code=400, detail="path required")
        path = await _normalized_path(raw_path)
        if not await asyncio.to_thread(Path(path).is_dir):
            raise HTTPException(status_code=400, detail="workspace must be an existing directory")
        return await store.create_workspace(
            principal_id=request.state.principal_id,
            path=path,
            label=body.label.strip() or path,
        )

    @app.delete(
        "/v1/workspaces/{workspace_id}",
        response_model=LocalWorkspaceAuthorization,
    )
    async def remove_workspace(request: Request, workspace_id: str) -> dict[str, Any]:
        """Revoke a workspace authorization. Returns the deleted row
        matching the TS `revokeLocalWorkspace` →
        `Promise<LocalWorkspaceAuthorization>` signature."""
        store: LocalStore = app.state.store
        # Fetch before delete so we can return the record. If it didn't
        # exist, surface a 404 — client `decodeLocalResponse` throws
        # which the renderer catches and shows to the user.
        existing = None
        principal_id = request.state.principal_id
        for ws in await store.list_workspaces(principal_id=principal_id):
            if ws["id"] == workspace_id:
                existing = ws
                break
        if existing is None:
            raise HTTPException(status_code=404, detail="workspace not found")
        await store.delete_workspace(principal_id=principal_id, workspace_id=workspace_id)
        return existing

    @app.get("/v1/threads", response_model=ListThreadsResponse)
    async def list_threads(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        before_created_at: str | None = Query(default=None),
        before_id: str | None = Query(default=None),
    ):
        store: LocalStore = app.state.store
        if (before_created_at is None) != (before_id is None):
            raise HTTPException(status_code=400, detail="both thread page cursors are required")
        threads, cursor, has_more = await store.list_threads(
            principal_id=request.state.principal_id,
            limit=limit,
            before_created_at=before_created_at,
            before_id=before_id,
        )
        return {
            "threads": [_thread_record_for_api(thread) for thread in threads],
            "cursor": cursor,
            "has_more": has_more,
            "next_before_created_at": threads[-1]["created_at"] if has_more and threads else None,
            "next_before_id": threads[-1]["id"] if has_more and threads else None,
        }

    @app.get("/v1/threads/changes", response_model=ListThreadChangesResponse)
    async def list_thread_changes(
        request: Request,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=1000),
    ):
        store: LocalStore = app.state.store
        changes, cursor = await store.thread_changes_since(
            principal_id=request.state.principal_id,
            after_cursor=after,
            limit=limit,
        )
        return {"changes": changes, "cursor": cursor}

    @app.get("/v1/threads/{thread_id}", response_model=LocalThreadSnapshot)
    async def get_thread_snapshot(
        request: Request,
        thread_id: str,
        before_position: int | None = Query(default=None, ge=1),
        item_limit: int = Query(default=200, ge=2, le=500),
        event_limit: int = Query(default=5000, ge=1, le=10000),
        expected_version: int | None = Query(default=None, ge=1),
    ):
        store: LocalStore = app.state.store
        try:
            snapshot = await store.get_thread_snapshot(
                principal_id=request.state.principal_id,
                thread_id=thread_id,
                before_position=before_position,
                item_limit=item_limit,
                event_limit=event_limit,
                expected_version=expected_version,
            )
        except RunResultConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if snapshot is None:
            raise HTTPException(status_code=404, detail="thread not found")
        items = []
        for item in snapshot["items"]:
            try:
                metadata = json.loads(item.get("metadata_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                metadata = {}
            items.append({**item, "metadata": metadata if isinstance(metadata, dict) else {}})
        events = []
        for event in snapshot["events"]:
            try:
                payload = json.loads(event.get("payload_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                payload = {}
            events.append({**event, "payload": payload if isinstance(payload, dict) else {}})
        return {
            **snapshot,
            "thread": _thread_record_for_api(snapshot["thread"]),
            "items": items,
            "runs": await _runs_with_inputs(store, snapshot["runs"]),
            "events": events,
        }

    @app.patch("/v1/threads/{thread_id}", response_model=LocalThread)
    async def update_thread(
        request: Request,
        thread_id: str,
        body: UpdateLocalThreadRequest,
    ):
        store: LocalStore = app.state.store
        thread = await store.update_thread(
            principal_id=request.state.principal_id,
            thread_id=thread_id,
            title=body.title,
            metadata=body.metadata,
            archived=body.archived,
        )
        if thread is None:
            raise HTTPException(status_code=404, detail="thread not found")
        return _thread_record_for_api(thread)

    @app.delete("/v1/threads/{thread_id}", response_model=DeleteLocalThreadResponse)
    async def delete_thread(request: Request, thread_id: str):
        store: LocalStore = app.state.store
        try:
            version = await store.delete_thread(
                principal_id=request.state.principal_id,
                thread_id=thread_id,
            )
        except RunResultConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if version is None:
            raise HTTPException(status_code=404, detail="thread not found")
        return {"id": thread_id, "deleted": True, "version": version}

    @app.get("/v1/runs", response_model=ListRunsResponse)
    async def list_runs(request: Request) -> dict[str, Any]:
        """Recent runs newest-first.

        Client `listLocalRuns()` (runtime/sdk/src/client.ts:283)
        reads `{runs: LocalRun[]}` on every boot. Previously this route
        didn't exist — every Electron launch silently 404'd here and
        the conversation history sidebar came up empty.
        """
        store: LocalStore = app.state.store
        runs = await store.list_runs(principal_id=request.state.principal_id)
        return {"runs": await _runs_with_inputs(store, runs)}

    @app.get("/v1/plugins", response_model=ListPluginsResponse)
    async def list_plugins(request: Request) -> dict[str, Any]:
        registry: PluginRegistry = app.state.plugin_registry
        return {"plugins": await registry.list(principal_id=request.state.principal_id)}

    @app.get(
        "/v1/plugins/runtime-assets/storage",
        response_model=RuntimeAssetStorage,
    )
    async def inspect_runtime_asset_storage(request: Request) -> dict[str, Any]:
        registry: PluginRegistry = app.state.plugin_registry
        return await registry.runtime_asset_storage()

    @app.delete(
        "/v1/plugins/runtime-assets/storage",
        response_model=RuntimeAssetCleanupResult,
    )
    async def cleanup_runtime_asset_storage(
        request: Request,
        scope: Literal["history", "all"] = Query(...),
    ) -> dict[str, Any]:
        registry: PluginRegistry = app.state.plugin_registry
        try:
            return await registry.cleanup_runtime_asset_storage(scope)
        except PluginRegistryError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    @app.get("/v1/plugins/{plugin_id}", response_model=PluginDetail)
    async def inspect_plugin(request: Request, plugin_id: str) -> dict[str, Any]:
        registry: PluginRegistry = app.state.plugin_registry
        try:
            return await registry.inspect(
                principal_id=request.state.principal_id,
                plugin_id=plugin_id,
            )
        except PluginRegistryError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    @app.get(
        "/v1/plugins/{plugin_id}/runtime-asset",
        response_model=FixedRuntimeAssetStatus,
        response_model_exclude_defaults=True,
    )
    async def inspect_fixed_runtime_asset(request: Request, plugin_id: str) -> dict[str, Any]:
        registry: PluginRegistry = app.state.plugin_registry
        try:
            return await registry.fixed_runtime_asset_status(
                principal_id=request.state.principal_id,
                plugin_id=plugin_id,
            )
        except PluginRegistryError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    @app.put(
        "/v1/plugins/{plugin_id}/runtime-asset",
        response_model=FixedRuntimeAssetStatus,
        response_model_exclude_defaults=True,
    )
    async def prepare_fixed_runtime_asset(request: Request, plugin_id: str) -> dict[str, Any]:
        registry: PluginRegistry = app.state.plugin_registry
        try:
            return await registry.prepare_fixed_runtime_asset(
                principal_id=request.state.principal_id,
                plugin_id=plugin_id,
            )
        except PluginRegistryError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    @app.delete(
        "/v1/plugins/{plugin_id}/runtime-asset",
        response_model=FixedRuntimeAssetStatus,
        response_model_exclude_defaults=True,
    )
    async def remove_fixed_runtime_asset(request: Request, plugin_id: str) -> dict[str, Any]:
        registry: PluginRegistry = app.state.plugin_registry
        try:
            return await registry.remove_fixed_runtime_asset(
                principal_id=request.state.principal_id,
                plugin_id=plugin_id,
            )
        except PluginRegistryError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    @app.get(
        "/v1/plugins/{plugin_id}/readiness",
        response_model=PluginReadinessSnapshot,
    )
    async def inspect_plugin_readiness(request: Request, plugin_id: str) -> dict[str, Any]:
        if plugin_id != "org.shejane.computer-use":
            raise HTTPException(status_code=404, detail="plugin readiness is unavailable")
        registry: PluginRegistry = app.state.plugin_registry
        try:
            return await registry.computer_use_readiness(principal_id=request.state.principal_id)
        except PluginRegistryError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    @app.post(
        "/v1/commands",
        response_model=(
            CancelRunCommandReceipt
            | AnswerQuestionCommandReceipt
            | ResolvePermissionCommandReceipt
            | PlanResolveCommandReceipt
            | ToolReconcileCommandReceipt
            | PluginInstallCommandReceipt
            | PluginModelBindCommandReceipt
            | RuntimeAssetInstallCommandReceipt
            | PluginStateCommandReceipt
            | PluginVersionSwitchCommandReceipt
            | PluginRemoveCommandReceipt
            | PluginSetupAdvanceCommandReceipt
        ),
    )
    async def accept_command(
        request: Request,
        body: (
            CancelRunCommand
            | AnswerQuestionCommand
            | ResolvePermissionCommand
            | PlanResolveCommand
            | ToolReconcileCommand
            | PluginInstallCommand
            | PluginModelBindCommand
            | RuntimeAssetInstallCommand
            | PluginEnableCommand
            | PluginDisableCommand
            | PluginUpdateCommand
            | PluginRollbackCommand
            | PluginRemoveCommand
            | PluginSetupAdvanceCommand
        ),
    ) -> dict[str, Any]:
        store: LocalStore = app.state.store
        coordinator: RunCoordinator = app.state.coordinator
        if isinstance(body, PluginSetupAdvanceCommand):
            registry: PluginRegistry = app.state.plugin_registry
            try:
                return await registry.advance_computer_use_setup(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    expected_revision=body.expected_revision,
                    action_id=body.action_id,
                )
            except CommandConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except PluginRegistryError as exc:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={"code": exc.code, "message": str(exc)},
                ) from exc
        if isinstance(body, PluginModelBindCommand):
            registry: PluginRegistry = app.state.plugin_registry
            async with coordinator._model_admission(
                request.state.principal_id,
                body.model,
                ("image_inputs",),
            ) as (binding, error):
                if error is not None:
                    raise HTTPException(
                        status_code=409,
                        detail={"code": error.code, "message": str(error)},
                    )
                try:
                    return await registry.bind_model(
                        principal_id=request.state.principal_id,
                        command_id=body.command_id,
                        plugin_id=body.plugin_id,
                        binding_id=body.binding_id,
                        requested_model=body.model,
                        model_binding=binding,
                        expected_digest=body.expected_digest,
                    )
                except CommandConflictError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                except PluginRegistryError as exc:
                    raise HTTPException(
                        status_code=exc.status_code,
                        detail={"code": exc.code, "message": str(exc)},
                    ) from exc
        if isinstance(body, RuntimeAssetInstallCommand):
            registry: PluginRegistry = app.state.plugin_registry
            try:
                return await registry.install_runtime_asset(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    source_path=body.source_path,
                    expected_digest=body.expected_digest,
                )
            except CommandConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except PluginRegistryError as exc:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={"code": exc.code, "message": str(exc)},
                ) from exc
        if isinstance(body, PluginInstallCommand):
            registry: PluginRegistry = app.state.plugin_registry
            try:
                return await registry.install(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    source_path=body.source_path,
                    expected_digest=body.expected_digest,
                    allow_unsigned=body.allow_unsigned,
                )
            except CommandConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except PluginRegistryError as exc:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={"code": exc.code, "message": str(exc)},
                ) from exc
        if isinstance(body, PluginUpdateCommand):
            registry = app.state.plugin_registry
            try:
                return await registry.update(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    plugin_id=body.plugin_id,
                    source_path=body.source_path,
                    expected_digest=body.expected_digest,
                    allow_unsigned=body.allow_unsigned,
                )
            except CommandConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except PluginRegistryError as exc:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={"code": exc.code, "message": str(exc)},
                ) from exc
        if isinstance(body, PluginRollbackCommand):
            registry = app.state.plugin_registry
            try:
                return await registry.rollback(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    plugin_id=body.plugin_id,
                    target_digest=body.target_digest,
                    expected_digest=body.expected_digest,
                )
            except CommandConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except PluginRegistryError as exc:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={"code": exc.code, "message": str(exc)},
                ) from exc
        if isinstance(body, PluginRemoveCommand):
            registry = app.state.plugin_registry
            try:
                return await registry.remove(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    plugin_id=body.plugin_id,
                    expected_digest=body.expected_digest,
                )
            except CommandConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except PluginRegistryError as exc:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={"code": exc.code, "message": str(exc)},
                ) from exc
        if isinstance(body, (PluginEnableCommand, PluginDisableCommand)):
            registry = app.state.plugin_registry
            try:
                return await registry.set_enabled(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    plugin_id=body.plugin_id,
                    expected_digest=body.expected_digest,
                    enabled=isinstance(body, PluginEnableCommand),
                )
            except CommandConflictError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except PluginRegistryError as exc:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={"code": exc.code, "message": str(exc)},
                ) from exc
        if isinstance(body, ToolReconcileCommand):
            command_payload = {
                "type": body.type,
                "operation_id": body.operation_id,
                "decision": body.decision,
            }
            try:
                replay = await store.accepted_command_receipt(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    command_type=body.type,
                    payload=command_payload,
                )
                if replay is not None:
                    if replay.get("resumed"):
                        coordinator.wake_jobs()
                    return replay
                reconciliation = await store.get_wait_candidate(body.operation_id)
                if reconciliation is None or reconciliation.get("kind") != "tool_reconciliation":
                    raise KeyError(body.operation_id)
                run = await _owned_run(
                    store,
                    principal_id=request.state.principal_id,
                    run_id=str(reconciliation["run_id"]),
                    not_found_detail="tool reconciliation not found",
                )
                await _authorized_workspace_path(
                    store,
                    principal_id=request.state.principal_id,
                    path=run.get("workspace_path"),
                )
                await coordinator.reconcile_resume_head(str(reconciliation["run_id"]))
                results = await _tool_reconciliation_results(
                    store,
                    operation_id=body.operation_id,
                    decision=body.decision,
                )
                receipt, _created = await store.request_tool_reconcile_command(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    operation_id=body.operation_id,
                    decision=body.decision,
                    **results,
                )
            except KeyError as exc:
                raise HTTPException(
                    status_code=404, detail="tool reconciliation not found"
                ) from exc
            except (CommandConflictError, WaitDecisionConflictError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except WorkspaceAdmissionError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if receipt["resumed"]:
                coordinator.wake_jobs()
            return receipt
        if isinstance(body, PlanResolveCommand):
            instructions = (body.instructions or "").strip() or None
            command_payload: dict[str, Any] = {
                "type": body.type,
                "approval_id": body.approval_id,
                "decision": body.decision,
            }
            if instructions is not None:
                command_payload["instructions"] = instructions
            try:
                replay = await store.accepted_command_receipt(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    command_type=body.type,
                    payload=command_payload,
                )
                if replay is not None:
                    if replay.get("resumed"):
                        coordinator.wake_jobs()
                    return replay
                approval = await store.get_plan_approval(body.approval_id)
                if approval is None:
                    raise KeyError(body.approval_id)
                run = await _owned_run(
                    store,
                    principal_id=request.state.principal_id,
                    run_id=str(approval["run_id"]),
                    not_found_detail="plan approval not found",
                )
                await _authorized_workspace_path(
                    store,
                    principal_id=request.state.principal_id,
                    path=run.get("workspace_path"),
                )
                await coordinator.reconcile_resume_head(str(approval["run_id"]))
                receipt, _created = await store.request_plan_resolve_command(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    approval_id=body.approval_id,
                    decision=body.decision,
                    instructions=instructions,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="plan approval not found") from exc
            except (CommandConflictError, WaitDecisionConflictError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except WorkspaceAdmissionError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if receipt["resumed"]:
                coordinator.wake_jobs()
            return receipt
        if isinstance(body, ResolvePermissionCommand):
            edited_action = body.edited_action.model_dump() if body.edited_action else None
            command_payload: dict[str, Any] = {
                "type": body.type,
                "permission_id": body.permission_id,
                "decision": body.decision,
                "scope": body.scope,
            }
            if edited_action is not None:
                command_payload["edited_action"] = edited_action
            try:
                replay = await store.accepted_command_receipt(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    command_type=body.type,
                    payload=command_payload,
                )
                if replay is not None:
                    if replay.get("resumed"):
                        coordinator.wake_jobs()
                    return replay
                permission = await store.get_permission(body.permission_id)
                if permission is None:
                    raise KeyError(body.permission_id)
                run = await _owned_run(
                    store,
                    principal_id=request.state.principal_id,
                    run_id=str(permission["run_id"]),
                    not_found_detail="permission not found",
                )
                await _authorized_workspace_path(
                    store,
                    principal_id=request.state.principal_id,
                    path=run.get("workspace_path"),
                )
                await coordinator.reconcile_resume_head(str(permission["run_id"]))
                receipt, _created = await store.request_permission_resolve_command(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    permission_id=body.permission_id,
                    decision=body.decision,
                    scope=body.scope,
                    edited_action=edited_action,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="permission not found") from exc
            except (CommandConflictError, WaitDecisionConflictError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except PermissionScopeNotAllowedError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except WorkspaceAdmissionError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if receipt["resumed"]:
                coordinator.wake_jobs()
            return receipt
        if isinstance(body, AnswerQuestionCommand):
            command_payload = {
                "type": body.type,
                "question_id": body.question_id,
                "answers": body.answers,
            }
            try:
                replay = await store.accepted_command_receipt(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    command_type=body.type,
                    payload=command_payload,
                )
                if replay is not None:
                    if replay.get("resumed"):
                        coordinator.wake_jobs()
                    return replay
                question = await store.get_question(body.question_id)
                if question is None:
                    raise KeyError(body.question_id)
                run = await _owned_run(
                    store,
                    principal_id=request.state.principal_id,
                    run_id=str(question["run_id"]),
                    not_found_detail="question not found",
                )
                await _authorized_workspace_path(
                    store,
                    principal_id=request.state.principal_id,
                    path=run.get("workspace_path"),
                )
                await coordinator.reconcile_resume_head(str(question["run_id"]))
                receipt, _created = await store.request_question_answer_command(
                    principal_id=request.state.principal_id,
                    command_id=body.command_id,
                    question_id=body.question_id,
                    answers=body.answers,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="question not found") from exc
            except (CommandConflictError, WaitDecisionConflictError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except WorkspaceAdmissionError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if receipt["resumed"]:
                coordinator.wake_jobs()
            return receipt
        try:
            receipt, created = await store.request_run_cancel_command(
                principal_id=request.state.principal_id,
                command_id=body.command_id,
                run_id=body.run_id,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "run_not_found", "message": "run not found"},
            ) from exc
        except CommandConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if created and receipt["canceled"]:
            await coordinator.cancel_run(body.run_id)
        return receipt

    @app.post("/v1/runs", response_model=LocalRun)
    async def create_run(request: Request, body: CreateRunRequest) -> dict[str, Any]:
        """Create a new run. Returns the flat `LocalRun` shape (NOT
        `{run: {...}}`) — that's the contract `client.test.ts:63-92`
        pins and what TypeScript's `createLocalRun` reads via
        `decodeLocalResponse<LocalRun>`."""
        goal = body.goal.strip()
        if not goal:
            raise HTTPException(status_code=400, detail="goal required")
        principal_id = request.state.principal_id
        workspace_path = (
            await _normalized_path(body.workspace_path) if body.workspace_path is not None else None
        )
        attachment_paths = [await _normalized_path(path) for path in body.attachment_paths]
        coordinator: RunCoordinator = app.state.coordinator
        try:
            run = await coordinator.start_run(
                principal_id=principal_id,
                command_id=body.command_id,
                client_message_id=body.client_message_id,
                protocol_version=body.protocol_version,
                required_capabilities=body.required_capabilities,
                required_tools=body.required_tools,
                goal=goal,
                thread_id=body.thread_id,
                user_input=body.user_input,
                assistant_message_id=body.assistant_message_id,
                thread_title=body.thread_title,
                thread_metadata=body.thread_metadata,
                user_item_metadata=body.user_item_metadata,
                replace_from_client_id=body.replace_from_client_id,
                workspace_path=workspace_path,
                attachment_paths=attachment_paths,
                # The runtime's legacy `mode` column carries the Runtime model selection.
                mode=body.model,
                permission_mode=body.permission_mode,
                history=body.history or [],
                parent_run_id=body.parent_run_id,
                plugin_refs=[reference.model_dump(mode="json") for reference in body.plugin_refs],
                plugin_command=(
                    body.plugin_command.model_dump(mode="json")
                    if body.plugin_command is not None
                    else None
                ),
                settings=body.settings,
                metadata=body.metadata,
            )
            return await _run_with_inputs(app.state.store, run)
        except CommandConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except WorkspaceAdmissionError as exc:
            status_code = 409 if "no longer available" in str(exc) else 403
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        except ParentRunAdmissionError as exc:
            status_code = 404 if "not found" in str(exc) else 409
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        except ThreadAdmissionError as exc:
            status_code = 404 if "not found" in str(exc) else 409
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        except RunAdmissionError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    @app.get("/v1/schedules", response_model=ListScheduledRunsResponse)
    async def list_schedules(
        request: Request,
        status: str | None = Query(default=None),
        notify_pending: bool = Query(default=False),
    ) -> dict[str, Any]:
        store: LocalStore = app.state.store
        schedules = await store.list_scheduled_runs_for_principal(
            principal_id=request.state.principal_id,
            status=status,
            notify_pending=notify_pending,
        )
        return {"schedules": schedules}

    @app.post("/v1/schedules", response_model=LocalScheduledRun)
    async def create_schedule(request: Request, body: CreateScheduledRunRequest) -> dict[str, Any]:
        goal = body.goal.strip()
        if not goal:
            raise HTTPException(status_code=400, detail="goal required")
        store: LocalStore = app.state.store
        principal_id = request.state.principal_id
        workspace_path = (
            await _normalized_path(body.workspace_path) if body.workspace_path is not None else None
        )
        try:
            return await store.create_scheduled_run(
                principal_id=principal_id,
                goal=goal,
                run_at=_normalize_schedule_time(body.run_at),
                workspace_path=workspace_path,
                model=body.model.strip(),
                history=body.history or [],
                settings=freeze_run_settings(
                    app.state.settings,
                    {**(body.settings or {}), "permission_mode": body.permission_mode},
                ),
                metadata=sanitize_run_metadata(body.metadata),
            )
        except WorkspaceAdmissionError as exc:
            status_code = 409 if "no longer available" in str(exc) else 403
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    @app.delete("/v1/schedules/{schedule_id}", response_model=LocalScheduledRun)
    async def cancel_schedule(request: Request, schedule_id: str) -> dict[str, Any]:
        store: LocalStore = app.state.store
        schedule = await store.cancel_scheduled_run(
            principal_id=request.state.principal_id,
            schedule_id=schedule_id,
        )
        if schedule is None:
            raise HTTPException(status_code=404, detail="schedule not found")
        return schedule

    @app.post("/v1/schedules/{schedule_id}/notified", response_model=LocalScheduledRun)
    async def mark_schedule_notified(request: Request, schedule_id: str) -> dict[str, Any]:
        store: LocalStore = app.state.store
        schedule = await store.mark_scheduled_run_notified(
            principal_id=request.state.principal_id,
            schedule_id=schedule_id,
        )
        if schedule is None:
            raise HTTPException(status_code=404, detail="schedule not found")
        return schedule

    @app.post("/v1/runs/{run_id}/fork", response_model=LocalRun)
    async def fork_run(request: Request, run_id: str, body: ForkRunRequest) -> dict[str, Any]:
        checkpoint_id = body.checkpoint_id.strip()
        if not checkpoint_id:
            raise HTTPException(status_code=400, detail="checkpoint_id required")
        await _owned_run(
            app.state.store,
            principal_id=request.state.principal_id,
            run_id=run_id,
        )
        coordinator: RunCoordinator = app.state.coordinator
        try:
            run = await coordinator.fork_run(
                principal_id=request.state.principal_id,
                source_run_id=run_id,
                command_id=body.command_id,
                client_message_id=body.client_message_id,
                assistant_message_id=body.assistant_message_id,
                thread_id=body.thread_id,
                protocol_version=body.protocol_version,
                required_capabilities=body.required_capabilities,
                checkpoint_id=checkpoint_id,
                goal=body.goal,
                user_input=body.user_input,
                thread_title=body.thread_title,
                thread_metadata=body.thread_metadata,
                user_item_metadata=body.user_item_metadata,
                metadata=body.metadata,
            )
            return await _run_with_inputs(app.state.store, run)
        except RunNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "run_not_found", "message": "run not found"},
            ) from exc
        except CheckpointNotFoundError as exc:
            raise HTTPException(status_code=404, detail="checkpoint not found") from exc
        except WorkspaceAdmissionError as exc:
            status_code = 409 if "no longer available" in str(exc) else 403
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        except ThreadAdmissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CommandConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RunAdmissionError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/runs/{run_id}", response_model=LocalRun)
    async def get_run(request: Request, run_id: str) -> dict[str, Any]:
        """Return the flat run record (same shape as POST /runs)."""
        store: LocalStore = app.state.store
        run = await _owned_run(
            store,
            principal_id=request.state.principal_id,
            run_id=run_id,
        )
        return await _run_with_inputs(store, run)

    @app.get("/v1/runs/{run_id}/children", response_model=ListChildRunsResponse)
    async def list_child_runs(request: Request, run_id: str) -> dict[str, Any]:
        store: LocalStore = app.state.store
        await _owned_run(
            store,
            principal_id=request.state.principal_id,
            run_id=run_id,
        )
        return {"children": await store.list_child_runs_for_run(run_id)}

    @app.get(
        "/v1/runs/{run_id}/collaboration",
        response_model=LocalCollaborationSnapshot,
    )
    async def get_collaboration_snapshot(request: Request, run_id: str) -> dict[str, Any]:
        await _owned_run(
            app.state.store,
            principal_id=request.state.principal_id,
            run_id=run_id,
        )
        try:
            return await app.state.coordinator.collaboration_snapshot(run_id)
        except RunAdmissionError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    @app.get("/v1/runs/{run_id}/mailbox", response_model=ListAgentMessagesResponse)
    async def list_agent_messages(
        request: Request,
        run_id: str,
        box: Literal["inbox", "outbox"] = Query(default="inbox"),
    ) -> dict[str, Any]:
        store: LocalStore = app.state.store
        await _owned_run(
            store,
            principal_id=request.state.principal_id,
            run_id=run_id,
        )
        messages = (
            await store.list_agent_inbox(run_id)
            if box == "inbox"
            else await store.list_agent_outbox(run_id)
        )
        return {"messages": messages}

    @app.get("/v1/runs/{run_id}/events", response_model=ListRunEventsResponse)
    async def list_run_events(
        request: Request,
        run_id: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=1000, ge=1, le=5000),
    ) -> dict[str, Any]:
        store: LocalStore = app.state.store
        await _owned_run(
            store,
            principal_id=request.state.principal_id,
            run_id=run_id,
        )
        raw_events = await store.events_since(run_id, after_seq=after, limit=limit + 1)
        has_more = len(raw_events) > limit
        page = raw_events[:limit]
        return {
            "events": [{**event, "payload": _event_payload(event)} for event in page],
            "has_more": has_more,
            "next_after": int(page[-1]["seq"]) if page else after,
        }

    @app.get("/v1/runs/{run_id}/stream")
    async def stream_run(
        request: Request,
        run_id: str,
        after: int = Query(default=0, ge=0),
    ) -> EventSourceResponse:
        store: LocalStore = app.state.store
        await _owned_run(
            store,
            principal_id=request.state.principal_id,
            run_id=run_id,
        )
        first_seq, latest_seq = await store.event_sequence_window(run_id)
        if after > latest_seq or (first_seq is not None and after < first_seq - 1):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "event_cursor_reset_required",
                    "message": "event cursor is outside the retained event window",
                    "requested_after": after,
                    "first_available_seq": first_seq,
                    "latest_seq": latest_seq,
                },
            )
        coordinator: RunCoordinator = app.state.coordinator

        async def gen():
            # The client's `parseAgentSSEChunk` (sse.ts) reads
            #   data: {"event_type": "...", "payload": {...}, "id":...}
            # and recognizes only `data: [DONE]` as the completion mark.
            # So we must:
            #   • dump the whole envelope into `data:` (not the bare
            #     payload like the old shape — that made event_type
            #     undefined on the client and the entire UI silently
            #     no-op'd);
            #   • end with `data: [DONE]` so the stream resolves.
            try:
                async for event in coordinator.stream(run_id, after_seq=after):
                    yield {
                        "id": str(event.get("seq") or event["id"]),
                        "event": event["event_type"],
                        "data": json.dumps(event, default=str, ensure_ascii=False),
                    }
            finally:
                yield {"data": "[DONE]"}

        # `sep="\n"` (LF) matches the Runtime SDK parser, which splits on
        # `/\n\n/`. sse-starlette's default `\r\n` is spec-correct but does
        # not match that protocol contract.
        return EventSourceResponse(gen(), sep="\n")

    @app.post("/v1/runs/{run_id}/cancel", response_model=CancelRunResponse)
    async def cancel_run(request: Request, run_id: str) -> dict[str, Any]:
        await _owned_run(
            app.state.store,
            principal_id=request.state.principal_id,
            run_id=run_id,
        )
        coordinator: RunCoordinator = app.state.coordinator
        ok = await coordinator.cancel_run(run_id)
        return {"canceled": ok}

    @app.post(
        "/v1/runs/{run_id}/inject",
        response_model=InjectRunInstructionResponse,
    )
    async def inject_run_instruction(
        request: Request,
        run_id: str,
        body: InjectRunInstructionRequest,
    ) -> dict[str, Any]:
        content = body.content.strip()
        if not content:
            raise HTTPException(status_code=400, detail="content required")
        store: LocalStore = app.state.store
        try:
            receipt, _created = await store.request_run_inject_command(
                principal_id=request.state.principal_id,
                command_id=body.command_id,
                run_id=run_id,
                content=content,
            )
            return receipt
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        except (CommandConflictError, RunAdmissionError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # ---- compatibility shims the client expects (pre-existing Node API) ----
    #
    # Some of these are full features that didn't make it into Phase 3'/4'
    # yet. They return safe defaults / 501 so the client can boot without
    # crashing on missing routes. Implementations land in Phase 6'+.

    @app.post("/v1/permissions/{permission_id}", response_model=PermissionResolution)
    async def resolve_permission(
        request: Request, permission_id: str, body: ResolvePermissionRequest
    ) -> dict[str, Any]:
        """Approve, edit, or deny a parameter-bound tool review.

        Translates the client's `{decision, scope}` body into the
        `{"decisions": [{"type": "approve"|"edit"|"reject", ...}]}` shape
        that `ToolReviewMiddleware` verifies on resume. One LangGraph
        interrupt can contain multiple action requests, so the run
        resumes only after every permission in the current pause batch is
        resolved, preserving the original `permission.required` order.

        `scope=run` is a durable grant for an eligible ordinary tool for the
        rest of the run, with the same version and risk class. Irreversible
        and unknown actions cannot receive this scope.
        """
        decision_text = body.decision
        scope = body.scope
        store: LocalStore = app.state.store
        record = await store.get_permission(permission_id)
        if record is None:
            raise HTTPException(status_code=404, detail="permission not found")
        run = await _owned_run(
            store,
            principal_id=request.state.principal_id,
            run_id=record["run_id"],
            not_found_detail="permission not found",
        )
        await _authorized_workspace_path(
            store,
            principal_id=request.state.principal_id,
            path=run.get("workspace_path"),
        )
        if decision_text == "approve":
            hitl_decision: dict[str, Any] = {"type": "approve"}
            persisted_status = "approved"
        elif decision_text == "edit":
            assert body.edited_action is not None
            if body.edited_action.name != record["tool_name"]:
                raise HTTPException(status_code=400, detail="tool name cannot be changed")
            hitl_decision = {
                "type": "edit",
                "edited_action": body.edited_action.model_dump(),
            }
            persisted_status = "approved"
        else:
            hitl_decision = {
                "type": "reject",
                "message": "Tool execution denied by user.",
            }
            persisted_status = "denied"
        already_resolved = record.get("status") != "pending"
        if not already_resolved and run.get("status") in _TERMINAL_RUN_STATUSES:
            raise HTTPException(status_code=409, detail="run is not awaiting a decision")
        resolution_event = {
            "request_id": permission_id,
            "tool": record["tool_name"],
            "tool_name": record["tool_name"],
            "operation_id": record.get("operation_id"),
            "decision": decision_text,
            "scope": str(scope),
        }
        try:
            await store.resolve_permission(
                permission_id,
                status=persisted_status,
                scope=str(scope),
                decision=hitl_decision,
                event_payload=None if already_resolved else resolution_event,
            )
        except PermissionScopeNotAllowedError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (PermissionDecisionConflictError, WaitDecisionConflictError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        coordinator: RunCoordinator = app.state.coordinator
        if not already_resolved:
            coordinator.wake_run(str(record["run_id"]))
        resume_payload = await store.wait_cycle_resume_payload(
            run_id=str(record["run_id"]),
            wait_cycle_id=str(record.get("wait_cycle_id") or record["id"]),
        )
        if resume_payload is None:
            ok = False
        else:
            ok = await _ensure_resume_job(
                store=store,
                coordinator=coordinator,
                run_id=record["run_id"],
                decision=resume_payload,
            )
        return {
            "permission_id": permission_id,
            "resolved": True,
            "decision": decision_text,
            "scope": scope,
            "resumed": ok,
        }

    @app.post("/v1/questions/{question_id}", response_model=QuestionAnswer)
    async def answer_question(
        request: Request, question_id: str, body: AnswerQuestionRequest
    ) -> dict[str, Any]:
        """Submit answers to a paused user.ask interrupt.

        Body shape (per `client.ts:answerLocalQuestion`):
        `{answers: Record<string, string[]>}`. We look up the question
        by id to find its run_id, persist the answers, then resume.
        """
        answers = body.answers
        store: LocalStore = app.state.store
        record = await store.get_question(question_id)
        if record is None:
            raise HTTPException(status_code=404, detail="question not found")
        run = await _owned_run(
            store,
            principal_id=request.state.principal_id,
            run_id=record["run_id"],
            not_found_detail="question not found",
        )
        await _authorized_workspace_path(
            store,
            principal_id=request.state.principal_id,
            path=run.get("workspace_path"),
        )
        already_answered = record.get("status") != "pending"
        if not already_answered and run.get("status") in _TERMINAL_RUN_STATUSES:
            raise HTTPException(status_code=409, detail="run is not awaiting a decision")
        answer_event = {"request_id": question_id, "answers": answers}
        try:
            await store.answer_question(
                question_id,
                answers=answers,
                event_payload=None if already_answered else answer_event,
            )
        except WaitDecisionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        coordinator: RunCoordinator = app.state.coordinator
        if not already_answered:
            coordinator.wake_run(str(record["run_id"]))
        resume_payload = await store.wait_cycle_resume_payload(
            run_id=str(record["run_id"]),
            wait_cycle_id=str(record.get("wait_cycle_id") or record["id"]),
        )
        if resume_payload is not None:
            ok = await _ensure_resume_job(
                store=store,
                coordinator=coordinator,
                run_id=record["run_id"],
                decision=resume_payload,
            )
        else:
            ok = False
        return {
            "question_id": question_id,
            "answered": True,
            "resumed": ok,
        }

    @app.post(
        "/v1/tool-reconciliations/{operation_id}",
        response_model=ToolReconciliationResolution,
    )
    async def reconcile_tool_operation(
        request: Request,
        operation_id: str,
        body: ReconcileToolRequest,
    ) -> dict[str, Any]:
        store: LocalStore = app.state.store
        command_id = f"legacy_reconcile_{operation_id}"
        try:
            replay = await store.accepted_command_receipt(
                principal_id=request.state.principal_id,
                command_id=command_id,
                command_type="tool.reconcile",
                payload={
                    "type": "tool.reconcile",
                    "operation_id": operation_id,
                    "decision": body.decision,
                },
            )
        except CommandConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if replay is not None:
            if replay.get("resumed"):
                app.state.coordinator.wake_jobs()
            return replay
        record = await store.get_wait_candidate(operation_id)
        if record is None or record.get("kind") != "tool_reconciliation":
            raise HTTPException(status_code=404, detail="tool reconciliation not found")
        run = await _owned_run(
            store,
            principal_id=request.state.principal_id,
            run_id=str(record["run_id"]),
            not_found_detail="tool reconciliation not found",
        )
        await _authorized_workspace_path(
            store,
            principal_id=request.state.principal_id,
            path=run.get("workspace_path"),
        )
        if run.get("status") in _TERMINAL_RUN_STATUSES:
            raise HTTPException(status_code=409, detail="run is not awaiting a decision")
        coordinator: RunCoordinator = app.state.coordinator
        await coordinator.reconcile_resume_head(str(record["run_id"]))
        try:
            results = await _tool_reconciliation_results(
                store,
                operation_id=operation_id,
                decision=body.decision,
            )
            receipt, _created = await store.request_tool_reconcile_command(
                principal_id=request.state.principal_id,
                command_id=command_id,
                operation_id=operation_id,
                decision=body.decision,
                **results,
            )
        except (CommandConflictError, WaitDecisionConflictError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except WorkspaceAdmissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if receipt["resumed"]:
            coordinator.wake_jobs()
        return receipt

    @app.post("/v1/plans/{approval_id}", response_model=PlanApprovalResolution)
    async def resolve_plan_approval(
        request: Request,
        approval_id: str,
        body: ResolvePlanApprovalRequest,
    ) -> dict[str, Any]:
        """Approve, revise, or reject a Plan Mode `write_todos` pause."""
        decision_text = body.decision
        instructions = (body.instructions or "").strip() or None
        if decision_text == "modify" and not instructions:
            raise HTTPException(status_code=400, detail="instructions required")

        store: LocalStore = app.state.store
        record = await store.get_plan_approval(approval_id)
        if record is None:
            raise HTTPException(status_code=404, detail="plan approval not found")
        run = await _owned_run(
            store,
            principal_id=request.state.principal_id,
            run_id=record["run_id"],
            not_found_detail="plan approval not found",
        )
        await _authorized_workspace_path(
            store,
            principal_id=request.state.principal_id,
            path=run.get("workspace_path"),
        )
        if run.get("status") in _TERMINAL_RUN_STATUSES:
            raise HTTPException(status_code=409, detail="run is not awaiting a decision")
        coordinator: RunCoordinator = app.state.coordinator
        await coordinator.reconcile_resume_head(str(record["run_id"]))
        try:
            receipt, _created = await store.request_plan_resolve_command(
                principal_id=request.state.principal_id,
                command_id=f"legacy_plan_{approval_id}",
                approval_id=approval_id,
                decision=decision_text,
                instructions=instructions,
            )
        except (CommandConflictError, WaitDecisionConflictError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except WorkspaceAdmissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if receipt["resumed"]:
            coordinator.wake_jobs()
        return receipt

    @app.get("/v1/artifacts/{artifact_id}", response_model=LocalArtifact)
    async def get_artifact(request: Request, artifact_id: str) -> dict[str, Any]:
        """Return a single artifact record.

        Shape matches the TS `LocalArtifact` interface
        (`client.ts:38-44`): `{id, title, content, tool_name?, created_at?}`.
        """
        store: LocalStore = app.state.store
        record = await store.get_artifact(artifact_id)
        if record is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        await _owned_run(
            store,
            principal_id=request.state.principal_id,
            run_id=record["run_id"],
            not_found_detail="artifact not found",
        )
        return {
            "id": record["id"],
            "title": record["title"],
            "content": record["content"],
            "content_type": record["content_type"],
            "bytes": record["bytes"],
            "sha256": record.get("sha256"),
            "storage_kind": record.get("storage_kind") or "inline_text",
            "tool_name": record.get("tool_name"),
            "created_at": record["created_at"],
        }

    @app.get("/v1/artifacts/{artifact_id}/content")
    async def get_artifact_content(request: Request, artifact_id: str) -> Response:
        store: LocalStore = app.state.store
        record = await store.get_artifact(artifact_id)
        if record is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        await _owned_run(
            store,
            principal_id=request.state.principal_id,
            run_id=record["run_id"],
            not_found_detail="artifact not found",
        )
        if record.get("storage_kind") != "blob":
            return Response(
                content=record["content"],
                media_type=record["content_type"],
                headers={"Content-Disposition": f'attachment; filename="{record["id"]}"'},
            )
        try:
            body = await asyncio.to_thread(store.artifact_body_path, record)
        except ArtifactConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return FileResponse(
            body,
            filename=record["title"],
            media_type=record["content_type"],
        )

    @app.get("/v1/workspace-files")
    async def get_workspace_file(
        request: Request,
        path: str = Query(..., description="Absolute file path inside an authorized workspace"),
    ):
        """Stream a file's bytes back to the renderer.

        Gated by `local_workspaces` — the file's parent chain must be inside
        a path the user previously authorized in the client. We do
        NOT serve arbitrary paths; that would let a compromised renderer
        exfiltrate the entire disk.

        Used by the right-side DocPreviewPanel to fetch .docx / .xlsx
        bytes for in-browser rendering (docx-preview, exceljs). No
        response_model — this is a binary stream, not a JSON shape, so
        it stays out of api_schemas.py / openapi.json by design.
        """
        if not path:
            raise HTTPException(status_code=400, detail="path required")
        resolved = Path(await _normalized_path(path))
        try:
            if not await asyncio.to_thread(resolved.is_file):
                raise HTTPException(status_code=404, detail="file not found")
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"invalid path: {exc}") from exc
        store: LocalStore = app.state.store
        workspaces = await store.list_workspaces(principal_id=request.state.principal_id)
        # `is_relative_to` walks the parent chain; we need the file to live
        # under *some* authorized workspace root.
        roots = [Path(ws["path"]) for ws in workspaces]
        if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
            raise HTTPException(
                status_code=403,
                detail="path is not inside any authorized workspace",
            )
        # Let FileResponse pick the right Content-Type from the extension.
        # docx → application/vnd.openxmlformats-officedocument.wordprocessingml.document
        # xlsx → application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
        #
        # Do NOT override `Content-Disposition`. Starlette's FileResponse
        # already emits an RFC-5987-compliant header (`filename*=utf-8''…`)
        # when `filename=` contains non-ASCII characters; setting a custom
        # header with raw CJK in the value triggers an ASGI latin-1
        # encoding error and the renderer sees "Failed to fetch". The
        # fetch() consumer doesn't care about the disposition anyway —
        # it reads response.arrayBuffer() directly.
        return FileResponse(resolved, filename=resolved.name)

    @app.get(
        "/v1/runs/{run_id}/inputs/{input_id}",
        response_class=FileResponse,
        responses={
            200: {
                "content": {
                    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
                }
            }
        },
    )
    async def get_run_input(request: Request, run_id: str, input_id: str):
        """Stream one immutable Runtime-owned input to its Run owner."""
        store: LocalStore = app.state.store
        await _owned_run(
            store,
            principal_id=request.state.principal_id,
            run_id=run_id,
        )
        record = next(
            (item for item in await store.list_run_inputs(run_id) if item["input_id"] == input_id),
            None,
        )
        if record is None:
            raise HTTPException(status_code=404, detail="run input not found")
        try:
            body = await asyncio.to_thread(store.run_input_body_path, record)
        except RunInputSnapshotError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return FileResponse(
            body,
            filename=record["original_name"],
            media_type="application/octet-stream",
        )

    @app.get("/v1/pptx-outline", response_model=PptxOutlineResponse)
    async def get_pptx_outline(
        request: Request,
        path: str = Query(..., description="Absolute .pptx path inside an authorized workspace"),
    ) -> dict[str, Any]:
        """Return the slide outline JSON for a .pptx file.

        Used by the right-side DocPreviewPanel's PptxPreview component
        — pptx has no mature pure-browser renderer, so the panel renders
        a structured outline (title + bullets + notes per slide) here
        rather than embedding a viewer in iframe.

        Gated by `local_workspaces`, same as `/workspace-files`. The
        path's parent chain must be inside a previously-authorized
        workspace. Calls the shared `_outline_pptx` helper that
        `office.outline` and `office.read_slides` also use, so the
        JSON shape is identical to those tools.
        """
        if not path:
            raise HTTPException(status_code=400, detail="path required")
        resolved = Path(await _normalized_path(path))
        try:
            if not await asyncio.to_thread(resolved.is_file):
                raise HTTPException(status_code=404, detail="file not found")
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"invalid path: {exc}") from exc
        if resolved.suffix.lower() != ".pptx":
            raise HTTPException(status_code=400, detail="path must point to a .pptx file")
        store: LocalStore = app.state.store
        workspaces = await store.list_workspaces(principal_id=request.state.principal_id)
        roots = [Path(ws["path"]) for ws in workspaces]
        if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
            raise HTTPException(
                status_code=403,
                detail="path is not inside any authorized workspace",
            )
        # Defer the import so the runtime boot path doesn't pay for
        # python-pptx unless someone actually previews a deck.
        from .tools.office import _outline_pptx

        try:
            return await asyncio.to_thread(_outline_pptx, str(resolved))
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"failed to outline .pptx: {exc.__class__.__name__}: {exc}",
            ) from exc

    @app.get(
        "/v1/runs/{run_id}/inputs/{input_id}/pptx-outline",
        response_model=PptxOutlineResponse,
    )
    async def get_run_input_pptx_outline(
        request: Request,
        run_id: str,
        input_id: str,
    ) -> dict[str, Any]:
        """Return a deck outline from the immutable Runtime-owned input."""
        store: LocalStore = app.state.store
        await _owned_run(
            store,
            principal_id=request.state.principal_id,
            run_id=run_id,
        )
        record = next(
            (item for item in await store.list_run_inputs(run_id) if item["input_id"] == input_id),
            None,
        )
        if record is None:
            raise HTTPException(status_code=404, detail="run input not found")
        if Path(str(record["original_name"])).suffix.lower() != ".pptx":
            raise HTTPException(status_code=400, detail="run input must be a .pptx file")
        try:
            body = await asyncio.to_thread(store.run_input_body_path, record)
        except RunInputSnapshotError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        from .tools.office import _outline_pptx

        try:
            return await asyncio.to_thread(_outline_pptx, str(body))
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"failed to outline .pptx: {exc.__class__.__name__}: {exc}",
            ) from exc

    @app.get("/v1/runs/{run_id}/diagnostics", response_model=LocalRunDiagnostics)
    async def run_diagnostics(request: Request, run_id: str) -> dict[str, Any]:
        """Return the full `LocalRunDiagnostics` payload.

        Shape is defined by `LocalRunDiagnostics` and generated into the SDK.
        It includes the redacted durable trace projection used by exports.

        Phase 5'+ used to return only `{run, events}`, so the
        `DiagnosticsPanel` rendered NaN counts (permissions.length on
        undefined) and the "latest checkpoint" tab was always missing.
        """
        store: LocalStore = app.state.store
        run = await _owned_run(
            store,
            principal_id=request.state.principal_id,
            run_id=run_id,
        )
        raw_events = await store.events_since(run_id, after_seq=0)
        events = [
            {
                "id": e["id"],
                "run_id": e["run_id"],
                "seq": e["seq"],
                "event_type": e["event_type"],
                "payload": json.loads(e.get("payload_json") or "{}"),
                "created_at": e["created_at"],
            }
            for e in raw_events
        ]
        permissions = await store.list_permissions_for_run(run_id)
        tool_receipts = await store.list_tool_receipts_for_run(run_id)
        model_calls = await store.list_model_calls_for_run(run_id)
        child_runs = await store.list_child_runs_for_run(run_id)
        wait_candidates = await store.list_wait_candidates_for_run(run_id)
        artifacts = await store.list_artifacts_for_run(run_id)
        latest_checkpoint = await _latest_checkpoint_summary(app.state.checkpointer, run)
        reflection = await _latest_checkpoint_reflection(app.state.checkpointer, run)
        return {
            "schema_version": 1,
            "exported_at": datetime.now(UTC).isoformat(),
            "runtime_version": __version__,
            "run": await _run_with_inputs(store, run),
            "events": events,
            "permissions": permissions,
            "tool_receipts": [
                {
                    key: receipt.get(key)
                    for key in (
                        "operation_id",
                        "tool_call_id",
                        "tool_name",
                        "tool_version",
                        "arguments_hash",
                        "risk",
                        "status",
                        "attempt_count",
                        "result_hash",
                        "error_type",
                        "created_at",
                        "started_at",
                        "completed_at",
                        "updated_at",
                    )
                }
                for receipt in tool_receipts
            ],
            "wait_candidates": [
                {
                    key: candidate.get(key)
                    for key in ("id", "kind", "status", "created_at", "resolved_at")
                }
                for candidate in wait_candidates
            ],
            "artifacts": artifacts,
            "latest_checkpoint": latest_checkpoint,
            "handoff": _build_diagnostics_handoff(run, events, permissions, artifacts),
            "feature_ledger": _latest_feature_ledger(artifacts),
            "reflection": reflection,
            "trace": build_run_trace(
                run,
                model_calls=model_calls,
                tool_receipts=tool_receipts,
                child_runs=child_runs,
                checkpoint=latest_checkpoint,
                event_count=len(events),
            ),
        }

    @app.post(
        "/v1/workspaces/diagnose",
        response_model=LocalWorkspaceDiagnosis,
        response_model_exclude_none=True,
    )
    async def diagnose_workspace(
        request: Request, body: DiagnoseWorkspaceRequest
    ) -> dict[str, Any]:
        """Inspect a candidate path against the authorization registry.

        Response matches the TS `LocalWorkspaceDiagnosis` shape — the
        `reason` enum drives the workspace-picker's "why disabled?"
        copy, keep it stable.
        """
        store: LocalStore = app.state.store
        path = body.path.strip()
        if not path:
            raise HTTPException(status_code=400, detail="path required")
        resolved = await _normalized_path(path)
        path_obj = Path(resolved)
        exists, is_directory = await asyncio.gather(
            asyncio.to_thread(path_obj.exists),
            asyncio.to_thread(path_obj.is_dir),
        )
        workspace = await store.workspace_by_path(
            principal_id=request.state.principal_id,
            path=resolved,
        )
        authorized = workspace is not None
        if not exists:
            reason = "not_found"
        elif not is_directory:
            reason = "not_directory"
        elif authorized:
            reason = "authorized"
        else:
            reason = "not_authorized"
        payload: dict[str, Any] = {
            "path": resolved,
            "exists": exists,
            "is_directory": is_directory,
            "authorized": authorized,
            "reason": reason,
        }
        if workspace is not None:
            payload["workspace"] = workspace
        return payload

    @app.delete("/v1/memory", response_model=ClearMemoryResponse)
    async def clear_memory(request: Request) -> dict[str, Any]:
        """Wipe this authenticated principal's long-term memory namespaces.

        Backs the "清空记忆 / Clear memory" button in the agent settings
        dialog. Walks every ("notes", ...) namespace in pages of 200
        (matches the BaseStore default search limit ceiling for SQLite stores)
        and deletes each key. Returns the total count so the UI can render an
        accurate "cleared N memories" toast.

        Idempotent: calling it on an empty store returns
        `deleted_count: 0` without error.
        """
        from .tools.memory import NAMESPACE, memory_namespace_prefix

        agent_store = getattr(app.state, "agent_store", None)
        if agent_store is None:
            raise HTTPException(status_code=503, detail="memory store not initialized")
        deleted = 0
        # `asearch(query=None)` returns everything in the namespace
        # (no semantic ranking needed — we just want the keys).
        # Loop until a page comes back smaller than `limit` so we don't
        # over-fetch on a single-item store.
        page_size = 200
        principal_prefix = memory_namespace_prefix(request.state.principal_id)
        namespaces = [principal_prefix]
        if request.state.principal_id == LOCAL_OWNER_PRINCIPAL_ID:
            namespaces.insert(0, NAMESPACE)
        if hasattr(agent_store, "alist_namespaces"):
            namespaces = (
                [NAMESPACE] if request.state.principal_id == LOCAL_OWNER_PRINCIPAL_ID else []
            )
            offset = 0
            while True:
                page = await agent_store.alist_namespaces(
                    prefix=principal_prefix,
                    limit=page_size,
                    offset=offset,
                )
                if not page:
                    break
                namespaces.extend(page)
                if len(page) < page_size:
                    break
                offset += len(page)
        for namespace in namespaces:
            while True:
                items = await agent_store.asearch(namespace, limit=page_size)
                if not items:
                    break
                for item in items:
                    try:
                        await agent_store.adelete(namespace, item.key)
                        deleted += 1
                    except Exception as exc:
                        log.warning(
                            "memory delete failed namespace=%s key=%s: %s", namespace, item.key, exc
                        )
                if len(items) < page_size:
                    break
        return {"cleared": True, "deleted_count": deleted}

    @app.get("/v1/mcp-servers", response_model=McpServerCatalog)
    async def list_mcp_servers() -> McpServerCatalog:
        """List MCP Servers explicitly owned by this Runtime."""
        from .config import get_settings
        from .tools.mcp import _candidate_source_files, discover_servers

        settings = get_settings()
        discovered = discover_servers(settings.data_dir)
        statuses = app.state.mcp_catalog.server_statuses()
        sources_scanned: list[str] = ["env"]
        for src in _candidate_source_files(settings.data_dir):
            if src.source not in sources_scanned:
                sources_scanned.append(src.source)
        servers = []
        for srv in discovered:
            status = statuses.get(srv.name, {})
            servers.append(
                McpServerInfo(
                    name=srv.name,
                    transport=srv.config.get("transport", "stdio"),
                    source=srv.source,
                    source_path=srv.source_path,
                    command=srv.config.get("command"),
                    args=list(srv.config.get("args", []) or []),
                    url=srv.config.get("url"),
                    # Never leak env *values* — only the keys, so the UI
                    # can show "needs API_KEY, TAVILY_KEY" without exposing
                    # secrets that were copy-pasted in.
                    env_keys=sorted(list((srv.config.get("env") or {}).keys())),
                    cwd=srv.config.get("cwd"),
                    status=status.get("status", "idle"),
                    tool_count=int(status.get("tool_count", 0)),
                    error_type=status.get("error_type"),
                )
            )
        return McpServerCatalog(servers=servers, sources_scanned=sources_scanned)

    @app.post("/v1/mcp-servers", response_model=McpServerWriteResponse)
    async def create_mcp_server(request: McpServerWriteRequest) -> McpServerWriteResponse:
        response = _write_mcp_server(request.name, request)
        await app.state.mcp_catalog.invalidate(response.server.name)
        app.state.mcp_catalog.request_refresh()
        return response

    @app.put("/v1/mcp-servers/{server_name}", response_model=McpServerWriteResponse)
    async def update_mcp_server(
        server_name: str, request: McpServerWriteRequest
    ) -> McpServerWriteResponse:
        response = _write_mcp_server(server_name, request)
        await app.state.mcp_catalog.invalidate(response.server.name)
        app.state.mcp_catalog.request_refresh()
        return response

    @app.delete("/v1/mcp-servers/{server_name}", response_model=McpServerDeleteResponse)
    async def delete_mcp_server(server_name: str) -> McpServerDeleteResponse:
        name = _safe_catalog_name(server_name)
        config = _read_shejane_mcp_config()
        servers = config.setdefault("mcpServers", {})
        if isinstance(servers, dict):
            servers.pop(name, None)
        _write_json_atomic(_shejane_mcp_config_path(), config)
        await app.state.mcp_catalog.invalidate(name)
        await app.state.store.delete_mcp_catalog(name)
        return McpServerDeleteResponse(name=name)

    @app.get("/v1/skills")
    async def list_local_skills() -> dict[str, Any]:
        """Catalog of every SKILL.md the runtime can see across all
        configured skill roots (`~/.shejane/skills/`, `~/.claude/skills/`,
        or `SHEJANE_RUNTIME_SKILLS_PATH` overrides). Skills are managed
        out-of-band — the user drops directories into a root themselves
        (or installs via the skills.sh CLI into `~/.claude/skills/`) and
        the runtime picks them up on next scan.

        Also surfaces the roots themselves under `roots` so the UI can
        render section headers (e.g. "Personal" for shejane) even when
        a root is empty — otherwise the user has no idea where to drop
        their SKILL.md directories.
        """
        from .agent.builder import _resolve_skills_dirs

        roots = [
            {
                "source": (d.parent.name or d.name).lstrip("."),
                "path": str(d),
            }
            for d in _resolve_skills_dirs()
        ]
        return {"skills": _list_skill_files(), "roots": roots}

    @app.post("/v1/skills", response_model=SkillWriteResponse)
    async def create_local_skill(request: SkillWriteRequest) -> SkillWriteResponse:
        return _write_local_skill(request.name, request)

    @app.get("/v1/skills/{skill_name}", response_model=SkillFile)
    async def get_local_skill(skill_name: str) -> SkillFile:
        name = _safe_catalog_name(skill_name)
        return _skill_file_from_path(name, _skill_md_path(name))

    @app.put("/v1/skills/{skill_name}", response_model=SkillWriteResponse)
    async def update_local_skill(skill_name: str, request: SkillWriteRequest) -> SkillWriteResponse:
        return _write_local_skill(skill_name, request)

    @app.delete("/v1/skills/{skill_name}", response_model=SkillDeleteResponse)
    async def delete_local_skill(skill_name: str) -> SkillDeleteResponse:
        name = _safe_catalog_name(skill_name)
        path = _skill_md_path(name)
        if not path.exists():
            raise HTTPException(status_code=404, detail="skill not found")
        shutil.rmtree(path.parent)
        return SkillDeleteResponse(name=name)

    return app


def _current_permission_batch(
    raw_events: list[dict[str, Any]],
    permissions: list[dict[str, Any]],
    fallback_permission_id: str,
) -> list[dict[str, Any]]:
    """Return permission rows for the currently paused HITL batch.

    deepagents/LangGraph can bundle several tool approvals into one interrupt
    and expects one ordered `decisions` list on resume. We derive the batch from
    `permission.required` events emitted since the latest run start/resume
    boundary; if old rows or a sparse event log confuse that lookup, fall back
    to the single permission the user just resolved.
    """
    permission_by_id = {
        permission_id: permission
        for permission in permissions
        if (permission_id := str(permission.get("id") or ""))
    }
    batch_ids = _current_permission_batch_ids(raw_events)
    if fallback_permission_id not in batch_ids:
        batch_ids = [fallback_permission_id]

    batch: list[dict[str, Any]] = []
    seen: set[str] = set()
    for permission_id in batch_ids:
        if permission_id in seen:
            continue
        record = permission_by_id.get(permission_id)
        if record is None:
            continue
        seen.add(permission_id)
        batch.append(record)
    fallback = permission_by_id.get(fallback_permission_id)
    if not batch and fallback is not None:
        return [fallback]
    return batch


async def _ensure_resume_job(
    *,
    store: LocalStore,
    coordinator: RunCoordinator,
    run_id: str,
    decision: dict[str, Any],
) -> bool:
    """Idempotently ensure a resolved wait has a durable resume owner.

    The decision and resume job are currently separate SQLite transactions.
    Replaying the same decision repairs the crash window between them instead
    of falsely acknowledging a run that remains permanently paused.
    """
    if await coordinator.resume_run(run_id=run_id, decision=decision):
        return True
    run = await store.get_run(run_id)
    if run is None:
        return False
    if run.get("status") in {"completed", "failed", "canceled"}:
        return True
    if run.get("status") not in {"waiting_permission", "waiting_input"}:
        return False
    active_job = await store.get_active_run_job(run_id)
    return bool(
        active_job
        and active_job.get("kind") == "resume"
        and active_job.get("status") in {"pending", "leased"}
    )


def _current_permission_batch_ids(raw_events: list[dict[str, Any]]) -> list[str]:
    boundary_index = -1
    for index, event in enumerate(raw_events):
        if event.get("event_type") in {"run.started", "run.resumed"}:
            boundary_index = index

    request_ids: list[str] = []
    seen: set[str] = set()
    for event in raw_events[boundary_index + 1 :]:
        if event.get("event_type") != "permission.required":
            continue
        payload = _event_payload(event)
        request_id = _first_string(payload.get("request_id"), payload.get("id"))
        if request_id is None or request_id in seen:
            continue
        seen.add(request_id)
        request_ids.append(request_id)
    return request_ids


def _thread_record_for_api(thread: dict[str, Any]) -> dict[str, Any]:
    try:
        metadata = json.loads(thread.get("metadata_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        metadata = {}
    return {**thread, "metadata": metadata if isinstance(metadata, dict) else {}}


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    if isinstance(payload, dict):
        return payload
    payload_json = event.get("payload_json")
    if isinstance(payload_json, str):
        try:
            parsed = json.loads(payload_json)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _tool_reconciliation_results(
    store: LocalStore,
    *,
    operation_id: str,
    decision: str,
) -> dict[str, str | None]:
    record = await store.get_wait_candidate(operation_id)
    if record is None or record.get("kind") != "tool_reconciliation":
        raise KeyError(operation_id)
    payload = _json_object(record.get("payload_json"))
    current_receipt = await store.get_tool_receipt(operation_id)
    prior_operation_id = str(payload.get("prior_operation_id") or operation_id)
    prior_receipt = await store.get_tool_receipt(prior_operation_id)
    if current_receipt is None or prior_receipt is None:
        raise WaitDecisionConflictError("tool reconciliation receipt is missing")
    current_result = (
        _tool_reconciliation_result(current_receipt, decision)
        if decision != "retry_not_executed"
        else None
    )
    prior_result = _tool_reconciliation_result(
        prior_receipt,
        "abort" if decision == "retry_not_executed" else decision,
    )
    return {
        "current_result_json": current_result,
        "current_result_hash": (
            hashlib.sha256(current_result.encode()).hexdigest()
            if current_result is not None
            else None
        ),
        "prior_result_json": prior_result,
        "prior_result_hash": hashlib.sha256(prior_result.encode()).hexdigest(),
    }


def _tool_reconciliation_result(receipt: dict[str, Any], decision: str) -> str:
    completed = decision == "confirmed_completed"
    return serialize_tool_result(
        ToolMessage(
            content=(
                "The user verified that the external action completed successfully."
                if completed
                else "The user verified that this uncertain action must not be retried automatically."
            ),
            name=str(receipt.get("tool_name") or ""),
            tool_call_id=str(receipt.get("tool_call_id") or ""),
            status="success" if completed else "error",
        )
    )


def _hitl_decision_for_permission(permission: dict[str, Any]) -> dict[str, Any]:
    raw = permission.get("decision_json")
    if isinstance(raw, str) and raw:
        try:
            decision = json.loads(raw)
        except json.JSONDecodeError:
            decision = None
        if isinstance(decision, dict):
            return decision
    # Compatibility for permission rows created before decision_json existed.
    if permission.get("status") == "approved":
        return {"type": "approve"}
    return {"type": "reject", "message": "Tool execution denied by user."}


def _build_diagnostics_handoff(
    run: dict[str, Any],
    events: list[dict[str, Any]],
    permissions: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    status = str(run.get("status") or "unknown")
    event_count = len(events)
    artifact_count = len(artifacts)
    pending_permissions = [p for p in permissions if p.get("status") == "pending"]
    recent_event_types = [str(e.get("event_type") or "") for e in events[-8:]]
    recent_event_types = [e for e in recent_event_types if e]
    ledger_state, ledger_message = _progress_ledger_state(events, artifacts)
    verification = _latest_task_verification(events)
    failure = _latest_failure_classification(events, run_status=status, verification=verification)

    blockers: list[str] = []
    if pending_permissions:
        names = sorted({str(p.get("tool_name") or "tool") for p in pending_permissions})
        blockers.append(f"Waiting for permission: {', '.join(names)}")

    if failure:
        blockers.append(_failure_blocker(failure))
    if verification and verification["status"] == "failed":
        blockers.append(f"Latest task.verify failed: {verification.get('reason') or 'unknown'}")

    if status == "completed":
        headline = f"Run completed with {event_count} events and {artifact_count} artifacts."
        next_actions = ["Review the final answer and any listed artifacts."]
    elif status == "waiting_permission" or pending_permissions:
        headline = f"Run is waiting on {len(pending_permissions)} permission request(s)."
        next_actions = ["Approve or deny pending permission requests to continue the run."]
    elif status == "waiting_input":
        headline = "Run is waiting for user input."
        next_actions = ["Answer the pending question to continue the run."]
    elif status in {"queued", "running"}:
        headline = f"Run is {status} with {event_count} persisted events."
        next_actions = ["Reconnect to the stream or wait for the run to reach a terminal state."]
    elif status == "cleanup_required":
        headline = "Run is quarantined because execution cleanup could not yet be confirmed."
        blockers.append("The Runtime has not released this execution generation.")
        next_actions = [
            "Do not retry automatically; inspect Runtime diagnostics and cleanup state."
        ]
    elif status == "failed":
        headline = f"Run failed after {event_count} events."
        next_actions = ["Inspect blockers and recent failed events before retrying."]
    elif status == "canceled":
        headline = f"Run was canceled after {event_count} events."
        next_actions = ["Start a new run if the goal still needs work."]
    else:
        headline = f"Run status is {status} with {event_count} events."
        next_actions = ["Inspect recent events before resuming work."]

    if status in _HANDOFF_STATUSES and ledger_state != "fresh":
        if ledger_message:
            blockers.append(ledger_message)
        if ledger_state == "missing":
            next_actions.append(
                "Call task.progress with current acceptance criteria, decisions, risks, and next actions."
            )
        elif ledger_state == "stale":
            next_actions.append("Refresh task.progress before handing off or resuming this run.")

    if failure and failure["suggested_action"] not in next_actions:
        next_actions.append(failure["suggested_action"])
    if verification and verification["status"] == "failed":
        action = "Fix the failing verification, then rerun task.verify before final handoff."
        if action not in next_actions:
            next_actions.append(action)

    return {
        "status": status,
        "headline": headline,
        "next_actions": next_actions,
        "blockers": blockers,
        "recent_event_types": recent_event_types,
        "ledger_state": ledger_state,
        "ledger_message": ledger_message,
        "failure": failure,
        "verification": verification,
    }


def _run_checkpoint_config(run: dict[str, Any]) -> dict[str, Any]:
    configurable = {"thread_id": str(run.get("graph_thread_id") or run["id"])}
    checkpoint_id = run.get("graph_checkpoint_id")
    if isinstance(checkpoint_id, str) and checkpoint_id:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


async def _latest_checkpoint_summary(
    checkpointer: Any, run: dict[str, Any]
) -> dict[str, Any] | None:
    if checkpointer is None:
        return None
    run_id = str(run["id"])
    try:
        item = await _run_checkpoint_tuple(checkpointer, run)
        if item is None:
            return None
        checkpoint = item.checkpoint if isinstance(item.checkpoint, dict) else {}
        metadata = item.metadata if isinstance(item.metadata, dict) else {}
        configurable = item.config.get("configurable", {})
        checkpoint_id = _first_string(checkpoint.get("id"), configurable.get("checkpoint_id"))
        if not checkpoint_id:
            return None
        step = _int_or_none(metadata.get("step"))
        reason = _first_string(metadata.get("source"), metadata.get("reason"), "checkpoint")
        return {
            "id": checkpoint_id,
            "run_id": run_id,
            "step": step if step is not None else 0,
            "reason": reason or "checkpoint",
            "messages_count": await _checkpoint_messages_count(checkpointer, item),
            "created_at": _first_string(checkpoint.get("ts"), metadata.get("created_at")),
        }
    except Exception as exc:
        log.warning("latest checkpoint summary failed run_id=%s: %s", run_id, exc)
    return None


async def _latest_checkpoint_reflection(
    checkpointer: Any, run: dict[str, Any]
) -> dict[str, Any] | None:
    if checkpointer is None:
        return None
    run_id = str(run["id"])
    try:
        item = await _run_checkpoint_tuple(checkpointer, run)
        checkpoint = item.checkpoint if item is not None else None
        if not isinstance(checkpoint, dict):
            return None
        channel_values = checkpoint.get("channel_values")
        if not isinstance(channel_values, dict):
            return None
        return _diagnostics_reflection(channel_values.get("reflection"))
    except Exception as exc:
        log.warning("latest checkpoint reflection failed run_id=%s: %s", run_id, exc)
    return None


async def _run_checkpoint_tuple(checkpointer: Any, run: dict[str, Any]) -> Any | None:
    config = _run_checkpoint_config(run)
    if run.get("graph_checkpoint_id") and hasattr(checkpointer, "aget_tuple"):
        return await checkpointer.aget_tuple(config)
    if hasattr(checkpointer, "alist"):
        async for item in checkpointer.alist(config, limit=1):
            return item
    return None


async def _checkpoint_messages_count(checkpointer: Any, item: Any) -> int:
    checkpoint = item.checkpoint if isinstance(item.checkpoint, dict) else {}
    channel_values = checkpoint.get("channel_values")
    if not isinstance(channel_values, dict):
        return 0
    messages = channel_values.get("messages")
    if isinstance(messages, list):
        return len(messages)

    get_history = getattr(checkpointer, "aget_delta_channel_history", None)
    if not callable(get_history) or "messages" not in checkpoint.get("channel_versions", {}):
        return 0
    history = await get_history(config=item.config, channels=["messages"])
    entry = history.get("messages") if isinstance(history, dict) else None
    if not isinstance(entry, dict):
        return 0
    seed = entry.get("seed", [])
    current = getattr(seed, "value", seed)
    if not isinstance(current, list):
        return 0
    for write in entry.get("writes", []):
        if isinstance(write, (list, tuple)) and len(write) == 3:
            current = add_messages(current, write[2])
    return len(current)


def _diagnostics_reflection(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for key in ("ai_messages", "tool_results", "final_answer_chars"):
        parsed = _int_or_none(value.get(key))
        if parsed is not None:
            out[key] = parsed
    critic = _diagnostics_reflection_critic(value.get("critic"))
    if critic:
        out["critic"] = critic
    return out or None


def _diagnostics_reflection_critic(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for key in ("coverage", "clarity", "grounding"):
        parsed = _int_or_none(value.get(key))
        if parsed is not None:
            out[key] = parsed
    notes = value.get("notes")
    if isinstance(notes, list):
        compact_notes = [
            note.strip()[:300] for note in notes[:3] if isinstance(note, str) and note.strip()
        ]
        if compact_notes:
            out["notes"] = compact_notes
    raw = _first_string(value.get("raw"))
    if raw:
        out["raw"] = raw[:1000]
    return out or None


def _latest_failure_classification(
    events: list[dict[str, Any]],
    *,
    run_status: str | None = None,
    verification: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if run_status == "completed" and (not verification or verification.get("status") != "failed"):
        return None
    for event in reversed(events):
        event_type = event.get("event_type")
        if event_type not in {"run.failed", "tool.failed"}:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        if (
            event_type == "tool.failed"
            and _is_task_verify_payload(payload)
            and verification
            and verification.get("status") == "passed"
        ):
            continue
        return classify_failure_payload(str(event_type), payload)
    return None


def _latest_task_verification(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        event_type = event.get("event_type")
        if event_type not in {"tool.completed", "tool.failed"}:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or not _is_task_verify_payload(payload):
            continue
        parsed = _parse_tool_content(payload.get("content"))
        if not isinstance(parsed, dict):
            parsed = {}
        status = (
            "passed" if event_type == "tool.completed" and _truthy(parsed.get("ok")) else "failed"
        )
        return {
            "status": status,
            "reason": _verification_reason(parsed),
            "pass_count": _int_or_none(parsed.get("pass_count")),
            "fail_count": _int_or_none(parsed.get("fail_count")),
            "source_event_type": str(event_type),
        }
    return None


def _is_task_verify_payload(payload: dict[str, Any]) -> bool:
    return str(payload.get("tool") or payload.get("name") or "") == "task.verify"


def _parse_tool_content(content: Any) -> Any:
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return None
    return None


def _verification_reason(payload: dict[str, Any]) -> str | None:
    results = payload.get("results")
    if isinstance(results, list):
        failed_details: list[str] = []
        passed_details: list[str] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            detail = item.get("detail")
            if not isinstance(detail, str) or not detail.strip():
                continue
            if _truthy(item.get("ok")):
                passed_details.append(detail.strip())
            else:
                failed_details.append(detail.strip())
        if failed_details:
            return failed_details[0]
        if passed_details:
            return passed_details[0]
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        return error.strip()
    return None


def _failure_blocker(failure: dict[str, Any]) -> str:
    code = failure.get("code")
    label = f"{failure.get('category')}: {code}" if code else str(failure.get("category"))
    tool = failure.get("tool")
    if tool:
        return f"{tool}: {label}"
    return label


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "ok", "passed"}
    if isinstance(value, (int, float)):
        return value != 0
    return bool(value)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


app = create_app()
