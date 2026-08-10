"""Durable same-root Agent mailbox persistence."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite

from .codec import encode_payload as _encode_payload
from .codec import json_payload as _json_payload
from .database import CURRENT_EXECUTION_LEASE as _CURRENT_EXECUTION_LEASE
from .database import LeaseFenceError, SqliteDatabase
from .database import utc_now as _now
from .errors import CommandConflictError, RunAdmissionError, ToolReceiptStateError
from .events import TERMINAL_RUN_STATUSES as _TERMINAL_RUN_STATUSES
from .ids import new_id as _new_id

MAX_AGENT_MAILBOX_PENDING = 32
MAX_AGENT_MAILBOX_MESSAGES_PER_ROOT = 512
MAX_AGENT_MAILBOX_HOPS = 8
MAX_AGENT_MAILBOX_ARTIFACT_REFS = 16
MAX_AGENT_MAILBOX_TEXT_BYTES = 32 * 1024
MAX_AGENT_MAILBOX_DATA_BYTES = 16 * 1024


def _agent_message_projection(row: dict[str, Any]) -> dict[str, Any]:
    projected = dict(row)
    projected["data"] = _json_payload(projected.pop("data_json", None))
    try:
        artifact_refs = json.loads(str(projected.pop("artifact_refs_json", "[]")))
    except (json.JSONDecodeError, TypeError):
        artifact_refs = []
    projected["artifact_refs"] = artifact_refs if isinstance(artifact_refs, list) else []
    projected["sequence"] = int(projected["sequence"])
    projected["hop_count"] = int(projected["hop_count"])
    projected["ttl_seconds"] = int(projected["ttl_seconds"])
    return projected


def _normalize_agent_message_content(
    *,
    kind: str,
    text: str,
    data: dict[str, Any],
    artifact_refs: Sequence[str],
    ttl_seconds: int,
) -> tuple[str, str, str, str, int]:
    normalized_kind = str(kind).strip().lower()
    if normalized_kind not in {"request", "question", "update", "result", "cancel"}:
        raise ValueError("Agent message kind is invalid")
    normalized_text = str(text).strip()
    if len(normalized_text.encode("utf-8")) > MAX_AGENT_MAILBOX_TEXT_BYTES:
        raise ValueError("Agent message text is too large")
    if not isinstance(data, dict):
        raise ValueError("Agent message data must be an object")
    try:
        data_json = json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Agent message data must be JSON-compatible") from exc
    if len(data_json.encode("utf-8")) > MAX_AGENT_MAILBOX_DATA_BYTES:
        raise ValueError("Agent message data is too large")
    if isinstance(artifact_refs, (str, bytes)):
        raise ValueError("Agent message artifact_refs must be a list")
    normalized_refs = list(dict.fromkeys(str(ref).strip() for ref in artifact_refs))
    if len(normalized_refs) > MAX_AGENT_MAILBOX_ARTIFACT_REFS or any(
        not ref or len(ref) > 512 for ref in normalized_refs
    ):
        raise ValueError("Agent message artifact_refs are invalid")
    if not normalized_text and not data and not normalized_refs:
        raise ValueError("Agent message content is empty")
    if isinstance(ttl_seconds, bool) or not 60 <= int(ttl_seconds) <= 24 * 60 * 60:
        raise ValueError("Agent message ttl_seconds must be between 60 and 86400")
    return (
        normalized_kind,
        normalized_text,
        data_json,
        _encode_payload(normalized_refs),
        int(ttl_seconds),
    )


class AgentMessageStore(SqliteDatabase):
    @staticmethod
    async def _require_mailbox_receipt_uncommitted(
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        tool_name: str,
        operation_id: str | None,
    ) -> None:
        lease = _CURRENT_EXECUTION_LEASE.get()
        if lease is None or lease.run_id != run_id:
            raise LeaseFenceError("Agent mailbox mutation requires the sender execution lease")
        execution_attempt_id = f"{lease.job_id}:{lease.lease_generation}"
        if operation_id is None:
            rows = await (
                await conn.execute(
                    "SELECT operation_id FROM local_tool_receipts WHERE run_id = ? "
                    "AND tool_name = ? AND status = 'running' AND execution_attempt_id = ?",
                    (run_id, tool_name, execution_attempt_id),
                )
            ).fetchall()
            if len(rows) != 1:
                raise ToolReceiptStateError(
                    f"Agent mailbox mutation requires one running {tool_name} receipt"
                )
            return
        receipt = await (
            await conn.execute(
                "SELECT tool_name, status, execution_attempt_id FROM local_tool_receipts "
                "WHERE operation_id = ? AND run_id = ?",
                (operation_id, run_id),
            )
        ).fetchone()
        if (
            receipt is None
            or str(receipt["tool_name"]) != tool_name
            or str(receipt["status"]) != "running"
        ):
            raise ToolReceiptStateError(
                f"Agent mailbox mutation requires its running {tool_name} receipt"
            )
        if str(receipt["execution_attempt_id"]) != execution_attempt_id:
            raise LeaseFenceError(f"{tool_name} receipt belongs to a stale execution attempt")

    async def _append_agent_message_event_uncommitted(
        self,
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        event_type: str,
        message: dict[str, Any],
        created_at: str,
    ) -> None:
        event = await self._append_event_uncommitted(
            conn,
            run_id,
            event_type,
            payload_json=_encode_payload(_agent_message_projection(message)),
            created_at=created_at,
        )
        await self._touch_thread_for_run_event_uncommitted(
            conn,
            run_id=run_id,
            change_type=event_type,
            event_high_watermark=int(event["seq"]),
            changed_at=created_at,
        )

    async def _expire_agent_messages_uncommitted(
        self,
        conn: aiosqlite.Connection,
        *,
        recipient_run_id: str,
        now: str,
    ) -> None:
        expired = await (
            await conn.execute(
                "SELECT * FROM local_agent_messages WHERE recipient_run_id = ? "
                "AND status IN ('queued', 'delivered') AND deadline_at <= ? "
                "ORDER BY created_at, id",
                (recipient_run_id, now),
            )
        ).fetchall()
        if not expired:
            return
        await conn.execute(
            "UPDATE local_agent_messages SET status = 'expired' WHERE recipient_run_id = ? "
            "AND status IN ('queued', 'delivered') AND deadline_at <= ?",
            (recipient_run_id, now),
        )
        for row in expired:
            message = {**dict(row), "status": "expired"}
            await self._append_agent_message_event_uncommitted(
                conn,
                run_id=recipient_run_id,
                event_type="agent.message.expired",
                message=message,
                created_at=now,
            )

    async def _insert_agent_message_uncommitted(
        self,
        conn: aiosqlite.Connection,
        *,
        sender_run_id: str,
        sender_operation_id: str,
        recipient_run_id: str,
        kind: str,
        text: str,
        data_json: str,
        artifact_refs_json: str,
        ttl_seconds: int,
        correlation_id: str | None,
        in_reply_to: str | None,
        sequence: int,
        hop_count: int,
        tool_name: str,
    ) -> tuple[dict[str, Any], bool]:
        existing = await (
            await conn.execute(
                "SELECT * FROM local_agent_messages WHERE sender_operation_id = ?",
                (sender_operation_id,),
            )
        ).fetchone()
        if existing is not None:
            record = dict(existing)
            expected_correlation = correlation_id or str(record["id"])
            if any(
                (str(record[key]) if record[key] is not None else None)
                != (str(value) if value is not None else None)
                for key, value in (
                    ("sender_run_id", sender_run_id),
                    ("recipient_run_id", recipient_run_id),
                    ("kind", kind),
                    ("text", text),
                    ("data_json", data_json),
                    ("artifact_refs_json", artifact_refs_json),
                    ("ttl_seconds", ttl_seconds),
                    ("correlation_id", expected_correlation),
                    ("in_reply_to", in_reply_to),
                    ("sequence", sequence),
                    ("hop_count", hop_count),
                )
            ):
                raise CommandConflictError(
                    f"mailbox operation {sender_operation_id} was reused with a different message"
                )
            return _agent_message_projection(record), False

        await self._require_mailbox_receipt_uncommitted(
            conn,
            run_id=sender_run_id,
            tool_name=tool_name,
            operation_id=sender_operation_id,
        )
        if sender_run_id == recipient_run_id:
            raise RunAdmissionError(
                "agent_message_self_send",
                "an Agent cannot send a mailbox message to itself",
            )
        rows = await (
            await conn.execute(
                "SELECT id, principal_id, run_kind, root_run_id, parent_run_id, "
                "collaboration_depth, status FROM local_runs WHERE id IN (?, ?)",
                (sender_run_id, recipient_run_id),
            )
        ).fetchall()
        by_id = {str(row["id"]): dict(row) for row in rows}
        if sender_run_id not in by_id or recipient_run_id not in by_id:
            raise RunAdmissionError(
                "agent_message_run_not_found",
                "Agent mailbox sender or recipient does not exist",
            )
        sender = by_id[sender_run_id]
        recipient = by_id[recipient_run_id]
        sender_root = str(sender.get("root_run_id") or sender_run_id)
        recipient_root = str(recipient.get("root_run_id") or recipient_run_id)
        if sender_root != recipient_root:
            raise RunAdmissionError(
                "agent_message_foreign_root",
                "Agent mailbox participants must share one collaboration root",
            )
        if sender["principal_id"] != recipient["principal_id"]:
            raise RunAdmissionError(
                "agent_message_foreign_principal",
                "Agent mailbox participants must share one principal",
            )
        if sender["run_kind"] != "child" and recipient["run_kind"] != "child":
            raise RunAdmissionError(
                "agent_message_requires_child",
                "Agent mailbox messages require a durable child participant",
            )
        for participant in (sender, recipient):
            if participant["run_kind"] == "child" and (
                str(participant.get("parent_run_id") or "") != sender_root
                or int(participant.get("collaboration_depth") or 0) != 1
            ):
                raise RunAdmissionError(
                    "agent_message_invalid_topology",
                    "Agent mailbox participants must belong to the direct-child topology",
                )
        artifact_refs = json.loads(artifact_refs_json)
        if artifact_refs:
            placeholders = ",".join("?" for _ in artifact_refs)
            authorized_artifacts = await (
                await conn.execute(
                    "SELECT a.id FROM local_artifacts a JOIN local_runs r ON r.id = a.run_id "
                    f"WHERE a.id IN ({placeholders}) AND COALESCE(r.root_run_id, r.id) = ?",
                    (*artifact_refs, sender_root),
                )
            ).fetchall()
            authorized_ids = {str(row["id"]) for row in authorized_artifacts}
            if any(ref not in authorized_ids for ref in artifact_refs):
                raise RunAdmissionError(
                    "agent_message_artifact_forbidden",
                    "Agent mailbox artifact references must belong to the collaboration root",
                )
        if str(recipient["status"]) in _TERMINAL_RUN_STATUSES:
            raise RunAdmissionError(
                "agent_message_recipient_terminal",
                "Agent mailbox recipient is already terminal",
            )

        now = _now()
        await self._expire_agent_messages_uncommitted(
            conn,
            recipient_run_id=recipient_run_id,
            now=now,
        )
        pending = await (
            await conn.execute(
                "SELECT COUNT(*) FROM local_agent_messages WHERE recipient_run_id = ? "
                "AND status IN ('queued', 'delivered')",
                (recipient_run_id,),
            )
        ).fetchone()
        if int(pending[0]) >= MAX_AGENT_MAILBOX_PENDING:
            raise RunAdmissionError(
                "agent_message_backpressure",
                "Agent mailbox backpressure limit is reached",
            )
        root_total = await (
            await conn.execute(
                "SELECT COUNT(*) FROM local_agent_messages WHERE root_run_id = ?",
                (sender_root,),
            )
        ).fetchone()
        if int(root_total[0]) >= MAX_AGENT_MAILBOX_MESSAGES_PER_ROOT:
            raise RunAdmissionError(
                "agent_message_root_budget_exhausted",
                "Agent mailbox message budget is exhausted for this collaboration root",
            )

        message_id = _new_id("agent_message")
        record: dict[str, Any] = {
            "id": message_id,
            "root_run_id": sender_root,
            "sender_run_id": sender_run_id,
            "recipient_run_id": recipient_run_id,
            "sender_operation_id": sender_operation_id,
            "kind": kind,
            "text": text,
            "data_json": data_json,
            "artifact_refs_json": artifact_refs_json,
            "correlation_id": correlation_id or message_id,
            "in_reply_to": in_reply_to,
            "sequence": sequence,
            "hop_count": hop_count,
            "status": "queued",
            "ttl_seconds": ttl_seconds,
            "deadline_at": (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat(),
            "created_at": now,
            "delivered_at": None,
            "acknowledged_at": None,
        }
        await conn.execute(
            "INSERT INTO local_agent_messages "
            "(id, root_run_id, sender_run_id, recipient_run_id, sender_operation_id, kind, "
            "text, data_json, artifact_refs_json, correlation_id, in_reply_to, sequence, "
            "hop_count, status, ttl_seconds, deadline_at, created_at, delivered_at, "
            "acknowledged_at) VALUES (:id, :root_run_id, :sender_run_id, :recipient_run_id, "
            ":sender_operation_id, :kind, :text, :data_json, :artifact_refs_json, "
            ":correlation_id, :in_reply_to, :sequence, :hop_count, :status, :ttl_seconds, "
            ":deadline_at, :created_at, :delivered_at, :acknowledged_at)",
            record,
        )
        await self._append_agent_message_event_uncommitted(
            conn,
            run_id=sender_run_id,
            event_type="agent.message.sent",
            message=record,
            created_at=now,
        )
        return _agent_message_projection(record), True

    async def send_agent_message(
        self,
        *,
        sender_run_id: str,
        sender_operation_id: str,
        recipient_run_id: str,
        kind: str,
        text: str,
        data: dict[str, Any],
        artifact_refs: Sequence[str],
        ttl_seconds: int,
    ) -> tuple[dict[str, Any], bool]:
        normalized = _normalize_agent_message_content(
            kind=kind,
            text=text,
            data=data,
            artifact_refs=artifact_refs,
            ttl_seconds=ttl_seconds,
        )
        async with self.run_write_transaction(sender_run_id) as conn:
            return await self._insert_agent_message_uncommitted(
                conn,
                sender_run_id=sender_run_id,
                sender_operation_id=sender_operation_id,
                recipient_run_id=recipient_run_id,
                kind=normalized[0],
                text=normalized[1],
                data_json=normalized[2],
                artifact_refs_json=normalized[3],
                ttl_seconds=normalized[4],
                correlation_id=None,
                in_reply_to=None,
                sequence=1,
                hop_count=0,
                tool_name="mailbox.send",
            )

    async def reply_agent_message(
        self,
        *,
        sender_run_id: str,
        sender_operation_id: str,
        in_reply_to: str,
        kind: str,
        text: str,
        data: dict[str, Any],
        artifact_refs: Sequence[str],
        ttl_seconds: int,
    ) -> tuple[dict[str, Any], bool]:
        normalized = _normalize_agent_message_content(
            kind=kind,
            text=text,
            data=data,
            artifact_refs=artifact_refs,
            ttl_seconds=ttl_seconds,
        )
        async with self.run_write_transaction(sender_run_id) as conn:
            replay = await (
                await conn.execute(
                    "SELECT * FROM local_agent_messages WHERE sender_operation_id = ?",
                    (sender_operation_id,),
                )
            ).fetchone()
            original = await (
                await conn.execute(
                    "SELECT * FROM local_agent_messages WHERE id = ?",
                    (in_reply_to,),
                )
            ).fetchone()
            if original is None:
                raise RunAdmissionError(
                    "agent_message_not_found",
                    "Agent mailbox reply target does not exist",
                )
            original_record = dict(original)
            if str(original_record["recipient_run_id"]) != sender_run_id:
                raise RunAdmissionError(
                    "agent_message_reply_forbidden",
                    "an Agent can only reply to a message addressed to it",
                )
            if str(original_record["kind"]) not in {"request", "question", "update"}:
                raise RunAdmissionError(
                    "agent_message_reply_terminal",
                    "result and cancel messages cannot be replied to",
                )
            if replay is None:
                thread_head = await (
                    await conn.execute(
                        "SELECT MAX(sequence), MAX(hop_count) FROM local_agent_messages "
                        "WHERE correlation_id = ?",
                        (original_record["correlation_id"],),
                    )
                ).fetchone()
                next_sequence = int(thread_head[0] or 0) + 1
                next_hop = int(thread_head[1] or 0) + 1
            else:
                next_sequence = int(replay["sequence"])
                next_hop = int(replay["hop_count"])
            if next_hop > MAX_AGENT_MAILBOX_HOPS:
                raise RunAdmissionError(
                    "agent_message_hop_limit",
                    "Agent mailbox conversation hop limit is exhausted",
                )
            return await self._insert_agent_message_uncommitted(
                conn,
                sender_run_id=sender_run_id,
                sender_operation_id=sender_operation_id,
                recipient_run_id=str(original_record["sender_run_id"]),
                kind=normalized[0],
                text=normalized[1],
                data_json=normalized[2],
                artifact_refs_json=normalized[3],
                ttl_seconds=normalized[4],
                correlation_id=str(original_record["correlation_id"]),
                in_reply_to=in_reply_to,
                sequence=next_sequence,
                hop_count=next_hop,
                tool_name="mailbox.reply",
            )

    async def deliver_agent_messages(self, recipient_run_id: str) -> list[dict[str, Any]]:
        lease = _CURRENT_EXECUTION_LEASE.get()
        if lease is None or lease.run_id != recipient_run_id:
            raise LeaseFenceError("Agent mailbox delivery requires the recipient execution lease")
        async with self.run_write_transaction(recipient_run_id) as conn:
            now = _now()
            await self._expire_agent_messages_uncommitted(
                conn,
                recipient_run_id=recipient_run_id,
                now=now,
            )
            queued = await (
                await conn.execute(
                    "SELECT * FROM local_agent_messages WHERE recipient_run_id = ? "
                    "AND status = 'queued' ORDER BY created_at, id",
                    (recipient_run_id,),
                )
            ).fetchall()
            for row in queued:
                message = {**dict(row), "status": "delivered", "delivered_at": now}
                await conn.execute(
                    "UPDATE local_agent_messages SET status = 'delivered', delivered_at = ? "
                    "WHERE id = ? AND status = 'queued'",
                    (now, row["id"]),
                )
                await self._append_agent_message_event_uncommitted(
                    conn,
                    run_id=recipient_run_id,
                    event_type="agent.message.received",
                    message=message,
                    created_at=now,
                )
            rows = await (
                await conn.execute(
                    "SELECT * FROM local_agent_messages WHERE recipient_run_id = ? "
                    "AND status = 'delivered' ORDER BY created_at, id",
                    (recipient_run_id,),
                )
            ).fetchall()
            return [_agent_message_projection(dict(row)) for row in rows]

    async def ack_agent_messages(
        self,
        *,
        recipient_run_id: str,
        message_ids: Sequence[str],
        operation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        normalized_ids = list(dict.fromkeys(str(message_id) for message_id in message_ids))
        if not normalized_ids or len(normalized_ids) > MAX_AGENT_MAILBOX_PENDING:
            raise ValueError("Agent mailbox acknowledgement message_ids are invalid")
        async with self.run_write_transaction(recipient_run_id) as conn:
            await self._require_mailbox_receipt_uncommitted(
                conn,
                run_id=recipient_run_id,
                tool_name="mailbox.ack",
                operation_id=operation_id,
            )
            placeholders = ",".join("?" for _ in normalized_ids)
            rows = await (
                await conn.execute(
                    f"SELECT * FROM local_agent_messages WHERE id IN ({placeholders})",
                    normalized_ids,
                )
            ).fetchall()
            by_id = {str(row["id"]): dict(row) for row in rows}
            if any(message_id not in by_id for message_id in normalized_ids):
                raise RunAdmissionError(
                    "agent_message_not_found",
                    "Agent mailbox acknowledgement target does not exist",
                )
            now = _now()
            for message_id in normalized_ids:
                record = by_id[message_id]
                if str(record["recipient_run_id"]) != recipient_run_id:
                    raise RunAdmissionError(
                        "agent_message_ack_forbidden",
                        "an Agent can only acknowledge a message addressed to it",
                    )
                if str(record["status"]) == "acknowledged":
                    continue
                if str(record["status"]) != "delivered":
                    raise RunAdmissionError(
                        "agent_message_not_delivered",
                        "Agent mailbox message must be delivered before acknowledgement",
                    )
                await conn.execute(
                    "UPDATE local_agent_messages SET status = 'acknowledged', "
                    "acknowledged_at = ? WHERE id = ? AND status = 'delivered'",
                    (now, message_id),
                )
                record.update(status="acknowledged", acknowledged_at=now)
                await self._append_agent_message_event_uncommitted(
                    conn,
                    run_id=recipient_run_id,
                    event_type="agent.message.acknowledged",
                    message=record,
                    created_at=now,
                )
            return [_agent_message_projection(by_id[message_id]) for message_id in normalized_ids]

    async def list_agent_inbox(self, run_id: str) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT * FROM local_agent_messages WHERE recipient_run_id = ? "
                "ORDER BY created_at, id",
                (run_id,),
            )
        ).fetchall()
        return [_agent_message_projection(dict(row)) for row in rows]

    async def list_agent_outbox(self, run_id: str) -> list[dict[str, Any]]:
        rows = await (
            await self._conn.execute(
                "SELECT * FROM local_agent_messages WHERE sender_run_id = ? "
                "ORDER BY created_at, id",
                (run_id,),
            )
        ).fetchall()
        return [_agent_message_projection(dict(row)) for row in rows]
