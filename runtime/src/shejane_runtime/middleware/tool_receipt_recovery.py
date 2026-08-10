"""Durable tool receipt replay, cancellation, and reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.types import Command

from ..store.sqlite import LocalStore, ToolOutcomeUnknownError, ToolReceiptStateError
from .tool_result_codec import _deserialize_tool_result, serialize_tool_result


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


async def _cancel_before_tool_start(store: LocalStore, run_id: str, operation_id: str) -> None:
    if not await store.tool_execution_cancel_requested(run_id):
        return
    receipt = await store.get_tool_receipt(operation_id)
    if receipt is not None and receipt.get("status") in {"prepared", "paused"}:
        settle = (
            store.settle_task_receipt
            if str(receipt.get("tool_name") or "") == "task"
            else store.settle_tool_receipt
        )
        await settle(
            operation_id=operation_id,
            run_id=run_id,
            status="canceled",
            error_type="RunCanceledBeforeToolStart",
        )
    raise asyncio.CancelledError
