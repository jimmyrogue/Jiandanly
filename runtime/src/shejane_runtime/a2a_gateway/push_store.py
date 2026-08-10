from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from .store_common import A2APushConfigConflictError, _now


class A2APushStore:
    async def create_push_config(
        self,
        *,
        config_id: str,
        peer_id: str,
        tenant: str,
        task_id: str,
        request_fingerprint: str,
        url: str,
        token_ciphertext: str | None,
        auth_scheme: str | None,
        credentials_ciphertext: str | None,
        start_after: int,
        snapshot_payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        async with self._transaction() as conn:
            task = await (
                await conn.execute(
                    "SELECT 1 FROM a2a_tasks WHERE id = ? AND peer_id = ? AND tenant = ?",
                    (task_id, peer_id, tenant),
                )
            ).fetchone()
            if task is None:
                raise KeyError(task_id)
            existing = await (
                await conn.execute(
                    "SELECT * FROM a2a_push_configs WHERE id = ? AND peer_id = ? AND task_id = ?",
                    (config_id, peer_id, task_id),
                )
            ).fetchone()
            if existing is not None:
                if (
                    existing["deleted_at"] is not None
                    or str(existing["task_id"]) != task_id
                    or str(existing["request_fingerprint"]) != request_fingerprint
                ):
                    raise A2APushConfigConflictError(
                        f"push config {config_id} already exists with different content"
                    )
                return dict(existing), False
            active_count = await (
                await conn.execute(
                    "SELECT COUNT(*) FROM a2a_push_configs "
                    "WHERE peer_id = ? AND task_id = ? AND deleted_at IS NULL",
                    (peer_id, task_id),
                )
            ).fetchone()
            if active_count is not None and int(active_count[0]) >= 8:
                raise ValueError("a task may have at most 8 push configurations")
            now = _now()
            await conn.execute(
                "INSERT INTO a2a_push_configs "
                "(id, peer_id, tenant, task_id, request_fingerprint, url, token_ciphertext, "
                "auth_scheme, credentials_ciphertext, created_at, updated_at, deleted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    config_id,
                    peer_id,
                    tenant,
                    task_id,
                    request_fingerprint,
                    url,
                    token_ciphertext,
                    auth_scheme,
                    credentials_ciphertext,
                    now,
                    now,
                ),
            )
            await conn.execute(
                "INSERT INTO a2a_push_cursors (task_id, peer_id, runtime_after, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET "
                "runtime_after = MAX(a2a_push_cursors.runtime_after, excluded.runtime_after), "
                "updated_at = excluded.updated_at",
                (task_id, peer_id, max(0, start_after), now),
            )
            await conn.execute(
                "INSERT INTO a2a_push_outbox "
                "(id, config_id, peer_id, task_id, event_key, payload_json, status, attempts, "
                "available_at, lease_until, last_error, created_at, delivered_at) "
                "VALUES (?, ?, ?, ?, 'snapshot', ?, 'pending', 0, ?, NULL, NULL, ?, NULL)",
                (
                    f"push_{uuid.uuid4().hex}",
                    config_id,
                    peer_id,
                    task_id,
                    json.dumps(snapshot_payload, ensure_ascii=False, separators=(",", ":")),
                    now,
                    now,
                ),
            )
            created = await (
                await conn.execute(
                    "SELECT * FROM a2a_push_configs WHERE id = ? AND peer_id = ? "
                    "AND tenant = ? AND task_id = ? AND deleted_at IS NULL",
                    (config_id, peer_id, tenant, task_id),
                )
            ).fetchone()
            assert created is not None
            return dict(created), True

    async def get_push_config(
        self, *, peer_id: str, tenant: str, task_id: str, config_id: str
    ) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "SELECT * FROM a2a_push_configs WHERE id = ? AND peer_id = ? "
                "AND tenant = ? AND task_id = ? AND deleted_at IS NULL",
                (config_id, peer_id, tenant, task_id),
            )
        ).fetchone()
        return dict(row) if row is not None else None

    async def list_push_configs(
        self, *, peer_id: str, tenant: str, task_id: str
    ) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT * FROM a2a_push_configs WHERE peer_id = ? AND tenant = ? "
                "AND task_id = ? AND deleted_at IS NULL ORDER BY created_at, id",
                (peer_id, tenant, task_id),
            )
        ).fetchall()
        return [dict(row) for row in rows]

    async def delete_push_config(
        self, *, peer_id: str, tenant: str, task_id: str, config_id: str
    ) -> bool:
        now = _now()
        async with self._transaction() as conn:
            cursor = await conn.execute(
                "UPDATE a2a_push_configs SET deleted_at = ?, updated_at = ? "
                "WHERE id = ? AND peer_id = ? AND tenant = ? AND task_id = ? "
                "AND deleted_at IS NULL",
                (now, now, config_id, peer_id, tenant, task_id),
            )
            if cursor.rowcount == 1:
                await conn.execute(
                    "UPDATE a2a_push_outbox SET status = 'canceled', lease_until = NULL "
                    "WHERE config_id = ? AND peer_id = ? AND task_id = ? "
                    "AND status IN ('pending', 'leased')",
                    (config_id, peer_id, task_id),
                )
                existed = True
            else:
                existing = await (
                    await conn.execute(
                        "SELECT 1 FROM a2a_push_configs WHERE id = ? AND peer_id = ? "
                        "AND tenant = ? AND task_id = ?",
                        (config_id, peer_id, tenant, task_id),
                    )
                ).fetchone()
                existed = existing is not None
            return existed

    async def list_push_watch_tasks(self) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT t.*, c.runtime_after FROM a2a_tasks AS t "
                "JOIN a2a_push_cursors AS c ON c.task_id = t.id "
                "WHERE EXISTS (SELECT 1 FROM a2a_push_configs AS p "
                "WHERE p.task_id = t.id AND p.peer_id = t.peer_id AND p.deleted_at IS NULL) "
                "ORDER BY t.created_at, t.id"
            )
        ).fetchall()
        return [dict(row) for row in rows]

    async def record_push_event(
        self,
        *,
        peer_id: str,
        task_id: str,
        event_seq: int,
        payloads: list[dict[str, Any]],
    ) -> None:
        async with self._transaction() as conn:
            cursor = await conn.execute(
                "SELECT runtime_after FROM a2a_push_cursors WHERE task_id = ? AND peer_id = ?",
                (task_id, peer_id),
            )
            row = await cursor.fetchone()
            if row is None or event_seq <= int(row["runtime_after"]):
                return
            configs = await (
                await conn.execute(
                    "SELECT id FROM a2a_push_configs "
                    "WHERE peer_id = ? AND task_id = ? AND deleted_at IS NULL",
                    (peer_id, task_id),
                )
            ).fetchall()
            now = _now()
            for config in configs:
                for index, payload in enumerate(payloads):
                    event_key = f"event:{event_seq:020d}:{index:04d}"
                    await conn.execute(
                        "INSERT OR IGNORE INTO a2a_push_outbox "
                        "(id, config_id, peer_id, task_id, event_key, payload_json, status, "
                        "attempts, available_at, lease_until, last_error, created_at, delivered_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, NULL, ?, NULL)",
                        (
                            f"push_{uuid.uuid4().hex}",
                            config["id"],
                            peer_id,
                            task_id,
                            event_key,
                            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                            now,
                            now,
                        ),
                    )
            await conn.execute(
                "UPDATE a2a_push_cursors SET runtime_after = ?, updated_at = ? "
                "WHERE task_id = ? AND peer_id = ? AND runtime_after < ?",
                (event_seq, now, task_id, peer_id, event_seq),
            )

    async def claim_push_delivery(self, *, lease_seconds: int = 30) -> dict[str, Any] | None:
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        lease_until = datetime.fromtimestamp(now_dt.timestamp() + lease_seconds, tz=UTC).isoformat()
        async with self._transaction() as conn:
            row = await (
                await conn.execute(
                    "SELECT o.*, p.url, p.token_ciphertext, p.auth_scheme, "
                    "p.credentials_ciphertext FROM a2a_push_outbox AS o "
                    "JOIN a2a_push_configs AS p ON p.id = o.config_id "
                    "AND p.peer_id = o.peer_id AND p.task_id = o.task_id "
                    "WHERE p.deleted_at IS NULL AND ("
                    "(o.status = 'pending' AND o.available_at <= ?) OR "
                    "(o.status = 'leased' AND o.lease_until <= ?)) "
                    "ORDER BY o.available_at, o.created_at, o.event_key, o.id LIMIT 1",
                    (now, now),
                )
            ).fetchone()
            if row is None:
                return None
            await conn.execute(
                "UPDATE a2a_push_outbox SET status = 'leased', attempts = attempts + 1, "
                "lease_until = ? WHERE id = ?",
                (lease_until, row["id"]),
            )
            return {**dict(row), "status": "leased", "attempts": int(row["attempts"]) + 1}

    async def settle_push_delivery(self, delivery_id: str) -> None:
        await self._conn.execute(
            "UPDATE a2a_push_outbox SET status = 'delivered', delivered_at = ?, "
            "lease_until = NULL, last_error = NULL WHERE id = ? AND status = 'leased'",
            (_now(), delivery_id),
        )

    async def retry_push_delivery(
        self, delivery_id: str, *, available_at: str, error: str, dead: bool
    ) -> None:
        await self._conn.execute(
            "UPDATE a2a_push_outbox SET status = ?, available_at = ?, lease_until = NULL, "
            "last_error = ? WHERE id = ? AND status = 'leased'",
            ("dead" if dead else "pending", available_at, error[:2048], delivery_id),
        )
