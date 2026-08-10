from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import AIMessage, ToolCall, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.types import Command
from pydantic import BaseModel

from shejane_runtime.agent.context_builder import AsyncToolExecutionGate, RuntimeContext
from shejane_runtime.auth import LOCAL_OWNER_PRINCIPAL_ID
from shejane_runtime.middleware.tool_execution import (
    MAX_MODEL_TOOL_RESULT_BYTES,
    ToolExecutionMiddleware,
    _batch_order_key,
    canonical_tool_execution_scope,
    execution_namespace_from_config,
    execution_scope_from_messages,
    serialize_tool_result,
    tool_operation_identity,
)
from shejane_runtime.middleware.tool_review import (
    ToolReviewMiddleware,
    ToolReviewStateError,
    _tool_input_error,
)
from shejane_runtime.store.sqlite import (
    ArtifactQuotaError,
    LocalStore,
    ToolReceiptConflictError,
    ToolReceiptStateError,
    WaitDecisionConflictError,
)
from shejane_runtime.tools.runtime import RuntimeToolExecution, bind_runtime_tool_execution


async def _store_and_run(tmp_path: Path) -> tuple[LocalStore, dict[str, object]]:
    store = await LocalStore.open(tmp_path / "runtime.db")
    run = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="test tools",
        workspace_path=None,
    )
    return store, run


@pytest.mark.asyncio
async def test_tool_review_rejects_duplicate_call_ids_before_execution() -> None:
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "call-1", "name": "read_file", "args": {"path": "a"}},
                    {"id": "call-1", "name": "read_file", "args": {"path": "b"}},
                ],
            )
        ]
    }
    with pytest.raises(ToolReviewStateError, match="unique"):
        await ToolReviewMiddleware().aafter_model(
            state,
            SimpleNamespace(context=None),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_tool_review_skips_calls_already_blocked_by_an_earlier_middleware(
    tmp_path: Path,
) -> None:
    store, run = await _store_and_run(tmp_path)
    calls = [
        ToolCall(
            type="tool_call", id=f"task-{index}", name="task", args={"description": str(index)}
        )
        for index in range(3)
    ]
    context = RuntimeContext(
        store=store,
        run_id=str(run["id"]),
        execution_attempt_id="job-limit:1",
        graph_definition_id="graph-v1",
        tool_registry={
            "task": SimpleNamespace(
                tool_call_schema={
                    "type": "object",
                    "properties": {"description": {"type": "string"}},
                    "required": ["description"],
                    "additionalProperties": False,
                }
            )
        },
    )
    state = {
        "messages": [
            AIMessage(content="", tool_calls=calls),
            ToolMessage(
                content="Tool call limit exceeded.",
                name="task",
                tool_call_id="task-1",
                status="error",
            ),
            ToolMessage(
                content="Tool call limit exceeded.",
                name="task",
                tool_call_id="task-2",
                status="error",
            ),
        ]
    }
    try:
        assert (
            await ToolReviewMiddleware().aafter_model(
                state,
                SimpleNamespace(context=context),  # type: ignore[arg-type]
            )
            is None
        )

        receipts = await store.list_tool_receipts_for_run(str(run["id"]))
        assert [(receipt["tool_call_id"], receipt["status"]) for receipt in receipts] == [
            ("task-0", "prepared")
        ]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_nested_tool_review_preserves_parent_operation_identity(tmp_path: Path) -> None:
    store, run = await _store_and_run(tmp_path)
    run_id = str(run["id"])
    await store.prepare_tool_receipt(
        operation_id="toolop_parent_task",
        run_id=run_id,
        execution_attempt_id="job-parent:1",
        execution_namespace="main",
        tool_call_id="call-parent-task",
        tool_name="task",
        tool_version="graph-v1",
        arguments_hash="parent-args",
        arguments_json='{"subagent_type":"researcher","description":"research"}',
        risk="control_flow",
    )
    context = RuntimeContext(
        store=store,
        run_id=run_id,
        execution_attempt_id="job-parent:1",
        graph_definition_id="graph-v1",
        tool_registry={
            "time.now": SimpleNamespace(
                tool_call_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                }
            )
        },
    )
    state = {
        "messages": [
            AIMessage(
                id="nested-batch",
                content="",
                tool_calls=[
                    ToolCall(type="tool_call", id="call-child-tool", name="time.now", args={})
                ],
            )
        ]
    }
    try:
        with bind_runtime_tool_execution(
            RuntimeToolExecution(
                context=context,
                operation_id="toolop_parent_task",
                tool_call_id="call-parent-task",
            )
        ):
            assert (
                await ToolReviewMiddleware().aafter_model(
                    state,
                    SimpleNamespace(context=context),  # type: ignore[arg-type]
                )
                is None
            )

        receipts = await store.list_tool_receipts_for_run(run_id)
        child = next(
            receipt for receipt in receipts if receipt["tool_call_id"] == "call-child-tool"
        )
        assert child["parent_operation_id"] == "toolop_parent_task"
    finally:
        await store.close()


def test_tool_review_validates_json_schema_tools() -> None:
    tool = SimpleNamespace(
        tool_call_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        }
    )

    assert _tool_input_error(tool, {"value": "ping"}) is None
    assert "ValidationError" in str(_tool_input_error(tool, {}))


def test_tool_review_rejects_unknown_fields_for_pydantic_tool_schemas() -> None:
    class ToolArgs(BaseModel):
        value: str

    tool = SimpleNamespace(tool_call_schema=ToolArgs)

    assert _tool_input_error(tool, {"value": "ping"}) is None
    assert "Additional properties are not allowed" in str(
        _tool_input_error(tool, {"value": "ping", "unexpected": True})
    )


@pytest.mark.asyncio
async def test_ordering_gate_does_not_advance_after_interrupted_position() -> None:
    gate = AsyncToolExecutionGate()
    with pytest.raises(RuntimeError, match="interrupted"):
        async with gate.ordered("batch", 0):
            raise RuntimeError("interrupted")
    assert gate._batch_next.get("batch", 0) == 0


def test_batch_order_key_shares_siblings_but_isolates_subagents() -> None:
    assert _batch_order_key("tools:a|batch_hash") == _batch_order_key("tools:b|batch_hash")
    assert _batch_order_key("tools:parent-a|agent:x|tools:a|batch_hash") != (
        _batch_order_key("tools:parent-b|agent:x|tools:b|batch_hash")
    )


def test_canonical_tool_scope_matches_review_and_execution_nodes() -> None:
    review = "agent:parent|ToolReviewMiddleware.after_model:review|batch_hash"
    execution = "agent:parent|tools:execute|batch_hash"

    assert canonical_tool_execution_scope(review) == canonical_tool_execution_scope(execution)


def test_long_batch_namespaces_preserve_parent_identity() -> None:
    def scope(parent: str, sibling: str) -> str:
        namespace = execution_namespace_from_config(
            {"configurable": {"checkpoint_ns": f"agent:{parent * 300}|tools:{sibling}"}}
        )
        return f"{namespace}|batch_hash"

    assert _batch_order_key(scope("a", "one")) == _batch_order_key(scope("a", "two"))
    assert _batch_order_key(scope("a", "one")) != _batch_order_key(scope("b", "one"))


@pytest.mark.asyncio
async def test_legacy_p10_tables_migrate_before_new_indexes(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE local_permissions (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, tool_call_id TEXT,
            tool_name TEXT NOT NULL, arguments_json TEXT NOT NULL,
            status TEXT NOT NULL, scope TEXT NOT NULL DEFAULT 'once',
            created_at TEXT NOT NULL, resolved_at TEXT
        );
        CREATE TABLE local_wait_candidates (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL,
            status TEXT NOT NULL, payload_json TEXT NOT NULL,
            decision_json TEXT, created_at TEXT NOT NULL, resolved_at TEXT
        );
        CREATE TABLE local_questions (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, tool_call_id TEXT,
            questions_json TEXT NOT NULL, status TEXT NOT NULL,
            answers_json TEXT, created_at TEXT NOT NULL, answered_at TEXT
        );
        INSERT INTO local_questions
            (id, run_id, tool_call_id, questions_json, status, answers_json, created_at, answered_at)
        VALUES ('legacy-question', 'legacy-run', 'legacy-call', '[]', 'pending', NULL, '2026-01-01', NULL);
        INSERT INTO local_permissions
            (id, run_id, tool_call_id, tool_name, arguments_json, status, scope, created_at)
        VALUES ('legacy-permission', 'legacy-run', 'legacy-call', 'execute', '{}', 'pending', 'once', '2026-01-01');
        CREATE TABLE local_plan_approvals (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
            todos_json TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending', instructions TEXT,
            created_at TEXT NOT NULL, resolved_at TEXT,
            UNIQUE (run_id, tool_call_id)
        );
        INSERT INTO local_plan_approvals
            (id, run_id, tool_call_id, todos_json, status, created_at)
        VALUES ('legacy-plan', 'legacy-run', 'legacy-plan-call', '[]', 'pending', '2026-01-01');
        INSERT INTO local_plan_approvals
            (id, run_id, tool_call_id, todos_json, status, instructions, created_at, resolved_at)
        VALUES ('legacy-plan-resolved', 'legacy-run', 'legacy-plan-resolved-call', '[]',
            'modified', 'Add verification.', '2026-01-01', '2026-01-02');
        INSERT INTO local_plan_approvals
            (id, run_id, tool_call_id, todos_json, status, created_at)
        VALUES ('legacy-plan-orphan', 'legacy-run', 'legacy-orphan-call', '[]',
            'pending', '2026-01-01');
        CREATE TABLE local_events (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, seq INTEGER NOT NULL,
            event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        INSERT INTO local_events VALUES
            ('event-plan-required', 'legacy-run', 1, 'plan.approval_required',
             '{"request_id":"legacy-plan","tool_call_id":"legacy-plan-call"}', '2026-01-01'),
            ('event-plan-waiting', 'legacy-run', 2, 'run.waiting',
             '{"wait_cycle_id":"wait-legacy-plan","interrupts":[{"id":"interrupt-legacy-plan","value":{"kind":"plan_approval","tool_call_id":"legacy-plan-call"}}]}', '2026-01-01'),
            ('event-resolved-required', 'legacy-run', 3, 'plan.approval_required',
             '{"request_id":"legacy-plan-resolved","tool_call_id":"legacy-plan-resolved-call"}', '2026-01-01'),
            ('event-resolved-waiting', 'legacy-run', 4, 'run.waiting',
             '{"wait_cycle_id":"wait-legacy-resolved","interrupts":[{"id":"interrupt-legacy-resolved","value":{"kind":"plan_approval","tool_call_id":"legacy-plan-resolved-call"}}]}', '2026-01-01'),
            ('event-orphan-required', 'legacy-run', 5, 'plan.approval_required',
             '{"request_id":"legacy-plan-orphan","tool_call_id":"legacy-orphan-call"}', '2026-01-01'),
            ('event-unrelated-waiting', 'legacy-run', 6, 'run.waiting',
             '{"wait_cycle_id":"wait-unrelated","interrupts":[{"id":"interrupt-unrelated","value":{"kind":"plan_approval","tool_call_id":"another-plan-call"}}]}', '2026-01-01');
        CREATE TABLE local_tool_receipts (
            operation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
            execution_attempt_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
            tool_name TEXT NOT NULL, tool_version TEXT NOT NULL DEFAULT '',
            arguments_hash TEXT NOT NULL, arguments_json TEXT NOT NULL,
            risk TEXT NOT NULL, status TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0, result_json TEXT,
            result_hash TEXT, error_type TEXT, created_at TEXT NOT NULL,
            started_at TEXT, completed_at TEXT, updated_at TEXT NOT NULL,
            UNIQUE (run_id, tool_call_id)
        );
        """
    )
    connection.close()

    store = await LocalStore.open(path)
    try:
        permission_columns = {
            row[1]
            for row in await (
                await store._conn.execute("PRAGMA table_info(local_permissions)")
            ).fetchall()
        }
        receipt_columns = {
            row[1]
            for row in await (
                await store._conn.execute("PRAGMA table_info(local_tool_receipts)")
            ).fetchall()
        }
        assert {"tool_version", "interrupt_id", "action_index"} <= permission_columns
        assert {"execution_namespace", "parent_operation_id"} <= receipt_columns
        legacy_question = await (
            await store._conn.execute(
                "SELECT wait_cycle_id, interrupt_id FROM local_questions WHERE id = ?",
                ("legacy-question",),
            )
        ).fetchone()
        assert tuple(legacy_question) == ("legacy-question", "legacy-question")
        legacy_permission = await (
            await store._conn.execute(
                "SELECT wait_cycle_id, interrupt_id FROM local_permissions WHERE id = ?",
                ("legacy-permission",),
            )
        ).fetchone()
        assert tuple(legacy_permission) == ("legacy-permission", "legacy-permission")
        legacy_plan = await (
            await store._conn.execute(
                "SELECT wait_cycle_id, interrupt_id FROM local_plan_approvals WHERE id = ?",
                ("legacy-plan",),
            )
        ).fetchone()
        assert tuple(legacy_plan) == ("wait-legacy-plan", "interrupt-legacy-plan")
        pending_candidate = await (
            await store._conn.execute(
                "SELECT kind, status FROM local_wait_candidates WHERE id = ?",
                ("legacy-plan",),
            )
        ).fetchone()
        assert tuple(pending_candidate) == ("plan", "pending")
        resolved_candidate = await (
            await store._conn.execute(
                "SELECT status, decision_json FROM local_wait_candidates WHERE id = ?",
                ("legacy-plan-resolved",),
            )
        ).fetchone()
        assert resolved_candidate[0] == "resolved"
        assert json.loads(resolved_candidate[1]) == {
            "approval_id": "legacy-plan-resolved",
            "decision": "modify",
            "instructions": "Add verification.",
        }
        assert await store.wait_cycle_resume_payload(
            run_id="legacy-run",
            wait_cycle_id="wait-legacy-resolved",
        ) == {
            "interrupt-legacy-resolved": {
                "approval_id": "legacy-plan-resolved",
                "decision": "modify",
                "instructions": "Add verification.",
            }
        }
        orphan_plan = await (
            await store._conn.execute(
                "SELECT wait_cycle_id, interrupt_id FROM local_plan_approvals WHERE id = ?",
                ("legacy-plan-orphan",),
            )
        ).fetchone()
        assert tuple(orphan_plan) == (None, None)
        orphan_candidate = await (
            await store._conn.execute(
                "SELECT 1 FROM local_wait_candidates WHERE id = ?",
                ("legacy-plan-orphan",),
            )
        ).fetchone()
        assert orphan_candidate is None
        repaired = await store.create_plan_approval(
            run_id="legacy-run",
            tool_call_id="legacy-orphan-call",
            todos=[],
            wait_cycle_id="wait-repaired",
            interrupt_id="interrupt-repaired",
        )
        assert repaired["id"] == "legacy-plan-orphan"
        repaired_candidate = await (
            await store._conn.execute(
                "SELECT wait_cycle_id, interrupt_id FROM local_wait_candidates WHERE id = ?",
                ("legacy-plan-orphan",),
            )
        ).fetchone()
        assert tuple(repaired_candidate) == ("wait-repaired", "interrupt-repaired")
    finally:
        await store.close()


def _request(
    store: LocalStore,
    run_id: str,
    *,
    tool_call_id: str = "call-1",
    tool_name: str = "web.fetch",
    arguments: dict[str, object] | None = None,
    context: RuntimeContext | None = None,
    messages: list[object] | None = None,
) -> ToolCallRequest:
    context = context or RuntimeContext(store=store, run_id=run_id, execution_attempt_id="job-1:1")
    return ToolCallRequest(
        tool_call={
            "id": tool_call_id,
            "name": tool_name,
            "args": arguments or {"url": "https://example.com"},
            "type": "tool_call",
        },
        tool=None,
        state={"messages": messages or []},
        runtime=SimpleNamespace(context=context),
    )


@pytest.mark.asyncio
async def test_completed_tool_result_replays_without_second_execution(tmp_path: Path) -> None:
    store, run = await _store_and_run(tmp_path)
    calls = 0

    async def handler(request: ToolCallRequest) -> ToolMessage:
        nonlocal calls
        calls += 1
        return ToolMessage(
            content="fetched",
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
        )

    try:
        request = _request(store, str(run["id"]))
        middleware = ToolExecutionMiddleware()

        first = await middleware.awrap_tool_call(request, handler)
        second = await middleware.awrap_tool_call(request, handler)

        assert first.content == second.content == "fetched"
        assert calls == 1
        receipts = await store.list_tool_receipts_for_run(str(run["id"]))
        assert len(receipts) == 1
        assert receipts[0]["status"] == "completed"
        assert receipts[0]["attempt_count"] == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_structured_failed_tool_result_is_failed_in_receipt_and_replay(
    tmp_path: Path,
) -> None:
    store, run = await _store_and_run(tmp_path)
    calls = 0

    async def handler(request: ToolCallRequest) -> ToolMessage:
        nonlocal calls
        calls += 1
        return ToolMessage(
            content='{"ok":"false","error":"blocked by policy"}',
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
        )

    try:
        request = _request(store, str(run["id"]))
        middleware = ToolExecutionMiddleware()

        first = await middleware.awrap_tool_call(request, handler)
        second = await middleware.awrap_tool_call(request, handler)

        assert isinstance(first, ToolMessage)
        assert isinstance(second, ToolMessage)
        assert first.status == second.status == "error"
        assert calls == 1
        receipts = await store.list_tool_receipts_for_run(str(run["id"]))
        assert len(receipts) == 1
        assert receipts[0]["status"] == "failed"
        assert receipts[0]["attempt_count"] == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_disabled_subagent_call_is_rejected_before_execution_or_receipt(
    tmp_path: Path,
) -> None:
    store, run = await _store_and_run(tmp_path)
    calls = 0

    async def handler(request: ToolCallRequest) -> ToolMessage:
        nonlocal calls
        calls += 1
        return ToolMessage(
            content="unexpected subagent result",
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
        )

    try:
        context = RuntimeContext(
            store=store,
            run_id=str(run["id"]),
            execution_attempt_id="job-1:1",
            subagents_enabled=False,
        )
        request = _request(
            store,
            str(run["id"]),
            tool_name="task",
            arguments={"description": "must not run"},
            context=context,
        )

        result = await ToolExecutionMiddleware().awrap_tool_call(request, handler)

        assert result.status == "error"
        assert result.content == "Subagent dispatch is disabled for this Run."
        assert calls == 0
        assert await store.list_tool_receipts_for_run(str(run["id"])) == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_tool_receipt_persists_auto_review_decision_for_replay(tmp_path: Path) -> None:
    store, run = await _store_and_run(tmp_path)
    try:
        receipt = await store.prepare_tool_receipt(
            operation_id="op-review-1",
            run_id=str(run["id"]),
            execution_attempt_id="job-review:1",
            execution_namespace="main",
            tool_call_id="call-review-1",
            tool_name="execute",
            tool_version="graph-v1",
            arguments_hash="args-v1",
            arguments_json='{"command":"make test"}',
            risk="external_or_unknown",
        )

        reviewed = await store.record_tool_review(
            operation_id=str(receipt["operation_id"]),
            run_id=str(run["id"]),
            decision="allow",
            source="llm",
            reason="The command directly implements the request.",
            model="local:test:model",
        )
        replayed = await store.record_tool_review(
            operation_id=str(receipt["operation_id"]),
            run_id=str(run["id"]),
            decision="allow",
            source="llm",
            reason="The command directly implements the request.",
            model="local:test:model",
        )

        assert reviewed["review_decision"] == replayed["review_decision"] == "allow"
        assert reviewed["review_source"] == "llm"
        assert reviewed["review_model"] == "local:test:model"
        assert reviewed["reviewed_at"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_auto_mode_does_not_use_model_reviewer_for_sandboxed_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, run = await _store_and_run(tmp_path)
    reviewer_calls = 0

    class ReviewerModel:
        async def ainvoke(self, _messages: list[object], **_kwargs: object) -> AIMessage:
            nonlocal reviewer_calls
            reviewer_calls += 1
            return AIMessage(
                content=json.dumps(
                    {
                        "decisions": [
                            {
                                "operation_id": operation_id,
                                "decision": "allow",
                                "reason": "The test command matches the task.",
                            }
                        ]
                    }
                )
            )

    call = ToolCall(
        type="tool_call", id="call-auto-review", name="execute", args={"command": "make test"}
    )
    context = RuntimeContext(
        store=store,
        run_id=str(run["id"]),
        execution_attempt_id="job-auto-review:1",
        graph_definition_id="graph-v1",
        permission_mode="auto",
        task_goal="Run the tests",
        mode="local:test:model",
        model=ReviewerModel(),
        tool_registry={
            "execute": SimpleNamespace(
                tool_call_schema={
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                    "additionalProperties": False,
                }
            )
        },
    )
    state = {
        "messages": [
            AIMessage(id="batch_auto", content="", tool_calls=[call]),
        ]
    }
    operation_id, _arguments_hash, _arguments_json = tool_operation_identity(
        run_id=str(run["id"]),
        tool_call_id=call["id"],
        tool_name=call["name"],
        arguments=call["args"],
        tool_version="graph-v1",
        execution_namespace=canonical_tool_execution_scope(
            execution_scope_from_messages("main", state["messages"])
        ),
    )
    monkeypatch.setattr(
        "shejane_runtime.middleware.tool_review.interrupt",
        lambda _payload: (_ for _ in ()).throw(AssertionError("approved action must not pause")),
    )
    try:
        result = await ToolReviewMiddleware().aafter_model(
            state,
            SimpleNamespace(context=context),  # type: ignore[arg-type]
        )
        replayed = await ToolReviewMiddleware().aafter_model(
            state,
            SimpleNamespace(context=context),  # type: ignore[arg-type]
        )

        assert result is None
        assert replayed is None
        assert reviewer_calls == 0
        receipts = await store.list_tool_receipts_for_run(str(run["id"]))
        assert len(receipts) == 1
        assert receipts[0]["review_decision"] == "allow"
        assert receipts[0]["review_source"] == "rule"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_auto_mode_falls_back_to_human_review_when_model_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, run = await _store_and_run(tmp_path)
    captured: dict[str, object] = {}

    class FailingReviewerModel:
        async def ainvoke(self, _messages: list[object], **_kwargs: object) -> AIMessage:
            raise TimeoutError("review provider unavailable")

    class PauseObserved(RuntimeError):
        pass

    def pause(payload: dict[str, object]) -> object:
        captured.update(payload)
        raise PauseObserved

    call = ToolCall(
        type="tool_call",
        id="call-auto-fallback",
        name="clipboard.write",
        args={"text": "copy this"},
    )
    context = RuntimeContext(
        store=store,
        run_id=str(run["id"]),
        execution_attempt_id="job-auto-fallback:1",
        graph_definition_id="graph-v1",
        permission_mode="auto",
        task_goal="Run the tests",
        mode="local:test:model",
        model=FailingReviewerModel(),
        tool_registry={
            "clipboard.write": SimpleNamespace(
                tool_call_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                }
            )
        },
    )
    monkeypatch.setattr("shejane_runtime.middleware.tool_review.interrupt", pause)
    try:
        with pytest.raises(PauseObserved):
            await ToolReviewMiddleware().aafter_model(
                {"messages": [AIMessage(content="", tool_calls=[call])]},
                SimpleNamespace(context=context),  # type: ignore[arg-type]
            )

        assert captured["kind"] == "tool_review"
        assert len(captured["action_requests"]) == 1  # type: ignore[arg-type]
        request = captured["action_requests"][0]  # type: ignore[index]
        assert request["review_source"] == "fallback"
        assert "fallback policy" in request["review_reason"]
        receipts = await store.list_tool_receipts_for_run(str(run["id"]))
        assert receipts[0]["review_decision"] == "ask"
        assert receipts[0]["review_source"] == "fallback"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_pdf_attachment_read_is_persisted_as_provider_safe_text(tmp_path: Path) -> None:
    store, run = await _store_and_run(tmp_path)
    attachment_path = "/attachments/booking.pdf"
    context = RuntimeContext(
        store=store,
        run_id=str(run["id"]),
        execution_attempt_id="job-1:1",
        attachments=(attachment_path,),
    )

    async def handler(request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content_blocks=[
                {
                    "type": "file",
                    "base64": "extracted booking text",
                    "mime_type": "application/pdf",
                }
            ],
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
            additional_kwargs={
                "read_file_path": attachment_path,
                "read_file_media_type": "application/pdf",
            },
        )

    try:
        result = await ToolExecutionMiddleware().awrap_tool_call(
            _request(
                store,
                str(run["id"]),
                tool_name="read_file",
                arguments={"file_path": attachment_path},
                context=context,
            ),
            handler,
        )

        assert isinstance(result, ToolMessage)
        assert result.content == "extracted booking text"
        assert result.content_blocks == [{"type": "text", "text": "extracted booking text"}]

        provider_payload = ChatOpenAI(
            model="deepseek-test",
            api_key="test-key",
        )._get_request_payload(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call-1",
                            "name": "read_file",
                            "args": {"file_path": attachment_path},
                        }
                    ],
                ),
                result,
            ]
        )
        assert provider_payload["messages"][1]["content"] == "extracted booking text"

        receipts = await store.list_tool_receipts_for_run(str(run["id"]))
        replayed = json.loads(str(receipts[0]["result_json"]))
        assert replayed["value"]["data"]["content"] == "extracted booking text"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_text_only_model_does_not_receive_image_tool_blocks(tmp_path: Path) -> None:
    store, run = await _store_and_run(tmp_path)
    context = RuntimeContext(
        store=store,
        run_id=str(run["id"]),
        execution_attempt_id="job-1:1",
        model=SimpleNamespace(profile={"image_inputs": False}),
    )

    async def handler(request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content_blocks=[
                {"type": "image", "base64": "encoded-image", "mime_type": "image/jpeg"}
            ],
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
        )

    try:
        result = await ToolExecutionMiddleware().awrap_tool_call(
            _request(
                store,
                str(run["id"]),
                tool_name="read_file",
                arguments={"file_path": "/workspace/photo.jpg"},
                context=context,
            ),
            handler,
        )

        assert isinstance(result, ToolMessage)
        assert result.content == (
            "Image content was not provided because the selected model is text-only. "
            "Choose a model marked as supporting images before describing this file."
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_text_only_model_keeps_computer_use_outline_without_screenshot(
    tmp_path: Path,
) -> None:
    store, run = await _store_and_run(tmp_path)
    context = RuntimeContext(
        store=store,
        run_id=str(run["id"]),
        execution_attempt_id="job-1:1",
        model=SimpleNamespace(profile={"image_inputs": False}),
    )

    async def handler(request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content_blocks=[
                {"type": "text", "text": "@e1 button Save"},
                {"type": "image", "base64": "encoded-image", "mime_type": "image/png"},
            ],
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
        )

    try:
        result = await ToolExecutionMiddleware().awrap_tool_call(
            _request(
                store,
                str(run["id"]),
                tool_name="plugin.org.shejane.computer-use.observe_ui",
                arguments={},
                context=context,
            ),
            handler,
        )

        assert isinstance(result, ToolMessage)
        assert result.content == [
            {"type": "text", "text": "@e1 button Save"},
            {
                "type": "text",
                "text": (
                    "Image content was not provided because the selected model is text-only. "
                    "Choose a model marked as supporting images before describing this file."
                ),
            },
        ]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_tool_definition_version_is_part_of_operation_identity(tmp_path: Path) -> None:
    store, run = await _store_and_run(tmp_path)

    async def handler(request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content="ok",
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
        )

    try:
        middleware = ToolExecutionMiddleware()
        first_context = RuntimeContext(
            store=store,
            run_id=str(run["id"]),
            execution_attempt_id="job-1:1",
            graph_definition_id="graph-v1",
        )
        second_context = RuntimeContext(
            store=store,
            run_id=str(run["id"]),
            execution_attempt_id="job-2:1",
            graph_definition_id="graph-v2",
        )
        await middleware.awrap_tool_call(
            _request(store, str(run["id"]), context=first_context), handler
        )
        with pytest.raises(
            ToolReceiptConflictError,
            match="reused with a different operation identity",
        ):
            await middleware.awrap_tool_call(
                _request(store, str(run["id"]), context=second_context), handler
            )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_consequential_tools_are_serialized_within_a_run(tmp_path: Path) -> None:
    store, run = await _store_and_run(tmp_path)
    context = RuntimeContext(
        store=store,
        run_id=str(run["id"]),
        execution_attempt_id="job-1:1",
    )
    active = 0
    max_active = 0

    async def handler(request: ToolCallRequest) -> ToolMessage:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return ToolMessage(
            content="ok",
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
        )

    try:
        middleware = ToolExecutionMiddleware()
        await asyncio.gather(
            middleware.awrap_tool_call(
                _request(
                    store,
                    str(run["id"]),
                    tool_call_id="execute-1",
                    tool_name="execute",
                    arguments={"command": "one"},
                    context=context,
                ),
                handler,
            ),
            middleware.awrap_tool_call(
                _request(
                    store,
                    str(run["id"]),
                    tool_call_id="execute-2",
                    tool_name="execute",
                    arguments={"command": "two"},
                    context=context,
                ),
                handler,
            ),
        )
        assert max_active == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_conflicting_batch_preserves_model_order(tmp_path: Path) -> None:
    store, run = await _store_and_run(tmp_path)
    context = RuntimeContext(
        store=store,
        run_id=str(run["id"]),
        execution_attempt_id="job-1:1",
        workspace_root=str(tmp_path),
    )
    tool_calls = [
        {"id": "write-1", "name": "write_file", "args": {"path": "a.txt"}},
        {"id": "read-1", "name": "read_file", "args": {"path": "a.txt"}},
    ]
    messages = [AIMessage(content="", tool_calls=tool_calls)]
    order: list[str] = []

    async def handler(request: ToolCallRequest) -> ToolMessage:
        call_id = str(request.tool_call["id"])
        order.append(f"start:{call_id}")
        await asyncio.sleep(0.01)
        order.append(f"end:{call_id}")
        return ToolMessage(content="ok", name=request.tool_call["name"], tool_call_id=call_id)

    try:
        middleware = ToolExecutionMiddleware()
        write_request = _request(
            store,
            str(run["id"]),
            tool_call_id="write-1",
            tool_name="write_file",
            arguments={"path": "a.txt"},
            context=context,
            messages=messages,
        )
        read_request = _request(
            store,
            str(run["id"]),
            tool_call_id="read-1",
            tool_name="read_file",
            arguments={"path": "a.txt"},
            context=context,
            messages=messages,
        )
        await asyncio.gather(
            middleware.awrap_tool_call(read_request, handler),
            middleware.awrap_tool_call(write_request, handler),
        )
        assert order == ["start:write-1", "end:write-1", "start:read-1", "end:read-1"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_replayed_first_call_advances_conflicting_batch_order(tmp_path: Path) -> None:
    store, run = await _store_and_run(tmp_path)
    tool_calls = [
        {"id": "write-1", "name": "write_file", "args": {"path": "a.txt"}},
        {"id": "read-1", "name": "read_file", "args": {"path": "a.txt"}},
    ]
    messages = [AIMessage(content="", id="batch-1", tool_calls=tool_calls)]
    calls: list[str] = []

    async def handler(request: ToolCallRequest) -> ToolMessage:
        calls.append(str(request.tool_call["id"]))
        return ToolMessage(
            content="ok",
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
        )

    try:
        first_context = RuntimeContext(
            store=store,
            run_id=str(run["id"]),
            execution_attempt_id="job-1:1",
            workspace_root=str(tmp_path),
        )
        middleware = ToolExecutionMiddleware()
        write = _request(
            store,
            str(run["id"]),
            tool_call_id="write-1",
            tool_name="write_file",
            arguments={"path": "a.txt"},
            context=first_context,
            messages=messages,
        )
        await middleware.awrap_tool_call(write, handler)

        recovered_context = RuntimeContext(
            store=store,
            run_id=str(run["id"]),
            execution_attempt_id="job-2:1",
            workspace_root=str(tmp_path),
        )
        replayed_write = _request(
            store,
            str(run["id"]),
            tool_call_id="write-1",
            tool_name="write_file",
            arguments={"path": "a.txt"},
            context=recovered_context,
            messages=messages,
        )
        read = _request(
            store,
            str(run["id"]),
            tool_call_id="read-1",
            tool_name="read_file",
            arguments={"path": "a.txt"},
            context=recovered_context,
            messages=messages,
        )
        await asyncio.wait_for(
            asyncio.gather(
                middleware.awrap_tool_call(read, handler),
                middleware.awrap_tool_call(replayed_write, handler),
            ),
            timeout=1,
        )
        assert calls == ["write-1", "read-1"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_pre_resolved_call_does_not_leave_an_ordering_gap(tmp_path: Path) -> None:
    store, run = await _store_and_run(tmp_path)
    messages = [
        AIMessage(
            content="",
            id="batch-rejected",
            tool_calls=[
                {"id": "write-1", "name": "write_file", "args": {"path": "a.txt"}},
                {"id": "read-1", "name": "read_file", "args": {"path": "a.txt"}},
            ],
        ),
        ToolMessage(
            content="rejected",
            name="write_file",
            tool_call_id="write-1",
            status="error",
        ),
    ]

    async def handler(request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content="ok",
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
        )

    try:
        result = await asyncio.wait_for(
            ToolExecutionMiddleware().awrap_tool_call(
                _request(
                    store,
                    str(run["id"]),
                    tool_call_id="read-1",
                    tool_name="read_file",
                    arguments={"path": "a.txt"},
                    messages=messages,
                ),
                handler,
            ),
            timeout=1,
        )
        assert result.content == "ok"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_provider_tool_call_ids_may_repeat_in_later_model_rounds(
    tmp_path: Path,
) -> None:
    store, run = await _store_and_run(tmp_path)
    context = RuntimeContext(
        store=store,
        run_id=str(run["id"]),
        execution_attempt_id="job-1:1",
    )

    async def handler(request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content=str(request.tool_call["args"]),
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
        )

    try:
        middleware = ToolExecutionMiddleware()
        first_messages = [
            AIMessage(
                content="",
                id="round-1",
                tool_calls=[{"id": "call-1", "name": "read_file", "args": {"path": "a"}}],
            )
        ]
        second_messages = [
            *first_messages,
            ToolMessage(content="a", tool_call_id="call-1"),
            AIMessage(
                content="",
                id="round-2",
                tool_calls=[{"id": "call-1", "name": "read_file", "args": {"path": "b"}}],
            ),
        ]
        await middleware.awrap_tool_call(
            _request(
                store,
                str(run["id"]),
                tool_call_id="call-1",
                tool_name="read_file",
                arguments={"path": "a"},
                context=context,
                messages=first_messages,
            ),
            handler,
        )
        await middleware.awrap_tool_call(
            _request(
                store,
                str(run["id"]),
                tool_call_id="call-1",
                tool_name="read_file",
                arguments={"path": "b"},
                context=context,
                messages=second_messages,
            ),
            handler,
        )
        assert len(await store.list_tool_receipts_for_run(str(run["id"]))) == 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_pure_read_batch_remains_parallel(tmp_path: Path) -> None:
    store, run = await _store_and_run(tmp_path)
    context = RuntimeContext(
        store=store,
        run_id=str(run["id"]),
        execution_attempt_id="job-1:1",
    )
    active = 0
    max_active = 0

    async def handler(request: ToolCallRequest) -> ToolMessage:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return ToolMessage(
            content="ok",
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
        )

    try:
        middleware = ToolExecutionMiddleware()
        await asyncio.gather(
            *[
                middleware.awrap_tool_call(
                    _request(
                        store,
                        str(run["id"]),
                        tool_call_id=f"read-{index}",
                        tool_name="read_file",
                        arguments={"path": f"{index}.txt"},
                        context=context,
                    ),
                    handler,
                )
                for index in range(2)
            ]
        )
        assert max_active == 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_large_tool_results_are_artifacts_with_bounded_model_handoff(
    tmp_path: Path,
) -> None:
    store, run = await _store_and_run(tmp_path)
    huge = "start\n" + ("x" * 200_000) + "\nend"

    async def message_handler(request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content=huge,
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
        )

    async def command_handler(request: ToolCallRequest) -> Command:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=huge,
                        name=request.tool_call["name"],
                        tool_call_id=request.tool_call["id"],
                    )
                ]
            }
        )

    try:
        middleware = ToolExecutionMiddleware()
        message_result = await middleware.awrap_tool_call(
            _request(store, str(run["id"]), tool_call_id="large-message"),
            message_handler,
        )
        command_result = await middleware.awrap_tool_call(
            _request(store, str(run["id"]), tool_call_id="large-command"),
            command_handler,
        )

        assert "Full tool output stored as artifact" in str(message_result.content)
        assert isinstance(command_result, Command)
        assert len(serialize_tool_result(message_result).encode()) <= MAX_MODEL_TOOL_RESULT_BYTES
        assert len(serialize_tool_result(command_result).encode()) <= MAX_MODEL_TOOL_RESULT_BYTES
        artifacts = await store.list_artifacts_for_run(str(run["id"]))
        assert len(artifacts) == 2
        assert all(artifact["kind"] == "tool_output" for artifact in artifacts)
        receipts = await store.list_tool_receipts_for_run(str(run["id"]))
        assert all(
            len(str(receipt["result_json"]).encode()) <= MAX_MODEL_TOOL_RESULT_BYTES
            for receipt in receipts
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_oversized_command_state_is_not_silently_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, run = await _store_and_run(tmp_path)

    async def handler(_request: ToolCallRequest) -> Command:
        return Command(update={"custom_state": "x" * 200_000}, goto="next")

    try:
        monkeypatch.setattr(
            "shejane_runtime.middleware.tool_execution.interrupt", lambda _payload: {}
        )
        with pytest.raises(ToolReceiptStateError):
            await ToolExecutionMiddleware().awrap_tool_call(
                _request(
                    store,
                    str(run["id"]),
                    tool_name="execute",
                    arguments={"command": "stateful"},
                ),
                handler,
            )
        receipt = (await store.list_tool_receipts_for_run(str(run["id"])))[0]
        assert receipt["status"] == "outcome_unknown"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_task_failure_is_not_misclassified_as_unknown_external_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, run = await _store_and_run(tmp_path)

    async def handler(_request: ToolCallRequest) -> ToolMessage:
        raise RuntimeError("subagent model budget exhausted")

    monkeypatch.setattr(
        "shejane_runtime.middleware.tool_execution.interrupt",
        lambda _payload: (_ for _ in ()).throw(AssertionError("task must not reconcile")),
    )
    try:
        result = await ToolExecutionMiddleware().awrap_tool_call(
            _request(store, str(run["id"]), tool_name="task", arguments={"description": "x"}),
            handler,
        )
        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert "subagent model budget exhausted" in str(result.content)
        receipt = (await store.list_tool_receipts_for_run(str(run["id"])))[0]
        assert receipt["status"] == "failed"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_child_lookup_error_returns_a_replayable_tool_failure(tmp_path: Path) -> None:
    store, run = await _store_and_run(tmp_path)

    async def handler(_request: ToolCallRequest) -> ToolMessage:
        raise KeyError("child run not found: invented-id")

    try:
        request = _request(
            store,
            str(run["id"]),
            tool_name="child.check",
            arguments={"run_ids": ["invented-id"]},
        )
        middleware = ToolExecutionMiddleware()

        first = await middleware.awrap_tool_call(request, handler)
        replayed = await middleware.awrap_tool_call(request, handler)

        assert isinstance(first, ToolMessage)
        assert first.status == "error"
        assert json.loads(str(first.content)) == {
            "ok": False,
            "error_code": "child_run_not_found",
            "message": "child run not found",
            "retryable": False,
        }
        assert replayed == first
        receipt = (await store.list_tool_receipts_for_run(str(run["id"])))[0]
        assert receipt["status"] == "failed"
        assert receipt["error_type"] == "KeyError"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_child_internal_key_error_is_not_misclassified_as_missing_run(
    tmp_path: Path,
) -> None:
    store, run = await _store_and_run(tmp_path)

    async def handler(_request: ToolCallRequest) -> ToolMessage:
        raise KeyError("unexpected implementation bug")

    try:
        request = _request(
            store,
            str(run["id"]),
            tool_name="child.check",
            arguments={"run_ids": ["child-1"]},
        )

        with pytest.raises(KeyError, match="unexpected implementation bug"):
            await ToolExecutionMiddleware().awrap_tool_call(request, handler)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_artifact_store_enforces_item_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, run = await _store_and_run(tmp_path)
    monkeypatch.setattr("shejane_runtime.store.artifacts.MAX_ARTIFACT_BYTES", 4)
    try:
        with pytest.raises(ArtifactQuotaError):
            await store.create_artifact(
                run_id=str(run["id"]),
                kind="tool_output",
                title="too large",
                content="12345",
            )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_blob_gc_keeps_catalog_bodies_and_removes_old_orphans(tmp_path: Path) -> None:
    store, run = await _store_and_run(tmp_path)
    source = tmp_path / "result.bin"
    source.write_bytes(b"catalog body")
    try:
        artifact = await store.create_file_artifact(
            run_id=str(run["id"]),
            kind="tool_output",
            title="result.bin",
            source_path=source,
            content_type="application/octet-stream",
        )
        referenced = store.artifact_body_path(artifact)
        orphan = tmp_path / "artifacts" / "sha256" / "ff" / ("f" * 64)
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(b"orphan")

        assert await store.gc_orphan_bodies(grace_seconds=0) == 1
        assert referenced.read_bytes() == b"catalog body"
        assert not orphan.exists()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_run_permission_grant_is_tool_level_for_the_rest_of_the_run(
    tmp_path: Path,
) -> None:
    store, run = await _store_and_run(tmp_path)
    try:
        permission = await store.create_permission(
            run_id=str(run["id"]),
            tool_call_id="approved-call",
            operation_id="approved-operation",
            tool_name="write_file",
            tool_version="graph-v1",
            arguments={"command": "make test"},
            arguments_hash="approved-hash",
            risk="workspace_write",
        )
        await store.resolve_permission(
            permission["id"],
            status="approved",
            scope="run",
            decision={"type": "approve"},
        )
        assert not await store.consume_run_permission_grant(
            run_id=str(run["id"]),
            operation_id="different-version-operation",
            tool_name="write_file",
            tool_version="graph-v2",
            risk="workspace_write",
        )

        uses = [
            await store.consume_run_permission_grant(
                run_id=str(run["id"]),
                operation_id=f"operation-{index}",
                tool_name="write_file",
                tool_version="graph-v1",
                risk="workspace_write",
            )
            for index in range(25)
        ]
        assert uses == [True] * 25
        assert await store.consume_run_permission_grant(
            run_id=str(run["id"]),
            operation_id="operation-0",
            tool_name="write_file",
            tool_version="graph-v1",
            risk="workspace_write",
        )
        assert not await store.consume_run_permission_grant(
            run_id=str(run["id"]),
            operation_id="different-operation",
            tool_name="edit_file",
            tool_version="graph-v1",
            risk="workspace_write",
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_tool_call_id_cannot_be_reused_with_other_arguments(tmp_path: Path) -> None:
    store, run = await _store_and_run(tmp_path)

    async def handler(request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content="ok",
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
        )

    try:
        middleware = ToolExecutionMiddleware()
        await middleware.awrap_tool_call(_request(store, str(run["id"])), handler)

        with pytest.raises(ToolReceiptConflictError):
            await middleware.awrap_tool_call(
                _request(
                    store,
                    str(run["id"]),
                    arguments={"url": "https://different.example"},
                ),
                handler,
            )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_uncertain_side_effect_requires_explicit_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, run = await _store_and_run(tmp_path)
    calls = 0

    async def handler(_request: ToolCallRequest) -> ToolMessage:
        nonlocal calls
        calls += 1
        raise TimeoutError("connection lost after dispatch")

    try:
        monkeypatch.setattr(
            "shejane_runtime.middleware.tool_execution.interrupt", lambda _payload: {}
        )
        request = _request(
            store,
            str(run["id"]),
            tool_name="execute",
            arguments={"command": "do-something"},
        )
        middleware = ToolExecutionMiddleware()
        with pytest.raises(TimeoutError):
            await middleware.awrap_tool_call(request, handler)
        receipt = (await store.list_tool_receipts_for_run(str(run["id"])))[0]
        await store.create_tool_reconciliation(
            run_id=str(run["id"]),
            operation_id=str(receipt["operation_id"]),
            wait_cycle_id="wait-abort",
            interrupt_id="interrupt-abort",
            payload={"operation_id": receipt["operation_id"]},
        )
        result_json = serialize_tool_result(
            ToolMessage(
                content="do not retry",
                name="execute",
                tool_call_id="call-1",
                status="error",
            )
        )
        await store.resolve_tool_reconciliation(
            str(receipt["operation_id"]),
            decision="abort",
            current_result_json=result_json,
            current_result_hash="hash",
            prior_result_json=result_json,
            prior_result_hash="hash",
        )
        result = await middleware.awrap_tool_call(request, handler)
        assert calls == 1
        assert result.status == "error"
        receipt = (await store.list_tool_receipts_for_run(str(run["id"])))[0]
        assert receipt["status"] == "failed"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_reconciliation_retries_only_after_user_confirms_not_executed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, run = await _store_and_run(tmp_path)
    calls = 0

    async def handler(request: ToolCallRequest) -> ToolMessage:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("connection lost after dispatch")
        return ToolMessage(
            content="completed on retry",
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
        )

    try:
        monkeypatch.setattr(
            "shejane_runtime.middleware.tool_execution.interrupt",
            lambda _payload: {},
        )
        request = _request(
            store,
            str(run["id"]),
            tool_name="execute",
            arguments={"command": "do-something"},
        )
        middleware = ToolExecutionMiddleware()
        with pytest.raises(TimeoutError):
            await middleware.awrap_tool_call(request, handler)
        receipt = (await store.list_tool_receipts_for_run(str(run["id"])))[0]
        await store.create_tool_reconciliation(
            run_id=str(run["id"]),
            operation_id=str(receipt["operation_id"]),
            wait_cycle_id="wait-retry",
            interrupt_id="interrupt-retry",
            payload={"operation_id": receipt["operation_id"]},
        )
        result_json = serialize_tool_result(
            ToolMessage(
                content="confirmed not executed",
                name="execute",
                tool_call_id="call-1",
                status="error",
            )
        )
        await store.resolve_tool_reconciliation(
            str(receipt["operation_id"]),
            decision="retry_not_executed",
            current_result_json=None,
            current_result_hash=None,
            prior_result_json=result_json,
            prior_result_hash="hash",
        )
        result = await middleware.awrap_tool_call(request, handler)
        assert result.content == "completed on retry"
        assert calls == 2
        receipt = (await store.list_tool_receipts_for_run(str(run["id"])))[0]
        assert receipt["status"] == "completed"
        assert receipt["attempt_count"] == 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_child_run_reconciles_ancestor_before_repeating_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source_run = await _store_and_run(tmp_path)
    intermediate_run = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="intermediate fork",
        workspace_path=None,
        parent_run_id=str(source_run["id"]),
    )
    target_run = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="retry uncertain tool",
        workspace_path=None,
        parent_run_id=str(intermediate_run["id"]),
    )
    operation_id, arguments_hash, arguments_json = tool_operation_identity(
        run_id=str(source_run["id"]),
        tool_call_id="source-call",
        tool_name="execute",
        arguments={"command": "deploy"},
    )
    await store.prepare_tool_receipt(
        operation_id=operation_id,
        run_id=str(source_run["id"]),
        execution_attempt_id="source-job:1",
        tool_call_id="source-call",
        tool_name="execute",
        tool_version="graph-v1",
        arguments_hash=arguments_hash,
        arguments_json=arguments_json,
        risk="sandboxed_command",
    )
    await store.begin_tool_receipt(
        operation_id=operation_id,
        run_id=str(source_run["id"]),
        execution_attempt_id="source-job:1",
    )
    await store.settle_tool_receipt(
        operation_id=operation_id,
        run_id=str(source_run["id"]),
        status="outcome_unknown",
        error_type="TimeoutError",
    )
    calls = 0

    async def handler(request: ToolCallRequest) -> ToolMessage:
        nonlocal calls
        calls += 1
        return ToolMessage(
            content="should not run",
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
        )

    try:
        monkeypatch.setattr(
            "shejane_runtime.middleware.tool_execution.interrupt",
            lambda _payload: {},
        )
        context = RuntimeContext(
            store=store,
            run_id=str(target_run["id"]),
            execution_attempt_id="target-job:1",
            graph_definition_id="graph-v2",
        )
        request = _request(
            store,
            str(target_run["id"]),
            tool_call_id="target-call",
            tool_name="execute",
            arguments={"command": "deploy"},
            context=context,
        )
        middleware = ToolExecutionMiddleware()
        with pytest.raises(ToolReceiptStateError):
            await middleware.awrap_tool_call(request, handler)
        current = (await store.list_tool_receipts_for_run(str(target_run["id"])))[0]
        await store.create_tool_reconciliation(
            run_id=str(target_run["id"]),
            operation_id=str(current["operation_id"]),
            wait_cycle_id="wait-ancestor",
            interrupt_id="interrupt-ancestor",
            payload={
                "operation_id": current["operation_id"],
                "prior_operation_id": operation_id,
            },
        )
        current_json = serialize_tool_result(
            ToolMessage(
                content="confirmed completed",
                name="execute",
                tool_call_id="target-call",
            )
        )
        prior_json = serialize_tool_result(
            ToolMessage(
                content="confirmed completed",
                name="execute",
                tool_call_id="source-call",
            )
        )
        await store.resolve_tool_reconciliation(
            str(current["operation_id"]),
            decision="confirmed_completed",
            current_result_json=current_json,
            current_result_hash="current-hash",
            prior_result_json=prior_json,
            prior_result_hash="prior-hash",
        )
        result = await middleware.awrap_tool_call(request, handler)
        assert calls == 0
        assert result.status == "success"
        assert (await store.get_tool_receipt(operation_id))["status"] == "completed"
        target_receipts = await store.list_tool_receipts_for_run(str(target_run["id"]))
        assert target_receipts[0]["status"] == "completed"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_reconciliation_rejects_a_foreign_parent_chain(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    foreign = await store.create_run(
        principal_id="foreign-owner",
        goal="foreign",
        workspace_path=None,
    )
    current = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="current",
        workspace_path=None,
        parent_run_id=str(foreign["id"]),
    )
    prior_id, prior_hash, prior_json = tool_operation_identity(
        run_id=str(foreign["id"]),
        tool_call_id="prior-call",
        tool_name="execute",
        arguments={"command": "deploy"},
    )
    current_id, current_hash, current_args = tool_operation_identity(
        run_id=str(current["id"]),
        tool_call_id="current-call",
        tool_name="execute",
        arguments={"command": "deploy"},
    )
    try:
        for run_id, operation_id, call_id, arguments_hash, arguments_json in (
            (str(foreign["id"]), prior_id, "prior-call", prior_hash, prior_json),
            (str(current["id"]), current_id, "current-call", current_hash, current_args),
        ):
            await store.prepare_tool_receipt(
                operation_id=operation_id,
                run_id=run_id,
                execution_attempt_id="job:1",
                tool_call_id=call_id,
                tool_name="execute",
                tool_version="",
                arguments_hash=arguments_hash,
                arguments_json=arguments_json,
                risk="external_or_unknown",
            )
        await store.begin_tool_receipt(
            operation_id=prior_id,
            run_id=str(foreign["id"]),
            execution_attempt_id="job:1",
        )
        await store.settle_tool_receipt(
            operation_id=prior_id,
            run_id=str(foreign["id"]),
            status="outcome_unknown",
        )
        await store.create_tool_reconciliation(
            run_id=str(current["id"]),
            operation_id=current_id,
            wait_cycle_id="wait-foreign",
            interrupt_id="interrupt-foreign",
            payload={"operation_id": current_id, "prior_operation_id": prior_id},
        )
        result_json = serialize_tool_result(
            ToolMessage(content="confirmed", name="execute", tool_call_id="current-call")
        )
        with pytest.raises(WaitDecisionConflictError):
            await store.resolve_tool_reconciliation(
                current_id,
                decision="confirmed_completed",
                current_result_json=result_json,
                current_result_hash="hash",
                prior_result_json=result_json,
                prior_result_hash="hash",
            )
        assert (await store.get_tool_receipt(prior_id))["status"] == "outcome_unknown"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_expired_execution_lease_marks_running_tool_outcome_unknown(
    tmp_path: Path,
) -> None:
    store, run = await _store_and_run(tmp_path)
    run_id = str(run["id"])
    try:
        job = await store.enqueue_run_job(run_id, kind="start")
        assert job is not None
        leased = await store.claim_run_job(worker_id="worker-1", lease_seconds=-1)
        assert leased is not None
        attempt_id = f"{leased['id']}:{leased['lease_generation']}"
        operation_id, arguments_hash, arguments_json = tool_operation_identity(
            run_id=run_id,
            tool_call_id="call-expired",
            tool_name="execute",
            arguments={"command": "side-effect"},
        )
        await store.prepare_tool_receipt(
            operation_id=operation_id,
            run_id=run_id,
            execution_attempt_id=attempt_id,
            tool_call_id="call-expired",
            tool_name="execute",
            arguments_hash=arguments_hash,
            arguments_json=arguments_json,
            risk="sandboxed_command",
        )
        await store.begin_tool_receipt(
            operation_id=operation_id,
            run_id=run_id,
            execution_attempt_id=attempt_id,
        )

        await store.claim_run_job(worker_id="worker-2", lease_seconds=30)

        receipt = await store.get_tool_receipt(operation_id)
        assert receipt is not None
        assert receipt["status"] == "outcome_unknown"
        assert receipt["error_type"] == "execution_lease_expired"
    finally:
        await store.close()
