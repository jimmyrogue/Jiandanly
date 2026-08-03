"""Typed tools for durable same-root Agent mailbox communication."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..store.sqlite import MAX_AGENT_MAILBOX_PENDING
from ..tools.runtime import current_runtime_tool_execution_or_none
from .context_builder import RuntimeContext

AgentMessageKind = Literal["request", "question", "update", "result", "cancel"]
AgentMessageStatus = Literal["queued", "delivered", "acknowledged", "expired"]


@dataclass(frozen=True, slots=True)
class AgentMailboxControl:
    """Narrow coordinator authority injected into one Runtime execution."""

    send: Callable[
        [str, str, str, AgentMessageKind, str, dict[str, Any], Sequence[str], int],
        Awaitable[dict[str, Any]],
    ]
    reply: Callable[
        [str, str, str, AgentMessageKind, str, dict[str, Any], Sequence[str], int],
        Awaitable[dict[str, Any]],
    ]
    inbox: Callable[[str], Awaitable[list[dict[str, Any]]]]
    ack: Callable[[str, str, Sequence[str]], Awaitable[list[dict[str, Any]]]]


class AgentMailboxControlError(RuntimeError):
    pass


class _MailboxToolRequest(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        arbitrary_types_allowed=True,
        json_schema_extra={"additionalProperties": False},
    )

    @model_validator(mode="after")
    def reject_untrusted_extras(self) -> _MailboxToolRequest:
        extras = self.__pydantic_extra__ or {}
        unknown = set(extras) - {"runtime"}
        if unknown:
            raise ValueError(f"unknown mailbox fields: {', '.join(sorted(unknown))}")
        runtime = extras.get("runtime")
        if runtime is not None and not isinstance(runtime, ToolRuntime):
            raise ValueError("mailbox runtime must be injected by ToolNode")
        return self


class SendAgentMessageRequest(_MailboxToolRequest):
    recipient_run_id: str = Field(min_length=1, max_length=128)
    kind: AgentMessageKind
    text: str = Field(default="", max_length=32 * 1024)
    data: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: list[str] = Field(default_factory=list, max_length=16)
    ttl_seconds: int = Field(default=3600, ge=60, le=86400)


class ReplyAgentMessageRequest(_MailboxToolRequest):
    in_reply_to: str = Field(min_length=1, max_length=128)
    kind: AgentMessageKind
    text: str = Field(default="", max_length=32 * 1024)
    data: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: list[str] = Field(default_factory=list, max_length=16)
    ttl_seconds: int = Field(default=3600, ge=60, le=86400)


class ListAgentMessagesRequest(_MailboxToolRequest):
    statuses: list[AgentMessageStatus] = Field(default_factory=list, max_length=4)


class AckAgentMessagesRequest(_MailboxToolRequest):
    message_ids: list[str] = Field(min_length=1, max_length=MAX_AGENT_MAILBOX_PENDING)

    @model_validator(mode="after")
    def message_ids_are_unique(self) -> AckAgentMessagesRequest:
        if len(set(self.message_ids)) != len(self.message_ids):
            raise ValueError("mailbox message_ids must be unique")
        return self


def build_agent_mailbox_tools() -> list[BaseTool]:
    async def send_message(
        recipient_run_id: str,
        kind: AgentMessageKind,
        text: str = "",
        data: dict[str, Any] | None = None,
        artifact_refs: list[str] | None = None,
        ttl_seconds: int = 3600,
        runtime: ToolRuntime[Any] = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        context, control = _context_and_control(runtime)
        execution = _operation("mailbox.send")
        return await control.send(
            str(context.run_id),
            execution.operation_id,
            recipient_run_id,
            kind,
            text,
            data or {},
            artifact_refs or [],
            ttl_seconds,
        )

    async def reply_message(
        in_reply_to: str,
        kind: AgentMessageKind,
        text: str = "",
        data: dict[str, Any] | None = None,
        artifact_refs: list[str] | None = None,
        ttl_seconds: int = 3600,
        runtime: ToolRuntime[Any] = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        context, control = _context_and_control(runtime)
        execution = _operation("mailbox.reply")
        return await control.reply(
            str(context.run_id),
            execution.operation_id,
            in_reply_to,
            kind,
            text,
            data or {},
            artifact_refs or [],
            ttl_seconds,
        )

    async def list_messages(
        statuses: list[AgentMessageStatus] | None = None,
        runtime: ToolRuntime[Any] = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        context, control = _context_and_control(runtime)
        messages = await control.inbox(str(context.run_id))
        selected = set(statuses or [])
        if selected:
            messages = [message for message in messages if message.get("status") in selected]
        return {"messages": messages}

    async def acknowledge_messages(
        message_ids: list[str],
        runtime: ToolRuntime[Any] = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        context, control = _context_and_control(runtime)
        execution = _operation("mailbox.ack")
        return {
            "messages": await control.ack(
                str(context.run_id),
                execution.operation_id,
                message_ids,
            )
        }

    return [
        StructuredTool.from_function(
            name="mailbox.send",
            coroutine=send_message,
            description=(
                "Send a durable typed message to the root coordinator or a sibling durable "
                "child Run. Delivery is at-least-once; use a stable recipient Run ID."
            ),
            args_schema=SendAgentMessageRequest,
            infer_schema=False,
        ),
        StructuredTool.from_function(
            name="mailbox.inbox",
            coroutine=list_messages,
            description=(
                "List durable messages addressed to this Run. Process delivered messages, "
                "then acknowledge their message IDs with mailbox.ack."
            ),
            args_schema=ListAgentMessagesRequest,
            infer_schema=False,
        ),
        StructuredTool.from_function(
            name="mailbox.reply",
            coroutine=reply_message,
            description=(
                "Reply to a request, question, or update addressed to this Run. The Runtime "
                "preserves the sender, correlation, ordering, and hop limit."
            ),
            args_schema=ReplyAgentMessageRequest,
            infer_schema=False,
        ),
        StructuredTool.from_function(
            name="mailbox.ack",
            coroutine=acknowledge_messages,
            description=(
                "Acknowledge delivered mailbox messages only after their contents have been "
                "processed or durably reflected in the current work."
            ),
            args_schema=AckAgentMessagesRequest,
            infer_schema=False,
        ),
    ]


def _operation(tool_name: str) -> Any:
    execution = current_runtime_tool_execution_or_none()
    if execution is None:
        raise AgentMailboxControlError(f"{tool_name} is missing its durable tool operation")
    return execution


def _context_and_control(
    runtime: ToolRuntime[Any] | None,
) -> tuple[RuntimeContext, AgentMailboxControl]:
    context = getattr(runtime, "context", None)
    if not isinstance(context, RuntimeContext) or not context.run_id:
        raise AgentMailboxControlError("mailbox control is missing Runtime execution context")
    control = context.agent_mailbox_control
    if not isinstance(control, AgentMailboxControl):
        raise AgentMailboxControlError("durable Agent mailbox control is unavailable")
    return context, control
