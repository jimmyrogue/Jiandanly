"""Plugin input materialization and output Artifact persistence."""

from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from langchain_core.tools import ToolException

from ..store.artifacts import file_identity
from ..store.sqlite import LocalStore
from .catalog import PluginActionDescriptor


class PluginActionError(ToolException):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


async def _resolve_inputs(
    *,
    store: LocalStore,
    run_id: str,
    action: PluginActionDescriptor,
    arguments: dict[str, Any],
    context_inputs: Any,
) -> list[dict[str, Any]]:
    compatible = [
        dict(item)
        for item in context_inputs
        if isinstance(item, dict) and item.get("media_type") in action.consumes
    ]
    selected_ids = _selected_input_ids(arguments)
    if selected_ids is None:
        return compatible

    resolved: list[dict[str, Any]] = []
    for selected_id in selected_ids:
        selected = next(
            (
                item
                for item in compatible
                if item.get("id") == selected_id or item.get("virtual_path") == selected_id
            ),
            None,
        )
        if selected is not None:
            resolved.append(selected)
            continue
        artifact_input = await _artifact_input(
            store=store,
            run_id=run_id,
            selected_id=selected_id,
            consumes=action.consumes,
        )
        if artifact_input is None:
            return []
        resolved.append(artifact_input)
    return resolved


def _selected_input_ids(arguments: dict[str, Any]) -> list[str] | None:
    selected_id = arguments.get("input_id")
    if isinstance(selected_id, str) and selected_id:
        return [selected_id]
    selected_ids = arguments.get("input_ids")
    if (
        isinstance(selected_ids, list)
        and selected_ids
        and all(isinstance(item, str) and item for item in selected_ids)
    ):
        return selected_ids
    return None


async def _artifact_input(
    *,
    store: LocalStore,
    run_id: str,
    selected_id: str,
    consumes: tuple[str, ...],
) -> dict[str, Any] | None:
    artifact = await store.get_artifact(selected_id)
    if (
        artifact is None
        or artifact.get("run_id") != run_id
        or artifact.get("storage_kind") != "blob"
        or artifact.get("content_type") not in consumes
        or not isinstance(artifact.get("bytes"), int)
        or not isinstance(artifact.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]) is None
    ):
        return None
    try:
        source = store.artifact_body_path(artifact)
    except (OSError, RuntimeError, ValueError):
        return None

    suffix = PurePosixPath(str(artifact.get("title") or "").replace("\\", "/")).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) is None:
        suffix = ""
    safe_id = selected_id
    if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", safe_id) is None:
        safe_id = hashlib.sha256(selected_id.encode("utf-8")).hexdigest()[:32]
    return {
        "id": selected_id,
        "path": f"/input/artifacts/{safe_id}/artifact{suffix}",
        "media_type": artifact["content_type"],
        "size_bytes": artifact["bytes"],
        "sha256": artifact["sha256"],
        "source_path": str(source),
    }


async def _materialize_inputs(inputs: list[dict[str, Any]], input_root: Path) -> None:
    for item in inputs:
        source = Path(str(item["source_path"]))
        if source.is_symlink() or not source.is_file():
            raise PluginActionError("invalid_invocation", "plugin input is unavailable")
        try:
            relative = PurePosixPath(str(item["path"])).relative_to("/input")
        except ValueError as exc:
            raise PluginActionError("invalid_invocation", "plugin input path is invalid") from exc
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise PluginActionError("invalid_invocation", "plugin input path is invalid")
        destination = input_root.joinpath(*relative.parts)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        await _copy_file(source, destination)
        size, digest = await asyncio.to_thread(file_identity, destination)
        if size != item["size_bytes"] or digest != item["sha256"]:
            raise PluginActionError(
                "invalid_invocation",
                "plugin input changed after Run admission",
            )


async def _copy_file(source: Path, destination: Path) -> None:
    await asyncio.to_thread(shutil.copyfile, source, destination)


async def _persist_artifacts(
    *,
    store: LocalStore,
    run_id: str,
    operation_id: str,
    tool_call_id: str,
    action: PluginActionDescriptor,
    output_root: Path,
    candidates: Any,
    provenance: dict[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(candidates, list) or len(candidates) > 128:
        raise PluginActionError("protocol_violation", "plugin returned invalid artifacts")
    validated: list[tuple[int, Path, str, str, int, str]] = []
    total = 0
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise PluginActionError("protocol_violation", "plugin artifact is invalid")
        try:
            relative = PurePosixPath(str(candidate["path"])).relative_to("/output")
            media_type = str(candidate["media_type"])
            name = str(candidate["name"])
        except (KeyError, ValueError) as exc:
            raise PluginActionError("protocol_violation", "plugin artifact is invalid") from exc
        if (
            not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
            or media_type not in action.produces
        ):
            raise PluginActionError("protocol_violation", "plugin artifact is not declared")
        source = output_root.joinpath(*relative.parts)
        if source.is_symlink() or not source.is_file():
            raise PluginActionError("protocol_violation", "plugin artifact is unavailable")
        try:
            source.resolve(strict=True).relative_to(output_root.resolve(strict=True))
        except ValueError as exc:
            raise PluginActionError(
                "protocol_violation", "plugin artifact escaped staging"
            ) from exc
        size, digest = await asyncio.to_thread(file_identity, source)
        total += size
        if total > int(action.limits["output_mb"]) * 1024 * 1024:
            raise PluginActionError("resource_exhausted", "plugin artifact limit exceeded")
        validated.append((index, source, media_type, name, size, digest))

    persisted: list[dict[str, Any]] = []
    for index, source, media_type, name, size, digest in validated:
        artifact = await store.create_file_artifact(
            artifact_id=f"art_{operation_id.removeprefix('toolop_')}_{index}",
            run_id=run_id,
            kind="plugin_output",
            title=name,
            source_path=source,
            content_type=media_type,
            expected_sha256=digest,
            tool_call_id=tool_call_id,
            tool_name=action.tool_name,
            metadata={
                "operation_id": operation_id,
                "plugin_id": action.plugin_id,
                "plugin_version": action.plugin_version,
                "plugin_digest": action.plugin_digest,
                "action_id": action.action_id,
                "storage_kind": "blob",
                "size_bytes": size,
                "sha256": digest,
                "provenance": provenance,
            },
        )
        persisted.append(
            {
                "artifact_id": artifact["id"],
                "name": name,
                "media_type": media_type,
                "size_bytes": size,
                "sha256": digest,
            }
        )
    return persisted
