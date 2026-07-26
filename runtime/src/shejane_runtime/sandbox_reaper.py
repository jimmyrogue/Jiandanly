"""Stop sandbox trees whose Runtime died before it could clean them up.

The store records which launchers an execution attempt is running; this turns
those records into signals. It is deliberately the only place that acts on
them, because that is where the irreversible part lives: the store may only
ever *mark* a record, never kill anything, so a rolled-back transaction can
never leave a process already dead.

Every kill is gated on the recorded identity still matching the live process.
A pid on its own is not enough to act on -- the kernel recycles pids, so a
record left behind by a killed Runtime can name a program that has nothing to
do with the sandbox.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .plugins.sandbox_runtime import SandboxProcessIdentity, terminate_sandbox_process
from .store.sqlite import LocalStore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReapSummary:
    """What one sweep did, for logging and for the cleanup verdict."""

    reaped: int = 0
    gone: int = 0
    stale: int = 0

    @property
    def total(self) -> int:
        return self.reaped + self.gone + self.stale

    @property
    def settled(self) -> bool:
        """Whether every record resolved to a process that is now stopped.

        A stale record is not settled: it means the pid was recycled, so the
        sandbox it named was never confirmed to have stopped.
        """

        return self.stale == 0


async def reap_sandbox_processes(
    store: LocalStore,
    *,
    include_running: bool = False,
) -> ReapSummary:
    """Signal every sandbox tree the store still holds a record for."""

    try:
        records = await store.list_reapable_sandbox_processes(include_running=include_running)
    except Exception:
        log.exception("sandbox reaper: could not list records")
        return ReapSummary()

    reaped = gone = stale = 0
    for record in records:
        identity = SandboxProcessIdentity(
            pid=int(record["pid"]),
            started_at=str(record["process_started_at"]),
            settings_path=str(record["settings_path"]),
        )
        try:
            # Blocking: it reads /proc or shells out to ps, and waits for the
            # tree to go down. Keep it off the event loop.
            outcome = await asyncio.to_thread(terminate_sandbox_process, identity)
        except Exception:
            log.exception("sandbox reaper: could not terminate pid %s", identity.pid)
            continue
        if outcome == "reaped":
            reaped += 1
        elif outcome == "gone":
            gone += 1
        else:
            stale += 1
            log.warning(
                "sandbox reaper: pid %s no longer matches its record, leaving it alone",
                identity.pid,
            )
        try:
            await store.settle_sandbox_process(str(record["id"]), status=outcome)
        except Exception:
            log.exception("sandbox reaper: could not settle record %s", record["id"])

    summary = ReapSummary(reaped=reaped, gone=gone, stale=stale)
    if summary.total:
        log.info(
            "sandbox reaper: %d killed, %d already gone, %d left alone as unrecognised",
            summary.reaped,
            summary.gone,
            summary.stale,
        )
    return summary
