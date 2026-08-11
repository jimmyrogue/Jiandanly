from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from langchain_core.messages import ToolMessage

from .middleware.tool_execution import serialize_tool_result
from .store.sqlite import LocalStore, WaitDecisionConflictError


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    if isinstance(payload, dict):
        return payload
    payload_json = event.get("payload_json")
    if isinstance(payload_json, str):
        try:
            parsed = json.loads(payload_json)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


async def _owned_run(
    store: LocalStore,
    *,
    principal_id: str,
    run_id: str,
    not_found_detail: str = "run not found",
) -> dict[str, Any]:
    run = await store.get_run_for_principal(principal_id=principal_id, run_id=run_id)
    if run is None:
        detail: str | dict[str, str] = not_found_detail
        if not_found_detail == "run not found":
            detail = {"code": "run_not_found", "message": not_found_detail}
        raise HTTPException(status_code=404, detail=detail)
    return run


async def _run_with_inputs(store: LocalStore, run: dict[str, Any]) -> dict[str, Any]:
    return (await _runs_with_inputs(store, [run]))[0]


async def _runs_with_inputs(store: LocalStore, runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    run_ids = [str(run["id"]) for run in runs]
    missing_subagent_ids = [str(run["id"]) for run in runs if "subagent_invocations" not in run]
    rows, subagent_rows, child_rows, model_calls = await asyncio.gather(
        store.list_run_inputs_for_runs(run_ids),
        store.list_subagent_invocations_for_runs(missing_subagent_ids),
        store.list_child_runs_for_runs(run_ids),
        store.latest_model_calls_for_runs(run_ids),
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["run_id"]), []).append(row)
    grouped_subagents: dict[str, list[dict[str, Any]]] = {}
    for row in subagent_rows:
        grouped_subagents.setdefault(str(row["parent_run_id"]), []).append(row)
    grouped_children: dict[str, list[dict[str, Any]]] = {}
    for row in child_rows:
        grouped_children.setdefault(str(row["parent_run_id"]), []).append(row)
    latest_model_call = {str(row["run_id"]): row for row in model_calls}
    return [
        {
            **run,
            "reasoning_mode": _run_reasoning_mode(run),
            **_run_model_phase(run, latest_model_call.get(str(run["id"]))),
            "inputs": [
                {
                    "client_index": index,
                    **{
                        key: item[key]
                        for key in (
                            "input_id",
                            "virtual_path",
                            "original_name",
                            "media_type",
                            "bytes",
                            "sha256",
                        )
                    },
                }
                for index, item in enumerate(grouped.get(str(run["id"]), []))
            ],
            "subagent_invocations": run.get(
                "subagent_invocations",
                grouped_subagents.get(str(run["id"]), []),
            ),
            "child_runs": run.get(
                "child_runs",
                grouped_children.get(str(run["id"]), []),
            ),
        }
        for run in runs
    ]


def _run_reasoning_mode(run: dict[str, Any]) -> str:
    try:
        settings = json.loads(str(run.get("settings_json") or "{}"))
    except (json.JSONDecodeError, TypeError):
        return "off"
    mode = settings.get("reasoning_mode") if isinstance(settings, dict) else None
    return str(mode) if mode in {"off", "high", "max"} else "off"


def _run_model_phase(
    run: dict[str, Any],
    model_call: dict[str, Any] | None,
) -> dict[str, str | None]:
    if model_call is not None:
        phase = str(model_call.get("phase") or "")
        if phase in {"waiting_provider", "reasoning", "answering", "tool_calling", "completed"}:
            return {
                "model_phase": phase,
                "model_phase_started_at": str(
                    model_call.get("phase_started_at") or model_call.get("created_at") or ""
                ) or None,
            }
    if run.get("status") in {"queued", "running"}:
        return {
            "model_phase": "waiting_provider",
            "model_phase_started_at": str(run.get("updated_at") or run.get("created_at") or "")
            or None,
        }
    return {"model_phase": None, "model_phase_started_at": None}


async def _normalized_path(raw: str) -> str:
    return await asyncio.to_thread(
        lambda: str(Path(os.path.abspath(os.path.expanduser(raw))).resolve())
    )


async def _authorized_workspace_path(
    store: LocalStore, *, principal_id: str, path: str | None
) -> str | None:
    if path is None:
        return None
    resolved = await _normalized_path(path)
    workspace = await store.workspace_by_path(principal_id=principal_id, path=resolved)
    if workspace is None:
        raise HTTPException(status_code=403, detail="workspace is not authorized")
    workspace_error = await store.workspace_admission_error(
        principal_id=principal_id,
        path=resolved,
    )
    if workspace_error is not None:
        raise HTTPException(status_code=409, detail=workspace_error)
    return resolved


async def _tool_reconciliation_results(
    store: LocalStore,
    *,
    operation_id: str,
    decision: str,
) -> dict[str, str | None]:
    record = await store.get_wait_candidate(operation_id)
    if record is None or record.get("kind") != "tool_reconciliation":
        raise KeyError(operation_id)
    payload = _json_object(record.get("payload_json"))
    current_receipt = await store.get_tool_receipt(operation_id)
    prior_operation_id = str(payload.get("prior_operation_id") or operation_id)
    prior_receipt = await store.get_tool_receipt(prior_operation_id)
    if current_receipt is None or prior_receipt is None:
        raise WaitDecisionConflictError("tool reconciliation receipt is missing")
    current_result = (
        _tool_reconciliation_result(current_receipt, decision)
        if decision != "retry_not_executed"
        else None
    )
    prior_result = _tool_reconciliation_result(
        prior_receipt,
        "abort" if decision == "retry_not_executed" else decision,
    )
    return {
        "current_result_json": current_result,
        "current_result_hash": (
            hashlib.sha256(current_result.encode()).hexdigest()
            if current_result is not None
            else None
        ),
        "prior_result_json": prior_result,
        "prior_result_hash": hashlib.sha256(prior_result.encode()).hexdigest(),
    }


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tool_reconciliation_result(receipt: dict[str, Any], decision: str) -> str:
    completed = decision == "confirmed_completed"
    return serialize_tool_result(
        ToolMessage(
            content=(
                "The user verified that the external action completed successfully."
                if completed
                else "The user verified that this uncertain action must not be retried automatically."
            ),
            name=str(receipt.get("tool_name") or ""),
            tool_call_id=str(receipt.get("tool_call_id") or ""),
            status="success" if completed else "error",
        )
    )
