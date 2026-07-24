from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import tool
from langgraph.graph import START, MessagesState, StateGraph

from shejane_runtime.auth import LOCAL_OWNER_PRINCIPAL_ID
from shejane_runtime.llm.ledger import (
    LedgerChatModel,
    ModelContextBudgetExceeded,
    _conservative_token_count,
    _enforce_context_envelope,
    _estimate_tool_tokens,
    _provider_tools,
    _truncate_large_message,
)
from shejane_runtime.store.sqlite import LocalStore, ModelCallBudgetExceeded


def test_tool_token_estimate_is_local_and_conservative() -> None:
    tool_schema = {
        "name": "lookup",
        "description": "查询天气",
        "input_schema": {"type": "object"},
    }

    estimated = _estimate_tool_tokens([tool_schema])

    payload = '[{"name": "lookup", "description": "查询天气", "input_schema": {"type": "object"}}]'
    assert estimated == math.ceil(len(payload.encode("utf-8")) / 2)


class _StreamingModel(BaseChatModel):
    fail_after_output: bool = False

    @property
    def _llm_type(self) -> str:
        return "ledger-test"

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: object,
    ) -> AsyncIterator[ChatGenerationChunk]:
        yield ChatGenerationChunk(message=AIMessageChunk(content="hello"))
        if self.fail_after_output:
            raise TimeoutError("provider disconnected")
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="",
                usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
                response_metadata={"request_id": "provider-1"},
            )
        )

    async def _agenerate(self, *args: object, **kwargs: object) -> ChatResult:
        raise NotImplementedError

    def _generate(self, *args: object, **kwargs: object) -> ChatResult:
        raise NotImplementedError


class _BindableStreamingModel(_StreamingModel):
    bound_tool_names: tuple[str, ...] = ()

    def bind_tools(
        self,
        tools: Sequence[object],
        *,
        tool_choice: str | None = None,
        **kwargs: object,
    ) -> BaseChatModel:
        del tool_choice, kwargs
        return self.model_copy(
            update={
                "bound_tool_names": tuple(
                    str(item["function"]["name"])  # type: ignore[index]
                    for item in tools
                )
            }
        )


class _RunnableBindingStreamingModel(_StreamingModel):
    def bind_tools(
        self,
        tools: Sequence[object],
        *,
        tool_choice: str | None = None,
        **kwargs: object,
    ) -> BaseChatModel:
        return self.bind(tools=tools, tool_choice=tool_choice, **kwargs)  # type: ignore[return-value]


class _StrictToolStreamingModel(_StreamingModel):
    bound_tool_name: str = ""

    def bind_tools(
        self,
        tools: Sequence[object],
        *,
        tool_choice: str | None = None,
        **kwargs: object,
    ) -> BaseChatModel:
        del tool_choice, kwargs
        function = tools[0]["function"]  # type: ignore[index]
        name = str(function["name"])
        assert re.fullmatch(r"[a-zA-Z0-9_-]+", name)
        return self.model_copy(update={"bound_tool_name": name})

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: object,
    ) -> AsyncIterator[ChatGenerationChunk]:
        del messages, stop, run_manager, kwargs
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {"name": self.bound_tool_name, "args": "{}", "id": "call-1", "index": 0}
                ],
            )
        )


class _SlowReviewModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "slow-review-test"

    async def _agenerate(self, *args: object, **kwargs: object) -> ChatResult:
        del args, kwargs
        await asyncio.sleep(60)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="allow"))])

    def _generate(self, *args: object, **kwargs: object) -> ChatResult:
        raise NotImplementedError


@tool("office.read")
def _office_read(path: str) -> str:
    """Read an Office document."""
    return path


@tool("workspace.read")
def _workspace_read(path: str) -> str:
    """Read a workspace file."""
    return path


@tool("workspace_read")
def _workspace_read_legacy(path: str) -> str:
    """Read a workspace file through a legacy safe name."""
    return path


async def _store_and_run(tmp_path: Path) -> tuple[LocalStore, dict[str, object]]:
    store = await LocalStore.open(tmp_path / "runtime.db")
    run = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="test",
        workspace_path=None,
    )
    return store, run


@pytest.mark.asyncio
async def test_stream_settles_usage_without_reading_sse(tmp_path: Path) -> None:
    store, run = await _store_and_run(tmp_path)
    try:
        model = LedgerChatModel(
            delegate=_StreamingModel(),
            store=store,
            run_id=str(run["id"]),
            execution_attempt_id="job-1:1",
            model_name="local:test:model",
            max_calls=2,
            profile={"max_input_tokens": 8_192},
        )

        chunks = [chunk async for chunk in model.astream([HumanMessage(content="hi")])]

        assert "".join(str(chunk.content) for chunk in chunks) == "hello"
        assert await store.model_usage_summary(str(run["id"])) == {
            "input_tokens": 7,
            "output_tokens": 3,
            "unmetered_calls": 0,
            "outcome_unknown_calls": 0,
            "model_calls": 1,
        }
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_graph_message_stream_exposes_only_ledger_model_chunks(tmp_path: Path) -> None:
    store, run = await _store_and_run(tmp_path)
    try:
        model = LedgerChatModel(
            delegate=_RunnableBindingStreamingModel(),
            store=store,
            run_id=str(run["id"]),
            execution_attempt_id="job-1:1",
            model_name="local:test:model",
            max_calls=2,
            profile={"max_input_tokens": 8_192},
        ).bind_tools([_workspace_read])

        async def call_model(state: MessagesState) -> dict[str, list[BaseMessage]]:
            return {"messages": [await model.ainvoke(state["messages"])]}

        graph_builder = StateGraph(MessagesState)
        graph_builder.add_node("model", call_model)
        graph_builder.add_edge(START, "model")
        graph = graph_builder.compile()

        chunks = []
        async for part in graph.astream(
            {"messages": [HumanMessage(content="hi")]},
            stream_mode=["updates", "messages"],
            version="v2",
        ):
            if part["type"] == "messages":
                chunks.append(part["data"][0])

        assert [chunk.content for chunk in chunks if chunk.content] == ["hello"]
        assert sum(bool(chunk.usage_metadata) for chunk in chunks) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_failure_after_visible_output_is_outcome_unknown(tmp_path: Path) -> None:
    store, run = await _store_and_run(tmp_path)
    try:
        model = LedgerChatModel(
            delegate=_StreamingModel(fail_after_output=True),
            store=store,
            run_id=str(run["id"]),
            execution_attempt_id="job-1:1",
            model_name="local:test:model",
            max_calls=2,
            profile={"max_input_tokens": 8_192},
        )

        with pytest.raises(TimeoutError):
            _ = [chunk async for chunk in model.astream([HumanMessage(content="hi")])]

        usage = await store.model_usage_summary(str(run["id"]))
        assert usage["outcome_unknown_calls"] == 1
        assert usage["model_calls"] == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_canceled_read_only_review_is_not_an_unknown_agent_outcome(tmp_path: Path) -> None:
    store, run = await _store_and_run(tmp_path)
    try:
        model = LedgerChatModel(
            delegate=_SlowReviewModel(),
            store=store,
            run_id=str(run["id"]),
            execution_attempt_id="job-1:1",
            model_name="local:test:model",
            max_calls=1,
            call_purpose="completion_review",
            profile={"max_input_tokens": 8_192},
        )

        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.01):
                await model.ainvoke([HumanMessage(content="review")])

        usage = await store.model_usage_summary(str(run["id"]))
        assert usage["outcome_unknown_calls"] == 0
        assert usage["model_calls"] == 1
        rows = await store.list_model_calls_for_run(str(run["id"]))
        assert rows[0]["status"] == "failed"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_model_budget_is_reserved_durably_across_attempts(tmp_path: Path) -> None:
    store, run = await _store_and_run(tmp_path)
    try:
        first = LedgerChatModel(
            delegate=_StreamingModel(),
            store=store,
            run_id=str(run["id"]),
            execution_attempt_id="job-1:1",
            model_name="local:test:model",
            max_calls=1,
            profile={"max_input_tokens": 8_192},
        )
        _ = [chunk async for chunk in first.astream([HumanMessage(content="hi")])]

        resumed = first.model_copy(update={"execution_attempt_id": "job-2:1"})
        with pytest.raises(ModelCallBudgetExceeded):
            _ = [chunk async for chunk in resumed.astream([HumanMessage(content="again")])]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_review_budgets_are_separate_but_use_the_same_ledger(tmp_path: Path) -> None:
    store, run = await _store_and_run(tmp_path)
    try:
        agent = LedgerChatModel(
            delegate=_StreamingModel(),
            store=store,
            run_id=str(run["id"]),
            execution_attempt_id="job-1:1",
            model_name="local:test:model",
            max_calls=1,
            call_purpose="agent",
            profile={"max_input_tokens": 8_192},
        )
        approval = agent.model_copy(update={"call_purpose": "approval_review", "max_calls": 1})
        clarification = agent.model_copy(
            update={"call_purpose": "clarification_review", "max_calls": 1}
        )
        completion = agent.model_copy(update={"call_purpose": "completion_review", "max_calls": 1})
        title = agent.model_copy(update={"call_purpose": "title_generation", "max_calls": 1})

        _ = [chunk async for chunk in agent.astream([HumanMessage(content="agent")])]
        _ = [chunk async for chunk in approval.astream([HumanMessage(content="approval")])]
        _ = [
            chunk async for chunk in clarification.astream([HumanMessage(content="clarification")])
        ]
        _ = [chunk async for chunk in completion.astream([HumanMessage(content="completion")])]
        _ = [chunk async for chunk in title.astream([HumanMessage(content="title")])]

        rows = await store.list_model_calls_for_run(str(run["id"]))
        assert [row["purpose"] for row in rows] == [
            "agent",
            "approval_review",
            "clarification_review",
            "completion_review",
            "title_generation",
        ]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_declared_tiny_context_fails_before_provider_call(tmp_path: Path) -> None:
    store, run = await _store_and_run(tmp_path)
    try:
        model = LedgerChatModel(
            delegate=_StreamingModel(),
            store=store,
            run_id=str(run["id"]),
            execution_attempt_id="job-1:1",
            model_name="local:test:tiny",
            max_calls=2,
            profile={"max_input_tokens": 100},
        )

        with pytest.raises(ModelContextBudgetExceeded, match="insufficient context"):
            _ = [chunk async for chunk in model.astream([HumanMessage(content="hello")])]

        assert await store.model_usage_summary(str(run["id"])) == {
            "input_tokens": 0,
            "output_tokens": 0,
            "unmetered_calls": 0,
            "outcome_unknown_calls": 0,
            "model_calls": 0,
        }
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_provider_boundary_filters_office_schema_for_plain_subagent_task(
    tmp_path: Path,
) -> None:
    store, run = await _store_and_run(tmp_path)
    try:
        unbound = LedgerChatModel(
            delegate=_BindableStreamingModel(),
            store=store,
            run_id=str(run["id"]),
            execution_attempt_id="job-1:1",
            model_name="local:test:small",
            max_calls=2,
            profile={"max_input_tokens": 8_192},
        )
        model = unbound.bind_tools([_office_read, _workspace_read])
        assert isinstance(model, LedgerChatModel)

        provider_model, schema_tokens, aliases = model._provider_model(
            [HumanMessage(content="Review this Python function and report the bug.")]
        )

        assert isinstance(provider_model, _BindableStreamingModel)
        assert len(provider_model.bound_tool_names) == 1
        assert aliases[provider_model.bound_tool_names[0]] == "workspace.read"
        assert schema_tokens < model.tool_schema_tokens
        model._bounded_messages(
            [HumanMessage(content="Review this Python function and report the bug.")],
            tool_schema_tokens=schema_tokens,
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_provider_boundary_aliases_and_restores_dotted_tool_names(tmp_path: Path) -> None:
    store, run = await _store_and_run(tmp_path)
    try:
        model = LedgerChatModel(
            delegate=_StrictToolStreamingModel(),
            store=store,
            run_id=str(run["id"]),
            execution_attempt_id="job-1:1",
            model_name="local:test:strict-tools",
            max_calls=2,
            profile={"max_input_tokens": 8_192},
        ).bind_tools([_workspace_read])

        provider_model, _, aliases = model._provider_model([HumanMessage(content="read it")])
        assert isinstance(provider_model, _StrictToolStreamingModel)
        assert provider_model.bound_tool_name == "workspace_read"
        assert aliases["workspace_read"] == "workspace.read"
        legacy_name = f"workspace_read_{hashlib.sha256(b'workspace.read').hexdigest()[:8]}"
        assert aliases[legacy_name] == "workspace.read"

        chunks = [chunk async for chunk in model.astream([HumanMessage(content="read it")])]
        assert chunks[0].tool_call_chunks[0]["name"] == "workspace.read"
    finally:
        await store.close()


def test_provider_boundary_hashes_only_colliding_tool_aliases() -> None:
    schemas, aliases, _ = _provider_tools([_workspace_read, _workspace_read_legacy])
    names = [schema["function"]["name"] for schema in schemas]

    assert names[1] == "workspace_read"
    assert names[0].startswith("workspace_read_")
    assert len(set(names)) == 2
    assert aliases[names[0]] == "workspace.read"
    assert aliases[names[1]] == "workspace_read"


def test_provider_boundary_hashes_tool_names_longer_than_wire_limit() -> None:
    original = f"plugin_{'x' * 70}"
    schemas, aliases, _ = _provider_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": original,
                    "description": "Long dynamic Plugin Tool",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
    )
    wire_name = schemas[0]["function"]["name"]

    assert len(wire_name) == 64
    assert wire_name.startswith("plugin_")
    assert aliases[wire_name] == original


@pytest.mark.asyncio
async def test_second_store_open_does_not_mutate_active_receipt(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    store, run = await _store_and_run(tmp_path)
    await store.reserve_model_call(
        run_id=str(run["id"]),
        execution_attempt_id="job-1:1",
        model="local:test:model",
        max_calls=2,
    )
    await store.close()

    reopened = await LocalStore.open(db_path)
    try:
        usage = await reopened.model_usage_summary(str(run["id"]))
        assert usage["outcome_unknown_calls"] == 0
        assert usage["model_calls"] == 1
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_expired_lease_marks_open_receipt_outcome_unknown(tmp_path: Path) -> None:
    store, run = await _store_and_run(tmp_path)
    run_id = str(run["id"])
    try:
        job = await store.enqueue_run_job(run_id, kind="start")
        assert job is not None
        leased = await store.claim_run_job(worker_id="worker-1", lease_seconds=-1)
        assert leased is not None
        attempt_id = f"{leased['id']}:{leased['lease_generation']}"
        await store.reserve_model_call(
            run_id=run_id,
            execution_attempt_id=attempt_id,
            model="local:test:model",
            max_calls=2,
        )

        assert await store.claim_run_job(worker_id="worker-2") is None

        usage = await store.model_usage_summary(run_id)
        assert usage["outcome_unknown_calls"] == 1
        quarantined = await store.get_run_job(str(leased["id"]))
        assert quarantined is not None
        assert quarantined["status"] == "leased"
        assert quarantined["quarantined_at"] is not None
        saved_run = await store.get_run(run_id)
        assert saved_run is not None and saved_run["status"] == "cleanup_required"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_model_call_updates_reject_missing_or_terminal_receipts(tmp_path: Path) -> None:
    store, run = await _store_and_run(tmp_path)
    run_id = str(run["id"])
    try:
        with pytest.raises(RuntimeError, match="missing-model-call"):
            await store.mark_model_call_output(
                run_id=run_id,
                call_id="missing-model-call",
            )

        receipt = await store.reserve_model_call(
            run_id=run_id,
            execution_attempt_id="job-1:1",
            model="local:test:model",
            max_calls=2,
        )
        await store.settle_model_call(
            run_id=run_id,
            call_id=str(receipt["id"]),
            provider_request_id=None,
            input_tokens=1,
            output_tokens=1,
        )

        with pytest.raises(RuntimeError, match=str(receipt["id"])):
            await store.fail_model_call(
                run_id=run_id,
                call_id=str(receipt["id"]),
                outcome_unknown=False,
            )
    finally:
        await store.close()


def test_context_envelope_bounds_single_oversized_message_with_explicit_marker() -> None:
    messages = [HumanMessage(content="A" * 900_000)]

    bounded = _enforce_context_envelope(messages, max_tokens=8_000)

    assert _conservative_token_count(bounded) <= 8_000
    joined = "\n".join(str(message.content) for message in bounded)
    assert "Runtime truncated" in joined or "Runtime context envelope" in joined


def test_context_envelope_bounds_large_history_for_summary_calls() -> None:
    messages = [HumanMessage(content=f"turn-{index}:" + ("x" * 20_000)) for index in range(80)]

    bounded = _enforce_context_envelope(messages, max_tokens=32_000)

    assert _conservative_token_count(bounded) <= 32_000
    assert any("Runtime context envelope" in str(message.content) for message in bounded)
    assert "turn-79" in str(bounded[-1].content)


def test_context_envelope_uses_conservative_count_for_chinese() -> None:
    messages = [HumanMessage(content="中" * 30_000)]

    bounded = _enforce_context_envelope(messages, max_tokens=8_000)

    assert _conservative_token_count(bounded) <= 8_000
    assert "Runtime truncated" in str(bounded[0].content)


def test_context_envelope_preserves_short_system_blocks() -> None:
    message = SystemMessage(
        content=[
            {"type": "text", "text": "A" * 4_000},
            {
                "type": "text",
                "text": "Read `/skills/example/SKILL.md` before answering.",
            },
            {"type": "text", "text": "B" * 4_000},
        ]
    )

    bounded = _truncate_large_message(message, max_chars=3_000)

    assert isinstance(bounded.content, list)
    assert bounded.content[1]["text"] == "Read `/skills/example/SKILL.md` before answering."
    assert "Runtime truncated" in bounded.content[0]["text"]
    assert "Runtime truncated" in bounded.content[2]["text"]


def test_context_envelope_preserves_latest_tool_result_with_block_system_prompt() -> None:
    messages = [
        SystemMessage(
            content=[
                {"type": "text", "text": "IDENTITY_RULE\n" + "I" * 7_000},
                {"type": "text", "text": "DEEP_AGENT_RULE\n" + "D" * 7_000},
                {"type": "text", "text": "TODO_RULE\n" + "T" * 7_000},
                {"type": "text", "text": "MEMORY_RULE\n" + "M" * 3_000},
            ]
        ),
        HumanMessage(content="Use the Skill."),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "call-skill",
                    "name": "read_file",
                    "args": {"file_path": "/skills/example/SKILL.md"},
                }
            ],
        ),
        ToolMessage(
            content="E2E_SKILL_ACTIVE",
            name="read_file",
            tool_call_id="call-skill",
        ),
    ]

    bounded = _enforce_context_envelope(messages, max_tokens=16_810)

    assert _conservative_token_count(bounded) <= 16_810
    assert any(
        isinstance(message, ToolMessage) and message.content == "E2E_SKILL_ACTIVE"
        for message in bounded
    )
    system_text = "".join(
        str(block.get("text") or "")
        for message in bounded
        if isinstance(message, SystemMessage) and isinstance(message.content, list)
        for block in message.content
        if isinstance(block, dict)
    )
    assert all(
        marker in system_text
        for marker in ("IDENTITY_RULE", "DEEP_AGENT_RULE", "TODO_RULE", "MEMORY_RULE")
    )


def test_context_envelope_keeps_parallel_tool_results_with_their_parent_call() -> None:
    tool_calls = [
        {"id": f"read-{index}", "name": "read_file", "args": {"file_path": f"/{index}.jpg"}}
        for index in range(7)
    ]
    messages = [
        SystemMessage(content="SYSTEM_RULE\n" + "S" * 7_000),
        HumanMessage(content="Rename these files from their contents."),
        AIMessage(content="", tool_calls=tool_calls),
        *[
            ToolMessage(
                content=f"result-{index}:" + "x" * 32_000,
                name="read_file",
                tool_call_id=f"read-{index}",
            )
            for index in range(7)
        ],
    ]

    bounded = _enforce_context_envelope(messages, max_tokens=16_810)

    active_tool_calls: set[str] = set()
    for message in bounded:
        if isinstance(message, AIMessage):
            active_tool_calls = {str(call["id"]) for call in message.tool_calls}
        elif isinstance(message, ToolMessage):
            assert message.tool_call_id in active_tool_calls
        elif not isinstance(message, SystemMessage):
            active_tool_calls = set()
    assert any(isinstance(message, ToolMessage) for message in bounded)
