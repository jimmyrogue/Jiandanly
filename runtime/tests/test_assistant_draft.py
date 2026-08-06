from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from shejane_runtime.auth import LOCAL_OWNER_PRINCIPAL_ID
from shejane_runtime.runs import (
    _assistant_draft_from_state,
    _assistant_draft_from_update,
    _assistant_round_from_update,
)
from shejane_runtime.store.sqlite import LocalStore


def test_extracts_complete_ai_message_not_user_or_tool_state() -> None:
    message = AIMessage(
        id="ai-1",
        content="complete answer",
        tool_calls=[{"id": "call-1", "name": "time.now", "args": {}, "type": "tool_call"}],
    )

    draft = _assistant_draft_from_update({"model": {"messages": [message]}})

    assert draft is not None
    assert draft["content"] == "complete answer"
    assert draft["tool_calls"][0]["name"] == "time.now"


def test_extracts_durable_round_identity_and_tool_call_ids() -> None:
    message = AIMessage(
        content="I’ll inspect the file.",
        additional_kwargs={"runtime_model_call_id": "model-call-1"},
        tool_calls=[{"id": "call-1", "name": "read_file", "args": {}, "type": "tool_call"}],
    )

    round_payload = _assistant_round_from_update(
        {"model": {"messages": [message]}},
        allow_reasoning_summary=True,
    )

    assert round_payload == {
        "round_id": "model-call-1",
        "text": "I’ll inspect the file.",
        "reasoning_summary": None,
        "tool_call_ids": ["call-1"],
    }


def test_extracts_only_explicit_provider_reasoning_summaries() -> None:
    message = AIMessage(
        content=[
            {
                "type": "reasoning",
                "summary": [
                    {"type": "summary_text", "text": "I will inspect the repository first."}
                ],
            },
            {"type": "text", "text": "I’ll inspect the file."},
        ],
        additional_kwargs={"runtime_model_call_id": "model-call-summary"},
        response_metadata={"model_provider": "openai"},
        tool_calls=[{"id": "call-1", "name": "read_file", "args": {}, "type": "tool_call"}],
    )

    round_payload = _assistant_round_from_update(
        {"model": {"messages": [message]}},
        allow_reasoning_summary=True,
    )

    assert round_payload is not None
    assert round_payload["reasoning_summary"] == "I will inspect the repository first."


def test_does_not_expose_generic_reasoning_blocks_as_summaries() -> None:
    message = AIMessage(
        content=[{"type": "reasoning", "reasoning": "private chain of thought"}],
        additional_kwargs={"runtime_model_call_id": "model-call-private"},
        tool_calls=[{"id": "call-1", "name": "read_file", "args": {}, "type": "tool_call"}],
    )

    round_payload = _assistant_round_from_update(
        {"model": {"messages": [message]}},
        allow_reasoning_summary=True,
    )

    assert round_payload is not None
    assert round_payload["reasoning_summary"] is None


def test_rejects_explicit_summary_from_an_unknown_provider() -> None:
    message = AIMessage(
        content=[
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "Untrusted summary."}],
            }
        ],
        additional_kwargs={"runtime_model_call_id": "model-call-unknown"},
        response_metadata={"model_provider": "compatible_proxy"},
        tool_calls=[{"id": "call-1", "name": "read_file", "args": {}, "type": "tool_call"}],
    )

    round_payload = _assistant_round_from_update(
        {"model": {"messages": [message]}},
        allow_reasoning_summary=True,
    )

    assert round_payload is not None
    assert round_payload["reasoning_summary"] is None


def test_final_draft_keeps_text_from_tool_call_rounds() -> None:
    run_id = "run-current"
    state = {
        "messages": [
            AIMessage(content="previous conversation"),
            HumanMessage(
                content="inspect web fetch",
                additional_kwargs={
                    "runtime_kind": "task_input",
                    "runtime_run_id": run_id,
                },
            ),
            AIMessage(
                id="ai-research",
                content="The implementation has three layers.",
                tool_calls=[{"id": "call-1", "name": "grep", "args": {}, "type": "tool_call"}],
            ),
            ToolMessage(content="result", tool_call_id="call-1"),
            AIMessage(id="ai-final", content="Ask me if you want a live verification."),
        ]
    }

    draft = _assistant_draft_from_state(state, run_id=run_id)

    assert draft is not None
    assert draft["content"] == (
        "The implementation has three layers.\n\nAsk me if you want a live verification."
    )
    assert draft["tool_calls"] == []


@pytest.mark.asyncio
async def test_draft_update_is_idempotent_and_revisions_are_monotonic(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    run = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="test",
        workspace_path=None,
    )
    run_id = str(run["id"])
    try:
        first = await store.update_assistant_draft(
            run_id=run_id,
            message_key="one",
            content="first",
            tool_calls=[],
        )
        replay = await store.update_assistant_draft(
            run_id=run_id,
            message_key="one",
            content="first",
            tool_calls=[],
        )
        second = await store.update_assistant_draft(
            run_id=run_id,
            message_key="two",
            content="second",
            tool_calls=[],
        )

        assert first["revision"] == replay["revision"] == 1
        assert second["revision"] == 2
        assert (await store.get_assistant_draft(run_id))["content"] == "second"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_assistant_round_commit_is_idempotent(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    run = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="test",
        workspace_path=None,
    )
    run_id = str(run["id"])
    payload = {
        "round_id": "model-call-1",
        "text": "I’ll inspect the file.",
        "reasoning_summary": None,
        "tool_call_ids": ["call-1"],
    }
    try:
        first, first_created = await store.commit_assistant_round(run_id, payload)
        replay, replay_created = await store.commit_assistant_round(run_id, payload)
        edited_replay, edited_replay_created = await store.commit_assistant_round(
            run_id,
            {**payload, "tool_call_ids": ["call-1__edit_replacement"]},
        )

        events = await store.events_since(run_id)
        assert first_created is True
        assert replay_created is False
        assert edited_replay_created is False
        assert replay["id"] == first["id"]
        assert edited_replay["id"] == first["id"]
        assert [event["event_type"] for event in events] == ["assistant.round.committed"]
    finally:
        await store.close()
