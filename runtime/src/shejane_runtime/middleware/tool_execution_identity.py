"""Stable tool risk, namespace, version, and operation identities."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.config import get_config

from ..store.sqlite import LocalStore

READ_ONLY_TOOLS = {
    "clipboard.read",
    "environment.observe",
    "glob",
    "grep",
    "ls",
    "memory.search",
    "office.outline",
    "office.read",
    "office.read_range",
    "office.read_slides",
    "pdf.inspect",
    "read_file",
    "task.verify",
    "time.now",
    "web.fetch",
    "web.search",
}
WORKSPACE_WRITE_TOOLS = {
    "edit_file",
    "write_file",
    "office.add_image_to_slide",
    "office.add_row",
    "office.add_slide",
    "office.apply_style",
    "office.create_pptx",
    "office.delete_paragraph",
    "office.delete_slide",
    "office.find_replace",
    "office.insert_paragraph",
    "office.merge_cells",
    "office.reorder_slides",
    "office.set_cell_format",
    "office.set_cells",
    "office.set_formula",
    "office.set_slide_bullets",
    "office.set_slide_notes",
    "office.set_slide_title",
    "office.update_paragraph",
    "office.update_slide",
}
RUNTIME_STATE_TOOLS = {"memory.write", "task.progress", "write_todos"}
CONTROL_FLOW_TOOLS = {
    "task",
    "team.run",
    "child.spawn",
    "child.list",
    "child.check",
    "child.wait",
    "child.cancel",
    "mailbox.send",
    "mailbox.inbox",
    "mailbox.reply",
    "mailbox.ack",
    "user.ask",
}
SANDBOXED_COMMAND_TOOLS = {"execute"}


def tool_execution_namespace(request: ToolCallRequest) -> str:
    config = getattr(getattr(request, "runtime", None), "config", None)
    return execution_namespace_from_config(config)


def current_execution_namespace() -> str:
    try:
        config = get_config()
    except RuntimeError:
        config = None
    return execution_namespace_from_config(config)


def execution_namespace_from_config(config: Any) -> str:
    configurable = config.get("configurable") if isinstance(config, dict) else None
    raw = configurable.get("checkpoint_ns") if isinstance(configurable, dict) else None
    value = str(raw or "main")
    if len(value) <= 256:
        return value
    parent, separator, leaf = value.rpartition("|")
    if separator:
        parent_token = f"ns_{hashlib.sha256(parent.encode()).hexdigest()}"
        leaf_token = (
            leaf if len(leaf) <= 128 else f"leaf_{hashlib.sha256(leaf.encode()).hexdigest()}"
        )
        return f"{parent_token}|{leaf_token}"
    return f"ns_{hashlib.sha256(value.encode()).hexdigest()}"


def execution_scope_from_messages(base_namespace: str, messages: Any) -> str:
    if not isinstance(messages, list):
        return base_namespace
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        calls = getattr(message, "tool_calls", None)
        if not isinstance(calls, list) or not calls:
            continue
        identity = json.dumps(
            {
                "message_id": str(getattr(message, "id", None) or ""),
                "message_index": index,
                "calls": calls,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return f"{base_namespace}|batch_{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
    return base_namespace


def canonical_tool_execution_scope(execution_scope: str) -> str:
    """Remove the LangGraph node-local namespace while retaining subagent ancestry."""
    base_namespace, marker, batch_hash = execution_scope.rpartition("|batch_")
    if not marker:
        return execution_scope
    parent_namespace = base_namespace.rsplit("|", 1)[0] if "|" in base_namespace else ""
    return f"{parent_namespace}|batch_{batch_hash}"


def tool_risk(tool_name: str) -> str:
    if tool_name.startswith("plugin."):
        return "plugin_action"
    if tool_name in READ_ONLY_TOOLS:
        return "read_only"
    if tool_name in WORKSPACE_WRITE_TOOLS:
        return "workspace_write"
    if tool_name in RUNTIME_STATE_TOOLS:
        return "runtime_state"
    if tool_name in CONTROL_FLOW_TOOLS:
        return "control_flow"
    if tool_name in SANDBOXED_COMMAND_TOOLS:
        return "sandboxed_command"
    return "external_or_unknown"


def tool_version_for_context(context: object, tool_name: str) -> str:
    plugin_versions = getattr(context, "plugin_tool_versions", None)
    if isinstance(plugin_versions, dict):
        plugin_version = plugin_versions.get(tool_name)
        if isinstance(plugin_version, str) and plugin_version:
            return plugin_version
    return str(getattr(context, "graph_definition_id", None) or "")


async def tool_version_for_invocation(
    context: object,
    tool_name: str,
    arguments: Any,
) -> str:
    base = tool_version_for_context(context, tool_name)
    plugin_versions = getattr(context, "plugin_tool_versions", None)
    if not isinstance(plugin_versions, dict) or tool_name not in plugin_versions:
        return base
    input_id = arguments.get("input_id") if isinstance(arguments, dict) else None
    input_ids = arguments.get("input_ids") if isinstance(arguments, dict) else None
    selected_ids: list[str]
    if isinstance(input_id, str) and input_id:
        selected_ids = [input_id]
    elif (
        isinstance(input_ids, list)
        and input_ids
        and all(isinstance(item, str) and item for item in input_ids)
    ):
        selected_ids = input_ids
    else:
        return base
    store = getattr(context, "store", None)
    run_id = str(getattr(context, "run_id", None) or "")
    if not isinstance(store, LocalStore) or not run_id:
        return base
    bindings: list[dict[str, Any]] = []
    for selected_id in selected_ids:
        artifact = await store.get_artifact(selected_id)
        if (
            artifact is None
            or artifact.get("run_id") != run_id
            or artifact.get("storage_kind") != "blob"
            or not isinstance(artifact.get("content_type"), str)
            or not isinstance(artifact.get("bytes"), int)
            or not isinstance(artifact.get("sha256"), str)
        ):
            continue
        bindings.append(
            {
                "id": selected_id,
                "media_type": artifact["content_type"],
                "size_bytes": artifact["bytes"],
                "sha256": artifact["sha256"],
            }
        )
    if not bindings:
        return base
    binding = json.dumps(
        bindings,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(binding.encode("utf-8")).hexdigest()
    return f"{base}:artifact-input:sha256:{digest}"


def tool_operation_identity(
    *,
    run_id: str,
    tool_call_id: str,
    tool_name: str,
    arguments: Any,
    tool_version: str = "",
    execution_namespace: str = "main",
) -> tuple[str, str, str]:
    arguments_json = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    arguments_hash = hashlib.sha256(arguments_json.encode("utf-8")).hexdigest()
    operation_hash = hashlib.sha256(
        f"{run_id}\0{execution_namespace}\0{tool_call_id}\0{tool_name}\0"
        f"{tool_version}\0{arguments_hash}".encode()
    ).hexdigest()
    return f"toolop_{operation_hash[:32]}", arguments_hash, arguments_json


def _ordered_batch_position(
    request: ToolCallRequest, execution_scope: str
) -> tuple[str, int, int] | None:
    """Order conflicting calls exactly as emitted while leaving pure reads parallel."""
    state = request.state if isinstance(request.state, dict) else {}
    messages = state.get("messages") if isinstance(state, dict) else None
    if not isinstance(messages, list):
        return None
    ai_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if getattr(messages[index], "tool_calls", None)
        ),
        None,
    )
    ai_message = messages[ai_index] if ai_index is not None else None
    calls = getattr(ai_message, "tool_calls", None)
    if not isinstance(calls, list):
        return None
    resolved_ids = {
        str(message.tool_call_id)
        for message in messages[(ai_index or 0) + 1 :]
        if isinstance(message, ToolMessage) and message.tool_call_id
    }
    ordered_calls = [
        call
        for call in calls
        if isinstance(call, dict) and tool_risk(str(call.get("name") or "")) != "control_flow"
    ]
    if not any(tool_risk(str(call.get("name") or "")) != "read_only" for call in ordered_calls):
        return None
    completed_prefix = 0
    for call in ordered_calls:
        if str(call.get("id") or "") not in resolved_ids:
            break
        completed_prefix += 1
    call_id = str(request.tool_call.get("id") or "")
    batch_key = _batch_order_key(execution_scope)
    for position, call in enumerate(ordered_calls):
        if str(call.get("id") or "") == call_id:
            return batch_key, position, completed_prefix
    return None


def _batch_order_key(execution_scope: str) -> str:
    """Share a key between sibling tools without merging separate subagents."""
    return canonical_tool_execution_scope(execution_scope)
