"""Task-local tool binding for reusable LangGraph definitions."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool

_CURRENT_TOOLS: ContextVar[dict[str, BaseTool] | None] = ContextVar(
    "shejane_runtime_tools",
    default=None,
)
_CURRENT_TOOL_EXECUTION: ContextVar[RuntimeToolExecution | None] = ContextVar(
    "shejane_runtime_tool_execution",
    default=None,
)


@dataclass(frozen=True, slots=True)
class RuntimeToolExecution:
    context: object
    operation_id: str
    tool_call_id: str


@contextmanager
def bind_runtime_tools(tools: dict[str, BaseTool]):
    token = _CURRENT_TOOLS.set(tools)
    try:
        yield
    finally:
        _CURRENT_TOOLS.reset(token)


@contextmanager
def bind_runtime_tool_execution(execution: RuntimeToolExecution):
    token = _CURRENT_TOOL_EXECUTION.set(execution)
    try:
        yield
    finally:
        _CURRENT_TOOL_EXECUTION.reset(token)


def current_runtime_tool_execution() -> RuntimeToolExecution:
    execution = _CURRENT_TOOL_EXECUTION.get()
    if execution is None:
        raise RuntimeError("runtime tool execution context is not bound")
    return execution


def current_runtime_tool_execution_or_none() -> RuntimeToolExecution | None:
    """Return the enclosing durable tool operation, if this call has one."""
    return _CURRENT_TOOL_EXECUTION.get()


class RuntimeToolProxy(BaseTool):
    """Schema-only tool definition that delegates to this execution's tool."""

    target_name: str

    @classmethod
    def from_tool(
        cls,
        tool: BaseTool,
        *,
        description: str | None = None,
        args_schema: dict[str, Any] | None = None,
    ) -> RuntimeToolProxy:
        return cls(
            name=tool.name,
            target_name=tool.name,
            description=description if description is not None else tool.description,
            args_schema=args_schema if args_schema is not None else tool.tool_call_schema,
            return_direct=tool.return_direct,
        )

    def _active(self) -> BaseTool:
        tools = _CURRENT_TOOLS.get() or {}
        tool = tools.get(self.target_name)
        if tool is None:
            raise RuntimeError(f"runtime tool is not bound: {self.target_name}")
        return tool

    def invoke(
        self,
        input: str | dict[str, Any],
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Any:
        """Delegate the original ToolCall so formatting happens exactly once."""
        return self._active().invoke(input, config=config, **kwargs)

    async def ainvoke(
        self,
        input: str | dict[str, Any],
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> Any:
        return await self._active().ainvoke(input, config=config, **kwargs)

    def _run(self, config: RunnableConfig | None = None, **kwargs: Any) -> Any:
        return self._active().invoke(kwargs, config=config)

    async def _arun(self, config: RunnableConfig | None = None, **kwargs: Any) -> Any:
        return await self._active().ainvoke(kwargs, config=config)
