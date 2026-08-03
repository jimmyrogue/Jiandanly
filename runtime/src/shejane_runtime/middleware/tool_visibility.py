"""Deterministic per-request tool visibility without changing graph identity."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from ..tools.mcp import MCP_TOOL_SEARCH_NAME, MCP_TOOL_SEARCH_RESULT_KIND

_OFFICE_SIGNALS = (
    "office.",
    ".docx",
    ".xlsx",
    ".pptx",
    "microsoft word",
    "microsoft excel",
    "powerpoint",
    "spreadsheet",
    "presentation",
    "workbook",
    "worksheet",
    "slide deck",
    "文档",
    "表格",
    "幻灯",
    "演示文稿",
    "工作簿",
    "工作表",
    "单元格",
)
_POST_ARTIFACT_FALLBACK_TOOLS = frozenset({"read_file", "execute", "task"})


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        function = tool.get("function")
        if isinstance(function, dict):
            return str(function.get("name") or "")
        return str(tool.get("name") or "")
    return str(getattr(tool, "name", "") or "")


def _message_text(message: Any) -> str:
    parts: list[str] = []
    text = getattr(message, "text", None)
    if isinstance(text, str):
        parts.append(text)
    else:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    value = block.get("text") or block.get("content")
                    if isinstance(value, str):
                        parts.append(value)
    for call in getattr(message, "tool_calls", ()) or ():
        if isinstance(call, dict):
            parts.append(str(call.get("name") or ""))
    name = getattr(message, "name", None)
    if isinstance(name, str):
        parts.append(name)
    return "\n".join(parts)


def visible_tools_for_messages(
    tools: Sequence[Any],
    messages: Sequence[Any],
    *,
    task_goal: str | None = None,
) -> list[Any]:
    """Return the request-visible subset without changing registered tools."""
    corpus = (
        [task_goal]
        if isinstance(task_goal, str) and task_goal.strip()
        else [_message_text(message) for message in messages]
    )
    text = "\n".join(corpus).lower()
    office_tools = [tool for tool in tools if _tool_name(tool).startswith("office.")]
    non_office_tools = [tool for tool in tools if not _tool_name(tool).startswith("office.")]
    if not any(signal in text for signal in _OFFICE_SIGNALS):
        return non_office_tools
    named = {
        name
        for tool in office_tools
        if (name := _tool_name(tool))
        and re.search(rf"(?<![\w.]){re.escape(name.lower())}(?![\w.])", text)
    }
    if named:
        return [tool for tool in tools if _tool_name(tool) in named or tool in non_office_tools]
    return list(tools)


def _mcp_search_result_names(messages: Sequence[Any]) -> set[str]:
    for message in reversed(messages):
        if getattr(message, "name", None) != MCP_TOOL_SEARCH_NAME:
            continue
        candidates = [getattr(message, "artifact", None), getattr(message, "content", None)]
        for candidate in candidates:
            if isinstance(candidate, str):
                try:
                    candidate = json.loads(candidate)
                except (TypeError, ValueError):
                    continue
            if not isinstance(candidate, dict):
                continue
            if candidate.get("kind") != MCP_TOOL_SEARCH_RESULT_KIND:
                continue
            tools = candidate.get("tools")
            if isinstance(tools, list):
                return {
                    str(item.get("name"))
                    for item in tools
                    if isinstance(item, dict) and item.get("name")
                }
        return set()
    return set()


def delivered_plugin_tool_name(messages: Sequence[Any]) -> str | None:
    for message in reversed(messages):
        if getattr(message, "type", None) == "human":
            return None
        if getattr(message, "type", None) != "tool":
            continue
        content = getattr(message, "content", None)
        if not isinstance(content, str):
            continue
        try:
            result = json.loads(content)
        except (TypeError, ValueError):
            continue
        if not isinstance(result, dict):
            continue
        provenance = result.get("provenance")
        if (
            result.get("status") == "succeeded"
            and isinstance(result.get("artifacts"), list)
            and result["artifacts"]
            and isinstance(provenance, dict)
            and isinstance(provenance.get("plugin"), dict)
        ):
            name = getattr(message, "name", None)
            return name if isinstance(name, str) and name else None
    return None


class ToolVisibilityMiddleware(AgentMiddleware):
    """Hide large optional tool families from a model request when irrelevant.

    The compiled graph still owns the complete tool set, so checkpoint forks and
    follow-up turns keep the same graph definition. The decision uses the current
    goal plus the complete retained message/tool-call history, not only one turn.
    """

    def __init__(
        self,
        *,
        deferred_tool_names: set[str] | None = None,
        blocked_tool_names: set[str] | None = None,
    ) -> None:
        super().__init__()
        self.deferred_tool_names = deferred_tool_names or set()
        self.blocked_tool_names = blocked_tool_names or set()

    @staticmethod
    def _apply(
        request: Any,
        deferred_tool_names: set[str] | None = None,
        blocked_tool_names: set[str] | None = None,
    ) -> Any:
        context = getattr(getattr(request, "runtime", None), "context", None)
        task_goal = getattr(context, "task_goal", None)
        visible = visible_tools_for_messages(
            request.tools,
            request.messages,
            task_goal=task_goal,
        )
        deferred = deferred_tool_names or set()
        if deferred:
            corpus = [task_goal] if isinstance(task_goal, str) else []
            corpus.extend(_message_text(message) for message in request.messages)
            text = "\n".join(corpus).lower()
            revealed = _mcp_search_result_names(request.messages)
            revealed.update(name for name in deferred if name.lower() in text)
            visible = [
                item
                for item in visible
                if _tool_name(item) not in deferred or _tool_name(item) in revealed
            ]
        blocked = blocked_tool_names or set()
        if blocked:
            visible = [item for item in visible if _tool_name(item) not in blocked]
        delivered_tool = delivered_plugin_tool_name(request.messages)
        if delivered_tool:
            visible = [
                item
                for item in visible
                if _tool_name(item) not in _POST_ARTIFACT_FALLBACK_TOOLS
                and _tool_name(item) != delivered_tool
            ]
        if len(visible) == len(request.tools):
            return request
        return request.override(tools=visible)

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return handler(
            self._apply(
                request,
                self.deferred_tool_names,
                self.blocked_tool_names,
            )
        )

    async def awrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        return await handler(
            self._apply(
                request,
                self.deferred_tool_names,
                self.blocked_tool_names,
            )
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        blocked = self._blocked_tool_result(request)
        return blocked if blocked is not None else handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        blocked = self._blocked_tool_result(request)
        return blocked if blocked is not None else await handler(request)

    def _blocked_tool_result(self, request: ToolCallRequest) -> ToolMessage | None:
        tool_name = str(request.tool_call.get("name") or "")
        if tool_name not in self.blocked_tool_names:
            return None
        return ToolMessage(
            content=f"Tool {tool_name} is not available to this Agent.",
            name=tool_name,
            tool_call_id=str(request.tool_call.get("id") or ""),
            status="error",
        )
