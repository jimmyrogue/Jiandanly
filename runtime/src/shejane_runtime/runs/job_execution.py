"""P5 leased Job attempt execution and heartbeat supervision."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ..store.sqlite import LeaseFenceError
from .errors import (
    ExecutionShutdownError,
    RunOutcome,
)
from .failure_projection import _run_failed_payload
from .job_attempt import RunClaimedAttemptMixin

log = logging.getLogger("shejane_runtime.runs")


class RunJobExecutionMixin(RunClaimedAttemptMixin):
    async def _execute_claimed_job(self, job: dict[str, Any]) -> None:
        run_id = str(job["run_id"])
        generation = int(job["lease_generation"])
        execution_attempt_id = f"{job['id']}:{generation}"
        try:
            input_payload = json.loads(job.get("input_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            input_payload = {}
        resume_payload: dict[str, Any] | None = None
        if job.get("resume_json"):
            try:
                decoded_resume = json.loads(job["resume_json"])
                if isinstance(decoded_resume, dict):
                    resume_payload = decoded_resume
            except (json.JSONDecodeError, TypeError):
                pass

        self._goals[run_id] = str(input_payload.get("goal") or "")
        self._user_inputs[run_id] = str(
            input_payload.get("user_input") or input_payload.get("goal") or ""
        )
        self._workspaces[run_id] = input_payload.get("workspace_path")
        self._histories[run_id] = list(input_payload.get("history") or [])
        self._attachments[run_id] = list(
            dict(input_payload.get("metadata") or {}).get("_attachments") or []
        )
        self._settings_overrides[run_id] = dict(input_payload.get("settings") or {})
        self._run_metadata[run_id] = dict(input_payload.get("metadata") or {})
        self._modes[run_id] = str(input_payload.get("mode") or "auto")
        wakeup = asyncio.Event()
        self._wakeups[run_id] = wakeup

        owner_task = asyncio.current_task()
        assert owner_task is not None
        self._started_jobs.add(owner_task)
        heartbeat = asyncio.create_task(
            self._heartbeat_job(job, owner_task),
            name=f"run-job-heartbeat:{run_id}:{generation}",
        )
        try:
            await self._run_claimed_attempt(
                job=job,
                input_payload=input_payload,
                resume_payload=resume_payload,
                wakeup=wakeup,
                owner_task=owner_task,
                execution_attempt_id=execution_attempt_id,
            )
        except LeaseFenceError:
            self._lost_leases.add(owner_task)
            log.info("run %s stopped after losing lease generation %s", run_id, generation)
            await self._confirm_lost_attempt_cleanup(
                wakeup=wakeup,
                run_id=run_id,
                execution_attempt_id=execution_attempt_id,
                job_id=str(job["id"]),
                lease_generation=generation,
                cleanup_report={"status": "completed"},
            )
        except asyncio.CancelledError:
            try:
                if owner_task in self._unconfirmed_cleanup:
                    # A cleanup failure is already or is about to be durably
                    # quarantined. Never submit a positive cleanup proof.
                    pass
                elif owner_task in self._lost_leases:
                    await self._confirm_lost_attempt_cleanup(
                        wakeup=wakeup,
                        run_id=run_id,
                        execution_attempt_id=execution_attempt_id,
                        job_id=str(job["id"]),
                        lease_generation=generation,
                        cleanup_report={"status": "completed"},
                    )
                else:
                    with self.store.bind_execution_lease(
                        job_id=str(job["id"]),
                        run_id=run_id,
                        lease_owner=self._worker_id,
                        lease_generation=generation,
                    ):
                        interrupted = (
                            RunOutcome(
                                "failed",
                                "run.failed",
                                _run_failed_payload(
                                    ExecutionShutdownError(
                                        "The Runtime shut down before this run completed."
                                    )
                                ),
                            )
                            if self._shutting_down
                            else RunOutcome("canceled", "run.canceled", {})
                        )
                        interrupted = await self._settle_execution_outcome(
                            run_id=run_id,
                            execution_attempt_id=execution_attempt_id,
                            outcome=interrupted,
                            cleanup_report={"status": "completed"},
                        )
                        await self._commit_run_result(
                            wakeup,
                            run_id,
                            interrupted.event_type,
                            interrupted.payload,
                            status=interrupted.status,
                        )
            except LeaseFenceError:
                self._lost_leases.add(owner_task)
        except Exception as exc:
            log.exception("run %s execution attempt crashed before result commit", run_id)
            if owner_task not in self._unconfirmed_cleanup:
                try:
                    with self.store.bind_execution_lease(
                        job_id=str(job["id"]),
                        run_id=run_id,
                        lease_owner=self._worker_id,
                        lease_generation=generation,
                    ):
                        run = await self.store.get_run(run_id)
                        if run is not None and run.get("status") in {"queued", "running"}:
                            crashed = await self._settle_execution_outcome(
                                run_id=run_id,
                                execution_attempt_id=execution_attempt_id,
                                outcome=RunOutcome(
                                    "failed",
                                    "run.failed",
                                    _run_failed_payload(exc),
                                ),
                                cleanup_report={"status": "completed"},
                            )
                            await self._commit_run_result(
                                wakeup,
                                run_id,
                                crashed.event_type,
                                crashed.payload,
                                status=crashed.status,
                            )
                except LeaseFenceError:
                    self._lost_leases.add(owner_task)
                except Exception:
                    log.exception("run %s crash result could not be persisted", run_id)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            try:
                if not self._shutting_down and owner_task not in self._lost_leases:
                    run = await self.store.get_run(run_id)
                    run_status = str((run or {}).get("status") or "")
                    job_status = {
                        "completed": "completed",
                        "failed": "dead",
                        "canceled": "canceled",
                        "waiting_permission": "completed",
                        "waiting_input": "completed",
                    }.get(run_status)
                    if job_status is not None:
                        await self.store.finish_run_job(
                            str(job["id"]),
                            lease_owner=self._worker_id,
                            lease_generation=generation,
                            status=job_status,
                        )
            except Exception:
                log.exception("run %s job finalization could not be persisted", run_id)
            finally:
                self._lost_leases.discard(owner_task)
                self._unconfirmed_cleanup.discard(owner_task)
                self._started_jobs.discard(owner_task)
                wakeup.set()
                if self._tasks.get(run_id) is owner_task:
                    self._tasks.pop(run_id, None)
                self._event_stream.discard_if_idle(run_id)
                if self._wakeups.get(run_id) is wakeup:
                    self._wakeups.pop(run_id, None)
                self._goals.pop(run_id, None)
                self._user_inputs.pop(run_id, None)
                self._workspaces.pop(run_id, None)
                self._histories.pop(run_id, None)
                self._attachments.pop(run_id, None)
                self._settings_overrides.pop(run_id, None)
                self._run_metadata.pop(run_id, None)
                self._modes.pop(run_id, None)
                self._slots.release()
                self._job_wakeup.set()

    async def _heartbeat_job(
        self,
        job: dict[str, Any],
        owner_task: asyncio.Task[Any],
    ) -> None:
        generation = int(job["lease_generation"])
        try:
            while True:
                renewed, cancel_requested = await self.store.renew_run_job(
                    str(job["id"]),
                    lease_owner=self._worker_id,
                    lease_generation=generation,
                    lease_seconds=self._lease_seconds,
                )
                if not renewed:
                    current = await self.store.get_run_job(str(job["id"]))
                    if (
                        owner_task in self._unconfirmed_cleanup
                        and current is not None
                        and current.get("status") == "leased"
                        and current.get("quarantined_at") is not None
                        and current.get("lease_owner") == self._worker_id
                        and int(current.get("lease_generation") or 0) == generation
                    ):
                        return
                    if (
                        current is not None
                        and current.get("status") in {"completed", "canceled", "dead"}
                        and current.get("lease_owner") == self._worker_id
                        and int(current.get("lease_generation") or 0) == generation
                    ):
                        return
                    self._lost_leases.add(owner_task)
                    owner_task.cancel()
                    return
                if cancel_requested:
                    owner_task.cancel()
                    return
                await asyncio.sleep(max(1.0, self._lease_seconds / 3))
        except asyncio.CancelledError:
            raise
