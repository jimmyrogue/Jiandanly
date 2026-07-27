"""Runtime-owned image generation/editing tools backed by configured model capabilities."""

from __future__ import annotations

import base64
import binascii
import json
import logging
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


class ImageToolError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def make_image_tools() -> list[BaseTool]:
    @tool("image.generate")
    async def image_generate(
        prompt: str,
        runtime: ToolRuntime[Any] = None,  # type: ignore[assignment]
        n: int = 1,
        size: str | None = None,
        quality: str | None = None,
        background: str | None = None,
    ) -> dict[str, Any]:
        """Generate images from a text prompt and save them as Runtime Artifacts."""
        return await _run_image_tool(
            capability="image_generation",
            prompt=prompt,
            runtime=runtime,
            n=n,
            options={"size": size, "quality": quality, "background": background},
        )

    @tool("image.edit")
    async def image_edit(
        source_artifact_id: str,
        prompt: str,
        runtime: ToolRuntime[Any] = None,  # type: ignore[assignment]
        n: int = 1,
        size: str | None = None,
    ) -> dict[str, Any]:
        """Edit an image Artifact and save each result as a new Runtime Artifact."""
        return await _run_image_tool(
            capability="image_editing",
            prompt=prompt,
            runtime=runtime,
            n=n,
            options={"size": size},
            source_artifact_id=source_artifact_id,
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
        if capability == "image_editing":
            source = await _owned_image_artifact(
                store,
                principal_id=str(principal_id),
                artifact_id=str(source_artifact_id or ""),
            )
            results = await _request_edit(
                binding, api_key, payload, store.artifact_body_path(source)
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
        return _failure(exc.code, str(exc), retryable=exc.retryable)
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
                source_path.name,
                source,
                _media_type_for_path(source_path),
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
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ImageToolError(
            "image_provider_failed",
            "image model service request failed",
            retryable=isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)),
        ) from exc
    raw = body.get("data") if isinstance(body, dict) else None
    if isinstance(raw, dict):
        raw = raw.get("images", [raw])
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


def _failure(code: str, message: str, *, retryable: bool = False) -> dict[str, Any]:
    return {
        "ok": "false",
        "error_code": code,
        "message": message,
        "recoverable": True,
        "retryable": retryable,
    }
