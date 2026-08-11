"""Runtime-owned defaults for model profiles with published limits."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

_DEEPSEEK_V4_LIMITS = {
    "deepseek-v4-flash": (1_000_000, 384_000),
    "deepseek-v4-pro": (1_000_000, 384_000),
}

_DEFAULT_REASONING_PROFILE = {
    "supported": False,
    "modes": ["off"],
    "default_mode": "off",
    "stream_field": None,
    "tool_roundtrip_required": False,
    "display_policy": "activity_only",
}

_DEEPSEEK_REASONING_PROFILE = {
    "supported": True,
    "modes": ["off", "high", "max"],
    "default_mode": "off",
    "stream_field": "reasoning_content",
    "tool_roundtrip_required": True,
    "display_policy": "activity_only",
}

_REASONING_MODES = {"off", "high", "max"}
_OPENAI_WEB_SEARCH_MODELS = {
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-5.6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "o4-mini",
}
_OPENAI_SNAPSHOT_SUFFIX = re.compile(r"-20\d{2}-\d{2}-\d{2}$")

MODEL_CAPABILITY_ORDER = {
    "agent_chat": 0,
    "image_understanding": 1,
    "image_generation": 2,
    "image_editing": 3,
}


def default_model_protocol(adapter_id: str, capability: str) -> str:
    if capability == "image_generation":
        return "openai_images_generations"
    if capability == "image_editing":
        return "openai_images_edits"
    if adapter_id == "anthropic_messages":
        return "anthropic_messages"
    if adapter_id == "google_genai":
        return "google_generate_content"
    return "openai_chat_completions"


def normalized_model_capabilities(
    model: dict[str, Any],
    *,
    adapter_id: str,
) -> list[dict[str, str]]:
    """Normalize multi-capability profiles and migrate earlier single-purpose rows."""
    raw_capabilities = model.get("capabilities")
    capabilities: dict[str, dict[str, str]] = {}
    if isinstance(raw_capabilities, list):
        for raw in raw_capabilities:
            if not isinstance(raw, dict):
                continue
            capability = str(raw.get("capability") or "")
            protocol = str(raw.get("protocol") or "")
            if capability not in MODEL_CAPABILITY_ORDER or not protocol:
                continue
            capabilities[capability] = {
                "capability": capability,
                "protocol": protocol,
                "verification": (
                    "verified" if raw.get("verification") == "verified" else "unverified"
                ),
            }

    legacy_purpose = str(model.get("purpose") or "")
    if legacy_purpose in MODEL_CAPABILITY_ORDER and legacy_purpose not in capabilities:
        capabilities[legacy_purpose] = {
            "capability": legacy_purpose,
            "protocol": str(
                model.get("protocol") or default_model_protocol(adapter_id, legacy_purpose)
            ),
            "verification": (
                "verified" if model.get("verification") == "verified" else "unverified"
            ),
        }

    if not capabilities and (
        model.get("source") == "bundled" or model.get("verification") == "verified"
    ):
        verification = "verified" if model.get("verification") == "verified" else "unverified"
        capabilities["agent_chat"] = {
            "capability": "agent_chat",
            "protocol": default_model_protocol(adapter_id, "agent_chat"),
            "verification": verification,
        }
        if bool(model.get("image_inputs")):
            capabilities["image_understanding"] = {
                "capability": "image_understanding",
                "protocol": default_model_protocol(adapter_id, "image_understanding"),
                "verification": verification,
            }
    return sorted(
        capabilities.values(),
        key=lambda item: MODEL_CAPABILITY_ORDER[item["capability"]],
    )


def model_capability(model: dict[str, Any], capability: str) -> dict[str, str] | None:
    return next(
        (
            item
            for item in model.get("capabilities", [])
            if isinstance(item, dict) and item.get("capability") == capability
        ),
        None,
    )


def _bounded_integer(value: Any, *, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if minimum <= value <= maximum else None


def _normalized_reasoning_profile(
    value: Any,
    *,
    provider_family: str,
    trusted: bool,
) -> dict[str, Any]:
    fallback = (
        _DEEPSEEK_REASONING_PROFILE if provider_family == "deepseek" else _DEFAULT_REASONING_PROFILE
    )
    if provider_family != "deepseek" or not trusted or not isinstance(value, dict):
        return dict(fallback)
    supported = value.get("supported")
    modes = value.get("modes")
    default_mode = value.get("default_mode")
    stream_field = value.get("stream_field")
    tool_roundtrip_required = value.get("tool_roundtrip_required")
    display_policy = value.get("display_policy")
    if (
        not isinstance(supported, bool)
        or not isinstance(modes, list)
        or not 1 <= len(modes) <= 3
        or any(not isinstance(mode, str) or mode not in _REASONING_MODES for mode in modes)
        or len(modes) != len(set(modes))
        or default_mode not in modes
        or stream_field not in {None, "reasoning_content", "content_blocks"}
        or not isinstance(tool_roundtrip_required, bool)
        or display_policy not in {"activity_only", "summary_only"}
        or (not supported and modes != ["off"])
    ):
        return dict(fallback)
    return {
        "supported": supported,
        "modes": list(modes),
        "default_mode": default_mode,
        "stream_field": stream_field,
        "tool_roundtrip_required": tool_roundtrip_required,
        "display_policy": display_policy,
    }


def _openai_model_supports_web_search(model_id: str) -> bool:
    return _OPENAI_SNAPSHOT_SUFFIX.sub("", model_id) in _OPENAI_WEB_SEARCH_MODELS


def _normalized_hosted_web_search(
    value: Any,
    *,
    model_id: str,
    hostname: str | None,
    trusted_model_catalog: bool,
) -> dict[str, Any] | None:
    if hostname == "api.openai.com" and _openai_model_supports_web_search(model_id):
        return {"verification": "verified", "full_sources": True}
    if hostname == "api.deepseek.com" and model_id == "deepseek-v4-flash":
        return {"verification": "verified", "full_sources": False}
    if not trusted_model_catalog or not isinstance(value, dict):
        return None
    if value.get("verification") not in {"verified", "unverified"} or not isinstance(
        value.get("full_sources"), bool
    ):
        return None
    return {
        "verification": value["verification"],
        "full_sources": value["full_sources"],
    }


def apply_known_model_profile_defaults(
    profile: dict[str, Any],
    *,
    service_base_url: str,
    trusted_model_catalog: bool = False,
    trusted_hosted_web_search: bool = False,
) -> dict[str, Any]:
    """Fill published limits and repair stale trusted-catalog agent flags."""
    normalized = dict(profile)
    hostname = urlparse(service_base_url).hostname
    model_id = str(normalized.get("model_id") or "")
    claimed_provider_family = str(normalized.get("provider_family") or "").strip().lower()
    provider_family = (
        claimed_provider_family
        if trusted_model_catalog
        and claimed_provider_family in {"openai", "deepseek", "anthropic", "google"}
        else "unknown"
    )
    if hostname == "api.deepseek.com" or (
        trusted_model_catalog and str(normalized.get("model_id") or "").startswith("deepseek-")
    ):
        provider_family = "deepseek"
    normalized["provider_family"] = provider_family
    normalized["reasoning"] = _normalized_reasoning_profile(
        normalized.get("reasoning"),
        provider_family=provider_family,
        trusted=trusted_model_catalog or hostname == "api.deepseek.com",
    )
    normalized["hosted_web_search"] = _normalized_hosted_web_search(
        normalized.get("hosted_web_search"),
        model_id=model_id,
        hostname=hostname,
        trusted_model_catalog=trusted_hosted_web_search,
    )
    if normalized["provider_family"] != "deepseek" and not trusted_model_catalog:
        return normalized
    limits = _DEEPSEEK_V4_LIMITS.get(model_id)
    if limits is None:
        return normalized
    max_input_tokens, max_output_tokens = limits
    if normalized.get("max_input_tokens") is None:
        normalized["max_input_tokens"] = max_input_tokens
    if normalized.get("max_output_tokens") is None:
        normalized["max_output_tokens"] = max_output_tokens
    if trusted_model_catalog:
        normalized["tool_calling"] = True
        normalized["streaming"] = True
    return normalized


def discovered_model_profile(
    candidate: dict[str, Any],
    *,
    model_id: str,
    display_name: str,
    service_base_url: str,
    catalog_model: dict[str, Any] | None = None,
    trusted_model_catalog: bool = False,
    trusted_hosted_web_search: bool = False,
) -> dict[str, Any]:
    """Normalize optional capability metadata exposed by model-list APIs."""
    architecture = candidate.get("architecture")
    input_modalities = (
        architecture.get("input_modalities") if isinstance(architecture, dict) else None
    )
    supported_parameters = candidate.get("supported_parameters")
    top_provider = candidate.get("top_provider")

    profile: dict[str, Any] = {
        "model_id": model_id,
        "display_name": display_name,
        "capabilities": [],
        "tool_calling": False,
        "streaming": False,
        "image_inputs": False,
        "max_input_tokens": None,
        "max_output_tokens": None,
        "provider_family": str(candidate.get("provider_family") or "unknown"),
        "reasoning": (
            dict(candidate["reasoning"]) if isinstance(candidate.get("reasoning"), dict) else None
        ),
        "hosted_web_search": (
            dict(candidate["hosted_web_search"])
            if isinstance(candidate.get("hosted_web_search"), dict)
            else None
        ),
    }
    if catalog_model:
        modalities = catalog_model.get("modalities")
        catalog_inputs = modalities.get("input") if isinstance(modalities, dict) else None
        limits = catalog_model.get("limit")
        if isinstance(catalog_model.get("tool_call"), bool):
            profile["tool_calling"] = catalog_model["tool_call"]
        if isinstance(catalog_inputs, list):
            profile["image_inputs"] = "image" in catalog_inputs
        if isinstance(limits, dict):
            profile["max_input_tokens"] = _bounded_integer(
                limits.get("input", limits.get("context")),
                minimum=1,
                maximum=10_000_000,
            )
            profile["max_output_tokens"] = _bounded_integer(
                limits.get("output"),
                minimum=128,
                maximum=1_000_000,
            )
    if isinstance(candidate.get("tool_calling"), bool):
        profile["tool_calling"] = candidate["tool_calling"]
    elif isinstance(supported_parameters, list):
        profile["tool_calling"] = "tools" in supported_parameters
    if isinstance(candidate.get("streaming"), bool):
        profile["streaming"] = candidate["streaming"]
    if isinstance(candidate.get("image_inputs"), bool):
        profile["image_inputs"] = candidate["image_inputs"]
    elif isinstance(input_modalities, list):
        profile["image_inputs"] = "image" in input_modalities

    raw_max_input = candidate.get("max_input_tokens", candidate.get("context_length"))
    if raw_max_input is not None:
        profile["max_input_tokens"] = _bounded_integer(
            raw_max_input,
            minimum=1,
            maximum=10_000_000,
        )
    raw_max_output = candidate.get("max_output_tokens")
    if raw_max_output is None and isinstance(top_provider, dict):
        raw_max_output = top_provider.get("max_completion_tokens")
    if raw_max_output is not None:
        profile["max_output_tokens"] = _bounded_integer(
            raw_max_output,
            minimum=128,
            maximum=1_000_000,
        )
    return apply_known_model_profile_defaults(
        profile,
        service_base_url=service_base_url,
        trusted_model_catalog=trusted_model_catalog,
        trusted_hosted_web_search=trusted_hosted_web_search,
    )
