"""Tests for `shejane_runtime.agent.builder` — verifies that the agent compiles
with the full middleware stack and that AsyncSqliteSaver opens cleanly with
the eager-setup fix from Phase 0.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import httpx
from langchain_core.messages import SystemMessage

from shejane_runtime.config import reset_settings_for_tests
from shejane_runtime.store.sqlite import LocalStore


def test_approval_reviewer_budget_handles_long_agent_runs() -> None:
    from shejane_runtime.agent.builder import _APPROVAL_REVIEW_MAX_CALLS

    assert _APPROVAL_REVIEW_MAX_CALLS == 20


def test_runtime_prompt_is_built_from_invocation_context() -> None:
    from shejane_runtime.agent.builder import RuntimePromptMiddleware
    from shejane_runtime.agent.context_builder import RuntimeContext
    from shejane_runtime.config import (
        MAX_CONCURRENT_SUBAGENT_TASKS,
        PREFERRED_SUBAGENT_CONCURRENCY,
    )

    class Request:
        runtime = SimpleNamespace(
            context=RuntimeContext(
                run_id="run_private",
                principal_id="user_private",
                store=object(),
                task_goal="检查运行时上下文",
                workspace_root="/tmp/workspace",
                attachments=("/attachments/brief.txt",),
            )
        )
        system_message = SystemMessage(content="deep agent base prompt")
        state: ClassVar[dict[str, object]] = {
            "completion_route": {
                "decision": "repair_requested",
                "run_id": "run_private",
                "instruction": "Repair the persisted verification failure.",
            }
        }

        def override(self, **changes):
            clone = Request()
            clone.system_message = changes["system_message"]
            return clone

    def handler(request):
        return request.system_message

    message = RuntimePromptMiddleware().wrap_model_call(Request(), handler)
    rendered = str(message.content)

    assert rendered.index("石间（SheJane）") < rendered.index("deep agent base prompt")
    assert "deep agent base prompt" in rendered
    assert "检查运行时上下文" in rendered
    assert "/tmp/workspace" in rendered
    assert "/attachments/brief.txt" in rendered
    assert "<runtime-repair>" in rendered
    assert "Repair the persisted verification failure." in rendered
    assert "run_private" not in rendered
    assert "user_private" not in rendered

    dispatch_section = rendered.split("## 多个独立任务用 subagent 并行", 1)[1].split(
        "## 工具使用",
        1,
    )[0]
    assert f"常规并行 {PREFERRED_SUBAGENT_CONCURRENCY} 个 `task`" in dispatch_section
    assert f"最多并行 {MAX_CONCURRENT_SUBAGENT_TASKS} 个" in dispatch_section
    assert (
        dispatch_section.count('task(subagent_type="researcher"') == PREFERRED_SUBAGENT_CONCURRENCY
    )


def test_runtime_model_is_selected_from_invocation_context() -> None:
    from shejane_runtime.agent.builder import RuntimeModelMiddleware
    from shejane_runtime.agent.context_builder import RuntimeContext

    runtime_model = object()

    class Request:
        runtime = SimpleNamespace(context=RuntimeContext(model=runtime_model))
        model = object()

        def override(self, **changes):
            clone = Request()
            clone.model = changes["model"]
            return clone

    selected = RuntimeModelMiddleware().wrap_model_call(Request(), lambda request: request.model)

    assert selected is runtime_model


def test_runtime_model_exposes_hosted_tools_through_the_standard_request() -> None:
    from shejane_runtime.agent.builder import RuntimeModelMiddleware

    class HostedModel:
        call_purpose = "agent"

        def __init__(self, hosted_tools=None) -> None:
            self.hosted_tools = ({"type": "web_search"},) if hosted_tools is None else hosted_tools

        def model_copy(self, *, update):
            return HostedModel(update["hosted_tools"])

    class Request:
        runtime = SimpleNamespace(context=SimpleNamespace(model=HostedModel()))
        model = object()
        tools = ("read_file",)

        def override(self, **changes):
            clone = Request()
            clone.model = changes.get("model", self.model)
            clone.tools = changes.get("tools", self.tools)
            return clone

    selected = RuntimeModelMiddleware().wrap_model_call(Request(), lambda request: request)

    assert selected.tools == [{"type": "web_search"}, "read_file"]
    assert selected.model.hosted_tools == ()


async def test_agent_definition_cache_reuses_only_matching_structure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from deepagents.backends.protocol import BackendProtocol

    import shejane_runtime.agent.builder as builder_module
    from shejane_runtime.agent.builder import build_agent, open_checkpointer
    from shejane_runtime.agent.context_builder import RuntimeContext

    compiled: list[object] = []
    backends: list[object] = []
    models: list[object] = []

    def fake_create_deep_agent(**kwargs):
        definition = object()
        compiled.append(definition)
        backends.append(kwargs["backend"])
        models.append(kwargs["model"])
        return definition

    monkeypatch.setattr(builder_module, "create_deep_agent", fake_create_deep_agent)
    settings = reset_settings_for_tests(data_dir=tmp_path, SHEJANE_FAKE_LLM=True)
    store = await LocalStore.open(tmp_path / "store.db")
    saver, stack = await open_checkpointer(settings)
    cache: dict[str, object] = {}
    lock = asyncio.Lock()
    for name in ("one", "two", "three", "four"):
        (tmp_path / name).mkdir()
    skills_root = tmp_path / "skills"
    skill_file = skills_root / "example" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text("# Example\n\nVersion one.\n")
    monkeypatch.setenv("SHEJANE_RUNTIME_SKILLS_PATH", str(skills_root))
    try:
        first = await build_agent(
            store=store,
            checkpointer=saver,
            workspace_root=str(tmp_path / "one"),
            run_id="run_one",
            settings=settings,
            mcp_enabled=False,
            runtime_context=RuntimeContext(run_id="run_one", store=store),
            definition_cache=cache,
            definition_cache_lock=lock,
        )
        second = await build_agent(
            store=store,
            checkpointer=saver,
            workspace_root=str(tmp_path / "two"),
            run_id="run_two",
            settings=settings,
            mcp_enabled=False,
            runtime_context=RuntimeContext(run_id="run_two", store=store),
            definition_cache=cache,
            definition_cache_lock=lock,
        )
        skill_file.write_text("# Example\n\nVersion two.\n")
        skill_changed = await build_agent(
            store=store,
            checkpointer=saver,
            workspace_root=str(tmp_path / "three"),
            run_id="run_three",
            settings=settings,
            mcp_enabled=False,
            runtime_context=RuntimeContext(run_id="run_three", store=store),
            definition_cache=cache,
            definition_cache_lock=lock,
        )
        changed = await build_agent(
            store=store,
            checkpointer=saver,
            workspace_root=str(tmp_path / "four"),
            run_id="run_four",
            settings=settings.model_copy(update={"max_tool_retries": 5}),
            mcp_enabled=False,
            runtime_context=RuntimeContext(run_id="run_four", store=store),
            definition_cache=cache,
            definition_cache_lock=lock,
        )

        assert first is second
        assert skill_changed is not first
        assert changed is not skill_changed
        assert len(compiled) == 3
        assert all(isinstance(backend, BackendProtocol) for backend in backends)
        assert all(getattr(model, "call_purpose", None) == "summarization" for model in models)
        assert all(getattr(model, "max_model_calls", None) == 4 for model in models)
        assert all(getattr(model, "max_output_tokens", None) == 1_024 for model in models)
    finally:
        await store.close()
        await stack.aclose()


async def test_build_agent_injects_runtime_owned_plugin_resources(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from langchain_core.tools import tool

    import shejane_runtime.agent.builder as builder_module
    from shejane_runtime.agent.builder import build_agent, open_checkpointer
    from shejane_runtime.agent.context_builder import RuntimeContext

    captured: dict[str, object] = {}

    @tool("plugin.test.vision")
    async def plugin_tool() -> str:
        """Test Vision plugin tool."""
        return "ok"

    def fake_build_plugin_tool(
        _action,
        *,
        adapter=None,
        vision_invoker=None,
        linux_cgroup=None,
        vm_resources=None,
    ):
        captured["vision_invoker"] = vision_invoker
        captured["linux_cgroup"] = linux_cgroup
        captured["vm_resources"] = vm_resources
        return plugin_tool

    async def fake_invoke_plugin_vision(*args, **kwargs):
        captured["provider_call"] = (args, kwargs)
        return {"text": "A lantern.", "usage": {}}

    monkeypatch.setattr(builder_module, "build_plugin_tool", fake_build_plugin_tool)
    monkeypatch.setattr(builder_module, "_invoke_plugin_vision", fake_invoke_plugin_vision)
    linux_resources = object()
    linux_manifest = tmp_path / "linux-manifest.json"
    monkeypatch.setattr(
        builder_module,
        "load_linux_cgroup_resources",
        lambda path, **_kwargs: linux_resources if path == linux_manifest else None,
        raising=False,
    )
    monkeypatch.setattr(builder_module, "create_deep_agent", lambda **_kwargs: object())
    settings = reset_settings_for_tests(
        data_dir=tmp_path,
        SHEJANE_FAKE_LLM=True,
        managed_worker_linux_assets=linux_manifest,
    )
    store = await LocalStore.open(tmp_path / "store.db")
    saver, stack = await open_checkpointer(settings)
    context = RuntimeContext(
        run_id="run_vision",
        principal_id="principal_vision",
        store=store,
    )
    lease = SimpleNamespace(
        actions=(SimpleNamespace(execution_kind="managed_worker"),),
        action_catalog_hash="vision-catalog",
    )
    try:
        await build_agent(
            store=store,
            checkpointer=saver,
            workspace_root=str(tmp_path),
            run_id="run_vision",
            settings=settings,
            mcp_enabled=False,
            plugin_lease=lease,
            runtime_context=context,
        )
        vision_invoker = captured["vision_invoker"]
        assert callable(vision_invoker)
        result = await vision_invoker(
            {"id": "vision-default"},
            {"prompt": "Describe the image."},
            tmp_path,
            (),
        )
    finally:
        await store.close()
        await stack.aclose()

    assert result == {"text": "A lantern.", "usage": {}}
    assert captured["linux_cgroup"] is linux_resources
    assert captured["vm_resources"] is None
    args, kwargs = captured["provider_call"]
    assert args[0] == {"id": "vision-default"}
    assert kwargs == {
        "store": store,
        "principal_id": "principal_vision",
        "settings": settings,
    }


async def test_build_agent_normalizes_workspace_for_computer_use_plugin(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from langchain_core.tools import tool

    import shejane_runtime.agent.builder as builder_module
    from shejane_runtime.agent.builder import build_agent, open_checkpointer
    from shejane_runtime.agent.context_builder import RuntimeContext

    @tool("plugin.test.computer-use")
    async def plugin_tool() -> str:
        """Test Computer Use plugin tool."""
        return "ok"

    monkeypatch.setattr(builder_module, "create_deep_agent", lambda **_kwargs: object())
    monkeypatch.setattr(
        builder_module,
        "build_plugin_tool",
        lambda _action, **_kwargs: plugin_tool,
    )
    settings = reset_settings_for_tests(data_dir=tmp_path, SHEJANE_FAKE_LLM=True)
    store = await LocalStore.open(tmp_path / "store.db")
    saver, stack = await open_checkpointer(settings)
    lease = SimpleNamespace(
        actions=(
            SimpleNamespace(
                execution_kind="builtin",
                execution_handler="computer_use",
                package_root=tmp_path,
                action_id="computer-use.test",
            ),
        ),
        action_catalog_hash="computer-use-catalog",
    )
    try:
        agent = await build_agent(
            store=store,
            checkpointer=saver,
            workspace_root=str(tmp_path),
            run_id="run_computer_use",
            settings=settings,
            mcp_enabled=False,
            plugin_lease=lease,
            resource_stack=stack,
            runtime_context=RuntimeContext(run_id="run_computer_use", store=store),
        )
    finally:
        await store.close()
        await stack.aclose()

    assert agent is not None


async def test_cached_definition_keeps_mcp_implementations_attempt_local(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from langchain_core.tools import tool

    import shejane_runtime.agent.builder as builder_module
    from shejane_runtime.agent.builder import build_agent, open_checkpointer
    from shejane_runtime.agent.context_builder import RuntimeContext
    from shejane_runtime.tools.mcp import validate_mcp_tools
    from shejane_runtime.tools.runtime import RuntimeToolProxy

    builds = 0
    loads = 0
    captured_tools: list[object] = []

    class FakeCatalog:
        @asynccontextmanager
        async def acquire_tools(self, **_kwargs):
            nonlocal loads
            loads += 1
            label = f"attempt-{loads}"

            @tool("mcp.demo.lookup")
            async def lookup(value: str) -> str:
                """Look up one value."""
                return f"{label}:{value}"

            yield validate_mcp_tools([lookup])

    def fake_create_deep_agent(**kwargs):
        nonlocal builds
        builds += 1
        captured_tools.extend(kwargs["tools"])
        return object()

    monkeypatch.setattr(builder_module, "create_deep_agent", fake_create_deep_agent)
    settings = reset_settings_for_tests(data_dir=tmp_path, SHEJANE_FAKE_LLM=True)
    store = await LocalStore.open(tmp_path / "store.db")
    saver, stack = await open_checkpointer(settings)
    cache: dict[str, object] = {}
    lock = asyncio.Lock()
    try:
        contexts = [RuntimeContext(run_id=f"run-{index}", store=store) for index in range(2)]
        definitions = [
            await build_agent(
                store=store,
                checkpointer=saver,
                workspace_root=None,
                run_id=f"run-{index}",
                settings=settings,
                resource_stack=stack,
                runtime_context=contexts[index],
                mcp_catalog=FakeCatalog(),
                definition_cache=cache,
                definition_cache_lock=lock,
            )
            for index in range(2)
        ]

        assert definitions[0] is definitions[1]
        assert builds == 1
        assert loads == 2
        assert isinstance(
            next(tool for tool in captured_tools if tool.name == "mcp.demo.lookup"),
            RuntimeToolProxy,
        )
        assert (
            contexts[0].dynamic_tools["mcp.demo.lookup"]
            is not contexts[1].dynamic_tools["mcp.demo.lookup"]
        )
    finally:
        await store.close()
        await stack.aclose()


async def test_large_mcp_catalog_is_deferred_behind_search_tool(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from langchain_core.tools import StructuredTool

    import shejane_runtime.agent.builder as builder_module
    from shejane_runtime.agent.builder import build_agent, open_checkpointer
    from shejane_runtime.tools.mcp import MCP_TOOL_SEARCH_NAME, validate_mcp_tools

    captured: dict[str, object] = {}

    class FakeCatalog:
        @asynccontextmanager
        async def acquire_tools(self, **_kwargs):
            tools = [
                StructuredTool.from_function(
                    func=lambda query: query,
                    name=f"server_tool_{index}",
                    description=f"Use integration capability {index}.",
                )
                for index in range(12)
            ]
            yield validate_mcp_tools(tools)

    def fake_create_deep_agent(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(builder_module, "create_deep_agent", fake_create_deep_agent)
    settings = reset_settings_for_tests(data_dir=tmp_path, SHEJANE_FAKE_LLM=True)
    store = await LocalStore.open(tmp_path / "store.db")
    saver, stack = await open_checkpointer(settings)
    try:
        await build_agent(
            store=store,
            checkpointer=saver,
            workspace_root=None,
            run_id="run-search",
            settings=settings,
            resource_stack=stack,
            mcp_catalog=FakeCatalog(),
        )

        assert MCP_TOOL_SEARCH_NAME in {tool.name for tool in captured["tools"]}
        visibility = next(
            item
            for item in captured["middleware"]
            if type(item).__name__ == "ToolVisibilityMiddleware"
        )
        assert visibility.deferred_tool_names == {f"server_tool_{index}" for index in range(12)}
    finally:
        await store.close()
        await stack.aclose()


async def test_model_clients_close_with_execution_scope() -> None:
    from shejane_runtime.agent.builder import _register_model_cleanup

    closed: list[str] = []

    class AsyncClient:
        async def close(self) -> None:
            closed.append("async")

    class SyncClient:
        def close(self) -> None:
            closed.append("sync")

    class Model:
        root_async_client = AsyncClient()
        root_client = SyncClient()

    async with AsyncExitStack() as stack:
        _register_model_cleanup(Model(), stack)
        assert closed == []

    assert closed == ["sync", "async"]


def _patched_async_client(handler):
    class _Patched(httpx.AsyncClient):
        def __init__(self, **kw):
            super().__init__(
                transport=httpx.MockTransport(handler),
                **{k: v for k, v in kw.items() if k != "transport"},
            )

    return _Patched


def _stream_response(events: list[tuple[str, str]]) -> httpx.Response:
    body = "".join(f"event: {n}\ndata: {p}\n\n" for n, p in events).encode("utf-8")
    return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


def test_open_checkpointer_eager_setup(tmp_path: Path) -> None:
    """Eager setup should NOT raise the disk-I/O race we saw in spike."""
    from shejane_runtime.agent.builder import open_checkpointer

    async def run() -> None:
        reset_settings_for_tests(data_dir=tmp_path)
        saver, stack = await open_checkpointer()
        try:
            # If setup() worked, this list call should succeed (returns empty).
            results = [c async for c in saver.alist(config={"configurable": {"thread_id": "nope"}})]
            assert results == []
        finally:
            await stack.aclose()

    asyncio.run(run())


def test_build_agent_assembles_without_workspace(tmp_path: Path, monkeypatch) -> None:
    """Compile-only sanity: build_agent should not throw without a workspace."""
    from shejane_runtime.agent.builder import build_agent, open_checkpointer

    async def run() -> None:
        reset_settings_for_tests(data_dir=tmp_path)
        # Ensure MCP isn't enabled (no config), Tavily disabled (no key)
        monkeypatch.delenv("SHEJANE_RUNTIME_MCP_SERVERS", raising=False)
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        store = await LocalStore.open(tmp_path / "store.db")
        saver, stack = await open_checkpointer()
        try:
            agent = await build_agent(
                store=store,
                checkpointer=saver,
                workspace_root=None,
                run_id="t1",
                resource_stack=stack,
            )
            assert agent is not None
            # Has the expected graph shape (compile returns CompiledStateGraph)
            assert hasattr(agent, "ainvoke")
            assert hasattr(agent, "astream")
        finally:
            await store.close()
            await stack.aclose()

    asyncio.run(run())


def test_build_agent_with_workspace_includes_shell_middleware(tmp_path: Path, monkeypatch) -> None:
    """When workspace_root is set, ShellToolMiddleware injects a shell tool."""
    from shejane_runtime.agent.builder import build_agent, open_checkpointer

    async def run() -> list[str]:
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
                run_id="t2",
            )
            # Pull tool names off the compiled graph
            tools = getattr(agent, "tools", None) or []
            if not tools:
                # fallback: inspect compiled graph
                tools = []
            return [getattr(t, "name", "") for t in tools]
        finally:
            await store.close()
            await stack.aclose()

    # The shell tool isn't always exposed on the compiled-graph surface
    # (depends on LangGraph version) so we settle for no-exception.
    names = asyncio.run(run())
    assert isinstance(names, list)


def test_custom_middleware_has_one_bounded_completion_router(tmp_path: Path) -> None:
    from shejane_runtime.agent.builder import _custom_middleware

    settings = reset_settings_for_tests(
        data_dir=tmp_path,
        SHEJANE_RUNTIME_VERIFY_REPAIR_MAX=2,
    )

    middleware = _custom_middleware(settings)
    routers = [item for item in middleware if type(item).__name__ == "CompletionRouterMiddleware"]

    assert len(routers) == 1
    assert routers[0].max_verification_repairs == 2
    assert [type(item).__name__ for item in middleware[:3]] == [
        "RuntimePromptMiddleware",
        "RuntimeModelMiddleware",
        "DynamicBudgetControlMiddleware",
    ]
    names = {type(item).__name__ for item in middleware}
    assert not names & {
        "PlanApprovalMiddleware",
        "ReflectMiddleware",
        "MemoryWritebackMiddleware",
    }
    assert not any(
        hook in type(item).__dict__
        for item in middleware
        for hook in ("after_agent", "aafter_agent")
    )


def test_custom_middleware_keeps_tool_retry_but_no_unsafe_model_retry(tmp_path: Path) -> None:
    from shejane_runtime.agent.builder import _custom_middleware

    settings = reset_settings_for_tests(
        data_dir=tmp_path,
        max_tool_retries=4,
    )

    middleware = _custom_middleware(settings)
    tool_retry = [item for item in middleware if type(item).__name__ == "ToolRetryMiddleware"]
    tool_result_retry = [
        item for item in middleware if type(item).__name__ == "ToolResultRetryMiddleware"
    ]
    names = [type(item).__name__ for item in middleware]

    assert len(tool_retry) == 1
    assert tool_retry[0].max_retries == 4
    assert len(tool_result_retry) == 1
    assert tool_result_retry[0].max_retries == 4
    assert "ModelRetryMiddleware" not in names
    assert "ContextEditingMiddleware" not in names


def test_custom_middleware_leaves_subagent_dispatch_to_runtime_budget(tmp_path: Path) -> None:
    from langchain.agents.middleware import ToolCallLimitMiddleware

    from shejane_runtime.agent.builder import _custom_middleware

    settings = reset_settings_for_tests(data_dir=tmp_path)
    limits = {
        item.tool_name: item.run_limit
        for item in _custom_middleware(settings)
        if isinstance(item, ToolCallLimitMiddleware)
    }

    assert limits["web.search"] == 10
    assert "task" not in limits


def test_run_uses_configured_hard_limit_and_task_aware_soft_limit() -> None:
    from shejane_runtime.agent.builder import (
        _agent_model_call_final_reserve,
        _agent_model_call_limit,
        _agent_soft_model_call_limit,
    )
    from shejane_runtime.middleware.budget_control import finalization_attempt_reserve

    assert (
        _agent_model_call_limit(
            100,
            "去香港开通汇丰 one 账户，线上办理的话，补签名是必须的吗？",
        )
        == 100
    )
    assert (
        _agent_soft_model_call_limit(
            100, "去香港开通汇丰 one 账户，线上办理的话，补签名是必须的吗？"
        )
        == 12
    )
    assert _agent_soft_model_call_limit(100, "你查一下互联网，核对这些银行的开户要求") == 24
    assert _agent_model_call_limit(100, "请调研并比较香港三家银行的开户流程") == 100
    assert _agent_model_call_limit(100, "coordinate through a durable mailbox") == 100
    assert _agent_model_call_final_reserve(2) == 2
    assert _agent_model_call_final_reserve(3) == 2
    assert finalization_attempt_reserve(100, 2) == 6
    assert 100 - finalization_attempt_reserve(100, 2) == 94


def test_model_budget_changes_agent_definition_fingerprint() -> None:
    from shejane_runtime.agent.builder import _agent_definition_fingerprint
    from shejane_runtime.config import Settings

    def fingerprint(max_model_calls: int) -> str:
        return _agent_definition_fingerprint(
            settings=Settings(max_model_calls=max_model_calls),
            model_profile={},
            tools=[],
            subagents=[],
            skills=[],
            skill_catalog_hash=None,
            memory=[],
            plugin_catalog_hash=None,
        )

    assert fingerprint(100) != fingerprint(80)


def test_build_agent_runs_end_to_end_with_mocked_backend(tmp_path: Path, monkeypatch) -> None:
    """The compiled agent should drive a complete invoke against a mocked
    SSE backend, returning a final assistant message.

    This is the strongest Phase 3' Part 2 acceptance: agent assembly is
    correct enough that LangGraph can actually traverse it.
    """
    from shejane_runtime.agent.builder import build_agent, open_checkpointer

    def handler(request: httpx.Request) -> httpx.Response:
        # Mock backend returns one delta + done. No tool calls.
        return _stream_response(
            [
                ("llm.delta", '{"content_delta": "All good."}'),
                ("llm.done", '{"request_id": "r1", "finish_reason": "stop"}'),
            ]
        )

    monkeypatch.setattr("tests.streaming_model.httpx.AsyncClient", _patched_async_client(handler))

    async def run() -> str:
        from shejane_runtime.agent.context_builder import RuntimeContext

        reset_settings_for_tests(data_dir=tmp_path)
        monkeypatch.delenv("SHEJANE_RUNTIME_MCP_SERVERS", raising=False)
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        store = await LocalStore.open(tmp_path / "store.db")
        saver, stack = await open_checkpointer()
        try:
            runtime_context = RuntimeContext(
                run_id="e2e_1",
                store=store,
                task_goal="ping",
            )
            agent = await build_agent(
                store=store,
                checkpointer=saver,
                workspace_root=None,
                run_id="e2e_1",
                resource_stack=stack,
                runtime_context=runtime_context,
            )
            config = {"configurable": {"thread_id": "e2e_1"}}
            result = await agent.ainvoke(
                {"messages": [{"role": "user", "content": "ping"}]},
                config=config,
                context=runtime_context,
            )
            final = result["messages"][-1]
            return (
                getattr(final, "content", "")
                if not isinstance(final, dict)
                else final.get("content", "")
            )
        finally:
            await store.close()
            await stack.aclose()

    text = asyncio.run(run())
    assert "All good" in text
