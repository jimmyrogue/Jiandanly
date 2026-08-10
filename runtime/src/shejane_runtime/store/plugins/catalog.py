"""Installed plugin catalog queries and immutable Run binding resolution."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from ...plugins.identity import plugin_action_catalog_hash
from ..database import SqliteDatabase
from ..errors import RunAdmissionError


def _plugin_record(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        **dict(row),
        "manifest": json.loads(row["manifest_json"]),
        "enabled": bool(row["enabled"]),
        "model_binding": (
            json.loads(row["model_binding_json"]) if row["model_binding_json"] is not None else None
        ),
    }


class PluginCatalogStore(SqliteDatabase):
    async def list_plugins(self, *, principal_id: str) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT v.*, i.enabled, i.model_binding_json, i.model_binding_revision, "
                "i.retired_at AS installation_retired_at "
                "FROM plugin_installations i "
                "JOIN plugin_versions v ON v.digest = i.active_digest "
                "WHERE i.principal_id = ? ORDER BY v.plugin_id",
                (principal_id,),
            )
        ).fetchall()
        return [_plugin_record(row) for row in rows]

    async def referenced_runtime_asset_digests(self) -> set[str]:
        rows = await (
            await self._conn.execute(
                "SELECT DISTINCT v.manifest_json FROM plugin_versions v "
                "WHERE v.digest IN ("
                "SELECT active_digest FROM plugin_installations WHERE retired_at IS NULL "
                "UNION SELECT digest FROM run_plugin_bindings)"
            )
        ).fetchall()
        digests: set[str] = set()
        for row in rows:
            manifest = json.loads(row["manifest_json"])
            execution = manifest.get("runtime", {}).get("execution", {})
            for reference in execution.get("runtime_assets", []):
                digest = reference.get("digest")
                if isinstance(digest, str):
                    digests.add(digest)
        return digests

    async def get_plugin(
        self,
        *,
        principal_id: str,
        plugin_id: str,
    ) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "SELECT v.*, i.enabled, i.model_binding_json, i.model_binding_revision, "
                "i.retired_at AS installation_retired_at "
                "FROM plugin_installations i "
                "JOIN plugin_versions v ON v.digest = i.active_digest "
                "WHERE i.principal_id = ? AND i.plugin_id = ?",
                (principal_id, plugin_id),
            )
        ).fetchone()
        return _plugin_record(row) if row is not None else None

    async def list_plugin_versions(
        self,
        *,
        principal_id: str,
        plugin_id: str,
    ) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT v.version, v.digest, v.signature_status, v.compatibility, "
                "v.state, v.created_at, i.active_digest "
                "FROM plugin_versions v JOIN plugin_installations i "
                "ON i.plugin_id = v.plugin_id "
                "WHERE i.principal_id = ? AND v.plugin_id = ? "
                "ORDER BY v.created_at DESC, v.version DESC",
                (principal_id, plugin_id),
            )
        ).fetchall()
        return [
            {
                "version": row["version"],
                "digest": row["digest"],
                "signature_status": row["signature_status"],
                "compatibility": row["compatibility"],
                "state": row["state"],
                "active": row["digest"] == row["active_digest"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    async def list_run_plugin_bindings(self, run_id: str) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT run_id, plugin_id, version, digest, selection_source, required, "
                "command_id, action_catalog_hash, model_binding_json "
                "FROM run_plugin_bindings "
                "WHERE run_id = ? ORDER BY plugin_id",
                (run_id,),
            )
        ).fetchall()
        bindings = []
        for row in rows:
            binding = {**dict(row), "required": bool(row["required"])}
            raw_model_binding = binding.pop("model_binding_json")
            if raw_model_binding is not None:
                binding["model_binding"] = json.loads(raw_model_binding)
            bindings.append(binding)
        return bindings

    @staticmethod
    async def _resolve_run_plugin_bindings(
        conn: aiosqlite.Connection,
        *,
        principal_id: str,
        plugin_refs: list[dict[str, Any]],
        plugin_command: dict[str, Any] | None,
        inherit_from_run_id: str | None,
    ) -> list[dict[str, Any]]:
        if inherit_from_run_id is not None:
            rows = await (
                await conn.execute(
                    "SELECT b.plugin_id, b.version, b.digest, b.selection_source, "
                    "b.required, b.command_id, b.action_catalog_hash, b.model_binding_json "
                    "FROM run_plugin_bindings b JOIN plugin_versions v "
                    "ON v.plugin_id = b.plugin_id AND v.digest = b.digest "
                    "WHERE b.run_id = ? ORDER BY b.plugin_id",
                    (inherit_from_run_id,),
                )
            ).fetchall()
            return [dict(row) for row in rows]

        rows = await (
            await conn.execute(
                "SELECT i.plugin_id, i.active_digest, i.enabled, i.model_binding_json, "
                "i.retired_at AS installation_retired_at, v.version, v.digest, "
                "v.manifest_json, v.compatibility, v.state "
                "FROM plugin_installations i JOIN plugin_versions v "
                "ON v.plugin_id = i.plugin_id AND v.digest = i.active_digest "
                "WHERE i.principal_id = ?",
                (principal_id,),
            )
        ).fetchall()
        installed = {str(row["plugin_id"]): row for row in rows}
        selected: dict[str, dict[str, Any]] = {}

        def require_available(plugin_id: str, expected_digest: str | None) -> aiosqlite.Row:
            row = installed.get(plugin_id)
            if row is None or row["installation_retired_at"] is not None:
                raise RunAdmissionError("plugin_not_found", f"plugin {plugin_id} is not installed")
            if not bool(row["enabled"]):
                raise RunAdmissionError("plugin_disabled", f"plugin {plugin_id} is disabled")
            if row["compatibility"] != "compatible":
                raise RunAdmissionError(
                    "plugin_incompatible", f"plugin {plugin_id} is incompatible"
                )
            if row["state"] == "retired":
                raise RunAdmissionError("plugin_retired", f"plugin {plugin_id} is retired")
            if expected_digest is not None and expected_digest != row["digest"]:
                raise RunAdmissionError(
                    "plugin_digest_mismatch", f"plugin {plugin_id} active digest changed"
                )
            return row

        def binding(
            row: aiosqlite.Row,
            *,
            selection_source: str,
            required: bool,
            command_id: str | None = None,
        ) -> dict[str, Any]:
            manifest = json.loads(str(row["manifest_json"]))
            command = next(
                (
                    item
                    for item in manifest["contributions"].get("commands", [])
                    if item["id"] == command_id
                ),
                None,
            )
            return {
                "plugin_id": str(row["plugin_id"]),
                "display_name": str(manifest["name"]),
                "version": str(row["version"]),
                "digest": str(row["digest"]),
                "selection_source": selection_source,
                "required": int(required),
                "command_id": command_id,
                "command_title": str(command["title"]) if command is not None else None,
                "action_catalog_hash": plugin_action_catalog_hash(
                    manifest,
                    plugin_digest=str(row["digest"]),
                ),
                "model_binding_json": row["model_binding_json"],
            }

        for row in rows:
            if (
                bool(row["enabled"])
                and row["compatibility"] == "compatible"
                and row["state"] != "retired"
                and row["installation_retired_at"] is None
            ):
                selected[str(row["plugin_id"])] = binding(
                    row,
                    selection_source="enabled",
                    required=False,
                )

        for reference in plugin_refs:
            plugin_id = str(reference["plugin_id"])
            row = require_available(plugin_id, reference.get("expected_digest"))
            selected[plugin_id] = binding(
                row,
                selection_source="explicit",
                required=bool(reference.get("required", True)),
            )

        if plugin_command is not None:
            plugin_id = str(plugin_command["plugin_id"])
            command_id = str(plugin_command["command_id"])
            row = require_available(plugin_id, plugin_command.get("expected_digest"))
            manifest = json.loads(str(row["manifest_json"]))
            commands = manifest["contributions"].get("commands", [])
            if not any(command["id"] == command_id for command in commands):
                raise RunAdmissionError(
                    "plugin_command_not_found",
                    f"plugin {plugin_id} does not contribute command {command_id}",
                )
            selected[plugin_id] = binding(
                row,
                selection_source="command",
                required=True,
                command_id=command_id,
            )

        return [selected[plugin_id] for plugin_id in sorted(selected)]
