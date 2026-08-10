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
import hashlib
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend
from deepagents.middleware import SkillsMiddleware
from langchain.agents.middleware import (
    AgentMiddleware,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
)
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.base import BaseStore
from langgraph.store.sqlite.aio import AsyncSqliteStore

from ..config import Settings, get_settings
from ..llm.ledger import LedgerChatModel
from ..llm.runtime import RuntimeModelProxy
from ..middleware.agent_mailbox import AgentMailboxMiddleware
from ..middleware.budget_control import (
    DynamicBudgetControlMiddleware,
    finalization_attempt_reserve,
)
from ..middleware.completion_router import CompletionRouterMiddleware
from ..middleware.file_write_conflict import FileWriteConflictMiddleware
from ..middleware.input_guard import InputGuardMiddleware
from ..middleware.outbound_policy import OutboundPolicyMiddleware
from ..middleware.plan_first import PlanFirstMiddleware
from ..middleware.steering import SteeringMiddleware
from ..middleware.tool_execution import ToolExecutionMiddleware
from ..middleware.tool_result_retry import ToolResultRetryMiddleware
from ..middleware.tool_review import ToolReviewMiddleware
from ..middleware.tool_visibility import (
    ToolVisibilityMiddleware,
    execution_policy_for_task,
)
from ..plugins.browser_qa import BrowserQAActionExecutor, BrowserQAService
from ..plugins.catalog import PluginExecutionLease
from ..plugins.computer_use import ComputerUseActionExecutor, ComputerUseService
from ..plugins.linux_cgroup import load_linux_cgroup_resources
from ..plugins.macos_vm import load_macos_vm_resources
from ..plugins.ocr import OCRActionExecutor
from ..plugins.platforms import current_managed_worker_platform
from ..plugins.sandbox_runtime import SandboxRuntimeError, configured_srt_launcher
from ..plugins.tools import PluginActionError, PluginToolAdapter, build_plugin_tool
from ..store.sqlite import MAX_DURABLE_CHILD_DEPTH, LocalStore
from ..tools.mcp import (
    MCP_TOOL_SEARCH_THRESHOLD,
    MCPToolCatalog,
    make_mcp_tool_search,
)
from ..tools.registry import build_tools, tool_definition
from ..tools.runtime import RuntimeToolProxy
from .backends import (
    ATTACHMENT_FILE_READ_MAX_MB,
    MODEL_FILE_READ_MAX_MB,
    ReadOnlyBackend,
    ReadOnlyFileBackend,
    RuntimeBackend,
    RuntimeFilesystemBackend,
    RuntimeLocalShellBackend,
)
from .child_runs import build_child_run_tools
from .context_builder import AsyncToolExecutionGate, RuntimeContext
from .mailbox import build_agent_mailbox_tools
from .model_runtime import (
    RuntimeModelMiddleware,
    _build_chat_model,
    _invoke_plugin_vision,
    _outbound_is_external,
    _outbound_pii_types,
    _register_model_cleanup,
)
from .model_runtime import (
    _build_byok_chat_model as _build_byok_chat_model,
)
from .prompt_middleware import RuntimePromptMiddleware
from .subagents import build_durable_child_definitions, build_subagents
from .team_graph import build_team_roster, build_team_tool

log = logging.getLogger("shejane_runtime.agent.builder")

_AGENT_DEFINITION_CACHE_MAX = 16
_AGENT_STATE_SCHEMA_VERSION = 2
MAX_SUBAGENT_TASKS_PER_RUN = 2
SIMPLE_MODEL_CALL_LIMIT = 12
COMPLEX_MODEL_CALL_SOFT_LIMIT = 24
_MAX_TEAM_RUNS_PER_RUN = 2
_MAX_CHILD_CONTROL_CALLS_PER_RUN = 16
_APPROVAL_REVIEW_MAX_CALLS = 20
_CLARIFICATION_REVIEW_MAX_CALLS = 4
_COMPLETION_REVIEW_MAX_CALLS = 4
_TITLE_GENERATION_MAX_CALLS = 1
_SUMMARIZATION_MAX_CALLS = 4
_SUMMARIZATION_MAX_OUTPUT_TOKENS = 1_024


def _agent_model_call_limit(configured_limit: int, task_goal: str | None) -> int:
    del task_goal
    return max(1, int(configured_limit))


def _agent_soft_model_call_limit(configured_limit: int, task_goal: str | None) -> int:
    policy = execution_policy_for_task(task_goal)
    return _agent_soft_model_call_limit_for_complexity(
        configured_limit,
        str(policy["complexity"]),
    )


def _agent_soft_model_call_limit_for_complexity(
    configured_limit: int,
    complexity: str,
) -> int:
    baseline = SIMPLE_MODEL_CALL_LIMIT if complexity == "simple" else COMPLEX_MODEL_CALL_SOFT_LIMIT
    return max(1, min(int(configured_limit), baseline))


def _agent_model_call_final_reserve(hard_limit: int) -> int:
    return min(2, max(1, hard_limit))


_SKILLS_SYSTEM_PROMPT = """<skills>
{skills_locations}
{skills_load_warnings}
Available Skills:
{skills_list}
For a relevant Skill, read its listed SKILL.md with read_file before following it.
</skills>"""

_DEEPAGENTS_TOOL_NAMES = {
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


# Read-only tools that benefit from auto-retry on transient failure.
# Consequential tools are deliberately excluded: after a timeout the Runtime
# cannot safely infer whether an external or filesystem side effect happened.
# We also exclude tools that use
# LangGraph control-flow exceptions (`interrupt()` → GraphInterrupt),
# because `ToolRetryMiddleware._handle_failure` would swallow that
# exception and convert it to a ToolMessage, defeating the pause.
RETRY_ELIGIBLE_TOOLS: list[str] = [
    "web.fetch",
    "read_file",
]


def _resolve_skills_dirs() -> list[Path]:
    """Return every existing skills directory the runtime should scan.

    We deliberately accept multiple roots so the agent can see skills
    from several ecosystems at once:

      1. `SHEJANE_RUNTIME_SKILLS_PATH` env var (comma-separated for
         multiple paths) — full override; when set, the defaults below
         are NOT consulted.
      2. Defaults (used when the env var is unset):
         - `~/.shejane/skills/` — our own canonical location
         - `~/.claude/skills/`  — Claude Code / skills.sh default install
           target (skills.sh CLI installs here when run with
           `--agent claude-code -g`, the most common case)

    Each entry is a `Path` that exists and is a directory. Missing
    paths are silently dropped so an unset Claude install doesn't error.
    """
    custom = os.environ.get("SHEJANE_RUNTIME_SKILLS_PATH", "").strip()
    if custom:
        raw_paths = [p.strip() for p in custom.split(",") if p.strip()]
    else:
        raw_paths = [
            str(Path.home() / ".shejane" / "skills"),
            str(Path.home() / ".claude" / "skills"),
        ]
    out: list[Path] = []
    for raw in raw_paths:
        candidate = Path(raw).expanduser()
        if candidate.is_dir():
            out.append(candidate)
    return out


def skill_catalog_fingerprint() -> str:
    """Hash the complete discovery tree visible to SkillsMiddleware.

    Discovery probes every directory directly below each configured root for
    ``SKILL.md``. Hashing the whole dedicated root therefore covers active
    packages, their supporting files, and directories that can enter or leave
    the catalog. Symlinks are hashed as links and are never traversed; the
    virtual backend rejects links that escape their configured root.
    """
    digest = hashlib.sha256(b"shejane-skill-catalog-v1\0")
    for root_index, root in enumerate(_resolve_skills_dirs()):
        resolved_root = root.resolve(strict=False)
        _update_catalog_digest(
            digest,
            "root",
            str(root_index),
            str(resolved_root),
        )
        for directory, child_dirs, file_names in os.walk(resolved_root, followlinks=False):
            child_dirs.sort()
            file_names.sort()
            directory_path = Path(directory)
            relative_directory = directory_path.relative_to(resolved_root)
            _update_catalog_digest(digest, "directory", relative_directory.as_posix())
            symlink_dirs = [name for name in child_dirs if (directory_path / name).is_symlink()]
            child_dirs[:] = [name for name in child_dirs if name not in symlink_dirs]
            for name in symlink_dirs:
                path = directory_path / name
                _update_catalog_digest(
                    digest,
                    "symlink",
                    (relative_directory / name).as_posix(),
                    os.readlink(path),
                )
            for name in file_names:
                path = directory_path / name
                relative_path = (relative_directory / name).as_posix()
                if path.is_symlink():
                    _update_catalog_digest(
                        digest,
                        "symlink",
                        relative_path,
                        os.readlink(path),
                    )
                    continue
                if not path.is_file():
                    _update_catalog_digest(
                        digest,
                        "special",
                        relative_path,
                        str(path.stat().st_mode),
                    )
                    continue
                _update_catalog_digest(digest, "file", relative_path)
                with path.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
                digest.update(b"\0")
    return digest.hexdigest()


def _update_catalog_digest(digest: Any, *parts: str) -> None:
    for part in parts:
        digest.update(part.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")


def _agent_backend_routes(
    *,
    skills_dirs: list[Path],
    memory_sources: list[str] | None,
    workspace_root: Path,
    attachment_bindings: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Return explicit filesystem routes that may live outside workspace.

    The main backend runs in `virtual_mode=True`, so absolute paths outside
    the selected workspace are blocked by default. SkillsMiddleware and
    MemoryMiddleware still need to read configured source directories; route
    only those exact roots through their own virtual backends.
    """
    routes: dict[str, Any] = {}
    for item in attachment_bindings or []:
        source = Path(item["source_path"])
        backend = ReadOnlyFileBackend(
            RuntimeFilesystemBackend(
                root_dir=source.parent,
                virtual_mode=True,
                max_file_size_mb=ATTACHMENT_FILE_READ_MAX_MB,
            ),
            source.name,
            display_name=Path(item["virtual_path"]).name,
        )
        routes[item["virtual_path"]] = backend
    for root in (path.expanduser() for path in skills_dirs):
        backend_root = root.resolve(strict=False)
        if workspace_root == backend_root or workspace_root.is_relative_to(backend_root):
            raise ValueError("writable workspace cannot be nested inside a read-only skill root")
        backend = ReadOnlyBackend(
            RuntimeFilesystemBackend(
                root_dir=backend_root,
                virtual_mode=True,
                max_file_size_mb=MODEL_FILE_READ_MAX_MB,
            )
        )
        for route in _absolute_route_keys(root):
            routes[route] = backend
        relative_route = _workspace_route(root, workspace_root, directory=True)
        if relative_route is not None:
            routes[relative_route] = backend
    for source in memory_sources or []:
        path = Path(source).expanduser()
        if path.is_dir():
            path = path / "AGENTS.md"
        backend = ReadOnlyFileBackend(
            RuntimeFilesystemBackend(
                root_dir=path.parent.resolve(strict=False),
                virtual_mode=True,
                max_file_size_mb=MODEL_FILE_READ_MAX_MB,
            ),
            path.name,
        )
        for route in _absolute_file_route_keys(path):
            routes[route] = backend
        relative_route = _workspace_route(path, workspace_root, directory=False)
        if relative_route is not None:
            routes[relative_route] = backend
    return routes


def _absolute_route_keys(path: Path) -> list[str]:
    expanded = path.expanduser()
    raw = expanded if expanded.is_absolute() else expanded.absolute()
    resolved = expanded.resolve(strict=False)
    keys = {
        str(raw).rstrip("/") + "/",
        str(resolved).rstrip("/") + "/",
    }
    return sorted(keys)


def _absolute_file_route_keys(path: Path) -> list[str]:
    expanded = path.expanduser()
    raw = expanded if expanded.is_absolute() else expanded.absolute()
    return sorted({str(raw), str(expanded.resolve(strict=False))})


def _workspace_route(path: Path, workspace_root: Path, *, directory: bool) -> str | None:
    try:
        relative = path.expanduser().resolve(strict=False).relative_to(workspace_root)
    except ValueError:
        return None
    route = "/" + relative.as_posix().lstrip("/")
    return route.rstrip("/") + "/" if directory else route


@dataclass(frozen=True)
class _StoreSandboxLedger:
    """Bind sandbox process records to the attempt that owns them."""

    store: LocalStore
    run_id: str
    execution_attempt_id: str

    async def record(self, *, pid: int, process_started_at: str, settings_path: str) -> str:
        return await self.store.record_sandbox_process(
            run_id=self.run_id,
            execution_attempt_id=self.execution_attempt_id,
            pid=pid,
            process_started_at=process_started_at,
            settings_path=settings_path,
        )

    async def forget(self, record_id: str) -> None:
        await self.store.forget_sandbox_process(record_id)


def _build_agent_backend(
    *,
    effective_workspace: str,
    skills_dirs: list[Path],
    memory_sources: list[str] | None,
    attachment_bindings: list[dict[str, str]] | None = None,
    sandbox_ledger: _StoreSandboxLedger | None = None,
):
    workspace_root = Path(effective_workspace).expanduser().resolve()
    default = RuntimeLocalShellBackend(
        root_dir=workspace_root,
        virtual_mode=True,
        sandbox_launcher=configured_srt_launcher(),
        sandbox_ledger=sandbox_ledger,
        env={
            key: os.environ[key]
            for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SHELL", "USER")
            if key in os.environ
        },
    )
    routes: dict[str, FilesystemBackend] = {}
    for route in _absolute_route_keys(Path(effective_workspace)):
        routes[route] = default
    routes.update(
        _agent_backend_routes(
            skills_dirs=skills_dirs,
            memory_sources=memory_sources,
            workspace_root=workspace_root,
            attachment_bindings=attachment_bindings,
        )
    )
    return CompositeBackend(default=default, routes=routes)


def _execution_scratch(
    settings: Settings,
    *,
    run_id: str,
    execution_attempt_id: str | None,
    resource_stack: AsyncExitStack | None,
) -> str:
    """Create one private filesystem root owned by this execution attempt."""
    settings.ensure_data_dir()
    parent = settings.data_dir / "execution-workspaces"
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    identity = f"{run_id}\0{execution_attempt_id or 'untracked'}"
    prefix = hashlib.sha256(identity.encode()).hexdigest()[:12] + "-"
    scratch = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    scratch.chmod(0o700)
    if resource_stack is None:
        shutil.rmtree(scratch)
        raise RuntimeError("no-workspace execution requires a resource stack")
    resource_stack.callback(shutil.rmtree, scratch)
    return str(scratch)


async def open_checkpointer(
    settings: Settings | None = None,
) -> tuple[AsyncSqliteSaver, AsyncExitStack]:
    """Open a long-lived AsyncSqliteSaver.

    Eager `await checkpointer.setup()` avoids the lazy-init disk-I/O race
    observed in the Phase 0 spike on macOS APFS.
    """
    settings = settings or get_settings()
    settings.ensure_data_dir()
    stack = AsyncExitStack()
    saver = await stack.enter_async_context(
        AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_db_path))
    )
    await saver.setup()
    log.info("checkpointer ready at %s", settings.checkpoint_db_path)
    return saver, stack


async def open_store(settings: Settings | None = None) -> tuple[BaseStore, AsyncExitStack]:
    """Open a long-lived `BaseStore` for cross-run durable memory.

    This is what explicit memory tools write into and what
    `langgraph.store.base.BaseStore`-aware tools read via `runtime.store`.

    Backed by `AsyncSqliteStore` on the runtime data dir — same WAL +
    eager-setup pattern as the checkpointer.
    """
    settings = settings or get_settings()
    settings.ensure_data_dir()
    stack = AsyncExitStack()
    store = await stack.enter_async_context(
        AsyncSqliteStore.from_conn_string(str(settings.store_db_path))
    )
    await store.setup()
    log.info("store ready at %s", settings.store_db_path)
    return store, stack


def _custom_middleware(
    settings: Settings,
    *,
    deferred_tool_names: set[str] | None = None,
) -> list[AgentMiddleware]:
    """Our middleware that deepagents doesn't auto-add.

    Order:
      InputGuard → ToolCallLimit → ToolRetry →
      durable model-call reservation →
      CompletionRouter

    `before_*` fire top-to-bottom, `after_*` fire bottom-to-top —
    CompletionRouter is the only custom after-model hook that may change the
    graph route. Execution settlement and cleanup are owned by RunCoordinator,
    outside the graph middleware chain.
    """
    middleware: list[AgentMiddleware] = [
        RuntimePromptMiddleware(),
        DynamicBudgetControlMiddleware(),
        RuntimeModelMiddleware(),
        ToolVisibilityMiddleware(
            deferred_tool_names=deferred_tool_names,
            blocked_tool_names={"task"} if not settings.enable_subagents else None,
        ),
        OutboundPolicyMiddleware(),
        InputGuardMiddleware(mode=settings.input_guard_mode),  # P1
        # Plan & Execute mode (off | always | auto; auto-skips trivial
        # tasks). Sourced from settings so the Advanced agent-settings
        # panel can override the SHEJANE_PLAN_FIRST env default per-run.
        PlanFirstMiddleware(mode=settings.plan_first_mode),
        ToolReviewMiddleware(),
        ToolExecutionMiddleware(),
        FileWriteConflictMiddleware(),
    ]
    middleware.extend(
        [
            ToolCallLimitMiddleware(  # P8
                tool_name="web.search",
                run_limit=settings.research_search_limit,
            ),
            ToolCallLimitMiddleware(
                tool_name="task",
                run_limit=MAX_SUBAGENT_TASKS_PER_RUN,
            ),
            ToolCallLimitMiddleware(
                tool_name="team.run",
                run_limit=_MAX_TEAM_RUNS_PER_RUN,
            ),
            *(
                ToolCallLimitMiddleware(
                    tool_name=tool_name,
                    run_limit=_MAX_CHILD_CONTROL_CALLS_PER_RUN,
                )
                for tool_name in (
                    "child.spawn",
                    "child.list",
                    "child.check",
                    "child.wait",
                    "child.cancel",
                    "mailbox.send",
                    "mailbox.inbox",
                    "mailbox.reply",
                    "mailbox.ack",
                )
            ),
            # Retry only network/IO-flaky tools, with a tight retryable
            # exception set. We deliberately exclude tools that use
            # `interrupt()` (user.ask, task, etc.) because
            # ToolRetryMiddleware's `_handle_failure` catches *any*
            # Exception (including GraphInterrupt) and converts it to a
            # ToolMessage — that would swallow our pause signals. Only
            # listing the tools we DO want retried (RETRY_ELIGIBLE_TOOLS)
            # keeps GraphInterrupt-flow tools out of its catch path.
            ToolRetryMiddleware(
                max_retries=settings.max_tool_retries,
                tools=list(RETRY_ELIGIBLE_TOOLS),
                retry_on=(
                    ConnectionError,
                    TimeoutError,
                    OSError,
                ),
            ),
            # Some tools return structured envelopes instead of raising.
            # Retry only when the envelope explicitly opts in with
            # `{ok:false, retryable:true}` and the tool is in the same
            # allowlist as exception retries.
            ToolResultRetryMiddleware(
                max_retries=settings.max_tool_retries,
                tools=list(RETRY_ELIGIBLE_TOOLS),
                initial_delay=0.25,
                max_delay=2.0,
            ),
        ]
    )
    middleware.extend(
        [
            CompletionRouterMiddleware(max_verification_repairs=settings.verification_repair_max),
        ]
    )
    return middleware


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

    tools = await build_tools(runtime_context=runtime_context)
    catalog = mcp_catalog or MCPToolCatalog(settings.data_dir)
    if mcp_catalog is None and resource_stack is not None:
        resource_stack.push_async_callback(catalog.close)
    if mcp_enabled and resource_stack is not None:
        dynamic_tools = await resource_stack.enter_async_context(
            catalog.acquire_tools(
                disabled_servers=mcp_disabled_servers,
                reserved_names={tool.name for tool in tools} | _DEEPAGENTS_TOOL_NAMES,
            )
        )
    elif mcp_enabled:
        dynamic_tools = await catalog.get_tools(
            disabled_servers=mcp_disabled_servers,
            reserved_names={tool.name for tool in tools} | _DEEPAGENTS_TOOL_NAMES,
        )
    else:
        dynamic_tools = []

    async def invoke_plugin_vision(
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
        return await _invoke_plugin_vision(
            binding,
            params,
            input_root,
            inputs,
            store=store,
            principal_id=principal_id,
            settings=settings,
        )

    managed_worker_actions = any(
        action.execution_kind == "managed_worker"
        for action in (plugin_lease.actions if plugin_lease else ())
    )
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

    actions = plugin_lease.actions if plugin_lease else ()
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
                vision_invoker=invoke_plugin_vision,
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
        tools = [t for t in tools if not t.name.startswith("memory.")]

    provider_model = _build_chat_model(
        settings,
        run_id,
        mode,
        model_binding=model_binding,
        model_api_key=model_api_key,
    )
    _register_model_cleanup(provider_model, resource_stack)
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
    model = (
        LedgerChatModel(
            delegate=provider_model,
            store=store,
            run_id=run_id,
            execution_attempt_id=execution_attempt_id,
            model_name=mode,
            max_calls=agent_model_call_limit,
            profile=getattr(provider_model, "profile", None),
        )
        if execution_attempt_id is not None
        else provider_model
    )
    approval_model = (
        model.model_copy(
            update={"call_purpose": "approval_review", "max_calls": _APPROVAL_REVIEW_MAX_CALLS}
        )
        if isinstance(model, LedgerChatModel)
        else model
    )
    clarification_model = (
        model.model_copy(
            update={
                "call_purpose": "clarification_review",
                "max_calls": _CLARIFICATION_REVIEW_MAX_CALLS,
            }
        )
        if isinstance(model, LedgerChatModel)
        else model
    )
    completion_model = (
        model.model_copy(
            update={
                "call_purpose": "completion_review",
                "max_calls": _COMPLETION_REVIEW_MAX_CALLS,
            }
        )
        if isinstance(model, LedgerChatModel)
        else model
    )
    title_model = (
        model.model_copy(
            update={"call_purpose": "title_generation", "max_calls": _TITLE_GENERATION_MAX_CALLS}
        )
        if isinstance(model, LedgerChatModel)
        else model
    )
    definition_model = RuntimeModelProxy(
        profile=getattr(model, "profile", None),
        call_purpose="summarization",
        max_model_calls=_SUMMARIZATION_MAX_CALLS,
        max_output_tokens=_SUMMARIZATION_MAX_OUTPUT_TOKENS,
    )
    subagent_model = RuntimeModelProxy(
        profile=getattr(model, "profile", None),
        max_model_calls=max(
            1,
            agent_model_call_limit
            - finalization_attempt_reserve(
                agent_model_call_limit,
                agent_model_call_final_reserve,
            ),
        ),
    )

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
        known = {tool.name for tool in tools} | _DEEPAGENTS_TOOL_NAMES
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
            execution_policy=execution_policy_for_task(task_goal),
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
            runtime_context.execution_policy = execution_policy_for_task(task_goal)
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

    for item in dynamic_tools:
        version = (item.tool.metadata or {}).get("shejane_tool_version")
        if isinstance(version, str) and version:
            runtime_context.plugin_tool_versions[item.name] = version

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


def _agent_definition_fingerprint(
    *,
    settings: Settings,
    model_profile: Any,
    tools: list[Any],
    subagents: list[Any] | None,
    skills: list[str] | None,
    skill_catalog_hash: str | None,
    memory: list[str] | None,
    plugin_catalog_hash: str | None,
    agent_definition_id: str = "shejane.default",
    agent_definition_version: str = "1",
    agent_role_prompt: str | None = None,
    allowed_tool_names: tuple[str, ...] = (),
) -> str:
    payload = {
        "version": _AGENT_STATE_SCHEMA_VERSION,
        "model_profile": model_profile,
        "tools": [tool_definition(tool) for tool in tools],
        "subagents": [
            {
                "name": item.get("name"),
                "description": item.get("description"),
                "system_prompt": item.get("system_prompt"),
                "tools": [tool_definition(tool) for tool in item.get("tools", [])],
                "middleware": [type(value).__qualname__ for value in item.get("middleware", [])],
            }
            for item in (subagents or [])
            if isinstance(item, dict)
        ],
        "skills": skills or [],
        "skill_catalog_hash": skill_catalog_hash,
        "memory": memory or [],
        "plugin_catalog_hash": plugin_catalog_hash,
        "durable_agent": {
            "id": agent_definition_id,
            "version": agent_definition_version,
            "role_prompt": agent_role_prompt,
            "allowed_tools": sorted(allowed_tool_names),
        },
        "middleware": {
            "max_model_calls": settings.max_model_calls,
            "input_guard": settings.input_guard_mode,
            "plan_first": settings.plan_first_mode,
            "research_limit": settings.research_search_limit,
            "tool_retries": settings.max_tool_retries,
            "verification_repairs": settings.verification_repair_max,
            "subagents": settings.enable_subagents,
            "browser_headless": settings.browser_headless,
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _cached_agent_definition(
    cache: dict[str, Any],
    fingerprint: str,
    compile_definition: Callable[[], Any],
) -> Any:
    if fingerprint in cache:
        definition = cache.pop(fingerprint)
        cache[fingerprint] = definition
        return definition
    definition = compile_definition()
    cache[fingerprint] = definition
    # ponytail: bounded process-local LRU; add durable cache only if compile
    # time remains material across runtime restarts.
    if len(cache) > _AGENT_DEFINITION_CACHE_MAX:
        cache.pop(next(iter(cache)))
    return definition


def _active_skill_names(skills_arg: list[str] | None) -> list[str]:
    """Best-effort: enumerate installed skill names from the skills
    directory so the ContextBuilder can hint the model that they're
    available. Empty list when skills are off / unresolved.

    The full SKILL.md bodies are loaded into the prompt by deepagents'
    SkillsMiddleware — this layer just primes the model that the
    skills exist (deepagents lists them too but earlier in the loop
    we want our own short echo so the `enabled_skills` priority sits
    above runtime context)."""
    if not skills_arg:
        return []
    names: list[str] = []
    for path_str in skills_arg:
        path = Path(path_str)
        if not path.is_dir():
            continue
        for entry in sorted(path.iterdir()):
            if entry.is_dir() and not entry.name.startswith("_"):
                names.append(entry.name)
    return names


def _resolve_memory_sources(settings: Settings) -> list[str] | None:
    """Parse SHEJANE_RUNTIME_MEMORY_PATHS (comma-separated paths) into the
    `memory=` argument of `create_deep_agent`. Each path is typically an
    `AGENTS.md` file or a directory of such files — `MemoryMiddleware`
    loads them into the system prompt at run start.

    None ⇒ memory loader skipped (MemoryMiddleware no-ops).
    """
    spec = (settings.memory_sources or "").strip()
    if not spec:
        return None
    items = [Path(p.strip()).expanduser() for p in spec.split(",") if p.strip()]
    # Deep Agents expects file paths. Preserve missing paths so its own
    # diagnostics remain useful, but normalize existing directories.
    expanded = [str(path / "AGENTS.md" if path.is_dir() else path) for path in items]
    return expanded or None
