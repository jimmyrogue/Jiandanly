"""Quarantine and cleanup confirmation for fenced run-job attempts."""

from __future__ import annotations

from typing import Any

import aiosqlite

from .codec import encode_payload as _encode_payload
from .database import CURRENT_EXECUTION_LEASE as _CURRENT_EXECUTION_LEASE
from .database import LeaseFenceError, SqliteDatabase
from .database import configure_connection as _configure_connection
from .database import utc_now as _now


def _lease_expiry_error(cleanup_report: dict[str, Any]) -> str:
    if cleanup_report.get("status") == "completed":
        return (
            "The execution lease expired. Every sandboxed command this attempt started has "
            "been stopped, but the run is quarantined and cannot be retried automatically."
        )
    return (
        "The execution lease expired before the Runtime could prove that external work "
        "stopped. This run is quarantined and cannot be retried automatically."
    )


class RunJobQuarantineStore(SqliteDatabase):
    async def _sandbox_cleanup_report_uncommitted(
        self,
        conn: aiosqlite.Connection,
        *,
        run_id: str,
        execution_attempt_id: str,
    ) -> dict[str, Any]:
        """Report whether this attempt's sandboxes are known to have stopped.

        Confirmation requires evidence, so an attempt that recorded no sandbox
        stays unconfirmed rather than claiming a clean exit it cannot show. A
        record left stale is likewise unconfirmed: the pid was recycled, so
        whatever it named was never proven to have stopped.
        """

        cursor = await conn.execute(
            "SELECT status, COUNT(*) AS total FROM local_sandbox_processes "
            "WHERE run_id = ? AND execution_attempt_id = ? GROUP BY status",
            (run_id, execution_attempt_id),
        )
        counts = {str(row["status"]): int(row["total"]) for row in await cursor.fetchall()}
        if not counts:
            return {"status": "unconfirmed"}
        stopped = counts.get("reaped", 0) + counts.get("gone", 0)
        if stopped == sum(counts.values()):
            return {"status": "completed", "sandboxes_stopped": stopped}
        return {
            "status": "unconfirmed",
            "sandboxes_stopped": stopped,
            "sandboxes_unaccounted": sum(counts.values()) - stopped,
        }

    async def quarantine_execution_attempt(
        self,
        run_id: str,
        *,
        reason: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Fence an attempt whose resource stillness cannot be proven.

        The job intentionally remains leased to its exact owner/generation,
        but with no renewable expiry. It therefore blocks new attempts while
        allowing that original owner to submit a final cleanup confirmation.
        """
        lease = _CURRENT_EXECUTION_LEASE.get()
        if lease is None or lease.run_id != run_id:
            raise LeaseFenceError("execution quarantine requires the current run lease")
        now = _now()
        async with self.run_write_transaction(run_id, lease=lease) as conn:
            execution_attempt_id = f"{lease.job_id}:{lease.lease_generation}"
            await conn.execute(
                "UPDATE local_model_calls SET status = 'outcome_unknown', completed_at = ? "
                "WHERE run_id = ? AND execution_attempt_id = ? "
                "AND status IN ('reserved', 'streaming')",
                (now, run_id, execution_attempt_id),
            )
            await self._mark_running_tool_receipts_outcome_unknown_uncommitted(
                conn,
                run_id=run_id,
                execution_attempt_id=execution_attempt_id,
                error_type=reason,
                now=now,
            )
            await self._cancel_unstarted_tool_receipts_uncommitted(
                conn,
                run_id=run_id,
                canceled_at=now,
                error_type="ParentRunCleanupRequired",
            )
            await conn.execute(
                "UPDATE local_runs SET status = 'cleanup_required', updated_at = ?, "
                "completed_at = NULL WHERE id = ?",
                (now, run_id),
            )
            event = await self._append_event_uncommitted(
                conn,
                run_id,
                "run.cleanup_required",
                payload_json=_encode_payload(payload),
                created_at=now,
            )
            await self._update_thread_projection_uncommitted(
                conn,
                run_id=run_id,
                run_status="cleanup_required",
                change_type="run.cleanup_required",
                payload=payload,
                event_high_watermark=int(event["seq"]),
                changed_at=now,
            )
            cursor = await conn.execute(
                "UPDATE local_run_jobs SET quarantined_at = ?, quarantine_reason = ?, "
                "lease_expires_at = NULL, updated_at = ? WHERE id = ? AND run_id = ? "
                "AND status = 'leased' AND lease_owner = ? AND lease_generation = ?",
                (
                    now,
                    reason,
                    now,
                    lease.job_id,
                    run_id,
                    lease.lease_owner,
                    lease.lease_generation,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseFenceError("execution attempt could not be quarantined")
            return event

    async def confirm_quarantined_cleanup(
        self,
        run_id: str,
        *,
        job_id: str,
        lease_owner: str,
        lease_generation: int,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Let only the quarantined owner close its attempt after cleanup."""
        now = _now()
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await conn.execute(
                        "SELECT 1 FROM local_run_jobs WHERE id = ? AND run_id = ? "
                        "AND status = 'leased' AND lease_owner = ? AND lease_generation = ? "
                        "AND quarantined_at IS NOT NULL",
                        (job_id, run_id, lease_owner, lease_generation),
                    )
                ).fetchone()
                if row is None:
                    await conn.rollback()
                    return None
                await self._cancel_unstarted_tool_receipts_uncommitted(
                    conn,
                    run_id=run_id,
                    canceled_at=now,
                    error_type="ParentRunCleanupRequired",
                )
                await conn.execute(
                    "UPDATE local_runs SET status = 'failed', updated_at = ?, completed_at = ? "
                    "WHERE id = ? AND status = 'cleanup_required'",
                    (now, now, run_id),
                )
                event = await self._append_event_uncommitted(
                    conn,
                    run_id,
                    "run.failed",
                    payload_json=_encode_payload(payload),
                    created_at=now,
                )
                await self._update_thread_projection_uncommitted(
                    conn,
                    run_id=run_id,
                    run_status="failed",
                    change_type="run.failed",
                    payload=payload,
                    event_high_watermark=int(event["seq"]),
                    changed_at=now,
                )
                await conn.execute(
                    "UPDATE local_run_jobs SET status = 'dead', updated_at = ?, "
                    "finished_at = ?, lease_expires_at = NULL WHERE id = ?",
                    (now, now, job_id),
                )
                await conn.commit()
                return event
            except BaseException:
                await conn.rollback()
                raise

    async def ensure_lost_execution_quarantined(
        self,
        run_id: str,
        *,
        job_id: str,
        lease_owner: str,
        lease_generation: int,
    ) -> tuple[bool, dict[str, Any] | None]:
        """Atomically record lease loss before an exact owner confirms cleanup.

        Heartbeat can observe expiry before the dispatcher/reaper does. This
        method closes that ordering window: the exact old generation may turn
        its expired lease into quarantine, settle uncertain ledgers, and then
        submit its already-completed cleanup proof. A crash between these two
        calls leaves a safe quarantine rather than a claimable job.
        """
        now = _now()
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await _configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await conn.execute(
                        "SELECT * FROM local_run_jobs WHERE id = ? AND run_id = ? "
                        "AND status = 'leased' AND lease_owner = ? AND lease_generation = ?",
                        (job_id, run_id, lease_owner, lease_generation),
                    )
                ).fetchone()
                if row is None:
                    await conn.rollback()
                    return False, None
                job = dict(row)
                already_quarantined = job.get("quarantined_at") is not None
                if not already_quarantined and str(job.get("lease_expires_at") or "") > now:
                    await conn.rollback()
                    return False, None
                execution_attempt_id = f"{job_id}:{lease_generation}"
                await conn.execute(
                    "UPDATE local_model_calls SET status = 'outcome_unknown', completed_at = ? "
                    "WHERE run_id = ? AND execution_attempt_id = ? "
                    "AND status IN ('reserved', 'streaming')",
                    (now, run_id, execution_attempt_id),
                )
                await self._mark_running_tool_receipts_outcome_unknown_uncommitted(
                    conn,
                    run_id=run_id,
                    execution_attempt_id=execution_attempt_id,
                    error_type="execution_lease_expired",
                    now=now,
                )
                await self._cancel_unstarted_tool_receipts_uncommitted(
                    conn,
                    run_id=run_id,
                    canceled_at=now,
                    error_type="ParentRunCleanupRequired",
                )
                await conn.execute(
                    "UPDATE local_sandbox_processes SET status = 'orphaned', updated_at = ? "
                    "WHERE run_id = ? AND execution_attempt_id = ? AND status = 'running'",
                    (now, run_id, execution_attempt_id),
                )
                lease_cleanup_report = await self._sandbox_cleanup_report_uncommitted(
                    conn,
                    run_id=run_id,
                    execution_attempt_id=execution_attempt_id,
                )
                event: dict[str, Any] | None = None
                if not already_quarantined:
                    await conn.execute(
                        "UPDATE local_run_jobs SET quarantined_at = ?, "
                        "quarantine_reason = 'execution_lease_expired', "
                        "lease_expires_at = NULL, updated_at = ? WHERE id = ?",
                        (now, now, job_id),
                    )
                    await conn.execute(
                        "UPDATE local_runs SET status = 'cleanup_required', updated_at = ?, "
                        "completed_at = NULL WHERE id = ?",
                        (now, run_id),
                    )
                    event = await self._append_event_uncommitted(
                        conn,
                        run_id,
                        "run.cleanup_required",
                        payload_json=_encode_payload(
                            {
                                "error": _lease_expiry_error(lease_cleanup_report),
                                "type": "ExecutionLeaseExpiredError",
                                "retryable": False,
                                "category": "execution_lease_expired",
                                "cleanup": lease_cleanup_report,
                            }
                        ),
                        created_at=now,
                    )
                    await self._update_thread_projection_uncommitted(
                        conn,
                        run_id=run_id,
                        run_status="cleanup_required",
                        change_type="run.cleanup_required",
                        payload={
                            "error": _lease_expiry_error(lease_cleanup_report),
                            "type": "ExecutionLeaseExpiredError",
                            "retryable": False,
                            "category": "execution_lease_expired",
                            "cleanup": lease_cleanup_report,
                        },
                        event_high_watermark=int(event["seq"]),
                        changed_at=now,
                    )
                await conn.commit()
                return True, event
            except BaseException:
                await conn.rollback()
                raise
