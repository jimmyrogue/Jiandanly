"""Test-only streaming model for Runtime integration tests.

The artificial SSE transport lets tests exercise partial output, tool calls,
usage, provider errors, cancellation, and timeouts without a live provider.

Wire protocol
-------------
We send a POST with:
    {
      "run_id":  str,
      "model":    str,
      "messages": [...]   # role/content/toolCalls/toolCallId
      "tools":    [...]   # name/description/inputSchema
    }

We receive SSE events:
    event: llm.delta         data: {content_delta, reasoning_delta}
    event: llm.tool_call     data: {id, name, arguments}
    event: llm.usage         data: {input_tokens, output_tokens}
    event: llm.done          data: {request_id, finish_reason}
    event: llm.error         data: {request_id, message}

We translate into LangChain `AIMessageChunk` / `AIMessage` with tool_calls
as a list of `{id, name, args}` dicts.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.tool import tool_call_chunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import BaseTool
from pydantic import Field

from shejane_runtime.llm.errors import ModelServiceError

log = logging.getLogger("tests.streaming_model")


TestServiceError = ModelServiceError


class TestStreamingChatModel(BaseChatModel):
    """Chat model backed by the artificial test SSE transport."""

    endpoint_base_url: str = Field(default="http://test-provider")
    api_token: str = Field(default="")
    mode: str = Field(default="test-model")
    run_id: str = Field(default="agent_local")
    request_timeout_s: float = Field(default=120.0)
    max_output_tokens: int = Field(default=8192, ge=128)

    # Tools bound via .bind_tools() — populated by langchain's bind_tools path
    bound_tools: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "shejane-test-streaming-model"

    def bind_tools(
        self,
        tools: list[BaseTool | dict[str, Any] | type] | None = None,
        **kwargs: Any,
    ) -> BaseChatModel:
        """Bind tools by recording their schema on a model copy.

        The test transport receives the same tool schemas the Runtime binds.
        """
        serialized: list[dict[str, Any]] = []
        for t in tools or []:
            if isinstance(t, BaseTool):
                schema: dict[str, Any]
                if t.args_schema:
                    try:
                        schema = t.args_schema.model_json_schema()
                    except Exception as exc:
                        # Some tools — notably deepagents' `task` —
                        # have Callable in their args schema which
                        # pydantic can't serialize to JSON Schema.
                        # The transport gets concrete args at call time,
                        # so a permissive placeholder is fine here.
                        log.debug(
                            "tool %s args_schema not JSON-serializable (%s); "
                            "using permissive object schema",
                            t.name,
                            exc,
                        )
                        schema = {"type": "object", "additionalProperties": True}
                else:
                    schema = {"type": "object", "properties": {}}
                serialized.append(
                    {
                        "name": t.name,
                        "description": t.description or "",
                        "inputSchema": schema,
                    }
                )
            elif isinstance(t, dict):
                # already in our shape; pass through
                serialized.append(t)
            else:
                log.warning("ignoring unsupported tool type: %s", type(t))

        merged = {**self.__dict__, "bound_tools": serialized, **kwargs}
        return self.__class__(**{k: v for k, v in merged.items() if not k.startswith("_")})

    # -- abstract method implementations --

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        payload = self._build_request(messages, **kwargs)
        async for chunk in self._stream_from_provider(payload, capture_meta=True):
            if run_manager is not None and chunk.message.content:
                await run_manager.on_llm_new_token(str(chunk.message.content), chunk=chunk)
            yield chunk

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        accumulated_content: list[str] = []
        accumulated_reasoning: list[str] = []
        accumulated_tool_calls: list[dict[str, Any]] = []
        finish_reason: str | None = None
        usage: dict[str, Any] = {}

        payload = self._build_request(messages, **kwargs)
        async for chunk in self._stream_from_provider(payload, capture_meta=True):
            content = chunk.message.content
            if isinstance(content, str) and content:
                accumulated_content.append(content)
                if run_manager is not None:
                    await run_manager.on_llm_new_token(content, chunk=chunk)
            reasoning = (
                chunk.message.additional_kwargs.get("reasoning_content")
                if isinstance(chunk.message, AIMessageChunk)
                else None
            )
            if reasoning:
                accumulated_reasoning.append(reasoning)
            # tool_call_chunks accumulate full args at done
            for tc in chunk.message.tool_calls if isinstance(chunk.message, AIMessageChunk) else []:
                accumulated_tool_calls.append(tc)
            meta = chunk.generation_info or {}
            if "finish_reason" in meta:
                finish_reason = meta["finish_reason"]
            if "usage" in meta:
                usage = meta["usage"]

        final = AIMessage(
            content="".join(accumulated_content),
            additional_kwargs={
                **(
                    {"reasoning_content": "".join(accumulated_reasoning)}
                    if accumulated_reasoning
                    else {}
                ),
                **({"usage": usage} if usage else {}),
            },
            tool_calls=accumulated_tool_calls,
        )
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=final,
                    generation_info={"finish_reason": finish_reason or "stop"},
                )
            ],
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        # Sync wrapper around the async path. The agent runtime is async
        # everywhere; this exists only for compatibility with callers that
        # invoke the model synchronously (mostly tests).
        return asyncio.run(self._agenerate(messages, stop, None, **kwargs))

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        # Async-to-sync bridge for completeness; the agent path uses _astream.
        async def _collect() -> list[ChatGenerationChunk]:
            return [c async for c in self._astream(messages, stop, None, **kwargs)]

        yield from asyncio.run(_collect())

    # --- internals ---

    def _build_request(
        self,
        messages: list[BaseMessage],
        **kwargs: Any,
    ) -> dict[str, Any]:
        body = {
            "run_id": kwargs.get("run_id", self.run_id),
            "prompt_owner": "runtime-v1",
            "model": kwargs.get("mode", self.mode),
            "messages": [_message_to_dict(m) for m in messages],
            "tools": list(self.bound_tools),
            "max_output_tokens": self.max_output_tokens,
        }
        return body

    async def _stream_from_provider(
        self,
        payload: dict[str, Any],
        *,
        capture_meta: bool = False,
    ) -> AsyncIterator[ChatGenerationChunk]:
        url = f"{self.endpoint_base_url.rstrip('/')}/v1/test/chat/stream"
        headers = {"Accept": "text/event-stream"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"

        async with httpx.AsyncClient(timeout=self.request_timeout_s) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise httpx.HTTPStatusError(
                        f"provider returned {resp.status_code}: {body.decode('utf-8', errors='replace')}",
                        request=resp.request,
                        response=resp,
                    )

                async for event, data in _parse_sse_stream(resp):
                    if event == "llm.error":
                        raise TestServiceError.from_payload(data)
                    chunk = _event_to_chunk(event, data, capture_meta)
                    if chunk is not None:
                        yield chunk


# --- message <-> dict converters ---


def _message_to_dict(message: BaseMessage) -> dict[str, Any]:
    if isinstance(message, HumanMessage):
        return {"role": "user", "content": _stringify(message.content)}
    if isinstance(message, SystemMessage):
        return {"role": "system", "content": _stringify(message.content)}
    if isinstance(message, ToolMessage):
        return {
            "role": "tool",
            "content": _stringify(message.content),
            "toolCallId": message.tool_call_id,
            "name": message.name or "",
        }
    if isinstance(message, AIMessage):
        out: dict[str, Any] = {
            "role": "assistant",
            "content": _stringify(message.content),
            "toolCalls": [
                {"id": tc["id"], "name": tc["name"], "arguments": tc.get("args", {})}
                for tc in message.tool_calls
            ],
        }
        # DeepSeek (and other thinking-mode providers) require that
        # `reasoning_content` from a previous assistant turn be passed
        # back on subsequent calls — otherwise:
        #   400: "The `reasoning_content` in the thinking mode must
        #         be passed back to the API."
        # We accumulate reasoning into additional_kwargs during streaming
        # (see `_event_to_chunk`), so just plumb it through here. The Go
        # API at /api/v1/agent/llm/stream accepts the camelCase key.
        reasoning = message.additional_kwargs.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            out["reasoningContent"] = reasoning
        return out
    # fallback — best-effort role mapping
    return {"role": "user", "content": _stringify(message.content)}


def _stringify(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # LangChain sometimes returns list[dict[type=text, ...]]; flatten text parts.
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
            else:
                parts.append(str(part))
        return "".join(parts)
    return str(content)


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


# --- SSE stream parser ---


async def _parse_sse_stream(
    resp: httpx.Response,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Yield (event_name, parsed_data_dict) pairs."""
    event_name: str | None = None
    data_lines: list[str] = []

    async for raw_line in resp.aiter_lines():
        line = raw_line.rstrip("\r")
        if not line:
            if event_name is not None and data_lines:
                joined = "\n".join(data_lines)
                try:
                    data = json.loads(joined)
                except json.JSONDecodeError:
                    log.warning("non-JSON SSE data: %s", joined[:200])
                    data = {}
                yield event_name, data
            event_name = None
            data_lines = []
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())


def _event_to_chunk(
    event: str,
    data: dict[str, Any],
    capture_meta: bool,
) -> ChatGenerationChunk | None:
    """Convert one SSE event into a LangChain `ChatGenerationChunk`."""
    if event == "llm.delta":
        content_delta = data.get("content_delta", "")
        reasoning_delta = data.get("reasoning_delta", "")
        kwargs: dict[str, Any] = {}
        if reasoning_delta:
            kwargs["reasoning_content"] = reasoning_delta
        msg = AIMessageChunk(content=content_delta, additional_kwargs=kwargs)
        return ChatGenerationChunk(message=msg)

    if event == "llm.tool_call":
        msg = AIMessageChunk(
            content="",
            tool_call_chunks=[
                tool_call_chunk(
                    id=data.get("id"),
                    name=data.get("name"),
                    args=json.dumps(data.get("arguments", {})),
                    index=data.get("index", 0),
                )
            ],
        )
        return ChatGenerationChunk(message=msg)

    if event == "llm.usage":
        # Emit usage on BOTH the streaming and non-streaming paths.
        #   • additional_kwargs — survives LangGraph's `messages`-mode
        #     re-emission of AIMessageChunk, so event_translator can turn it
        #     into a wire-level `llm.usage` event for the per-turn usage chip.
        #   • generation_info — read by _agenerate (non-streaming path).
        # generation_info does NOT survive into messages mode (only
        # additional_kwargs does), which is why usage must ride in both.
        msg = AIMessageChunk(content="", additional_kwargs={"usage": data})
        return ChatGenerationChunk(
            message=msg,
            generation_info={"usage": data},
        )

    if event == "llm.done":
        if not capture_meta:
            return None
        msg = AIMessageChunk(content="")
        return ChatGenerationChunk(
            message=msg,
            generation_info={
                "finish_reason": data.get("finish_reason", "stop"),
                "request_id": data.get("request_id", ""),
            },
        )

    if event == "llm.error":
        # surface as a non-fatal chunk with error info — caller decides.
        msg = AIMessageChunk(
            content="",
            additional_kwargs={"backend_error": data.get("message", "")},
        )
        return ChatGenerationChunk(
            message=msg,
            generation_info={"finish_reason": "error"},
        )

    return None
