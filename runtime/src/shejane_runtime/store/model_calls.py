"""P8 model-call ledger, sandbox records, and assistant drafts."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from .artifacts import MAX_SETTLEMENT_ARTIFACT_REFS
from .codec import encode_payload as _encode_payload
from .codec import json_payload as _json_payload
from .database import SqliteDatabase
from .database import configure_connection as _configure_connection
from .database import utc_now as _now
from .errors import ModelCallBudgetExceeded, RunResultConflictError
from .ids import new_id as _new_id


class ModelCallStore(SqliteDatabase):
    # --- model services ---

    async def reserve_model_call(
        self,
        *,
        run_id: str,
        execution_attempt_id: str,
        model: str,
        max_calls: int,
        purpose: str = "agent",
        parent_tool_operation_id: str | None = None,
        logical_call_id: str | None = None,
        retry_attempt: int = 0,
    ) -> dict[str, Any]:
        """Atomically reserve one durable model-call slot for a run."""
        if purpose not in {
            "agent",
            "approval_review",
            "clarification_review",
            "completion_review",
            "title_generation",
            "summarization",
        }:
            raise ValueError("model call purpose is invalid")
        async with self.run_write_transaction(run_id) as conn:
            if parent_tool_operation_id is not None:
                await self._require_tool_receipt_in_run_uncommitted(
                    conn,
                    operation_id=parent_tool_operation_id,
                    run_id=run_id,
                )
            row = await (
                await conn.execute(
                    "SELECT COUNT(*) AS total_count, "
                    "COALESCE(SUM(CASE WHEN purpose = ? THEN 1 ELSE 0 END), 0) AS purpose_count "
                    "FROM local_model_calls WHERE run_id = ?",
                    (purpose, run_id),
                )
            ).fetchone()
            call_index = int(row["total_count"] if row is not None else 0) + 1
            purpose_index = int(row["purpose_count"] if row is not None else 0) + 1
            if purpose_index > max(1, int(max_calls)):
                raise ModelCallBudgetExceeded(
                    f"{purpose} model call budget exhausted for run {run_id}: {max_calls}"
                )
            call_id = _new_id("model_call")
            record = {
                "id": call_id,
                "run_id": run_id,
                "execution_attempt_id": execution_attempt_id,
                "call_index": call_index,
                "model": model,
                "purpose": purpose,
                "parent_tool_operation_id": parent_tool_operation_id,
                "logical_call_id": logical_call_id or call_id,
                "retry_attempt": max(0, int(retry_attempt)),
                "status": "reserved",
                "created_at": _now(),
            }
            await conn.execute(
                "INSERT INTO local_model_calls "
                "(id, run_id, execution_attempt_id, call_index, model, purpose, "
                "parent_tool_operation_id, logical_call_id, retry_attempt, status, created_at) "
                "VALUES (:id, :run_id, :execution_attempt_id, :call_index, :model, :purpose, "
                ":parent_tool_operation_id, :logical_call_id, :retry_attempt, :status, :created_at)",
                record,
            )
        return record

    async def record_sandbox_process(
        self,
        *,
        run_id: str,
        execution_attempt_id: str,
        pid: int,
        process_started_at: str,
        settings_path: str,
    ) -> str:
        """Durably note a sandbox launcher this attempt is responsible for.

        Written before the command can outlive us so that a Runtime killed mid
        command leaves the next one something to act on. ``process_started_at``
        and ``settings_path`` are what later prove the pid was not recycled.
        """

        now = _now()
        record = {
            "id": _new_id("sbx"),
            "run_id": run_id,
            "execution_attempt_id": execution_attempt_id,
            "pid": int(pid),
            "process_started_at": process_started_at,
            "settings_path": settings_path,
            "status": "running",
            "created_at": now,
            "updated_at": now,
        }
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute(
                "INSERT INTO local_sandbox_processes "
                "(id, run_id, execution_attempt_id, pid, process_started_at, settings_path, "
                "status, created_at, updated_at) "
                "VALUES (:id, :run_id, :execution_attempt_id, :pid, :process_started_at, "
                ":settings_path, :status, :created_at, :updated_at)",
                record,
            )
            await conn.commit()
        return str(record["id"])

    async def forget_sandbox_process(self, sandbox_process_id: str) -> None:
        """Drop the record for a launcher that exited under our supervision."""

        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute(
                "DELETE FROM local_sandbox_processes WHERE id = ?",
                (sandbox_process_id,),
            )
            await conn.commit()

    async def list_reapable_sandbox_processes(
        self,
        *,
        include_running: bool = False,
    ) -> list[dict[str, Any]]:
        """Sandbox records that no live execution is supervising any more.

        ``include_running`` is for boot: a row still marked running when the
        Runtime has only just started belongs to the process that died, because
        this one has not spawned anything yet.
        """

        statuses = ("orphaned", "running") if include_running else ("orphaned",)
        placeholders = ", ".join("?" for _ in statuses)
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            cursor = await conn.execute(
                "SELECT * FROM local_sandbox_processes "
                f"WHERE status IN ({placeholders}) ORDER BY created_at, id",
                statuses,
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def settle_sandbox_process(self, sandbox_process_id: str, *, status: str) -> None:
        """Record what the reaper found, so a record is never acted on twice."""

        if status not in {"reaped", "gone", "stale"}:
            raise ValueError(f"sandbox process cannot settle as {status!r}")
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute(
                "UPDATE local_sandbox_processes SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), sandbox_process_id),
            )
            await conn.commit()

    async def list_model_calls_for_run(self, run_id: str) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(
            "SELECT * FROM local_model_calls WHERE run_id = ? ORDER BY call_index",
            (run_id,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def mark_model_call_output(self, *, run_id: str, call_id: str) -> None:
        async with self.run_write_transaction(run_id) as conn:
            cursor = await conn.execute(
                "UPDATE local_model_calls SET status = 'streaming', output_started = 1, "
                "first_output_at = COALESCE(first_output_at, ?) "
                "WHERE id = ? AND run_id = ? AND status IN ('reserved', 'streaming')",
                (_now(), call_id, run_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"model call {call_id} cannot record output")

    async def settle_model_call(
        self,
        *,
        run_id: str,
        call_id: str,
        provider_request_id: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> None:
        usage_known = input_tokens is not None or output_tokens is not None
        status = "completed" if usage_known else "completed_unmetered"
        async with self.run_write_transaction(run_id) as conn:
            cursor = await conn.execute(
                "UPDATE local_model_calls SET status = ?, provider_request_id = ?, "
                "input_tokens = ?, output_tokens = ?, completed_at = ? "
                "WHERE id = ? AND run_id = ? AND status IN ('reserved', 'streaming')",
                (
                    status,
                    provider_request_id,
                    input_tokens,
                    output_tokens,
                    _now(),
                    call_id,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"model call {call_id} cannot be settled twice")

    async def fail_model_call(
        self,
        *,
        run_id: str,
        call_id: str,
        outcome_unknown: bool,
        error_code: str | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        status = "outcome_unknown" if outcome_unknown else "failed"
        async with self.run_write_transaction(run_id) as conn:
            cursor = await conn.execute(
                "UPDATE local_model_calls SET status = ?, error_code = ?, "
                "provider_request_id = ?, completed_at = ? "
                "WHERE id = ? AND run_id = ? AND status IN ('reserved', 'streaming')",
                (status, error_code, provider_request_id, _now(), call_id, run_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"model call {call_id} cannot be failed")

    async def model_usage_summary(self, run_id: str) -> dict[str, int]:
        row = await (
            await self._conn.execute(
                "SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens, "
                "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
                "SUM(CASE WHEN status = 'completed_unmetered' THEN 1 ELSE 0 END) "
                "AS unmetered_calls, "
                "SUM(CASE WHEN status = 'outcome_unknown' THEN 1 ELSE 0 END) "
                "AS outcome_unknown_calls, COUNT(*) AS model_calls "
                "FROM local_model_calls WHERE run_id = ?",
                (run_id,),
            )
        ).fetchone()
        return {
            key: int(row[key] or 0)
            for key in (
                "input_tokens",
                "output_tokens",
                "unmetered_calls",
                "outcome_unknown_calls",
                "model_calls",
            )
        }

    async def model_call_budget_status(self, run_id: str, *, purpose: str) -> dict[str, int]:
        """Return durable usage for one independently budgeted model purpose."""
        row = await (
            await self._conn.execute(
                "SELECT COUNT(*) AS model_calls, "
                "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
                "COALESCE(SUM(output_tokens), 0) AS output_tokens "
                "FROM local_model_calls WHERE run_id = ? AND purpose = ?",
                (run_id, purpose),
            )
        ).fetchone()
        return {key: int(row[key] or 0) for key in ("model_calls", "input_tokens", "output_tokens")}

    async def execution_settlement_snapshot(self, run_id: str) -> dict[str, Any]:
        """Read the authoritative records needed to settle one execution."""
        model_rows = await (
            await self._conn.execute(
                "SELECT status, COUNT(*) AS count FROM local_model_calls "
                "WHERE run_id = ? GROUP BY status ORDER BY status",
                (run_id,),
            )
        ).fetchall()
        tool_rows = await (
            await self._conn.execute(
                "SELECT status, COUNT(*) AS count FROM local_tool_receipts "
                "WHERE run_id = ? GROUP BY status ORDER BY status",
                (run_id,),
            )
        ).fetchall()
        draft = await (
            await self._conn.execute(
                "SELECT * FROM local_assistant_drafts WHERE run_id = ?",
                (run_id,),
            )
        ).fetchone()
        artifact_count_row = await (
            await self._conn.execute(
                "SELECT COUNT(*) AS count FROM local_artifacts WHERE run_id = ?",
                (run_id,),
            )
        ).fetchone()
        artifacts = await (
            await self._conn.execute(
                "SELECT id, kind, content_type, bytes FROM local_artifacts "
                "WHERE run_id = ? ORDER BY created_at, id LIMIT ?",
                (run_id, MAX_SETTLEMENT_ARTIFACT_REFS),
            )
        ).fetchall()
        verification = await (
            await self._conn.execute(
                "SELECT operation_id, status, result_hash FROM local_tool_receipts "
                "WHERE run_id = ? AND tool_name = 'task.verify' "
                "ORDER BY created_at DESC, operation_id DESC LIMIT 1",
                (run_id,),
            )
        ).fetchone()
        return {
            "assistant": dict(draft) if draft is not None else None,
            "usage": await self.model_usage_summary(run_id),
            "model_statuses": {str(row["status"]): int(row["count"]) for row in model_rows},
            "tool_statuses": {str(row["status"]): int(row["count"]) for row in tool_rows},
            "artifacts": {
                "count": int(artifact_count_row["count"] if artifact_count_row else 0),
                "items": [dict(row) for row in artifacts],
                "truncated": int(artifact_count_row["count"] if artifact_count_row else 0)
                > len(artifacts),
            },
            "verification": dict(verification) if verification is not None else None,
        }

    async def update_assistant_draft(
        self,
        *,
        run_id: str,
        message_key: str,
        content: str,
        tool_calls: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Persist one fully assembled top-level assistant model round."""
        now = _now()
        async with self.run_write_transaction(run_id) as conn:
            existing = await (
                await conn.execute(
                    "SELECT * FROM local_assistant_drafts WHERE run_id = ?",
                    (run_id,),
                )
            ).fetchone()
            if existing is not None and existing["message_key"] == message_key:
                return dict(existing)
            revision = int(existing["revision"] if existing is not None else 0) + 1
            created_at = str(existing["created_at"] if existing is not None else now)
            await conn.execute(
                "INSERT INTO local_assistant_drafts "
                "(run_id, revision, message_key, content, tool_calls_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET revision = excluded.revision, "
                "message_key = excluded.message_key, content = excluded.content, "
                "tool_calls_json = excluded.tool_calls_json, updated_at = excluded.updated_at",
                (
                    run_id,
                    revision,
                    message_key,
                    content,
                    json.dumps(tool_calls, ensure_ascii=False, separators=(",", ":"), default=str),
                    created_at,
                    now,
                ),
            )
        return {
            "run_id": run_id,
            "revision": revision,
            "message_key": message_key,
            "content": content,
            "tool_calls_json": json.dumps(
                tool_calls,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ),
            "created_at": created_at,
            "updated_at": now,
        }

    async def commit_assistant_round(
        self,
        run_id: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Append one complete model round exactly once across execution replay."""
        round_id = str(payload.get("round_id") or "")
        if not round_id:
            raise ValueError("assistant round requires round_id")
        payload_json = _encode_payload(payload)
        async with self.run_write_transaction(run_id) as conn:
            existing = await (
                await conn.execute(
                    "SELECT * FROM local_events WHERE run_id = ? "
                    "AND event_type = 'assistant.round.committed' "
                    "AND json_extract(payload_json, '$.round_id') = ? LIMIT 1",
                    (run_id, round_id),
                )
            ).fetchone()
            if existing is not None:
                record = dict(existing)
                if str(record["payload_json"]) != payload_json:
                    committed = _json_payload(record["payload_json"])
                    # An approved edit creates a replacement tool-call id while
                    # replaying the same model round. That routing identity is
                    # allowed to differ; display text and summary are immutable.
                    if any(
                        committed.get(key) != payload.get(key)
                        for key in ("text", "reasoning_summary")
                    ):
                        raise RunResultConflictError(
                            f"assistant round {round_id} was already committed differently"
                        )
                return record, False
            committed_at = _now()
            event = await self._append_event_uncommitted(
                conn,
                run_id,
                "assistant.round.committed",
                payload_json=payload_json,
                created_at=committed_at,
            )
            await self._touch_thread_for_run_event_uncommitted(
                conn,
                run_id=run_id,
                change_type="assistant.round.committed",
                event_high_watermark=int(event["seq"]),
                changed_at=committed_at,
            )
            return event, True

    async def get_assistant_draft(self, run_id: str) -> dict[str, Any] | None:
        row = await (
            await self._conn.execute(
                "SELECT * FROM local_assistant_drafts WHERE run_id = ?",
                (run_id,),
            )
        ).fetchone()
        return dict(row) if row is not None else None
