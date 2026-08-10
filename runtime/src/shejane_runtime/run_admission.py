"""Admission of new and checkpoint-forked Runs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .agent.builder import skill_catalog_fingerprint
from .build_info import runtime_build_identity
from .config import Settings
from .run_configuration import (
    RUNTIME_PROTOCOL_VERSION,
    _execution_policy_snapshot,
    freeze_run_settings,
    public_run_settings,
    runtime_capabilities,
    sanitize_run_metadata,
)
from .run_errors import CheckpointNotFoundError, RunNotFoundError
from .run_inputs import (
    _attachment_admission_error,
    _attachment_bindings,
    _prepare_run_inputs,
)
from .run_stream_state import _checkpoint_is_ancestor, _json_object
from .store.sqlite import (
    LocalStore,
    RunAdmissionError,
    RunInputSnapshotError,
    WorkspaceAdmissionError,
)


async def admit_run(
    *,
    store: LocalStore,
    runtime_settings: Settings,
    model_admission: Callable[..., Any],
    capability_binding_snapshots: Callable[..., Awaitable[Any]],
    wake_jobs: Callable[[], None],
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
    accepted = await store.accepted_run_for_command(
        principal_id=principal_id,
        command_id=command_id,
        client_message_id=client_message_id,
        command_payload=command_payload,
    )
    if accepted is not None:
        wake_jobs()
        return accepted

    admission_error: RunAdmissionError | None = None
    if protocol_version != RUNTIME_PROTOCOL_VERSION:
        admission_error = RunAdmissionError(
            "protocol_version_unsupported",
            f"runtime protocol version {protocol_version} is not supported",
        )
    missing = sorted(set(required_capabilities) - runtime_capabilities(runtime_settings))
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
    async with model_admission(principal_id, mode) as (
        model_binding,
        model_error,
    ):
        if admission_error is None:
            admission_error = model_error
        settings_snapshot = (
            dict(public_settings)
            if settings_are_frozen
            else freeze_run_settings(runtime_settings, public_settings)
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
            capability_bindings, capability_error = await capability_binding_snapshots(
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
                prepared_inputs = await _prepare_run_inputs(store, attachment_bindings)
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

        run, _created = await store.accept_run_command(
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
    wake_jobs()
    return run


async def admit_fork(
    *,
    store: LocalStore,
    runtime_settings: Settings,
    checkpointer: AsyncSqliteSaver,
    reconcile_graph_head: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    wake_jobs: Callable[[], None],
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
    accepted = await store.accepted_run_for_command(
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
    missing = sorted(set(required_capabilities) - runtime_capabilities(runtime_settings))
    if missing:
        raise RunAdmissionError(
            "capability_unavailable",
            f"runtime capabilities are unavailable: {', '.join(missing)}",
        )

    source = await store.get_run_for_principal(
        principal_id=principal_id,
        run_id=source_run_id,
    )
    if source is None:
        raise RunNotFoundError(source_run_id)
    workspace_error = await store.workspace_admission_error(
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
    source = await reconcile_graph_head(source)
    checkpoint_id = checkpoint_id.strip()
    if not checkpoint_id:
        raise CheckpointNotFoundError(checkpoint_id)

    graph_thread_id = str(source.get("graph_thread_id") or source_run_id)
    source_head_id = source.get("graph_checkpoint_id")
    if not isinstance(source_head_id, str) or not source_head_id:
        raise CheckpointNotFoundError(checkpoint_id)
    if not await _checkpoint_is_ancestor(
        checkpointer,
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
        fork_settings = freeze_run_settings(runtime_settings, source_settings)
    elif source_settings.get("_snapshot_version") == 1:
        fork_settings = source_settings
    else:
        raise RunAdmissionError(
            "settings_snapshot_unsupported",
            "source run settings snapshot version is unsupported",
        )
    run, _created = await store.accept_run_command(
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
    wake_jobs()
    return run
