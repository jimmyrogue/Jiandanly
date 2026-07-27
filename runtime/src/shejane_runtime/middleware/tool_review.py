"""Parameter-bound human review before consequential tool execution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import AgentState
from langchain_core.messages import AIMessage, ToolCall, ToolMessage
from langgraph.config import get_stream_writer
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from ..permission_policy import IRREVERSIBLE_TOOLS, can_grant_for_run
from ..store.sqlite import LocalStore, PermissionDecisionConflictError
from ..tool_schemas import validate_tool_input
from .approval_reviewer import ApprovalReviewUnavailable, review_approval_batch
from .tool_execution import (
    canonical_tool_execution_scope,
    current_execution_namespace,
    execution_scope_from_messages,
    serialize_tool_result,
    tool_operation_identity,
    tool_risk,
    tool_version_for_invocation,
)


class ToolReviewStateError(RuntimeError):
    """A review resume does not match its persisted wait candidate."""

    code = "tool_review_state_invalid"
    retryable = False


@dataclass(frozen=True, slots=True)
class ApprovalPolicyDecision:
    decision: str
    reason: str


def approval_policy_decision(
    tool_name: str,
    risk: str,
    permission_mode: str = "ask",
) -> ApprovalPolicyDecision:
    """Return the Runtime-owned P10 decision before optional model review."""
    if tool_name in IRREVERSIBLE_TOOLS:
        return ApprovalPolicyDecision("ask", "irreversible")
    if tool_name == "image.generate":
        return ApprovalPolicyDecision("allow", "image_generation")
    if permission_mode == "full_access":
        return ApprovalPolicyDecision("allow", "full_access")
    if tool_name == "clipboard.read":
        return ApprovalPolicyDecision("ask", "protected_runtime_state")
    if permission_mode == "auto":
        if risk == "external_or_unknown":
            return ApprovalPolicyDecision("review", "external_or_unknown")
        return ApprovalPolicyDecision("allow", "runtime_safe")
    if risk in {"workspace_write", "sandboxed_command", "external_or_unknown", "plugin_action"}:
        return ApprovalPolicyDecision("ask", risk)
    return ApprovalPolicyDecision("allow", "read_only")


def tool_requires_review(tool_name: str, risk: str, permission_mode: str = "ask") -> bool:
    """Return whether policy requires a person before this call executes."""
    return approval_policy_decision(tool_name, risk, permission_mode).decision in {"ask", "review"}


class ToolReviewMiddleware(AgentMiddleware):
    """Pause a complete model tool batch before any reviewed call executes.

    The interrupt carries stable call ids and argument fingerprints. A resume
    is accepted only when the corresponding durable permission rows contain
    the same decisions; callers cannot bypass the typed permission endpoint by
    posting an arbitrary LangGraph resume payload.
    """

    async def aafter_model(
        self,
        state: AgentState[Any],
        runtime: Runtime[Any],
    ) -> dict[str, Any] | None:
        messages = state.get("messages") or []
        last_ai_index = next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if isinstance(messages[index], AIMessage)
            ),
            None,
        )
        if last_ai_index is None:
            return None
        last_ai = messages[last_ai_index]
        if not last_ai.tool_calls:
            return None
        answered_call_ids = {
            str(message.tool_call_id)
            for message in messages[last_ai_index + 1 :]
            if isinstance(message, ToolMessage) and message.tool_call_id
        }
        call_ids = [str(call.get("id") or "") for call in last_ai.tool_calls]
        if any(not call_id for call_id in call_ids) or len(call_ids) != len(set(call_ids)):
            raise ToolReviewStateError("tool review requires unique non-empty call ids per batch")

        context = runtime.context
        store = getattr(context, "store", None)
        run_id = str(getattr(context, "run_id", None) or "")
        execution_attempt_id = str(getattr(context, "execution_attempt_id", None) or "")
        execution_namespace = canonical_tool_execution_scope(
            execution_scope_from_messages(current_execution_namespace(), messages)
        )
        permission_mode = str(getattr(context, "permission_mode", "ask") or "ask")
        if not isinstance(store, LocalStore) or not run_id or not execution_attempt_id:
            raise ToolReviewStateError("tool review is missing durable Runtime context")

        review_calls: list[tuple[int, ToolCall, dict[str, Any]]] = []
        action_requests: list[dict[str, Any]] = []
        model_review_calls: list[tuple[int, ToolCall, dict[str, Any]]] = []
        artificial_messages: list[ToolMessage] = []
        tool_registry = getattr(context, "tool_registry", None)
        if not isinstance(tool_registry, dict):
            raise ToolReviewStateError("tool review is missing its frozen tool registry")
        for index, call in enumerate(last_ai.tool_calls):
            tool_call_id = str(call.get("id") or "")
            if tool_call_id in answered_call_ids:
                continue
            tool_name = str(call.get("name") or "")
            if not tool_name:
                raise ToolReviewStateError("tool review requires stable call ids and names")
            arguments = call.get("args") or {}
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
            validation_error = _tool_input_error(tool_registry.get(tool_name), arguments)
            if validation_error is not None:
                message = ToolMessage(
                    content=validation_error,
                    name=tool_name,
                    tool_call_id=tool_call_id,
                    status="error",
                )
                artificial_messages.append(message)
                await _record_preflight_failure(
                    store=store,
                    run_id=run_id,
                    execution_attempt_id=execution_attempt_id,
                    tool_version=tool_version,
                    execution_namespace=execution_namespace,
                    call=call,
                    operation_id=operation_id,
                    arguments_hash=arguments_hash,
                    risk=risk,
                    message=message,
                )
                continue
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
            )
            current_permission = await store.get_permission_for_operation(
                run_id=run_id, operation_id=operation_id
            )
            # Code before LangGraph interrupt() is replayed on resume. The
            # current operation must reach interrupt() again to consume its
            # Command(resume=...). A run grant may skip only a *new* call id
            # with the same approved argument fingerprint.
            if current_permission is None:
                if await store.consume_run_permission_grant(
                    run_id=run_id,
                    operation_id=operation_id,
                    tool_name=tool_name,
                    tool_version=tool_version,
                    risk=risk,
                ):
                    await _record_review_decision(
                        store=store,
                        run_id=run_id,
                        receipt=receipt,
                        decision="allow",
                        source="run_grant",
                        reason="A matching run-scoped user grant is active.",
                        model=None,
                    )
                    _emit_auto_approved(
                        operation_id=operation_id,
                        tool_name=tool_name,
                        risk=risk,
                        source="run_grant",
                        reason="A matching run-scoped user grant is active.",
                    )
                    continue
            metadata = {
                "tool_call_id": tool_call_id,
                "operation_id": operation_id,
                "tool_version": tool_version,
                "arguments_hash": arguments_hash,
                "risk": risk,
            }
            persisted_decision = str(receipt.get("review_decision") or "")
            if persisted_decision == "allow":
                continue
            if persisted_decision == "ask" or current_permission is not None:
                persisted_metadata = {
                    **metadata,
                    **(
                        {"review_source": str(receipt["review_source"])}
                        if receipt.get("review_source")
                        else {}
                    ),
                    **(
                        {"review_reason": str(receipt["review_reason"])}
                        if receipt.get("review_reason")
                        else {}
                    ),
                }
                _append_human_review(
                    review_calls,
                    action_requests,
                    index=index,
                    call=call,
                    metadata=persisted_metadata,
                )
                continue
            policy = approval_policy_decision(tool_name, risk, permission_mode)
            if policy.decision == "allow":
                await _record_review_decision(
                    store=store,
                    run_id=run_id,
                    receipt=receipt,
                    decision="allow",
                    source="rule",
                    reason=policy.reason,
                    model=None,
                )
                _emit_auto_approved(
                    operation_id=operation_id,
                    tool_name=tool_name,
                    risk=risk,
                    source="rule",
                    reason=policy.reason,
                )
                continue
            if policy.decision == "review":
                model_review_calls.append((index, call, metadata))
                continue
            await _record_review_decision(
                store=store,
                run_id=run_id,
                receipt=receipt,
                decision="ask",
                source="rule",
                reason=policy.reason,
                model=None,
            )
            _append_human_review(
                review_calls,
                action_requests,
                index=index,
                call=call,
                metadata=metadata,
            )

        if model_review_calls:
            review_actions = [
                {
                    "operation_id": metadata["operation_id"],
                    "tool_name": call["name"],
                    "risk": metadata["risk"],
                    "arguments": call.get("args") or {},
                }
                for _index, call, metadata in model_review_calls
            ]
            try:
                model_decisions = await review_approval_batch(
                    model=(
                        getattr(context, "approval_model", None) or getattr(context, "model", None)
                    ),
                    task_goal=str(getattr(context, "task_goal", None) or ""),
                    actions=review_actions,
                )
                review_source = "llm"
                review_model = str(getattr(context, "mode", None) or "") or None
            except ApprovalReviewUnavailable:
                model_decisions = {
                    metadata["operation_id"]: {
                        "decision": "ask",
                        "reason": "The model reviewer is unavailable; fallback policy requires confirmation.",
                    }
                    for _index, _call, metadata in model_review_calls
                }
                review_source = "fallback"
                review_model = None
            for index, call, metadata in model_review_calls:
                reviewed = model_decisions[metadata["operation_id"]]
                receipt = await store.get_tool_receipt(metadata["operation_id"])
                if receipt is None:
                    raise ToolReviewStateError("tool review receipt disappeared before decision")
                await _record_review_decision(
                    store=store,
                    run_id=run_id,
                    receipt=receipt,
                    decision=reviewed["decision"],
                    source=review_source,
                    reason=reviewed["reason"],
                    model=review_model,
                )
                if reviewed["decision"] == "allow":
                    _emit_auto_approved(
                        operation_id=metadata["operation_id"],
                        tool_name=call["name"],
                        risk=metadata["risk"],
                        source=review_source,
                        reason=reviewed["reason"],
                    )
                    continue
                _append_human_review(
                    review_calls,
                    action_requests,
                    index=index,
                    call=call,
                    metadata={
                        **metadata,
                        "review_source": review_source,
                        "review_reason": reviewed["reason"],
                    },
                )

        if not review_calls:
            if not artificial_messages:
                return None
            return {"messages": [last_ai, *artificial_messages]}

        response = interrupt(
            {
                "kind": "tool_review",
                "batch_mode": "pause_before_all",
                "action_requests": action_requests,
            }
        )
        decisions = response.get("decisions") if isinstance(response, dict) else None
        if not isinstance(decisions, list) or len(decisions) != len(review_calls):
            raise ToolReviewStateError("tool review decision count does not match the batch")

        revised_calls = list(last_ai.tool_calls)
        for decision, (index, original_call, metadata) in zip(decisions, review_calls, strict=True):
            if not isinstance(decision, dict):
                raise ToolReviewStateError("tool review decision is not an object")
            await _verify_persisted_decision(
                store=store,
                run_id=run_id,
                operation_id=metadata["operation_id"],
                decision=decision,
            )
            decision_type = decision.get("type")
            if decision_type == "approve":
                continue
            if decision_type == "edit":
                edited = decision.get("edited_action")
                if not isinstance(edited, dict) or not isinstance(edited.get("args"), dict):
                    raise ToolReviewStateError("edited tool action must contain object args")
                if str(edited.get("name") or "") != original_call["name"]:
                    raise ToolReviewStateError("tool review cannot change the tool name")
                edited_args = edited["args"]
                edited_call_id = _edited_tool_call_id(original_call["id"], edited_args)
                edited_call = ToolCall(
                    type="tool_call",
                    id=edited_call_id,
                    name=original_call["name"],
                    args=edited_args,
                )
                await _cancel_replaced_receipt(
                    store=store,
                    run_id=run_id,
                    operation_id=metadata["operation_id"],
                )
                revised_calls[index] = edited_call
                validation_error = _tool_input_error(
                    tool_registry.get(original_call["name"]), edited_args
                )
                if validation_error is not None:
                    message = ToolMessage(
                        content=validation_error,
                        name=original_call["name"],
                        tool_call_id=edited_call_id,
                        status="error",
                    )
                    artificial_messages.append(message)
                    edited_operation_id, edited_arguments_hash, _ = tool_operation_identity(
                        run_id=run_id,
                        tool_call_id=edited_call_id,
                        tool_name=original_call["name"],
                        arguments=edited_args,
                        tool_version=metadata["tool_version"],
                        execution_namespace=execution_namespace,
                    )
                    await _record_preflight_failure(
                        store=store,
                        run_id=run_id,
                        execution_attempt_id=execution_attempt_id,
                        tool_version=metadata["tool_version"],
                        execution_namespace=execution_namespace,
                        call=edited_call,
                        operation_id=edited_operation_id,
                        arguments_hash=edited_arguments_hash,
                        risk=tool_risk(original_call["name"]),
                        message=message,
                    )
                    continue
                continue
            if decision_type != "reject":
                raise ToolReviewStateError("unsupported tool review decision")
            message = ToolMessage(
                content=str(decision.get("message") or "Tool execution denied by user."),
                name=original_call["name"],
                tool_call_id=original_call["id"],
                status="error",
            )
            artificial_messages.append(message)
            await _record_rejection(
                store=store,
                run_id=run_id,
                execution_attempt_id=execution_attempt_id,
                tool_version=metadata["tool_version"],
                execution_namespace=execution_namespace,
                call=original_call,
                metadata=metadata,
                message=message,
            )

        revised_ai = last_ai.model_copy(update={"tool_calls": revised_calls})
        return {"messages": [revised_ai, *artificial_messages]}


def _append_human_review(
    review_calls: list[tuple[int, ToolCall, dict[str, Any]]],
    action_requests: list[dict[str, Any]],
    *,
    index: int,
    call: ToolCall,
    metadata: dict[str, Any],
) -> None:
    review_calls.append((index, call, metadata))
    action_requests.append(
        {
            "name": call["name"],
            "args": call.get("args") or {},
            **metadata,
            "description": _review_description(
                call["name"], call.get("args") or {}, metadata["risk"]
            ),
            "allowed_decisions": ["approve", "edit", "reject"],
            "allow_run_scope": can_grant_for_run(
                tool_name=str(call["name"]),
                risk=str(metadata["risk"]),
            ),
        }
    )


async def _record_review_decision(
    *,
    store: LocalStore,
    run_id: str,
    receipt: dict[str, Any],
    decision: str,
    source: str,
    reason: str,
    model: str | None,
) -> None:
    await store.record_tool_review(
        operation_id=str(receipt["operation_id"]),
        run_id=run_id,
        decision=decision,
        source=source,
        reason=reason,
        model=model,
    )


def _edited_tool_call_id(original_call_id: str, edited_args: dict[str, Any]) -> str:
    rendered = json.dumps(
        edited_args,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:16]
    return f"{original_call_id}__edit_{digest}"


async def _cancel_replaced_receipt(
    *,
    store: LocalStore,
    run_id: str,
    operation_id: str,
) -> None:
    receipt = await store.get_tool_receipt(operation_id)
    if receipt is None:
        raise ToolReviewStateError("edited tool receipt disappeared before replacement")
    if receipt.get("status") == "canceled":
        return
    await store.settle_tool_receipt(
        operation_id=operation_id,
        run_id=run_id,
        status="canceled",
        error_type="ToolReviewEdited",
    )


def _emit_auto_approved(
    *,
    operation_id: str,
    tool_name: str,
    risk: str,
    source: str,
    reason: str,
) -> None:
    if risk not in {
        "workspace_write",
        "sandboxed_command",
        "plugin_action",
        "external_or_unknown",
    }:
        return
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    writer(
        {
            "event": "permission.auto_approved",
            "data": {
                "request_id": operation_id,
                "operation_id": operation_id,
                "tool": tool_name,
                "tool_name": tool_name,
                "risk": risk,
                "source": source,
                "reason": reason,
                "scope": "run",
            },
        }
    )


async def _verify_persisted_decision(
    *,
    store: LocalStore,
    run_id: str,
    operation_id: str,
    decision: dict[str, Any],
) -> None:
    record = await store.get_permission_for_operation(run_id=run_id, operation_id=operation_id)
    if record is None or record.get("status") == "pending":
        raise ToolReviewStateError("tool review was resumed without a resolved permission")
    expected = json.dumps(
        decision,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if str(record.get("decision_json") or "") != expected:
        raise PermissionDecisionConflictError(
            "tool review resume does not match the persisted decision"
        )


async def _record_rejection(
    *,
    store: LocalStore,
    run_id: str,
    execution_attempt_id: str,
    tool_version: str,
    execution_namespace: str,
    call: ToolCall,
    metadata: dict[str, Any],
    message: ToolMessage,
) -> None:
    _operation_id, _arguments_hash, arguments_json = tool_operation_identity(
        run_id=run_id,
        tool_call_id=call["id"],
        tool_name=call["name"],
        arguments=call.get("args") or {},
        tool_version=tool_version,
        execution_namespace=execution_namespace,
    )
    receipt = await store.prepare_tool_receipt(
        operation_id=metadata["operation_id"],
        run_id=run_id,
        execution_attempt_id=execution_attempt_id,
        tool_call_id=call["id"],
        tool_name=call["name"],
        tool_version=tool_version,
        execution_namespace=execution_namespace,
        arguments_hash=metadata["arguments_hash"],
        arguments_json=arguments_json,
        risk=metadata["risk"],
    )
    if receipt.get("status") == "rejected":
        return
    result_json = serialize_tool_result(message)
    await store.settle_tool_receipt(
        operation_id=metadata["operation_id"],
        run_id=run_id,
        status="rejected",
        result_json=result_json,
        result_hash=hashlib.sha256(result_json.encode("utf-8")).hexdigest(),
    )


async def _record_preflight_failure(
    *,
    store: LocalStore,
    run_id: str,
    execution_attempt_id: str,
    tool_version: str,
    execution_namespace: str,
    call: ToolCall,
    operation_id: str,
    arguments_hash: str,
    risk: str,
    message: ToolMessage,
) -> None:
    _operation_id, _arguments_hash, arguments_json = tool_operation_identity(
        run_id=run_id,
        tool_call_id=call["id"],
        tool_name=call["name"],
        arguments=call.get("args") or {},
        tool_version=tool_version,
        execution_namespace=execution_namespace,
    )
    receipt = await store.prepare_tool_receipt(
        operation_id=operation_id,
        run_id=run_id,
        execution_attempt_id=execution_attempt_id,
        tool_call_id=call["id"],
        tool_name=call["name"],
        tool_version=tool_version,
        execution_namespace=execution_namespace,
        arguments_hash=arguments_hash,
        arguments_json=arguments_json,
        risk=risk,
    )
    if receipt.get("status") == "failed":
        return
    result_json = serialize_tool_result(message)
    await store.settle_tool_receipt(
        operation_id=operation_id,
        run_id=run_id,
        status="failed",
        result_json=result_json,
        result_hash=hashlib.sha256(result_json.encode("utf-8")).hexdigest(),
        error_type="ToolInputValidationError",
    )


def _tool_input_error(tool: Any, arguments: Any) -> str | None:
    if tool is None:
        return "Tool call rejected: the tool is not available in this Runtime definition."
    if not isinstance(arguments, dict):
        return "Tool call rejected: arguments must be an object."
    try:
        # tool_call_schema excludes injected ToolRuntime/store parameters;
        # get_input_schema includes them and would falsely reject every
        # Deep Agents filesystem tool for a missing `runtime` argument.
        validate_tool_input(tool, arguments)
    except Exception as exc:
        return f"Tool call rejected before execution: {type(exc).__name__}: {exc}"
    return None


def _review_description(tool_name: str, arguments: Any, risk: str) -> str:
    rendered = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    return f"Review {risk} tool `{tool_name}` with arguments: {rendered}"
