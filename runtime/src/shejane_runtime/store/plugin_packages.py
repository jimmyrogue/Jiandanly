"""Transactional plugin package install, update, rollback, and removal."""

from __future__ import annotations

from typing import Any

import aiosqlite

from .codec import encode_payload as _encode_payload
from .database import SqliteDatabase
from .database import configure_connection as _configure_connection
from .database import utc_now as _now
from .errors import PluginStateError, PluginVersionConflictError


class PluginPackageStore(SqliteDatabase):
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
                await self._record_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type=command_type,
                    payload_json=payload_json,
                    receipt=receipt,
                    created_at=now,
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
                await self._record_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type=command_type,
                    payload_json=payload_json,
                    receipt=receipt,
                    created_at=now,
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
                await self._record_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type="plugin.rollback",
                    payload_json=payload_json,
                    receipt=receipt,
                    created_at=now,
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
                await self._record_command_receipt_uncommitted(
                    conn,
                    principal_id=principal_id,
                    command_id=command_id,
                    command_type="plugin.remove",
                    payload_json=payload_json,
                    receipt=receipt,
                    created_at=now,
                )
                await conn.commit()
                return receipt, True
            except BaseException:
                await conn.rollback()
                raise
