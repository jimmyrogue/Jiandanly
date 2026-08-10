"""P11 deterministic execution and child-coordination settlement."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from ..store.sqlite import LocalStore
from .collaboration import _CHILD_TERMINAL_STATUSES, _collaboration_completion_summary
from .errors import (
    ChildCoordinationError,
    ExecutionLeaseExpiredError,
    ExecutionSettlementError,
    RunOutcome,
)
from .failure_projection import _run_failed_payload


class RunSettlement:
    def __init__(
        self,
        store: LocalStore,
        *,
        worker_id: str,
        cancel_child_runs: Callable[[str, Sequence[str]], Awaitable[list[dict[str, Any]]]],
        wait_for_child_runs_terminal: Callable[
            [str, Sequence[str]], Awaitable[list[dict[str, Any]]]
        ],
        wait_for_child_status_change: Callable[
            [str, Sequence[dict[str, Any]], Sequence[str]], Awaitable[None]
        ],
    ) -> None:
        self.store = store
        self._worker_id = worker_id
        self._cancel_child_runs = cancel_child_runs
        self._wait_for_child_runs_terminal = wait_for_child_runs_terminal
        self._wait_for_child_status_change = wait_for_child_status_change

    async def _confirm_lost_attempt_cleanup(
        self,
        *,
        wakeup: asyncio.Event,
        run_id: str,
        execution_attempt_id: str,
        job_id: str,
        lease_generation: int,
        cleanup_report: dict[str, Any],
    ) -> None:
        accepted, quarantine_event = await self.store.ensure_lost_execution_quarantined(
            run_id,
            job_id=job_id,
            lease_owner=self._worker_id,
            lease_generation=lease_generation,
        )
        if not accepted:
            return
        if quarantine_event is not None:
            wakeup.set()
        outcome = RunOutcome(
            "failed",
            "run.failed",
            _run_failed_payload(
                ExecutionLeaseExpiredError("The execution lease expired before cleanup completed.")
            ),
        )
        outcome = await self._settle_execution_outcome(
            run_id=run_id,
            execution_attempt_id=execution_attempt_id,
            outcome=outcome,
            cleanup_report=cleanup_report,
            lease_state="lost",
        )
        event = await self.store.confirm_quarantined_cleanup(
            run_id,
            job_id=job_id,
            lease_owner=self._worker_id,
            lease_generation=lease_generation,
            payload=outcome.payload,
        )
        if event is not None:
            wakeup.set()

    async def _settle_execution_outcome(
        self,
        *,
        run_id: str,
        execution_attempt_id: str,
        outcome: RunOutcome,
        cleanup_report: dict[str, Any],
        lease_state: str = "current",
    ) -> RunOutcome:
        """Build one deterministic P11 result from durable Runtime records."""
        outcome, collaboration = await self._settle_child_coordination(run_id, outcome)
        snapshot = await self.store.execution_settlement_snapshot(run_id)
        model_statuses = snapshot["model_statuses"]
        tool_statuses = snapshot["tool_statuses"]
        violations: list[str] = []
        if any(model_statuses.get(status, 0) for status in ("reserved", "streaming")):
            violations.append("model calls are still active")
        if tool_statuses.get("running", 0):
            violations.append("tool calls are still active")

        assistant = snapshot.get("assistant")
        if outcome.status == "completed":
            if not isinstance(assistant, dict) or not str(assistant.get("content") or "").strip():
                violations.append("final assistant draft is missing")
            else:
                try:
                    pending_calls = json.loads(assistant.get("tool_calls_json") or "[]")
                except (json.JSONDecodeError, TypeError):
                    pending_calls = ["invalid"]
                if pending_calls:
                    violations.append("final assistant draft still contains tool calls")
            if any(
                tool_statuses.get(status, 0) for status in ("prepared", "paused", "outcome_unknown")
            ):
                violations.append("completed run has unsettled tool receipts")
            if model_statuses.get("outcome_unknown", 0):
                violations.append("completed run has unknown model outcomes")

        if violations:
            error = ExecutionSettlementError("; ".join(violations))
            outcome = RunOutcome(
                status="failed",
                event_type="run.failed",
                payload=_run_failed_payload(error),
            )

        run = await self.store.get_run(run_id)
        usage = snapshot["usage"]
        assistant_ref = (
            {
                "message_key": str(assistant["message_key"]),
                "revision": int(assistant["revision"]),
            }
            if isinstance(assistant, dict)
            else None
        )
        execution = {
            "attempt_id": execution_attempt_id,
            "lease": lease_state,
            "checkpoint_id": (run or {}).get("graph_checkpoint_id"),
            "assistant": assistant_ref,
            "model_calls": {
                "statuses": model_statuses,
                **usage,
            },
            "tool_receipts": {"statuses": tool_statuses},
            "artifacts": snapshot["artifacts"],
            "verification": snapshot["verification"],
            "collaboration": collaboration,
            "cleanup": cleanup_report,
        }
        payload = {**outcome.payload, "execution": execution}
        if outcome.status == "completed" and isinstance(assistant, dict):
            payload.update(
                {
                    "final_text": str(assistant["content"]),
                    "final_answer_ref": assistant_ref,
                    **usage,
                }
            )
        return RunOutcome(
            status=outcome.status,
            event_type=outcome.event_type,
            payload=payload,
        )

    async def _settle_child_coordination(
        self,
        run_id: str,
        outcome: RunOutcome,
    ) -> tuple[RunOutcome, dict[str, Any]]:
        children = await self.store.list_child_runs_for_run(run_id)
        summary = _collaboration_completion_summary(children)
        if not children or outcome.status not in {"completed", "failed", "canceled"}:
            return outcome, summary

        if outcome.status in {"failed", "canceled"}:
            active = [
                str(child["id"])
                for child in children
                if child.get("status") not in _CHILD_TERMINAL_STATUSES
            ]
            if active:
                await self._cancel_child_runs(run_id, active)
                await self._wait_for_child_runs_terminal(run_id, active)
                children = await self.store.list_child_runs_for_run(run_id)
            return outcome, _collaboration_completion_summary(children)

        while True:
            summary = _collaboration_completion_summary(children)
            to_cancel = summary["cancel"]
            if to_cancel:
                await self._cancel_child_runs(run_id, to_cancel)
                await self._wait_for_child_runs_terminal(run_id, to_cancel)
                children = await self.store.list_child_runs_for_run(run_id)
                summary = _collaboration_completion_summary(children)
            if summary["impossible"]:
                active = [
                    str(child["id"])
                    for child in children
                    if child.get("status") not in _CHILD_TERMINAL_STATUSES
                ]
                if active:
                    await self._cancel_child_runs(run_id, active)
                    await self._wait_for_child_runs_terminal(run_id, active)
                    children = await self.store.list_child_runs_for_run(run_id)
                    summary = _collaboration_completion_summary(children)
                error = ChildCoordinationError(
                    "Required child work failed or could not satisfy its quorum."
                )
                return (
                    RunOutcome("failed", "run.failed", _run_failed_payload(error)),
                    summary,
                )
            if summary["satisfied"]:
                return outcome, summary
            wait_for = summary["wait_for"]
            if not wait_for:
                error = ChildCoordinationError(
                    "Child completion policy has no satisfiable continuation."
                )
                return (
                    RunOutcome("failed", "run.failed", _run_failed_payload(error)),
                    summary,
                )
            await self._wait_for_child_status_change(run_id, children, wait_for)
            children = await self.store.list_child_runs_for_run(run_id)
