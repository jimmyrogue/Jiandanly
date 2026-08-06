from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from shejane_runtime.llm.ledger import LedgerChatModel
from shejane_runtime.llm.runtime import RuntimeModelProxy, bind_runtime_model


class NamedModel(BaseChatModel):
    label: str

    @property
    def _llm_type(self) -> str:
        return "named-test-model"

    def bind_tools(self, _tools: Any, **_kwargs: Any) -> BaseChatModel:
        return self.model_copy(update={"label": f"{self.label}-bound"})

    async def _agenerate(
        self,
        _messages: list[BaseMessage],
        **_kwargs: Any,
    ) -> ChatResult:
        await asyncio.sleep(0)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.label))])

    def _generate(self, _messages: list[BaseMessage], **_kwargs: Any) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.label))])


class KwargModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "kwarg-test-model"

    async def _agenerate(
        self,
        _messages: list[BaseMessage],
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=str(kwargs.get("max_tokens"))))]
        )

    def _generate(self, _messages: list[BaseMessage], **kwargs: Any) -> ChatResult:
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=str(kwargs.get("max_tokens"))))]
        )


async def test_runtime_model_proxy_is_task_local_and_preserves_tool_binding() -> None:
    proxy = RuntimeModelProxy().bind_tools([{"name": "demo"}])

    async def invoke(label: str) -> str:
        with bind_runtime_model(NamedModel(label=label)):
            result = await proxy.ainvoke([("user", "hello")])
            return str(result.content)

    assert await asyncio.gather(invoke("alpha"), invoke("beta")) == [
        "alpha-bound",
        "beta-bound",
    ]


async def test_runtime_model_proxy_rejects_unbound_calls() -> None:
    with pytest.raises(RuntimeError, match="outside a bound execution"):
        await RuntimeModelProxy().ainvoke([("user", "hello")])


def test_runtime_model_proxy_reserves_parent_model_calls() -> None:
    model = LedgerChatModel(
        delegate=NamedModel(label="active"),
        store=object(),
        run_id="run-1",
        execution_attempt_id="job-1:1",
        model_name="local:test:model",
        max_calls=100,
    )

    with bind_runtime_model(model):
        active = RuntimeModelProxy(max_model_calls=95)._active()

    assert isinstance(active, LedgerChatModel)
    assert active.max_calls == 95


def test_runtime_model_proxy_scopes_summarization_ledger_calls() -> None:
    model = LedgerChatModel(
        delegate=NamedModel(label="active"),
        store=object(),
        run_id="run-1",
        execution_attempt_id="job-1:1",
        model_name="local:test:model",
        max_calls=100,
    )

    with bind_runtime_model(model):
        active = RuntimeModelProxy(
            call_purpose="summarization",
            max_model_calls=4,
        )._active()

    assert isinstance(active, LedgerChatModel)
    assert active.call_purpose == "summarization"
    assert active.max_calls == 4


async def test_runtime_model_proxy_caps_summarization_output() -> None:
    with bind_runtime_model(KwargModel()):
        result = await RuntimeModelProxy(max_output_tokens=1_024).ainvoke([("user", "summarize")])

    assert result.content == "1024"
