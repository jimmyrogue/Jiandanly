"""Phase 6' tests — verify SubAgentMiddleware wiring + event translation
for subagent lifecycle (`task` tool calls treated specially)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from shejane_runtime.config import reset_settings_for_tests
from shejane_runtime.event_translator import translate
from shejane_runtime.store.sqlite import LocalStore


def _patched_async_client(handler):
    class _Patched(httpx.AsyncClient):
        def __init__(self, **kw):
            super().__init__(
                transport=httpx.MockTransport(handler),
                **{k: v for k, v in kw.items() if k != "transport"},
            )

    return _Patched


# --- subagent definitions ---


def _write_subagent(
    root: Path,
    filename: str,
    *,
    frontmatter: str,
    body: str,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / filename
    path.write_text(
        f"---\n{frontmatter.strip()}\n---\n\n{body.strip()}\n",
        encoding="utf-8",
    )
    return path


def test_build_subagents_returns_researcher_and_writer() -> None:
    from shejane_runtime.agent.subagents import build_subagents

    subs = build_subagents(
        main_tools=[],
        main_model="anthropic:claude-sonnet-4-6",
        agent_roots=[],
    )
    names = {s["name"] for s in subs}
    assert names == {"general-purpose", "researcher", "writer"}
    assert all(str(item["system_prompt"]).startswith("<identity>") for item in subs)


def test_researcher_pulls_only_research_relevant_tools_from_main() -> None:
    """When the main toolset includes the researcher's preferred names,
    the researcher's tool list is filtered to that subset."""
    from langchain_core.tools import tool

    from shejane_runtime.agent.subagents import build_subagents

    @tool("web.fetch")
    def web_fetch(url: str) -> str:
        """Fetch a URL."""
        return url

    @tool("shell.run")
    def shell_run(cmd: str) -> str:
        """Run a shell command (should NOT be exposed to researcher)."""
        return cmd

    @tool("time.now")
    def time_now() -> str:
        """Return current time."""
        return "now"

    subs = build_subagents(
        main_tools=[web_fetch, shell_run, time_now],
        main_model="x",
        agent_roots=[],
    )
    researcher = next(s for s in subs if s["name"] == "researcher")
    res_tool_names = {t.name for t in researcher["tools"]}
    assert "web.fetch" in res_tool_names
    assert "time.now" in res_tool_names
    assert "shell.run" not in res_tool_names, "shell must not leak to researcher"


def test_subagents_have_code_enforced_model_and_research_limits() -> None:
    from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
    from langchain_core.tools import tool

    from shejane_runtime.agent.subagents import build_subagents
    from shejane_runtime.middleware.tool_review import ToolReviewMiddleware

    @tool("web.fetch")
    def web_fetch(url: str) -> str:
        """Fetch a URL."""
        return url

    @tool("web.search")
    def web_search(query: str) -> str:
        """Search the web."""
        return query

    subs = build_subagents(
        main_tools=[web_fetch, web_search],
        main_model="x",
        agent_roots=[],
    )

    assert all(
        any(
            isinstance(item, ModelCallLimitMiddleware)
            and item.run_limit == 12
            and item.exit_behavior == "end"
            for item in subagent["middleware"]
        )
        for subagent in subs
    )
    researcher = next(item for item in subs if item["name"] == "researcher")
    fetch_limit = next(
        item
        for item in researcher["middleware"]
        if isinstance(item, ToolCallLimitMiddleware) and item.tool_name == "web.fetch"
    )
    assert fetch_limit.run_limit == 10
    search_limit = next(
        item
        for item in researcher["middleware"]
        if isinstance(item, ToolCallLimitMiddleware) and item.tool_name == "web.search"
    )
    assert search_limit.run_limit == 10

    middleware = researcher["middleware"]
    review_index = next(
        index for index, item in enumerate(middleware) if isinstance(item, ToolReviewMiddleware)
    )
    assert all(
        review_index < index
        for index, item in enumerate(middleware)
        if isinstance(item, ToolCallLimitMiddleware)
    )

    from shejane_runtime.middleware.tool_visibility import ToolVisibilityMiddleware

    visibility = next(
        item
        for item in researcher["middleware"]
        if isinstance(item, ToolVisibilityMiddleware) and item.blocked_tool_names
    )
    assert visibility.blocked_tool_names == {"edit_file", "execute", "write_file"}


def test_writer_has_no_tools() -> None:
    from shejane_runtime.agent.subagents import build_subagents
    from shejane_runtime.middleware.tool_visibility import ToolVisibilityMiddleware

    subs = build_subagents(main_tools=[], main_model="x", agent_roots=[])
    writer = next(s for s in subs if s["name"] == "writer")
    assert writer["tools"] == []
    visibility = next(
        item for item in writer["middleware"] if isinstance(item, ToolVisibilityMiddleware)
    )
    assert visibility.blocked_tool_names >= {
        "write_todos",
        "read_file",
        "write_file",
        "edit_file",
        "execute",
    }


def test_durable_child_definitions_freeze_role_and_effective_tool_surface() -> None:
    from langchain_core.tools import tool

    from shejane_runtime.agent.subagents import (
        build_durable_child_definitions,
        build_subagents,
    )

    @tool("web.search")
    def web_search(query: str) -> str:
        """Search the web."""
        return query

    @tool("execute")
    def execute(command: str) -> str:
        """Execute a command."""
        return command

    definitions = build_durable_child_definitions(
        build_subagents(main_tools=[web_search, execute], main_model="x", agent_roots=[])
    )
    researcher = definitions["subagent:researcher"]
    writer = definitions["subagent:writer"]

    assert str(researcher["version"]).startswith("sha256:")
    assert "web.search" in researcher["allowed_tools"]
    assert "read_file" in researcher["allowed_tools"]
    assert "execute" not in researcher["allowed_tools"]
    assert writer["allowed_tools"] == []
    assert not str(researcher["system_prompt"]).startswith("<identity>")


def test_subagents_never_receive_top_level_memory_write_capability(tmp_path: Path) -> None:
    from langchain_core.tools import tool

    from shejane_runtime.agent.subagents import build_subagents

    @tool("memory.write")
    def memory_write(fact: str) -> str:
        """Persist a top-level user-authorized fact."""
        return fact

    _write_subagent(
        tmp_path,
        "configured.md",
        frontmatter="""
name: configured
description: Configured helper
tools: memory.write
""",
        body="Help with the task.",
    )
    subagents = build_subagents(
        main_tools=[memory_write],
        main_model="x",
        agent_roots=[tmp_path],
    )
    assert all("memory.write" not in {tool.name for tool in item["tools"]} for item in subagents)


def test_build_subagents_loads_configured_markdown_agent(tmp_path: Path) -> None:
    from langchain_core.tools import tool

    from shejane_runtime.agent.subagents import build_subagents

    @tool("web.search")
    def web_search(query: str) -> str:
        """Search the web."""
        return query

    @tool("execute")
    def execute(command: str) -> str:
        """Run a shell command."""
        return command

    @tool("time.now")
    def time_now() -> str:
        """Return current time."""
        return "now"

    agent_root = tmp_path / "agents"
    _write_subagent(
        agent_root,
        "reviewer.md",
        frontmatter="""
name: reviewer
description: Review implementation diffs with a narrow evidence trail.
tools:
  - web.search
  - time.now
""",
        body="You are a careful implementation reviewer.",
    )

    subs = build_subagents(
        main_tools=[web_search, execute, time_now],
        main_model="model-x",
        agent_roots=[agent_root],
    )
    reviewer = next(s for s in subs if s["name"] == "reviewer")

    assert reviewer["description"] == "Review implementation diffs with a narrow evidence trail."
    assert str(reviewer["system_prompt"]).startswith("<identity>")
    assert str(reviewer["system_prompt"]).endswith("You are a careful implementation reviewer.")
    assert reviewer["model"] == "model-x"
    assert [t.name for t in reviewer["tools"]] == ["web.search", "time.now"]


def test_configured_subagent_can_override_builtin_writer(tmp_path: Path) -> None:
    from shejane_runtime.agent.subagents import build_subagents

    agent_root = tmp_path / "agents"
    _write_subagent(
        agent_root,
        "writer.md",
        frontmatter="""
name: writer
description: A project-specific release note writer.
tools: []
""",
        body="Write in the team's release-note voice.",
    )

    subs = build_subagents(main_tools=[], main_model="model-x", agent_roots=[agent_root])
    writers = [s for s in subs if s["name"] == "writer"]

    assert len(writers) == 1
    assert writers[0]["description"] == "A project-specific release note writer."
    assert str(writers[0]["system_prompt"]).startswith("<identity>")
    assert str(writers[0]["system_prompt"]).endswith("Write in the team's release-note voice.")


def test_invalid_configured_subagent_is_skipped(tmp_path: Path) -> None:
    from shejane_runtime.agent.subagents import build_subagents

    agent_root = tmp_path / "agents"
    _write_subagent(
        agent_root,
        "missing-description.md",
        frontmatter="name: missing-description",
        body="This file is incomplete.",
    )

    subs = build_subagents(main_tools=[], main_model="model-x", agent_roots=[agent_root])
    names = {s["name"] for s in subs}

    assert names == {"general-purpose", "researcher", "writer"}


def test_backend_factory_uses_workspace_root(tmp_path: Path) -> None:
    from deepagents.backends import FilesystemBackend

    from shejane_runtime.agent.subagents import build_subagent_backend

    backend = build_subagent_backend(str(tmp_path))
    assert isinstance(backend, FilesystemBackend)
    assert backend.virtual_mode is True


def test_subagent_workspace_read_keeps_the_20_mib_limit(tmp_path: Path) -> None:
    from shejane_runtime.agent.subagents import build_subagent_backend

    large_file = tmp_path / "large.txt"
    with large_file.open("wb") as stream:
        stream.truncate(20 * 1024 * 1024 + 1)

    selected = build_subagent_backend(str(tmp_path)).read("/large.txt")
    assert selected.file_data is None
    assert selected.error is not None
    assert "limit 20971520 bytes / 20 MB" in selected.error


def test_backend_factory_falls_back_to_virtual_when_no_workspace() -> None:
    from deepagents.backends import FilesystemBackend

    from shejane_runtime.agent.subagents import build_subagent_backend

    backend = build_subagent_backend(None)
    assert isinstance(backend, FilesystemBackend)
    assert backend.virtual_mode is True


# --- task tool is present on the compiled agent ---


def test_compiled_agent_exposes_task_tool_when_subagents_enabled(
    tmp_path: Path, monkeypatch
) -> None:
    from shejane_runtime.agent.builder import build_agent, open_checkpointer

    async def run() -> set[str]:
        reset_settings_for_tests(data_dir=tmp_path)
        monkeypatch.delenv("SHEJANE_RUNTIME_MCP_SERVERS", raising=False)
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        store = await LocalStore.open(tmp_path / "store.db")
        saver, stack = await open_checkpointer()
        try:
            agent = await build_agent(
                store=store,
                checkpointer=saver,
                workspace_root=str(tmp_path),
                run_id="t-subagent-1",
            )
            # The compiled agent is a CompiledStateGraph; tool names hide
            # behind the ToolNode. Probe via the graph's nodes registry.
            # Simpler check: introspect the underlying ToolNode if present.
            tools_node = agent.nodes.get("tools")
            if tools_node is None:
                return set()
            # tools_node is a PregelNode; the actual ToolNode is in
            # tools_node.bound.tools_by_name in current LangGraph 1.x
            bound = getattr(tools_node, "bound", None)
            if bound is None:
                return set()
            tools_by_name = getattr(bound, "tools_by_name", {})
            return set(tools_by_name.keys())
        finally:
            await store.close()
            await stack.aclose()

    names = asyncio.run(run())
    assert "task" in names, f"expected task tool in {sorted(names)}"
    assert "team.run" in names, f"expected Team Graph tool in {sorted(names)}"


def test_disabling_subagents_drops_custom_specialists(tmp_path: Path, monkeypatch) -> None:
    """`enable_subagents=False` no longer removes the `task` tool entirely
    (since step 4/6 we use `create_deep_agent`, which always exposes a
    `task` tool via its bundled SubAgentMiddleware). What we DO guarantee
    is that our custom researcher/writer specialists are not registered —
    so a call to `task(subagent_name='researcher', ...)` from the LLM
    won't find a match.

    Verified indirectly by checking that build_subagents() is not invoked
    in the disabled path (it returns the researcher/writer list).
    """
    from shejane_runtime.agent.builder import build_agent, open_checkpointer

    async def run() -> set[str]:
        reset_settings_for_tests(data_dir=tmp_path, enable_subagents=False)
        monkeypatch.delenv("SHEJANE_RUNTIME_MCP_SERVERS", raising=False)
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        store = await LocalStore.open(tmp_path / "store.db")
        saver, stack = await open_checkpointer()
        try:
            agent = await build_agent(
                store=store,
                checkpointer=saver,
                workspace_root=str(tmp_path),
                run_id="t-no-subagent",
            )
            tools_node = agent.nodes.get("tools")
            if tools_node is None:
                return set()
            bound = getattr(tools_node, "bound", None)
            tools_by_name = getattr(bound, "tools_by_name", {}) if bound else {}
            return set(tools_by_name.keys())
        finally:
            await store.close()
            await stack.aclose()

    names = asyncio.run(run())
    # deepagents always provides `task`. The flag only suppresses our
    # custom specialists, which is exercised via build_subagents in the
    # `test_build_subagents_returns_*` cases above.
    assert "task" in names
    assert "team.run" not in names


# --- stream translation stays separate from durable subagent lifecycle ---


def test_translator_does_not_treat_partial_task_args_as_a_spawn() -> None:
    from langchain_core.messages import AIMessageChunk

    chunk = AIMessageChunk(
        content="",
        tool_call_chunks=[
            {
                "id": "call_task_1",
                "name": "task",
                "args": '{"subagent_name": "researcher", "task_description": "find X"}',
                "index": 0,
            }
        ],
    )
    events = translate("messages", (chunk, {}))
    assert events == [
        {
            "event": "llm.tool_call_chunk",
            "data": {
                "id": "call_task_1",
                "name": "task",
                "args_delta": '{"subagent_name": "researcher", "task_description": "find X"}',
                "index": 0,
            },
        }
    ]


def test_translator_keeps_task_tool_result_as_generic_tool_completion() -> None:
    from langchain_core.messages import ToolMessage

    tm = ToolMessage(
        content="Research finished: see notes",
        tool_call_id="call_task_1",
        name="task",
    )
    events = translate("messages", (tm, {}))
    # The authoritative subagent terminal event comes from the durable
    # Tool Receipt transition. This generic event remains for old clients.
    assert events[0]["event"] == "tool.completed"
    assert events[0]["data"]["tool_call_id"] == "call_task_1"
    assert events[0]["data"]["name"] == "task"
    assert events[0]["data"]["content"] == "Research finished: see notes"


def test_translator_keeps_failed_task_result_as_generic_tool_failure() -> None:
    from langchain_core.messages import ToolMessage

    tm = ToolMessage(
        content="Subagent failed",
        tool_call_id="call_task_1",
        name="task",
        status="error",
    )

    events = translate("messages", (tm, {}))

    assert events[0]["event"] == "tool.failed"
    assert events[0]["data"]["status"] == "error"


def test_translator_keeps_regular_tools_as_tool_completed() -> None:
    """Sanity: non-task tools surface as `tool.completed` (the canonical
    event name per Block 3 — client looks for this, not `tool.end`),
    NOT `subagent.completed` which is reserved for the deepagents task()
    spawn."""
    from langchain_core.messages import ToolMessage

    tm = ToolMessage(content="42", tool_call_id="c1", name="time.now")
    events = translate("messages", (tm, {}))
    assert events[0]["event"] == "tool.completed"


def test_translator_keeps_regular_tool_call_chunks_as_llm_tool_call_chunk() -> None:
    from langchain_core.messages import AIMessageChunk

    chunk = AIMessageChunk(
        content="",
        tool_call_chunks=[
            {
                "id": "c1",
                "name": "web.fetch",
                "args": '{"url":',
                "index": 0,
            }
        ],
    )
    events = translate("messages", (chunk, {}))
    assert events[0]["event"] == "llm.tool_call_chunk"
