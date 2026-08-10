"""Installed plugin queries, enablement, and model binding."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ...store.sqlite import LocalStore, PluginStateError
from ..computer_use import COMPUTER_USE_PLUGIN_ID
from .types import PluginRegistryError


class PluginInstallations:
    def __init__(
        self,
        *,
        store: LocalStore,
        computer_use_readiness: Callable[..., Awaitable[dict[str, Any]]],
    ) -> None:
        self._store = store
        self._computer_use_readiness = computer_use_readiness

    async def list(self, *, principal_id: str) -> list[dict[str, Any]]:
        records = await self._store.list_plugins(principal_id=principal_id)
        return [_plugin_summary(record) for record in records]

    async def inspect(self, *, principal_id: str, plugin_id: str) -> dict[str, Any]:
        record = await self._store.get_plugin(principal_id=principal_id, plugin_id=plugin_id)
        if record is None:
            raise PluginRegistryError(
                "plugin_not_found", "plugin is not installed", status_code=404
            )
        manifest = record["manifest"]
        versions = await self._store.list_plugin_versions(
            principal_id=principal_id,
            plugin_id=plugin_id,
        )
        return {
            **_plugin_summary(record),
            "description": manifest["description"],
            "license": manifest.get("license"),
            "actions": [
                {
                    key: action[key]
                    for key in (
                        "id",
                        "title",
                        "description",
                        "consumes",
                        "produces",
                        "effects",
                        "determinism",
                        "capabilities",
                        "limits",
                    )
                }
                for action in manifest["contributions"]["actions"]
            ],
            "skills": [
                {key: skill[key] for key in ("id", "path")}
                for skill in manifest["contributions"].get("skills", [])
            ],
            "commands": [
                {key: command[key] for key in ("id", "title", "description", "required_actions")}
                for command in manifest["contributions"].get("commands", [])
            ],
            "mcp_servers": [
                {key: binding[key] for key in ("id", "path")}
                for binding in manifest["contributions"].get("mcp_servers", [])
            ],
            "versions": versions,
            "model_binding": _model_binding_summary(record.get("model_binding")),
        }

    async def set_enabled(
        self,
        *,
        principal_id: str,
        command_id: str,
        plugin_id: str,
        expected_digest: str | None,
        enabled: bool,
    ) -> dict[str, Any]:
        if plugin_id == COMPUTER_USE_PLUGIN_ID and enabled:
            readiness = await self._computer_use_readiness(principal_id=principal_id)
            if readiness["state"] != "ready":
                raise PluginRegistryError(
                    "plugin_setup_required",
                    "Finish Computer Use setup before enabling it",
                    status_code=409,
                )
        command_type = "plugin.enable" if enabled else "plugin.disable"
        try:
            receipt, _created = await self._store.set_plugin_enabled_command(
                principal_id=principal_id,
                command_id=command_id,
                command_type=command_type,
                plugin_id=plugin_id,
                expected_digest=expected_digest,
                enabled=enabled,
            )
            return receipt
        except PluginStateError as exc:
            status_code = 404 if exc.code == "plugin_not_found" else 409
            raise PluginRegistryError(exc.code, str(exc), status_code=status_code) from exc

    async def bind_model(
        self,
        *,
        principal_id: str,
        command_id: str,
        plugin_id: str,
        binding_id: str,
        requested_model: str,
        model_binding: dict[str, Any],
        expected_digest: str | None,
    ) -> dict[str, Any]:
        try:
            receipt, _created = await self._store.bind_plugin_model_command(
                principal_id=principal_id,
                command_id=command_id,
                plugin_id=plugin_id,
                binding_id=binding_id,
                requested_model=requested_model,
                model_binding=model_binding,
                expected_digest=expected_digest,
            )
            return receipt
        except PluginStateError as exc:
            status_code = 404 if exc.code == "plugin_not_found" else 409
            raise PluginRegistryError(exc.code, str(exc), status_code=status_code) from exc


def _plugin_summary(record: dict[str, Any]) -> dict[str, Any]:
    manifest = record["manifest"]
    return {
        "id": record["plugin_id"],
        "name": manifest["name"],
        "description": manifest["description"],
        "version": record["version"],
        "digest": record["digest"],
        "publisher": {
            "id": manifest["publisher"]["id"],
            "name": manifest["publisher"]["name"],
        },
        "execution_kind": record["execution_kind"],
        "signature_status": record["signature_status"],
        "compatibility": record["compatibility"],
        "enabled": record["enabled"],
        "retired": record["installation_retired_at"] is not None,
    }


def _model_binding_summary(binding: Any) -> dict[str, Any] | None:
    if not isinstance(binding, dict):
        return None
    return {
        "id": str(binding["id"]),
        "requested_model": str(binding["requested_model"]),
        "connection_id": str(binding["connection_id"]),
        "connection_version": int(binding["connection_version"]),
        "model_id": str(binding["model_id"]),
    }
