"""Authorized workspace persistence and admission checks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import aiosqlite

from .database import SqliteDatabase
from .database import utc_now as _now
from .ids import new_id as _new_id


class WorkspaceStore(SqliteDatabase):
    # --- workspaces ---

    async def create_workspace(self, *, principal_id: str, path: str, label: str) -> dict[str, Any]:
        ws = {
            "id": _new_id("ws"),
            "principal_id": principal_id,
            "path": path,
            "label": label,
            "created_at": _now(),
            "last_used_at": _now(),
        }
        await self._conn.execute(
            "INSERT INTO local_workspaces "
            "(id, principal_id, path, label, created_at, last_used_at) "
            "VALUES (:id, :principal_id, :path, :label, :created_at, :last_used_at) "
            "ON CONFLICT(principal_id, path) DO NOTHING",
            ws,
        )
        row = await (
            await self._conn.execute(
                "SELECT * FROM local_workspaces WHERE principal_id = ? AND path = ?",
                (principal_id, path),
            )
        ).fetchone()
        assert row is not None
        return dict(row)

    async def list_workspaces(self, *, principal_id: str) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(
            "SELECT * FROM local_workspaces WHERE principal_id = ? ORDER BY last_used_at DESC",
            (principal_id,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def workspace_by_path(self, *, principal_id: str, path: str) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM local_workspaces WHERE principal_id = ? AND path = ?",
            (principal_id, path),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def find_outcome_unknown_tool_receipt_in_lineage(
        self,
        *,
        current_run_id: str,
        tool_name: str,
        arguments_hash: str,
        risk: str,
    ) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "WITH RECURSIVE lineage(id, owner, depth) AS ("
                "SELECT parent_run_id, principal_id, 0 FROM local_runs "
                "WHERE id = ? AND parent_run_id IS NOT NULL UNION ALL "
                "SELECT ancestor.parent_run_id, lineage.owner, lineage.depth + 1 "
                "FROM local_runs AS ancestor JOIN lineage ON ancestor.id = lineage.id "
                "WHERE ancestor.principal_id = lineage.owner "
                "AND ancestor.parent_run_id IS NOT NULL AND lineage.depth < 64"
                ") SELECT local_tool_receipts.* FROM local_tool_receipts "
                "JOIN lineage ON lineage.id = local_tool_receipts.run_id "
                "JOIN local_runs AS ancestor ON ancestor.id = lineage.id "
                "AND ancestor.principal_id = lineage.owner "
                "WHERE local_tool_receipts.tool_name = ? "
                "AND local_tool_receipts.arguments_hash = ? "
                "AND local_tool_receipts.risk = ? "
                "AND local_tool_receipts.status = 'outcome_unknown' "
                "ORDER BY lineage.depth, local_tool_receipts.updated_at DESC LIMIT 1",
                (current_run_id, tool_name, arguments_hash, risk),
            )
        ).fetchone()
        return dict(row) if row else None

    async def workspace_admission_error(self, *, principal_id: str, path: str | None) -> str | None:
        owner_error = await self._workspace_owner_error(
            self._conn,
            principal_id=principal_id,
            path=path,
        )
        return owner_error or await self._workspace_path_error(path)

    @staticmethod
    async def _workspace_owner_error(
        conn: aiosqlite.Connection, *, principal_id: str, path: str | None
    ) -> str | None:
        if path is None:
            return None
        workspace = await (
            await conn.execute(
                "SELECT 1 FROM local_workspaces WHERE principal_id = ? AND path = ?",
                (principal_id, path),
            )
        ).fetchone()
        if workspace is None:
            return "workspace is not authorized"
        return None

    @staticmethod
    async def _workspace_path_error(path: str | None) -> str | None:
        if path is None:
            return None
        return await asyncio.to_thread(WorkspaceStore._workspace_path_error_sync, path)

    @staticmethod
    def _workspace_path_error_sync(path: str) -> str | None:
        root = Path(path)
        try:
            if root.is_symlink() or str(root.resolve(strict=True)) != path or not root.is_dir():
                return "workspace is no longer available"
        except OSError:
            return "workspace is no longer available"
        return None

    async def delete_workspace(self, *, principal_id: str, workspace_id: str) -> bool:
        cursor = await self._conn.execute(
            "DELETE FROM local_workspaces WHERE principal_id = ? AND id = ?",
            (principal_id, workspace_id),
        )
        return cursor.rowcount > 0

    async def touch_workspace(self, *, principal_id: str, workspace_id: str) -> None:
        await self._conn.execute(
            "UPDATE local_workspaces SET last_used_at = ? WHERE principal_id = ? AND id = ?",
            (_now(), principal_id, workspace_id),
        )
