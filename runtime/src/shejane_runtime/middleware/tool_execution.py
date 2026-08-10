"""Durable idempotency boundary around Runtime tool execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.types import Command, interrupt

from ..store.sqlite import (
    LocalStore,
    RunAdmissionError,
    ToolReceiptStateError,
)
from ..tool_outcomes import tool_result_envelope, tool_result_envelope_failed
from ..tools.runtime import (
    RuntimeToolExecution,
    bind_runtime_tool_execution,
    current_runtime_tool_execution_or_none,
)

if TYPE_CHECKING:
    from ..agent.tool_execution_gate import AsyncToolExecutionGate
from .tool_execution_identity import (
    CONTROL_FLOW_TOOLS as CONTROL_FLOW_TOOLS,
)
from .tool_execution_identity import (
    READ_ONLY_TOOLS as READ_ONLY_TOOLS,
)
from .tool_execution_identity import (
    RUNTIME_STATE_TOOLS as RUNTIME_STATE_TOOLS,
)
from .tool_execution_identity import (
    SANDBOXED_COMMAND_TOOLS as SANDBOXED_COMMAND_TOOLS,
)
from .tool_execution_identity import (
    WORKSPACE_WRITE_TOOLS as WORKSPACE_WRITE_TOOLS,
)
from .tool_execution_identity import (
    _batch_order_key as _batch_order_key,
)
from .tool_execution_identity import (
    _ordered_batch_position,
    tool_execution_namespace,
)
from .tool_execution_identity import (
    canonical_tool_execution_scope as canonical_tool_execution_scope,
)
from .tool_execution_identity import (
    current_execution_namespace as current_execution_namespace,
)
from .tool_execution_identity import (
    execution_namespace_from_config as execution_namespace_from_config,
)
from .tool_execution_identity import (
    execution_scope_from_messages as execution_scope_from_messages,
)
from .tool_execution_identity import (
    tool_operation_identity as tool_operation_identity,
)
from .tool_execution_identity import (
    tool_risk as tool_risk,
)
from .tool_execution_identity import (
    tool_version_for_context as tool_version_for_context,
)
from .tool_execution_identity import (
    tool_version_for_invocation as tool_version_for_invocation,
)
from .tool_receipt_recovery import (
    _cancel_before_tool_start,
    _receipt_result,
    _recover_child_spawn_receipt,
)
from .tool_result_codec import (
    MAX_MODEL_TOOL_RESULT_BYTES as MAX_MODEL_TOOL_RESULT_BYTES,
)
from .tool_result_codec import (
    MAX_TOOL_ARTIFACT_BYTES as MAX_TOOL_ARTIFACT_BYTES,
)
from .tool_result_codec import (
    _bound_tool_result,
    _provider_safe_tool_result,
)
from .tool_result_codec import (
    serialize_tool_result as serialize_tool_result,
)


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
        from ..agent.tool_execution_gate import AsyncToolExecutionGate

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
            contained_content: str | None = None
            if tool_name == "task" and not isinstance(exc, asyncio.CancelledError):
                contained_content = f"Subagent failed: {type(exc).__name__}: {str(exc)[:2000]}"
            elif (
                tool_name.startswith("child.")
                and isinstance(exc, KeyError)
                and str(exc).strip("'").startswith("child run not found:")
            ):
                contained_content = json.dumps(
                    {
                        "ok": False,
                        "error_code": "child_run_not_found",
                        "message": "child run not found",
                        "retryable": False,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            if contained_content is not None:
                result = ToolMessage(
                    content=contained_content,
                    name=tool_name,
                    tool_call_id=str(request.tool_call.get("id") or ""),
                    status="error",
                )
                result_json = serialize_tool_result(result)
                settle = (
                    store.settle_task_receipt if tool_name == "task" else store.settle_tool_receipt
                )
                await asyncio.shield(
                    settle(
                        operation_id=operation_id,
                        run_id=run_id,
                        status="failed",
                        result_json=result_json,
                        result_hash=hashlib.sha256(result_json.encode("utf-8")).hexdigest(),
                        error_type=type(exc).__name__,
                    )
                )
                return result
            settle = (
                store.settle_task_receipt
                if tool_name == "task" and status == "canceled"
                else store.settle_tool_receipt
            )
            await asyncio.shield(
                settle(
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
        settle = (
            store.settle_task_receipt
            if str(request.tool_call.get("name") or "") == "task"
            else store.settle_tool_receipt
        )
        await settle(
            operation_id=operation_id,
            run_id=run_id,
            status=status,
            result_json=result_json,
            result_hash=result_hash,
        )
        return result
