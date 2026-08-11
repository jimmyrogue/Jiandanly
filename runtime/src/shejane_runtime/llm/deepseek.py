"""DeepSeek Chat Completions adapter.

The generic OpenAI adapter deliberately ignores provider-specific
``reasoning_content``. DeepSeek requires that field to be replayed after a
thinking tool call, so this adapter owns both conversion directions.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI

_REASONING_DELTA_KEY = "_shejane_deepseek_reasoning_delta"
_REASONING_MARKER = "_shejane_reasoning"


def deepseek_request_options(reasoning_mode: str) -> dict[str, Any]:
    if reasoning_mode == "off":
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    if reasoning_mode in {"high", "max"}:
        return {
            "extra_body": {"thinking": {"type": "enabled"}},
            "reasoning_effort": reasoning_mode,
        }
    raise ValueError(f"unsupported DeepSeek reasoning mode: {reasoning_mode}")


def model_output_phase(message: BaseMessage) -> str | None:
    if isinstance(message, (AIMessage, AIMessageChunk)):
        if message.tool_calls or getattr(message, "tool_call_chunks", None):
            return "tool_calling"
        content = message.content
        if (isinstance(content, str) and content) or (isinstance(content, list) and content):
            return "answering"
        if message.additional_kwargs.get("reasoning_content") or message.additional_kwargs.get(
            _REASONING_MARKER
        ):
            return "reasoning"
    return None


class DeepSeekChatOpenAI(ChatOpenAI):
    """Preserve DeepSeek reasoning chunks and required tool-roundtrip context."""

    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        outgoing = payload.get("messages")
        if not isinstance(outgoing, list):
            return payload
        for source, target in zip(messages, outgoing, strict=False):
            if not isinstance(source, AIMessage) or not source.tool_calls:
                continue
            reasoning = source.additional_kwargs.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning and isinstance(target, dict):
                target["reasoning_content"] = reasoning
        return payload

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict[str, Any],
        default_chunk_class: type,
        base_generation_info: dict[str, Any] | None,
    ) -> Any:
        generation = super()._convert_chunk_to_generation_chunk(
            chunk,
            default_chunk_class,
            base_generation_info,
        )
        choices = chunk.get("choices") or chunk.get("chunk", {}).get("choices") or []
        delta = choices[0].get("delta") if choices and isinstance(choices[0], dict) else None
        reasoning = delta.get("reasoning_content") if isinstance(delta, dict) else None
        if generation is None or not isinstance(reasoning, str) or not reasoning:
            return generation
        if not isinstance(generation.message, AIMessageChunk):
            return generation
        return generation.model_copy(
            update={
                "generation_info": {
                    **(generation.generation_info or {}),
                    _REASONING_DELTA_KEY: reasoning,
                },
            }
        )

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        buffered_reasoning: list[str] = []
        tool_seen = False
        for generation in super()._stream(
            messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        ):
            generation, tool_seen = _checkpoint_safe_generation(
                generation,
                buffered_reasoning,
                tool_seen=tool_seen,
            )
            yield generation

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        buffered_reasoning: list[str] = []
        tool_seen = False
        async for generation in super()._astream(
            messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        ):
            generation, tool_seen = _checkpoint_safe_generation(
                generation,
                buffered_reasoning,
                tool_seen=tool_seen,
            )
            yield generation

    def _create_chat_result(
        self,
        response: Any,
        generation_info: dict[str, Any] | None = None,
    ) -> Any:
        result = super()._create_chat_result(response, generation_info)
        response_dict = response if isinstance(response, dict) else response.model_dump(warnings=False)
        choices = response_dict.get("choices") if isinstance(response_dict, dict) else None
        if not isinstance(choices, list):
            return result
        generations = list(result.generations)
        for index, choice in enumerate(choices):
            if index >= len(generations) or not isinstance(choice, dict):
                continue
            raw_message = choice.get("message")
            reasoning = (
                raw_message.get("reasoning_content")
                if isinstance(raw_message, dict)
                else None
            )
            message = generations[index].message
            if (
                not isinstance(message, AIMessage)
                or not message.tool_calls
                or not isinstance(reasoning, str)
                or not reasoning
            ):
                continue
            generations[index] = generations[index].model_copy(
                update={
                    "message": message.model_copy(
                        update={
                            "additional_kwargs": {
                                **message.additional_kwargs,
                                "reasoning_content": reasoning,
                            }
                        }
                    )
                }
            )
        return result.model_copy(update={"generations": generations})


def _checkpoint_safe_generation(
    generation: ChatGenerationChunk,
    buffered_reasoning: list[str],
    *,
    tool_seen: bool,
) -> tuple[ChatGenerationChunk, bool]:
    """Keep raw reasoning only when DeepSeek requires it for a Tool replay."""
    info = dict(generation.generation_info or {})
    reasoning = info.pop(_REASONING_DELTA_KEY, None)
    if isinstance(reasoning, str) and reasoning:
        buffered_reasoning.append(reasoning)

    message = generation.message
    if not isinstance(message, AIMessageChunk):
        return generation.model_copy(update={"generation_info": info or None}), tool_seen
    has_tool = bool(message.tool_calls or message.tool_call_chunks)
    replay_reasoning = ""
    if has_tool and buffered_reasoning:
        replay_reasoning = "".join(buffered_reasoning)
        buffered_reasoning.clear()
    elif tool_seen and isinstance(reasoning, str):
        replay_reasoning = reasoning
        buffered_reasoning.clear()

    additional_kwargs = {
        key: value
        for key, value in message.additional_kwargs.items()
        if key != "reasoning_content"
    }
    if isinstance(reasoning, str) and reasoning:
        additional_kwargs[_REASONING_MARKER] = True
    if replay_reasoning:
        additional_kwargs["reasoning_content"] = replay_reasoning
    return (
        generation.model_copy(
            update={
                "message": message.model_copy(
                    update={"additional_kwargs": additional_kwargs}
                ),
                "generation_info": info or None,
            }
        ),
        tool_seen or has_tool,
    )
