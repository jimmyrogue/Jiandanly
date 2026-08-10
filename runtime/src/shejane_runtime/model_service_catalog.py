from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

from .api_schemas import ModelServiceConnection
from .model_credentials import CredentialStoreError, get_model_api_key
from .model_profiles import (
    MODEL_CAPABILITY_ORDER,
    apply_known_model_profile_defaults,
    default_model_protocol,
    discovered_model_profile,
    model_capability,
    normalized_model_capabilities,
)
from .model_services import openai_compatible_endpoint
from .store.sqlite import LocalStore


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
            trusted_model_catalog=row.get("preset_id") == "shejane-official",
        )
        normalized["capabilities"] = normalized_model_capabilities(
            normalized,
            adapter_id=str(row.get("adapter_id") or "openai_chat"),
        )
        if not normalized["capabilities"] and normalized.get("source") == "discovered":
            normalized["capabilities"] = [
                {
                    "capability": "agent_chat",
                    "protocol": default_model_protocol(
                        str(row.get("adapter_id") or "openai_chat"),
                        "agent_chat",
                    ),
                    "verification": "unverified",
                }
            ]
        if row.get("preset_id") == "shejane-official":
            for capability in normalized["capabilities"]:
                if capability["capability"] != "agent_chat":
                    capability["verification"] = "verified"
        raw_recommended_for = normalized.get("recommended_for")
        recommended_for: list[str] = []
        if isinstance(raw_recommended_for, list):
            for capability in raw_recommended_for:
                if (
                    capability in MODEL_CAPABILITY_ORDER
                    and capability not in recommended_for
                    and model_capability(normalized, capability) is not None
                ):
                    recommended_for.append(capability)
        if (
            not recommended_for
            and normalized.get("recommended")
            and model_capability(normalized, "agent_chat") is not None
        ):
            recommended_for = ["agent_chat"]
        normalized["recommended_for"] = recommended_for
        normalized["recommended"] = bool(recommended_for)
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
            capabilities = {
                str(item["capability"]): dict(item)
                for item in model.get("capabilities", [])
                if isinstance(item, dict) and item.get("capability")
            }
            for item in previous.get("capabilities", []):
                if not isinstance(item, dict) or not item.get("capability"):
                    continue
                capability = str(item["capability"])
                if capability not in capabilities or item.get("verification") == "verified":
                    capabilities[capability] = dict(item)
            merged_capabilities = sorted(
                capabilities.values(),
                key=lambda item: MODEL_CAPABILITY_ORDER[str(item["capability"])],
            )
            model = {
                **model,
                "capabilities": merged_capabilities,
                "verification": (
                    "verified"
                    if any(item.get("verification") == "verified" for item in merged_capabilities)
                    else "unverified"
                ),
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


async def _ensure_default_model_capability_binding(
    store: LocalStore,
    *,
    principal_id: str,
    capability_name: str,
) -> None:
    if (
        await store.get_model_capability_binding(
            principal_id=principal_id,
            capability=capability_name,
        )
        is not None
    ):
        return
    candidates = []
    for connection in await store.list_model_connections(principal_id=principal_id):
        for model in _model_connection_models(connection):
            capability = model_capability(model, capability_name)
            if capability is not None and capability.get("verification") == "verified":
                candidates.append((connection, model, capability))
    if not candidates:
        return
    connection, model, capability = max(
        candidates,
        key=lambda candidate: (
            candidate[0].get("preset_id") == "shejane-official",
            capability_name in candidate[1].get("recommended_for", []),
        ),
    )
    await store.create_model_capability_binding_if_absent(
        principal_id=principal_id,
        capability=capability_name,
        connection_id=str(connection["id"]),
        connection_version=int(connection.get("version") or 1),
        model_id=str(model["model_id"]),
        protocol=str(capability["protocol"]),
    )


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
            trusted_model_catalog=preset.get("id") == "shejane-official",
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
                                "verification": (
                                    "unverified" if capability == "agent_chat" else "verified"
                                ),
                            }
                            for capability in declared_capabilities
                            if isinstance(capability, str) and capability in MODEL_CAPABILITY_ORDER
                        ]
                    },
                    adapter_id=adapter_id,
                )
                if "agent_chat" in declared_capabilities:
                    profile["tool_calling"] = True
                    profile["streaming"] = True
            recommended_for = candidate.get("recommended_for")
            if isinstance(recommended_for, list):
                profile["recommended_for"] = []
                for capability in recommended_for:
                    if (
                        isinstance(capability, str)
                        and capability not in profile["recommended_for"]
                        and model_capability(profile, capability) is not None
                    ):
                        profile["recommended_for"].append(capability)
                profile["recommended"] = bool(profile["recommended_for"])
        models.append(profile)
        if len(models) >= 1000:
            break
    for model in bundled:
        if model["model_id"] not in seen:
            models.append(model)
    return models, "ready"
