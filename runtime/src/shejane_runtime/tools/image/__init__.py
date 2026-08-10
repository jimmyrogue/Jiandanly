"""Runtime-owned image generation/editing tools backed by configured model capabilities."""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool

from ...model_services.credentials import CredentialStoreError, get_model_api_key
from ...model_services.profiles import model_capability, normalized_model_capabilities
from ...store.sqlite import ArtifactConflictError, ArtifactQuotaError, LocalStore
from .provider import (
    MAX_IMAGES as _MAX_IMAGES,
)
from .provider import (
    ImageToolError,
)
from .provider import (
    request_edit as _request_edit,
)
from .provider import (
    request_generation as _request_generation,
)
from .provider import (
    result_bytes as _result_bytes,
)
from .provider import (
    verified_image as _verified_image,
)

log = logging.getLogger("shejane_runtime.tools.image")

_CAPABILITY_PROTOCOLS = {
    "image_generation": "openai_images_generations",
    "image_editing": "openai_images_edits",
}


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
