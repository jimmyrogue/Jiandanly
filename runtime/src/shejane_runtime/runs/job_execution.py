"""P5 leased Job attempt execution and heartbeat supervision."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from typing import Any

from ..store.sqlite import LeaseFenceError
from .errors import (
    ExecutionIdentityError,
    ExecutionModelBindingError,
    ExecutionShutdownError,
    ExecutionSkillBindingError,
    ExecutionWorkspaceError,
    RunOutcome,
)
from .failure_projection import _run_failed_payload
from .inputs import _resolved_attachment_bindings
from .stream_state import _json_object

log = logging.getLogger("shejane_runtime.runs")


class RunJobExecutionMixin:
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
            with self.store.bind_execution_lease(
                job_id=str(job["id"]),
                run_id=run_id,
                lease_owner=self._worker_id,
                lease_generation=generation,
            ):
                run = await self.store.get_run(run_id)
                principal_id = input_payload.get("principal_id")
                identity_error: str | None = None
                if run is None:
                    identity_error = f"run {run_id} is missing for claimed job"
                elif not isinstance(principal_id, str) or not principal_id:
                    identity_error = f"run job {job['id']} is missing principal_id"
                elif principal_id != run.get("principal_id"):
                    identity_error = f"run job {job['id']} principal_id does not match its run"
                elif input_payload.get("workspace_path") != run.get("workspace_path"):
                    identity_error = f"run job {job['id']} workspace_path does not match its run"
                elif input_payload.get("mode") != run.get("mode"):
                    identity_error = f"run job {job['id']} model does not match its run"
                elif input_payload.get("run_kind", run.get("run_kind")) != run.get("run_kind"):
                    identity_error = f"run job {job['id']} kind does not match its run"
                elif input_payload.get("root_run_id", run.get("root_run_id")) != run.get(
                    "root_run_id"
                ):
                    identity_error = f"run job {job['id']} root does not match its run"
                elif input_payload.get(
                    "agent_definition_id", run.get("agent_definition_id")
                ) != run.get("agent_definition_id"):
                    identity_error = f"run job {job['id']} Agent definition does not match its run"
                elif input_payload.get(
                    "agent_definition_version", run.get("agent_definition_version")
                ) != run.get("agent_definition_version"):
                    identity_error = (
                        f"run job {job['id']} Agent definition version does not match its run"
                    )
                elif int(
                    input_payload.get(
                        "collaboration_depth",
                        run.get("collaboration_depth") or 0,
                    )
                ) != int(run.get("collaboration_depth") or 0):
                    identity_error = (
                        f"run job {job['id']} collaboration depth does not match its run"
                    )
                frozen_settings = _json_object(run.get("settings_json")) if run is not None else {}
                if identity_error is None and input_payload.get("settings") != frozen_settings:
                    identity_error = f"run job {job['id']} settings snapshot does not match its run"
                binding = frozen_settings.get("_model_binding")
                if (
                    identity_error is None
                    and job.get("kind") == "start"
                    and frozen_settings.get("_snapshot_version") == 1
                    and (
                        not isinstance(binding, dict)
                        or binding.get("requested_model") != run.get("mode")
                    )
                ):
                    identity_error = f"run job {job['id']} model binding does not match its run"
                if identity_error is not None:
                    log.error(identity_error)
                    if run is None:
                        await self.store.finish_run_job(
                            str(job["id"]),
                            lease_owner=self._worker_id,
                            lease_generation=generation,
                            status="dead",
                        )
                        return
                    outcome = await self._settle_execution_outcome(
                        run_id=run_id,
                        execution_attempt_id=execution_attempt_id,
                        outcome=RunOutcome(
                            "failed",
                            "run.failed",
                            _run_failed_payload(ExecutionIdentityError(identity_error)),
                        ),
                        cleanup_report={"status": "completed"},
                    )
                    await self._commit_run_result(
                        wakeup,
                        run_id,
                        outcome.event_type,
                        outcome.payload,
                        status=outcome.status,
                    )
                    return
                assert isinstance(principal_id, str)
                run = await self._reconcile_graph_head(run)
                self._modes[run_id] = str(run.get("mode") or "auto")
                workspace_path = run.get("workspace_path")
                workspace_error = await self.store.workspace_admission_error(
                    principal_id=principal_id,
                    path=str(workspace_path) if workspace_path is not None else None,
                )
                if workspace_error is not None:
                    outcome = await self._settle_execution_outcome(
                        run_id=run_id,
                        execution_attempt_id=execution_attempt_id,
                        outcome=RunOutcome(
                            "failed",
                            "run.failed",
                            _run_failed_payload(ExecutionWorkspaceError(workspace_error)),
                        ),
                        cleanup_report={"status": "completed"},
                    )
                    await self._commit_run_result(
                        wakeup,
                        run_id,
                        outcome.event_type,
                        outcome.payload,
                        status=outcome.status,
                    )
                    return
                run_metadata = _json_object(run.get("metadata_json"))
                attachment_bindings, attachment_error = await _resolved_attachment_bindings(
                    self.store,
                    run_id,
                    list(run_metadata.get("_attachments") or []),
                )
                if attachment_error is not None:
                    outcome = await self._settle_execution_outcome(
                        run_id=run_id,
                        execution_attempt_id=execution_attempt_id,
                        outcome=RunOutcome(
                            "failed",
                            "run.failed",
                            _run_failed_payload(ExecutionWorkspaceError(attachment_error)),
                        ),
                        cleanup_report={"status": "completed"},
                    )
                    await self._commit_run_result(
                        wakeup,
                        run_id,
                        outcome.event_type,
                        outcome.payload,
                        status=outcome.status,
                    )
                    return
                self._attachments[run_id] = attachment_bindings
                skill_binding_error = await self._model_bindings.skill_binding_error(
                    frozen_settings
                )
                if skill_binding_error is not None:
                    outcome = await self._settle_execution_outcome(
                        run_id=run_id,
                        execution_attempt_id=execution_attempt_id,
                        outcome=RunOutcome(
                            "failed",
                            "run.failed",
                            _run_failed_payload(ExecutionSkillBindingError(skill_binding_error)),
                        ),
                        cleanup_report={"status": "completed"},
                    )
                    await self._commit_run_result(
                        wakeup,
                        run_id,
                        outcome.event_type,
                        outcome.payload,
                        status=outcome.status,
                    )
                    return
                binding_error, model_api_key = await self._model_binding_error(
                    principal_id,
                    frozen_settings,
                )
                if binding_error is not None:
                    outcome = await self._settle_execution_outcome(
                        run_id=run_id,
                        execution_attempt_id=execution_attempt_id,
                        outcome=RunOutcome(
                            "failed",
                            "run.failed",
                            _run_failed_payload(ExecutionModelBindingError(binding_error)),
                        ),
                        cleanup_report={"status": "completed"},
                    )
                    await self._commit_run_result(
                        wakeup,
                        run_id,
                        outcome.event_type,
                        outcome.payload,
                        status=outcome.status,
                    )
                    return
                self._workspaces[run_id] = workspace_path
                self._settings_overrides[run_id] = frozen_settings
                outcome: RunOutcome | None = None
                cleanup_report: dict[str, Any] = {"status": "completed"}
                resource_stack = AsyncExitStack()
                await resource_stack.__aenter__()
                drive_error: BaseException | None = None
                try:
                    outcome = await self._drive_run(
                        run_id=run_id,
                        principal_id=principal_id,
                        resume_payload=resume_payload,
                        mode=self._modes[run_id],
                        checkpointer=self._fenced_checkpointer,
                        model_api_key=model_api_key,
                        resource_stack=resource_stack,
                        graph_thread_id=str(run["graph_thread_id"]),
                        graph_checkpoint_id=run.get("graph_checkpoint_id"),
                        graph_input_kind=str(run.get("graph_input_kind") or "new"),
                        execution_attempt_id=execution_attempt_id,
                    )
                except BaseException as exc:
                    drive_error = exc

                cleanup_error: BaseException | None = None
                try:
                    await resource_stack.aclose()
                except BaseException as exc:
                    cleanup_error = exc

                if cleanup_error is not None:
                    # Set before the quarantine transaction. Once cleanup has
                    # failed, no concurrent heartbeat/shutdown cancellation may
                    # reinterpret this attempt as safely cleaned.
                    self._unconfirmed_cleanup.add(owner_task)
                    cleanup_report = {
                        "status": "failed",
                        "error_type": type(cleanup_error).__name__,
                    }
                    log.error(
                        "run %s resource cleanup failed: %s",
                        run_id,
                        type(cleanup_error).__name__,
                    )
                    quarantine_payload = {
                        "error": (
                            "The Runtime could not prove that all execution resources stopped. "
                            "This run is quarantined and cannot be retried automatically."
                        ),
                        "type": type(cleanup_error).__name__,
                        "retryable": False,
                        "category": "execution_cleanup_unconfirmed",
                        "cleanup": cleanup_report,
                    }
                    try:
                        await self.store.quarantine_execution_attempt(
                            run_id,
                            reason="execution_cleanup_unconfirmed",
                            payload=quarantine_payload,
                        )
                        wakeup.set()
                    except LeaseFenceError:
                        # The lease reaper may have quarantined this exact
                        # generation first. Leaving it sealed is the safe result.
                        self._lost_leases.add(owner_task)
                    outcome = None
                elif isinstance(drive_error, (asyncio.CancelledError, LeaseFenceError)):
                    lease_lost = owner_task in self._lost_leases or isinstance(
                        drive_error, LeaseFenceError
                    )
                    if lease_lost:
                        await self._confirm_lost_attempt_cleanup(
                            wakeup=wakeup,
                            run_id=run_id,
                            execution_attempt_id=execution_attempt_id,
                            job_id=str(job["id"]),
                            lease_generation=generation,
                            cleanup_report=cleanup_report,
                        )
                        outcome = None
                    elif self._shutting_down:
                        outcome = RunOutcome(
                            "failed",
                            "run.failed",
                            _run_failed_payload(
                                ExecutionShutdownError(
                                    "The Runtime shut down before this run completed."
                                )
                            ),
                        )
                    else:
                        outcome = RunOutcome("canceled", "run.canceled", {})
                elif isinstance(drive_error, Exception):
                    outcome = RunOutcome(
                        status="failed",
                        event_type="run.failed",
                        payload=_run_failed_payload(
                            drive_error,
                            secrets=(model_api_key,) if model_api_key else (),
                        ),
                    )
                elif drive_error is not None:
                    raise drive_error

                if outcome is not None:
                    outcome = await self._settle_execution_outcome(
                        run_id=run_id,
                        execution_attempt_id=execution_attempt_id,
                        outcome=outcome,
                        cleanup_report=cleanup_report,
                    )
                    await self._commit_run_result(
                        wakeup,
                        run_id,
                        outcome.event_type,
                        outcome.payload,
                        status=outcome.status,
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
