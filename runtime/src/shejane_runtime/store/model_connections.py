"""Model-service connection persistence."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiosqlite

from .database import SqliteDatabase
from .database import configure_connection as _configure_connection
from .database import finish_task_despite_cancellation as _finish_task_despite_cancellation
from .database import utc_now as _now


class ModelConnectionStore(SqliteDatabase):
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
