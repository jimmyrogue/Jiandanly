"""Assemble the LangGraph agent via `create_deep_agent`.

We use `deepagents.create_deep_agent` instead of plain
`langchain.agents.create_agent` because it auto-assembles a sensible
batteries-included middleware stack and the SubAgent / Filesystem / Shell
integrations we need anyway. Our
remaining job is to:

  1. Bind the Runtime-selected BYOK model connection.
  2. Build a *narrow* tool list (everything outside the deepagents auto
     stack: time, environment, clipboard, local web fetch, MCP, browser).
  3. Pass per-run config: `subagents=`, `backend=`, `checkpointer=`.
  4. Append our custom middleware, including DeepAgents' SkillsMiddleware
     at the prompt tail so its absolute paths survive context compaction.

What deepagents auto-adds for us (we no longer wire these manually):

  TodoListMiddleware              ← planning (P3)
  FilesystemMiddleware            ← ls/read_file/write_file/edit_file
                                    + glob/grep + execute tools
  SubAgentMiddleware              ← when `subagents=` passed
  SummarizationMiddleware         ← auto context compaction
  PatchToolCallsMiddleware        ← orphan tool_call self-heal
  ToolExclusionMiddleware         ← conditional tool gating
  Prompt caching                  ← adapter middleware when supported
  MemoryMiddleware                ← AGENTS.md loader
  Tool review + durable receipts  ← our Runtime middleware, including subagents
"""

from __future__ import annotations

import asyncio
import logging
import shutil as shutil
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any

from deepagents import create_deep_agent
from deepagents.middleware import SkillsMiddleware
from langchain.agents.middleware import (
    AgentMiddleware,
)
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.base import BaseStore

from ..config import Settings, get_settings
from ..middleware.agent_mailbox import AgentMailboxMiddleware
from ..middleware.steering import SteeringMiddleware
from ..middleware.tool_visibility import (
    ToolVisibilityMiddleware,
)
from ..plugins.catalog import PluginExecutionLease
from ..plugins.linux_cgroup import load_linux_cgroup_resources
from ..plugins.macos_vm import load_macos_vm_resources
from ..plugins.tools import build_plugin_tool
from ..run_configuration import (
    _agent_model_call_final_reserve,
    _agent_model_call_limit,
    _agent_soft_model_call_limit,
    _execution_policy_snapshot,
)
from ..run_configuration import (
    _agent_soft_model_call_limit_for_complexity as _agent_soft_model_call_limit_for_complexity,
)
from ..store.sqlite import MAX_DURABLE_CHILD_DEPTH, LocalStore
from ..tools.mcp import (
    MCPToolCatalog,
)
from ..tools.registry import build_tools
from .backend_factory import (
    _build_agent_backend as _build_agent_backend,
)
from .backend_factory import (
    _execution_scratch as _execution_scratch,
)
from .backend_factory import (
    _StoreSandboxLedger as _StoreSandboxLedger,
)
from .backends import (
    RuntimeBackend,
)
from .child_runs import build_child_run_tools
from .context_builder import RuntimeContext
from .definition_cache import (
    _agent_definition_fingerprint as _agent_definition_fingerprint,
)
from .definition_cache import (
    _cached_agent_definition as _cached_agent_definition,
)
from .mailbox import build_agent_mailbox_tools
from .middleware_stack import (
    _custom_middleware as _custom_middleware,
)
from .model_runtime import (
    _APPROVAL_REVIEW_MAX_CALLS as _APPROVAL_REVIEW_MAX_CALLS,
)
from .model_runtime import (
    RuntimeModelMiddleware as RuntimeModelMiddleware,
)
from .model_runtime import (
    _build_byok_chat_model as _build_byok_chat_model,
)
from .model_runtime import (
    _build_chat_model as _build_chat_model,
)
from .model_runtime import (
    _build_run_model_bundle,
    _invoke_plugin_vision,
    _outbound_is_external,
    _outbound_pii_types,
)
from .model_runtime import (
    _register_model_cleanup as _register_model_cleanup,
)
from .persistence import open_checkpointer as open_checkpointer
from .persistence import open_store as open_store
from .prompt_middleware import RuntimePromptMiddleware as RuntimePromptMiddleware
from .skill_catalog import (
    _active_skill_names as _active_skill_names,
)
from .skill_catalog import (
    _resolve_memory_sources as _resolve_memory_sources,
)
from .skill_catalog import (
    _resolve_skills_dirs as _resolve_skills_dirs,
)
from .skill_catalog import (
    skill_catalog_fingerprint as skill_catalog_fingerprint,
)
from .subagents import build_durable_child_definitions, build_subagents
from .team_graph import build_team_roster, build_team_tool
from .tool_bundle import DEEPAGENTS_TOOL_NAMES, build_agent_tool_bundle
from .tool_execution_gate import AsyncToolExecutionGate

log = logging.getLogger("shejane_runtime.agent.builder")

_SKILLS_SYSTEM_PROMPT = """<skills>
{skills_locations}
{skills_load_warnings}
Available Skills:
{skills_list}
For a relevant Skill, read its listed SKILL.md with read_file before following it.
</skills>"""


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


async def build_agent(
    *,
    store: LocalStore,
    checkpointer: AsyncSqliteSaver,
    agent_store: BaseStore | None = None,
    workspace_root: str | None,
    attachment_bindings: list[dict[str, str]] | None = None,
    run_id: str,
    mode: str = "fast",
    task_goal: str | None = None,
    turn_count: int | None = None,
    repair_context: dict[str, Any] | None = None,
    retry_context: dict[str, Any] | None = None,
    memory_enabled: bool = True,
    skills_enabled: bool = True,
    skill_catalog_hash: str | None = None,
    mcp_enabled: bool = True,
    mcp_disabled_servers: set[str] | None = None,
    mcp_catalog: MCPToolCatalog | None = None,
    plugin_lease: PluginExecutionLease | None = None,
    settings: Settings | None = None,
    model_binding: dict[str, Any] | None = None,
    model_api_key: str | None = None,
    resource_stack: AsyncExitStack | None = None,
    execution_attempt_id: str | None = None,
    runtime_context: RuntimeContext | None = None,
    definition_cache: dict[str, Any] | None = None,
    definition_cache_lock: asyncio.Lock | None = None,
    steering_emit: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    extra_middleware: list[AgentMiddleware] | None = None,
) -> Any:
    """Build a compiled agent for one run via `create_deep_agent`.

    Args:
        store:           Runtime-level SQLite store (workspace lookups).
        checkpointer:    Shared AsyncSqliteSaver from `open_checkpointer`.
        workspace_root:  Authorized filesystem root for this run.
                         Becomes the FilesystemBackend's root_dir, which
                         deepagents' built-in FilesystemMiddleware + shell
                         execute use as their sandbox. None ⇒ virtual_mode.
        run_id:          LangGraph `thread_id` — unique per logical run.
        mode:            Runtime model selection stored with the run.
        task_goal:       Current user goal for this run. Echoed into the
                         <task> layer of the prompt so it survives long
                         tool-call chains.
        turn_count:      How many messages we're into the conversation
                         (incl. current user message). Used for the
                         <state> layer.
        repair_context:  Optional run metadata for a user-confirmed repair
                         attempt. Rendered into the <state> layer so the
                         model can distinguish repair from ordinary retry.
        retry_context:   Optional run metadata for a user-confirmed retry
                         attempt. Rendered into the <state> layer so the
                         model can avoid repeating the failed path blindly.
        memory_enabled:  When False, drops `memory.search` and `memory.write`
                         from the tool list.
                         The user toggle in agent settings flows in here
                         via RunCoordinator._settings_overrides.
        skills_enabled:  When False, omits SkillsMiddleware so no Skill
                         catalog is injected into the model prompt. Mirrors
                         the memory toggle pattern.
        mcp_enabled:     When False, omits MCP tools from this execution
                         entirely so no MCP tools land in the agent's tool
                         list. The discovered servers are still reported
                         via GET /v1/mcp-servers — only their
                         activation is suppressed. Same toggle pattern as
                         memory + skills.
        mcp_disabled_servers:
                         Per-server opt-out. Names in this set are
                         filtered before the Runtime MCP catalog is read.
                         Driven by the per-row switches in the client's
                         MCP tab; layered ON TOP of mcp_enabled — if the
                         master flag is off, this set is moot.
        mcp_catalog:     Runtime-owned MCP directory and Server Supervisor.
                         Runs acquire a fixed snapshot lease through the
                         execution resource stack.
        settings:        Override settings (tests).
        steering_emit:   Optional async event sink used by SteeringMiddleware
                         to mirror injected instructions onto the run SSE
                         stream after it drains the SQLite queue.
        extra_middleware: Appended after the built-in custom stack.
    """
    settings = settings or get_settings()
    if workspace_root is None and resource_stack is None:
        raise RuntimeError("no-workspace execution requires a resource stack")

    tool_bundle = await build_agent_tool_bundle(
        store=store,
        settings=settings,
        runtime_context=runtime_context,
        workspace_root=workspace_root,
        resource_stack=resource_stack,
        memory_enabled=memory_enabled,
        mcp_enabled=mcp_enabled,
        mcp_disabled_servers=mcp_disabled_servers,
        mcp_catalog=mcp_catalog,
        plugin_lease=plugin_lease,
        build_tools=build_tools,
        build_plugin_tool=build_plugin_tool,
        invoke_plugin_vision=_invoke_plugin_vision,
        load_linux_cgroup_resources=load_linux_cgroup_resources,
        load_macos_vm_resources=load_macos_vm_resources,
    )
    tools = tool_bundle.tools
    dynamic_tool_map = tool_bundle.dynamic_tool_map
    deferred_tool_names = tool_bundle.deferred_tool_names

    agent_model_call_limit = (
        runtime_context.model_call_hard_limit
        if runtime_context is not None and runtime_context.model_call_hard_limit is not None
        else _agent_model_call_limit(settings.max_model_calls, task_goal)
    )
    agent_model_call_soft_limit = (
        runtime_context.model_call_soft_limit
        if runtime_context is not None and runtime_context.model_call_soft_limit is not None
        else _agent_soft_model_call_limit(settings.max_model_calls, task_goal)
    )
    agent_model_call_final_reserve = (
        runtime_context.model_call_final_reserve
        if runtime_context is not None and runtime_context.model_call_final_reserve is not None
        else _agent_model_call_final_reserve(agent_model_call_limit)
    )
    models = _build_run_model_bundle(
        settings=settings,
        store=store,
        run_id=run_id,
        mode=mode,
        model_binding=model_binding,
        model_api_key=model_api_key,
        execution_attempt_id=execution_attempt_id,
        resource_stack=resource_stack,
        hard_limit=agent_model_call_limit,
        final_reserve=agent_model_call_final_reserve,
        phase_emit=(
            runtime_context.steering_emit
            if runtime_context is not None and callable(runtime_context.steering_emit)
            else None
        ),
        build_chat_model=_build_chat_model,
    )
    model = models.model
    approval_model = models.approval
    clarification_model = models.clarification
    completion_model = models.completion
    title_model = models.title
    definition_model = models.definition
    subagent_model = models.subagent

    skills_dirs = _resolve_skills_dirs() if skills_enabled else []
    skills_arg = [str(d) for d in skills_dirs] if skills_dirs else None
    effective_skill_catalog_hash = skill_catalog_hash if skills_arg else None
    if skills_arg and not effective_skill_catalog_hash:
        effective_skill_catalog_hash = skill_catalog_fingerprint()
    memory_arg = _resolve_memory_sources(settings)

    # FilesystemBackend serves three deepagents subsystems at once:
    #   - FilesystemMiddleware tools (ls / read_file / write_file / edit_file)
    #   - `execute` shell tool (run commands inside the sandbox)
    #   - SubAgentMiddleware (subagents share this scratch area)
    #   - SkillsMiddleware (reads `<skill-dir>/SKILL.md`)
    #
    # The default backend runs in virtual mode so the selected workspace
    # is a real path boundary. Skills and configured memory sources can
    # still live elsewhere, but only through explicit per-root routes.
    if workspace_root:
        effective_workspace = workspace_root
    else:
        effective_workspace = _execution_scratch(
            settings,
            run_id=run_id,
            execution_attempt_id=execution_attempt_id,
            resource_stack=resource_stack,
        )
    backend = _build_agent_backend(
        effective_workspace=effective_workspace,
        skills_dirs=skills_dirs,
        memory_sources=memory_arg,
        attachment_bindings=attachment_bindings,
        # Without an attempt to own them, sandbox records have nothing to be
        # reaped against, so untracked runs simply keep the old behaviour.
        sandbox_ledger=(
            _StoreSandboxLedger(
                store=store,
                run_id=run_id,
                execution_attempt_id=execution_attempt_id,
            )
            if execution_attempt_id is not None
            else None
        ),
    )

    is_durable_child = runtime_context is not None and runtime_context.run_kind == "child"
    subagents_arg = (
        build_subagents(
            main_tools=tools,
            main_model=subagent_model,
            deferred_tool_names=deferred_tool_names,
        )
        if settings.enable_subagents and not is_durable_child
        else None
    )
    child_definitions = build_durable_child_definitions(subagents_arg) if subagents_arg else {}
    if runtime_context is not None:
        runtime_context.child_agent_definitions = child_definitions
    if subagents_arg:
        tools.append(
            build_team_tool(
                roster=build_team_roster(subagents_arg),
                checkpointer=checkpointer,
            )
        )
    if (
        runtime_context is not None
        and runtime_context.child_run_control is not None
        and runtime_context.collaboration_depth < MAX_DURABLE_CHILD_DEPTH
        and child_definitions
    ):
        tools.extend(build_child_run_tools(child_definitions))
    if runtime_context is not None and runtime_context.agent_mailbox_control is not None:
        tools.extend(build_agent_mailbox_tools())

    middleware = _custom_middleware(
        settings,
        deferred_tool_names=deferred_tool_names,
    )
    middleware.insert(3, SteeringMiddleware())
    if runtime_context is not None and runtime_context.agent_mailbox_control is not None:
        middleware.insert(4, AgentMailboxMiddleware())
    if is_durable_child:
        allowed = set(runtime_context.allowed_tool_names) | {
            "mailbox.send",
            "mailbox.inbox",
            "mailbox.reply",
            "mailbox.ack",
        }
        known = {tool.name for tool in tools} | DEEPAGENTS_TOOL_NAMES
        visibility = next(item for item in middleware if isinstance(item, ToolVisibilityMiddleware))
        visibility.blocked_tool_names.update(known - allowed)

    if extra_middleware:
        middleware.extend(extra_middleware)
    if skills_arg:
        # DeepAgents normally installs SkillsMiddleware near the front of its
        # prompt stack. Filesystem and subagent instructions then push the
        # catalog into the middle of a large SystemMessage, where the context
        # envelope can split an absolute SKILL.md path. Keep the dependency's
        # loader/state contract, but place its prompt fragment at the tail.
        middleware.append(
            SkillsMiddleware(
                backend=RuntimeBackend(),
                sources=skills_arg,
                system_prompt=_SKILLS_SYSTEM_PROMPT,
            )
        )

    # Complete provider-independent prompt stack: Runtime identity and safety,
    # developer instructions, task, skills hint, run state, and environment.
    # See context_builder.py for the full layout.
    if runtime_context is None:
        runtime_context = RuntimeContext(
            run_id=run_id,
            store=store,
            steering_emit=steering_emit,
            backend=backend,
            model=model,
            model_call_soft_limit=agent_model_call_soft_limit,
            model_call_hard_limit=agent_model_call_limit,
            model_call_final_reserve=agent_model_call_final_reserve,
            execution_policy=_execution_policy_snapshot(
                task_goal or "",
                {
                    "max_model_calls": settings.max_model_calls,
                    "subagents": settings.enable_subagents,
                },
            ),
            approval_model=approval_model,
            clarification_model=clarification_model,
            completion_model=completion_model,
            title_model=title_model,
            dynamic_tools=dynamic_tool_map,
            execution_attempt_id=execution_attempt_id,
            subagents_enabled=bool(subagents_arg),
            tool_mutation_lock=AsyncToolExecutionGate(),
            outbound_is_external=_outbound_is_external(settings, model_binding),
            outbound_pii_types=_outbound_pii_types(settings.pii_redact_types),
            outbound_secrets=(model_api_key,) if model_api_key else (),
            memory_enabled=memory_enabled,
            plugin_catalog_hash=(plugin_lease.action_catalog_hash if plugin_lease else None),
            plugin_lease=plugin_lease,
            workspace_root=workspace_root,
            attachments=tuple(
                str(item.get("virtual_path"))
                for item in attachment_bindings or []
                if item.get("virtual_path")
            ),
            enabled_skills=_active_skill_names(skills_arg),
            task_goal=task_goal,
            mode=mode,
            turn_count=turn_count,
            repair_intent=bool(repair_context),
            repair_attempt=_int_or_none((repair_context or {}).get("attempt")),
            repair_max_attempts=_int_or_none((repair_context or {}).get("max_attempts")),
            repair_source_run_id=_str_or_none((repair_context or {}).get("source_run_id")),
            repair_source_message_id=_str_or_none((repair_context or {}).get("source_message_id")),
            repair_failure_category=_str_or_none((repair_context or {}).get("failure_category")),
            repair_failure_action_kind=_str_or_none(
                (repair_context or {}).get("failure_action_kind")
            ),
            retry_intent=bool(retry_context),
            retry_attempt=_int_or_none((retry_context or {}).get("attempt")),
            retry_source_run_id=_str_or_none((retry_context or {}).get("source_run_id")),
            retry_source_message_id=_str_or_none((retry_context or {}).get("source_message_id")),
            retry_failure_category=_str_or_none((retry_context or {}).get("failure_category")),
            retry_failure_action_kind=_str_or_none(
                (retry_context or {}).get("failure_action_kind")
            ),
        )
    else:
        runtime_context.enabled_skills = _active_skill_names(skills_arg)
        runtime_context.backend = backend
        runtime_context.model = model
        if runtime_context.model_call_soft_limit is None:
            runtime_context.model_call_soft_limit = agent_model_call_soft_limit
        if runtime_context.model_call_hard_limit is None:
            runtime_context.model_call_hard_limit = agent_model_call_limit
        if runtime_context.model_call_final_reserve is None:
            runtime_context.model_call_final_reserve = agent_model_call_final_reserve
        if not runtime_context.execution_policy:
            runtime_context.execution_policy = _execution_policy_snapshot(
                task_goal or "",
                {
                    "max_model_calls": settings.max_model_calls,
                    "subagents": settings.enable_subagents,
                },
            )
        runtime_context.approval_model = approval_model
        runtime_context.clarification_model = clarification_model
        runtime_context.completion_model = completion_model
        runtime_context.title_model = title_model
        runtime_context.execution_attempt_id = execution_attempt_id
        if not isinstance(runtime_context.tool_mutation_lock, AsyncToolExecutionGate):
            runtime_context.tool_mutation_lock = AsyncToolExecutionGate()
        runtime_context.outbound_is_external = _outbound_is_external(settings, model_binding)
        runtime_context.outbound_pii_types = _outbound_pii_types(settings.pii_redact_types)
        runtime_context.outbound_secrets = (model_api_key,) if model_api_key else ()
        runtime_context.dynamic_tools = dynamic_tool_map
        runtime_context.memory_enabled = memory_enabled
        runtime_context.subagents_enabled = bool(subagents_arg)
        runtime_context.plugin_catalog_hash = (
            plugin_lease.action_catalog_hash if plugin_lease else None
        )
        runtime_context.plugin_lease = plugin_lease
        runtime_context.attachments = tuple(
            str(item.get("virtual_path"))
            for item in attachment_bindings or []
            if item.get("virtual_path")
        )

    runtime_context.plugin_tool_versions.update(tool_bundle.plugin_tool_versions)

    fingerprint = _agent_definition_fingerprint(
        settings=settings,
        model_profile=getattr(definition_model, "profile", None),
        tools=tools,
        subagents=subagents_arg,
        skills=skills_arg,
        skill_catalog_hash=effective_skill_catalog_hash,
        memory=memory_arg,
        plugin_catalog_hash=(plugin_lease.action_catalog_hash if plugin_lease else None),
        agent_definition_id=runtime_context.agent_definition_id,
        agent_definition_version=runtime_context.agent_definition_version,
        agent_role_prompt=runtime_context.agent_role_prompt,
        allowed_tool_names=runtime_context.allowed_tool_names,
    )
    runtime_context.graph_definition_id = fingerprint

    def compile_definition() -> Any:
        return create_deep_agent(
            model=definition_model,
            tools=tools,
            middleware=middleware,
            subagents=subagents_arg,
            skills=None,
            memory=memory_arg,
            backend=RuntimeBackend(),
            checkpointer=checkpointer,
            store=agent_store,
            context_schema=RuntimeContext,
        )

    if definition_cache is None or extra_middleware:
        agent = compile_definition()
    elif definition_cache_lock is None:
        agent = _cached_agent_definition(definition_cache, fingerprint, compile_definition)
    else:
        async with definition_cache_lock:
            agent = _cached_agent_definition(definition_cache, fingerprint, compile_definition)
    nodes = getattr(agent, "nodes", {})
    tools_node = nodes.get("tools") if isinstance(nodes, dict) else None
    bound_tools = getattr(getattr(tools_node, "bound", None), "tools_by_name", {})
    runtime_context.tool_registry = dict(bound_tools) if isinstance(bound_tools, dict) else {}
    return agent
