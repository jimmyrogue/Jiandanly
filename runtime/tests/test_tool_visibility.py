from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from shejane_runtime.middleware.tool_visibility import ToolVisibilityMiddleware
from shejane_runtime.tools.mcp import MCP_TOOL_SEARCH_RESULT_KIND, make_mcp_tool_search


@tool("office.read")
def office_read(path: str) -> str:
    """Read an Office document."""
    return path


@tool("workspace.read")
def workspace_read(path: str) -> str:
    """Read a workspace file."""
    return path


@tool("read_file")
def read_file(path: str) -> str:
    """Read a file."""
    return path


@tool("execute")
def execute(command: str) -> str:
    """Execute a shell command."""
    return command


@tool("task")
def task(description: str) -> str:
    """Delegate a task."""
    return description


@tool("plugin.example.archive.extract")
def archive_extract(input_id: str) -> str:
    """Extract an archive."""
    return input_id


@tool("plugin.example.text.summarize")
def text_summarize(input_id: str) -> str:
    """Summarize a text artifact."""
    return input_id


@tool("office.update_paragraph")
def office_update_paragraph(path: str) -> str:
    """Update an Office document paragraph."""
    return path


def _request(messages: list[Any], goal: str = "") -> Any:
    request = SimpleNamespace(
        messages=messages,
        tools=[office_read, office_update_paragraph, workspace_read],
        runtime=SimpleNamespace(context=SimpleNamespace(task_goal=goal)),
    )
    request.override = lambda **changes: SimpleNamespace(
        **{**request.__dict__, **changes, "override": request.override}
    )
    return request


def test_irrelevant_office_tools_are_hidden_only_for_the_model_request() -> None:
    original = _request([HumanMessage("explain this Python function")])

    filtered = ToolVisibilityMiddleware._apply(original)

    assert [item.name for item in filtered.tools] == ["workspace.read"]
    assert [item.name for item in original.tools] == [
        "office.read",
        "office.update_paragraph",
        "workspace.read",
    ]


def test_current_office_goal_keeps_office_tools() -> None:
    request = _request([HumanMessage("continue")], goal="edit quarterly-report.xlsx")

    filtered = ToolVisibilityMiddleware._apply(request)

    assert [item.name for item in filtered.tools] == [
        "office.read",
        "office.update_paragraph",
        "workspace.read",
    ]


def test_explicit_office_tool_name_reveals_only_that_tool() -> None:
    request = _request([HumanMessage("continue")], goal="Use office.read for this file")

    filtered = ToolVisibilityMiddleware._apply(request)

    assert [item.name for item in filtered.tools] == ["office.read", "workspace.read"]


def test_tool_output_cannot_enable_office_tools_for_an_unrelated_goal() -> None:
    messages = [
        HumanMessage("list files"),
        ToolMessage("['report.docx']", tool_call_id="1", name="ls"),
    ]

    filtered = ToolVisibilityMiddleware._apply(_request(messages, goal="list files"))

    assert [item.name for item in filtered.tools] == ["workspace.read"]


def test_office_follow_up_is_detected_from_retained_tool_history() -> None:
    messages = [
        HumanMessage("edit the deck"),
        AIMessage(
            content="",
            tool_calls=[{"name": "office.read", "args": {"path": "deck.pptx"}, "id": "1"}],
        ),
        ToolMessage("loaded", tool_call_id="1", name="office.read"),
        HumanMessage("continue with the second page and make it blue"),
    ]

    filtered = ToolVisibilityMiddleware._apply(_request(messages))

    assert [item.name for item in filtered.tools] == ["office.read", "workspace.read"]


def test_fork_goal_can_enable_office_without_changing_registered_tools() -> None:
    original = _request([HumanMessage("retry")], goal="update the presentation")

    filtered = ToolVisibilityMiddleware._apply(original)

    assert [item.name for item in filtered.tools] == [
        "office.read",
        "office.update_paragraph",
        "workspace.read",
    ]
    assert filtered.tools is original.tools


def test_delivered_plugin_artifacts_hide_fallback_tools_until_the_next_user_turn() -> None:
    result = {
        "status": "succeeded",
        "artifacts": [{"artifact_id": "artifact-1"}],
        "provenance": {"plugin": {"id": "example.archive"}},
    }
    request = _request(
        [
            HumanMessage("extract this archive"),
            ToolMessage(
                content=json.dumps(result),
                tool_call_id="extract-1",
                name="plugin.example.archive.extract",
            ),
        ]
    )
    request.tools = [read_file, execute, task, archive_extract, text_summarize]

    filtered = ToolVisibilityMiddleware._apply(request)

    assert [item.name for item in filtered.tools] == ["plugin.example.text.summarize"]
    request.messages.append(HumanMessage("now inspect it with a shell command"))
    assert ToolVisibilityMiddleware._apply(request) is request


def test_blocked_tools_are_hidden_even_when_the_goal_names_them() -> None:
    request = _request([HumanMessage("delegate this")], goal="use task to delegate")
    request.tools = [workspace_read, task]
    middleware = ToolVisibilityMiddleware(blocked_tool_names={"task"})

    filtered = middleware._apply(
        request,
        middleware.deferred_tool_names,
        middleware.blocked_tool_names,
    )

    assert [item.name for item in filtered.tools] == ["workspace.read"]


@pytest.mark.asyncio
async def test_blocked_tool_is_rejected_even_when_model_guesses_its_name() -> None:
    middleware = ToolVisibilityMiddleware(blocked_tool_names={"execute"})
    request = ToolCallRequest(
        tool_call={
            "id": "call-hidden-execute",
            "name": "execute",
            "args": {"command": "touch should-not-run"},
            "type": "tool_call",
        },
        tool=execute,
        state={"messages": []},
        runtime=SimpleNamespace(),
    )
    calls = 0

    async def handler(_request: ToolCallRequest) -> ToolMessage:
        nonlocal calls
        calls += 1
        return ToolMessage(
            content="executed",
            name="execute",
            tool_call_id="call-hidden-execute",
        )

    result = await middleware.awrap_tool_call(request, handler)

    assert calls == 0
    assert result.status == "error"
    assert result.content == "Tool execute is not available to this Agent."


@tool("docs_lookup")
def docs_lookup(query: str) -> str:
    """Search the product documentation for setup and API details."""
    return query


@tool("issues_create")
def issues_create(title: str) -> str:
    """Create an issue in the project tracker."""
    return title


def test_mcp_tool_search_returns_ranked_machine_readable_results() -> None:
    search = make_mcp_tool_search([docs_lookup, issues_create])

    result = search.invoke({"query": "API documentation", "limit": 1})

    assert result["kind"] == MCP_TOOL_SEARCH_RESULT_KIND
    assert [item["name"] for item in result["tools"]] == ["docs_lookup"]


def test_mcp_tools_are_hidden_until_search_reveals_them() -> None:
    search = make_mcp_tool_search([docs_lookup, issues_create])
    request = _request([HumanMessage("set up an integration")])
    request.tools = [workspace_read, docs_lookup, issues_create, search]
    middleware = ToolVisibilityMiddleware(deferred_tool_names={"docs_lookup", "issues_create"})

    initial = middleware._apply(request, middleware.deferred_tool_names)
    assert [item.name for item in initial.tools] == ["workspace.read", "mcp.search_tools"]

    request.messages.extend(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "mcp.search_tools",
                        "args": {"query": "API documentation"},
                        "id": "search-1",
                    }
                ],
            ),
            ToolMessage(
                content={
                    "kind": MCP_TOOL_SEARCH_RESULT_KIND,
                    "tools": [
                        {
                            "name": "docs_lookup",
                            "description": docs_lookup.description,
                        }
                    ],
                },
                tool_call_id="search-1",
                name="mcp.search_tools",
            ),
        ]
    )

    revealed = middleware._apply(request, middleware.deferred_tool_names)
    assert [item.name for item in revealed.tools] == [
        "workspace.read",
        "docs_lookup",
        "mcp.search_tools",
    ]
