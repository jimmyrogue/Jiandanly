from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langchain.tools import ToolRuntime

from shejane_runtime.agent.context_builder import RuntimeContext
from shejane_runtime.agent.mailbox import AgentMailboxControl, build_agent_mailbox_tools
from shejane_runtime.middleware.agent_mailbox import AgentMailboxMiddleware
from shejane_runtime.tools.runtime import RuntimeToolExecution, bind_runtime_tool_execution


def _runtime(control: AgentMailboxControl) -> tuple[RuntimeContext, ToolRuntime[Any, Any]]:
    context = RuntimeContext(run_id="child-a", agent_mailbox_control=control)
    runtime = ToolRuntime(
        state={},
        context=context,
        config={},
        stream_writer=lambda _chunk: None,
        tool_call_id="call-mailbox",
        store=None,
    )
    return context, runtime


@pytest.mark.asyncio
async def test_mailbox_tools_keep_sender_and_operation_identity_out_of_model_control() -> None:
    calls: list[tuple[str, object]] = []
    message = {"id": "message-1", "status": "queued"}

    async def send(*args: object) -> dict[str, Any]:
        calls.append(("send", args))
        return message

    async def reply(*args: object) -> dict[str, Any]:
        calls.append(("reply", args))
        return {**message, "id": "message-2"}

    async def inbox(run_id: str) -> list[dict[str, Any]]:
        calls.append(("inbox", run_id))
        return [message]

    async def ack(*args: object) -> list[dict[str, Any]]:
        calls.append(("ack", args))
        return [{**message, "status": "acknowledged"}]

    control = AgentMailboxControl(send=send, reply=reply, inbox=inbox, ack=ack)
    context, runtime = _runtime(control)
    tools = {tool.name: tool for tool in build_agent_mailbox_tools()}

    with bind_runtime_tool_execution(
        RuntimeToolExecution(
            context=context,
            operation_id="toolop-send",
            tool_call_id="call-mailbox",
        )
    ):
        sent = await tools["mailbox.send"].coroutine(  # type: ignore[misc]
            recipient_run_id="child-b",
            kind="question",
            text="What did you find?",
            data={"claim": 1},
            artifact_refs=[],
            ttl_seconds=3600,
            runtime=runtime,
        )
    with bind_runtime_tool_execution(
        RuntimeToolExecution(
            context=context,
            operation_id="toolop-reply",
            tool_call_id="call-mailbox",
        )
    ):
        replied = await tools["mailbox.reply"].coroutine(  # type: ignore[misc]
            in_reply_to="message-1",
            kind="result",
            text="Primary source found.",
            runtime=runtime,
        )
    assert await tools["mailbox.inbox"].coroutine(runtime=runtime) == {  # type: ignore[misc]
        "messages": [message]
    }
    with bind_runtime_tool_execution(
        RuntimeToolExecution(
            context=context,
            operation_id="toolop-ack",
            tool_call_id="call-mailbox",
        )
    ):
        acknowledged = await tools["mailbox.ack"].coroutine(  # type: ignore[misc]
            message_ids=["message-1"],
            runtime=runtime,
        )

    assert sent == message
    assert replied["id"] == "message-2"
    assert acknowledged["messages"][0]["status"] == "acknowledged"
    assert calls == [
        (
            "send",
            (
                "child-a",
                "toolop-send",
                "child-b",
                "question",
                "What did you find?",
                {"claim": 1},
                [],
                3600,
            ),
        ),
        (
            "reply",
            (
                "child-a",
                "toolop-reply",
                "message-1",
                "result",
                "Primary source found.",
                {},
                [],
                3600,
            ),
        ),
        ("inbox", "child-a"),
        ("ack", ("child-a", "toolop-ack", ["message-1"])),
    ]


@pytest.mark.asyncio
async def test_mailbox_middleware_reinjects_until_checkpoint_contains_message_id() -> None:
    message = {
        "id": "message-1",
        "sender_run_id": "child-a",
        "recipient_run_id": "child-b",
        "kind": "question",
        "text": "What did you find?",
        "data": {"claim": 1},
        "artifact_refs": [],
        "correlation_id": "message-1",
        "in_reply_to": None,
        "sequence": 1,
        "deadline_at": "2026-08-03T00:00:00+00:00",
    }

    class Store:
        async def deliver_agent_messages(self, run_id: str) -> list[dict[str, Any]]:
            assert run_id == "child-b"
            return [message]

    runtime = SimpleNamespace(context=RuntimeContext(run_id="child-b", store=Store()))
    middleware = AgentMailboxMiddleware()
    injected = await middleware.abefore_model({"messages": []}, runtime)
    assert injected is not None
    injected_message = injected["messages"][0]
    assert injected_message.additional_kwargs == {
        "runtime_kind": "agent_mailbox",
        "agent_message_id": "message-1",
        "sender_run_id": "child-a",
        "correlation_id": "message-1",
    }
    assert "What did you find?" in str(injected_message.content)
    assert await middleware.abefore_model({"messages": [injected_message]}, runtime) is None


def test_mailbox_tool_schemas_reject_untrusted_runtime_and_duplicate_ids() -> None:
    tools = {tool.name: tool for tool in build_agent_mailbox_tools()}
    with pytest.raises(ValueError, match="unknown mailbox fields"):
        tools["mailbox.send"].args_schema.model_validate(  # type: ignore[union-attr]
            {
                "recipient_run_id": "child-b",
                "kind": "update",
                "text": "x",
                "untrusted": True,
            }
        )
    with pytest.raises(ValueError, match="must be unique"):
        tools["mailbox.ack"].args_schema.model_validate(  # type: ignore[union-attr]
            {"message_ids": ["message-1", "message-1"]}
        )
