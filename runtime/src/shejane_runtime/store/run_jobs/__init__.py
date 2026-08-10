"""Durable run-job queue, leases, recovery, and quarantine."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite

from ..codec import encode_payload as _encode_payload
from ..database import configure_connection as _configure_connection
from ..database import utc_now as _now
from ..errors import WorkspaceAdmissionError
from ..events import TERMINAL_RUN_STATUSES as _TERMINAL_RUN_STATUSES
from ..ids import new_id as _new_id
from .quarantine import RunJobQuarantineStore, _lease_expiry_error


class RunJobStore(RunJobQuarantineStore):
    @staticmethod
    def _run_job_input(run: dict[str, Any]) -> dict[str, Any]:
        return {
            "principal_id": run["principal_id"],
            "goal": run["goal"],
            "user_input": run.get("user_input") or run["goal"],
            "workspace_path": run["workspace_path"],
            "mode": run["mode"],
            "history": json.loads(run["history_json"] or "[]"),
            "settings": json.loads(run["settings_json"] or "{}"),
            "metadata": json.loads(run["metadata_json"] or "{}"),
            "run_kind": run.get("run_kind") or "turn",
            "root_run_id": run.get("root_run_id") or run.get("id") or run["run_id"],
            "agent_definition_id": run.get("agent_definition_id") or "shejane.default",
            "agent_definition_version": run.get("agent_definition_version") or "1",
            "collaboration_depth": int(run.get("collaboration_depth") or 0),
            "collaboration_policy": json.loads(run.get("collaboration_policy_json") or "{}"),
        }

    @staticmethod
    def _new_run_job_record(
        *,
        run_id: str,
        kind: str,
        input_payload: dict[str, Any],
        resume_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        created_at = _now()
        return {
            "id": _new_id("job"),
            "run_id": run_id,
            "kind": kind,
            "status": "pending",
            "input_json": _encode_payload(input_payload),
            "resume_json": _encode_payload(resume_payload) if resume_payload is not None else None,
            "lease_owner": None,
            "lease_generation": 0,
            "lease_expires_at": None,
            "attempt": 0,
            "cancel_requested_at": None,
            "created_at": created_at,
            "updated_at": created_at,
            "finished_at": None,
        }

    @staticmethod
    async def _insert_run_job(conn: aiosqlite.Connection, job: dict[str, Any]) -> None:
        await conn.execute(
            "INSERT INTO local_run_jobs "
            "(id, run_id, kind, status, input_json, resume_json, lease_owner, "
            " lease_generation, lease_expires_at, attempt, cancel_requested_at, "
            " created_at, updated_at, finished_at) "
            "VALUES (:id, :run_id, :kind, :status, :input_json, :resume_json, "
            " :lease_owner, :lease_generation, :lease_expires_at, :attempt, "
            " :cancel_requested_at, :created_at, :updated_at, :finished_at)",
            job,
        )

    async def run_initial_thread_title_seed(self, run_id: str) -> str | None:
        row = await (
            await self._conn.execute(
                "SELECT c.payload_json, r.user_input, r.goal FROM local_runs r "
                "LEFT JOIN local_commands c ON c.run_id = r.id "
                "AND c.command_type IN ('run.start', 'run.fork') WHERE r.id = ? "
                "ORDER BY c.created_at LIMIT 1",
                (run_id,),
            )
        ).fetchone()
        if row is None:
            return None
        try:
            envelope = json.loads(row["payload_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            envelope = {}
        payload = envelope.get("payload") if isinstance(envelope, dict) else None
        requested = payload.get("thread_title") if isinstance(payload, dict) else None
        return " ".join(str(requested or row["user_input"] or row["goal"] or "").split())[:80]

    async def enqueue_run_job(
        self,
        run_id: str,
        *,
        kind: str,
        resume_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        existing_run = await self.get_run(run_id)
        if existing_run is None:
            return None
        path_error = await self._workspace_path_error(existing_run.get("workspace_path"))
        if path_error is not None:
            raise WorkspaceAdmissionError(path_error)
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await conn.execute("SELECT * FROM local_runs WHERE id = ?", (run_id,))
                ).fetchone()
                if row is None:
                    await conn.rollback()
                    return None
                run = dict(row)
                workspace_error = await self._workspace_owner_error(
                    conn,
                    principal_id=str(run["principal_id"]),
                    path=run["workspace_path"],
                )
                if workspace_error is not None:
                    raise WorkspaceAdmissionError(workspace_error)
                active = await (
                    await conn.execute(
                        "SELECT * FROM local_run_jobs WHERE run_id = ? "
                        "AND status IN ('pending', 'leased')",
                        (run_id,),
                    )
                ).fetchone()
                if active is not None:
                    await conn.rollback()
                    return dict(active)
                job = self._new_run_job_record(
                    run_id=run_id,
                    kind=kind,
                    input_payload=self._run_job_input(run),
                    resume_payload=resume_payload,
                )
                await self._insert_run_job(conn, job)
                await conn.commit()
                return job
            except BaseException:
                await conn.rollback()
                raise

    async def get_active_run_job(self, run_id: str) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM local_run_jobs WHERE run_id = ? "
            "AND status IN ('pending', 'leased') AND quarantined_at IS NULL",
            (run_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_run_job(self, job_id: str) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM local_run_jobs WHERE id = ?",
            (job_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def _requeue_expired_jobs_uncommitted(
        self,
        conn: aiosqlite.Connection,
        now: str,
    ) -> None:
        expired = await (
            await conn.execute(
                "SELECT local_run_jobs.*, local_runs.status AS run_status "
                "FROM local_run_jobs JOIN local_runs ON local_runs.id = local_run_jobs.run_id "
                "WHERE local_run_jobs.status = 'leased' "
                "AND local_run_jobs.lease_expires_at <= ? "
                "ORDER BY local_run_jobs.lease_expires_at, local_run_jobs.id",
                (now,),
            )
        ).fetchall()
        for expired_row in expired:
            job = dict(expired_row)
            execution_attempt_id = f"{job['id']}:{job['lease_generation']}"
            await conn.execute(
                "UPDATE local_model_calls SET status = 'outcome_unknown', completed_at = ? "
                "WHERE run_id = ? AND execution_attempt_id = ? "
                "AND status IN ('reserved', 'streaming')",
                (now, job["run_id"], execution_attempt_id),
            )
            await self._mark_running_tool_receipts_outcome_unknown_uncommitted(
                conn,
                run_id=str(job["run_id"]),
                execution_attempt_id=execution_attempt_id,
                error_type="execution_lease_expired",
                now=now,
            )
            # Only flag them here. Killing is an irreversible side effect and
            # this runs inside BEGIN IMMEDIATE, so it must not happen while the
            # write lock is held or a rollback would leave processes already
            # dead. The reaper picks these up outside the transaction.
            await conn.execute(
                "UPDATE local_sandbox_processes SET status = 'orphaned', updated_at = ? "
                "WHERE run_id = ? AND execution_attempt_id = ? AND status = 'running'",
                (now, job["run_id"], execution_attempt_id),
            )
            settled_job_status = {
                "completed": "completed",
                "failed": "dead",
                "canceled": "canceled",
                "waiting_permission": "completed",
                "waiting_input": "completed",
            }.get(str(job["run_status"]))
            if settled_job_status is not None:
                run_status = str(job["run_status"])
                if run_status in _TERMINAL_RUN_STATUSES:
                    await self._cancel_unstarted_tool_receipts_uncommitted(
                        conn,
                        run_id=str(job["run_id"]),
                        canceled_at=now,
                        error_type={
                            "completed": "ParentRunCompleted",
                            "failed": "ParentRunFailed",
                            "canceled": "RunCanceled",
                        }[run_status],
                    )
                await conn.execute(
                    "UPDATE local_run_jobs SET status = ?, updated_at = ?, finished_at = ?, "
                    "lease_owner = NULL, lease_expires_at = NULL "
                    "WHERE id = ? AND status = 'leased'",
                    (settled_job_status, now, now, job["id"]),
                )
                continue
            await self._cancel_unstarted_tool_receipts_uncommitted(
                conn,
                run_id=str(job["run_id"]),
                canceled_at=now,
                error_type="ParentRunCleanupRequired",
            )
            await conn.execute(
                "UPDATE local_run_jobs SET quarantined_at = ?, "
                "quarantine_reason = 'execution_lease_expired', "
                "lease_expires_at = NULL, updated_at = ? "
                "WHERE id = ? AND status = 'leased'",
                (now, now, job["id"]),
            )
            await conn.execute(
                "UPDATE local_runs SET status = 'cleanup_required', updated_at = ?, "
                "completed_at = NULL "
                "WHERE id = ?",
                (now, job["run_id"]),
            )
            cleanup_report = await self._sandbox_cleanup_report_uncommitted(
                conn,
                run_id=str(job["run_id"]),
                execution_attempt_id=execution_attempt_id,
            )
            cleanup_payload = {
                "error": _lease_expiry_error(cleanup_report),
                "type": "ExecutionLeaseExpiredError",
                "retryable": False,
                "category": "execution_lease_expired",
                "cleanup": cleanup_report,
            }
            event = await self._append_event_uncommitted(
                conn,
                job["run_id"],
                "run.cleanup_required",
                payload_json=_encode_payload(cleanup_payload),
                created_at=now,
            )
            await self._update_thread_projection_uncommitted(
                conn,
                run_id=job["run_id"],
                run_status="cleanup_required",
                change_type="run.cleanup_required",
                payload=cleanup_payload,
                event_high_watermark=int(event["seq"]),
                changed_at=now,
            )

    async def claim_run_job(
        self,
        *,
        worker_id: str,
        lease_seconds: float = 30.0,
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                now = _now()
                await self._requeue_expired_jobs_uncommitted(conn, now)
                blocked_rows = await (
                    await conn.execute(
                        "SELECT DISTINCT j.run_id FROM local_run_jobs j "
                        "JOIN local_child_dependencies d ON d.child_run_id = j.run_id "
                        "JOIN local_runs dependency ON dependency.id = d.dependency_run_id "
                        "WHERE j.status = 'pending' "
                        "AND dependency.status IN ('failed', 'canceled', 'cleanup_required') "
                        "ORDER BY j.created_at, j.id"
                    )
                ).fetchall()
                for blocked in blocked_rows:
                    await self._request_run_cancel_uncommitted(conn, str(blocked["run_id"]))
                row = await (
                    await conn.execute(
                        "SELECT j.* FROM local_run_jobs j JOIN local_runs r ON r.id = j.run_id "
                        "WHERE j.status = 'pending' AND (r.run_kind != 'child' OR NOT EXISTS ("
                        "SELECT 1 FROM local_child_dependencies d "
                        "JOIN local_runs dependency ON dependency.id = d.dependency_run_id "
                        "WHERE d.child_run_id = j.run_id AND dependency.status != 'completed'"
                        ")) ORDER BY j.created_at, j.id LIMIT 1"
                    )
                ).fetchone()
                if row is None:
                    await conn.commit()
                    return None
                job = dict(row)
                expires_at = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()
                await conn.execute(
                    "UPDATE local_run_jobs SET status = 'leased', lease_owner = ?, "
                    "lease_generation = lease_generation + 1, lease_expires_at = ?, "
                    "attempt = attempt + 1, updated_at = ? WHERE id = ? AND status = 'pending'",
                    (worker_id, expires_at, now, job["id"]),
                )
                await conn.execute(
                    "UPDATE local_runs SET status = 'running', updated_at = ?, completed_at = NULL "
                    "WHERE id = ?",
                    (now, job["run_id"]),
                )
                claimed = await (
                    await conn.execute("SELECT * FROM local_run_jobs WHERE id = ?", (job["id"],))
                ).fetchone()
                await conn.commit()
                return dict(claimed) if claimed else None
            except BaseException:
                await conn.rollback()
                raise

    async def renew_run_job(
        self,
        job_id: str,
        *,
        lease_owner: str,
        lease_generation: int,
        lease_seconds: float = 30.0,
    ) -> tuple[bool, bool]:
        renewed_at = _now()
        expires_at = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                "UPDATE local_run_jobs SET lease_expires_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'leased' AND lease_owner = ? "
                "AND lease_generation = ? AND quarantined_at IS NULL "
                "AND lease_expires_at > ?",
                (
                    expires_at,
                    renewed_at,
                    job_id,
                    lease_owner,
                    lease_generation,
                    renewed_at,
                ),
            )
            if cursor.rowcount != 1:
                await conn.rollback()
                return False, False
            row = await (
                await conn.execute(
                    "SELECT cancel_requested_at FROM local_run_jobs WHERE id = ?", (job_id,)
                )
            ).fetchone()
            await conn.commit()
            return True, bool(row and row[0])

    async def finish_run_job(
        self,
        job_id: str,
        *,
        lease_owner: str,
        lease_generation: int,
        status: str,
    ) -> bool:
        if status not in {"completed", "canceled", "dead"}:
            raise ValueError(f"invalid finished job status: {status}")
        finished_at = _now()
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                "UPDATE local_run_jobs SET status = ?, updated_at = ?, finished_at = ?, "
                "lease_expires_at = NULL WHERE id = ? AND status = 'leased' "
                "AND lease_owner = ? AND lease_generation = ? AND lease_expires_at > ?",
                (
                    status,
                    finished_at,
                    finished_at,
                    job_id,
                    lease_owner,
                    lease_generation,
                    finished_at,
                ),
            )
            await conn.commit()
            return cursor.rowcount == 1
