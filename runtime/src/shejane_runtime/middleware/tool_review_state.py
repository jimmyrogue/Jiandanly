"""Durable tool-review receipt and resume-state operations."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from langchain_core.messages import ToolCall, ToolMessage

from ..store.sqlite import LocalStore, PermissionDecisionConflictError
from ..tools.runtime import current_runtime_tool_execution_or_none
from .tool_execution import serialize_tool_result, tool_operation_identity


class ToolReviewStateError(RuntimeError):
    """A review resume does not match its persisted wait candidate."""

    code = "tool_review_state_invalid"
    retryable = False


def _parent_operation_id() -> str | None:
    execution = current_runtime_tool_execution_or_none()
    return execution.operation_id if execution is not None else None


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
        parent_operation_id=_parent_operation_id(),
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
        parent_operation_id=_parent_operation_id(),
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
