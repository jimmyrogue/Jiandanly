from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

from ..agent.builder import skill_catalog_fingerprint
from ..config import Settings
from ..model_services.credentials import CredentialStoreError
from ..model_services.profiles import (
    apply_known_model_profile_defaults,
    model_capability,
    normalized_model_capabilities,
)
from ..store.sqlite import LocalStore, RunAdmissionError

_IMAGE_TOOL_CAPABILITIES = {
    "image.generate": "image_generation",
    "image.edit": "image_editing",
}


def reasoning_mode_error(
    model_binding: dict[str, Any],
    reasoning_mode: str,
) -> RunAdmissionError | None:
    profile = model_binding.get("profile")
    reasoning = profile.get("reasoning") if isinstance(profile, dict) else None
    modes = reasoning.get("modes") if isinstance(reasoning, dict) else None
    supported_modes = {
        str(mode)
        for mode in modes
        if isinstance(mode, str) and mode in {"off", "high", "max"}
    } if isinstance(modes, list) else {"off"}
    if reasoning_mode not in supported_modes:
        return RunAdmissionError(
            "model_reasoning_mode_unsupported",
            f"selected model does not support reasoning mode {reasoning_mode}",
        )
    return None


def default_reasoning_mode(model_binding: dict[str, Any]) -> str:
    profile = model_binding.get("profile")
    reasoning = profile.get("reasoning") if isinstance(profile, dict) else None
    default_mode = reasoning.get("default_mode") if isinstance(reasoning, dict) else None
    return str(default_mode) if default_mode in {"off", "high", "max"} else "off"


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

    async def binding(
        self,
        principal_id: str,
        requested_model: str,
    ) -> tuple[dict[str, Any], RunAdmissionError | None]:
        if self.settings.fake_llm:
            return {
                "adapter_id": "fake",
                "credential_ref": None,
                "requested_model": requested_model,
                "required_capabilities": ["streaming", "tool_calling"],
                "reasoning_mode": "off",
            }, None
        if requested_model.startswith("local:"):
            parts = requested_model.split(":", 2)
            if len(parts) != 3 or not parts[1] or not parts[2]:
                return {}, RunAdmissionError(
                    "model_spec_invalid",
                    "local model spec must be local:<connection>:<model>",
                )
            connection_id, model_id = parts[1], parts[2]
            async with self.connection_lock(principal_id, connection_id):
                return await self.local_binding_locked(
                    principal_id=principal_id,
                    connection_id=connection_id,
                    model_id=model_id,
                    requested_model=requested_model,
                    required_capabilities=("streaming", "tool_calling"),
                )
        return {}, RunAdmissionError(
            "model_service_missing",
            "select a Runtime BYOK model before starting a run",
        )

    @asynccontextmanager
    async def admission(
        self,
        principal_id: str,
        requested_model: str,
        required_capabilities: tuple[str, ...] = ("streaming", "tool_calling"),
        *,
        binding: Callable[[str, str], Awaitable[tuple[dict[str, Any], RunAdmissionError | None]]]
        | None = None,
        local_binding_locked: Callable[
            ..., Awaitable[tuple[dict[str, Any], RunAdmissionError | None]]
        ]
        | None = None,
    ) -> AsyncIterator[tuple[dict[str, Any], RunAdmissionError | None]]:
        """Keep a model connection stable until its Run is durably admitted."""
        if self.settings.fake_llm:
            yield (
                {
                    "adapter_id": "fake",
                    "credential_ref": None,
                    "requested_model": requested_model,
                    "profile": {capability: True for capability in required_capabilities},
                    "required_capabilities": list(required_capabilities),
                },
                None,
            )
            return
        if not requested_model.startswith("local:"):
            resolve_binding = binding or self.binding
            yield await resolve_binding(principal_id, requested_model)
            return
        parts = requested_model.split(":", 2)
        if len(parts) != 3 or not parts[1] or not parts[2]:
            yield (
                {},
                RunAdmissionError(
                    "model_spec_invalid",
                    "local model spec must be local:<connection>:<model>",
                ),
            )
            return
        connection_id, model_id = parts[1], parts[2]
        async with self.connection_lock(principal_id, connection_id):
            resolver = local_binding_locked or self.local_binding_locked
            yield await resolver(
                principal_id=principal_id,
                connection_id=connection_id,
                model_id=model_id,
                requested_model=requested_model,
                required_capabilities=required_capabilities,
            )

    async def binding_error(
        self,
        principal_id: str,
        settings_snapshot: dict[str, Any],
    ) -> tuple[str | None, str | None]:
        if "_snapshot_version" not in settings_snapshot:
            return None, None
        if settings_snapshot.get("_snapshot_version") != 1:
            return "run settings snapshot version is unsupported", None
        binding = settings_snapshot.get("_model_binding")
        if not isinstance(binding, dict):
            return "run model binding snapshot is missing", None
        if binding.get("adapter_id") == "fake":
            return (
                (None, None) if self.settings.fake_llm else ("fake model service is disabled", None)
            )
        if binding.get("adapter_id") in {
            "openai_chat",
            "anthropic_messages",
            "google_genai",
        }:
            connection_id = binding.get("connection_id")
            if not isinstance(connection_id, str):
                return "run model credential reference is invalid", None
            async with self.connection_lock(principal_id, connection_id):
                return await self.binding_error_locked(
                    principal_id=principal_id,
                    connection_id=connection_id,
                    binding=binding,
                )
        return "run model adapter is no longer supported", None

    async def local_binding_locked(
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
            trusted_model_catalog=connection.get("preset_id")
            in {"shejane-official", "deepseek"},
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
            "provider_family": str(profile.get("provider_family") or "unknown"),
            "reasoning": dict(profile.get("reasoning") or {}),
            "required_capabilities": list(required_capabilities),
            "display_reasoning_summary": (
                protocol == "openai_responses"
                and preset_id == "openai"
                and urlparse(base_url).hostname == "api.openai.com"
            ),
        }, None

    async def capability_binding_snapshots(
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
                trusted_model_catalog=connection.get("preset_id")
                in {"shejane-official", "deepseek"},
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

    async def skill_binding_error(self, settings_snapshot: dict[str, Any]) -> str | None:
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

    async def binding_error_locked(
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
