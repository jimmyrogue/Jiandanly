"""Durable idempotency boundary around Runtime tool execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.messages import BaseMessage, ToolMessage, message_to_dict, messages_from_dict
from langgraph.config import get_config
from langgraph.errors import GraphBubbleUp
from langgraph.types import Command, interrupt

from ..agent.context_builder import AsyncToolExecutionGate
from ..store.sqlite import (
    LocalStore,
    RunAdmissionError,
    ToolOutcomeUnknownError,
    ToolReceiptStateError,
)
from ..tool_outcomes import tool_result_envelope, tool_result_envelope_failed
from ..tools.runtime import (
    RuntimeToolExecution,
    bind_runtime_tool_execution,
    current_runtime_tool_execution_or_none,
)

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
MAX_MODEL_TOOL_RESULT_BYTES = 64 * 1024
MAX_TOOL_ARTIFACT_BYTES = 16 * 1024 * 1024


class WorkspaceRequiredError(RuntimeError):
    code = "workspace_required"
    retryable = False


class WorkspaceResourceOwnershipError(RuntimeError):
    code = "collaboration_resource_not_owned"
    retryable = False


def _tool_error_status(*, risk: str, tool_name: str, error: BaseException) -> str:
    if tool_name == "task" and isinstance(error, asyncio.CancelledError):
        return "canceled"
    # A task only orchestrates Runtime-owned child work. Any real child side
    # effect has its own durable receipt and remains independently reconcilable.
    known = (
        risk in {"read_only", "plugin_action"}
        or tool_name == "task"
        or tool_name.startswith("child.")
        or isinstance(error, WorkspaceRequiredError)
        or isinstance(error, WorkspaceResourceOwnershipError)
    )
    return "failed" if known else "outcome_unknown"


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


class ToolExecutionMiddleware(AgentMiddleware):
    """Make one model tool call replay-safe across checkpoint recovery."""

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        context = getattr(request.runtime, "context", None)
        store = getattr(context, "store", None)
        run_id = str(getattr(context, "run_id", None) or "")
        execution_attempt_id = str(getattr(context, "execution_attempt_id", None) or "")
        tool_name = str(request.tool_call.get("name") or "")
        execution_namespace = canonical_tool_execution_scope(
            execution_scope_from_messages(
                tool_execution_namespace(request),
                request.state.get("messages") if isinstance(request.state, dict) else None,
            )
        )
        if not isinstance(store, LocalStore) or not run_id or not execution_attempt_id:
            raise ToolReceiptStateError("tool execution is missing durable Runtime context")
        gate = getattr(context, "tool_mutation_lock", None)
        if not isinstance(gate, AsyncToolExecutionGate):
            raise ToolReceiptStateError("tool execution is missing its shared ordering gate")

        call = request.tool_call
        tool_call_id = str(call.get("id") or "")
        tool_name = str(call.get("name") or "")
        if not tool_call_id or not tool_name:
            raise ToolReceiptStateError("tool call is missing a stable id or name")
        if tool_name == "task" and getattr(context, "subagents_enabled", True) is False:
            return ToolMessage(
                content="Subagent dispatch is disabled for this Run.",
                name=tool_name,
                tool_call_id=tool_call_id,
                status="error",
            )
        arguments = call.get("args") or {}
        parent_execution = current_runtime_tool_execution_or_none()
        tool_version = await tool_version_for_invocation(context, tool_name, arguments)
        operation_id, arguments_hash, arguments_json = tool_operation_identity(
            run_id=run_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            tool_version=tool_version,
            execution_namespace=execution_namespace,
        )
        risk = tool_risk(tool_name)
        batch_order = _ordered_batch_position(request, execution_namespace)
        receipt = await store.prepare_tool_receipt(
            operation_id=operation_id,
            run_id=run_id,
            execution_attempt_id=execution_attempt_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_version=tool_version,
            execution_namespace=execution_namespace,
            arguments_hash=arguments_hash,
            arguments_json=arguments_json,
            risk=risk,
            parent_operation_id=(
                parent_execution.operation_id if parent_execution is not None else None
            ),
        )
        if receipt.get("status") == "outcome_unknown" and tool_name == "child.spawn":
            receipt = await _recover_child_spawn_receipt(
                store=store,
                receipt=receipt,
                run_id=run_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
        if receipt.get("status") == "outcome_unknown" and tool_name.startswith("mailbox."):
            # Every mailbox mutation is transactionally idempotent on the Tool
            # Receipt operation id; inbox is read-only. Re-running is therefore
            # the authoritative reconciliation path after a lost process.
            receipt = await store.reconcile_tool_receipt(
                operation_id=operation_id,
                run_id=run_id,
                decision="retry_not_executed",
            )
        if receipt.get("status") == "outcome_unknown":
            _request_tool_reconciliation(
                operation_id=operation_id,
                tool_name=tool_name,
                arguments_hash=arguments_hash,
                risk=risk,
            )
            raise ToolReceiptStateError(
                "tool reconciliation resumed without a persisted receipt decision"
            )
        if receipt.get("status") == "prepared":
            prior = await store.find_outcome_unknown_tool_receipt_in_lineage(
                current_run_id=run_id,
                tool_name=tool_name,
                arguments_hash=arguments_hash,
                risk=risk,
            )
            if prior is not None:
                _request_tool_reconciliation(
                    operation_id=operation_id,
                    prior_operation_id=str(prior["operation_id"]),
                    tool_name=tool_name,
                    arguments_hash=arguments_hash,
                    risk=risk,
                    tool_version=tool_version,
                    prior_tool_version=str(prior.get("tool_version") or ""),
                )
                raise ToolReceiptStateError(
                    "ancestor reconciliation resumed without a persisted receipt decision"
                )
        replay = _receipt_result(receipt)
        if replay is not None:
            replay = _provider_safe_tool_result(request, replay)
            if batch_order is not None:
                async with gate.ordered(*batch_order):
                    return replay
            return replay
        await _cancel_before_tool_start(store, run_id, operation_id)

        if batch_order is not None:
            async with gate.ordered(*batch_order):
                return await self._execute_with_gate(
                    gate=gate,
                    request=request,
                    handler=handler,
                    store=store,
                    run_id=run_id,
                    execution_attempt_id=execution_attempt_id,
                    operation_id=operation_id,
                    risk=risk,
                )
        return await self._execute_with_gate(
            gate=gate,
            request=request,
            handler=handler,
            store=store,
            run_id=run_id,
            execution_attempt_id=execution_attempt_id,
            operation_id=operation_id,
            risk=risk,
        )

    async def _execute_with_gate(
        self,
        *,
        gate: AsyncToolExecutionGate,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
        store: LocalStore,
        run_id: str,
        execution_attempt_id: str,
        operation_id: str,
        risk: str,
    ) -> ToolMessage | Command[Any]:
        lock = (
            gate.read()
            if risk == "read_only"
            else gate.write()
            if risk not in {"control_flow"}
            else None
        )
        if lock is None:
            return await self._execute_once(
                request=request,
                handler=handler,
                store=store,
                run_id=run_id,
                execution_attempt_id=execution_attempt_id,
                operation_id=operation_id,
                risk=risk,
            )
        async with lock:
            await _cancel_before_tool_start(store, run_id, operation_id)
            return await self._execute_once(
                request=request,
                handler=handler,
                store=store,
                run_id=run_id,
                execution_attempt_id=execution_attempt_id,
                operation_id=operation_id,
                risk=risk,
            )

    async def _execute_once(
        self,
        *,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
        store: LocalStore,
        run_id: str,
        execution_attempt_id: str,
        operation_id: str,
        risk: str,
    ) -> ToolMessage | Command[Any]:
        receipt = await store.begin_tool_receipt(
            operation_id=operation_id,
            run_id=run_id,
            execution_attempt_id=execution_attempt_id,
        )
        replay = _receipt_result(receipt)
        if replay is not None:
            return _provider_safe_tool_result(request, replay)

        try:
            if risk == "workspace_write" and not getattr(
                request.runtime.context, "workspace_root", None
            ):
                raise WorkspaceRequiredError(
                    "Authorize a workspace before creating or changing files."
                )
            if risk == "workspace_write":
                arguments = request.tool_call.get("args") or {}
                requested_path = (
                    arguments.get("file_path") or arguments.get("path")
                    if isinstance(arguments, dict)
                    else None
                )
                if isinstance(requested_path, str) and requested_path:
                    try:
                        await store.assert_workspace_resource_owner(
                            run_id=run_id,
                            requested_path=requested_path,
                        )
                    except RunAdmissionError as exc:
                        raise WorkspaceResourceOwnershipError(str(exc)) from exc
            if risk == "sandboxed_command" and await store.has_foreign_workspace_resource_claims(
                run_id
            ):
                raise WorkspaceResourceOwnershipError(
                    "execute is unavailable while another collaboration member owns workspace "
                    "resources; use the typed file tools so ownership can be enforced"
                )
            with bind_runtime_tool_execution(
                RuntimeToolExecution(
                    context=request.runtime.context,
                    operation_id=operation_id,
                    tool_call_id=str(request.tool_call.get("id") or ""),
                )
            ):
                result = await handler(request)
        except GraphBubbleUp:
            await asyncio.shield(
                store.settle_tool_receipt(
                    operation_id=operation_id,
                    run_id=run_id,
                    status="paused",
                )
            )
            raise
        except BaseException as exc:
            tool_name = str(request.tool_call.get("name") or "")
            status = _tool_error_status(
                risk=risk,
                tool_name=tool_name,
                error=exc,
            )
            if tool_name == "task" and not isinstance(exc, asyncio.CancelledError):
                result = ToolMessage(
                    content=f"Subagent failed: {type(exc).__name__}: {str(exc)[:2000]}",
                    name=tool_name,
                    tool_call_id=str(request.tool_call.get("id") or ""),
                    status="error",
                )
                result_json = serialize_tool_result(result)
                await asyncio.shield(
                    store.settle_tool_receipt(
                        operation_id=operation_id,
                        run_id=run_id,
                        status="failed",
                        result_json=result_json,
                        result_hash=hashlib.sha256(result_json.encode("utf-8")).hexdigest(),
                        error_type=type(exc).__name__,
                    )
                )
                return result
            await asyncio.shield(
                store.settle_tool_receipt(
                    operation_id=operation_id,
                    run_id=run_id,
                    status=status,
                    error_type=type(exc).__name__,
                )
            )
            if status == "outcome_unknown" and not isinstance(exc, asyncio.CancelledError):
                _request_tool_reconciliation(
                    operation_id=operation_id,
                    tool_name=tool_name,
                    arguments_hash=str(receipt.get("arguments_hash") or ""),
                    risk=risk,
                )
            raise

        try:
            if (
                isinstance(result, ToolMessage)
                and str(result.status or "") != "error"
                and tool_result_envelope_failed(tool_result_envelope(result.content))
            ):
                result = result.model_copy(update={"status": "error"})
            result = _provider_safe_tool_result(request, result)
            result = await _bound_tool_result(
                result=result,
                store=store,
                run_id=run_id,
                operation_id=operation_id,
                tool_call=request.tool_call,
            )
            result_json = serialize_tool_result(result)
            if len(result_json.encode("utf-8")) > MAX_MODEL_TOOL_RESULT_BYTES:
                raise ToolReceiptStateError("bounded tool result still exceeds model limit")
        except BaseException as exc:
            status = _tool_error_status(
                risk=risk,
                tool_name=str(request.tool_call.get("name") or ""),
                error=exc,
            )
            await asyncio.shield(
                store.settle_tool_receipt(
                    operation_id=operation_id,
                    run_id=run_id,
                    status=status,
                    error_type=type(exc).__name__,
                )
            )
            if status == "outcome_unknown" and not isinstance(exc, asyncio.CancelledError):
                _request_tool_reconciliation(
                    operation_id=operation_id,
                    tool_name=str(request.tool_call.get("name") or ""),
                    arguments_hash=str(receipt.get("arguments_hash") or ""),
                    risk=risk,
                )
            raise
        result_hash = hashlib.sha256(result_json.encode("utf-8")).hexdigest()
        status = (
            "failed"
            if isinstance(result, ToolMessage) and str(result.status or "") == "error"
            else "completed"
        )
        await store.settle_tool_receipt(
            operation_id=operation_id,
            run_id=run_id,
            status=status,
            result_json=result_json,
            result_hash=result_hash,
        )
        return result


async def _recover_child_spawn_receipt(
    *,
    store: LocalStore,
    receipt: dict[str, Any],
    run_id: str,
    tool_name: str,
    tool_call_id: str,
) -> dict[str, Any]:
    """Reconcile the one internal side effect whose outcome Runtime owns."""
    operation_id = str(receipt["operation_id"])
    child = await store.child_run_for_spawn_operation(run_id, operation_id)
    if child is None:
        return await store.reconcile_tool_receipt(
            operation_id=operation_id,
            run_id=run_id,
            decision="retry_not_executed",
        )
    result = ToolMessage(
        content=json.dumps(child, ensure_ascii=False),
        name=tool_name,
        tool_call_id=tool_call_id,
    )
    result_json = serialize_tool_result(result)
    return await store.reconcile_tool_receipt(
        operation_id=operation_id,
        run_id=run_id,
        decision="confirmed_completed",
        result_json=result_json,
        result_hash=hashlib.sha256(result_json.encode("utf-8")).hexdigest(),
    )


def _provider_safe_tool_result(
    request: ToolCallRequest,
    result: ToolMessage | Command[Any],
) -> ToolMessage | Command[Any]:
    """Keep file results compatible with the selected model and provider.

    Text-only models receive an explicit limitation instead of image bytes.
    Runtime-extracted PDF text is also unwrapped from Deep Agents' synthetic
    file block before it reaches OpenAI-compatible providers.
    """
    if not isinstance(result, ToolMessage):
        return result
    blocks = result.content
    call = request.tool_call
    context = getattr(request.runtime, "context", None)
    model_profile = getattr(getattr(context, "model", None), "profile", None)
    if (
        isinstance(blocks, list)
        and any(isinstance(block, dict) and block.get("type") == "image" for block in blocks)
        and isinstance(model_profile, dict)
        and model_profile.get("image_inputs") is False
    ):
        limitation = (
            "Image content was not provided because the selected model is text-only. "
            "Choose a model marked as supporting images before describing this file."
        )
        remaining = [
            block
            for block in blocks
            if not (isinstance(block, dict) and block.get("type") == "image")
        ]
        return result.model_copy(
            update={
                "content": (
                    [*remaining, {"type": "text", "text": limitation}] if remaining else limitation
                ),
                "additional_kwargs": {
                    **result.additional_kwargs,
                    "runtime_image_omitted": True,
                },
            }
        )
    if str(call.get("name") or "") != "read_file":
        return result
    arguments = call.get("args")
    requested_path = arguments.get("file_path") if isinstance(arguments, dict) else None
    if not isinstance(requested_path, str) or not requested_path.lower().endswith(".pdf"):
        return result
    attachments = getattr(context, "attachments", ())
    if requested_path not in attachments:
        return result
    blocks = result.content
    if not isinstance(blocks, list) or len(blocks) != 1:
        return result
    block = blocks[0]
    if not isinstance(block, dict) or block.get("type") != "file":
        return result
    if block.get("mime_type") != "application/pdf" or not isinstance(block.get("base64"), str):
        return result
    return result.model_copy(
        update={
            "content": block["base64"],
            "additional_kwargs": {
                **result.additional_kwargs,
                "runtime_extracted_text_from": "application/pdf",
            },
        }
    )


def _receipt_result(receipt: dict[str, Any]) -> ToolMessage | Command[Any] | None:
    status = str(receipt.get("status") or "")
    if status == "outcome_unknown":
        raise ToolOutcomeUnknownError(
            f"tool operation {receipt.get('operation_id')} requires reconciliation"
        )
    if status == "canceled":
        raise asyncio.CancelledError
    result_json = receipt.get("result_json")
    if status in {"completed", "failed", "rejected"}:
        if not isinstance(result_json, str) or not result_json:
            raise ToolReceiptStateError(
                f"terminal tool receipt {receipt.get('operation_id')} has no result"
            )
        return _deserialize_tool_result(result_json)
    return None


def _request_tool_reconciliation(
    *,
    operation_id: str,
    tool_name: str,
    arguments_hash: str,
    risk: str,
    prior_operation_id: str | None = None,
    tool_version: str | None = None,
    prior_tool_version: str | None = None,
) -> Any:
    return interrupt(
        {
            "kind": "tool_reconciliation",
            "operation_id": operation_id,
            "prior_operation_id": prior_operation_id,
            "tool_version": tool_version,
            "prior_tool_version": prior_tool_version,
            "tool_name": tool_name,
            "arguments_hash": arguments_hash,
            "risk": risk,
            "allowed_decisions": [
                "confirmed_completed",
                "retry_not_executed",
                "abort",
            ],
        }
    )


async def _cancel_before_tool_start(store: LocalStore, run_id: str, operation_id: str) -> None:
    if not await store.tool_execution_cancel_requested(run_id):
        return
    receipt = await store.get_tool_receipt(operation_id)
    if receipt is not None and receipt.get("status") in {"prepared", "paused"}:
        await store.settle_tool_receipt(
            operation_id=operation_id,
            run_id=run_id,
            status="canceled",
            error_type="RunCanceledBeforeToolStart",
        )
    raise asyncio.CancelledError


def serialize_tool_result(result: ToolMessage | Command[Any]) -> str:
    if isinstance(result, ToolMessage):
        payload = {"kind": "tool_message", "value": message_to_dict(result)}
    elif isinstance(result, Command):
        payload = {
            "kind": "command",
            "graph": result.graph,
            "update": _encode_json_value(result.update),
            "resume": _encode_json_value(result.resume),
            "goto": _encode_json_value(result.goto),
        }
    else:  # pragma: no cover - enforced by LangChain's wrapper contract
        raise ToolReceiptStateError("tool handler returned an unsupported result type")
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


async def _bound_tool_result(
    *,
    result: ToolMessage | Command[Any],
    store: LocalStore,
    run_id: str,
    operation_id: str,
    tool_call: dict[str, Any],
) -> ToolMessage | Command[Any]:
    if isinstance(result, ToolMessage):
        raw = _message_content_text(result.content)
        content_type = "text/plain"
        if len(raw.encode("utf-8")) <= MAX_MODEL_TOOL_RESULT_BYTES:
            serialized = serialize_tool_result(result)
            if len(serialized.encode("utf-8")) <= MAX_MODEL_TOOL_RESULT_BYTES:
                return result
    else:
        raw = serialize_tool_result(result)
        content_type = "application/json"
    raw_bytes = raw.encode("utf-8")
    if not isinstance(result, ToolMessage) and len(raw_bytes) <= MAX_MODEL_TOOL_RESULT_BYTES:
        return result
    # ponytail: SQLite artifacts cap at 16 MiB; move oversized bodies to a
    # content-addressed file/blob store when real workloads exceed this ceiling.
    artifact_content = _head_tail_bytes(raw_bytes, MAX_TOOL_ARTIFACT_BYTES)
    artifact = await store.create_artifact(
        artifact_id=f"art_{operation_id.removeprefix('toolop_')}",
        run_id=run_id,
        kind="tool_output",
        title=f"{tool_call.get('name') or 'tool'} full output",
        content=artifact_content,
        content_type=content_type,
        tool_call_id=str(tool_call.get("id") or ""),
        tool_name=str(tool_call.get("name") or ""),
        metadata={
            "operation_id": operation_id,
            "original_bytes": len(raw_bytes),
            "artifact_truncated": len(raw_bytes) > MAX_TOOL_ARTIFACT_BYTES,
        },
    )
    source = raw
    preview = _head_tail_bytes(source.encode("utf-8"), 32 * 1024)
    artifact_is_complete = len(raw_bytes) <= MAX_TOOL_ARTIFACT_BYTES
    summary = ToolMessage(
        content=(
            f"{preview}\n\n[{'Full' if artifact_is_complete else 'Truncated'} tool output "
            f"stored as artifact {artifact['id']}; "
            f"original size {len(raw_bytes)} bytes.]"
        ),
        name=str(tool_call.get("name") or ""),
        tool_call_id=str(tool_call.get("id") or ""),
        status=result.status if isinstance(result, ToolMessage) else "success",
        additional_kwargs={
            "artifact_id": artifact["id"],
            "original_bytes": len(raw_bytes),
        },
    )
    if not isinstance(result, Command):
        return summary
    update = result.update
    if isinstance(update, dict):
        bounded_update = {**update, "messages": [summary]}
        candidate = Command(
            graph=result.graph,
            update=bounded_update,
            resume=result.resume,
            goto=result.goto,
        )
        if len(serialize_tool_result(candidate).encode("utf-8")) <= MAX_MODEL_TOOL_RESULT_BYTES:
            return candidate
    raise ToolReceiptStateError(
        "oversized Command contains non-message state that cannot be safely compacted"
    )


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def _head_tail_bytes(value: bytes, limit: int) -> str:
    if len(value) <= limit:
        return value.decode("utf-8", errors="replace")
    marker = f"\n…[{len(value) - limit} bytes omitted]…\n".encode()
    available = max(0, limit - len(marker))
    head = available * 3 // 4
    tail = available - head
    return (value[:head] + marker + value[-tail:]).decode("utf-8", errors="replace")


def _deserialize_tool_result(value: str) -> ToolMessage | Command[Any]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ToolReceiptStateError("stored tool result is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ToolReceiptStateError("stored tool result is not an object")
    if payload.get("kind") == "tool_message":
        raw = payload.get("value")
        if not isinstance(raw, dict):
            raise ToolReceiptStateError("stored ToolMessage is invalid")
        messages = messages_from_dict([raw])
        if len(messages) != 1 or not isinstance(messages[0], ToolMessage):
            raise ToolReceiptStateError("stored result is not a ToolMessage")
        return messages[0]
    if payload.get("kind") == "command":
        return Command(
            graph=payload.get("graph"),
            update=_decode_json_value(payload.get("update")),
            resume=_decode_json_value(payload.get("resume")),
            goto=_decode_json_value(payload.get("goto")),
        )
    raise ToolReceiptStateError("stored tool result kind is unsupported")


def _encode_json_value(value: Any) -> Any:
    if isinstance(value, BaseMessage):
        return {"__runtime_type__": "message", "value": message_to_dict(value)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ToolReceiptStateError("tool result contains a non-string mapping key")
        return {key: _encode_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode_json_value(item) for item in value]
    raise ToolReceiptStateError(f"tool result contains unsupported value {type(value).__name__}")


def _decode_json_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode_json_value(item) for item in value]
    if isinstance(value, dict):
        if value.get("__runtime_type__") == "message":
            raw = value.get("value")
            if not isinstance(raw, dict):
                raise ToolReceiptStateError("stored message value is invalid")
            return messages_from_dict([raw])[0]
        return {key: _decode_json_value(item) for key, item in value.items()}
    return value


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
