"""Runtime-owned image generation/editing tools backed by configured model capabilities."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import tempfile
from pathlib import Path
from typing import Any

import httpx
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool
from PIL import Image, UnidentifiedImageError

from ..config import get_settings
from ..model_credentials import CredentialStoreError, get_model_api_key
from ..model_profiles import model_capability, normalized_model_capabilities
from ..model_services import openai_compatible_endpoint
from ..store.sqlite import ArtifactConflictError, ArtifactQuotaError, LocalStore
from .web import _pinned_transport

log = logging.getLogger("shejane_runtime.tools.image")

_MAX_IMAGES = 4
_MAX_IMAGE_BYTES = 64 * 1024 * 1024
_CAPABILITY_PROTOCOLS = {
    "image_generation": "openai_images_generations",
    "image_editing": "openai_images_edits",
}
_FORMAT_MEDIA_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_PROVIDER_STATUS_ERRORS = {
    400: ("image_provider_invalid_request", "image provider rejected the request (400)", False),
    401: ("image_provider_unauthorized", "image provider rejected the API key", False),
    402: ("provider_quota_exceeded", "image provider quota or balance is insufficient", False),
    403: ("image_provider_permission_denied", "image provider denied access to the model", False),
    408: ("image_provider_unavailable", "image provider unavailable (408)", True),
    429: ("image_provider_rate_limited", "image provider rate limit (429)", True),
}


class ImageToolError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.request_id = request_id


def make_image_tools() -> list[BaseTool]:
    @tool("image.generate")
    async def image_generate(
        prompt: str,
        runtime: ToolRuntime[Any] = None,  # type: ignore[assignment]
        n: int = 1,
        size: str | None = None,
        quality: str | None = None,
        background: str | None = None,
        source_attachment_path: str | None = None,
    ) -> dict[str, Any]:
        """Generate images, optionally using one uploaded `/attachments/...` image as reference."""
        return await _run_image_tool(
            capability="image_generation",
            prompt=prompt,
            runtime=runtime,
            n=n,
            options={"size": size, "quality": quality, "background": background},
            source_attachment_path=source_attachment_path,
        )

    @tool("image.edit")
    async def image_edit(
        prompt: str,
        runtime: ToolRuntime[Any] = None,  # type: ignore[assignment]
        n: int = 1,
        size: str | None = None,
        source_artifact_id: str | None = None,
        source_attachment_path: str | None = None,
    ) -> dict[str, Any]:
        """Edit one image Artifact or uploaded `/attachments/...` image."""
        return await _run_image_tool(
            capability="image_editing",
            prompt=prompt,
            runtime=runtime,
            n=n,
            options={"size": size},
            source_artifact_id=source_artifact_id,
            source_attachment_path=source_attachment_path,
        )

    return [image_generate, image_edit]


async def _run_image_tool(
    *,
    capability: str,
    prompt: str,
    runtime: ToolRuntime[Any] | Any,
    n: int,
    options: dict[str, str | None],
    source_artifact_id: str | None = None,
    source_attachment_path: str | None = None,
) -> dict[str, Any]:
    context = getattr(runtime, "context", None)
    store = getattr(context, "store", None)
    run_id = getattr(context, "run_id", None)
    principal_id = getattr(context, "principal_id", None)
    if not isinstance(store, LocalStore) or not run_id or not principal_id:
        return _failure("image_tool_unbound", "image tool is not bound to a Runtime run")
    prompt = prompt.strip()
    if not prompt:
        return _failure("invalid_image_request", "prompt is required")
    count = max(1, min(_MAX_IMAGES, int(n)))
    try:
        binding, api_key = await _active_binding(context, capability)
        payload = {"model": binding["model_id"], "prompt": prompt, "n": count}
        payload.update({key: value for key, value in options.items() if value})
        source_request: tuple[Path, str, str] | None = None
        if capability == "image_editing":
            if bool(source_artifact_id) == bool(source_attachment_path):
                raise ImageToolError(
                    "image_source_required",
                    "provide exactly one source_artifact_id or source_attachment_path",
                )
            if source_attachment_path:
                source_request = _bound_image_attachment(
                    context,
                    source_attachment_path,
                )
            else:
                source = await _owned_image_artifact(
                    store,
                    principal_id=str(principal_id),
                    artifact_id=str(source_artifact_id or ""),
                )
                source_path = store.artifact_body_path(source)
                source_request = (
                    source_path,
                    str(source.get("title") or source_path.name),
                    str(source.get("content_type") or "application/octet-stream"),
                )
        elif source_attachment_path:
            source_request = _bound_image_attachment(
                context,
                source_attachment_path,
            )
        if source_request is not None:
            source_path, source_name, source_media_type = source_request
            results = await _request_edit(
                binding,
                api_key,
                payload,
                source_path,
                source_name=source_name,
                source_media_type=source_media_type,
            )
        else:
            results = await _request_generation(binding, api_key, payload)
        artifacts = await _persist_results(
            store=store,
            run_id=str(run_id),
            tool_call_id=getattr(runtime, "tool_call_id", None),
            tool_name="image.edit" if capability == "image_editing" else "image.generate",
            binding=binding,
            results=results,
        )
        return {
            "ok": "true",
            "artifacts": artifacts,
            "model": {
                "connection_id": binding["connection_id"],
                "model_id": binding["model_id"],
                "protocol": binding["protocol"],
            },
        }
    except ImageToolError as exc:
        return _failure(
            exc.code,
            str(exc),
            retryable=exc.retryable,
            request_id=exc.request_id,
        )
    except (ArtifactConflictError, ArtifactQuotaError) as exc:
        return _failure("image_artifact_failed", str(exc))
    except Exception:
        log.exception("image tool failed")
        return _failure("image_tool_failed", "image tool failed unexpectedly", retryable=True)


async def _active_binding(context: Any, capability: str) -> tuple[dict[str, Any], str]:
    bindings = getattr(context, "capability_bindings", None)
    binding = bindings.get(capability) if isinstance(bindings, dict) else None
    if not isinstance(binding, dict):
        raise ImageToolError("image_model_unconfigured", f"{capability} model is not configured")
    expected_protocol = _CAPABILITY_PROTOCOLS[capability]
    if binding.get("protocol") != expected_protocol:
        raise ImageToolError("image_model_stale", "image model binding protocol changed")
    store = context.store
    principal_id = str(context.principal_id)
    connection_id = str(binding.get("connection_id") or "")
    connection = await store.get_model_connection(
        principal_id=principal_id,
        connection_id=connection_id,
    )
    if (
        connection is None
        or int(connection.get("version") or 0) != int(binding.get("connection_version") or -1)
        or connection.get("credential_ref") != binding.get("credential_ref")
        or connection.get("base_url") != binding.get("base_url")
    ):
        raise ImageToolError("image_model_stale", "image model service changed after Run admission")
    try:
        models = json.loads(connection.get("models_json") or "[]")
    except (json.JSONDecodeError, TypeError) as exc:
        raise ImageToolError("image_model_stale", "image model catalog is invalid") from exc
    profile = next(
        (
            item
            for item in models
            if isinstance(item, dict) and item.get("model_id") == binding.get("model_id")
        ),
        None,
    )
    if profile is None:
        raise ImageToolError("image_model_stale", "image model is no longer available")
    profile["capabilities"] = normalized_model_capabilities(
        profile,
        adapter_id=str(connection.get("adapter_id") or "openai_chat"),
    )
    current = model_capability(profile, capability)
    if (
        current is None
        or current.get("verification") != "verified"
        or current.get("protocol") != expected_protocol
    ):
        raise ImageToolError("image_model_stale", "image model capability is no longer verified")
    try:
        api_key = await get_model_api_key(
            principal_id,
            connection_id,
            str(binding["credential_ref"]),
        )
    except CredentialStoreError as exc:
        raise ImageToolError("image_credential_unavailable", str(exc)) from exc
    if not api_key:
        raise ImageToolError("image_credential_unavailable", "image model API key is unavailable")
    return binding, api_key


async def _owned_image_artifact(
    store: LocalStore,
    *,
    principal_id: str,
    artifact_id: str,
) -> dict[str, Any]:
    if not artifact_id:
        raise ImageToolError("image_source_required", "source_artifact_id is required")
    artifact = await store.get_artifact(artifact_id)
    run = await store.get_run(str((artifact or {}).get("run_id") or ""))
    if artifact is None or run is None or run.get("principal_id") != principal_id:
        raise ImageToolError("image_source_not_found", "source image Artifact was not found")
    if not str(artifact.get("content_type") or "").startswith("image/"):
        raise ImageToolError("image_source_invalid", "source Artifact is not an image")
    if artifact.get("storage_kind") != "blob":
        raise ImageToolError("image_source_invalid", "source image Artifact has no file body")
    return artifact


def _bound_image_attachment(context: Any, virtual_path: str) -> tuple[Path, str, str]:
    inputs = getattr(context, "plugin_inputs", ())
    source = next(
        (
            item
            for item in inputs
            if isinstance(item, dict) and item.get("virtual_path") == virtual_path
        ),
        None,
    )
    if source is None:
        raise ImageToolError("image_source_not_found", "source image attachment was not found")
    media_type = str(source.get("media_type") or "")
    source_path = source.get("source_path")
    if not media_type.startswith("image/") or not isinstance(source_path, str):
        raise ImageToolError("image_source_invalid", "source attachment is not an image")
    path = Path(source_path)
    if not path.is_file():
        raise ImageToolError("image_source_invalid", "source image attachment could not be read")
    return path, Path(virtual_path).name, media_type


async def _request_generation(
    binding: dict[str, Any],
    api_key: str,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    return await _request_provider(
        openai_compatible_endpoint(str(binding["base_url"]), "images/generations"),
        api_key,
        json_payload=payload,
    )


async def _request_edit(
    binding: dict[str, Any],
    api_key: str,
    payload: dict[str, Any],
    source_path: Path,
    *,
    source_name: str | None = None,
    source_media_type: str | None = None,
) -> list[dict[str, Any]]:
    try:
        source = source_path.read_bytes()
    except OSError as exc:
        raise ImageToolError("image_source_invalid", "source image could not be read") from exc
    return await _request_provider(
        openai_compatible_endpoint(str(binding["base_url"]), "images/edits"),
        api_key,
        form_payload={key: str(value) for key, value in payload.items()},
        files={
            "image": (
                source_name or source_path.name,
                source,
                source_media_type or _media_type_for_path(source_path),
            )
        },
    )


async def _request_provider(
    url: str,
    api_key: str,
    *,
    json_payload: dict[str, Any] | None = None,
    form_payload: dict[str, str] | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
) -> list[dict[str, Any]]:
    try:
        async with httpx.AsyncClient(
            timeout=get_settings().model_request_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(
                url,
                headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
                json=json_payload,
                data=form_payload,
                files=files,
            )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise _provider_http_error(exc.response, api_key) from exc
    except httpx.RequestError as exc:
        raise ImageToolError(
            "image_provider_unavailable",
            "image model service network request failed",
            retryable=True,
        ) from exc
    try:
        body = response.json()
    except ValueError as exc:
        raise ImageToolError(
            "image_provider_invalid_response",
            "image model service returned invalid JSON",
        ) from exc
    raw = body.get("data") if isinstance(body, dict) else None
    if isinstance(raw, dict):
        raw = raw.get("images") or raw.get("data") or [raw]
    if raw is None and isinstance(body, dict):
        raw = body.get("images")
    results = (
        [item for item in raw or [] if isinstance(item, dict)] if isinstance(raw, list) else []
    )
    if not results or not any(
        item.get("b64_json") or item.get("base64") or item.get("url") for item in results
    ):
        raise ImageToolError("image_provider_invalid_response", "image model returned no images")
    return results[:_MAX_IMAGES]


def _provider_http_error(response: httpx.Response, api_key: str) -> ImageToolError:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    provider_error = payload.get("error") if isinstance(payload, dict) else None
    provider_code = str(
        (provider_error.get("code") if isinstance(provider_error, dict) else None)
        or (payload.get("code") if isinstance(payload, dict) else None)
        or ""
    )
    provider_message = str(
        (provider_error.get("message") if isinstance(provider_error, dict) else None)
        or (payload.get("message") if isinstance(payload, dict) else None)
        or (payload.get("msg") if isinstance(payload, dict) else None)
        or ""
    )
    provider_message = re.sub(
        r"\s+",
        " ",
        provider_message.replace(api_key, "[redacted]"),
    ).strip()[:240]
    request_id = next(
        (
            str(value)
            for value in (
                payload.get("request_id") if isinstance(payload, dict) else None,
                response.headers.get("x-request-id"),
                response.headers.get("request-id"),
            )
            if value
        ),
        None,
    )
    status = response.status_code
    suffix = f" Provider message: {provider_message}" if provider_message else ""
    if provider_code == "get_channel_failed":
        return ImageToolError(
            "image_model_unavailable",
            "image model has no available provider channel." + suffix,
            request_id=request_id,
        )
    failure = _PROVIDER_STATUS_ERRORS.get(status)
    if failure is not None:
        code, message, retryable = failure
    elif status >= 500:
        code, message, retryable = (
            "image_provider_unavailable",
            f"image provider unavailable ({status})",
            True,
        )
    else:
        code, message, retryable = (
            "image_provider_failed",
            f"image provider request failed ({status})",
            False,
        )
    return ImageToolError(
        code,
        message + suffix,
        retryable=retryable,
        request_id=request_id,
    )


async def _persist_results(
    *,
    store: LocalStore,
    run_id: str,
    tool_call_id: str | None,
    tool_name: str,
    binding: dict[str, Any],
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="shejane-image-") as temp_dir:
        for index, result in enumerate(results, 1):
            body = await _result_bytes(result)
            path = Path(temp_dir) / f"image-{index}"
            path.write_bytes(body)
            media_type, extension = _verified_image(path)
            titled_path = path.with_suffix(extension)
            path.rename(titled_path)
            artifact = await store.create_file_artifact(
                source_path=titled_path,
                run_id=run_id,
                kind="image",
                title=f"generated-image-{index}{extension}",
                content_type=media_type,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                metadata={
                    "connection_id": binding["connection_id"],
                    "model_id": binding["model_id"],
                    "protocol": binding["protocol"],
                },
            )
            artifacts.append(
                {
                    "artifact_id": artifact["id"],
                    "name": artifact["title"],
                    "media_type": artifact["content_type"],
                    "size_bytes": artifact["bytes"],
                    "sha256": artifact["sha256"],
                }
            )
    return artifacts


async def _result_bytes(result: dict[str, Any]) -> bytes:
    encoded = result.get("b64_json") or result.get("base64")
    if isinstance(encoded, str) and encoded:
        if encoded.startswith("data:"):
            encoded = encoded.partition(",")[2]
        try:
            body = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ImageToolError(
                "image_provider_invalid_response", "image Base64 is invalid"
            ) from exc
        if not body or len(body) > _MAX_IMAGE_BYTES:
            raise ImageToolError("image_provider_invalid_response", "image exceeds the size limit")
        return body
    url = result.get("url")
    if isinstance(url, str) and url:
        return await _download_image(url)
    raise ImageToolError("image_provider_invalid_response", "image result has no content")


async def _download_image(url: str) -> bytes:
    transport, reason = _pinned_transport(url)
    if transport is None:
        raise ImageToolError("image_download_blocked", reason)
    try:
        async with httpx.AsyncClient(
            timeout=get_settings().model_request_timeout_seconds,
            follow_redirects=False,
            transport=transport,
        ) as client:
            async with client.stream("GET", url, headers={"Accept": "image/*"}) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > _MAX_IMAGE_BYTES:
                        raise ImageToolError(
                            "image_provider_invalid_response", "image exceeds the size limit"
                        )
                    body.extend(chunk)
    except httpx.HTTPError as exc:
        raise ImageToolError(
            "image_download_failed", "generated image could not be downloaded"
        ) from exc
    if not body:
        raise ImageToolError("image_provider_invalid_response", "downloaded image is empty")
    return bytes(body)


def _verified_image(path: Path) -> tuple[str, str]:
    try:
        with Image.open(path) as image:
            image.verify()
            image_format = str(image.format or "").upper()
    except (OSError, UnidentifiedImageError) as exc:
        raise ImageToolError(
            "image_provider_invalid_response", "provider result is not an image"
        ) from exc
    media_type = _FORMAT_MEDIA_TYPES.get(image_format)
    if media_type is None:
        raise ImageToolError(
            "image_provider_invalid_response", "provider image format is unsupported"
        )
    return media_type, {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[image_format]


def _media_type_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(
        suffix,
        "application/octet-stream",
    )


def _failure(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    request_id: str | None = None,
) -> dict[str, Any]:
    payload = {
        "ok": "false",
        "error_code": code,
        "message": message,
        "recoverable": True,
        "retryable": retryable,
    }
    if request_id:
        payload["request_id"] = request_id
    return payload
