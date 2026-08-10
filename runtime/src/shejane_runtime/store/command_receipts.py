"""Immutable command receipts shared by Run, wait, and plugin commands."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from .codec import encode_payload as _encode_payload
from .database import SqliteDatabase
from .database import configure_connection as _configure_connection
from .database import utc_now as _now
from .errors import CommandConflictError


class CommandReceiptStore(SqliteDatabase):
    @staticmethod
    async def _accepted_run_for_command(
        conn: aiosqlite.Connection,
        *,
        principal_id: str,
        command_id: str,
        payload_json: str,
    ) -> dict[str, Any] | None:
        command = await (
            await conn.execute(
                "SELECT payload_json, run_id FROM local_commands WHERE principal_id = ? AND id = ?",
                (principal_id, command_id),
            )
        ).fetchone()
        if command is None:
            return None
        if command["payload_json"] != payload_json:
            raise CommandConflictError(
                f"command {command_id} was already accepted with different content"
            )
        run = await (
            await conn.execute(
                "SELECT r.*, c.id AS command_id, c.client_message_id "
                "FROM local_runs r JOIN local_commands c ON c.run_id = r.id "
                "AND c.principal_id = r.principal_id "
                "WHERE r.id = ? AND c.principal_id = ? AND c.id = ?",
                (command["run_id"], principal_id, command_id),
            )
        ).fetchone()
        if run is None:
            raise RuntimeError(f"command {command_id} references a missing run")
        return dict(run)

    @staticmethod
    async def _accepted_command_receipt_uncommitted(
        conn: aiosqlite.Connection,
        *,
        principal_id: str,
        command_id: str,
        command_type: str,
        payload_json: str,
    ) -> dict[str, Any] | None:
        existing = await (
            await conn.execute(
                "SELECT command_type, payload_json, response_json "
                "FROM local_commands WHERE principal_id = ? AND id = ?",
                (principal_id, command_id),
            )
        ).fetchone()
        if existing is None:
            return None
        if existing["command_type"] != command_type or existing["payload_json"] != payload_json:
            raise CommandConflictError(
                f"command {command_id} was already accepted with different content"
            )
        return json.loads(existing["response_json"])

    @staticmethod
    async def _record_command_receipt_uncommitted(
        conn: aiosqlite.Connection,
        *,
        principal_id: str,
        command_id: str,
        command_type: str,
        payload_json: str,
        receipt: dict[str, Any],
        created_at: str,
        run_id: str | None = None,
    ) -> None:
        await conn.execute(
            "INSERT INTO local_commands "
            "(principal_id, id, command_type, client_message_id, payload_json, "
            "response_json, run_id, created_at) VALUES (?, ?, ?, '', ?, ?, ?, ?)",
            (
                principal_id,
                command_id,
                command_type,
                payload_json,
                _encode_payload(receipt),
                run_id,
                created_at,
            ),
        )

    async def accepted_command_receipt(
        self,
        *,
        principal_id: str,
        command_id: str,
        command_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        return await self._accepted_command_receipt_uncommitted(
            self._conn,
            principal_id=principal_id,
            command_id=command_id,
            command_type=command_type,
            payload_json=_encode_payload(payload),
        )

    async def record_command_receipt(
        self,
        *,
        principal_id: str,
        command_id: str,
        command_type: str,
        payload: dict[str, Any],
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        payload_json = _encode_payload(payload)
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
                    return existing
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
                return receipt
            except BaseException:
                await conn.rollback()
                raise

    async def accepted_run_for_command(
        self,
        *,
        principal_id: str,
        command_id: str,
        client_message_id: str,
        command_payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Return an immutable command receipt before checking mutable resources."""
        return await self._accepted_run_for_command(
            self._conn,
            principal_id=principal_id,
            command_id=command_id,
            payload_json=_encode_payload(
                {"client_message_id": client_message_id, "payload": command_payload}
            ),
        )
