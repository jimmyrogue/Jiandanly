"""Tests for PlanFirstMiddleware — Plan & Execute mode."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from shejane_runtime.middleware.plan_first import (
    AUTO_COMPLEX_CHARS,
    PlanFirstMiddleware,
    _looks_complex,
)

# --- mode parsing ---


def test_mode_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("SHEJANE_PLAN_FIRST", raising=False)
    mw = PlanFirstMiddleware()
    assert mw.mode == "off"


def test_mode_always_synonyms(monkeypatch) -> None:
    for val in ("1", "true", "yes", "on", "always", "ALWAYS"):
        monkeypatch.setenv("SHEJANE_PLAN_FIRST", val)
        mw = PlanFirstMiddleware()
        assert mw.mode == "always", f"value {val!r} should map to always"


def test_mode_auto(monkeypatch) -> None:
    monkeypatch.setenv("SHEJANE_PLAN_FIRST", "auto")
    mw = PlanFirstMiddleware()
    assert mw.mode == "auto"


def test_mode_explicit_constructor_overrides_env(monkeypatch) -> None:
    monkeypatch.setenv("SHEJANE_PLAN_FIRST", "off")
    mw = PlanFirstMiddleware(mode="always")
    assert mw.mode == "always"


# --- _looks_complex heuristic ---


def test_looks_complex_long_message() -> None:
    long_text = "a" * (AUTO_COMPLEX_CHARS + 1)
    assert _looks_complex([HumanMessage(content=long_text)]) is True


def test_looks_complex_short_message_with_hint() -> None:
    assert _looks_complex([HumanMessage(content="please research X")]) is True


def test_explicit_internet_research_is_complex() -> None:
    assert _looks_complex([HumanMessage(content="你查一下互联网，核对这些银行的开户要求")])


def test_looks_complex_short_simple_message() -> None:
    assert _looks_complex([HumanMessage(content="what time is it?")]) is False


def test_informational_how_to_question_is_not_complex_execution() -> None:
    assert (
        _looks_complex([HumanMessage(content="帮我搜索一下 swift 如何实现 view 的内阴影？")])
        is False
    )


def test_looks_complex_no_user_message() -> None:
    assert _looks_complex([]) is False


def test_looks_complex_uses_current_run_input_in_a_multi_turn_conversation() -> None:
    messages = [
        HumanMessage(content="hi"),
        HumanMessage(
            content="请实现一个完整的文件导入流程",
            additional_kwargs={"runtime_kind": "task_input", "runtime_run_id": "run-2"},
        ),
    ]
    assert _looks_complex(messages) is True


def test_old_complex_turn_does_not_force_a_new_trivial_follow_up() -> None:
    messages = [
        HumanMessage(content="please research and implement a complete solution"),
        HumanMessage(
            content="谢谢",
            additional_kwargs={"runtime_kind": "task_input", "runtime_run_id": "run-2"},
        ),
    ]
    assert _looks_complex(messages) is False


def test_explicit_single_tool_operation_is_not_forced_into_a_multi_task_plan() -> None:
    message = HumanMessage(
        content=(
            "Use the office.read Tool. Call exactly the office.read tool once with exactly "
            'these JSON arguments: {"path":"/report.docx"}. Do not change any argument.'
        )
    )
    assert _looks_complex([message]) is False


@pytest.mark.parametrize(
    "content",
    [
        (
            "Call write_todos with one todo whose content is E2E_TODO_ACTIVE and status "
            "is in_progress. Include E2E_TODO_ACTIVE in the final answer."
        ),
        (
            'Call user.ask exactly once with question "Choose an E2E option" and options '
            '["Option A", "Option B"]. After the answer, include the selected option in '
            "the final response."
        ),
        (
            'You must call the task tool with subagent_type "writer". Ask the subagent to '
            "return exactly E2E_SUBAGENT_RESULT, then include that exact token in your final answer."
        ),
    ],
)
def test_explicit_one_tool_contract_is_not_misclassified_as_complex(content: str) -> None:
    assert _looks_complex([HumanMessage(content=content)]) is False


# --- before_agent behavior ---


def test_off_mode_does_not_inject() -> None:
    mw = PlanFirstMiddleware(mode="off")
    state = {"messages": [HumanMessage(content="research market trends in Q4 2026")]}
    assert mw.before_agent(state, runtime=None) is None


def test_always_mode_marks_runtime_state_without_injecting_prompt_text() -> None:
    mw = PlanFirstMiddleware(mode="always")
    state = {"messages": [HumanMessage(content="hi")]}
    result = mw.before_agent(state, runtime=None)
    assert result is not None
    assert "messages" not in result
    assert result["incremental_execution"] == {
        "required": True,
        "mode": "always",
        "run_id": "",
        "repairs": {},
    }


def test_auto_mode_skips_trivial_task() -> None:
    mw = PlanFirstMiddleware(mode="auto")
    state = {"messages": [HumanMessage(content="what time is it?")]}
    assert mw.before_agent(state, runtime=None) is None


def test_auto_mode_triggers_on_complex_task() -> None:
    mw = PlanFirstMiddleware(mode="auto")
    state = {
        "messages": [HumanMessage(content="please research the latest LangGraph release notes")]
    }
    result = mw.before_agent(state, runtime=None)
    assert result is not None
    assert result["incremental_execution"]["required"] is True


def test_auto_mode_uses_frozen_execution_policy_instead_of_reclassifying_goal() -> None:
    mw = PlanFirstMiddleware(mode="auto")
    state = {"messages": [HumanMessage(content="please research and compare many sources")]}
    runtime = SimpleNamespace(
        context=SimpleNamespace(
            run_id="run-frozen",
            execution_policy={"complexity": "simple"},
        )
    )

    assert mw.before_agent(state, runtime) is None


def test_chinese_complex_task_uses_incremental_execution() -> None:
    mw = PlanFirstMiddleware(mode="auto")
    result = mw.before_agent(
        {"messages": [HumanMessage(content="请创建一个对对碰游戏并保存到桌面")]},
        runtime=None,
    )
    assert result is not None
    assert result["incremental_execution"]["required"] is True
