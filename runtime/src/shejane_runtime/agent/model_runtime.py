from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import httpx
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from PIL import Image, UnidentifiedImageError

from ..config import Settings
from ..middleware.outbound_policy import sanitize_outbound_text
from ..model_credentials import CredentialStoreError, get_model_api_key
from ..model_profiles import apply_known_model_profile_defaults
from ..plugins.tools import PluginActionError
from ..store.sqlite import LocalStore

log = logging.getLogger("shejane_runtime.agent.model_runtime")

_VISION_MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024
_VISION_MAX_IMAGE_PIXELS = 40_000_000
_VISION_MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


class RuntimeModelMiddleware(AgentMiddleware):
    """Select the model connection owned by this invocation."""

    @staticmethod
    def _request_with_model(request: Any) -> Any:
        context = getattr(getattr(request, "runtime", None), "context", None)
        model = getattr(context, "model", None)
        return request.override(model=model) if model is not None else request

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return handler(self._request_with_model(request))

    async def awrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        return await handler(self._request_with_model(request))


def _build_chat_model(
    settings: Settings,
    run_id: str,
    mode: str,
    *,
    model_binding: dict[str, Any] | None = None,
    model_api_key: str | None = None,
) -> Any:
    """Build the selected BYOK model, or the deterministic test model."""
    if settings.fake_llm:
        from ..llm.fake import FakeBackendChatModel

        return FakeBackendChatModel(
            profile={
                "max_input_tokens": settings.unknown_model_max_input_tokens,
                "max_output_tokens": settings.unknown_model_max_output_tokens,
            }
        )
    return _build_byok_chat_model(
        settings=settings,
        model_binding=model_binding,
        model_api_key=model_api_key,
    )


def _build_byok_chat_model(
    *,
    settings: Settings,
    model_binding: dict[str, Any] | None,
    model_api_key: str | None,
) -> Any:
    if not model_binding or model_binding.get("adapter_id") not in {
        "openai_chat",
        "anthropic_messages",
        "google_genai",
    }:
        raise RuntimeError("Runtime BYOK model binding is required")
    raw_profile = model_binding.get("profile")
    profile = (
        {
            key: raw_profile[key]
            for key in (
                "tool_calling",
                "image_inputs",
                "max_input_tokens",
                "max_output_tokens",
            )
            if key in raw_profile and raw_profile[key] is not None
        }
        if isinstance(raw_profile, dict)
        else {}
    )
    profile.setdefault("max_input_tokens", settings.unknown_model_max_input_tokens)
    profile.setdefault("max_output_tokens", settings.unknown_model_max_output_tokens)
    profile.setdefault("image_inputs", False)
    profile["image_tool_message"] = profile["image_inputs"]
    if model_binding["adapter_id"] == "anthropic_messages":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=str(model_binding["model_id"]),
            base_url=str(model_binding["base_url"]),
            api_key=model_api_key or "local",
            streaming=True,
            stream_usage=True,
            max_retries=0,
            max_tokens=int(profile["max_output_tokens"]),
            timeout=settings.model_request_timeout_seconds,
            profile=profile,
        )
    if model_binding["adapter_id"] == "google_genai":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=str(model_binding["model_id"]),
            api_key=model_api_key or "local",
            client_options=str(model_binding["base_url"]),
            streaming=True,
            retries=0,
            max_tokens=int(profile["max_output_tokens"]),
            request_timeout=settings.model_request_timeout_seconds,
            output_version="v1",
            profile=profile,
        )

    from langchain_openai import ChatOpenAI

    base_url = str(model_binding["base_url"])
    extra_body = (
        {"tool_stream": True}
        if urlparse(base_url).hostname in {"open.bigmodel.cn", "api.z.ai"}
        else None
    )
    responses = model_binding.get("protocol") == "openai_responses"
    return ChatOpenAI(
        model=str(model_binding["model_id"]),
        base_url=base_url,
        api_key=model_api_key or "local",
        http_client=httpx.Client(),
        http_async_client=httpx.AsyncClient(),
        streaming=True,
        stream_usage=True,
        max_retries=0,
        max_tokens=int(profile["max_output_tokens"]),
        timeout=settings.model_request_timeout_seconds,
        profile=profile,
        extra_body=extra_body,
        **(
            {
                "use_responses_api": True,
                "use_previous_response_id": False,
                "output_version": "v1",
            }
            if responses
            else {}
        ),
    )


async def _invoke_plugin_vision(
    model_binding: Mapping[str, Any],
    params: dict[str, Any],
    input_root: Path,
    inputs: tuple[dict[str, Any], ...],
    *,
    store: LocalStore,
    principal_id: str,
    settings: Settings,
) -> dict[str, Any]:
    connection_id = str(model_binding["connection_id"])
    connection = await store.get_model_connection(
        principal_id=principal_id,
        connection_id=connection_id,
    )
    try:
        models = json.loads(connection.get("models_json") or "[]") if connection else []
    except (json.JSONDecodeError, TypeError):
        models = []
    current_profile = next(
        (
            item
            for item in models
            if isinstance(item, dict) and item.get("model_id") == model_binding["model_id"]
        ),
        None,
    )
    if isinstance(current_profile, dict):
        current_profile = apply_known_model_profile_defaults(
            current_profile,
            service_base_url=str(connection.get("base_url") or "") if connection else "",
            trusted_model_catalog=bool(
                connection and connection.get("preset_id") == "shejane-official"
            ),
        )
    if (
        connection is None
        or int(connection.get("version") or 1) != int(model_binding["connection_version"])
        or str(connection.get("adapter_id")) != str(model_binding["adapter_id"])
        or str(connection.get("base_url")) != str(model_binding["base_url"])
        or str(connection.get("credential_ref")) != str(model_binding["credential_ref"])
        or current_profile != dict(model_binding["profile"])
        or not bool(current_profile.get("image_inputs"))
    ):
        raise PluginActionError(
            "model_binding_unavailable",
            "configured Vision model binding changed or is unavailable",
        )
    try:
        api_key = await get_model_api_key(
            principal_id,
            connection_id,
            str(model_binding["credential_ref"]),
        )
    except CredentialStoreError as exc:
        raise PluginActionError(
            "model_credential_store_unavailable",
            "Vision model credential store is unavailable",
        ) from exc
    if not api_key:
        raise PluginActionError(
            "model_binding_unavailable",
            "configured Vision model credential is unavailable",
        )

    blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": sanitize_outbound_text(
                params["prompt"],
                secrets=(api_key,) if api_key else (),
                pii_types=_outbound_pii_types(settings.pii_redact_types),
                external=_outbound_is_external(settings, dict(model_binding)),
            ),
        }
    ]
    references = {str(item["id"]): item for item in inputs}
    total_bytes = 0
    input_root = input_root.resolve(strict=True)
    for input_id in params["input_ids"]:
        reference = references[input_id]
        media_type = str(reference["media_type"])
        if media_type not in _VISION_MEDIA_TYPES:
            raise PluginActionError("invalid_invocation", "Vision input media type is unsupported")
        try:
            relative = PurePosixPath(str(reference["path"])).relative_to("/input")
        except ValueError as exc:
            raise PluginActionError("invalid_invocation", "Vision input path is invalid") from exc
        candidate = input_root.joinpath(*relative.parts)
        try:
            candidate.resolve(strict=True).relative_to(input_root)
        except (FileNotFoundError, ValueError) as exc:
            raise PluginActionError("invalid_invocation", "Vision input is unavailable") from exc
        body = candidate.read_bytes()
        total_bytes += len(body)
        if (
            len(body) != int(reference["size_bytes"])
            or hashlib.sha256(body).hexdigest() != reference["sha256"]
            or total_bytes > _VISION_MAX_TOTAL_IMAGE_BYTES
        ):
            raise PluginActionError("resource_exhausted", "Vision input byte limit exceeded")
        try:
            with Image.open(candidate) as image:
                if (
                    image.width <= 0
                    or image.height <= 0
                    or image.width * image.height > _VISION_MAX_IMAGE_PIXELS
                    or int(getattr(image, "n_frames", 1)) != 1
                ):
                    raise PluginActionError(
                        "resource_exhausted",
                        "Vision image dimensions or frame count are unsupported",
                    )
                image.verify()
        except (Image.DecompressionBombError, UnidentifiedImageError, OSError) as exc:
            raise PluginActionError("invalid_invocation", "Vision input image is invalid") from exc
        encoded = base64.b64encode(body).decode("ascii")
        if model_binding["adapter_id"] == "anthropic_messages":
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": encoded,
                    },
                }
            )
        else:
            image_url: dict[str, Any] = {"url": f"data:{media_type};base64,{encoded}"}
            if params.get("detail") is not None:
                image_url["detail"] = params["detail"]
            blocks.append({"type": "image_url", "image_url": image_url})

    model = _build_chat_model(
        settings,
        "plugin-vision",
        "vision",
        model_binding=dict(model_binding),
        model_api_key=api_key,
    ).bind(
        max_tokens=int(params["max_output_tokens"]),
        temperature=float(params.get("temperature", 0)),
    )
    try:
        response = await model.ainvoke([HumanMessage(content=blocks)])
    except Exception as exc:
        log.warning(
            "plugin vision model request failed connection=%s model=%s error=%s",
            connection_id,
            model_binding["model_id"],
            type(exc).__name__,
        )
        raise PluginActionError(
            "vision_model_service_failed",
            "configured Vision model service request failed",
        ) from exc
    content = response.content
    text = (
        content
        if isinstance(content, str)
        else "".join(
            str(item["text"])
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    )
    if not text or len(text) > 262_144:
        raise PluginActionError(
            "vision_model_service_failed",
            "Vision model service returned invalid text",
        )
    raw_usage = getattr(response, "usage_metadata", None)
    usage = {
        key: int(value)
        for key, value in (raw_usage.items() if isinstance(raw_usage, dict) else ())
        if key in {"input_tokens", "output_tokens", "total_tokens"}
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    }
    return {
        "text": text,
        "model": {
            "connection_id": connection_id,
            "connection_version": int(model_binding["connection_version"]),
            "model_id": str(model_binding["model_id"]),
        },
        "usage": usage,
    }


def _outbound_is_external(
    settings: Settings,
    model_binding: dict[str, Any] | None,
) -> bool:
    if settings.fake_llm or (model_binding or {}).get("provider") == "fake":
        return False
    if model_binding is None:
        return True
    raw_url = str((model_binding or {}).get("base_url") or "")
    hostname = (urlparse(raw_url).hostname or "").strip().lower()
    if hostname == "localhost":
        return False
    try:
        return not ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return True


def _outbound_pii_types(spec: str) -> tuple[str, ...]:
    valid = {"email", "credit_card", "ip", "mac_address", "url"}
    return tuple(
        dict.fromkeys(
            item for item in (part.strip().lower() for part in spec.split(",")) if item in valid
        )
    )


def _register_model_cleanup(model: Any, stack: AsyncExitStack | None) -> None:
    """Close provider clients when the owning execution attempt ends."""
    if stack is None:
        return
    seen: set[int] = set()
    for name in ("root_async_client", "_async_client"):
        client = getattr(model, name, None)
        close = getattr(client, "close", None)
        if callable(close) and id(client) not in seen:
            seen.add(id(client))
            stack.push_async_callback(close)
    for name in ("root_client", "_client"):
        client = getattr(model, name, None)
        close = getattr(client, "close", None)
        if callable(close) and id(client) not in seen:
            seen.add(id(client))
            stack.callback(close)
