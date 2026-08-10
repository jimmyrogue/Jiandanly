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
import hashlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlparse

from langchain_core.messages import AIMessageChunk, HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.base import BaseStore
from langgraph.types import Command

from .agent.builder import (
    build_agent,
    skill_catalog_fingerprint,
)
from .agent.child_runs import ChildRunControl
from .agent.context_builder import RuntimeContext
from .agent.mailbox import AgentMailboxControl, AgentMessageKind
from .build_info import runtime_build_identity
from .config import Settings, get_settings
from .dev_trace import trace_stream_event
from .event_translator import translate
from .llm.errors import ModelServiceError
from .llm.runtime import bind_runtime_model
from .model_credentials import (
    CredentialStoreError,
    get_model_api_key,
)
from .model_profiles import (
    apply_known_model_profile_defaults,
    model_capability,
    normalized_model_capabilities,
)
from .observability import build_callbacks
from .plugins.catalog import PluginCatalog
from .plugins.identity import plugin_action_tool_version
from .progress_ledger import build_handoff_snapshot
from .run_configuration import (
    RUNTIME_PROTOCOL_VERSION,
    _apply_advanced_overrides,
    _execution_policy_snapshot,
    freeze_run_settings,
    public_run_settings,
    runtime_capabilities,
    sanitize_run_metadata,
)
from .run_errors import (
    RUN_SHUTDOWN_TIMEOUT_SECONDS,
    CheckpointNotFoundError,
    ChildCoordinationError,
    ExecutionIdentityError,
    ExecutionLeaseExpiredError,
    ExecutionModelBindingError,
    ExecutionSettlementError,
    ExecutionShutdownError,
    ExecutionSkillBindingError,
    ExecutionWorkspaceError,
    RunNotFoundError,
    RunOutcome,
)
from .run_event_stream import RunEventStream
from .run_inputs import (
    _attachment_admission_error,
    _attachment_bindings,
    _generate_conversation_title,
    _plugin_input_snapshots,
    _prepare_run_inputs,
    _resolved_attachment_bindings,
)
from .run_stream_state import (
    _assistant_draft_from_state,
    _assistant_draft_from_update,
    _assistant_round_from_update,
    _checkpoint_id_from_config,
    _checkpoint_id_from_stream,
    _checkpoint_is_ancestor,
    _completion_failure_payload,
    _json_object,
    _normalize_question_options,
    _repair_context_from_metadata,
    _repair_context_rejected,
    _repair_rejected_failure_payload,
    _repair_workflow_payload,
    _retry_context_from_metadata,
    _run_failed_payload,
    _task_interrupts,
    _waiting_status_for_interrupts,
    normalize_todos,
    summarize_todos,
)
from .sandbox_reaper import reap_sandbox_processes
from .store.fenced_checkpointer import FencedCheckpointer
from .store.sqlite import (
    TRANSIENT_RUN_EVENT_TYPES,
    GraphHeadConflictError,
    LeaseFenceError,
    LocalStore,
    RunAdmissionError,
    RunInputSnapshotError,
    WorkspaceAdmissionError,
)
from .tools.mcp import MCPToolCatalog
from .tools.memory import extract_memory_write_facts
from .tools.runtime import bind_runtime_tools

log = logging.getLogger("shejane_runtime.runs")

# The idle dispatch branch runs a few times a second; sandboxes only become
# reapable when a lease expires, so sweeping every poll would be pure overhead.
_SANDBOX_SWEEP_SECONDS = 5.0

# Attachments are admitted as immutable Runtime-owned references. Model-facing
# attachment and PDF reads have a 200 MiB ceiling; other workspace, Skill,
# Memory, and subagent reads use 20 MiB in agent/backends.py.
_IMAGE_TOOL_CAPABILITIES = {
    "image.generate": "image_generation",
    "image.edit": "image_editing",
}
_CHILD_TERMINAL_STATUSES = {"completed", "failed", "canceled", "cleanup_required"}


def _child_wait_satisfied(
    children: Sequence[dict[str, Any]],
    condition: Literal["all", "any"],
) -> bool:
    terminal = [child.get("status") in _CHILD_TERMINAL_STATUSES for child in children]
    return all(terminal) if condition == "all" else any(terminal)


def _collaboration_completion_summary(
    children: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    active = [
        str(child["id"])
        for child in children
        if child.get("status") not in _CHILD_TERMINAL_STATUSES
    ]
    required = [child for child in children if child.get("completion_mode") == "required"]
    required_failed = [
        str(child["id"])
        for child in required
        if child.get("status") in _CHILD_TERMINAL_STATUSES and child.get("status") != "completed"
    ]
    required_waiting = [
        str(child["id"])
        for child in required
        if child.get("status") not in _CHILD_TERMINAL_STATUSES
    ]
    best_effort_active = [
        str(child["id"])
        for child in children
        if child.get("completion_mode") == "best_effort"
        and child.get("status") not in _CHILD_TERMINAL_STATUSES
    ]

    quorum_members: dict[str, list[dict[str, Any]]] = {}
    for child in children:
        if child.get("completion_mode") != "quorum":
            continue
        quorum_members.setdefault(str(child.get("quorum_group") or ""), []).append(child)
    quorum_groups: list[dict[str, Any]] = []
    quorum_waiting: list[str] = []
    quorum_cancel: list[str] = []
    quorum_impossible = False
    quorum_satisfied = True
    for group, members in sorted(quorum_members.items()):
        requirements = {int(member.get("quorum_required") or 0) for member in members}
        required_count = next(iter(requirements)) if len(requirements) == 1 else 0
        completed = sum(member.get("status") == "completed" for member in members)
        member_active = [
            str(member["id"])
            for member in members
            if member.get("status") not in _CHILD_TERMINAL_STATUSES
        ]
        failed = sum(
            member.get("status") in _CHILD_TERMINAL_STATUSES and member.get("status") != "completed"
            for member in members
        )
        satisfied = required_count > 0 and completed >= required_count
        impossible = (
            required_count <= 0
            or len(members) < required_count
            or completed + len(member_active) < required_count
        )
        if satisfied or impossible:
            quorum_cancel.extend(member_active)
        else:
            quorum_waiting.extend(member_active)
        quorum_satisfied = quorum_satisfied and satisfied
        quorum_impossible = quorum_impossible or impossible
        quorum_groups.append(
            {
                "group": group,
                "required": required_count,
                "completed": completed,
                "active": len(member_active),
                "failed": failed,
                "satisfied": satisfied,
                "impossible": impossible,
            }
        )

    impossible = bool(required_failed) or quorum_impossible
    required_satisfied = not required_failed and not required_waiting
    satisfied = required_satisfied and quorum_satisfied and not impossible
    wait_for = [] if impossible else [*required_waiting, *quorum_waiting]
    cancel = [*best_effort_active, *quorum_cancel]
    if impossible:
        cancel = active
    return {
        "satisfied": satisfied,
        "impossible": impossible,
        "required": {
            "total": len(required),
            "completed": sum(child.get("status") == "completed" for child in required),
            "failed": required_failed,
            "active": len(required_waiting),
        },
        "quorum_groups": quorum_groups,
        "best_effort_active": len(best_effort_active),
        "wait_for": list(dict.fromkeys(wait_for)),
        "cancel": list(dict.fromkeys(cancel)),
    }


class RunCoordinator:
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
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._shutting_down = False
        self._lost_leases: set[asyncio.Task[Any]] = set()
        self._last_sandbox_sweep = 0.0
        self._unconfirmed_cleanup: set[asyncio.Task[Any]] = set()
        self._started_jobs: set[asyncio.Task[Any]] = set()
        self._model_connection_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._agent_definitions: dict[str, Any] = {}
        self._agent_definition_lock = asyncio.Lock()
        self._child_wait_locks: dict[str, asyncio.Lock] = {}
        self._fenced_checkpointer = (
            FencedCheckpointer(checkpointer, store) if checkpointer is not None else None
        )

    def start(self) -> None:
        if self._dispatcher_task is None or self._dispatcher_task.done():
            self._shutting_down = False
            self._dispatcher_task = asyncio.create_task(
                self._dispatch_jobs(), name="run-job-dispatcher"
            )
            self._job_wakeup.set()

    async def stop(self) -> None:
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
            _done, pending = await asyncio.wait(tasks, timeout=RUN_SHUTDOWN_TIMEOUT_SECONDS)
            if pending:
                raise RuntimeError(
                    "runtime shutdown could not confirm cleanup for "
                    f"{len(pending)} execution attempt(s)"
                )
        callbacks = set(self._terminal_callback_tasks)
        if callbacks:
            _done, pending = await asyncio.wait(
                callbacks,
                timeout=RUN_SHUTDOWN_TIMEOUT_SECONDS,
            )
            if pending:
                log.warning("central diagnostics shutdown timed out pending=%s", len(pending))
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

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
        if self.settings.fake_llm:
            return {
                "adapter_id": "fake",
                "credential_ref": None,
                "requested_model": requested_model,
                "required_capabilities": ["streaming", "tool_calling"],
            }, None
        if requested_model.startswith("local:"):
            parts = requested_model.split(":", 2)
            if len(parts) != 3 or not parts[1] or not parts[2]:
                return {}, RunAdmissionError(
                    "model_spec_invalid",
                    "local model spec must be local:<connection>:<model>",
                )
            connection_id, model_id = parts[1], parts[2]
            async with self._model_connection_lock(principal_id, connection_id):
                return await self._local_model_binding_locked(
                    principal_id=principal_id,
                    connection_id=connection_id,
                    model_id=model_id,
                    requested_model=requested_model,
                    required_capabilities=("streaming", "tool_calling"),
                )

        return {}, RunAdmissionError(
            "model_service_missing",
            "select a Runtime BYOK model before starting a run",
        )

    @asynccontextmanager
    async def _model_admission(
        self,
        principal_id: str,
        requested_model: str,
        required_capabilities: tuple[str, ...] = ("streaming", "tool_calling"),
    ) -> AsyncIterator[tuple[dict[str, Any], RunAdmissionError | None]]:
        """Keep a model connection stable until its Run is durably admitted."""
        if self.settings.fake_llm:
            yield (
                {
                    "adapter_id": "fake",
                    "credential_ref": None,
                    "requested_model": requested_model,
                    "profile": {capability: True for capability in required_capabilities},
                    "required_capabilities": list(required_capabilities),
                },
                None,
            )
            return
        if not requested_model.startswith("local:"):
            yield await self._model_binding(principal_id, requested_model)
            return
        parts = requested_model.split(":", 2)
        if len(parts) != 3 or not parts[1] or not parts[2]:
            yield (
                {},
                RunAdmissionError(
                    "model_spec_invalid",
                    "local model spec must be local:<connection>:<model>",
                ),
            )
            return
        connection_id, model_id = parts[1], parts[2]
        async with self._model_connection_lock(principal_id, connection_id):
            yield await self._local_model_binding_locked(
                principal_id=principal_id,
                connection_id=connection_id,
                model_id=model_id,
                requested_model=requested_model,
                required_capabilities=required_capabilities,
            )

    async def _local_model_binding_locked(
        self,
        *,
        principal_id: str,
        connection_id: str,
        model_id: str,
        requested_model: str,
        required_capabilities: tuple[str, ...] = ("streaming", "tool_calling"),
    ) -> tuple[dict[str, Any], RunAdmissionError | None]:
        connection = await self.store.get_model_connection(
            principal_id=principal_id,
            connection_id=connection_id,
        )
        if connection is None:
            return {}, RunAdmissionError(
                "model_service_missing",
                "model service is not connected",
            )
        try:
            models = json.loads(connection.get("models_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            models = []
        profile = next(
            (
                model
                for model in models
                if isinstance(model, dict) and model.get("model_id") == model_id
            ),
            None,
        )
        if profile is None:
            return {}, RunAdmissionError(
                "model_not_found",
                "model is not available from this connection",
            )
        profile = apply_known_model_profile_defaults(
            profile,
            service_base_url=str(connection.get("base_url") or ""),
            trusted_model_catalog=connection.get("preset_id") == "shejane-official",
        )
        profile["capabilities"] = normalized_model_capabilities(
            profile,
            adapter_id=str(connection.get("adapter_id") or "openai_chat"),
        )
        agent_capability = model_capability(profile, "agent_chat")
        if agent_capability is None:
            return {}, RunAdmissionError(
                "model_capability_unavailable",
                "model does not declare Agent chat capability",
            )
        try:
            if not await get_model_api_key(
                principal_id,
                connection_id,
                str(connection["credential_ref"]),
            ):
                return {}, RunAdmissionError(
                    "model_service_missing",
                    "model service API key is not configured",
                )
        except CredentialStoreError as exc:
            return {}, RunAdmissionError(
                "model_credential_store_unavailable",
                str(exc),
            )
        protocol = str(agent_capability.get("protocol"))
        base_url = str(connection["base_url"])
        preset_id = str(connection.get("preset_id") or "")
        return {
            "adapter_id": {
                "openai_chat_completions": "openai_chat",
                "openai_responses": "openai_chat",
                "anthropic_messages": "anthropic_messages",
                "google_generate_content": "google_genai",
            }.get(protocol, str(connection["adapter_id"])),
            "protocol": protocol,
            "preset_id": preset_id,
            "connection_id": connection_id,
            "connection_version": int(connection.get("version") or 1),
            "base_url": base_url,
            "credential_ref": str(connection["credential_ref"]),
            "requested_model": requested_model,
            "model_id": model_id,
            "profile": profile,
            "required_capabilities": list(required_capabilities),
            "display_reasoning_summary": (
                protocol == "openai_responses"
                and preset_id == "openai"
                and urlparse(base_url).hostname == "api.openai.com"
            ),
        }, None

    async def _model_binding_error(
        self,
        principal_id: str,
        settings_snapshot: dict[str, Any],
    ) -> tuple[str | None, str | None]:
        # Runs accepted before settings snapshots existed remain resumable.
        # Every newly accepted Run is versioned and must pass the strict check.
        if "_snapshot_version" not in settings_snapshot:
            return None, None
        if settings_snapshot.get("_snapshot_version") != 1:
            return "run settings snapshot version is unsupported", None
        binding = settings_snapshot.get("_model_binding")
        if not isinstance(binding, dict):
            return "run model binding snapshot is missing", None
        if binding.get("adapter_id") == "fake":
            return (
                (None, None) if self.settings.fake_llm else ("fake model service is disabled", None)
            )
        if binding.get("adapter_id") in {
            "openai_chat",
            "anthropic_messages",
            "google_genai",
        }:
            connection_id = binding.get("connection_id")
            if not isinstance(connection_id, str):
                return "run model credential reference is invalid", None
            async with self._model_connection_lock(principal_id, connection_id):
                return await self._model_binding_error_locked(
                    principal_id=principal_id,
                    connection_id=connection_id,
                    binding=binding,
                )
        return "run model adapter is no longer supported", None

    async def _capability_binding_snapshots(
        self,
        *,
        principal_id: str,
        required_tools: list[str],
    ) -> tuple[dict[str, dict[str, Any]], RunAdmissionError | None]:
        """Resolve Runtime-owned default image bindings into an immutable Run snapshot."""
        rows = {
            str(row["capability"]): row
            for row in await self.store.list_model_capability_bindings(principal_id=principal_id)
        }
        snapshots: dict[str, dict[str, Any]] = {}
        for capability in set(_IMAGE_TOOL_CAPABILITIES.values()):
            row = rows.get(capability)
            if row is None:
                continue
            connection_id = str(row["connection_id"])
            connection = await self.store.get_model_connection(
                principal_id=principal_id,
                connection_id=connection_id,
            )
            if connection is None or int(connection.get("version") or 0) != int(
                row["connection_version"]
            ):
                continue
            try:
                models = json.loads(connection.get("models_json") or "[]")
            except (json.JSONDecodeError, TypeError):
                models = []
            profile = next(
                (
                    item
                    for item in models
                    if isinstance(item, dict) and item.get("model_id") == row["model_id"]
                ),
                None,
            )
            if profile is None:
                continue
            profile = apply_known_model_profile_defaults(
                profile,
                service_base_url=str(connection.get("base_url") or ""),
                trusted_model_catalog=connection.get("preset_id") == "shejane-official",
            )
            profile["capabilities"] = normalized_model_capabilities(
                profile,
                adapter_id=str(connection.get("adapter_id") or "openai_chat"),
            )
            verified = model_capability(profile, capability)
            if (
                verified is None
                or verified.get("verification") != "verified"
                or verified.get("protocol") != row["protocol"]
            ):
                continue
            try:
                api_key = await get_model_api_key(
                    principal_id,
                    connection_id,
                    str(connection["credential_ref"]),
                )
            except CredentialStoreError as exc:
                return {}, RunAdmissionError("model_credential_store_unavailable", str(exc))
            if not api_key:
                continue
            snapshots[capability] = {
                "capability": capability,
                "connection_id": connection_id,
                "connection_version": int(connection["version"]),
                "base_url": str(connection["base_url"]),
                "credential_ref": str(connection["credential_ref"]),
                "model_id": str(row["model_id"]),
                "protocol": str(row["protocol"]),
                "revision": int(row["revision"]),
            }

        missing = [
            tool_name
            for tool_name in required_tools
            if _IMAGE_TOOL_CAPABILITIES[tool_name] not in snapshots
        ]
        if missing:
            return snapshots, RunAdmissionError(
                "required_tool_unavailable",
                f"required tools are not configured: {', '.join(missing)}",
            )
        return snapshots, None

    async def _skill_binding_error(self, settings_snapshot: dict[str, Any]) -> str | None:
        # Runs accepted before Skill fingerprints existed remain resumable.
        if settings_snapshot.get("skills") != "on":
            return None
        admitted = settings_snapshot.get("_skills_fingerprint")
        if not isinstance(admitted, str) or not admitted:
            return None
        try:
            current = await asyncio.to_thread(skill_catalog_fingerprint)
        except OSError as exc:
            return f"Skill configuration is unavailable: {exc}"
        if current != admitted:
            return "Skill configuration changed after Run admission"
        return None

    async def _model_binding_error_locked(
        self,
        *,
        principal_id: str,
        connection_id: str,
        binding: dict[str, Any],
    ) -> tuple[str | None, str | None]:
        connection = await self.store.get_model_connection(
            principal_id=principal_id,
            connection_id=connection_id,
        )
        if (
            connection is None
            or int(connection.get("version") or 0) != binding.get("connection_version")
            or binding.get("credential_ref") != connection.get("credential_ref")
        ):
            return "model service connection was changed or removed", None
        try:
            api_key = await get_model_api_key(
                principal_id,
                connection_id,
                str(binding["credential_ref"]),
            )
        except CredentialStoreError as exc:
            return str(exc), None
        if not api_key:
            return "model service API key is no longer configured", None
        return None, api_key

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
        """Start a new agent run.

        `history`, `parent_run_id`, `settings` are the optional fields
        the client sends in the POST /runs body (see TS `createLocalRun`
        in runtime/sdk/src/client.ts). Previously they
        were silently dropped — meaning every conversation turn restarted
        the agent with zero context (multi-turn memory broken in local
        mode). We persist them on the run row and feed `history` into
        the initial state.
        """
        if settings_are_frozen:
            if not isinstance(settings, dict) or settings.get("_snapshot_version") != 1:
                raise RunAdmissionError(
                    "settings_snapshot_unsupported",
                    "run settings snapshot version is unsupported",
                )
            public_settings = dict(settings)
        else:
            public_settings = public_run_settings(settings)
            public_settings["permission_mode"] = permission_mode
        public_metadata = (
            dict(metadata or {}) if metadata_is_trusted else sanitize_run_metadata(metadata)
        )
        attachment_bindings = _attachment_bindings(attachment_paths or [])
        if attachment_bindings:
            public_metadata["_attachments"] = attachment_bindings
        command_metadata = dict(public_metadata)
        command_payload = {
            "type": "run.start",
            "thread_id": thread_id,
            "user_input": user_input,
            "assistant_message_id": assistant_message_id,
            "thread_title": thread_title,
            "thread_metadata": thread_metadata,
            "user_item_metadata": user_item_metadata,
            "replace_from_client_id": replace_from_client_id,
            "protocol_version": protocol_version,
            "required_capabilities": sorted(set(required_capabilities)),
            "required_tools": sorted(set(required_tools or [])),
            "goal": goal,
            "workspace_path": workspace_path,
            "attachment_paths": [item["source_path"] for item in attachment_bindings],
            "model": mode,
            "permission_mode": public_settings.get("permission_mode", "ask"),
            "history": history or [],
            "parent_run_id": parent_run_id,
            "plugin_refs": plugin_refs or [],
            "plugin_command": plugin_command,
            "settings": public_settings,
            "metadata": command_metadata,
        }
        accepted = await self.store.accepted_run_for_command(
            principal_id=principal_id,
            command_id=command_id,
            client_message_id=client_message_id,
            command_payload=command_payload,
        )
        if accepted is not None:
            self._job_wakeup.set()
            return accepted

        admission_error: RunAdmissionError | None = None
        if protocol_version != RUNTIME_PROTOCOL_VERSION:
            admission_error = RunAdmissionError(
                "protocol_version_unsupported",
                f"runtime protocol version {protocol_version} is not supported",
            )
        missing = sorted(set(required_capabilities) - runtime_capabilities(self.settings))
        if admission_error is None and missing:
            admission_error = RunAdmissionError(
                "capability_unavailable",
                f"runtime capabilities are unavailable: {', '.join(missing)}",
            )
        if attachment_bindings:
            if admission_error is None:
                attachment_error = await _attachment_admission_error(attachment_bindings)
                if attachment_error is not None:
                    admission_error = RunAdmissionError("attachment_unavailable", attachment_error)
        async with self._model_admission(principal_id, mode) as (
            model_binding,
            model_error,
        ):
            if admission_error is None:
                admission_error = model_error
            settings_snapshot = (
                dict(public_settings)
                if settings_are_frozen
                else freeze_run_settings(self.settings, public_settings)
            )
            settings_snapshot["_model_binding"] = model_binding
            settings_snapshot["_diagnostics_build"] = runtime_build_identity(
                protocol_version=RUNTIME_PROTOCOL_VERSION
            )
            settings_snapshot["_execution_policy"] = _execution_policy_snapshot(
                goal,
                settings_snapshot,
            )
            if settings_are_frozen:
                capability_bindings = settings_snapshot.get("_capability_bindings")
                required_tool_names = settings_snapshot.get("_required_tools")
                if not isinstance(capability_bindings, dict):
                    capability_bindings = {}
                if not isinstance(required_tool_names, list):
                    required_tool_names = []
            else:
                required_tool_names = sorted(set(required_tools or []))
                capability_bindings, capability_error = await self._capability_binding_snapshots(
                    principal_id=principal_id,
                    required_tools=required_tool_names,
                )
                if admission_error is None:
                    admission_error = capability_error
            settings_snapshot["_capability_bindings"] = capability_bindings
            settings_snapshot["_required_tools"] = required_tool_names
            if (
                settings_snapshot.get("skills") == "on"
                and "_skills_fingerprint" not in settings_snapshot
            ):
                try:
                    settings_snapshot["_skills_fingerprint"] = await asyncio.to_thread(
                        skill_catalog_fingerprint
                    )
                except OSError as exc:
                    if admission_error is None:
                        admission_error = RunAdmissionError(
                            "skill_catalog_unavailable",
                            f"Skill configuration could not be inspected: {exc}",
                        )
            prepared_inputs: list[dict[str, object]] = []
            admitted_user_item_metadata = dict(user_item_metadata or {})
            if admission_error is None and attachment_bindings:
                try:
                    prepared_inputs = await _prepare_run_inputs(self.store, attachment_bindings)
                except (OSError, RunInputSnapshotError) as exc:
                    admission_error = RunAdmissionError(
                        "attachment_import_failed",
                        f"attachment could not be imported into Runtime storage: {exc}",
                    )
                else:
                    public_metadata["_attachments"] = [
                        {
                            "input_id": item["input_id"],
                            "virtual_path": item["virtual_path"],
                        }
                        for item in prepared_inputs
                    ]
                    visible_attachments = admitted_user_item_metadata.get("attachments")
                    if isinstance(visible_attachments, list) and len(visible_attachments) == len(
                        prepared_inputs
                    ):
                        admitted_user_item_metadata["attachments"] = [
                            {
                                **attachment,
                                "input_id": prepared["input_id"],
                                "media_type": prepared["media_type"],
                                "bytes": prepared["bytes"],
                            }
                            if isinstance(attachment, dict)
                            else attachment
                            for attachment, prepared in zip(
                                visible_attachments,
                                prepared_inputs,
                                strict=True,
                            )
                        ]

            run, _created = await self.store.accept_run_command(
                principal_id=principal_id,
                command_id=command_id,
                client_message_id=client_message_id,
                command_payload=command_payload,
                goal=goal,
                thread_id=thread_id,
                user_input=user_input,
                assistant_message_id=assistant_message_id,
                thread_title=thread_title,
                thread_metadata=thread_metadata,
                user_item_metadata=admitted_user_item_metadata or None,
                replace_from_client_id=replace_from_client_id,
                workspace_path=workspace_path,
                mode=mode,
                history=history,
                parent_run_id=parent_run_id,
                settings=settings_snapshot,
                metadata=public_metadata,
                admission_error=admission_error,
                plugin_refs=plugin_refs,
                plugin_command=plugin_command,
                run_inputs=prepared_inputs,
            )
        self._job_wakeup.set()
        return run

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
        """Create a new product thread rooted at an existing graph checkpoint."""
        fork_metadata = sanitize_run_metadata(metadata)
        fork_metadata.update(
            {
                "intent": "checkpoint_fork",
                "source_run_id": source_run_id,
                "source_checkpoint_id": checkpoint_id.strip(),
            }
        )
        command_payload = {
            "type": "run.fork",
            "source_run_id": source_run_id,
            "protocol_version": protocol_version,
            "required_capabilities": sorted(set(required_capabilities)),
            "checkpoint_id": checkpoint_id.strip(),
            "thread_id": thread_id,
            "assistant_message_id": assistant_message_id,
            "goal": goal,
            "user_input": user_input,
            "thread_title": thread_title,
            "thread_metadata": thread_metadata,
            "user_item_metadata": user_item_metadata,
            "metadata": fork_metadata,
        }
        accepted = await self.store.accepted_run_for_command(
            principal_id=principal_id,
            command_id=command_id,
            client_message_id=client_message_id,
            command_payload=command_payload,
        )
        if accepted is not None:
            return accepted

        if protocol_version != RUNTIME_PROTOCOL_VERSION:
            raise RunAdmissionError(
                "protocol_version_unsupported",
                f"runtime protocol version {protocol_version} is not supported",
            )
        missing = sorted(set(required_capabilities) - runtime_capabilities(self.settings))
        if missing:
            raise RunAdmissionError(
                "capability_unavailable",
                f"runtime capabilities are unavailable: {', '.join(missing)}",
            )

        source = await self.store.get_run_for_principal(
            principal_id=principal_id,
            run_id=source_run_id,
        )
        if source is None:
            raise RunNotFoundError(source_run_id)
        workspace_error = await self.store.workspace_admission_error(
            principal_id=str(source["principal_id"]),
            path=source.get("workspace_path"),
        )
        if workspace_error is not None:
            raise WorkspaceAdmissionError(workspace_error)
        if source.get("status") in {"queued", "running", "cleanup_required"} and not source.get(
            "graph_checkpoint_id"
        ):
            raise CheckpointNotFoundError(checkpoint_id)
        if source.get("status") in {"queued", "running", "cleanup_required"}:
            raise ValueError("cannot fork a run while it is executing")
        source = await self._reconcile_graph_head(source)
        checkpoint_id = checkpoint_id.strip()
        if not checkpoint_id:
            raise CheckpointNotFoundError(checkpoint_id)

        graph_thread_id = str(source.get("graph_thread_id") or source_run_id)
        source_head_id = source.get("graph_checkpoint_id")
        if not isinstance(source_head_id, str) or not source_head_id:
            raise CheckpointNotFoundError(checkpoint_id)
        if not await _checkpoint_is_ancestor(
            self.checkpointer,
            graph_thread_id=graph_thread_id,
            head_checkpoint_id=source_head_id,
            candidate_checkpoint_id=checkpoint_id,
        ):
            raise CheckpointNotFoundError(checkpoint_id)

        fork_goal = (goal or source.get("goal") or "").strip()
        if not fork_goal:
            raise ValueError("goal required")
        fork_mode = str(source.get("mode") or "auto")
        source_settings = _json_object(source.get("settings_json"))
        if "_snapshot_version" not in source_settings:
            fork_settings = freeze_run_settings(self.settings, source_settings)
        elif source_settings.get("_snapshot_version") == 1:
            fork_settings = source_settings
        else:
            raise RunAdmissionError(
                "settings_snapshot_unsupported",
                "source run settings snapshot version is unsupported",
            )
        run, _created = await self.store.accept_run_command(
            principal_id=str(source["principal_id"]),
            command_id=command_id,
            client_message_id=client_message_id,
            command_payload=command_payload,
            goal=fork_goal,
            thread_id=thread_id,
            user_input=user_input,
            assistant_message_id=assistant_message_id,
            thread_title=thread_title,
            thread_metadata=thread_metadata,
            user_item_metadata=user_item_metadata,
            require_new_thread=True,
            workspace_path=source.get("workspace_path"),
            parent_run_id=source_run_id,
            settings=fork_settings,
            metadata=fork_metadata,
            mode=fork_mode,
            graph_thread_id=graph_thread_id,
            graph_checkpoint_id=checkpoint_id,
            graph_definition_id=source.get("graph_definition_id"),
            graph_input_kind="fork",
            inherit_plugin_bindings_from=source_run_id,
        )
        self._job_wakeup.set()
        return run

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
                skill_binding_error = await self._skill_binding_error(frozen_settings)
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

    def wake_jobs(self) -> None:
        self._job_wakeup.set()

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
        return ChildRunControl(
            spawn=self._spawn_child_run,
            list=self.store.list_child_runs_for_run,
            check=self.store.child_runs_for_parent,
            wait=self._wait_for_child_runs,
            cancel=self._cancel_child_runs,
        )

    def agent_mailbox_control(self) -> AgentMailboxControl:
        return AgentMailboxControl(
            send=self._send_agent_message,
            reply=self._reply_agent_message,
            inbox=self.store.list_agent_inbox,
            ack=self._ack_agent_messages,
        )

    async def collaboration_snapshot(self, root_run_id: str) -> dict[str, Any]:
        snapshot = await self.store.collaboration_snapshot(root_run_id)
        snapshot["completion"] = _collaboration_completion_summary(snapshot["children"])
        return snapshot

    async def _send_agent_message(
        self,
        sender_run_id: str,
        sender_operation_id: str,
        recipient_run_id: str,
        kind: AgentMessageKind,
        text: str,
        data: dict[str, Any],
        artifact_refs: Sequence[str],
        ttl_seconds: int,
    ) -> dict[str, Any]:
        message, _created = await self.store.send_agent_message(
            sender_run_id=sender_run_id,
            sender_operation_id=sender_operation_id,
            recipient_run_id=recipient_run_id,
            kind=kind,
            text=text,
            data=data,
            artifact_refs=artifact_refs,
            ttl_seconds=ttl_seconds,
        )
        return message

    async def _reply_agent_message(
        self,
        sender_run_id: str,
        sender_operation_id: str,
        in_reply_to: str,
        kind: AgentMessageKind,
        text: str,
        data: dict[str, Any],
        artifact_refs: Sequence[str],
        ttl_seconds: int,
    ) -> dict[str, Any]:
        message, _created = await self.store.reply_agent_message(
            sender_run_id=sender_run_id,
            sender_operation_id=sender_operation_id,
            in_reply_to=in_reply_to,
            kind=kind,
            text=text,
            data=data,
            artifact_refs=artifact_refs,
            ttl_seconds=ttl_seconds,
        )
        return message

    async def _ack_agent_messages(
        self,
        recipient_run_id: str,
        operation_id: str,
        message_ids: Sequence[str],
    ) -> list[dict[str, Any]]:
        return await self.store.ack_agent_messages(
            recipient_run_id=recipient_run_id,
            message_ids=message_ids,
            operation_id=operation_id,
        )

    async def _spawn_child_run(
        self,
        parent_run_id: str,
        spawn_operation_id: str,
        goal: str,
        agent_definition: dict[str, Any],
        coordination: dict[str, Any],
    ) -> dict[str, Any]:
        parent = await self.store.get_run(parent_run_id)
        parent_settings = (
            _json_object(parent.get("settings_json")) if isinstance(parent, dict) else {}
        )
        parent_settings.pop("_execution_policy", None)
        child_execution_policy = _execution_policy_snapshot(goal, parent_settings)
        child, created = await self.store.accept_child_run(
            parent_run_id=parent_run_id,
            spawn_operation_id=spawn_operation_id,
            goal=goal,
            agent_definition=agent_definition,
            coordination=coordination,
            execution_policy=child_execution_policy,
        )
        spawn_event = child.pop("_spawn_event", None)
        if isinstance(spawn_event, dict):
            async with self._event_stream.publication(parent_run_id):
                self._event_stream.publish_live(
                    parent_run_id,
                    self._event_stream.stored_event_envelope(spawn_event),
                )
        if created:
            self._job_wakeup.set()
        return (await self.store.child_runs_for_parent(parent_run_id, [str(child["id"])]))[0]

    async def _wait_for_child_runs(
        self,
        parent_run_id: str,
        child_run_ids: Sequence[str],
        condition: Literal["all", "any"],
        timeout_seconds: float,
    ) -> list[dict[str, Any]]:
        snapshots = await self.store.child_runs_for_parent(parent_run_id, child_run_ids)
        if _child_wait_satisfied(snapshots, condition) or timeout_seconds <= 0:
            return snapshots
        lock = self._child_wait_locks.setdefault(parent_run_id, asyncio.Lock())
        async with lock:
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            async with self._yield_execution_slot(parent_run_id):
                while True:
                    snapshots = await self.store.child_runs_for_parent(parent_run_id, child_run_ids)
                    if _child_wait_satisfied(snapshots, condition):
                        break
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(0.25, remaining))
        if not lock.locked():
            self._child_wait_locks.pop(parent_run_id, None)
        return snapshots

    async def _wait_for_child_runs_terminal(
        self,
        parent_run_id: str,
        child_run_ids: Sequence[str],
    ) -> list[dict[str, Any]]:
        if not child_run_ids:
            return []
        lock = self._child_wait_locks.setdefault(parent_run_id, asyncio.Lock())
        async with lock:
            async with self._yield_execution_slot(parent_run_id):
                while True:
                    snapshots = await self.store.child_runs_for_parent(parent_run_id, child_run_ids)
                    if _child_wait_satisfied(snapshots, "all"):
                        break
                    await asyncio.sleep(0.25)
        if not lock.locked():
            self._child_wait_locks.pop(parent_run_id, None)
        return snapshots

    async def _wait_for_child_status_change(
        self,
        parent_run_id: str,
        current: Sequence[dict[str, Any]],
        child_run_ids: Sequence[str],
    ) -> None:
        previous = {
            str(child["id"]): (str(child.get("status") or ""), str(child.get("updated_at") or ""))
            for child in current
            if str(child["id"]) in child_run_ids
        }
        lock = self._child_wait_locks.setdefault(parent_run_id, asyncio.Lock())
        async with lock:
            async with self._yield_execution_slot(parent_run_id):
                while True:
                    snapshots = await self.store.child_runs_for_parent(parent_run_id, child_run_ids)
                    if any(
                        previous.get(str(child["id"]))
                        != (str(child.get("status") or ""), str(child.get("updated_at") or ""))
                        for child in snapshots
                    ):
                        break
                    await asyncio.sleep(0.25)
        if not lock.locked():
            self._child_wait_locks.pop(parent_run_id, None)

    @asynccontextmanager
    async def _yield_execution_slot(self, parent_run_id: str):
        task = self._tasks.get(parent_run_id)
        should_yield = task is not None and not task.done()
        if not should_yield:
            yield
            return
        self._slots.release()
        self._job_wakeup.set()
        try:
            yield
        finally:
            acquire = asyncio.create_task(self._slots.acquire())
            try:
                await asyncio.shield(acquire)
            except asyncio.CancelledError:
                await acquire
                raise

    async def _cancel_child_runs(
        self,
        parent_run_id: str,
        child_run_ids: Sequence[str],
    ) -> list[dict[str, Any]]:
        await self.store.child_runs_for_parent(parent_run_id, child_run_ids)
        for child_run_id in child_run_ids:
            await self.cancel_run(str(child_run_id))
        return await self.store.child_runs_for_parent(parent_run_id, child_run_ids)

    async def cancel_model_connection_runs(
        self,
        *,
        principal_id: str,
        connection_id: str,
    ) -> int:
        """Cancel active runs before mutating a model connection."""
        run_ids: list[str] = []
        for run_id, settings in list(self._settings_overrides.items()):
            if run_id not in self._tasks:
                continue
            binding = settings.get("_model_binding")
            capability_bindings = settings.get("_capability_bindings")
            uses_connection = (
                isinstance(binding, dict) and binding.get("connection_id") == connection_id
            ) or (
                isinstance(capability_bindings, dict)
                and any(
                    isinstance(item, dict) and item.get("connection_id") == connection_id
                    for item in capability_bindings.values()
                )
            )
            if not uses_connection:
                continue
            run = await self.store.get_run(run_id)
            if run is not None and run.get("principal_id") == principal_id:
                run_ids.append(run_id)
        tasks = [self._tasks[run_id] for run_id in run_ids if run_id in self._tasks]
        for run_id in run_ids:
            canceled = await self.cancel_run(run_id)
            if not canceled:
                task = self._tasks.get(run_id)
                if task is not None:
                    task.cancel()
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=5.0)
            if pending:
                raise RuntimeError("active model connection runs did not stop")
        return len(run_ids)

    def _model_connection_lock(self, principal_id: str, connection_id: str) -> asyncio.Lock:
        key = (principal_id, connection_id)
        lock = self._model_connection_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._model_connection_locks[key] = lock
        return lock

    @asynccontextmanager
    async def model_connection_mutation(
        self,
        *,
        principal_id: str,
        connection_id: str,
    ) -> AsyncIterator[None]:
        """Fence admission and execution while a model connection changes."""
        async with self._model_connection_lock(principal_id, connection_id):
            await self.cancel_model_connection_runs(
                principal_id=principal_id,
                connection_id=connection_id,
            )
            yield

    @asynccontextmanager
    async def model_connection_catalog_update(
        self,
        *,
        principal_id: str,
        connection_id: str,
    ) -> AsyncIterator[None]:
        """Serialize catalog writes with admission without canceling Runs."""
        async with self._model_connection_lock(principal_id, connection_id):
            yield

    async def stream(self, run_id: str, *, after_seq: int = 0) -> AsyncIterator[dict[str, Any]]:
        """Yield AgentRunEvent envelopes (matching the TS interface):
            {id, run_id, seq, event_type, payload, created_at}

        Durable events include ``seq`` and replay from ``local_events``.
        Temporary model output has no durable sequence and only reaches
        subscribers that are connected while it is produced.
        """
        async for event in self._event_stream.stream(run_id, after_seq=after_seq):
            yield event

    # ---- driver ----

    async def _drive_run(
        self,
        *,
        run_id: str,
        principal_id: str,
        resume_payload: dict[str, Any] | None,
        mode: str,
        graph_thread_id: str,
        graph_checkpoint_id: str | None,
        graph_input_kind: str,
        execution_attempt_id: str,
        checkpointer: Any | None = None,
        model_api_key: str | None = None,
        resource_stack: AsyncExitStack | None = None,
    ) -> RunOutcome:
        wakeup = self._wakeups[run_id]
        workspace_path = self._workspaces.get(run_id)
        attachment_bindings = self._attachments.get(run_id, [])
        goal = self._goals.get(run_id, "")
        repair_context: dict[str, Any] | None = None
        retry_context: dict[str, Any] | None = None

        try:
            settings = self.settings
            run_metadata = self._run_metadata.get(run_id) or {}
            run_record = await self.store.get_run(run_id)
            if run_record is None:
                raise ExecutionIdentityError(f"run {run_id} disappeared before execution")
            run_kind = str(run_record.get("run_kind") or "turn")
            root_run_id = str(run_record.get("root_run_id") or run_id)
            agent_definition_id = str(run_record.get("agent_definition_id") or "shejane.default")
            agent_definition_version = str(run_record.get("agent_definition_version") or "1")
            collaboration_depth = int(run_record.get("collaboration_depth") or 0)
            child_definition: dict[str, Any] | None = None
            if run_kind == "child":
                candidate = run_metadata.get("_child_agent_definition")
                if (
                    not isinstance(candidate, dict)
                    or candidate.get("id") != agent_definition_id
                    or candidate.get("version") != agent_definition_version
                    or not isinstance(candidate.get("system_prompt"), str)
                    or not isinstance(candidate.get("allowed_tools"), list)
                    or not all(isinstance(name, str) for name in candidate["allowed_tools"])
                ):
                    raise ExecutionIdentityError(
                        f"child run {run_id} has an incompatible frozen Agent definition"
                    )
                child_definition = candidate
            repair_context = _repair_context_from_metadata(
                run_metadata,
                max_attempts=settings.repair_workflow_max,
            )
            retry_context = _retry_context_from_metadata(run_metadata)
            recovery_context = retry_context or repair_context
            answered_questions = []
            source_run_id = (recovery_context or {}).get("source_run_id")
            if source_run_id:
                answered_questions = await self.store.list_answered_question_choices_for_run(
                    principal_id=principal_id,
                    run_id=str(source_run_id),
                )
            clarification_count = await self.store.count_questions_for_run(run_id)

            # Mark the run as started FIRST — before model resolution and the
            # (slow) agent build. The client treats run.started as "the runtime
            # accepted this run"; emitting it late opened a window where a
            # quick cancel produced a stream with run.canceled but no
            # run.started (flaked test_cancel_midflight on slow CI runners).
            if resume_payload is None:
                await self._enqueue(wakeup, run_id, "run.started", {"goal": goal})

                if repair_context is not None:
                    if _repair_context_rejected(repair_context):
                        await self._enqueue(
                            wakeup,
                            run_id,
                            "repair.workflow",
                            _repair_workflow_payload(
                                repair_context,
                                status="rejected",
                                reason="repair attempt limit exceeded",
                            ),
                        )
                        return RunOutcome(
                            status="failed",
                            event_type="run.failed",
                            payload=_repair_rejected_failure_payload(repair_context),
                        )
                    await self._enqueue(
                        wakeup,
                        run_id,
                        "repair.workflow",
                        _repair_workflow_payload(repair_context, status="started"),
                    )

            resolved_model = mode
            run_settings = self._settings_overrides.get(run_id) or {}
            model_binding = run_settings.get("_model_binding")

            self._modes[run_id] = resolved_model

            # Per-run effective settings = base runtime settings with any
            # "Advanced" knobs the client sent folded on top.
            effective_settings = _apply_advanced_overrides(settings, run_settings)
            execution_policy = _execution_policy_snapshot(goal, run_settings)

            # The ingress schema and 1 MiB request limit are the safety boundary.
            # Context compaction belongs to Deep Agents' token-aware
            # SummarizationMiddleware; do not apply a second message-count policy
            # or manufacture a heuristic summary here.
            history = self._histories.get(run_id, [])
            full_messages: list[dict[str, str]] = [
                {"role": str(item.get("role", "user")), "content": str(item.get("content", ""))}
                for item in history
                if item.get("content")
            ]
            # +1 for the current user goal that gets appended below.
            turn_count = len(full_messages) + 1
            thread_title_seed = (
                await self.store.run_initial_thread_title_seed(run_id)
                if not full_messages
                else None
            )

            # Defaults: memory + skills + mcp all ON. The client's
            # agent settings panel has them enabled by default; legacy
            # callers (curl, tests) that don't send any settings
            # inherit the same default. Only an explicit "off" disables.
            memory_enabled = str(run_settings.get("memory", "on")).lower() != "off"
            skills_enabled = str(run_settings.get("skills", "on")).lower() != "off"
            mcp_enabled = str(run_settings.get("mcp", "on")).lower() != "off"
            # Code execution defaults ON now (since v7 of the client
            # storage, ~2026-05-26). The original opt-in toggle was
            # Per-server opt-out from the MCP tab. The client persists
            # a list of names the user disabled and ships it on every
            # run. Defensive coercion: drop non-strings and dedupe so
            # a buggy renderer can't crash the loop.
            raw_disabled = run_settings.get("mcp_disabled") or []
            mcp_disabled_servers: set[str] = {
                str(name) for name in raw_disabled if isinstance(name, str)
            }

            async def emit_steering_event(event_type: str, payload: dict[str, Any]) -> None:
                await self._enqueue(wakeup, run_id, event_type, payload)

            runtime_context = RuntimeContext(
                run_id=run_id,
                principal_id=principal_id,
                store=self.store,
                steering_emit=emit_steering_event,
                child_run_control=self.child_run_control(),
                agent_mailbox_control=self.agent_mailbox_control(),
                memory_enabled=memory_enabled,
                memory_write_facts=(
                    ()
                    if run_kind == "child"
                    else extract_memory_write_facts(
                        self._user_inputs.get(run_id, goal),
                        history=full_messages,
                    )
                ),
                execution_attempt_id=execution_attempt_id,
                workspace_root=workspace_path,
                attachments=tuple(
                    str(item.get("virtual_path"))
                    for item in attachment_bindings
                    if item.get("virtual_path")
                ),
                task_goal=goal,
                model_call_soft_limit=int(execution_policy["soft_model_call_limit"]),
                model_call_hard_limit=int(execution_policy["max_model_calls"]),
                model_call_final_reserve=int(execution_policy["final_model_call_reserve"]),
                execution_policy=dict(execution_policy),
                agent_role_prompt=(
                    str(child_definition["system_prompt"]) if child_definition is not None else None
                ),
                allowed_tool_names=(
                    tuple(str(name) for name in child_definition["allowed_tools"])
                    if child_definition is not None
                    else ()
                ),
                mode=resolved_model,
                run_kind=run_kind,
                root_run_id=root_run_id,
                agent_definition_id=agent_definition_id,
                agent_definition_version=agent_definition_version,
                collaboration_depth=collaboration_depth,
                permission_mode=str(run_settings.get("permission_mode") or "ask"),
                capability_bindings={
                    str(key): dict(value)
                    for key, value in (
                        run_settings.get("_capability_bindings", {}).items()
                        if isinstance(run_settings.get("_capability_bindings"), dict)
                        else ()
                    )
                    if isinstance(value, dict)
                },
                required_tools=tuple(
                    str(value)
                    for value in (
                        run_settings.get("_required_tools", [])
                        if isinstance(run_settings.get("_required_tools"), list)
                        else []
                    )
                ),
                turn_count=turn_count,
                clarification_count=clarification_count,
                repair_intent=bool(repair_context),
                repair_attempt=(repair_context or {}).get("attempt"),
                repair_max_attempts=(repair_context or {}).get("max_attempts"),
                repair_source_run_id=(repair_context or {}).get("source_run_id"),
                repair_source_message_id=(repair_context or {}).get("source_message_id"),
                repair_failure_category=(repair_context or {}).get("failure_category"),
                repair_failure_action_kind=(repair_context or {}).get("failure_action_kind"),
                retry_intent=bool(retry_context),
                retry_attempt=(retry_context or {}).get("attempt"),
                retry_source_run_id=(retry_context or {}).get("source_run_id"),
                retry_source_message_id=(retry_context or {}).get("source_message_id"),
                retry_failure_category=(retry_context or {}).get("failure_category"),
                retry_failure_action_kind=(retry_context or {}).get("failure_action_kind"),
                recovery_answered_questions=tuple(
                    (
                        str(item["question"]),
                        tuple(str(answer) for answer in item["answers"]),
                    )
                    for item in answered_questions
                ),
            )
            runtime_context.plugin_inputs = await _plugin_input_snapshots(
                self.store,
                run_id,
                attachment_bindings,
            )
            if resource_stack is None:
                raise RuntimeError(
                    "plugin snapshot acquisition requires an execution resource stack"
                )
            plugin_bindings = await self.store.list_run_plugin_bindings(run_id)
            plugin_lease = await resource_stack.enter_async_context(
                self.plugin_catalog.acquire_snapshot(
                    plugin_bindings,
                    execution_context=runtime_context,
                )
            )
            runtime_context.plugin_catalog_hash = plugin_lease.action_catalog_hash
            runtime_context.plugin_lease = plugin_lease
            public_inputs = [
                {key: value for key, value in item.items() if key != "source_path"}
                for item in runtime_context.plugin_inputs
            ]
            for action in plugin_lease.actions:
                action_inputs = [
                    item for item in public_inputs if item["media_type"] in action.consumes
                ]
                invocation_identity = {
                    "action": {
                        "plugin_id": action.plugin_id,
                        "plugin_version": action.plugin_version,
                        "plugin_digest": action.plugin_digest,
                        "action_id": action.action_id,
                    },
                    "inputs": action_inputs,
                    "grants": {
                        "capabilities": sorted(
                            set(action.capabilities)
                            & {
                                "input.read",
                                "artifact.write",
                                "computer.observe",
                                "computer.control",
                                "computer.setup",
                            }
                        )
                    },
                    "limits": dict(action.limits),
                    "environment": {
                        "locale": runtime_context.locale or "en-US",
                        "timezone": "UTC",
                    },
                    "model_binding": action.model_binding,
                }
                runtime_context.plugin_tool_versions[action.tool_name] = plugin_action_tool_version(
                    invocation_identity,
                    action_schema_digest=action.action_schema_digest,
                )
            agent = await build_agent(
                store=self.store,
                checkpointer=checkpointer or self.checkpointer,
                agent_store=self.agent_store,
                workspace_root=workspace_path,
                attachment_bindings=attachment_bindings,
                run_id=run_id,
                mode=resolved_model,
                task_goal=goal,
                turn_count=turn_count,
                memory_enabled=memory_enabled,
                skills_enabled=skills_enabled,
                skill_catalog_hash=(
                    str(run_settings["_skills_fingerprint"])
                    if isinstance(run_settings.get("_skills_fingerprint"), str)
                    else None
                ),
                mcp_enabled=mcp_enabled,
                mcp_disabled_servers=mcp_disabled_servers or None,
                mcp_catalog=self.mcp_catalog,
                plugin_lease=plugin_lease,
                settings=effective_settings,
                model_binding=model_binding if isinstance(model_binding, dict) else None,
                model_api_key=model_api_key,
                resource_stack=resource_stack,
                execution_attempt_id=execution_attempt_id,
                runtime_context=runtime_context,
                definition_cache=self._agent_definitions,
                definition_cache_lock=self._agent_definition_lock,
                repair_context=repair_context,
                retry_context=retry_context,
                steering_emit=emit_steering_event,
            )
            if not runtime_context.graph_definition_id:
                raise RuntimeError("agent definition id is missing")
            await self.store.bind_graph_definition(
                run_id,
                runtime_context.graph_definition_id,
            )
            config = {
                "configurable": {
                    "thread_id": graph_thread_id,
                    "checkpoint_ns": "",
                    "workspace_root": workspace_path or "",
                    "runtime_principal_id": principal_id,
                    "runtime_run_id": run_id,
                    "runtime_attempt_id": execution_attempt_id,
                    **(
                        {"checkpoint_id": graph_checkpoint_id}
                        if graph_checkpoint_id is not None
                        else {}
                    ),
                },
                "callbacks": build_callbacks(),
            }
            if resume_payload is not None:
                input_payload: Any = Command(resume=resume_payload)
                await self._enqueue(wakeup, run_id, "run.resumed", {"payload": resume_payload})
            else:
                if graph_input_kind not in {"new", "fork"}:
                    raise RuntimeError(f"unsupported graph input kind: {graph_input_kind}")
                messages = list(full_messages)
                messages.append(
                    HumanMessage(
                        content=goal,
                        additional_kwargs={
                            "runtime_kind": "task_input",
                            "runtime_run_id": run_id,
                        },
                    )
                )
                input_payload = {"messages": messages}
                # run.started + the "running" status were already emitted at
                # the top of the try block (before resolution/agent build).

            # Auto-approve loop. We may iterate multiple times if the
            # run hits successive HITL gates and every gated tool has
            # an in-run `scope=run` grant. Each iteration drains one
            # astream() cycle; on every paused state we either:
            #   • surface to the user (one-shot approval or a tool the
            #     user hasn't granted run-scope on), OR
            #   • build a synthetic Command(resume={"decisions": [...]})
            #     and loop again — making the pause invisible to the UI.
            current_checkpoint_id = graph_checkpoint_id
            while True:
                latest_checkpoint: dict[str, Any] | None = None
                if runtime_context.model is None:
                    raise RuntimeError("agent model is not bound")
                with (
                    bind_runtime_model(runtime_context.model),  # type: ignore[arg-type]
                    bind_runtime_tools(runtime_context.dynamic_tools),  # type: ignore[arg-type]
                ):
                    active_model_round: tuple[object, object] | None = None
                    active_model_call_id: str | None = None
                    async for part in agent.astream(
                        input_payload,
                        config=config,
                        context=runtime_context,
                        stream_mode=["updates", "messages", "custom", "checkpoints"],
                        durability="sync",
                        version="v2",
                    ):
                        if not isinstance(part, dict):
                            continue
                        kind = part.get("type")
                        payload = part.get("data")
                        if not isinstance(kind, str):
                            continue
                        if kind == "messages" and isinstance(payload, tuple) and len(payload) == 2:
                            chunk, metadata = payload
                            if isinstance(chunk, AIMessageChunk):
                                if (
                                    not isinstance(metadata, dict)
                                    or metadata.get("langgraph_node") != "model"
                                    or part.get("ns")
                                ):
                                    continue
                                chunk_round_id = str(
                                    chunk.additional_kwargs.get("runtime_model_call_id") or ""
                                )
                                if chunk_round_id:
                                    active_model_call_id = chunk_round_id
                                model_round = (
                                    metadata.get("langgraph_checkpoint_ns"),
                                    metadata.get("langgraph_step"),
                                )
                                if model_round != active_model_round:
                                    await self._enqueue(
                                        wakeup,
                                        run_id,
                                        "llm.round.started",
                                        {"round_id": active_model_call_id},
                                    )
                                    active_model_round = model_round
                        if kind == "checkpoints":
                            checkpoint_id = _checkpoint_id_from_stream(payload)
                            if checkpoint_id is not None:
                                await self.store.advance_graph_checkpoint(
                                    run_id,
                                    graph_thread_id=graph_thread_id,
                                    expected_checkpoint_id=current_checkpoint_id,
                                    checkpoint_id=checkpoint_id,
                                )
                                current_checkpoint_id = checkpoint_id
                                latest_checkpoint = payload
                            continue
                        if kind == "updates" and not part.get("ns"):
                            draft = _assistant_draft_from_update(payload)
                            if draft is not None:
                                await self.store.update_assistant_draft(
                                    run_id=run_id,
                                    **draft,
                                )
                            assistant_round = _assistant_round_from_update(
                                payload,
                                allow_reasoning_summary=bool(
                                    isinstance(model_binding, dict)
                                    and model_binding.get("display_reasoning_summary") is True
                                ),
                            )
                            if assistant_round is not None:
                                (
                                    round_event,
                                    round_created,
                                ) = await self.store.commit_assistant_round(run_id, assistant_round)
                                if round_created:
                                    trace_stream_event(
                                        self._event_stream.stored_event_envelope(round_event)
                                    )
                                committed_item_ids = []
                                if str(assistant_round.get("reasoning_summary") or "").strip():
                                    committed_item_ids.append(
                                        f"round:{assistant_round['round_id']}:reasoning"
                                    )
                                if str(assistant_round.get("text") or "").strip():
                                    committed_item_ids.append(
                                        f"round:{assistant_round['round_id']}:progress"
                                    )
                                await self._enqueue(
                                    wakeup,
                                    run_id,
                                    "llm.round.closed",
                                    {
                                        "round_id": assistant_round["round_id"],
                                        "committed_item_ids": committed_item_ids,
                                    },
                                )
                        if part.get("ns"):
                            continue
                        for translated in translate(kind, payload):
                            data = (
                                translated["data"]
                                if isinstance(translated["data"], dict)
                                else {"value": translated["data"]}
                            )
                            if translated["event"].startswith("llm.") and active_model_call_id:
                                data.setdefault("round_id", active_model_call_id)
                            await self._enqueue(wakeup, run_id, translated["event"], data)

                if current_checkpoint_id is None:
                    raise RuntimeError("graph execution produced no checkpoint")
                if latest_checkpoint is None:
                    raise RuntimeError("graph execution produced no checkpoint payload")
                config = {
                    **config,
                    "configurable": {
                        **config["configurable"],
                        "checkpoint_id": current_checkpoint_id,
                    },
                }
                # v2 checkpoint parts are emitted before pending interrupt
                # writes are folded into tasks. The public state read at this
                # exact branch head includes them; v3 lifecycle streams can
                # replace this once that API is stable.
                snapshot = await agent.aget_state(config)
                next_nodes = list(snapshot.next)
                if not next_nodes:
                    completion_failure = _completion_failure_payload(
                        snapshot.values,
                        current_run_id=run_id,
                    )
                    if completion_failure is not None:
                        if repair_context is not None:
                            await self._enqueue(
                                wakeup,
                                run_id,
                                "repair.workflow",
                                _repair_workflow_payload(
                                    repair_context,
                                    status="failed",
                                    reason=str(completion_failure["error"]),
                                ),
                            )
                        return RunOutcome(
                            status="failed",
                            event_type="run.failed",
                            payload=completion_failure,
                        )
                    final_draft = _assistant_draft_from_state(snapshot.values, run_id=run_id)
                    if final_draft is not None:
                        await self.store.update_assistant_draft(run_id=run_id, **final_draft)
                    draft = await self.store.get_assistant_draft(run_id)
                    if draft is None:
                        raise ExecutionSettlementError("final assistant draft is missing")
                    if repair_context is not None:
                        await self._enqueue(
                            wakeup,
                            run_id,
                            "repair.workflow",
                            _repair_workflow_payload(repair_context, status="completed"),
                        )
                    result_payload: dict[str, Any] = {}
                    if thread_title_seed:
                        try:
                            generated_title = await _generate_conversation_title(
                                getattr(runtime_context, "title_model", None),
                                user_input=self._user_inputs.get(run_id, goal),
                                assistant_answer=str(draft.get("content") or ""),
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            log.warning(
                                "run %s title generation failed: %s",
                                run_id,
                                type(exc).__name__,
                            )
                        else:
                            if generated_title:
                                result_payload = {
                                    "thread_title": generated_title,
                                    "thread_title_seed": thread_title_seed,
                                }
                    return RunOutcome(
                        status="completed",
                        event_type="run.completed",
                        payload=result_payload,
                    )

                # Gather interrupts from BOTH places LangGraph stores them:
                #   • snapshot.interrupts — aggregated top-level list
                #     (LangGraph 1.x). Reliable when present.
                #   • snapshot.tasks[*].interrupts — per-task lists. With
                #     parallel tool calls (e.g. ToolNode dispatches 3
                #     web.search + 1 user.ask in one step), each tool
                #     gets its own task; the user.ask interrupt lands in
                #     whichever task index ran it, NOT necessarily
                #     tasks[0]. Earlier code only checked tasks[0] and
                #     missed the interrupt → run stalled with empty
                #     interrupts and `next=["tools"]`.
                # We prefer the top-level list and fall back to scanning
                # every task. Dedupe by interrupt id so neither source
                # double-counts.
                interrupts_top = list(getattr(snapshot, "interrupts", ()) or ())
                interrupts_per_task = [
                    intr for task in (snapshot.tasks or ()) for intr in _task_interrupts(task)
                ]
                seen_ids: set[Any] = set()
                interrupts: list[Any] = []
                for intr in interrupts_top + interrupts_per_task:
                    key = getattr(intr, "id", None)
                    if key is None:
                        interrupts.append(intr)
                        continue
                    if key in seen_ids:
                        continue
                    seen_ids.add(key)
                    interrupts.append(intr)
                interrupt_ids = [
                    str(getattr(interrupt, "id", None) or f"anonymous-{index}")
                    for index, interrupt in enumerate(interrupts)
                ]
                wait_cycle_id = (
                    "wait_"
                    + hashlib.sha256(
                        f"{run_id}\0{current_checkpoint_id}\0".encode()
                        + "\0".join(interrupt_ids).encode()
                    ).hexdigest()[:32]
                )
                # Surface to user.
                for snap_interrupt in interrupts:
                    await self._handle_interrupt(
                        wakeup,
                        run_id,
                        snap_interrupt,
                        wait_cycle_id=wait_cycle_id,
                    )
                return RunOutcome(
                    status=_waiting_status_for_interrupts(interrupts),
                    event_type="run.waiting",
                    payload={
                        "next": next_nodes,
                        "wait_cycle_id": wait_cycle_id,
                        "interrupts": [
                            {"value": getattr(i, "value", None), "id": getattr(i, "id", None)}
                            for i in interrupts
                        ],
                        "handoff": await self._build_waiting_handoff(run_id),
                    },
                )

        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if self._shutting_down or current_task in self._lost_leases:
                raise
            if repair_context is not None:
                await self._enqueue(
                    wakeup,
                    run_id,
                    "repair.workflow",
                    _repair_workflow_payload(repair_context, status="canceled"),
                )
            return RunOutcome(
                status="canceled",
                event_type="run.canceled",
                payload={},
            )
        except Exception as exc:
            failure_payload = _run_failed_payload(
                exc,
                secrets=(model_api_key,) if model_api_key else (),
            )
            if model_api_key:
                log.error(
                    "run %s failed type=%s error=%s",
                    run_id,
                    type(exc).__name__,
                    failure_payload.get("error", "model service request failed"),
                )
            else:
                log.exception("run %s failed", run_id)
            if isinstance(exc, ModelServiceError):
                await self._enqueue(wakeup, run_id, "llm.error", failure_payload)
            if repair_context is not None:
                await self._enqueue(
                    wakeup,
                    run_id,
                    "repair.workflow",
                    _repair_workflow_payload(
                        repair_context,
                        status="failed",
                        reason=str(failure_payload.get("error") or exc),
                    ),
                )
            return RunOutcome(
                status="failed",
                event_type="run.failed",
                payload=failure_payload,
            )

    async def _build_waiting_handoff(self, run_id: str) -> dict[str, Any]:
        raw_events = await self.store.events_since(run_id, after_seq=0)
        events: list[dict[str, Any]] = []
        for event in raw_events:
            try:
                payload = json.loads(event.get("payload_json") or "{}")
            except json.JSONDecodeError:
                payload = {}
            events.append(
                {
                    "id": event.get("id"),
                    "run_id": event.get("run_id"),
                    "seq": event.get("seq"),
                    "event_type": event.get("event_type"),
                    "payload": payload,
                    "created_at": event.get("created_at"),
                }
            )
        artifacts = await self.store.list_artifacts_for_run(run_id)
        return build_handoff_snapshot(events, artifacts)

    async def _handle_interrupt(
        self,
        wakeup: asyncio.Event,
        run_id: str,
        snap_interrupt: Any,
        *,
        wait_cycle_id: str,
    ) -> None:
        """Bridge a LangGraph `interrupt(...)` into either:

        * `permission.required` (for the Runtime's parameter-bound tool
          review gate) — persisted in `local_permissions` so the
          renderer can resume after reload, and the POST resolver can
          look up `run_id` from the `permission_id` alone.
        * `question.asked` (for the `user.ask` tool) — persisted in
          `local_questions`.
        * `plan.approval_required` (for Plan Mode `write_todos`) —
          persisted in `local_plan_approvals`.

        Without this bridge, both flows surface only as the generic
        `run.waiting` and the UI can't render approval bars or question
        prompts — the agent silently stalls forever from the user's
        point of view.
        """
        value = getattr(snap_interrupt, "value", None)
        if isinstance(value, dict) and value.get("kind") == "tool_reconciliation":
            operation_id = str(value.get("operation_id") or "")
            interrupt_id = str(getattr(snap_interrupt, "id", None) or "")
            if not operation_id or not interrupt_id:
                raise RuntimeError("tool reconciliation is missing durable identity")
            record = await self.store.create_tool_reconciliation(
                run_id=run_id,
                operation_id=operation_id,
                wait_cycle_id=wait_cycle_id,
                interrupt_id=interrupt_id,
                payload=value,
            )
            await self._enqueue(
                wakeup,
                run_id,
                "tool.reconciliation_required",
                {
                    "request_id": record["id"],
                    "operation_id": operation_id,
                    "tool_name": str(value.get("tool_name") or ""),
                    "arguments_hash": str(value.get("arguments_hash") or ""),
                    "risk": str(value.get("risk") or ""),
                    "allowed_decisions": value.get("allowed_decisions") or [],
                    "wait_cycle_id": wait_cycle_id,
                    "interrupt_id": interrupt_id,
                },
            )
            return
        if isinstance(value, dict) and value.get("kind") == "plan_approval":
            todos = normalize_todos(value.get("todos"))
            tool_call_id = str(
                value.get("tool_call_id") or getattr(snap_interrupt, "id", None) or ""
            )
            summary = str(value.get("summary") or summarize_todos(todos))
            record = await self.store.create_plan_approval(
                run_id=run_id,
                tool_call_id=tool_call_id,
                todos=todos,
                summary=summary,
                wait_cycle_id=wait_cycle_id,
                interrupt_id=str(getattr(snap_interrupt, "id", None) or ""),
            )
            await self._enqueue(
                wakeup,
                run_id,
                "plan.approval_required",
                {
                    "request_id": record["id"],
                    "tool_call_id": tool_call_id,
                    "todos": record["todos"],
                    "summary": record["summary"],
                    "wait_cycle_id": wait_cycle_id,
                    "interrupt_id": record["interrupt_id"],
                },
            )
            return

        if isinstance(value, dict) and value.get("kind") == "question":
            question_text = str(value.get("question", ""))
            options_raw = value.get("options") or []
            # The `user.ask` tool signature is `options: list[str]`, but
            # the TS `AgentQuestionChoice` contract is `{label, description?}`.
            # Normalize at this boundary — every option becomes an object
            # with `label`. If the agent ever upgrades to passing dicts
            # (e.g. with descriptions), we pass those through unchanged.
            # Without this conversion the renderer's parseQuestionPayload
            # filters out every string option and silently shows nothing.
            options = _normalize_question_options(options_raw)
            questions = [
                {
                    "question": question_text,
                    "options": options,
                }
            ]
            record = await self.store.create_question(
                run_id=run_id,
                tool_call_id=getattr(snap_interrupt, "id", None),
                questions=questions,
                wait_cycle_id=wait_cycle_id,
                interrupt_id=str(getattr(snap_interrupt, "id", None) or ""),
            )
            # Attach the persisted id back onto each question so the
            # renderer's answer-binding code has a stable key.
            for q in questions:
                q["id"] = record["id"]
            await self._enqueue(
                wakeup,
                run_id,
                "question.asked",
                {
                    "request_id": record["id"],
                    "questions": questions,
                },
            )
            return

        # Parameter-bound tool review. The middleware includes the original
        # tool call id, operation id, arguments hash, and risk class. Persist
        # them verbatim so resume can prove it is authorizing this exact call.
        action_requests: list[dict[str, Any]] = []
        if isinstance(value, dict):
            ar_raw = value.get("action_requests")
            if isinstance(ar_raw, list):
                action_requests = [a for a in ar_raw if isinstance(a, dict)]
        if not action_requests:
            # Legacy / non-HITL interrupt shape — fall back to a single
            # generic record so we still surface something.
            action_requests = [{"name": "", "args": {}}]
        interrupt_id = str(getattr(snap_interrupt, "id", None) or "")
        for action_index, action in enumerate(action_requests):
            tool_name = str(action.get("name", ""))
            args_raw = action.get("args") or {}
            arguments = args_raw if isinstance(args_raw, dict) else {"value": args_raw}
            description = action.get("description") or ""
            record = await self.store.create_permission(
                run_id=run_id,
                tool_call_id=str(action.get("tool_call_id") or ""),
                tool_name=tool_name,
                tool_version=str(action.get("tool_version") or ""),
                arguments=arguments,
                operation_id=str(action.get("operation_id") or "") or None,
                arguments_hash=str(action.get("arguments_hash") or "") or None,
                risk=str(action.get("risk") or "") or None,
                wait_cycle_id=wait_cycle_id,
                interrupt_id=interrupt_id,
                action_index=action_index,
            )
            await self._enqueue(
                wakeup,
                run_id,
                "permission.required",
                {
                    "request_id": record["id"],
                    "tool": tool_name,
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "description": description,
                    "tool_call_id": record.get("tool_call_id"),
                    "operation_id": record.get("operation_id"),
                    "arguments_hash": record.get("arguments_hash"),
                    "risk": record.get("risk"),
                    "review_source": action.get("review_source"),
                    "review_reason": action.get("review_reason"),
                    "allowed_decisions": action.get("allowed_decisions") or ["approve", "reject"],
                    "allow_run_scope": action.get("allow_run_scope") is True,
                    "wait_cycle_id": wait_cycle_id,
                    "interrupt_id": interrupt_id,
                },
            )

    async def _enqueue(
        self,
        wakeup: asyncio.Event | None,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Publish transient output or persist an authoritative event."""
        parent_event: dict[str, Any] | None = None
        async with self._event_stream.publication(run_id):
            if event_type in TRANSIENT_RUN_EVENT_TYPES:
                event = {
                    "id": f"transient_{uuid.uuid4().hex}",
                    "run_id": run_id,
                    "event_type": event_type,
                    "payload": payload,
                    "created_at": datetime.now(UTC).isoformat(),
                }
                round_id = str(payload.get("round_id") or "")
                if event_type == "llm.delta" and round_id:
                    event["presentation_change"] = {
                        "kind": "draft.delta",
                        "round_id": round_id,
                        "content": str(payload.get("content") or ""),
                    }
                elif event_type == "llm.round.closed" and round_id:
                    event["presentation_change"] = {
                        "kind": "draft.closed",
                        "round_id": round_id,
                        "committed_item_ids": payload.get("committed_item_ids") or [],
                    }
            else:
                stored_event = await self.store.append_event(run_id, event_type, payload)
                candidate = stored_event.get("_parent_event")
                if isinstance(candidate, dict):
                    parent_event = candidate
                event = self._event_stream.stored_event_envelope(stored_event)
            self._event_stream.publish_live(run_id, event)
        trace_stream_event(event)
        if parent_event is not None:
            await self._publish_derived_parent_event(parent_event)
        if wakeup is not None:
            wakeup.set()

    async def _commit_run_result(
        self,
        wakeup: asyncio.Event,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        status: str,
    ) -> None:
        """Persist the authoritative result before notifying live subscribers."""
        parent_event: dict[str, Any] | None = None
        envelope: dict[str, Any] | None = None
        async with self._event_stream.publication(run_id):
            event, created = await self.store.commit_run_result(
                run_id,
                status=status,
                event_type=event_type,
                payload=payload,
            )
            if created:
                envelope = self._event_stream.stored_event_envelope(event)
                self._event_stream.publish_live(run_id, envelope)
                candidate = event.get("_parent_event")
                if isinstance(candidate, dict):
                    parent_event = candidate
        if created and envelope is not None:
            trace_stream_event(envelope)
        if parent_event is not None:
            await self._publish_derived_parent_event(parent_event)
        if not created:
            return
        wakeup.set()
        if self._terminal_callback is not None and status in {
            "completed",
            "failed",
            "canceled",
            "cleanup_required",
        }:
            task = asyncio.create_task(
                self._terminal_callback(run_id, status, payload),
                name=f"central-diagnostics:{run_id}",
            )
            self._terminal_callback_tasks.add(task)
            task.add_done_callback(
                lambda completed: self._terminal_callback_finished(
                    completed,
                    run_id=run_id,
                    status=status,
                )
            )
        if status in {"waiting_permission", "waiting_input"}:
            resume_payload = await self.store.latest_resolved_wait_cycle_payload(run_id)
            if resume_payload is not None:
                await self.resume_run(run_id=run_id, decision=resume_payload)

    async def _publish_derived_parent_event(self, event: dict[str, Any]) -> None:
        parent_run_id = str(event["run_id"])
        async with self._event_stream.publication(parent_run_id):
            self._event_stream.publish_live(
                parent_run_id,
                self._event_stream.stored_event_envelope(event),
            )
        wakeup = self._wakeups.get(parent_run_id)
        if wakeup is not None:
            wakeup.set()

    def _terminal_callback_finished(
        self,
        task: asyncio.Task[None],
        *,
        run_id: str,
        status: str,
    ) -> None:
        self._terminal_callback_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.warning(
                "central diagnostics upload failed run_id=%s status=%s error_type=%s",
                run_id,
                status,
                type(exc).__name__,
            )
