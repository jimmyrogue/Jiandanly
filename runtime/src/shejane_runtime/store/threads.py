"""Runtime-owned thread snapshots, metadata, and change cursors."""

from __future__ import annotations

from typing import Any

import aiosqlite

from .codec import encode_payload as _encode_payload
from .database import SqliteDatabase
from .database import configure_connection as _configure_connection
from .database import utc_now as _now
from .errors import RunResultConflictError

_PRESENTATION_EVENT_TYPES = (
    "assistant.round.committed",
    "tool.requested",
    "tool.completed",
    "tool.failed",
    "tool.canceled",
    "subagent.spawned",
    "subagent.started",
    "subagent.waiting",
    "subagent.completed",
    "subagent.failed",
    "subagent.canceled",
    "subagent.outcome_unknown",
    "permission.required",
    "permission.resolved",
    "question.asked",
    "question.answered",
    "plan.approval_required",
    "plan.resolved",
    "tool.reconciliation_required",
    "tool.reconciliation_resolved",
    "artifact.created",
    "run.completed",
    "run.failed",
    "run.canceled",
    "run.cleanup_required",
)


class ThreadStore(SqliteDatabase):
    async def list_threads(
        self,
        *,
        principal_id: str,
        limit: int = 100,
        before_created_at: str | None = None,
        before_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN")
            page_limit = max(1, min(int(limit), 500))
            page_filter = ""
            params: list[Any] = [principal_id]
            if before_created_at is not None and before_id is not None:
                page_filter = "AND (created_at < ? OR (created_at = ? AND id < ?)) "
                params.extend([before_created_at, before_created_at, before_id])
            params.append(page_limit + 1)
            rows = await (
                await conn.execute(
                    "SELECT * FROM local_threads WHERE principal_id = ? AND deleted_at IS NULL "
                    + page_filter
                    + "ORDER BY created_at DESC, id DESC LIMIT ?",
                    params,
                )
            ).fetchall()
            cursor_row = await (
                await conn.execute(
                    "SELECT COALESCE(MAX(cursor), 0) FROM local_thread_changes WHERE principal_id = ?",
                    (principal_id,),
                )
            ).fetchone()
            await conn.commit()
            has_more = len(rows) > page_limit
            return (
                [dict(row) for row in rows[:page_limit]],
                int(cursor_row[0] if cursor_row else 0),
                has_more,
            )

    async def get_thread_snapshot(
        self,
        *,
        principal_id: str,
        thread_id: str,
        before_position: int | None = None,
        item_limit: int = 200,
        event_limit: int = 5000,
        expected_version: int | None = None,
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN")
            thread = await (
                await conn.execute(
                    "SELECT * FROM local_threads WHERE principal_id = ? AND id = ? "
                    "AND deleted_at IS NULL",
                    (principal_id, thread_id),
                )
            ).fetchone()
            if thread is None:
                await conn.rollback()
                return None
            if expected_version is not None and int(thread["version"]) != expected_version:
                await conn.rollback()
                raise RunResultConflictError("thread changed while reading snapshot")
            bounded_item_limit = max(2, min(int(item_limit), 500))
            item_filter = ""
            item_params: list[Any] = [thread_id]
            if before_position is not None:
                item_filter = "AND position < ? "
                item_params.append(before_position)
            item_params.append(bounded_item_limit + 1)
            items = await (
                await conn.execute(
                    "SELECT * FROM local_thread_items WHERE thread_id = ? "
                    "AND superseded_at IS NULL "
                    + item_filter
                    + "ORDER BY position DESC, id DESC LIMIT ?",
                    item_params,
                )
            ).fetchall()
            has_more_items = len(items) > bounded_item_limit
            page_items = list(reversed(items[:bounded_item_limit]))
            run_ids = list(
                dict.fromkeys(str(item["run_id"]) for item in page_items if item["run_id"])
            )
            runs: list[aiosqlite.Row] = []
            subagent_invocations_by_run: dict[str, list[dict[str, Any]]] = {}
            tool_receipts_by_run: dict[str, list[dict[str, Any]]] = {}
            wait_candidates_by_run: dict[str, list[dict[str, Any]]] = {}
            artifacts_by_run: dict[str, list[dict[str, Any]]] = {}
            events: list[aiosqlite.Row] = []
            presentation_events: list[aiosqlite.Row] = []
            presentation_high_watermarks: dict[str, int] = {}
            event_high_watermarks: dict[str, int] = {}
            events_truncated = False
            if run_ids:
                placeholders = ",".join("?" for _ in run_ids)
                runs = await (
                    await conn.execute(
                        "SELECT r.*, c.id AS command_id, c.client_message_id, "
                        "(SELECT COUNT(*) FROM local_events e WHERE e.run_id = r.id) AS events_count "
                        "FROM local_runs r LEFT JOIN local_commands c ON c.run_id = r.id "
                        "AND c.principal_id = r.principal_id "
                        "AND c.command_type IN ('run.start', 'run.fork') "
                        f"WHERE r.principal_id = ? AND r.id IN ({placeholders}) "
                        "ORDER BY datetime(r.created_at), r.id",
                        [principal_id, *run_ids],
                    )
                ).fetchall()
                bounded_event_limit = max(1, min(int(event_limit), 10000))
                events = await (
                    await conn.execute(
                        "SELECT e.* FROM local_events e JOIN local_runs r ON r.id = e.run_id "
                        f"WHERE r.principal_id = ? AND e.run_id IN ({placeholders}) "
                        "ORDER BY datetime(r.created_at), r.id, e.seq LIMIT ?",
                        [principal_id, *run_ids, bounded_event_limit + 1],
                    )
                ).fetchall()
                events_truncated = len(events) > bounded_event_limit
                events = events[:bounded_event_limit]
                presentation_events = await (
                    await conn.execute(
                        "SELECT e.* FROM local_events e JOIN local_runs r ON r.id = e.run_id "
                        f"WHERE r.principal_id = ? AND e.run_id IN ({placeholders}) "
                        f"AND e.event_type IN ({','.join('?' for _ in _PRESENTATION_EVENT_TYPES)}) "
                        "ORDER BY datetime(r.created_at), r.id, e.seq",
                        [principal_id, *run_ids, *_PRESENTATION_EVENT_TYPES],
                    )
                ).fetchall()
                subagent_invocations = await self._subagent_invocations_uncommitted(
                    conn,
                    run_ids,
                )
                for invocation in subagent_invocations:
                    subagent_invocations_by_run.setdefault(
                        str(invocation["parent_run_id"]), []
                    ).append(invocation)
                tool_receipts = await (
                    await conn.execute(
                        "SELECT * FROM local_tool_receipts "
                        f"WHERE run_id IN ({placeholders}) ORDER BY created_at, operation_id",
                        run_ids,
                    )
                ).fetchall()
                for receipt in tool_receipts:
                    tool_receipts_by_run.setdefault(str(receipt["run_id"]), []).append(
                        dict(receipt)
                    )
                wait_candidates = await (
                    await conn.execute(
                        "SELECT * FROM local_wait_candidates "
                        f"WHERE run_id IN ({placeholders}) ORDER BY created_at, id",
                        run_ids,
                    )
                ).fetchall()
                for candidate in wait_candidates:
                    wait_candidates_by_run.setdefault(str(candidate["run_id"]), []).append(
                        dict(candidate)
                    )
                artifacts = await (
                    await conn.execute(
                        "SELECT * FROM local_artifacts "
                        f"WHERE run_id IN ({placeholders}) ORDER BY created_at, id",
                        run_ids,
                    )
                ).fetchall()
                for artifact in artifacts:
                    artifacts_by_run.setdefault(str(artifact["run_id"]), []).append(dict(artifact))
                event_high_watermarks = dict.fromkeys(run_ids, 0)
                presentation_high_watermarks = dict.fromkeys(run_ids, 0)
                for event in events:
                    event_run_id = str(event["run_id"])
                    event_high_watermarks[event_run_id] = max(
                        event_high_watermarks[event_run_id],
                        int(event["seq"]),
                    )
                for event in presentation_events:
                    event_run_id = str(event["run_id"])
                    presentation_high_watermarks[event_run_id] = int(event["seq"])
            cursor_row = await (
                await conn.execute(
                    "SELECT COALESCE(MAX(cursor), 0) FROM local_thread_changes "
                    "WHERE principal_id = ? AND thread_id = ?",
                    (principal_id, thread_id),
                )
            ).fetchone()
            await conn.commit()
            return {
                "thread": dict(thread),
                "items": [dict(item) for item in page_items],
                "runs": [
                    {
                        **dict(run),
                        "subagent_invocations": subagent_invocations_by_run.get(str(run["id"]), []),
                    }
                    for run in runs
                ],
                "events": [dict(event) for event in events],
                "presentation_events": [dict(event) for event in presentation_events],
                "presentation_high_watermarks": presentation_high_watermarks,
                "tool_receipts_by_run": tool_receipts_by_run,
                "wait_candidates_by_run": wait_candidates_by_run,
                "artifacts_by_run": artifacts_by_run,
                "event_high_watermarks": event_high_watermarks,
                "cursor": int(cursor_row[0] if cursor_row else 0),
                "has_more_items": has_more_items,
                "next_before_position": int(page_items[0]["position"])
                if has_more_items and page_items
                else None,
                "events_truncated": events_truncated,
            }

    async def get_run_presentation_facts(self, run_id: str) -> dict[str, Any] | None:
        """Read one Run's presentation sources from a single SQLite snapshot."""
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN")
            run = await (
                await conn.execute("SELECT * FROM local_runs WHERE id = ?", (run_id,))
            ).fetchone()
            if run is None:
                await conn.rollback()
                return None
            assistant_item = None
            if run["assistant_item_id"]:
                assistant_item = await (
                    await conn.execute(
                        "SELECT * FROM local_thread_items WHERE id = ? AND run_id = ?",
                        (run["assistant_item_id"], run_id),
                    )
                ).fetchone()
            events = await (
                await conn.execute(
                    "SELECT * FROM local_events WHERE run_id = ? "
                    f"AND event_type IN ({','.join('?' for _ in _PRESENTATION_EVENT_TYPES)}) "
                    "ORDER BY seq",
                    (run_id, *_PRESENTATION_EVENT_TYPES),
                )
            ).fetchall()
            receipts = await (
                await conn.execute(
                    "SELECT * FROM local_tool_receipts WHERE run_id = ? "
                    "ORDER BY created_at, operation_id",
                    (run_id,),
                )
            ).fetchall()
            wait_candidates = await (
                await conn.execute(
                    "SELECT * FROM local_wait_candidates WHERE run_id = ? ORDER BY created_at, id",
                    (run_id,),
                )
            ).fetchall()
            artifacts = await (
                await conn.execute(
                    "SELECT * FROM local_artifacts WHERE run_id = ? ORDER BY created_at, id",
                    (run_id,),
                )
            ).fetchall()
            await conn.commit()
            return {
                "run": dict(run),
                "assistant_item": dict(assistant_item) if assistant_item is not None else None,
                "events": [dict(event) for event in events],
                "tool_receipts": [dict(receipt) for receipt in receipts],
                "wait_candidates": [dict(candidate) for candidate in wait_candidates],
                "artifacts": [dict(artifact) for artifact in artifacts],
                "event_high_watermark": int(events[-1]["seq"]) if events else 0,
            }

    async def update_thread(
        self,
        *,
        principal_id: str,
        thread_id: str,
        title: str | None,
        metadata: dict[str, Any] | None,
        archived: bool | None,
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await conn.execute(
                        "SELECT * FROM local_threads WHERE principal_id = ? AND id = ? "
                        "AND deleted_at IS NULL",
                        (principal_id, thread_id),
                    )
                ).fetchone()
                if row is None:
                    await conn.rollback()
                    return None
                now = _now()
                version = int(row["version"]) + 1
                archived_at = row["archived_at"]
                if archived is True and archived_at is None:
                    archived_at = now
                elif archived is False:
                    archived_at = None
                await conn.execute(
                    "UPDATE local_threads SET title = ?, metadata_json = ?, archived_at = ?, "
                    "version = ?, updated_at = ? WHERE id = ?",
                    (
                        " ".join((title or row["title"]).split())[:80],
                        _encode_payload(metadata) if metadata is not None else row["metadata_json"],
                        archived_at,
                        version,
                        now,
                        thread_id,
                    ),
                )
                await conn.execute(
                    "INSERT INTO local_thread_changes "
                    "(principal_id, thread_id, thread_version, change_type, created_at) "
                    "VALUES (?, ?, ?, 'thread.updated', ?)",
                    (principal_id, thread_id, version, now),
                )
                updated = await (
                    await conn.execute("SELECT * FROM local_threads WHERE id = ?", (thread_id,))
                ).fetchone()
                await conn.commit()
                return dict(updated) if updated else None
            except BaseException:
                await conn.rollback()
                raise

    async def delete_thread(self, *, principal_id: str, thread_id: str) -> int | None:
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await conn.execute(
                        "SELECT * FROM local_threads WHERE principal_id = ? AND id = ?",
                        (principal_id, thread_id),
                    )
                ).fetchone()
                if row is None:
                    await conn.rollback()
                    return None
                if row["deleted_at"] is not None:
                    await conn.commit()
                    return int(row["version"])
                active = await (
                    await conn.execute(
                        "SELECT 1 FROM local_runs r "
                        "LEFT JOIN local_run_jobs j ON j.run_id = r.id "
                        "WHERE r.principal_id = ? AND r.thread_id = ? AND ("
                        "r.status NOT IN ('completed', 'failed', 'canceled') OR "
                        "j.status IN ('pending', 'leased')) LIMIT 1",
                        (principal_id, thread_id),
                    )
                ).fetchone()
                if active is not None:
                    raise RunResultConflictError("thread has an unsettled run")
                now = _now()
                version = int(row["version"]) + 1
                await conn.execute(
                    "UPDATE local_threads SET deleted_at = ?, archived_at = ?, version = ?, "
                    "updated_at = ? WHERE id = ?",
                    (now, now, version, now, thread_id),
                )
                await conn.execute(
                    "INSERT INTO local_thread_changes "
                    "(principal_id, thread_id, thread_version, change_type, created_at) "
                    "VALUES (?, ?, ?, 'thread.deleted', ?)",
                    (principal_id, thread_id, version, now),
                )
                await conn.commit()
                return version
            except BaseException:
                await conn.rollback()
                raise

    async def thread_changes_since(
        self,
        *,
        principal_id: str,
        after_cursor: int,
        limit: int = 500,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = await (
            await self._conn.execute(
                "SELECT cursor, thread_id, thread_version, change_type, run_id, created_at "
                "FROM local_thread_changes WHERE principal_id = ? AND cursor > ? "
                "ORDER BY cursor LIMIT ?",
                (principal_id, max(0, int(after_cursor)), max(1, min(int(limit), 1000))),
            )
        ).fetchall()
        changes = [dict(row) for row in rows]
        return changes, (int(changes[-1]["cursor"]) if changes else max(0, int(after_cursor)))
