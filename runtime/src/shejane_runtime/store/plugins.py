"""Installed plugin packages, bindings, and lifecycle commands."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from ..plugins.identity import plugin_action_catalog_hash
from .codec import encode_payload as _encode_payload
from .database import SqliteDatabase
from .database import configure_connection as _configure_connection
from .database import utc_now as _now
from .errors import PluginStateError, PluginVersionConflictError, RunAdmissionError


class PluginStore(SqliteDatabase):
    async def install_plugin_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        command_payload: dict[str, Any],
        manifest: dict[str, Any],
        digest: str,
        signature_status: str,
        signer_key_id: str | None,
        compatibility: str,
        source: str,
        command_type: str = "plugin.install",
        receipt_type: str = "plugin.install",
        receipt_extra: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        payload_json = _encode_payload(command_payload)
        plugin_id = str(manifest["id"])
        version = str(manifest["version"])
        execution_kind = str(manifest["runtime"]["execution"]["kind"])
        now = _now()
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

                bound = await (
                    await conn.execute(
                        "SELECT digest FROM plugin_versions WHERE plugin_id = ? AND version = ?",
                        (plugin_id, version),
                    )
                ).fetchone()
                if bound is not None and bound["digest"] != digest:
                    raise PluginVersionConflictError(
                        f"plugin {plugin_id} version {version} already has different content"
                    )
                by_digest = await (
                    await conn.execute(
                        "SELECT plugin_id, version FROM plugin_versions WHERE digest = ?",
                        (digest,),
                    )
                ).fetchone()
                if by_digest is not None and (
                    by_digest["plugin_id"] != plugin_id or by_digest["version"] != version
                ):
                    raise PluginVersionConflictError("plugin digest is bound to another identity")
                if bound is None:
                    await conn.execute(
                        "INSERT INTO plugin_versions "
                        "(plugin_id, version, digest, manifest_json, execution_kind, "
                        "signature_status, signer_key_id, compatibility, source, state, "
                        "created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'installed', ?, ?)",
                        (
                            plugin_id,
                            version,
                            digest,
                            _encode_payload(manifest),
                            execution_kind,
                            signature_status,
                            signer_key_id,
                            compatibility,
                            source,
                            now,
                            now,
                        ),
                    )

                installation = await (
                    await conn.execute(
                        "SELECT active_digest, enabled, retired_at FROM plugin_installations "
                        "WHERE principal_id = ? AND plugin_id = ?",
                        (principal_id, plugin_id),
                    )
                ).fetchone()
                if installation is not None and installation["active_digest"] != digest:
                    raise PluginVersionConflictError(
                        f"plugin {plugin_id} is already installed; use plugin.update"
                    )
                if installation is None:
                    await conn.execute(
                        "INSERT INTO plugin_installations "
                        "(principal_id, plugin_id, active_digest, enabled, source, created_at, updated_at) "
                        "VALUES (?, ?, ?, 0, ?, ?, ?)",
                        (principal_id, plugin_id, digest, source, now, now),
                    )
                    enabled = False
                else:
                    enabled = bool(installation["enabled"])
                    if installation["retired_at"] is not None:
                        await conn.execute(
                            "UPDATE plugin_installations SET retired_at = NULL, "
                            "revision = revision + 1, updated_at = ? "
                            "WHERE principal_id = ? AND plugin_id = ?",
                            (now, principal_id, plugin_id),
                        )
                        await conn.execute(
                            "UPDATE plugin_versions SET state = 'installed', retired_at = NULL, "
                            "updated_at = ? WHERE digest = ?",
                            (now, digest),
                        )

                receipt = {
                    "type": receipt_type,
                    "command_id": command_id,
                    "plugin_id": plugin_id,
                    "version": version,
                    "digest": digest,
                    "installed": True,
                    "enabled": enabled,
                }
                if receipt_extra:
                    receipt.update(receipt_extra)
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) "
                    "VALUES (?, ?, ?, '', ?, ?, NULL, ?)",
                    (
                        principal_id,
                        command_id,
                        command_type,
                        payload_json,
                        _encode_payload(receipt),
                        now,
                    ),
                )
                await conn.commit()
                return receipt, True
            except BaseException:
                await conn.rollback()
                raise

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
        return [
            {
                **dict(row),
                "manifest": json.loads(row["manifest_json"]),
                "enabled": bool(row["enabled"]),
                "model_binding": (
                    json.loads(row["model_binding_json"])
                    if row["model_binding_json"] is not None
                    else None
                ),
            }
            for row in rows
        ]

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

    async def discard_stale_fixed_capability(
        self,
        *,
        principal_id: str,
        plugin_id: str,
    ) -> bool:
        """Disable a `runtime_builtin` plugin installation whose source
        package is no longer available.  Returns `True` when a change
        was committed.  User-installed plugins are never touched."""
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

    async def get_plugin_setup_flow(self, *, principal_id: str, plugin_id: str) -> dict[str, Any]:
        row = await (
            await self._conn.execute(
                "SELECT stage, revision, updated_at FROM plugin_setup_flows "
                "WHERE principal_id = ? AND plugin_id = ?",
                (principal_id, plugin_id),
            )
        ).fetchone()
        if row is None:
            return {"stage": "idle", "revision": 0, "updated_at": None}
        return dict(row)

    async def begin_plugin_setup_action(
        self,
        *,
        principal_id: str,
        plugin_id: str,
        expected_revision: int,
        next_stage: str,
    ) -> dict[str, Any]:
        now = _now()
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await conn.execute(
                        "SELECT stage, revision FROM plugin_setup_flows "
                        "WHERE principal_id = ? AND plugin_id = ?",
                        (principal_id, plugin_id),
                    )
                ).fetchone()
                revision = int(row["revision"]) if row is not None else 0
                if revision != expected_revision:
                    raise PluginStateError("plugin_setup_stale", "plugin setup state changed")
                next_revision = revision + 1
                await conn.execute(
                    "INSERT INTO plugin_setup_flows "
                    "(principal_id, plugin_id, stage, revision, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(principal_id, plugin_id) DO UPDATE SET "
                    "stage = excluded.stage, revision = excluded.revision, "
                    "updated_at = excluded.updated_at",
                    (principal_id, plugin_id, next_stage, next_revision, now),
                )
                await conn.commit()
                return {
                    "stage": next_stage,
                    "revision": next_revision,
                    "updated_at": now,
                }
            except BaseException:
                await conn.rollback()
                raise

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
        if row is None:
            return None
        return {
            **dict(row),
            "manifest": json.loads(row["manifest_json"]),
            "enabled": bool(row["enabled"]),
            "model_binding": (
                json.loads(row["model_binding_json"])
                if row["model_binding_json"] is not None
                else None
            ),
        }

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
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) VALUES (?, ?, ?, '', ?, ?, NULL, ?)",
                    (
                        principal_id,
                        command_id,
                        command_type,
                        payload_json,
                        _encode_payload(receipt),
                        _now(),
                    ),
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
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) "
                    "VALUES (?, ?, 'plugin.model.bind', '', ?, ?, NULL, ?)",
                    (principal_id, command_id, payload_json, _encode_payload(receipt), now),
                )
                await conn.commit()
                return receipt, True
            except BaseException:
                await conn.rollback()
                raise

    async def update_plugin_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        command_payload: dict[str, Any],
        plugin_id: str,
        manifest: dict[str, Any],
        digest: str,
        signature_status: str,
        signer_key_id: str | None,
        compatibility: str,
        source: str,
        command_type: str = "plugin.update",
        receipt_type: str = "plugin.update",
        receipt_extra: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        payload_json = _encode_payload(command_payload)
        version = str(manifest["version"])
        now = _now()
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
                installation = await (
                    await conn.execute(
                        "SELECT active_digest, enabled, retired_at FROM plugin_installations "
                        "WHERE principal_id = ? AND plugin_id = ?",
                        (principal_id, plugin_id),
                    )
                ).fetchone()
                if installation is None:
                    raise PluginStateError("plugin_not_found", "plugin is not installed")
                if installation["retired_at"] is not None:
                    raise PluginStateError("plugin_retired", "retired plugin must be reinstalled")
                previous_digest = str(installation["active_digest"])
                expected_digest = command_payload.get(
                    "expected_digest", command_payload.get("expected_active_digest")
                )
                if expected_digest is not None and expected_digest != previous_digest:
                    raise PluginStateError("plugin_digest_mismatch", "plugin active digest changed")
                if manifest["id"] != plugin_id:
                    raise PluginStateError(
                        "plugin_identity_mismatch", "update package has a different plugin id"
                    )
                if compatibility != "compatible":
                    raise PluginStateError(
                        "plugin_incompatible", "update is incompatible with this Runtime"
                    )
                bound = await (
                    await conn.execute(
                        "SELECT digest FROM plugin_versions WHERE plugin_id = ? AND version = ?",
                        (plugin_id, version),
                    )
                ).fetchone()
                if bound is not None and bound["digest"] != digest:
                    raise PluginVersionConflictError(
                        f"plugin {plugin_id} version {version} already has different content"
                    )
                if bound is None:
                    await conn.execute(
                        "INSERT INTO plugin_versions "
                        "(plugin_id, version, digest, manifest_json, execution_kind, "
                        "signature_status, signer_key_id, compatibility, source, state, "
                        "created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'installed', ?, ?)",
                        (
                            plugin_id,
                            version,
                            digest,
                            _encode_payload(manifest),
                            manifest["runtime"]["execution"]["kind"],
                            signature_status,
                            signer_key_id,
                            compatibility,
                            source,
                            now,
                            now,
                        ),
                    )
                else:
                    await conn.execute(
                        "UPDATE plugin_versions SET state = 'installed', retired_at = NULL, "
                        "updated_at = ? WHERE digest = ?",
                        (now, digest),
                    )
                await conn.execute(
                    "UPDATE plugin_installations SET active_digest = ?, "
                    "revision = revision + 1, updated_at = ? "
                    "WHERE principal_id = ? AND plugin_id = ?",
                    (digest, now, principal_id, plugin_id),
                )
                receipt = {
                    "type": receipt_type,
                    "command_id": command_id,
                    "plugin_id": plugin_id,
                    "version": version,
                    "previous_digest": previous_digest,
                    "digest": digest,
                    "enabled": bool(installation["enabled"]),
                }
                if receipt_extra:
                    receipt.update(receipt_extra)
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) "
                    "VALUES (?, ?, ?, '', ?, ?, NULL, ?)",
                    (
                        principal_id,
                        command_id,
                        command_type,
                        payload_json,
                        _encode_payload(receipt),
                        now,
                    ),
                )
                await conn.commit()
                return receipt, True
            except BaseException:
                await conn.rollback()
                raise

    async def rollback_plugin_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        plugin_id: str,
        target_digest: str,
        expected_digest: str | None,
    ) -> tuple[dict[str, Any], bool]:
        command_payload: dict[str, Any] = {
            "type": "plugin.rollback",
            "plugin_id": plugin_id,
            "target_digest": target_digest,
        }
        if expected_digest is not None:
            command_payload["expected_digest"] = expected_digest
        payload_json = _encode_payload(command_payload)
        now = _now()
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type="plugin.rollback",
                    payload_json=payload_json,
                )
                if existing is not None:
                    await conn.rollback()
                    return existing, False
                installation = await (
                    await conn.execute(
                        "SELECT active_digest, enabled, retired_at FROM plugin_installations "
                        "WHERE principal_id = ? AND plugin_id = ?",
                        (principal_id, plugin_id),
                    )
                ).fetchone()
                if installation is None:
                    raise PluginStateError("plugin_not_found", "plugin is not installed")
                if installation["retired_at"] is not None:
                    raise PluginStateError("plugin_retired", "retired plugin must be reinstalled")
                previous_digest = str(installation["active_digest"])
                if expected_digest is not None and expected_digest != previous_digest:
                    raise PluginStateError("plugin_digest_mismatch", "plugin active digest changed")
                target = await (
                    await conn.execute(
                        "SELECT version, compatibility FROM plugin_versions "
                        "WHERE plugin_id = ? AND digest = ?",
                        (plugin_id, target_digest),
                    )
                ).fetchone()
                if target is None:
                    raise PluginStateError(
                        "plugin_version_unavailable", "rollback target is not installed"
                    )
                if target["compatibility"] != "compatible":
                    raise PluginStateError("plugin_incompatible", "rollback target is incompatible")
                await conn.execute(
                    "UPDATE plugin_versions SET state = 'installed', retired_at = NULL, "
                    "updated_at = ? WHERE digest = ?",
                    (now, target_digest),
                )
                await conn.execute(
                    "UPDATE plugin_installations SET active_digest = ?, "
                    "revision = revision + 1, updated_at = ? "
                    "WHERE principal_id = ? AND plugin_id = ?",
                    (target_digest, now, principal_id, plugin_id),
                )
                receipt = {
                    "type": "plugin.rollback",
                    "command_id": command_id,
                    "plugin_id": plugin_id,
                    "version": target["version"],
                    "previous_digest": previous_digest,
                    "digest": target_digest,
                    "enabled": bool(installation["enabled"]),
                }
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) "
                    "VALUES (?, ?, 'plugin.rollback', '', ?, ?, NULL, ?)",
                    (principal_id, command_id, payload_json, _encode_payload(receipt), now),
                )
                await conn.commit()
                return receipt, True
            except BaseException:
                await conn.rollback()
                raise

    async def remove_plugin_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        plugin_id: str,
        expected_digest: str | None,
    ) -> tuple[dict[str, Any], bool]:
        command_payload: dict[str, Any] = {"type": "plugin.remove", "plugin_id": plugin_id}
        if expected_digest is not None:
            command_payload["expected_digest"] = expected_digest
        payload_json = _encode_payload(command_payload)
        now = _now()
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._accepted_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type="plugin.remove",
                    payload_json=payload_json,
                )
                if existing is not None:
                    await conn.rollback()
                    return existing, False
                installation = await (
                    await conn.execute(
                        "SELECT active_digest FROM plugin_installations "
                        "WHERE principal_id = ? AND plugin_id = ?",
                        (principal_id, plugin_id),
                    )
                ).fetchone()
                if installation is None:
                    raise PluginStateError("plugin_not_found", "plugin is not installed")
                digest = str(installation["active_digest"])
                if expected_digest is not None and expected_digest != digest:
                    raise PluginStateError("plugin_digest_mismatch", "plugin active digest changed")
                await conn.execute(
                    "UPDATE plugin_installations SET enabled = 0, retired_at = ?, "
                    "revision = revision + 1, updated_at = ? "
                    "WHERE principal_id = ? AND plugin_id = ?",
                    (now, now, principal_id, plugin_id),
                )
                active_count = int(
                    (
                        await (
                            await conn.execute(
                                "SELECT COUNT(*) FROM plugin_installations "
                                "WHERE active_digest = ? AND retired_at IS NULL",
                                (digest,),
                            )
                        ).fetchone()
                    )[0]
                )
                if active_count == 0:
                    await conn.execute(
                        "UPDATE plugin_versions SET state = 'retired', retired_at = ?, "
                        "updated_at = ? WHERE digest = ?",
                        (now, now, digest),
                    )
                receipt = {
                    "type": "plugin.remove",
                    "command_id": command_id,
                    "plugin_id": plugin_id,
                    "digest": digest,
                    "retired": True,
                    "enabled": False,
                }
                await conn.execute(
                    "INSERT INTO local_commands "
                    "(principal_id, id, command_type, client_message_id, payload_json, "
                    "response_json, run_id, created_at) "
                    "VALUES (?, ?, 'plugin.remove', '', ?, ?, NULL, ?)",
                    (principal_id, command_id, payload_json, _encode_payload(receipt), now),
                )
                await conn.commit()
                return receipt, True
            except BaseException:
                await conn.rollback()
                raise
