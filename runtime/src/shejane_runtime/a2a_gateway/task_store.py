from __future__ import annotations

import json
from typing import Any

import aiosqlite

from .store_common import A2AMessageConflictError, _now


def _decode_message(row: aiosqlite.Row) -> dict[str, Any]:
    record = dict(row)
    record["message"] = json.loads(str(record.pop("message_json")))
    return record


class A2ATaskStore:
    async def prepare_message(
        self,
        *,
        peer_id: str,
        tenant: str,
        message_id: str,
        task_id: str | None,
        context_id: str | None,
        reference_task_ids: list[str],
        request_fingerprint: str,
        message: dict[str, Any],
        new_task_id: str,
        new_context_id: str,
        runtime_thread_id: str,
        runtime_command_id: str,
        runtime_client_message_id: str,
        output_mode: str = "text/plain",
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        async with self._transaction() as conn:
            existing = await (
                await conn.execute(
                    "SELECT * FROM a2a_messages WHERE peer_id = ? AND message_id = ?",
                    (peer_id, message_id),
                )
            ).fetchone()
            if existing is not None:
                if str(existing["request_fingerprint"]) != request_fingerprint:
                    raise A2AMessageConflictError(
                        f"message {message_id} was already accepted with different content"
                    )
                task = await (
                    await conn.execute(
                        "SELECT * FROM a2a_tasks WHERE id = ? AND peer_id = ? AND tenant = ?",
                        (existing["task_id"], peer_id, tenant),
                    )
                ).fetchone()
                if task is None:
                    raise RuntimeError(f"message {message_id} references a missing task")
                return dict(task), _decode_message(existing), False

            selected_task: aiosqlite.Row | None = None
            selected_context_id = context_id
            if task_id:
                selected_task = await (
                    await conn.execute(
                        "SELECT * FROM a2a_tasks WHERE id = ? AND peer_id = ? AND tenant = ?",
                        (task_id, peer_id, tenant),
                    )
                ).fetchone()
                if selected_task is None:
                    raise KeyError(task_id)
                inferred_context = str(selected_task["context_id"])
                if selected_context_id and selected_context_id != inferred_context:
                    raise ValueError("message context_id does not match its task")
                selected_context_id = inferred_context
            elif selected_context_id:
                known_context = await (
                    await conn.execute(
                        "SELECT 1 FROM a2a_tasks "
                        "WHERE peer_id = ? AND tenant = ? AND context_id = ? LIMIT 1",
                        (peer_id, tenant, selected_context_id),
                    )
                ).fetchone()
                if known_context is None:
                    raise KeyError(selected_context_id)
            else:
                selected_context_id = new_context_id

            if reference_task_ids:
                placeholders = ",".join("?" for _ in reference_task_ids)
                rows = await (
                    await conn.execute(
                        f"SELECT id FROM a2a_tasks WHERE peer_id = ? AND tenant = ? "
                        f"AND id IN ({placeholders})",
                        (peer_id, tenant, *reference_task_ids),
                    )
                ).fetchall()
                if {str(row["id"]) for row in rows} != set(reference_task_ids):
                    raise KeyError("referenced task not found")

            now = _now()
            created_task = selected_task is None
            if created_task:
                await conn.execute(
                    "INSERT INTO a2a_tasks "
                    "(id, peer_id, tenant, context_id, runtime_run_id, runtime_thread_id, "
                    "create_command_id, create_client_message_id, output_mode, admission_status, "
                    "rejection_reason, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, 'pending', NULL, ?, ?)",
                    (
                        new_task_id,
                        peer_id,
                        tenant,
                        selected_context_id,
                        runtime_thread_id,
                        runtime_command_id,
                        runtime_client_message_id,
                        output_mode,
                        now,
                        now,
                    ),
                )
                task_id = new_task_id
            else:
                task_id = str(selected_task["id"])

            normalized_message = dict(message)
            normalized_message["taskId"] = task_id
            normalized_message["contextId"] = selected_context_id
            await conn.execute(
                "INSERT INTO a2a_messages "
                "(peer_id, message_id, tenant, task_id, context_id, request_fingerprint, "
                "message_json, runtime_command_id, runtime_instruction_id, delivery_status, "
                "rejection_reason, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'pending', NULL, ?, ?)",
                (
                    peer_id,
                    message_id,
                    tenant,
                    task_id,
                    selected_context_id,
                    request_fingerprint,
                    json.dumps(normalized_message, ensure_ascii=False, separators=(",", ":")),
                    runtime_command_id,
                    now,
                    now,
                ),
            )
            task = await (
                await conn.execute(
                    "SELECT * FROM a2a_tasks WHERE id = ? AND peer_id = ? AND tenant = ?",
                    (task_id, peer_id, tenant),
                )
            ).fetchone()
            stored_message = await (
                await conn.execute(
                    "SELECT * FROM a2a_messages WHERE peer_id = ? AND message_id = ?",
                    (peer_id, message_id),
                )
            ).fetchone()
            assert task is not None and stored_message is not None
            return dict(task), _decode_message(stored_message), created_task

    async def get_task(self, *, peer_id: str, tenant: str, task_id: str) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "SELECT * FROM a2a_tasks WHERE id = ? AND peer_id = ? AND tenant = ?",
                (task_id, peer_id, tenant),
            )
        ).fetchone()
        return dict(row) if row is not None else None

    async def list_tasks(self, *, peer_id: str, tenant: str) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT * FROM a2a_tasks WHERE peer_id = ? AND tenant = ? "
                "ORDER BY created_at DESC, id DESC",
                (peer_id, tenant),
            )
        ).fetchall()
        return [dict(row) for row in rows]

    async def get_message(self, *, peer_id: str, message_id: str) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "SELECT * FROM a2a_messages WHERE peer_id = ? AND message_id = ?",
                (peer_id, message_id),
            )
        ).fetchone()
        return _decode_message(row) if row is not None else None

    async def list_task_messages(self, *, peer_id: str, task_id: str) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT * FROM a2a_messages WHERE peer_id = ? AND task_id = ? "
                "ORDER BY created_at, message_id",
                (peer_id, task_id),
            )
        ).fetchall()
        return [_decode_message(row) for row in rows]

    async def register_artifact(
        self,
        *,
        artifact_id: str,
        peer_id: str,
        tenant: str,
        task_id: str,
        runtime_artifact_id: str,
        title: str,
        media_type: str,
        size_bytes: int,
        sha256: str | None,
        storage_kind: str,
        inline_content: str | None,
        created_at: str,
    ) -> dict[str, Any]:
        await self._conn.execute(
            "INSERT INTO a2a_artifacts "
            "(id, peer_id, tenant, task_id, runtime_artifact_id, title, media_type, "
            "size_bytes, sha256, storage_kind, inline_content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(peer_id, task_id, runtime_artifact_id) DO NOTHING",
            (
                artifact_id,
                peer_id,
                tenant,
                task_id,
                runtime_artifact_id,
                title,
                media_type,
                size_bytes,
                sha256,
                storage_kind,
                inline_content,
                created_at,
            ),
        )
        row = await (
            await self._conn.execute(
                "SELECT * FROM a2a_artifacts "
                "WHERE peer_id = ? AND task_id = ? AND runtime_artifact_id = ?",
                (peer_id, task_id, runtime_artifact_id),
            )
        ).fetchone()
        assert row is not None
        return dict(row)

    async def get_artifact(
        self, *, peer_id: str, tenant: str, artifact_id: str
    ) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "SELECT * FROM a2a_artifacts WHERE id = ? AND peer_id = ? AND tenant = ?",
                (artifact_id, peer_id, tenant),
            )
        ).fetchone()
        return dict(row) if row is not None else None

    async def list_task_artifacts(self, *, peer_id: str, task_id: str) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT * FROM a2a_artifacts WHERE peer_id = ? AND task_id = ? "
                "ORDER BY created_at, id",
                (peer_id, task_id),
            )
        ).fetchall()
        return [dict(row) for row in rows]

    async def settle_task_admission(
        self, *, peer_id: str, task_id: str, runtime_run_id: str
    ) -> dict[str, Any]:
        now = _now()
        cursor = await self._conn.execute(
            "UPDATE a2a_tasks SET runtime_run_id = ?, admission_status = 'accepted', "
            "rejection_reason = NULL, updated_at = ? "
            "WHERE id = ? AND peer_id = ? AND admission_status IN ('pending', 'accepted') "
            "AND (runtime_run_id IS NULL OR runtime_run_id = ?)",
            (runtime_run_id, now, task_id, peer_id, runtime_run_id),
        )
        if cursor.rowcount != 1:
            raise A2AMessageConflictError("task admission settled with a different Runtime Run")
        row = await (
            await self._conn.execute(
                "SELECT * FROM a2a_tasks WHERE id = ? AND peer_id = ?",
                (task_id, peer_id),
            )
        ).fetchone()
        assert row is not None
        return dict(row)

    async def settle_message_delivery(
        self,
        *,
        peer_id: str,
        message_id: str,
        runtime_instruction_id: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        cursor = await self._conn.execute(
            "UPDATE a2a_messages SET delivery_status = 'accepted', "
            "runtime_instruction_id = COALESCE(runtime_instruction_id, ?), "
            "rejection_reason = NULL, updated_at = ? "
            "WHERE peer_id = ? AND message_id = ? AND delivery_status IN ('pending', 'accepted') "
            "AND (runtime_instruction_id IS NULL OR runtime_instruction_id = ?)",
            (runtime_instruction_id, now, peer_id, message_id, runtime_instruction_id),
        )
        if cursor.rowcount != 1:
            raise A2AMessageConflictError("message delivery settled with a different instruction")
        message = await self.get_message(peer_id=peer_id, message_id=message_id)
        assert message is not None
        return message

    async def reject_task_admission(
        self, *, peer_id: str, task_id: str, reason: str
    ) -> dict[str, Any]:
        now = _now()
        cursor = await self._conn.execute(
            "UPDATE a2a_tasks SET admission_status = 'rejected', rejection_reason = ?, "
            "updated_at = ? WHERE id = ? AND peer_id = ? AND admission_status = 'pending'",
            (reason[:2048], now, task_id, peer_id),
        )
        if cursor.rowcount != 1:
            task = await (
                await self._conn.execute(
                    "SELECT * FROM a2a_tasks WHERE id = ? AND peer_id = ?",
                    (task_id, peer_id),
                )
            ).fetchone()
            if task is None or task["admission_status"] != "rejected":
                raise A2AMessageConflictError("task admission could not be rejected")
        row = await (
            await self._conn.execute(
                "SELECT * FROM a2a_tasks WHERE id = ? AND peer_id = ?",
                (task_id, peer_id),
            )
        ).fetchone()
        assert row is not None
        return dict(row)

    async def reject_message_delivery(
        self, *, peer_id: str, message_id: str, reason: str
    ) -> dict[str, Any]:
        now = _now()
        cursor = await self._conn.execute(
            "UPDATE a2a_messages SET delivery_status = 'rejected', rejection_reason = ?, "
            "updated_at = ? WHERE peer_id = ? AND message_id = ? AND delivery_status = 'pending'",
            (reason[:2048], now, peer_id, message_id),
        )
        if cursor.rowcount != 1:
            message = await self.get_message(peer_id=peer_id, message_id=message_id)
            if message is None or message["delivery_status"] != "rejected":
                raise A2AMessageConflictError("message delivery could not be rejected")
        message = await self.get_message(peer_id=peer_id, message_id=message_id)
        assert message is not None
        return message
