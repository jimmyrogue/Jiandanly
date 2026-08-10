"""Runtime settings and MCP catalog persistence facade."""

from __future__ import annotations

import json
from typing import Any

from ..codec import encode_payload as _encode_payload
from ..codec import json_payload as _json_payload
from ..database import utc_now as _now
from .capability_bindings import ModelCapabilityBindingStore
from .connections import ModelConnectionStore


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


class ConfigurationStore(ModelConnectionStore, ModelCapabilityBindingStore):
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
