"""Current plugin installation state and per-installation configuration."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from .codec import encode_payload as _encode_payload
from .database import SqliteDatabase
from .database import configure_connection as _configure_connection
from .database import utc_now as _now
from .errors import PluginStateError


class PluginInstallationStore(SqliteDatabase):
    async def discard_stale_fixed_capability(
        self,
        *,
        principal_id: str,
        plugin_id: str,
    ) -> bool:
        """Disable an unavailable ``runtime_builtin`` installation."""
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await conn.execute(
                        "SELECT enabled, source FROM plugin_installations "
                        "WHERE principal_id = ? AND plugin_id = ?",
                        (principal_id, plugin_id),
                    )
                ).fetchone()
                if row is None or not bool(row["enabled"]):
                    await conn.rollback()
                    return False
                if row["source"] != "runtime_builtin":
                    await conn.rollback()
                    return False
                await conn.execute(
                    "UPDATE plugin_installations SET enabled = 0, "
                    "revision = revision + 1, updated_at = ? "
                    "WHERE principal_id = ? AND plugin_id = ?",
                    (_now(), principal_id, plugin_id),
                )
                await conn.commit()
                return True
            except BaseException:
                await conn.rollback()
                raise

    async def set_plugin_enabled_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        command_type: str,
        plugin_id: str,
        expected_digest: str | None,
        enabled: bool,
    ) -> tuple[dict[str, Any], bool]:
        command_payload: dict[str, Any] = {"type": command_type, "plugin_id": plugin_id}
        if expected_digest is not None:
            command_payload["expected_digest"] = expected_digest
        payload_json = _encode_payload(command_payload)
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type=command_type,
                    payload_json=payload_json,
                )
                if existing is not None:
                    await conn.rollback()
                    return existing, False
                plugin = await (
                    await conn.execute(
                        "SELECT i.active_digest, i.enabled, i.retired_at, "
                        "i.model_binding_json, v.compatibility, v.state, v.manifest_json "
                        "FROM plugin_installations i JOIN plugin_versions v "
                        "ON v.digest = i.active_digest "
                        "WHERE i.principal_id = ? AND i.plugin_id = ?",
                        (principal_id, plugin_id),
                    )
                ).fetchone()
                if plugin is None:
                    raise PluginStateError("plugin_not_found", "plugin is not installed")
                digest = str(plugin["active_digest"])
                if expected_digest is not None and expected_digest != digest:
                    raise PluginStateError("plugin_digest_mismatch", "plugin active digest changed")
                if enabled and plugin["compatibility"] != "compatible":
                    raise PluginStateError(
                        "plugin_incompatible", "plugin is incompatible with this Runtime"
                    )
                if enabled and (plugin["retired_at"] is not None or plugin["state"] == "retired"):
                    raise PluginStateError("plugin_retired", "retired plugin cannot be enabled")
                manifest = json.loads(str(plugin["manifest_json"]))
                needs_model_binding = any(
                    "model.vision.invoke" in action.get("capabilities", [])
                    for action in manifest.get("contributions", {}).get("actions", [])
                    if isinstance(action, dict)
                )
                if enabled and needs_model_binding and not plugin["model_binding_json"]:
                    raise PluginStateError(
                        "plugin_model_binding_required",
                        "plugin requires an explicit Vision model binding before enablement",
                    )
                if bool(plugin["enabled"]) != enabled:
                    await conn.execute(
                        "UPDATE plugin_installations SET enabled = ?, revision = revision + 1, "
                        "updated_at = ? WHERE principal_id = ? AND plugin_id = ?",
                        (int(enabled), _now(), principal_id, plugin_id),
                    )
                receipt = {
                    "type": command_type,
                    "command_id": command_id,
                    "plugin_id": plugin_id,
                    "digest": digest,
                    "enabled": enabled,
                }
                await self._record_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type=command_type,
                    payload_json=payload_json,
                    receipt=receipt,
                    created_at=_now(),
                )
                await conn.commit()
                return receipt, True
            except BaseException:
                await conn.rollback()
                raise

    async def bind_plugin_model_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        plugin_id: str,
        binding_id: str,
        requested_model: str,
        model_binding: dict[str, Any],
        expected_digest: str | None,
    ) -> tuple[dict[str, Any], bool]:
        command_payload: dict[str, Any] = {
            "type": "plugin.model.bind",
            "plugin_id": plugin_id,
            "binding_id": binding_id,
            "model": requested_model,
        }
        if expected_digest is not None:
            command_payload["expected_digest"] = expected_digest
        payload_json = _encode_payload(command_payload)
        frozen_binding = {**model_binding, "id": binding_id}
        binding_json = _encode_payload(frozen_binding)
        now = _now()
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type="plugin.model.bind",
                    payload_json=payload_json,
                )
                if existing is not None:
                    await conn.rollback()
                    return existing, False
                plugin = await (
                    await conn.execute(
                        "SELECT i.active_digest, i.retired_at, i.model_binding_json, "
                        "i.model_binding_revision, v.manifest_json, v.execution_kind, v.state "
                        "FROM plugin_installations i JOIN plugin_versions v "
                        "ON v.digest = i.active_digest "
                        "WHERE i.principal_id = ? AND i.plugin_id = ?",
                        (principal_id, plugin_id),
                    )
                ).fetchone()
                if plugin is None:
                    raise PluginStateError("plugin_not_found", "plugin is not installed")
                digest = str(plugin["active_digest"])
                if expected_digest is not None and expected_digest != digest:
                    raise PluginStateError("plugin_digest_mismatch", "plugin active digest changed")
                if plugin["retired_at"] is not None or plugin["state"] == "retired":
                    raise PluginStateError("plugin_retired", "retired plugin cannot be configured")
                manifest = json.loads(str(plugin["manifest_json"]))
                if plugin["execution_kind"] != "managed_worker" or not any(
                    "model.vision.invoke" in action.get("capabilities", [])
                    for action in manifest["contributions"]["actions"]
                ):
                    raise PluginStateError(
                        "plugin_capability_denied",
                        "plugin does not declare model.vision.invoke",
                    )
                revision = int(plugin["model_binding_revision"])
                if plugin["model_binding_json"] != binding_json:
                    revision += 1
                    await conn.execute(
                        "UPDATE plugin_installations SET model_binding_json = ?, "
                        "model_binding_revision = ?, revision = revision + 1, updated_at = ? "
                        "WHERE principal_id = ? AND plugin_id = ?",
                        (binding_json, revision, now, principal_id, plugin_id),
                    )
                summary = {
                    "id": binding_id,
                    "requested_model": requested_model,
                    "connection_id": str(model_binding["connection_id"]),
                    "connection_version": int(model_binding["connection_version"]),
                    "model_id": str(model_binding["model_id"]),
                }
                receipt = {
                    "type": "plugin.model.bind",
                    "command_id": command_id,
                    "plugin_id": plugin_id,
                    "digest": digest,
                    "model_binding_revision": revision,
                    "model_binding": summary,
                }
                await self._record_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type="plugin.model.bind",
                    payload_json=payload_json,
                    receipt=receipt,
                    created_at=now,
                )
                await conn.commit()
                return receipt, True
            except BaseException:
                await conn.rollback()
                raise
