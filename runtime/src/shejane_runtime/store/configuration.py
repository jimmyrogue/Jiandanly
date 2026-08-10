"""Runtime settings, MCP catalog, and model-connection persistence."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiosqlite

from .codec import encode_payload as _encode_payload
from .codec import json_payload as _json_payload
from .database import SqliteDatabase
from .database import configure_connection as _configure_connection
from .database import finish_task_despite_cancellation as _finish_task_despite_cancellation
from .database import utc_now as _now


def _decode_mcp_catalog_row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    record = dict(row)
    try:
        tools = json.loads(str(record.pop("tools_json", "[]")))
    except (json.JSONDecodeError, TypeError):
        tools = []
    record["tools"] = tools if isinstance(tools, list) else []
    record["version"] = int(record["version"])
    return record


class ConfigurationStore(SqliteDatabase):
    async def get_runtime_settings(self) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "SELECT settings_json, version, updated_at FROM local_runtime_settings WHERE id = 1"
            )
        ).fetchone()
        if row is None:
            return None
        return {
            "settings": _json_payload(row["settings_json"]),
            "version": int(row["version"]),
            "updated_at": str(row["updated_at"]),
        }

    async def patch_runtime_settings(
        self,
        patch: dict[str, Any],
        *,
        initial_settings: dict[str, Any],
    ) -> dict[str, Any]:
        now = _now()
        initial_payload = {**initial_settings, **patch}
        patch_json = _encode_payload(patch)
        cursor = await self._conn.execute(
            "INSERT INTO local_runtime_settings (id, settings_json, version, updated_at) "
            "VALUES (1, ?, 1, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "settings_json = json_patch(local_runtime_settings.settings_json, ?), "
            "version = local_runtime_settings.version + 1, "
            "updated_at = excluded.updated_at "
            "WHERE json_patch(local_runtime_settings.settings_json, ?) "
            "IS NOT local_runtime_settings.settings_json "
            "RETURNING settings_json, version, updated_at",
            (
                _encode_payload(initial_payload),
                now,
                patch_json,
                patch_json,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            current = await self.get_runtime_settings()
            assert current is not None
            return current
        assert row is not None
        return {
            "settings": _json_payload(row["settings_json"]),
            "version": int(row["version"]),
            "updated_at": str(row["updated_at"]),
        }

    async def get_mcp_catalog(self, server_name: str) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "SELECT * FROM local_mcp_catalog WHERE server_name = ?",
                (server_name,),
            )
        ).fetchone()
        return _decode_mcp_catalog_row(row)

    async def list_mcp_catalogs(self) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute("SELECT * FROM local_mcp_catalog ORDER BY server_name")
        ).fetchall()
        return [record for row in rows if (record := _decode_mcp_catalog_row(row))]

    async def upsert_mcp_catalog(
        self,
        *,
        server_name: str,
        config_fingerprint: str,
        tools: list[dict[str, Any]],
        status: str,
        error_type: str | None,
    ) -> dict[str, Any]:
        if status not in {"ready", "error", "stale"}:
            raise ValueError("invalid MCP catalog status")
        now = _now()
        cursor = await self._conn.execute(
            "INSERT INTO local_mcp_catalog "
            "(server_name, config_fingerprint, tools_json, status, error_type, "
            "version, updated_at, last_success_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?) "
            "ON CONFLICT(server_name) DO UPDATE SET "
            "config_fingerprint = excluded.config_fingerprint, "
            "tools_json = excluded.tools_json, status = excluded.status, "
            "error_type = excluded.error_type, version = local_mcp_catalog.version + 1, "
            "updated_at = excluded.updated_at, "
            "last_success_at = COALESCE(excluded.last_success_at, local_mcp_catalog.last_success_at) "
            "RETURNING *",
            (
                server_name,
                config_fingerprint,
                json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                status,
                error_type,
                now,
                now if status == "ready" else None,
            ),
        )
        row = await cursor.fetchone()
        record = _decode_mcp_catalog_row(row)
        assert record is not None
        return record

    async def delete_mcp_catalog(self, server_name: str) -> None:
        await self._conn.execute(
            "DELETE FROM local_mcp_catalog WHERE server_name = ?",
            (server_name,),
        )

    async def list_model_connections(self, *, principal_id: str) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT * FROM model_connections WHERE principal_id = ? ORDER BY created_at, id",
                (principal_id,),
            )
        ).fetchall()
        return [dict(row) for row in rows]

    async def get_model_connection(
        self,
        *,
        principal_id: str,
        connection_id: str,
    ) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "SELECT * FROM model_connections WHERE principal_id = ? AND id = ?",
                (principal_id, connection_id),
            )
        ).fetchone()
        return dict(row) if row is not None else None

    async def create_model_connection(
        self,
        *,
        principal_id: str,
        connection_id: str,
        preset_id: str,
        name: str,
        region: str,
        adapter_id: str,
        base_url: str,
        requires_api_key: bool,
        credential_ref: str,
        models: list[dict[str, Any]],
        catalog_status: str,
    ) -> dict[str, Any]:
        now = _now()
        models_json = json.dumps(
            models,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        await self._conn.execute(
            "INSERT INTO model_connections "
            "(principal_id, id, preset_id, name, region, adapter_id, base_url, "
            " requires_api_key, credential_ref, models_json, catalog_status, version, "
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (
                principal_id,
                connection_id,
                preset_id,
                name,
                region,
                adapter_id,
                base_url,
                int(requires_api_key),
                credential_ref,
                models_json,
                catalog_status,
                now,
                now,
            ),
        )
        connection = await self.get_model_connection(
            principal_id=principal_id,
            connection_id=connection_id,
        )
        assert connection is not None
        return connection

    async def delete_model_connection(
        self,
        *,
        principal_id: str,
        connection_id: str,
    ) -> dict[str, Any] | None:
        connection = await self.get_model_connection(
            principal_id=principal_id,
            connection_id=connection_id,
        )
        if connection is None:
            return None
        await self._conn.execute(
            "DELETE FROM model_connections WHERE principal_id = ? AND id = ?",
            (principal_id, connection_id),
        )
        return connection

    async def update_model_connection_catalog(
        self,
        *,
        principal_id: str,
        connection_id: str,
        models: list[dict[str, Any]],
        catalog_status: str,
    ) -> dict[str, Any] | None:
        connection = await self.get_model_connection(
            principal_id=principal_id,
            connection_id=connection_id,
        )
        if connection is None:
            return None
        models_json = json.dumps(
            models,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if (
            connection["models_json"] == models_json
            and connection["catalog_status"] == catalog_status
        ):
            return connection
        cursor = await self._conn.execute(
            "UPDATE model_connections SET models_json = ?, catalog_status = ?, "
            "updated_at = ? "
            "WHERE principal_id = ? AND id = ?",
            (
                models_json,
                catalog_status,
                _now(),
                principal_id,
                connection_id,
            ),
        )
        if cursor.rowcount == 0:
            return None
        return await self.get_model_connection(
            principal_id=principal_id,
            connection_id=connection_id,
        )

    async def replace_model_connection_credential(
        self,
        *,
        principal_id: str,
        connection_id: str,
        credential_ref: str,
        base_url: str,
        models: list[dict[str, Any]],
        catalog_status: str,
    ) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "UPDATE model_connections SET credential_ref = ?, base_url = ?, models_json = ?, "
            "catalog_status = ?, version = version + 1, updated_at = ? "
            "WHERE principal_id = ? AND id = ?",
            (
                credential_ref,
                base_url,
                json.dumps(
                    models,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                catalog_status,
                _now(),
                principal_id,
                connection_id,
            ),
        )
        if cursor.rowcount == 0:
            return None
        return await self.get_model_connection(
            principal_id=principal_id,
            connection_id=connection_id,
        )

    async def replace_official_model_connection(
        self,
        *,
        principal_id: str,
        connection_id: str,
        name: str,
        base_url: str,
        credential_ref: str,
        models: list[dict[str, Any]],
        catalog_status: str,
        capability_bindings: dict[str, dict[str, str]],
        timestamp: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Atomically install one official connection and retire every older one."""
        models_json = json.dumps(
            models,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        async def replace() -> tuple[dict[str, Any], list[dict[str, Any]]]:
            async with aiosqlite.connect(str(self._db_path)) as conn:
                await _configure_connection(conn)
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    previous_rows = await (
                        await conn.execute(
                            "SELECT * FROM model_connections "
                            "WHERE principal_id = ? AND preset_id = 'shejane-official'",
                            (principal_id,),
                        )
                    ).fetchall()
                    previous_connections = [dict(row) for row in previous_rows]
                    previous_ids = {str(row["id"]) for row in previous_connections}
                    await conn.execute(
                        "INSERT INTO model_connections "
                        "(principal_id, id, preset_id, name, region, adapter_id, base_url, "
                        "requires_api_key, credential_ref, models_json, catalog_status, version, "
                        "created_at, updated_at) VALUES (?, ?, ?, ?, 'official', 'openai_chat', "
                        "?, 1, ?, ?, ?, 1, ?, ?)",
                        (
                            principal_id,
                            connection_id,
                            "shejane-official",
                            name,
                            base_url,
                            credential_ref,
                            models_json,
                            catalog_status,
                            timestamp,
                            timestamp,
                        ),
                    )
                    for capability, binding in capability_bindings.items():
                        current = await (
                            await conn.execute(
                                "SELECT connection_id FROM model_capability_bindings "
                                "WHERE principal_id = ? AND capability = ?",
                                (principal_id, capability),
                            )
                        ).fetchone()
                        if (
                            current is not None
                            and str(current["connection_id"]) not in previous_ids
                        ):
                            continue
                        await conn.execute(
                            "INSERT INTO model_capability_bindings "
                            "(principal_id, capability, connection_id, connection_version, model_id, "
                            "protocol, revision, updated_at) VALUES (?, ?, ?, 1, ?, ?, 1, ?) "
                            "ON CONFLICT(principal_id, capability) DO UPDATE SET "
                            "connection_id = excluded.connection_id, "
                            "connection_version = excluded.connection_version, "
                            "model_id = excluded.model_id, protocol = excluded.protocol, "
                            "revision = model_capability_bindings.revision + 1, "
                            "updated_at = excluded.updated_at",
                            (
                                principal_id,
                                capability,
                                connection_id,
                                binding["model_id"],
                                binding["protocol"],
                                timestamp,
                            ),
                        )
                    if previous_ids:
                        await conn.executemany(
                            "DELETE FROM model_connections WHERE principal_id = ? AND id = ?",
                            [(principal_id, previous_id) for previous_id in previous_ids],
                        )
                    await conn.commit()
                except BaseException:
                    if conn.in_transaction:
                        await conn.rollback()
                    raise
            connection = await self.get_model_connection(
                principal_id=principal_id,
                connection_id=connection_id,
            )
            assert connection is not None
            return connection, previous_connections

        return await _finish_task_despite_cancellation(asyncio.create_task(replace()))

    async def list_model_capability_bindings(
        self,
        *,
        principal_id: str,
    ) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT * FROM model_capability_bindings "
                "WHERE principal_id = ? ORDER BY capability",
                (principal_id,),
            )
        ).fetchall()
        return [dict(row) for row in rows]

    async def get_model_capability_binding(
        self,
        *,
        principal_id: str,
        capability: str,
    ) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "SELECT * FROM model_capability_bindings WHERE principal_id = ? AND capability = ?",
                (principal_id, capability),
            )
        ).fetchone()
        return dict(row) if row is not None else None

    async def set_model_capability_binding(
        self,
        *,
        principal_id: str,
        capability: str,
        connection_id: str,
        connection_version: int,
        model_id: str,
        protocol: str,
    ) -> dict[str, Any]:
        cursor = await self._conn.execute(
            "INSERT INTO model_capability_bindings "
            "(principal_id, capability, connection_id, connection_version, model_id, "
            "protocol, revision, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?) "
            "ON CONFLICT(principal_id, capability) DO UPDATE SET "
            "connection_id = excluded.connection_id, "
            "connection_version = excluded.connection_version, "
            "model_id = excluded.model_id, protocol = excluded.protocol, "
            "revision = model_capability_bindings.revision + 1, "
            "updated_at = excluded.updated_at RETURNING *",
            (
                principal_id,
                capability,
                connection_id,
                connection_version,
                model_id,
                protocol,
                _now(),
            ),
        )
        row = await cursor.fetchone()
        assert row is not None
        return dict(row)

    async def create_model_capability_binding_if_absent(
        self,
        *,
        principal_id: str,
        capability: str,
        connection_id: str,
        connection_version: int,
        model_id: str,
        protocol: str,
    ) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "INSERT INTO model_capability_bindings "
            "(principal_id, capability, connection_id, connection_version, model_id, "
            "protocol, revision, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?) "
            "ON CONFLICT(principal_id, capability) DO NOTHING RETURNING *",
            (
                principal_id,
                capability,
                connection_id,
                connection_version,
                model_id,
                protocol,
                _now(),
            ),
        )
        row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def delete_model_capability_binding(
        self,
        *,
        principal_id: str,
        capability: str,
    ) -> dict[str, Any] | None:
        existing = await self.get_model_capability_binding(
            principal_id=principal_id,
            capability=capability,
        )
        if existing is None:
            return None
        await self._conn.execute(
            "DELETE FROM model_capability_bindings WHERE principal_id = ? AND capability = ?",
            (principal_id, capability),
        )
        return existing
