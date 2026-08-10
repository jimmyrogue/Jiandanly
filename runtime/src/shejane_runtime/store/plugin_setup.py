"""Revision-fenced plugin setup flow state."""

from __future__ import annotations

from typing import Any

import aiosqlite

from .database import SqliteDatabase
from .database import configure_connection as _configure_connection
from .database import utc_now as _now
from .errors import PluginStateError


class PluginSetupStore(SqliteDatabase):
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
