from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from shejane_runtime.agent.context_builder import RuntimeContext
from shejane_runtime.middleware.budget_control import DynamicBudgetControlMiddleware


class _BudgetStore:
    def __init__(self, calls: int) -> None:
        self.calls = calls

    async def model_call_budget_status(self, run_id: str, *, purpose: str) -> dict[str, int]:
        assert run_id == "run-budget"
        assert purpose == "agent"
        return {"model_calls": self.calls, "input_tokens": 0, "output_tokens": 0}


@dataclass
class _Request:
    runtime: Any
    messages: list[Any]
    system_message: SystemMessage
    tools: tuple[str, ...] = ("read_file", "web.fetch")

    def override(self, **changes: Any) -> _Request:
        return replace(self, **changes)


def _request(
    *,
    calls: int,
    messages: list[Any],
    hard_limit: int = 100,
    soft_limit: int = 12,
    final_reserve: int = 2,
) -> _Request:
    return _Request(
        runtime=SimpleNamespace(
            context=RuntimeContext(
                run_id="run-budget",
                store=_BudgetStore(calls),
                model_call_soft_limit=soft_limit,
                model_call_hard_limit=hard_limit,
                model_call_final_reserve=final_reserve,
            )
        ),
        messages=messages,
        system_message=SystemMessage(content="base prompt"),
    )


async def _identity(value: _Request) -> _Request:
    return value


@pytest.mark.asyncio
async def test_soft_budget_warns_without_disabling_tools() -> None:
    request = _request(
        calls=12,
        messages=[
            HumanMessage(content="research", additional_kwargs={"runtime_kind": "task_input"})
        ],
    )

    controlled = await DynamicBudgetControlMiddleware().awrap_model_call(
        request,
        _identity,
    )

    assert list(controlled.tools) == ["read_file", "web.fetch"]
    assert "<runtime-budget>" in str(controlled.system_message.content)
    assert "soft budget" in str(controlled.system_message.content).lower()
    assert "88 model calls remain" in str(controlled.system_message.content)


@pytest.mark.asyncio
async def test_repeated_read_loop_forces_a_final_answer_with_tools_hidden() -> None:
    messages: list[Any] = [
        HumanMessage(content="research", additional_kwargs={"runtime_kind": "task_input"})
    ]
    for index in range(3):
        messages.extend(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {"file_path": "/conversation_history/run.md"},
                            "id": f"call-{index}",
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(content="history", name="read_file", tool_call_id=f"call-{index}"),
            ]
        )
    request = _request(calls=12, messages=messages)

    controlled = await DynamicBudgetControlMiddleware().awrap_model_call(
        request,
        _identity,
    )

    assert controlled.tools == []
    rendered = str(controlled.system_message.content)
    assert "convergence mode" in rendered.lower()
    assert "read_file" in rendered
    assert "best available final answer" in rendered.lower()


@pytest.mark.asyncio
async def test_hard_limit_reserves_the_last_two_calls_for_finalization() -> None:
    request = _request(
        # Each logical model turn may consume the initial provider attempt plus
        # two durable retries, so two final turns require six ledger slots.
        calls=94,
        messages=[
            HumanMessage(content="research", additional_kwargs={"runtime_kind": "task_input"})
        ],
    )

    controlled = await DynamicBudgetControlMiddleware().awrap_model_call(
        request,
        _identity,
    )

    assert controlled.tools == []
    assert "6 model calls remain" in str(controlled.system_message.content)


@pytest.mark.asyncio
async def test_retry_reserve_does_not_hide_tools_one_call_early() -> None:
    request = _request(
        calls=93,
        messages=[
            HumanMessage(content="research", additional_kwargs={"runtime_kind": "task_input"})
        ],
    )

    controlled = await DynamicBudgetControlMiddleware().awrap_model_call(request, _identity)

    assert list(controlled.tools) == ["read_file", "web.fetch"]
    assert "soft budget" in str(controlled.system_message.content).lower()


@pytest.mark.asyncio
async def test_synchronous_delegation_closes_before_the_final_reserve() -> None:
    request = replace(
        _request(
            calls=93,
            messages=[HumanMessage(content="finish")],
        ),
        tools=("read_file", "task", "team.run"),
    )

    controlled = await DynamicBudgetControlMiddleware().awrap_model_call(request, _identity)

    assert controlled.tools == ["read_file"]
    assert "synchronous delegation is unavailable" in str(controlled.system_message.content).lower()


@pytest.mark.asyncio
async def test_soft_budget_closes_delegation_but_keeps_ordinary_tools() -> None:
    request = replace(
        _request(
            calls=12,
            messages=[HumanMessage(content="continue")],
        ),
        tools=("read_file", "web.fetch", "task", "team.run"),
    )

    controlled = await DynamicBudgetControlMiddleware().awrap_model_call(request, _identity)

    assert controlled.tools == ["read_file", "web.fetch"]


@pytest.mark.asyncio
@pytest.mark.parametrize("hard_limit", range(2, 8))
async def test_small_hard_budget_keeps_the_first_turn_available_for_tools(
    hard_limit: int,
) -> None:
    request = _request(
        calls=0,
        messages=[HumanMessage(content="use a tool")],
        hard_limit=hard_limit,
        soft_limit=hard_limit,
    )

    controlled = await DynamicBudgetControlMiddleware().awrap_model_call(request, _identity)

    assert list(controlled.tools) == ["read_file", "web.fetch"]


@pytest.mark.asyncio
@pytest.mark.parametrize("hard_limit", range(2, 8))
async def test_small_hard_budget_hides_model_backed_delegation(hard_limit: int) -> None:
    request = replace(
        _request(
            calls=0,
            messages=[HumanMessage(content="delegate and read")],
            hard_limit=hard_limit,
            soft_limit=hard_limit,
        ),
        tools=("read_file", "task"),
    )

    controlled = await DynamicBudgetControlMiddleware().awrap_model_call(request, _identity)

    assert controlled.tools == ["read_file"]


@pytest.mark.asyncio
async def test_one_call_hard_budget_converges_immediately() -> None:
    request = _request(
        calls=0,
        messages=[HumanMessage(content="answer")],
        hard_limit=1,
        soft_limit=1,
        final_reserve=1,
    )

    controlled = await DynamicBudgetControlMiddleware().awrap_model_call(request, _identity)

    assert controlled.tools == []


@pytest.mark.asyncio
async def test_tool_guard_blocks_guessed_calls_during_subagent_convergence() -> None:
    request = SimpleNamespace(
        state={"budget_control": {"mode": "converge", "run_id": "run-budget"}},
        tool_call={"id": "guessed", "name": "read_file", "args": {}},
    )

    async def must_not_execute(_request: Any) -> Any:
        raise AssertionError("convergence tool call reached its handler")

    result = await DynamicBudgetControlMiddleware().awrap_tool_call(request, must_not_execute)

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "not executed" in str(result.content)


@pytest.mark.asyncio
async def test_tool_guard_rejects_task_batch_that_cannot_fit_before_subagent_cap() -> None:
    calls = [
        {
            "id": f"task-{index}",
            "name": "task",
            "args": {"description": f"work {index}", "subagent_type": "researcher"},
            "type": "tool_call",
        }
        for index in range(2)
    ]
    request = SimpleNamespace(
        state={
            "budget_control": {
                "mode": "warn",
                "used": 92,
                "delegation_model_call_limit": 94,
            },
            "messages": [AIMessage(content="", tool_calls=calls)],
        },
        tool_call=calls[0],
    )

    async def must_not_execute(_request: Any) -> Any:
        raise AssertionError("oversized task batch reached its handler")

    result = await DynamicBudgetControlMiddleware().awrap_tool_call(request, must_not_execute)

    assert isinstance(result, ToolMessage)
    assert result.status == "error"


@pytest.mark.asyncio
async def test_tool_guard_allows_team_fanout_when_every_member_fits() -> None:
    call = {
        "id": "team-1",
        "name": "team.run",
        "args": {"assignments": [{"id": str(index)} for index in range(5)]},
        "type": "tool_call",
    }
    request = SimpleNamespace(
        state={
            "budget_control": {
                "mode": "warn",
                "used": 88,
                "delegation_model_call_limit": 94,
            },
            "messages": [AIMessage(content="", tool_calls=[call])],
        },
        tool_call=call,
    )

    async def execute(_request: Any) -> ToolMessage:
        return ToolMessage(content="ok", name="team.run", tool_call_id="team-1")

    result = await DynamicBudgetControlMiddleware().awrap_tool_call(request, execute)

    assert isinstance(result, ToolMessage)
    assert result.status != "error"


@pytest.mark.asyncio
async def test_repeated_read_warning_below_soft_limit_does_not_claim_budget_exhaustion() -> None:
    messages = [HumanMessage(content="research", additional_kwargs={"runtime_kind": "task_input"})]
    for index in range(2):
        messages.extend(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "web.search",
                            "args": {"query": "same"},
                            "id": f"call-{index}",
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(content="result", name="web.search", tool_call_id=f"call-{index}"),
            ]
        )
    request = _request(calls=4, messages=messages)

    controlled = await DynamicBudgetControlMiddleware().awrap_model_call(request, _identity)

    rendered = str(controlled.system_message.content).lower()
    assert controlled.tools == ("read_file", "web.fetch")
    assert "repeated with identical arguments" in rendered
    assert "crossed its" not in rendered


@pytest.mark.asyncio
async def test_write_action_resets_the_repeated_read_window() -> None:
    messages = [HumanMessage(content="research", additional_kwargs={"runtime_kind": "task_input"})]
    for index in range(3):
        messages.extend(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {"file_path": "/work/source.txt"},
                            "id": f"read-{index}",
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(content="source", name="read_file", tool_call_id=f"read-{index}"),
            ]
        )
    messages.extend(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"file_path": "/work/output.txt", "content": "done"},
                        "id": "write-1",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(content="ok", name="write_file", tool_call_id="write-1"),
        ]
    )
    request = _request(calls=4, messages=messages)

    controlled = await DynamicBudgetControlMiddleware().awrap_model_call(request, _identity)

    assert controlled.tools == ("read_file", "web.fetch")
    assert controlled.system_message.content == "base prompt"
