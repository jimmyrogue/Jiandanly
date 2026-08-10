from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI

from ..shejane_authorization import OFFICIAL_CLOUD_ORIGIN
from . import model_service_preset, openai_compatible_endpoint
from .catalog import (
    _model_connection_models,
    _model_service_response,
    _refresh_model_service_models,
)
from .credentials import (
    CredentialStoreError,
    credential_ref,
    delete_model_api_key,
    set_model_api_key,
)
from .profiles import model_capability

log = logging.getLogger("shejane_runtime.server")


async def _resolve_task_outcome[T](
    task: asyncio.Task[T],
) -> tuple[T | None, BaseException | None, bool]:
    cancellation_requested = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancellation_requested = True
        except BaseException:
            break
    try:
        return task.result(), None, cancellation_requested
    except BaseException as exc:
        return None, exc, cancellation_requested


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
    committed = False
    write_task = asyncio.create_task(
        set_model_api_key(
            principal_id,
            connection_id,
            token,
            next_credential_ref,
        )
    )
    _result, write_error, write_cancelled = await _resolve_task_outcome(write_task)
    if write_cancelled or write_error is not None:
        cleanup_task = asyncio.create_task(
            delete_model_api_key(principal_id, connection_id, next_credential_ref)
        )
        _result, cleanup_error, _cancelled = await _resolve_task_outcome(cleanup_task)
        if cleanup_error is not None:
            raise cleanup_error
        if write_cancelled:
            raise asyncio.CancelledError
        assert write_error is not None
        if isinstance(write_error, CredentialStoreError):
            raise RuntimeError("system credential store is unavailable") from write_error
        raise write_error

    try:
        models, catalog_status = await _refresh_model_service_models(
            preset=preset,
            base_url=official_api_base_url,
            adapter_id="openai_chat",
            api_key=token,
        )
        async with app.state.shejane_authorization_lock:
            existing = [
                row
                for row in await app.state.store.list_model_connections(principal_id=principal_id)
                if row["preset_id"] == "shejane-official"
            ]
            if catalog_status != "ready" or not models:
                for previous in sorted(
                    existing,
                    key=lambda row: (str(row["updated_at"]), str(row["id"])),
                    reverse=True,
                ):
                    try:
                        previous_models = json.loads(str(previous["models_json"]))
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(previous_models, list) and previous_models:
                        models = previous_models
                        catalog_status = "stale"
                        break
                else:
                    raise RuntimeError("official model catalog is unavailable")
            async with AsyncExitStack() as mutation_fences:
                for previous_id in sorted(str(row["id"]) for row in existing):
                    await mutation_fences.enter_async_context(
                        app.state.coordinator.model_connection_mutation(
                            principal_id=principal_id,
                            connection_id=previous_id,
                        )
                    )
                existing = [
                    row
                    for row in await app.state.store.list_model_connections(
                        principal_id=principal_id
                    )
                    if row["preset_id"] == "shejane-official"
                ]
                now = datetime.now(UTC).isoformat()
                row = {
                    "principal_id": principal_id,
                    "id": connection_id,
                    "preset_id": "shejane-official",
                    "name": str(preset["name"]),
                    "region": "official",
                    "adapter_id": "openai_chat",
                    "base_url": official_api_base_url,
                    "requires_api_key": 1,
                    "credential_ref": next_credential_ref,
                    "models_json": json.dumps(
                        models,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "catalog_status": catalog_status,
                    "version": 1,
                    "created_at": now,
                    "updated_at": now,
                }
                response = await _model_service_response(row, credential_configured=True)
                normalized_models = _model_connection_models(row)
                old_ids = {str(previous["id"]) for previous in existing}
                bindings = {
                    str(binding["capability"]): binding
                    for binding in await app.state.store.list_model_capability_bindings(
                        principal_id=principal_id
                    )
                }
                replacements: dict[str, dict[str, str]] = {}
                for capability_name in ("image_generation", "image_editing"):
                    binding = bindings.get(capability_name)
                    if binding is not None and str(binding["connection_id"]) not in old_ids:
                        continue
                    candidates = []
                    for model in normalized_models:
                        capability = model_capability(model, capability_name)
                        if capability is not None and capability.get("verification") == "verified":
                            candidates.append((model, capability))
                    if not candidates:
                        continue
                    preferred_model_id = str(binding["model_id"]) if binding is not None else ""
                    model, capability = max(
                        candidates,
                        key=lambda candidate: (
                            str(candidate[0]["model_id"]) == preferred_model_id,
                            capability_name in candidate[0].get("recommended_for", []),
                        ),
                    )
                    replacements[capability_name] = {
                        "model_id": str(model["model_id"]),
                        "protocol": str(capability["protocol"]),
                    }
                (
                    _created,
                    previous_connections,
                ) = await app.state.store.replace_official_model_connection(
                    principal_id=principal_id,
                    connection_id=connection_id,
                    name=str(preset["name"]),
                    base_url=official_api_base_url,
                    credential_ref=next_credential_ref,
                    models=models,
                    catalog_status=catalog_status,
                    capability_bindings=replacements,
                    timestamp=now,
                )
                committed = True
                for previous in previous_connections:
                    try:
                        await delete_model_api_key(
                            principal_id,
                            str(previous["id"]),
                            str(previous["credential_ref"]),
                        )
                    except CredentialStoreError:
                        log.warning(
                            "could not delete replaced official model-service credential "
                            "connection=%s",
                            previous["id"],
                        )
    except BaseException as exc:
        if not committed:
            cleanup_task = asyncio.create_task(
                delete_model_api_key(principal_id, connection_id, next_credential_ref)
            )
            _result, cleanup_error, _cancelled = await _resolve_task_outcome(cleanup_task)
            if cleanup_error is not None:
                raise cleanup_error from exc
        raise
    return response.model_dump()
