"""Tests for the observability layer (RuntimeObserver + structlog config)."""

from __future__ import annotations

import asyncio
import os
from typing import Any
from uuid import uuid4

import pytest
import structlog
from structlog.testing import capture_logs

from shejane_runtime.diagnostics_trace import build_run_trace
from shejane_runtime.observability import (
    RuntimeObserver,
    build_callbacks,
    configure_logging,
    is_disabled,
)


@pytest.fixture(autouse=True)
def reset_structlog_state():
    """Each test gets a fresh structlog config (the module caches setup)."""
    import shejane_runtime.observability as obs

    obs._configured = False
    structlog.reset_defaults()
    yield
    obs._configured = False


def test_configure_logging_is_idempotent() -> None:
    configure_logging(json_output=True)
    configure_logging(json_output=True)  # second call should be safe
    log = structlog.get_logger("test")
    log.info("hello")  # must not raise


def test_pytest_disables_external_langsmith_tracing_by_default() -> None:
    assert os.environ.get("LANGSMITH_TRACING") == "false"
    assert os.environ.get("LANGCHAIN_TRACING_V2") == "false"
    assert "LANGSMITH_API_KEY" not in os.environ
    assert "LANGCHAIN_API_KEY" not in os.environ


def test_is_disabled_respects_env(monkeypatch: Any) -> None:
    monkeypatch.delenv("SHEJANE_DISABLE_OBSERVABILITY", raising=False)
    assert is_disabled() is False
    monkeypatch.setenv("SHEJANE_DISABLE_OBSERVABILITY", "1")
    assert is_disabled() is True
    monkeypatch.setenv("SHEJANE_DISABLE_OBSERVABILITY", "true")
    assert is_disabled() is True
    monkeypatch.setenv("SHEJANE_DISABLE_OBSERVABILITY", "0")
    assert is_disabled() is False


def test_build_callbacks_returns_observer_by_default(monkeypatch: Any) -> None:
    monkeypatch.delenv("SHEJANE_DISABLE_OBSERVABILITY", raising=False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    callbacks = build_callbacks()
    assert len(callbacks) == 1
    assert isinstance(callbacks[0], RuntimeObserver)


def test_build_callbacks_empty_when_disabled(monkeypatch: Any) -> None:
    monkeypatch.setenv("SHEJANE_DISABLE_OBSERVABILITY", "1")
    assert build_callbacks() == []


def test_build_callbacks_disables_inherited_external_tracing(monkeypatch: Any) -> None:
    monkeypatch.delenv("SHEJANE_DISABLE_OBSERVABILITY", raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "must-not-be-used")
    callbacks = build_callbacks()
    assert len(callbacks) == 1
    assert isinstance(callbacks[0], RuntimeObserver)
    assert os.environ["LANGSMITH_TRACING"] == "false"
    assert "LANGSMITH_API_KEY" not in os.environ


# --- RuntimeObserver event capture ---


def test_observer_logs_tool_start_and_end() -> None:
    obs = RuntimeObserver()
    run_id = uuid4()

    async def run() -> None:
        await obs.on_tool_start(
            {"name": "fs.read"},
            "{'path': '/tmp/x'}",
            run_id=run_id,
        )
        await obs.on_tool_end("file content here", run_id=run_id)

    with capture_logs() as captured:
        asyncio.run(run())

    events = [e["event"] for e in captured]
    assert "tool.start" in events
    assert "tool.end" in events

    end_event = next(e for e in captured if e["event"] == "tool.end")
    assert end_event["elapsed_ms"] is not None
    assert end_event["elapsed_ms"] >= 0


def test_observer_logs_tool_error_clears_timer() -> None:
    obs = RuntimeObserver()
    run_id = uuid4()

    async def run() -> None:
        await obs.on_tool_start({"name": "shell.run"}, "ls /nope", run_id=run_id)
        await obs.on_tool_error(RuntimeError("file not found"), run_id=run_id)

    with capture_logs() as captured:
        asyncio.run(run())

    err_event = next(e for e in captured if e["event"] == "tool.error")
    assert err_event["error_type"] == "RuntimeError"
    # Subsequent end events should not show negative elapsed (timer was cleared)
    assert run_id not in obs._timers  # type: ignore[attr-defined]


def test_observer_logs_llm_lifecycle() -> None:
    from langchain_core.outputs import Generation, LLMResult

    obs = RuntimeObserver()
    run_id = uuid4()

    async def run() -> None:
        await obs.on_chat_model_start(
            {"name": "test-model"},
            [["msg1", "msg2"]],
            run_id=run_id,
        )
        result = LLMResult(
            generations=[[Generation(text="answer")]],
            llm_output={"token_usage": {"input_tokens": 12, "output_tokens": 5}},
        )
        await obs.on_llm_end(result, run_id=run_id)

    with capture_logs() as captured:
        asyncio.run(run())

    events_by_name = {e["event"]: e for e in captured}
    assert "llm.start" in events_by_name
    assert "llm.end" in events_by_name

    end = events_by_name["llm.end"]
    assert end["input_tokens"] == 12
    assert end["output_tokens"] == 5
    assert end["elapsed_ms"] is not None


def test_observer_never_logs_model_or_tool_content() -> None:
    obs = RuntimeObserver()
    run_id = uuid4()
    secret = "private prompt, tool result, or credential"

    async def run() -> None:
        await obs.on_tool_start({"name": "noisy"}, secret, run_id=run_id)
        await obs.on_tool_end(secret, run_id=run_id)
        await obs.on_tool_error(RuntimeError(secret), run_id=run_id)
        await obs.on_agent_finish({"output": secret}, run_id=run_id)

    with capture_logs() as captured:
        asyncio.run(run())

    assert secret not in str(captured)
    assert not any("input_preview" in event or "output_preview" in event for event in captured)


def test_durable_trace_links_redacted_model_tool_checkpoint_and_terminal_spans() -> None:
    trace = build_run_trace(
        {
            "id": "run-1",
            "status": "completed",
            "created_at": "2026-07-26T00:00:00+00:00",
            "updated_at": "2026-07-26T00:00:03+00:00",
            "completed_at": "2026-07-26T00:00:03+00:00",
        },
        model_calls=[
            {
                "id": "model-1",
                "execution_attempt_id": "attempt-1",
                "call_index": 1,
                "model": "local:connection:model",
                "purpose": "agent",
                "status": "completed",
                "output_started": 1,
                "input_tokens": 10,
                "output_tokens": 5,
                "created_at": "2026-07-26T00:00:00.500000+00:00",
                "completed_at": "2026-07-26T00:00:01+00:00",
            }
        ],
        tool_receipts=[
            {
                "operation_id": "operation-1",
                "execution_attempt_id": "attempt-1",
                "tool_call_id": "call-1",
                "tool_name": "read_file",
                "tool_version": "1",
                "arguments_hash": "args-hash",
                "arguments_json": '{"secret":"must-not-appear"}',
                "risk": "workspace_read",
                "status": "completed",
                "attempt_count": 1,
                "result_hash": "result-hash",
                "result_json": '"private output"',
                "created_at": "2026-07-26T00:00:01.100000+00:00",
                "started_at": "2026-07-26T00:00:01.200000+00:00",
                "completed_at": "2026-07-26T00:00:02+00:00",
            }
        ],
        child_runs=[],
        checkpoint={
            "id": "checkpoint-1",
            "step": 2,
            "reason": "loop",
            "messages_count": 3,
            "created_at": "2026-07-26T00:00:02.500000+00:00",
        },
        event_count=8,
    )

    spans = {span["id"]: span for span in trace["spans"]}
    assert spans["span:tool:operation-1"]["parent_id"] == "span:model:model-1"
    assert spans["span:checkpoint:checkpoint-1"]["parent_id"] == trace["root_span_id"]
    assert spans["span:terminal:run-1"]["status"] == "completed"
    assert "arguments_json" not in str(trace)
    assert "private output" not in str(trace)


def test_durable_trace_uses_receipt_lineage_for_subagent_model_and_tools() -> None:
    trace = build_run_trace(
        {
            "id": "run-lineage",
            "status": "failed",
            "created_at": "2026-08-06T00:00:00+00:00",
            "updated_at": "2026-08-06T00:00:04+00:00",
            "completed_at": "2026-08-06T00:00:04+00:00",
        },
        model_calls=[
            {
                "id": "model-main",
                "logical_call_id": "model-main",
                "retry_attempt": 0,
                "execution_attempt_id": "attempt-1",
                "call_index": 1,
                "model": "local:connection:model",
                "purpose": "agent",
                "status": "completed",
                "output_started": 1,
                "created_at": "2026-08-06T00:00:00.100000+00:00",
                "completed_at": "2026-08-06T00:00:01+00:00",
            },
            {
                "id": "model-child",
                "logical_call_id": "model-child",
                "retry_attempt": 0,
                "execution_attempt_id": "attempt-1",
                "parent_tool_operation_id": "operation-task",
                "call_index": 2,
                "model": "local:connection:model",
                "purpose": "agent",
                "status": "failed",
                "output_started": 0,
                "error_code": "service_unavailable",
                "created_at": "2026-08-06T00:00:02+00:00",
                "completed_at": "2026-08-06T00:00:03+00:00",
            },
        ],
        tool_receipts=[
            {
                "operation_id": "operation-task",
                "execution_attempt_id": "attempt-1",
                "execution_namespace": "main",
                "tool_call_id": "call-task",
                "tool_name": "task",
                "status": "failed",
                "attempt_count": 1,
                "created_at": "2026-08-06T00:00:01.100000+00:00",
                "started_at": "2026-08-06T00:00:01.200000+00:00",
                "completed_at": "2026-08-06T00:00:03.100000+00:00",
            },
            {
                "operation_id": "operation-child-tool",
                "parent_operation_id": "operation-task",
                "execution_attempt_id": "attempt-1",
                "execution_namespace": "child:researcher",
                "tool_call_id": "call-child-tool",
                "tool_name": "execute",
                "status": "canceled",
                "attempt_count": 0,
                "created_at": "2026-08-06T00:00:02.500000+00:00",
                "completed_at": "2026-08-06T00:00:03.100000+00:00",
            },
        ],
        child_runs=[],
        checkpoint=None,
        event_count=4,
    )

    spans = {span["id"]: span for span in trace["spans"]}
    assert spans["span:tool:operation-task"]["parent_id"] == "span:model:model-main"
    assert spans["span:model:model-child"]["parent_id"] == "span:tool:operation-task"
    assert spans["span:tool:operation-child-tool"]["parent_id"] == ("span:tool:operation-task")
    assert spans["span:model:model-child"]["attributes"]["error_code"] == ("service_unavailable")
