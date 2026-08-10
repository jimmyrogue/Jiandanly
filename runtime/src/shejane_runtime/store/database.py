"""SQLite connection, transaction, and execution-lease primitives."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite


class LeaseFenceError(RuntimeError):
    """An execution attempted to write after losing its job lease."""


@dataclass(frozen=True, slots=True)
class ExecutionLease:
    job_id: str
    run_id: str
    lease_owner: str
    lease_generation: int


CURRENT_EXECUTION_LEASE: ContextVar[ExecutionLease | None] = ContextVar(
    "shejane_execution_lease", default=None
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


async def configure_connection(conn: aiosqlite.Connection) -> None:
    """Apply invariants that SQLite scopes to each individual connection."""
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA busy_timeout=5000")


async def finish_task_despite_cancellation[T](task: asyncio.Task[T]) -> T:
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return await task


class SqliteDatabase:
    """Own the shared connection and fenced write-transaction boundary."""

    def __init__(self, conn: aiosqlite.Connection, db_path: Path) -> None:
        self._conn = conn
        self._db_path = db_path

    async def close(self) -> None:
        await self._conn.close()

    @contextmanager
    def bind_execution_lease(
        self,
        *,
        job_id: str,
        run_id: str,
        lease_owner: str,
        lease_generation: int,
    ):
        lease = ExecutionLease(job_id, run_id, lease_owner, lease_generation)
        token = CURRENT_EXECUTION_LEASE.set(lease)
        try:
            yield lease
        finally:
            CURRENT_EXECUTION_LEASE.reset(token)

    @staticmethod
    def current_execution_lease() -> ExecutionLease | None:
        return CURRENT_EXECUTION_LEASE.get()

    @asynccontextmanager
    async def run_write_transaction(
        self,
        run_id: str,
        *,
        lease: ExecutionLease | None = None,
    ):
        active_lease = lease or CURRENT_EXECUTION_LEASE.get()
        async with aiosqlite.connect(str(self._db_path)) as conn:
            await configure_connection(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                if active_lease is not None:
                    if active_lease.run_id != run_id:
                        raise LeaseFenceError(
                            f"lease for {active_lease.run_id} cannot write run {run_id}"
                        )
                    row = await (
                        await conn.execute(
                            "SELECT 1 FROM local_run_jobs WHERE id = ? AND run_id = ? "
                            "AND status = 'leased' AND lease_owner = ? "
                            "AND lease_generation = ? AND quarantined_at IS NULL "
                            "AND lease_expires_at > ?",
                            (
                                active_lease.job_id,
                                active_lease.run_id,
                                active_lease.lease_owner,
                                active_lease.lease_generation,
                                utc_now(),
                            ),
                        )
                    ).fetchone()
                    if row is None:
                        raise LeaseFenceError(
                            f"run {run_id} lease generation "
                            f"{active_lease.lease_generation} is stale"
                        )
                yield conn
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
