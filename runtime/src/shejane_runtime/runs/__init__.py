"""Run coordinator — leases durable SQLite jobs into supervised asyncio
tasks and exposes submit / cancel / resume / stream primitives to FastAPI.

Streaming pipeline
------------------
For each leased run:

  agent.astream(version="v2", stream_mode=[...])
       │ (LangGraph emits typed stream parts)
       ▼
RunCoordinator._drive_run persists state changes and broadcasts temporary
model output through bounded per-subscriber queues. `/v1/runs/:id/stream`
merges both sources; reconnects replay only the durable database cursor.

Cancellation is a `task.cancel()` on the driver coroutine. LangGraph
propagates CancelledError into the graph and the checkpointer persists
state up to the last superstep.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.base import BaseStore

from ..agent.child_runs import ChildRunControl
from ..agent.mailbox import AgentMailboxControl
from ..config import Settings, get_settings
from ..dev_trace import trace_stream_event
from ..model_services.credentials import get_model_api_key
from ..plugins.catalog import PluginCatalog
from ..run_configuration import (
    _execution_policy_snapshot as _execution_policy_snapshot,
)
from ..store.fenced_checkpointer import FencedCheckpointer
from ..store.sqlite import (
    GraphHeadConflictError,
    LocalStore,
    RunAdmissionError,
    WorkspaceAdmissionError,
)
from ..tools.mcp import MCPToolCatalog
from .admission import admit_fork, admit_run
from .collaboration import (
    RunCollaboration,
)
from .collaboration import (
    _collaboration_completion_summary as _collaboration_completion_summary,
)
from .coordinator_lifecycle import RunCoordinatorLifecycleMixin
from .errors import (
    RUN_SHUTDOWN_TIMEOUT_SECONDS as RUN_SHUTDOWN_TIMEOUT_SECONDS,
)
from .errors import (
    ExecutionSettlementError as ExecutionSettlementError,
)
from .errors import (
    RunOutcome,
)
from .event_publisher import RunEventPublisherMixin
from .event_stream import RunEventStream
from .graph_driver import RunGraphDriverMixin
from .inputs import _plugin_input_snapshots as _plugin_input_snapshots
from .job_execution import RunJobExecutionMixin
from .model_bindings import RunModelBindings
from .model_connection_coordination import RunModelConnectionCoordinationMixin
from .settlement import RunSettlement
from .stream_state import (
    _assistant_draft_from_state as _assistant_draft_from_state,
)
from .stream_state import (
    _assistant_draft_from_update as _assistant_draft_from_update,
)
from .stream_state import (
    _assistant_round_from_update as _assistant_round_from_update,
)
from .stream_state import (
    _checkpoint_id_from_config,
    _checkpoint_is_ancestor,
)
from .stream_state import (
    _completion_failure_payload as _completion_failure_payload,
)
from .stream_state import (
    _run_failed_payload as _run_failed_payload,
)
from .stream_state import (
    _waiting_status_for_interrupts as _waiting_status_for_interrupts,
)

# Attachments are admitted as immutable Runtime-owned references. Model-facing
# attachment and PDF reads have a 200 MiB ceiling; other workspace, Skill,
# Memory, and subagent reads use 20 MiB in agent/backends.py.
_IMAGE_TOOL_CAPABILITIES = {
    "image.generate": "image_generation",
    "image.edit": "image_editing",
}


class RunCoordinator(
    RunCoordinatorLifecycleMixin,
    RunJobExecutionMixin,
    RunGraphDriverMixin,
    RunEventPublisherMixin,
    RunModelConnectionCoordinationMixin,
):
    def __init__(
        self,
        store: LocalStore,
        checkpointer: AsyncSqliteSaver,
        agent_store: BaseStore | None = None,
        max_concurrent_runs: int = 2,
        lease_seconds: float = 30.0,
        settings: Settings | None = None,
        mcp_catalog: MCPToolCatalog | None = None,
        plugin_catalog: PluginCatalog | None = None,
        terminal_callback: Callable[[str, str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.store = store
        self.checkpointer = checkpointer
        self.agent_store = agent_store
        self.settings = settings or get_settings()
        self.mcp_catalog = mcp_catalog or MCPToolCatalog(self.settings.data_dir, store=store)
        self.plugin_catalog = plugin_catalog or PluginCatalog(self.settings.data_dir)
        self._terminal_callback = terminal_callback
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._terminal_callback_tasks: set[asyncio.Task[None]] = set()
        self._wakeups: dict[str, asyncio.Event] = {}
        self._event_stream = RunEventStream(
            store,
            run_is_active=lambda run_id: run_id in self._tasks,
        )
        self._goals: dict[str, str] = {}
        self._user_inputs: dict[str, str] = {}
        self._workspaces: dict[str, str | None] = {}
        self._histories: dict[str, list[dict[str, str]]] = {}
        self._attachments: dict[str, list[dict[str, str]]] = {}
        self._settings_overrides: dict[str, dict[str, Any]] = {}
        self._run_metadata: dict[str, dict[str, Any]] = {}
        # Resolved tier per run (fast|deep|…). Mirrors local_runs.mode; lets a
        # resume after restart continue at the user's chosen tier.
        self._modes: dict[str, str] = {}
        self._worker_id = f"worker_{uuid.uuid4().hex}"
        self._lease_seconds = lease_seconds
        self._slots = asyncio.Semaphore(max(1, max_concurrent_runs))
        self._job_wakeup = asyncio.Event()
        self._collaboration = RunCollaboration(
            store,
            start_run=lambda *args, **kwargs: self.start_run(*args, **kwargs),
            cancel_run=lambda run_id: self.cancel_run(run_id),
            tasks=lambda: self._tasks,
            slots=lambda: self._slots,
            job_wakeup=lambda: self._job_wakeup,
            event_stream=lambda: self._event_stream,
        )
        self._settlement = RunSettlement(
            store,
            worker_id=self._worker_id,
            cancel_child_runs=self._collaboration._cancel_child_runs,
            wait_for_child_runs_terminal=self._collaboration._wait_for_child_runs_terminal,
            wait_for_child_status_change=self._collaboration._wait_for_child_status_change,
        )
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._shutting_down = False
        self._lost_leases: set[asyncio.Task[Any]] = set()
        self._last_sandbox_sweep = 0.0
        self._unconfirmed_cleanup: set[asyncio.Task[Any]] = set()
        self._started_jobs: set[asyncio.Task[Any]] = set()
        self._model_bindings = RunModelBindings(
            store,
            self.settings,
            lambda *args, **kwargs: get_model_api_key(*args, **kwargs),
        )
        self._agent_definitions: dict[str, Any] = {}
        self._agent_definition_lock = asyncio.Lock()
        self._fenced_checkpointer = (
            FencedCheckpointer(checkpointer, store) if checkpointer is not None else None
        )

    async def emit_for_run(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Publish an HTTP-originated event and wake live subscribers.

        Used by HTTP handlers to surface side-effects (`permission.resolved`,
        `question.answered`) that originate from the API surface rather
        than the LangGraph stream itself. If the stream already closed at a
        waiting point, the event is still persisted so the resume stream can
        replay it before `run.resumed`.
        """
        wakeup = self._wakeups.get(run_id)
        await self._enqueue(wakeup, run_id, event_type, payload)

    def _trace_stream_event(self, event: dict[str, Any]) -> None:
        """Keep the historical runs.trace_stream_event patch seam."""
        trace_stream_event(event)

    def wake_run(self, run_id: str) -> None:
        """Wake subscribers after an HTTP decision and its event commit together."""
        wakeup = self._wakeups.get(run_id)
        if wakeup is not None:
            wakeup.set()

    # ---- public API ----

    async def _model_binding(
        self,
        principal_id: str,
        requested_model: str,
    ) -> tuple[dict[str, Any], RunAdmissionError | None]:
        return await self._model_bindings.binding(principal_id, requested_model)

    @asynccontextmanager
    async def _model_admission(
        self,
        principal_id: str,
        requested_model: str,
        required_capabilities: tuple[str, ...] = ("streaming", "tool_calling"),
    ) -> AsyncIterator[tuple[dict[str, Any], RunAdmissionError | None]]:
        """Compatibility entry used by the command route."""
        async with self._model_bindings.admission(
            principal_id,
            requested_model,
            required_capabilities,
            binding=self._model_binding,
            local_binding_locked=self._local_model_binding_locked,
        ) as admission:
            yield admission

    async def _local_model_binding_locked(
        self,
        *,
        principal_id: str,
        connection_id: str,
        model_id: str,
        requested_model: str,
        required_capabilities: tuple[str, ...] = ("streaming", "tool_calling"),
    ) -> tuple[dict[str, Any], RunAdmissionError | None]:
        return await self._model_bindings.local_binding_locked(
            principal_id=principal_id,
            connection_id=connection_id,
            model_id=model_id,
            requested_model=requested_model,
            required_capabilities=required_capabilities,
        )

    async def _model_binding_error(
        self,
        principal_id: str,
        settings_snapshot: dict[str, Any],
    ) -> tuple[str | None, str | None]:
        return await self._model_bindings.binding_error(principal_id, settings_snapshot)

    async def start_run(
        self,
        *,
        principal_id: str,
        command_id: str,
        client_message_id: str,
        protocol_version: int,
        required_capabilities: list[str],
        goal: str,
        required_tools: list[str] | None = None,
        thread_id: str | None = None,
        user_input: str | None = None,
        assistant_message_id: str | None = None,
        thread_title: str | None = None,
        thread_metadata: dict[str, Any] | None = None,
        user_item_metadata: dict[str, Any] | None = None,
        replace_from_client_id: str | None = None,
        workspace_path: str | None = None,
        attachment_paths: list[str] | None = None,
        mode: str = "fast",
        permission_mode: str = "ask",
        history: list[dict[str, str]] | None = None,
        parent_run_id: str | None = None,
        plugin_refs: list[dict[str, Any]] | None = None,
        plugin_command: dict[str, Any] | None = None,
        settings: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        settings_are_frozen: bool = False,
        metadata_is_trusted: bool = False,
    ) -> dict[str, Any]:
        return await admit_run(
            store=self.store,
            runtime_settings=self.settings,
            model_admission=self._model_admission,
            capability_binding_snapshots=self._model_bindings.capability_binding_snapshots,
            wake_jobs=self.wake_jobs,
            principal_id=principal_id,
            command_id=command_id,
            client_message_id=client_message_id,
            protocol_version=protocol_version,
            required_capabilities=required_capabilities,
            goal=goal,
            required_tools=required_tools,
            thread_id=thread_id,
            user_input=user_input,
            assistant_message_id=assistant_message_id,
            thread_title=thread_title,
            thread_metadata=thread_metadata,
            user_item_metadata=user_item_metadata,
            replace_from_client_id=replace_from_client_id,
            workspace_path=workspace_path,
            attachment_paths=attachment_paths,
            mode=mode,
            permission_mode=permission_mode,
            history=history,
            parent_run_id=parent_run_id,
            plugin_refs=plugin_refs,
            plugin_command=plugin_command,
            settings=settings,
            metadata=metadata,
            settings_are_frozen=settings_are_frozen,
            metadata_is_trusted=metadata_is_trusted,
        )

    async def fork_run(
        self,
        *,
        principal_id: str,
        source_run_id: str,
        command_id: str,
        client_message_id: str,
        assistant_message_id: str,
        thread_id: str,
        protocol_version: int,
        required_capabilities: list[str],
        checkpoint_id: str,
        goal: str | None = None,
        user_input: str,
        thread_title: str | None = None,
        thread_metadata: dict[str, Any] | None = None,
        user_item_metadata: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await admit_fork(
            store=self.store,
            runtime_settings=self.settings,
            checkpointer=self.checkpointer,
            reconcile_graph_head=self._reconcile_graph_head,
            wake_jobs=self.wake_jobs,
            principal_id=principal_id,
            source_run_id=source_run_id,
            command_id=command_id,
            client_message_id=client_message_id,
            assistant_message_id=assistant_message_id,
            thread_id=thread_id,
            protocol_version=protocol_version,
            required_capabilities=required_capabilities,
            checkpoint_id=checkpoint_id,
            goal=goal,
            user_input=user_input,
            thread_title=thread_title,
            thread_metadata=thread_metadata,
            user_item_metadata=user_item_metadata,
            metadata=metadata,
        )

    async def _reconcile_graph_head(self, run: dict[str, Any]) -> dict[str, Any]:
        """Advance a stale product head from checkpoint metadata after a crash."""
        if self.checkpointer is None:
            return run
        run_id = str(run["id"])
        graph_thread_id = str(run.get("graph_thread_id") or run_id)
        current_head = run.get("graph_checkpoint_id")
        latest_id: str | None = None
        async for item in self.checkpointer.alist(
            {"configurable": {"thread_id": graph_thread_id, "checkpoint_ns": ""}},
            filter={"runtime_run_id": run_id},
            limit=1,
        ):
            latest_id = _checkpoint_id_from_config(item.config)
        if latest_id is None or latest_id == current_head:
            return run
        if (
            isinstance(current_head, str)
            and current_head
            and not await _checkpoint_is_ancestor(
                self.checkpointer,
                graph_thread_id=graph_thread_id,
                head_checkpoint_id=latest_id,
                candidate_checkpoint_id=current_head,
            )
        ):
            raise GraphHeadConflictError(f"run {run_id} checkpoint journal diverged")
        await self.store.advance_graph_checkpoint(
            run_id,
            graph_thread_id=graph_thread_id,
            expected_checkpoint_id=current_head,
            checkpoint_id=latest_id,
        )
        return {**run, "graph_checkpoint_id": latest_id}

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
        await self._settlement._confirm_lost_attempt_cleanup(
            wakeup=wakeup,
            run_id=run_id,
            execution_attempt_id=execution_attempt_id,
            job_id=job_id,
            lease_generation=lease_generation,
            cleanup_report=cleanup_report,
        )

    async def _settle_execution_outcome(
        self,
        *,
        run_id: str,
        execution_attempt_id: str,
        outcome: RunOutcome,
        cleanup_report: dict[str, Any],
        lease_state: str = "current",
    ) -> RunOutcome:
        return await self._settlement._settle_execution_outcome(
            run_id=run_id,
            execution_attempt_id=execution_attempt_id,
            outcome=outcome,
            cleanup_report=cleanup_report,
            lease_state=lease_state,
        )

    async def _settle_child_coordination(
        self,
        run_id: str,
        outcome: RunOutcome,
    ) -> tuple[RunOutcome, dict[str, Any]]:
        return await self._settlement._settle_child_coordination(run_id, outcome)

    async def resume_run(
        self,
        *,
        run_id: str,
        decision: dict[str, Any],
    ) -> bool:
        """Resume a paused run with a decision payload (e.g. permission
        approve/deny). Returns False if the run isn't paused or unknown."""
        run = await self.store.get_run(run_id)
        if run is None or run.get("status") not in {"waiting_permission", "waiting_input"}:
            return False
        await self._reconcile_graph_head(run)
        try:
            job = await self.store.enqueue_run_job(
                run_id,
                kind="resume",
                resume_payload=decision,
            )
        except WorkspaceAdmissionError:
            return False
        if job is None or job.get("status") != "pending":
            return False
        # commit_run_result atomically settles the previous leased job before
        # publishing run.waiting. A still-present task entry only means that
        # the previous coroutine is finishing its in-memory bookkeeping; it
        # must not reject this durable resume command.
        self._job_wakeup.set()
        return True

    async def reconcile_resume_head(self, run_id: str) -> bool:
        run = await self.store.get_run(run_id)
        if run is None:
            return False
        await self._reconcile_graph_head(run)
        return True

    async def cancel_run(self, run_id: str) -> bool:
        states = await self.store.request_run_cancel_tree(run_id)
        if not states:
            return False
        for target_run_id, state in states.items():
            task = self._tasks.get(target_run_id)
            if state == "leased" and task is not None and task in self._started_jobs:
                task.cancel()
        self._job_wakeup.set()
        return run_id in states

    def child_run_control(self) -> ChildRunControl:
        return self._collaboration.child_run_control()

    def agent_mailbox_control(self) -> AgentMailboxControl:
        return self._collaboration.agent_mailbox_control()

    async def collaboration_snapshot(self, root_run_id: str) -> dict[str, Any]:
        return await self._collaboration.collaboration_snapshot(root_run_id)

    async def _wait_for_child_runs_terminal(
        self,
        parent_run_id: str,
        child_run_ids: Sequence[str],
    ) -> list[dict[str, Any]]:
        return await self._collaboration._wait_for_child_runs_terminal(
            parent_run_id,
            child_run_ids,
        )

    async def _wait_for_child_status_change(
        self,
        parent_run_id: str,
        current: Sequence[dict[str, Any]],
        child_run_ids: Sequence[str],
    ) -> None:
        await self._collaboration._wait_for_child_status_change(
            parent_run_id,
            current,
            child_run_ids,
        )

    @asynccontextmanager
    async def _yield_execution_slot(self, parent_run_id: str):
        async with self._collaboration._yield_execution_slot(parent_run_id):
            yield

    async def _cancel_child_runs(
        self,
        parent_run_id: str,
        child_run_ids: Sequence[str],
    ) -> list[dict[str, Any]]:
        return await self._collaboration._cancel_child_runs(parent_run_id, child_run_ids)

    async def stream(self, run_id: str, *, after_seq: int = 0) -> AsyncIterator[dict[str, Any]]:
        """Yield AgentRunEvent envelopes (matching the TS interface):
            {id, run_id, seq, event_type, payload, created_at}

        Durable events include ``seq`` and replay from ``local_events``.
        Temporary model output has no durable sequence and only reaches
        subscribers that are connected while it is produced.
        """
        async for event in self._event_stream.stream(run_id, after_seq=after_seq):
            yield event
