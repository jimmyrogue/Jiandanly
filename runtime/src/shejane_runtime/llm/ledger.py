"""Durable model-call boundary for the Runtime.

Every model invocation reserves a row before contacting a provider, records the
first model-visible output before yielding it, and settles usage from the final
provider response. Product usage is read from this ledger, never reconstructed
from a client SSE connection.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import BaseTool
from langgraph.constants import TAG_NOSTREAM
from pydantic import Field

from ..failure_policy import build_retry_decision
from ..middleware.tool_visibility import visible_tools_for_messages
from ..store.sqlite import LocalStore
from ..tools.runtime import current_runtime_tool_execution_or_none
from . import context_envelope as _context_envelope
from .errors import ModelServiceError
from .tool_aliases import provider_tools as _provider_tools
from .tool_aliases import rewrite_tool_names as _rewrite_tool_names

_conservative_token_count = _context_envelope.conservative_token_count
_enforce_context_envelope = _context_envelope.enforce_context_envelope
_estimate_tool_tokens = _context_envelope.estimate_tool_tokens
_truncate_large_message = _context_envelope.truncate_large_message


class ModelContextBudgetExceeded(RuntimeError):
    code = "model_context_budget_exhausted"
    retryable = False


class ModelContextProfileMissing(RuntimeError):
    code = "model_context_profile_missing"
    retryable = False


class ModelResponseIncomplete(RuntimeError):
    code = "model_response_incomplete"
    recoverable = False
    retryable = False

    def __init__(self, reason: str, *, request_id: str | None = None) -> None:
        super().__init__(f"provider returned an incomplete response ({reason})")
        self.request_id = request_id


MODEL_RETRY_ATTEMPTS = 2


class LedgerChatModel(BaseChatModel):
    """Wrap one bound provider model with durable reservation and settlement."""

    delegate: BaseChatModel = Field(exclude=True)
    store: Any = Field(exclude=True)
    run_id: str
    execution_attempt_id: str
    model_name: str
    max_calls: int
    call_purpose: str = "agent"
    supports_json_schema_output: bool = False
    tool_schema_tokens: int = 0
    bound_tools: tuple[Any, ...] = Field(default_factory=tuple, exclude=True)
    hosted_tools: tuple[dict[str, Any], ...] = Field(default_factory=tuple, exclude=True)
    bound_tool_choice: Any = Field(default=None, exclude=True)
    bound_tool_kwargs: dict[str, Any] = Field(default_factory=dict, exclude=True)

    @property
    def _llm_type(self) -> str:
        return "shejane-ledger"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> BaseChatModel:
        return self.model_copy(
            update={
                "bound_tools": tuple(tools),
                "bound_tool_choice": tool_choice,
                "bound_tool_kwargs": dict(kwargs),
                "tool_schema_tokens": _estimate_tool_tokens(tools),
            }
        )

    def _provider_model(
        self,
        messages: list[BaseMessage],
    ) -> tuple[BaseChatModel, int, dict[str, str], dict[str, str]]:
        if not self.bound_tools:
            return self.delegate, self.tool_schema_tokens, {}, {}
        visible_tools = visible_tools_for_messages(self.bound_tools, messages)
        provider_tools, aliases, choices = _provider_tools(visible_tools)
        return (
            self.delegate.bind_tools(
                provider_tools,
                tool_choice=choices.get(self.bound_tool_choice, self.bound_tool_choice),
                **self.bound_tool_kwargs,
            ),
            _estimate_tool_tokens(visible_tools),
            aliases,
            choices,
        )

    async def _reserve(
        self,
        *,
        logical_call_id: str | None = None,
        retry_attempt: int = 0,
    ) -> dict[str, Any]:
        store = self.store
        if not isinstance(store, LocalStore):
            raise RuntimeError("model ledger store is not bound")
        parent_execution = current_runtime_tool_execution_or_none()
        return await store.reserve_model_call(
            run_id=self.run_id,
            execution_attempt_id=self.execution_attempt_id,
            model=self.model_name,
            max_calls=self.max_calls,
            purpose=self.call_purpose,
            parent_tool_operation_id=(
                parent_execution.operation_id if parent_execution is not None else None
            ),
            logical_call_id=logical_call_id,
            retry_attempt=retry_attempt,
        )

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        provider_model, tool_schema_tokens, aliases, choices = self._provider_model(messages)
        messages = self._bounded_messages(messages, tool_schema_tokens=tool_schema_tokens)
        messages = [_rewrite_tool_names(message, choices) for message in messages]
        retry_attempt = 0
        logical_call_id: str | None = None
        while True:
            receipt = await self._reserve(
                logical_call_id=logical_call_id,
                retry_attempt=retry_attempt,
            )
            logical_call_id = str(receipt["logical_call_id"])
            output_started = False
            usage: dict[str, Any] = {}
            try:
                message = _rewrite_tool_names(
                    await provider_model.ainvoke(
                        messages,
                        stop=stop,
                        config={"callbacks": [], "tags": [TAG_NOSTREAM]},
                        **kwargs,
                    ),
                    aliases,
                )
                usage = _usage_from_message(message)
                if incomplete := _incomplete_response_error(message):
                    raise incomplete
                message = _with_model_call_id(message, str(receipt["id"]))
                if _has_visible_output(message):
                    await self.store.mark_model_call_output(
                        run_id=self.run_id,
                        call_id=receipt["id"],
                    )
                    output_started = True
                await self.store.settle_model_call(
                    run_id=self.run_id,
                    call_id=receipt["id"],
                    provider_request_id=_request_id_from_message(message),
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    usage=usage,
                )
                return ChatResult(generations=[ChatGeneration(message=message)])
            except BaseException as exc:
                outcome_unknown = self._outcome_unknown(exc, output_started=output_started)
                await asyncio.shield(
                    self.store.fail_model_call(
                        run_id=self.run_id,
                        call_id=receipt["id"],
                        outcome_unknown=outcome_unknown,
                        error_code=_error_code(exc),
                        provider_request_id=_request_id_from_error(exc),
                        input_tokens=usage.get("input_tokens"),
                        output_tokens=usage.get("output_tokens"),
                        usage=usage,
                    )
                )
                decision = _model_retry_decision(
                    exc,
                    output_started=output_started,
                    outcome_unknown=outcome_unknown,
                    retry_attempt=retry_attempt,
                )
                if not decision["should_retry"]:
                    raise
                await asyncio.sleep(float(decision["delay_s"]))
                retry_attempt += 1

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        provider_model, tool_schema_tokens, aliases, choices = self._provider_model(messages)
        messages = self._bounded_messages(messages, tool_schema_tokens=tool_schema_tokens)
        messages = [_rewrite_tool_names(message, choices) for message in messages]
        retry_attempt = 0
        logical_call_id: str | None = None
        while True:
            receipt = await self._reserve(
                logical_call_id=logical_call_id,
                retry_attempt=retry_attempt,
            )
            logical_call_id = str(receipt["logical_call_id"])
            output_started = False
            usage: dict[str, Any] = {}
            provider_request_id: str | None = None
            first_chunk = True
            try:
                async for provider_message in provider_model.astream(
                    messages,
                    stop=stop,
                    config={"callbacks": [], "tags": [TAG_NOSTREAM]},
                    **kwargs,
                ):
                    message = _rewrite_tool_names(provider_message, aliases)
                    if first_chunk:
                        message = _with_model_call_id(message, str(receipt["id"]))
                        first_chunk = False
                    if not output_started and _has_visible_output(message):
                        await self.store.mark_model_call_output(
                            run_id=self.run_id,
                            call_id=receipt["id"],
                        )
                        output_started = True
                    current_usage = _usage_from_message(message)
                    if current_usage:
                        usage = current_usage
                    provider_request_id = _request_id_from_message(message) or provider_request_id
                    if incomplete := _incomplete_response_error(message):
                        raise incomplete
                    yield ChatGenerationChunk(message=message)
                await self.store.settle_model_call(
                    run_id=self.run_id,
                    call_id=receipt["id"],
                    provider_request_id=provider_request_id,
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    usage=usage,
                )
                return
            except BaseException as exc:
                outcome_unknown = self._outcome_unknown(exc, output_started=output_started)
                await asyncio.shield(
                    self.store.fail_model_call(
                        run_id=self.run_id,
                        call_id=receipt["id"],
                        outcome_unknown=outcome_unknown,
                        error_code=_error_code(exc),
                        provider_request_id=_request_id_from_error(exc),
                        input_tokens=usage.get("input_tokens"),
                        output_tokens=usage.get("output_tokens"),
                        usage=usage,
                    )
                )
                decision = _model_retry_decision(
                    exc,
                    output_started=output_started,
                    outcome_unknown=outcome_unknown,
                    retry_attempt=retry_attempt,
                )
                if not decision["should_retry"]:
                    raise
                await asyncio.sleep(float(decision["delay_s"]))
                retry_attempt += 1

    def _outcome_unknown(self, exc: BaseException, *, output_started: bool) -> bool:
        if isinstance(exc, ModelResponseIncomplete):
            return False
        # Review calls are read-only and cannot emit Tool calls into the graph.
        # A timeout may leave provider billing uncertain, but never leaves the
        # Agent's external execution outcome uncertain.
        if self.call_purpose != "agent":
            return False
        return output_started or _outcome_may_be_unknown(exc)

    def _bounded_messages(
        self,
        messages: list[BaseMessage],
        *,
        tool_schema_tokens: int | None = None,
    ) -> list[BaseMessage]:
        profile_limit = (self.profile or {}).get("max_input_tokens")
        if not isinstance(profile_limit, int) or profile_limit <= 0:
            raise ModelContextProfileMissing("selected model does not declare max_input_tokens")
        max_input_tokens = int(profile_limit)
        schema_tokens = (
            self.tool_schema_tokens if tool_schema_tokens is None else tool_schema_tokens
        )
        if schema_tokens >= int(max_input_tokens * 0.9):
            raise ModelContextBudgetExceeded(
                "visible tool schemas exceed the selected model's context budget "
                f"({schema_tokens} >= {int(max_input_tokens * 0.9)})"
            )
        # Leave room for provider framing and schemas that cannot be measured
        # exactly by LangChain's message counter.
        message_budget = int(max_input_tokens * 0.9) - max(0, int(schema_tokens))
        if message_budget < 128:
            raise ModelContextBudgetExceeded(
                "selected model has insufficient context capacity for a minimum "
                f"request ({message_budget} tokens remain)"
            )
        return _enforce_context_envelope(messages, max_tokens=message_budget)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise RuntimeError("LedgerChatModel is async-only")

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        raise RuntimeError("LedgerChatModel is async-only")


def _with_model_call_id(message: BaseMessage, call_id: str) -> BaseMessage:
    return message.model_copy(
        update={
            "additional_kwargs": {
                **message.additional_kwargs,
                "runtime_model_call_id": call_id,
            }
        }
    )


def _has_visible_output(message: BaseMessage) -> bool:
    content = getattr(message, "content", None)
    if isinstance(content, str) and bool(content):
        return True
    if isinstance(content, list) and bool(content):
        return True
    if isinstance(message, (AIMessage, AIMessageChunk)):
        if message.tool_calls or getattr(message, "tool_call_chunks", None):
            return True
        return bool(message.additional_kwargs.get("reasoning_content"))
    return False


def _usage_from_message(message: BaseMessage) -> dict[str, Any]:
    raw = getattr(message, "usage_metadata", None)
    if not isinstance(raw, dict) and isinstance(message, (AIMessage, AIMessageChunk)):
        raw = message.additional_kwargs.get("usage")
    if not isinstance(raw, dict):
        return {}
    usage: dict[str, Any] = {
        "input_tokens": _int_or_none(raw.get("input_tokens")),
        "output_tokens": _int_or_none(raw.get("output_tokens")),
        "total_tokens": _int_or_none(raw.get("total_tokens")),
    }
    for key in ("input_token_details", "output_token_details"):
        details = raw.get(key)
        if isinstance(details, Mapping):
            normalized = {
                str(name): count
                for name, value in details.items()
                if (count := _int_or_none(value)) is not None
            }
            if normalized:
                usage[key] = normalized
    return {key: value for key, value in usage.items() if value is not None}


def _incomplete_response_error(message: BaseMessage) -> ModelResponseIncomplete | None:
    metadata = getattr(message, "response_metadata", None)
    if not isinstance(metadata, dict) or str(metadata.get("status") or "").lower() != "incomplete":
        return None
    details = metadata.get("incomplete_details")
    reason = (
        str(details.get("reason") or "unspecified")
        if isinstance(details, dict)
        else "unspecified"
    )
    return ModelResponseIncomplete(reason, request_id=_request_id_from_message(message))


def _request_id_from_message(message: BaseMessage) -> str | None:
    metadata = getattr(message, "response_metadata", None)
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("request_id") or metadata.get("id")
    return str(value) if value else None


def _request_id_from_error(exc: BaseException) -> str | None:
    if isinstance(exc, ModelServiceError):
        return exc.request_id
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.headers.get("x-request-id")
    value = getattr(exc, "request_id", None)
    return str(value) if value else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _outcome_may_be_unknown(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (
            asyncio.CancelledError,
            httpx.TimeoutException,
            httpx.TransportError,
            TimeoutError,
            ConnectionError,
        ),
    )


def _model_retry_decision(
    exc: BaseException,
    *,
    output_started: bool,
    outcome_unknown: bool,
    retry_attempt: int,
) -> dict[str, Any]:
    if output_started or outcome_unknown or isinstance(exc, asyncio.CancelledError):
        return {"should_retry": False, "delay_s": 0.0}
    if isinstance(exc, ModelServiceError):
        payload = exc.to_event_payload()
    else:
        payload = {
            "type": type(exc).__name__,
            "error_code": _error_code(exc),
            "message": str(exc),
        }
        for field in ("recoverable", "retryable"):
            value = getattr(exc, field, None)
            if isinstance(value, bool):
                payload[field] = value
    decision = build_retry_decision(
        "model.failed",
        payload,
        attempt=retry_attempt,
        max_attempts=MODEL_RETRY_ATTEMPTS,
    )
    retry_after = _retry_after_seconds(exc)
    if decision["should_retry"] and retry_after is not None:
        decision["delay_s"] = max(float(decision["delay_s"]), retry_after)
    return decision


def _retry_after_seconds(exc: BaseException) -> float | None:
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    value = exc.response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return max(0.0, (parsed - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if code:
        return str(code)[:100]
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_{exc.response.status_code}"
    return type(exc).__name__[:100]
