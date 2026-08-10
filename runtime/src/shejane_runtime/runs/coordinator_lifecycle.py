"""Run coordinator startup, shutdown, orphan recovery, and Job dispatch."""

from __future__ import annotations

import asyncio
import logging
import time

from ..sandbox_reaper import reap_sandbox_processes

log = logging.getLogger("shejane_runtime.runs")

_SANDBOX_SWEEP_SECONDS = 5.0


def _shutdown_timeout_seconds() -> float:
    # Preserve the historical shejane_runtime.runs monkeypatch seam.
    from . import RUN_SHUTDOWN_TIMEOUT_SECONDS

    return RUN_SHUTDOWN_TIMEOUT_SECONDS


class RunCoordinatorLifecycleMixin:
    def start(self) -> None:
        if self._dispatcher_task is None or self._dispatcher_task.done():
            self._shutting_down = False
            self._dispatcher_task = asyncio.create_task(
                self._dispatch_jobs(), name="run-job-dispatcher"
            )
            self._job_wakeup.set()

    async def stop(self) -> None:
        shutdown_timeout = _shutdown_timeout_seconds()
        self._shutting_down = True
        if self._dispatcher_task is not None:
            self._dispatcher_task.cancel()
            try:
                await self._dispatcher_task
            except asyncio.CancelledError:
                pass
            self._dispatcher_task = None
        tasks = list(self._tasks.values())
        if tasks:
            for task in tasks:
                task.cancel()
            _done, pending = await asyncio.wait(tasks, timeout=shutdown_timeout)
            if pending:
                raise RuntimeError(
                    "runtime shutdown could not confirm cleanup for "
                    f"{len(pending)} execution attempt(s)"
                )
        callbacks = set(self._terminal_callback_tasks)
        if callbacks:
            _done, pending = await asyncio.wait(
                callbacks,
                timeout=shutdown_timeout,
            )
            if pending:
                log.warning("central diagnostics shutdown timed out pending=%s", len(pending))
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

    async def recover_orphans(self) -> None:
        """At boot, reconcile runs left non-terminal by the previous process.
        Runs backed by a pending/leased durable job remain owned by the job
        system. Legacy queued/running rows without a job are failed. Paused
        checkpointed runs remain available for a future resume command."""
        # Sandbox trees first: they are the part still consuming the machine.
        # Records left as running belong to the process that died, since this
        # Runtime has not spawned anything yet -- which holds because the port
        # binding keeps a second Runtime off the same data dir.
        await reap_sandbox_processes(self.store, include_running=True)
        try:
            active = await self.store.list_active_runs()
        except Exception:
            log.exception("recover_orphans: failed to list active runs")
            return
        failed = 0
        kept = 0
        for run in active:
            run_id = run.get("id")
            if not run_id:
                continue
            try:
                run = await self._reconcile_graph_head(run)
            except Exception:
                log.exception("recover_orphans: graph head reconciliation failed for %s", run_id)
                try:
                    await self.store.commit_run_result(
                        run_id,
                        status="failed",
                        event_type="run.failed",
                        payload={
                            "error": "The graph checkpoint head could not be reconciled.",
                            "type": "GraphHeadConflictError",
                            "retryable": False,
                            "category": "checkpoint_incompatible",
                        },
                        orphan_recovery=True,
                    )
                    failed += 1
                except Exception:
                    log.exception("recover_orphans: failed to fail run %s", run_id)
                continue
            status = run.get("status")
            if status in ("queued", "running"):
                active_job = await self.store.get_active_run_job(run_id)
                if active_job is not None:
                    kept += 1
                    continue
                try:
                    await self.store.commit_run_result(
                        run_id,
                        status="failed",
                        event_type="run.failed",
                        payload={
                            "error": "The local runtime stopped before this run completed.",
                            "type": "RuntimeInterruptedError",
                            "retryable": True,
                            "category": "runtime_interrupted",
                        },
                        orphan_recovery=True,
                    )
                    failed += 1
                except Exception:
                    log.exception("recover_orphans: failed to fail run %s", run_id)
            elif status in {"waiting_permission", "waiting_input"}:
                resume_payload = await self.store.latest_resolved_wait_cycle_payload(run_id)
                if resume_payload is not None:
                    await self.resume_run(run_id=run_id, decision=resume_payload)
                kept += 1
        if failed or kept:
            log.info(
                "recover_orphans: %d orphaned run(s) marked failed, "
                "%d waiting run(s) left resumable",
                failed,
                kept,
            )

    async def _reap_expired_sandboxes(self) -> None:
        """Clear sandboxes a lease expiry orphaned, while the loop is idle.

        `claim_run_job` marks those records but cannot signal anything from
        inside its write transaction, so the idle branch is where they get
        collected. Rate limited because the common answer is "nothing to do".
        """

        now = time.monotonic()
        if now - self._last_sandbox_sweep < _SANDBOX_SWEEP_SECONDS:
            return
        self._last_sandbox_sweep = now
        try:
            await reap_sandbox_processes(self.store)
        except Exception:
            log.exception("sandbox sweep failed")

    async def _dispatch_jobs(self) -> None:
        try:
            while True:
                await self._slots.acquire()
                self._job_wakeup.clear()
                try:
                    job = await self.store.claim_run_job(
                        worker_id=self._worker_id,
                        lease_seconds=self._lease_seconds,
                    )
                except asyncio.CancelledError:
                    self._slots.release()
                    raise
                except Exception:
                    self._slots.release()
                    log.exception("run job claim failed")
                    await asyncio.sleep(0.5)
                    continue
                if job is None:
                    self._slots.release()
                    await self._reap_expired_sandboxes()
                    try:
                        await asyncio.wait_for(self._job_wakeup.wait(), timeout=0.5)
                    except TimeoutError:
                        pass
                    continue
                run_id = str(job["run_id"])
                task = asyncio.create_task(
                    self._execute_claimed_job(job),
                    name=f"run-job:{run_id}:{job['lease_generation']}",
                )
                self._tasks[run_id] = task
        except asyncio.CancelledError:
            raise

    def wake_jobs(self) -> None:
        self._job_wakeup.set()
