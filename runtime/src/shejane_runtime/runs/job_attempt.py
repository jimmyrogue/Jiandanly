"""Admission, resource cleanup, and settlement for one claimed Run Job."""

from __future__ import annotations

import asyncio
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


def _claimed_job_identity_error(
    job: dict[str, Any],
    input_payload: dict[str, Any],
    run: dict[str, Any] | None,
    frozen_settings: dict[str, Any],
) -> str | None:
    run_id = str(job["run_id"])
    principal_id = input_payload.get("principal_id")
    if run is None:
        return f"run {run_id} is missing for claimed job"
    if not isinstance(principal_id, str) or not principal_id:
        return f"run job {job['id']} is missing principal_id"
    if principal_id != run.get("principal_id"):
        return f"run job {job['id']} principal_id does not match its run"
    if input_payload.get("workspace_path") != run.get("workspace_path"):
        return f"run job {job['id']} workspace_path does not match its run"
    if input_payload.get("mode") != run.get("mode"):
        return f"run job {job['id']} model does not match its run"
    if input_payload.get("run_kind", run.get("run_kind")) != run.get("run_kind"):
        return f"run job {job['id']} kind does not match its run"
    if input_payload.get("root_run_id", run.get("root_run_id")) != run.get("root_run_id"):
        return f"run job {job['id']} root does not match its run"
    if input_payload.get("agent_definition_id", run.get("agent_definition_id")) != run.get(
        "agent_definition_id"
    ):
        return f"run job {job['id']} Agent definition does not match its run"
    if input_payload.get(
        "agent_definition_version", run.get("agent_definition_version")
    ) != run.get("agent_definition_version"):
        return f"run job {job['id']} Agent definition version does not match its run"
    if int(input_payload.get("collaboration_depth", run.get("collaboration_depth") or 0)) != int(
        run.get("collaboration_depth") or 0
    ):
        return f"run job {job['id']} collaboration depth does not match its run"
    if input_payload.get("settings") != frozen_settings:
        return f"run job {job['id']} settings snapshot does not match its run"
    binding = frozen_settings.get("_model_binding")
    if (
        job.get("kind") == "start"
        and frozen_settings.get("_snapshot_version") == 1
        and (not isinstance(binding, dict) or binding.get("requested_model") != run.get("mode"))
    ):
        return f"run job {job['id']} model binding does not match its run"
    return None


class RunClaimedAttemptMixin:
    async def _fail_claimed_attempt(
        self,
        *,
        wakeup: asyncio.Event,
        run_id: str,
        execution_attempt_id: str,
        error: Exception,
    ) -> None:
        outcome = await self._settle_execution_outcome(
            run_id=run_id,
            execution_attempt_id=execution_attempt_id,
            outcome=RunOutcome("failed", "run.failed", _run_failed_payload(error)),
            cleanup_report={"status": "completed"},
        )
        await self._commit_run_result(
            wakeup,
            run_id,
            outcome.event_type,
            outcome.payload,
            status=outcome.status,
        )

    async def _run_claimed_attempt(
        self,
        *,
        job: dict[str, Any],
        input_payload: dict[str, Any],
        resume_payload: dict[str, Any] | None,
        wakeup: asyncio.Event,
        owner_task: asyncio.Task[Any],
        execution_attempt_id: str,
    ) -> None:
        run_id = str(job["run_id"])
        generation = int(job["lease_generation"])
        with self.store.bind_execution_lease(
            job_id=str(job["id"]),
            run_id=run_id,
            lease_owner=self._worker_id,
            lease_generation=generation,
        ):
            run = await self.store.get_run(run_id)
            frozen_settings = _json_object(run.get("settings_json")) if run is not None else {}
            identity_error = _claimed_job_identity_error(
                job,
                input_payload,
                run,
                frozen_settings,
            )
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
                await self._fail_claimed_attempt(
                    wakeup=wakeup,
                    run_id=run_id,
                    execution_attempt_id=execution_attempt_id,
                    error=ExecutionIdentityError(identity_error),
                )
                return

            principal_id = input_payload["principal_id"]
            run = await self._reconcile_graph_head(run)
            self._modes[run_id] = str(run.get("mode") or "auto")
            workspace_path = run.get("workspace_path")
            workspace_error = await self.store.workspace_admission_error(
                principal_id=principal_id,
                path=str(workspace_path) if workspace_path is not None else None,
            )
            if workspace_error is not None:
                await self._fail_claimed_attempt(
                    wakeup=wakeup,
                    run_id=run_id,
                    execution_attempt_id=execution_attempt_id,
                    error=ExecutionWorkspaceError(workspace_error),
                )
                return

            run_metadata = _json_object(run.get("metadata_json"))
            attachment_bindings, attachment_error = await _resolved_attachment_bindings(
                self.store,
                run_id,
                list(run_metadata.get("_attachments") or []),
            )
            if attachment_error is not None:
                await self._fail_claimed_attempt(
                    wakeup=wakeup,
                    run_id=run_id,
                    execution_attempt_id=execution_attempt_id,
                    error=ExecutionWorkspaceError(attachment_error),
                )
                return
            self._attachments[run_id] = attachment_bindings

            skill_binding_error = await self._model_bindings.skill_binding_error(frozen_settings)
            if skill_binding_error is not None:
                await self._fail_claimed_attempt(
                    wakeup=wakeup,
                    run_id=run_id,
                    execution_attempt_id=execution_attempt_id,
                    error=ExecutionSkillBindingError(skill_binding_error),
                )
                return
            binding_error, model_api_key = await self._model_binding_error(
                principal_id,
                frozen_settings,
            )
            if binding_error is not None:
                await self._fail_claimed_attempt(
                    wakeup=wakeup,
                    run_id=run_id,
                    execution_attempt_id=execution_attempt_id,
                    error=ExecutionModelBindingError(binding_error),
                )
                return

            self._workspaces[run_id] = workspace_path
            self._settings_overrides[run_id] = frozen_settings
            await self._drive_claimed_attempt(
                job=job,
                run=run,
                principal_id=principal_id,
                resume_payload=resume_payload,
                wakeup=wakeup,
                owner_task=owner_task,
                execution_attempt_id=execution_attempt_id,
                model_api_key=model_api_key,
            )

    async def _drive_claimed_attempt(
        self,
        *,
        job: dict[str, Any],
        run: dict[str, Any],
        principal_id: str,
        resume_payload: dict[str, Any] | None,
        wakeup: asyncio.Event,
        owner_task: asyncio.Task[Any],
        execution_attempt_id: str,
        model_api_key: str | None,
    ) -> None:
        run_id = str(job["run_id"])
        generation = int(job["lease_generation"])
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
            # A failed cleanup can never be reinterpreted as safely completed.
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
                self._lost_leases.add(owner_task)
            outcome = None
        elif isinstance(drive_error, (asyncio.CancelledError, LeaseFenceError)):
            lease_lost = owner_task in self._lost_leases or isinstance(drive_error, LeaseFenceError)
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
                        ExecutionShutdownError("The Runtime shut down before this run completed.")
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
