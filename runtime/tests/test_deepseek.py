from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputItemAddedEvent,
    ResponseOutputItemDoneEvent,
    ResponseReasoningItem,
    ResponseReasoningTextDeltaEvent,
)

from shejane_runtime.llm.deepseek import (
    DeepSeekChatOpenAI,
    _checkpoint_safe_generation,
    deepseek_request_options,
    model_output_phase,
)
from shejane_runtime.model_services.profiles import discovered_model_profile
from shejane_runtime.runs.model_bindings import reasoning_mode_error


def _model() -> DeepSeekChatOpenAI:
    return DeepSeekChatOpenAI(
        model="deepseek-v4-flash",
        api_key="test-key",
        base_url="https://api.deepseek.com",
        streaming=True,
        max_retries=0,
    )


def test_deepseek_reasoning_modes_map_to_explicit_provider_options() -> None:
    assert deepseek_request_options("off") == {
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    assert deepseek_request_options("high") == {
        "extra_body": {"thinking": {"type": "enabled"}},
        "reasoning_effort": "high",
    }
    assert deepseek_request_options("max") == {
        "extra_body": {"thinking": {"type": "enabled"}},
        "reasoning_effort": "max",
    }

    assert deepseek_request_options("off", responses=True) == {
        "reasoning": {"effort": "none"},
    }
    assert deepseek_request_options("high", responses=True) == {
        "reasoning": {"effort": "high"},
    }
    assert deepseek_request_options("max", responses=True) == {
        "reasoning": {"effort": "max"},
    }


def test_deepseek_replays_reasoning_only_for_tool_roundtrips() -> None:
    payload = _model()._get_request_payload(
        [
            HumanMessage(content="look it up"),
            AIMessage(
                content="",
                additional_kwargs={"reasoning_content": "private prior reasoning"},
                tool_calls=[{"id": "call-1", "name": "lookup", "args": {}}],
            ),
            ToolMessage(content="result", tool_call_id="call-1", name="lookup"),
            AIMessage(
                content="finished",
                additional_kwargs={"reasoning_content": "must not be replayed"},
            ),
        ]
    )

    assert payload["messages"][1]["reasoning_content"] == "private prior reasoning"
    assert "reasoning_content" not in payload["messages"][3]


def test_deepseek_stream_preserves_reasoning_without_exposing_it_as_content() -> None:
    generation = _model()._convert_chunk_to_generation_chunk(
        {
            "id": "chunk-1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "private delta",
                    },
                    "finish_reason": None,
                }
            ],
        },
        AIMessageChunk,
        None,
    )

    assert generation is not None
    buffered_reasoning: list[str] = []
    safe_generation, tool_seen = _checkpoint_safe_generation(
        generation,
        buffered_reasoning,
        tool_seen=False,
    )
    assert safe_generation.message.content == ""
    assert safe_generation.message.additional_kwargs["_shejane_reasoning"] is True
    assert "reasoning_content" not in safe_generation.message.additional_kwargs
    assert tool_seen is False

    tool_generation, tool_seen = _checkpoint_safe_generation(
        type(generation)(
            message=AIMessageChunk(
                content="",
                tool_call_chunks=[{"id": "call-1", "name": "lookup", "args": "{}", "index": 0}],
            )
        ),
        buffered_reasoning,
        tool_seen=tool_seen,
    )
    assert tool_seen is True
    assert tool_generation.message.additional_kwargs["reasoning_content"] == "private delta"


@pytest.mark.asyncio
async def test_deepseek_responses_replays_reasoning_only_for_function_tool_roundtrip() -> None:
    events = [
        ResponseOutputItemAddedEvent(
            item=ResponseReasoningItem(
                id="reasoning-1",
                summary=[],
                type="reasoning",
                status="in_progress",
            ),
            output_index=0,
            sequence_number=1,
            type="response.output_item.added",
        ),
        ResponseReasoningTextDeltaEvent(
            content_index=0,
            delta="private response reasoning",
            item_id="reasoning-1",
            output_index=0,
            sequence_number=2,
            type="response.reasoning_text.delta",
        ),
        ResponseOutputItemDoneEvent(
            item=ResponseReasoningItem(
                id="reasoning-1",
                summary=[],
                type="reasoning",
                status="completed",
            ),
            output_index=0,
            sequence_number=3,
            type="response.output_item.done",
        ),
        ResponseOutputItemAddedEvent(
            item=ResponseFunctionToolCall(
                arguments="{}",
                call_id="call-1",
                id="function-1",
                name="lookup",
                type="function_call",
                status="in_progress",
            ),
            output_index=1,
            sequence_number=4,
            type="response.output_item.added",
        ),
    ]

    class FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not events:
                raise StopAsyncIteration
            return events.pop(0)

    class FakeResponses:
        async def create(self, **_payload):
            return FakeStream()

    model = DeepSeekChatOpenAI(
        model="deepseek-v4-flash",
        api_key="test-key",
        base_url="https://api.deepseek.com",
        streaming=True,
        max_retries=0,
        use_responses_api=True,
        output_version="v1",
    )
    object.__setattr__(
        model,
        "root_async_client",
        SimpleNamespace(responses=FakeResponses()),
    )

    chunks = [
        chunk async for chunk in model._astream_responses([HumanMessage(content="look it up")])
    ]
    content = [item for chunk in chunks for item in chunk.message.content if isinstance(item, dict)]

    reasoning = next(item for item in content if item.get("type") == "reasoning")
    assert reasoning["content"] == [
        {"type": "reasoning_text", "text": "private response reasoning"}
    ]
    assert reasoning["status"] == "completed"
    assert content.index(reasoning) < next(
        index for index, item in enumerate(content) if item.get("type") == "function_call"
    )
    assert all(
        "private response reasoning" not in str(chunk.message.content) for chunk in chunks[:2]
    )

    assert model_output_phase(AIMessage(content=[reasoning])) == "reasoning"


def test_deepseek_nonstream_response_preserves_reasoning_for_tool_replay() -> None:
    result = _model()._create_chat_result(
        {
            "id": "response-1",
            "object": "chat.completion",
            "created": 1,
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "private complete reasoning",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": "{}"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
    )

    message = result.generations[0].message
    assert message.content == ""
    assert message.additional_kwargs["reasoning_content"] == "private complete reasoning"
    assert message.tool_calls[0]["name"] == "lookup"


def test_deepseek_nonstream_response_discards_reasoning_without_tool_replay() -> None:
    result = _model()._create_chat_result(
        {
            "id": "response-2",
            "object": "chat.completion",
            "created": 1,
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "public answer",
                        "reasoning_content": "private final reasoning",
                    },
                    "finish_reason": "stop",
                }
            ],
        }
    )

    message = result.generations[0].message
    assert message.content == "public answer"
    assert "reasoning_content" not in message.additional_kwargs


def test_model_output_phase_prefers_public_action_for_assembled_messages() -> None:
    assert (
        model_output_phase(
            AIMessageChunk(content="", additional_kwargs={"reasoning_content": "thinking"})
        )
        == "reasoning"
    )
    assert (
        model_output_phase(
            AIMessage(
                content="I will use a tool.",
                additional_kwargs={"reasoning_content": "thinking"},
                tool_calls=[{"id": "call-1", "name": "lookup", "args": {}}],
            )
        )
        == "tool_calling"
    )
    assert (
        model_output_phase(
            AIMessage(content="answer", additional_kwargs={"reasoning_content": "thinking"})
        )
        == "answering"
    )


def test_reasoning_capabilities_require_a_trusted_deepseek_identity() -> None:
    spoofed = discovered_model_profile(
        {
            "provider_family": "deepseek",
            "reasoning": {
                "supported": True,
                "modes": ["off", "high", "max"],
                "default_mode": "off",
                "stream_field": "reasoning_content",
                "tool_roundtrip_required": True,
                "display_policy": "activity_only",
            },
        },
        model_id="attacker-model",
        display_name="Attacker model",
        service_base_url="https://attacker.example/v1",
    )
    assert spoofed["provider_family"] == "unknown"
    assert spoofed["reasoning"]["modes"] == ["off"]

    official_alias = discovered_model_profile(
        {
            "provider_family": "deepseek",
            "reasoning": {
                "supported": True,
                "modes": ["max"],
                "default_mode": "max",
                "stream_field": "reasoning_content",
                "tool_roundtrip_required": True,
                "display_policy": "activity_only",
            },
        },
        model_id="deepseek-v4-flash-max",
        display_name="DeepSeek V4 Flash Max",
        service_base_url="https://app.shejane.com/v1",
        trusted_model_catalog=True,
    )
    assert official_alias["reasoning"]["modes"] == ["max"]
    assert reasoning_mode_error({"profile": official_alias}, "max") is None
    assert reasoning_mode_error({"profile": official_alias}, "off") is not None

    direct = discovered_model_profile(
        {},
        model_id="deepseek-v4-flash",
        display_name="DeepSeek V4 Flash",
        service_base_url="https://api.deepseek.com",
    )
    assert direct["reasoning"]["modes"] == ["off", "high", "max"]
