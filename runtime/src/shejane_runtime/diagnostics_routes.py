from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request

from . import __version__
from .api_schemas import LocalRunDiagnostics
from .diagnostics_trace import build_run_trace
from .http_route_helpers import _owned_run, _run_with_inputs
from .runs.diagnostics_projection import (
    build_diagnostics_handoff as _build_diagnostics_handoff,
)
from .runs.diagnostics_projection import diagnostics_build as _diagnostics_build
from .runs.diagnostics_projection import (
    diagnostics_execution_policy as _diagnostics_execution_policy,
)
from .runs.diagnostics_projection import (
    latest_checkpoint_reflection as _latest_checkpoint_reflection,
)
from .runs.diagnostics_projection import (
    latest_checkpoint_summary as _latest_checkpoint_summary,
)
from .runs.diagnostics_projection import latest_feature_ledger as _latest_feature_ledger
from .store.sqlite import LocalStore

diagnostics_router = APIRouter()


@diagnostics_router.get("/v1/runs/{run_id}/diagnostics", response_model=LocalRunDiagnostics)
async def run_diagnostics(request: Request, run_id: str) -> dict[str, Any]:
    """Return the complete, redacted durable diagnostics projection."""
    store: LocalStore = request.app.state.store
    run = await _owned_run(
        store,
        principal_id=request.state.principal_id,
        run_id=run_id,
    )
    raw_events = await store.events_since(run_id, after_seq=0)
    events = [
        {
            "id": event["id"],
            "run_id": event["run_id"],
            "seq": event["seq"],
            "event_type": event["event_type"],
            "payload": json.loads(event.get("payload_json") or "{}"),
            "created_at": event["created_at"],
        }
        for event in raw_events
    ]
    permissions = await store.list_permissions_for_run(run_id)
    tool_receipts = await store.list_tool_receipts_for_run(run_id)
    model_calls = await store.list_model_calls_for_run(run_id)
    child_runs = await store.list_child_runs_for_run(run_id)
    wait_candidates = await store.list_wait_candidates_for_run(run_id)
    artifacts = await store.list_artifacts_for_run(run_id)
    latest_checkpoint = await _latest_checkpoint_summary(request.app.state.checkpointer, run)
    reflection = await _latest_checkpoint_reflection(request.app.state.checkpointer, run)
    return {
        "schema_version": 2,
        "exported_at": datetime.now(UTC).isoformat(),
        "runtime_version": __version__,
        "build": _diagnostics_build(run),
        "execution_policy": _diagnostics_execution_policy(run),
        "run": await _run_with_inputs(store, run),
        "events": events,
        "permissions": permissions,
        "model_calls": [
            {
                "id": str(call["id"]),
                "logical_call_id": str(call.get("logical_call_id") or call["id"]),
                "retry_attempt": int(call.get("retry_attempt") or 0),
                "execution_attempt_id": str(call["execution_attempt_id"]),
                "parent_tool_operation_id": call.get("parent_tool_operation_id"),
                "call_index": int(call["call_index"]),
                "model": str(call["model"]),
                "purpose": str(call.get("purpose") or "agent"),
                "status": str(call["status"]),
                "output_started": bool(call.get("output_started")),
                "outcome_unknown": call.get("status") == "outcome_unknown",
                "provider_request_id": call.get("provider_request_id"),
                "input_tokens": call.get("input_tokens"),
                "output_tokens": call.get("output_tokens"),
                "reasoning_tokens": call.get("reasoning_tokens"),
                "phase": str(call.get("phase") or "waiting_provider"),
                "error_code": call.get("error_code"),
                "created_at": str(call["created_at"]),
                "request_started_at": str(
                    call.get("request_started_at") or call["created_at"]
                ),
                "response_headers_at": call.get("response_headers_at"),
                "first_raw_chunk_at": call.get("first_raw_chunk_at"),
                "reasoning_started_at": call.get("reasoning_started_at"),
                "first_visible_output_at": call.get("first_visible_output_at"),
                "phase_started_at": str(
                    call.get("phase_started_at") or call["created_at"]
                ),
                "first_output_at": call.get("first_output_at"),
                "completed_at": call.get("completed_at"),
            }
            for call in model_calls
        ],
        "tool_receipts": [
            {
                key: receipt.get(key)
                for key in (
                    "operation_id",
                    "execution_namespace",
                    "parent_operation_id",
                    "tool_call_id",
                    "tool_name",
                    "tool_version",
                    "arguments_hash",
                    "risk",
                    "status",
                    "attempt_count",
                    "result_hash",
                    "error_type",
                    "review_decision",
                    "review_source",
                    "created_at",
                    "started_at",
                    "completed_at",
                    "updated_at",
                )
            }
            | {
                "review_reason_hash": (
                    hashlib.sha256(str(receipt["review_reason"]).encode()).hexdigest()
                    if receipt.get("review_reason")
                    else None
                )
            }
            for receipt in tool_receipts
        ],
        "wait_candidates": [
            {
                key: candidate.get(key)
                for key in ("id", "kind", "status", "created_at", "resolved_at")
            }
            for candidate in wait_candidates
        ],
        "artifacts": artifacts,
        "latest_checkpoint": latest_checkpoint,
        "handoff": _build_diagnostics_handoff(run, events, permissions, artifacts),
        "feature_ledger": _latest_feature_ledger(artifacts),
        "reflection": reflection,
        "trace": build_run_trace(
            run,
            model_calls=model_calls,
            tool_receipts=tool_receipts,
            child_runs=child_runs,
            checkpoint=latest_checkpoint,
            event_count=len(events),
        ),
    }
