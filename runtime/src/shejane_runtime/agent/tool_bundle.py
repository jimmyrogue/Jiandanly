"""Build the fixed per-attempt Runtime, MCP, and plugin tool bundle."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Settings
from ..plugins.browser_qa import BrowserQAActionExecutor, BrowserQAService
from ..plugins.catalog import PluginExecutionLease
from ..plugins.computer_use import ComputerUseActionExecutor, ComputerUseService
from ..plugins.ocr import OCRActionExecutor
from ..plugins.platforms import current_managed_worker_platform
from ..plugins.sandbox_runtime import SandboxRuntimeError
from ..plugins.tools import PluginActionError, PluginToolAdapter
from ..store.sqlite import LocalStore
from ..tools.mcp import MCP_TOOL_SEARCH_THRESHOLD, MCPToolCatalog, make_mcp_tool_search
from ..tools.runtime import RuntimeToolProxy
from .context_builder import RuntimeContext

DEEPAGENTS_TOOL_NAMES = {
    "write_todos",
    "task",
    "team.run",
    "child.spawn",
    "child.list",
    "child.check",
    "child.wait",
    "child.cancel",
    "mailbox.send",
    "mailbox.inbox",
    "mailbox.reply",
    "mailbox.ack",
    "ls",
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    "execute",
}


@dataclass(frozen=True)
class AgentToolBundle:
    tools: list[Any]
    dynamic_tool_map: dict[str, Any]
    deferred_tool_names: set[str]
    plugin_tool_versions: dict[str, str]


async def build_agent_tool_bundle(
    *,
    store: LocalStore,
    settings: Settings,
    runtime_context: RuntimeContext | None,
    workspace_root: str | None,
    resource_stack: AsyncExitStack | None,
    memory_enabled: bool,
    mcp_enabled: bool,
    mcp_disabled_servers: set[str] | None,
    mcp_catalog: MCPToolCatalog | None,
    plugin_lease: PluginExecutionLease | None,
    build_tools: Callable[..., Awaitable[list[Any]]],
    build_plugin_tool: Callable[..., Any],
    invoke_plugin_vision: Callable[..., Awaitable[dict[str, Any]]],
    load_linux_cgroup_resources: Callable[..., Any],
    load_macos_vm_resources: Callable[..., Any],
) -> AgentToolBundle:
    tools = await build_tools(runtime_context=runtime_context)
    catalog = mcp_catalog or MCPToolCatalog(settings.data_dir)
    if mcp_catalog is None and resource_stack is not None:
        resource_stack.push_async_callback(catalog.close)
    if mcp_enabled and resource_stack is not None:
        dynamic_tools = await resource_stack.enter_async_context(
            catalog.acquire_tools(
                disabled_servers=mcp_disabled_servers,
                reserved_names={tool.name for tool in tools} | DEEPAGENTS_TOOL_NAMES,
            )
        )
    elif mcp_enabled:
        dynamic_tools = await catalog.get_tools(
            disabled_servers=mcp_disabled_servers,
            reserved_names={tool.name for tool in tools} | DEEPAGENTS_TOOL_NAMES,
        )
    else:
        dynamic_tools = []

    async def vision_invoker(
        binding: Mapping[str, Any],
        params: dict[str, Any],
        input_root: Path,
        inputs: tuple[dict[str, Any], ...],
    ) -> dict[str, Any]:
        principal_id = runtime_context.principal_id if runtime_context is not None else None
        if not principal_id:
            raise PluginActionError(
                "model_binding_unavailable",
                "Vision Action is missing its Runtime principal",
            )
        return await invoke_plugin_vision(
            binding,
            params,
            input_root,
            inputs,
            store=store,
            principal_id=principal_id,
            settings=settings,
        )

    actions = plugin_lease.actions if plugin_lease else ()
    managed_worker_actions = any(action.execution_kind == "managed_worker" for action in actions)
    vm_resources = None
    if settings.managed_worker_vm_assets is not None and managed_worker_actions:
        try:
            vm_resources = load_macos_vm_resources(settings.managed_worker_vm_assets)
        except SandboxRuntimeError as exc:
            raise PluginActionError("executor_unavailable", str(exc)) from exc
    linux_cgroup = None
    if settings.managed_worker_linux_assets is not None and managed_worker_actions:
        try:
            linux_cgroup = load_linux_cgroup_resources(
                settings.managed_worker_linux_assets,
                host_platform=current_managed_worker_platform() or "unsupported",
            )
        except SandboxRuntimeError as exc:
            raise PluginActionError("executor_unavailable", str(exc)) from exc

    builtin_services: dict[str, ComputerUseService] = {}
    builtin_actions = [action for action in actions if action.execution_kind == "builtin"]
    if builtin_actions:
        if resource_stack is None:
            raise PluginActionError(
                "executor_unavailable", "Built-in plugins require a Runtime resource stack"
            )
        for action in builtin_actions:
            handler = action.execution_handler
            if handler == "ocr":
                if len(action.runtime_assets) != 1:
                    raise PluginActionError(
                        "executor_unavailable", "OCR requires one fixed Runtime Asset"
                    )
                continue
            if handler in builtin_services:
                continue
            if handler == "computer_use":
                service: ComputerUseService = ComputerUseService(
                    action.package_root,
                    workspace_root=Path(workspace_root) if workspace_root else settings.data_dir,
                )
            elif handler == "browser_qa":
                if len(action.runtime_assets) != 1:
                    raise PluginActionError(
                        "executor_unavailable", "Browser QA requires one fixed Runtime Asset"
                    )
                workspace_identity = hashlib.sha256(
                    str(workspace_root or settings.data_dir).encode("utf-8")
                ).hexdigest()[:24]
                service = BrowserQAService(
                    action.package_root,
                    workspace_root=Path(workspace_root) if workspace_root else settings.data_dir,
                    profile_root=settings.data_dir / "browser-qa" / "profiles" / workspace_identity,
                    browser_runtime_root=settings.data_dir / "browser-qa" / "runtime",
                    runtime_asset=action.runtime_assets[0],
                    headless=settings.browser_headless,
                )
            else:
                raise PluginActionError(
                    "executor_unavailable", f"Unknown built-in plugin handler: {handler}"
                )
            builtin_services[str(handler)] = service
            resource_stack.push_async_callback(service.aclose)

    plugin_tools = []
    for action in actions:
        adapter = None
        if action.execution_kind == "builtin":
            if action.execution_handler == "ocr":
                executor = OCRActionExecutor(action.package_root, action.runtime_assets[0])
            else:
                service = builtin_services[str(action.execution_handler)]
                executor = (
                    BrowserQAActionExecutor(service, action.action_id)
                    if action.execution_handler == "browser_qa"
                    else ComputerUseActionExecutor(service, action.action_id)
                )
            adapter = PluginToolAdapter(
                executor_factory=lambda _selected, executor=executor: executor
            )
        plugin_tools.append(
            build_plugin_tool(
                action,
                adapter=adapter,
                vision_invoker=vision_invoker,
                linux_cgroup=linux_cgroup,
                vm_resources=vm_resources,
            )
        )

    dynamic_tool_map = {item.name: item.tool for item in dynamic_tools}
    dynamic_tool_map.update({tool.name: tool for tool in plugin_tools})
    mcp_tool_names = {item.name for item in dynamic_tools}
    tools.extend(
        RuntimeToolProxy.from_tool(
            item.tool,
            description=item.description,
            args_schema=item.args_schema,
        )
        for item in dynamic_tools
    )
    tools.extend(RuntimeToolProxy.from_tool(tool) for tool in plugin_tools)
    deferred_tool_names = (
        mcp_tool_names if len(mcp_tool_names) >= MCP_TOOL_SEARCH_THRESHOLD else set()
    )
    if deferred_tool_names:
        tools.append(make_mcp_tool_search([item.tool for item in dynamic_tools]))
    if not memory_enabled:
        tools = [tool for tool in tools if not tool.name.startswith("memory.")]

    plugin_tool_versions = {}
    for item in dynamic_tools:
        version = (item.tool.metadata or {}).get("shejane_tool_version")
        if isinstance(version, str) and version:
            plugin_tool_versions[item.name] = version
    return AgentToolBundle(
        tools=tools,
        dynamic_tool_map=dynamic_tool_map,
        deferred_tool_names=deferred_tool_names,
        plugin_tool_versions=plugin_tool_versions,
    )
