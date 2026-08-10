"""Model capability binding persistence."""

from __future__ import annotations

from typing import Any

from ..database import SqliteDatabase
from ..database import utc_now as _now


class ModelCapabilityBindingStore(SqliteDatabase):
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
