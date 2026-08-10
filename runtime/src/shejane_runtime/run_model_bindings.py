from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

from .agent.builder import skill_catalog_fingerprint
from .config import Settings
from .model_credentials import CredentialStoreError
from .model_profiles import (
    apply_known_model_profile_defaults,
    model_capability,
    normalized_model_capabilities,
)
from .store.sqlite import LocalStore, RunAdmissionError

_IMAGE_TOOL_CAPABILITIES = {
    "image.generate": "image_generation",
    "image.edit": "image_editing",
}


class RunModelBindings:
    def __init__(
        self,
        store: LocalStore,
        settings: Settings,
        get_model_api_key: Callable[..., Awaitable[str | None]],
    ) -> None:
        self.store = store
        self.settings = settings
        self._get_model_api_key = get_model_api_key
        self._connection_locks: dict[tuple[str, str], asyncio.Lock] = {}

    def connection_lock(self, principal_id: str, connection_id: str) -> asyncio.Lock:
        key = (principal_id, connection_id)
        lock = self._connection_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._connection_locks[key] = lock
        return lock

    async def _local_model_binding_locked(
        self,
        *,
        principal_id: str,
        connection_id: str,
        model_id: str,
        requested_model: str,
        required_capabilities: tuple[str, ...] = ("streaming", "tool_calling"),
    ) -> tuple[dict[str, Any], RunAdmissionError | None]:
        connection = await self.store.get_model_connection(
            principal_id=principal_id,
            connection_id=connection_id,
        )
        if connection is None:
            return {}, RunAdmissionError(
                "model_service_missing",
                "model service is not connected",
            )
        try:
            models = json.loads(connection.get("models_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            models = []
        profile = next(
            (
                model
                for model in models
                if isinstance(model, dict) and model.get("model_id") == model_id
            ),
            None,
        )
        if profile is None:
            return {}, RunAdmissionError(
                "model_not_found",
                "model is not available from this connection",
            )
        profile = apply_known_model_profile_defaults(
            profile,
            service_base_url=str(connection.get("base_url") or ""),
            trusted_model_catalog=connection.get("preset_id") == "shejane-official",
        )
        profile["capabilities"] = normalized_model_capabilities(
            profile,
            adapter_id=str(connection.get("adapter_id") or "openai_chat"),
        )
        agent_capability = model_capability(profile, "agent_chat")
        if agent_capability is None:
            return {}, RunAdmissionError(
                "model_capability_unavailable",
                "model does not declare Agent chat capability",
            )
        try:
            if not await self._get_model_api_key(
                principal_id,
                connection_id,
                str(connection["credential_ref"]),
            ):
                return {}, RunAdmissionError(
                    "model_service_missing",
                    "model service API key is not configured",
                )
        except CredentialStoreError as exc:
            return {}, RunAdmissionError(
                "model_credential_store_unavailable",
                str(exc),
            )
        protocol = str(agent_capability.get("protocol"))
        base_url = str(connection["base_url"])
        preset_id = str(connection.get("preset_id") or "")
        return {
            "adapter_id": {
                "openai_chat_completions": "openai_chat",
                "openai_responses": "openai_chat",
                "anthropic_messages": "anthropic_messages",
                "google_generate_content": "google_genai",
            }.get(protocol, str(connection["adapter_id"])),
            "protocol": protocol,
            "preset_id": preset_id,
            "connection_id": connection_id,
            "connection_version": int(connection.get("version") or 1),
            "base_url": base_url,
            "credential_ref": str(connection["credential_ref"]),
            "requested_model": requested_model,
            "model_id": model_id,
            "profile": profile,
            "required_capabilities": list(required_capabilities),
            "display_reasoning_summary": (
                protocol == "openai_responses"
                and preset_id == "openai"
                and urlparse(base_url).hostname == "api.openai.com"
            ),
        }, None

    async def _capability_binding_snapshots(
        self,
        *,
        principal_id: str,
        required_tools: list[str],
    ) -> tuple[dict[str, dict[str, Any]], RunAdmissionError | None]:
        """Resolve Runtime-owned default image bindings into an immutable Run snapshot."""
        rows = {
            str(row["capability"]): row
            for row in await self.store.list_model_capability_bindings(principal_id=principal_id)
        }
        snapshots: dict[str, dict[str, Any]] = {}
        for capability in set(_IMAGE_TOOL_CAPABILITIES.values()):
            row = rows.get(capability)
            if row is None:
                continue
            connection_id = str(row["connection_id"])
            connection = await self.store.get_model_connection(
                principal_id=principal_id,
                connection_id=connection_id,
            )
            if connection is None or int(connection.get("version") or 0) != int(
                row["connection_version"]
            ):
                continue
            try:
                models = json.loads(connection.get("models_json") or "[]")
            except (json.JSONDecodeError, TypeError):
                models = []
            profile = next(
                (
                    item
                    for item in models
                    if isinstance(item, dict) and item.get("model_id") == row["model_id"]
                ),
                None,
            )
            if profile is None:
                continue
            profile = apply_known_model_profile_defaults(
                profile,
                service_base_url=str(connection.get("base_url") or ""),
                trusted_model_catalog=connection.get("preset_id") == "shejane-official",
            )
            profile["capabilities"] = normalized_model_capabilities(
                profile,
                adapter_id=str(connection.get("adapter_id") or "openai_chat"),
            )
            verified = model_capability(profile, capability)
            if (
                verified is None
                or verified.get("verification") != "verified"
                or verified.get("protocol") != row["protocol"]
            ):
                continue
            try:
                api_key = await self._get_model_api_key(
                    principal_id,
                    connection_id,
                    str(connection["credential_ref"]),
                )
            except CredentialStoreError as exc:
                return {}, RunAdmissionError("model_credential_store_unavailable", str(exc))
            if not api_key:
                continue
            snapshots[capability] = {
                "capability": capability,
                "connection_id": connection_id,
                "connection_version": int(connection["version"]),
                "base_url": str(connection["base_url"]),
                "credential_ref": str(connection["credential_ref"]),
                "model_id": str(row["model_id"]),
                "protocol": str(row["protocol"]),
                "revision": int(row["revision"]),
            }

        missing = [
            tool_name
            for tool_name in required_tools
            if _IMAGE_TOOL_CAPABILITIES[tool_name] not in snapshots
        ]
        if missing:
            return snapshots, RunAdmissionError(
                "required_tool_unavailable",
                f"required tools are not configured: {', '.join(missing)}",
            )
        return snapshots, None

    async def _skill_binding_error(self, settings_snapshot: dict[str, Any]) -> str | None:
        # Runs accepted before Skill fingerprints existed remain resumable.
        if settings_snapshot.get("skills") != "on":
            return None
        admitted = settings_snapshot.get("_skills_fingerprint")
        if not isinstance(admitted, str) or not admitted:
            return None
        try:
            current = await asyncio.to_thread(skill_catalog_fingerprint)
        except OSError as exc:
            return f"Skill configuration is unavailable: {exc}"
        if current != admitted:
            return "Skill configuration changed after Run admission"
        return None

    async def _model_binding_error_locked(
        self,
        *,
        principal_id: str,
        connection_id: str,
        binding: dict[str, Any],
    ) -> tuple[str | None, str | None]:
        connection = await self.store.get_model_connection(
            principal_id=principal_id,
            connection_id=connection_id,
        )
        if (
            connection is None
            or int(connection.get("version") or 0) != binding.get("connection_version")
            or binding.get("credential_ref") != connection.get("credential_ref")
        ):
            return "model service connection was changed or removed", None
        try:
            api_key = await self._get_model_api_key(
                principal_id,
                connection_id,
                str(binding["credential_ref"]),
            )
        except CredentialStoreError as exc:
            return str(exc), None
        if not api_key:
            return "model service API key is no longer configured", None
        return None, api_key
