"""Durable model-call boundary for the Runtime.

Every model invocation reserves a row before contacting a provider, records the
first model-visible output before yielding it, and settles usage from the final
provider response. Product usage is read from this ledger, never reconstructed
from a client SSE connection.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from typing import Any

import httpx
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.constants import TAG_NOSTREAM
from pydantic import Field

from ..middleware.tool_visibility import visible_tools_for_messages
from ..store.sqlite import LocalStore
from .errors import ModelServiceError


class ModelContextBudgetExceeded(RuntimeError):
    code = "model_context_budget_exhausted"
    retryable = False


class ModelContextProfileMissing(RuntimeError):
    code = "model_context_profile_missing"
    retryable = False


class LedgerChatModel(BaseChatModel):
    """Wrap one bound provider model with durable reservation and settlement."""

    delegate: BaseChatModel = Field(exclude=True)
    store: Any = Field(exclude=True)
    run_id: str
    execution_attempt_id: str
    model_name: str
    max_calls: int
    call_purpose: str = "agent"
    tool_schema_tokens: int = 0
    bound_tools: tuple[Any, ...] = Field(default_factory=tuple, exclude=True)
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

    async def _reserve(self) -> dict[str, Any]:
        store = self.store
        if not isinstance(store, LocalStore):
            raise RuntimeError("model ledger store is not bound")
        return await store.reserve_model_call(
            run_id=self.run_id,
            execution_attempt_id=self.execution_attempt_id,
            model=self.model_name,
            max_calls=self.max_calls,
            purpose=self.call_purpose,
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
        receipt = await self._reserve()
        output_started = False
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
            if _has_visible_output(message):
                await self.store.mark_model_call_output(
                    run_id=self.run_id,
                    call_id=receipt["id"],
                )
                output_started = True
            usage = _usage_from_message(message)
            await self.store.settle_model_call(
                run_id=self.run_id,
                call_id=receipt["id"],
                provider_request_id=_request_id_from_message(message),
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
            )
            return ChatResult(generations=[ChatGeneration(message=message)])
        except BaseException as exc:
            await asyncio.shield(
                self.store.fail_model_call(
                    run_id=self.run_id,
                    call_id=receipt["id"],
                    outcome_unknown=self._outcome_unknown(exc, output_started=output_started),
                    error_code=_error_code(exc),
                )
            )
            raise

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
        receipt = await self._reserve()
        output_started = False
        usage: dict[str, int | None] = {}
        provider_request_id: str | None = None
        try:
            async for provider_message in provider_model.astream(
                messages,
                stop=stop,
                config={"callbacks": [], "tags": [TAG_NOSTREAM]},
                **kwargs,
            ):
                message = _rewrite_tool_names(provider_message, aliases)
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
                yield ChatGenerationChunk(message=message)
            await self.store.settle_model_call(
                run_id=self.run_id,
                call_id=receipt["id"],
                provider_request_id=provider_request_id,
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
            )
        except BaseException as exc:
            await asyncio.shield(
                self.store.fail_model_call(
                    run_id=self.run_id,
                    call_id=receipt["id"],
                    outcome_unknown=self._outcome_unknown(exc, output_started=output_started),
                    error_code=_error_code(exc),
                )
            )
            raise

    def _outcome_unknown(self, exc: BaseException, *, output_started: bool) -> bool:
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


_SAFE_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _provider_tools(
    tools: Sequence[Any],
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, str]]:
    converted = [convert_to_openai_tool(tool) for tool in tools]
    reserved_names = {
        function["name"]
        for schema in converted
        if isinstance((function := schema.get("function")), dict)
        and isinstance(function.get("name"), str)
        and _SAFE_TOOL_NAME.fullmatch(function["name"])
    }
    used_names: set[str] = set()
    schemas: list[dict[str, Any]] = []
    aliases: dict[str, str] = {}
    choices: dict[str, str] = {}
    for schema in converted:
        function = schema.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            schemas.append(schema)
            continue
        original = function["name"]
        if _SAFE_TOOL_NAME.fullmatch(original):
            wire_name = original
            legacy_wire_name = None
        else:
            stem = re.sub(r"[^a-zA-Z0-9_-]+", "_", original).strip("_") or "tool"
            legacy_wire_name = f"{stem[:55]}_{hashlib.sha256(original.encode()).hexdigest()[:8]}"
            wire_name = stem
            if len(wire_name) > 64 or wire_name in reserved_names or wire_name in used_names:
                wire_name = legacy_wire_name
        used_names.add(wire_name)
        schemas.append({**schema, "function": {**function, "name": wire_name}})
        aliases[wire_name] = original
        if legacy_wire_name is not None and legacy_wire_name not in reserved_names:
            aliases.setdefault(legacy_wire_name, original)
        choices[original] = wire_name
    return schemas, aliases, choices


def _rewrite_tool_names(message: BaseMessage, aliases: dict[str, str]) -> BaseMessage:
    if not aliases:
        return message
    if isinstance(message, ToolMessage):
        additional_kwargs = message.additional_kwargs
        raw_name = additional_kwargs.get("name")
        return message.model_copy(
            update={
                "name": aliases.get(message.name, message.name),
                "additional_kwargs": (
                    {**additional_kwargs, "name": aliases.get(raw_name, raw_name)}
                    if isinstance(raw_name, str)
                    else additional_kwargs
                ),
            }
        )
    if not isinstance(message, (AIMessage, AIMessageChunk)):
        return message
    updates: dict[str, Any] = {}
    if isinstance(message.content, list):
        updates["content"] = [
            {
                **block,
                "name": aliases.get(block.get("name"), block.get("name")),
            }
            if isinstance(block, dict)
            and block.get("type") in {"tool_call", "function_call", "tool_use"}
            and isinstance(block.get("name"), str)
            else block
            for block in message.content
        ]
    for field in ("tool_calls", "invalid_tool_calls", "tool_call_chunks"):
        calls = getattr(message, field, None)
        if calls:
            updates[field] = [
                {**call, "name": aliases.get(call.get("name"), call.get("name"))} for call in calls
            ]
    raw_calls = message.additional_kwargs.get("tool_calls")
    if isinstance(raw_calls, list):
        normalized = []
        for call in raw_calls:
            function = call.get("function") if isinstance(call, dict) else None
            normalized.append(
                {
                    **call,
                    "function": {
                        **function,
                        "name": aliases.get(function.get("name"), function.get("name")),
                    },
                }
                if isinstance(call, dict) and isinstance(function, dict)
                else call
            )
        updates["additional_kwargs"] = {**message.additional_kwargs, "tool_calls": normalized}
    return message.model_copy(update=updates)


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


def _usage_from_message(message: BaseMessage) -> dict[str, int | None]:
    raw = getattr(message, "usage_metadata", None)
    if not isinstance(raw, dict) and isinstance(message, (AIMessage, AIMessageChunk)):
        raw = message.additional_kwargs.get("usage")
    if not isinstance(raw, dict):
        return {}
    return {
        "input_tokens": _int_or_none(raw.get("input_tokens")),
        "output_tokens": _int_or_none(raw.get("output_tokens")),
    }


def _request_id_from_message(message: BaseMessage) -> str | None:
    metadata = getattr(message, "response_metadata", None)
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("request_id") or metadata.get("id")
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


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, ModelServiceError) and exc.code:
        return exc.code[:100]
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_{exc.response.status_code}"
    return type(exc).__name__[:100]


def _enforce_context_envelope(
    messages: list[BaseMessage],
    *,
    max_tokens: int,
) -> list[BaseMessage]:
    """Return a deterministic, explicit provider-safe message envelope."""
    if _conservative_token_count(messages) <= max_tokens:
        return messages

    max_single_chars = max(1_000, int(max_tokens * 0.65))
    bounded = [_truncate_large_message(message, max_chars=max_single_chars) for message in messages]
    if _conservative_token_count(bounded) <= max_tokens:
        return bounded

    marker = SystemMessage(
        content=(
            "[Runtime context envelope: older model input was omitted to fit the "
            "selected model's declared context window. Do not assume omitted details.]"
        )
    )
    system_prefix: list[BaseMessage] = []
    for message in bounded:
        if not isinstance(message, SystemMessage):
            break
        system_prefix.append(message)
    turn_start = max(
        (index for index, message in enumerate(bounded) if message.type == "human"),
        default=len(system_prefix),
    )
    latest_turn = bounded[turn_start:]
    turn_budget = max_tokens - _conservative_token_count([*system_prefix, marker])
    fitted_turn = _fit_message_contents(latest_turn, max_tokens=max(128, turn_budget))
    result = [*system_prefix, marker, *fitted_turn]
    if (fitted_turn or not latest_turn) and _conservative_token_count(result) <= max_tokens:
        return result

    # ponytail: if fixed tool-call metadata alone exceeds the window, retain
    # the latest user input and let the model redo the batch instead of sending
    # an invalid orphan ToolMessage sequence.
    latest_human = next((message for message in latest_turn if message.type == "human"), None)
    fallback = (
        [_truncate_large_message(latest_human, max_chars=max(128, turn_budget))]
        if latest_human is not None
        else []
    )
    return [*system_prefix, marker, *fallback]


def _fit_message_contents(
    messages: list[BaseMessage],
    *,
    max_tokens: int,
) -> list[BaseMessage]:
    if _conservative_token_count(messages) <= max_tokens:
        return messages
    low = 128
    high = max(
        (len(message.content) for message in messages if isinstance(message.content, str)),
        default=low,
    )
    best: list[BaseMessage] = []
    while low <= high:
        limit = (low + high) // 2
        candidate = [_truncate_large_message(message, max_chars=limit) for message in messages]
        if _conservative_token_count(candidate) <= max_tokens:
            best = candidate
            low = limit + 1
        else:
            high = limit - 1
    return best


def _truncate_large_message(message: BaseMessage, *, max_chars: int) -> BaseMessage:
    content = message.content
    if isinstance(content, str):
        return (
            message
            if len(content) <= max_chars
            else message.model_copy(update={"content": _truncate_text(content, max_chars)})
        )
    if not isinstance(content, list):
        return message

    text_parts: list[tuple[int, str, bool]] = []
    for index, block in enumerate(content):
        if isinstance(block, str) and block:
            text_parts.append((index, block, False))
        elif (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
            and block["text"]
        ):
            text_parts.append((index, block["text"], True))
    total_chars = sum(len(text) for _, text, _ in text_parts)
    if not text_parts and isinstance(message, ToolMessage):
        serialized_size = len(json.dumps(content, ensure_ascii=False, default=str))
        if serialized_size > max_chars:
            artifact_id = message.additional_kwargs.get("artifact_id")
            suffix = f" Artifact: {artifact_id}." if artifact_id else ""
            return message.model_copy(
                update={
                    "content": (
                        "[Runtime omitted oversized binary tool content; call the tool again "
                        f"if it is still needed.]{suffix}"
                    )
                }
            )
    if total_chars <= max_chars:
        return message

    low = 0
    high = max(len(text) for _, text, _ in text_parts)
    per_part_limit = 0
    while low <= high:
        candidate = (low + high) // 2
        if sum(min(len(text), candidate) for _, text, _ in text_parts) <= max_chars:
            per_part_limit = candidate
            low = candidate + 1
        else:
            high = candidate - 1

    bounded = list(content)
    for index, text, is_block in text_parts:
        replacement = text if len(text) <= per_part_limit else _truncate_text(text, per_part_limit)
        bounded[index] = {**bounded[index], "text": replacement} if is_block else replacement
    return message.model_copy(update={"content": bounded})


def _truncate_text(content: str, max_chars: int) -> str:
    marker = f"\n\n[Runtime truncated {max(0, len(content) - max_chars)} characters from this message.]\n\n"
    if max_chars <= len(marker):
        return marker[:max_chars]
    available = max_chars - len(marker)
    head = available // 2
    tail = available - head
    return content[:head] + marker + content[-tail:]


def _estimate_tool_tokens(
    tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
) -> int:
    serializable: list[Any] = []
    for tool in tools:
        if isinstance(tool, dict):
            serializable.append(tool)
            continue
        schema: Any = {}
        args_schema = getattr(tool, "args_schema", None)
        if args_schema is not None:
            try:
                schema = args_schema.model_json_schema()
            except Exception:
                schema = str(args_schema)
        serializable.append(
            {
                "name": getattr(tool, "name", getattr(tool, "__name__", "")),
                "description": getattr(tool, "description", ""),
                "input_schema": schema,
            }
        )
    payload = json.dumps(serializable, ensure_ascii=False, default=str)
    # JSON is mostly ASCII (~4 bytes/token), while CJK approaches 3 bytes/token.
    # Two bytes/token stays conservative without downloading tokenizer data.
    return max(0, math.ceil(len(payload.encode("utf-8")) / 2))


def _conservative_token_count(messages: list[BaseMessage]) -> int:
    return count_tokens_approximately(messages, chars_per_token=1.0)
