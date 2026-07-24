"""Assemble the LangGraph agent via `create_deep_agent`.

We use `deepagents.create_deep_agent` instead of plain
`langchain.agents.create_agent` because it auto-assembles a sensible
batteries-included middleware stack and the SubAgent / Skills /
Filesystem / Shell integrations we need anyway. Our
remaining job is to:

  1. Bind the Runtime-selected BYOK model connection.
  2. Build a *narrow* tool list (everything outside the deepagents auto
     stack: time, environment, clipboard, local web fetch, MCP, browser).
  3. Pass per-run config: `subagents=`, `skills=`, `backend=`,
     `checkpointer=`.
  4. Append our custom middleware (5 phase hooks + retry/limit knobs)
     into the user-middleware slot.

What deepagents auto-adds for us (we no longer wire these manually):

  TodoListMiddleware              ← planning (P3)
  SkillsMiddleware                ← when `skills=` passed
  FilesystemMiddleware            ← ls/read_file/write_file/edit_file
                                    + glob/grep + execute tools
  SubAgentMiddleware + Async      ← when `subagents=` passed
  SummarizationMiddleware         ← auto context compaction
  PatchToolCallsMiddleware        ← orphan tool_call self-heal
  ToolExclusionMiddleware         ← conditional tool gating
  Prompt caching                  ← adapter middleware when supported
  MemoryMiddleware                ← AGENTS.md loader
  Tool review + durable receipts  ← our Runtime middleware, including subagents
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import httpx
from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend
from langchain.agents.middleware import (
    AgentMiddleware,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
)
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.base import BaseStore
from langgraph.store.sqlite.aio import AsyncSqliteStore
from PIL import Image, UnidentifiedImageError

from ..config import Settings, get_settings
from ..llm.ledger import LedgerChatModel
from ..llm.runtime import RuntimeModelProxy
from ..middleware.completion_router import (
    CompletionRouterMiddleware,
    completion_repair_instruction,
)
from ..middleware.file_write_conflict import FileWriteConflictMiddleware
from ..middleware.input_guard import InputGuardMiddleware
from ..middleware.outbound_policy import OutboundPolicyMiddleware, sanitize_outbound_text
from ..middleware.plan_first import PlanFirstMiddleware
from ..middleware.steering import SteeringMiddleware
from ..middleware.tool_execution import ToolExecutionMiddleware
from ..middleware.tool_result_retry import ToolResultRetryMiddleware
from ..middleware.tool_review import ToolReviewMiddleware
from ..middleware.tool_visibility import ToolVisibilityMiddleware, delivered_plugin_tool_name
from ..model_credentials import CredentialStoreError, get_model_api_key
from ..model_profiles import apply_known_model_profile_defaults
from ..plugins.browser_qa import BrowserQAActionExecutor, BrowserQAService
from ..plugins.catalog import PluginExecutionLease
from ..plugins.computer_use import ComputerUseActionExecutor, ComputerUseService
from ..plugins.linux_cgroup import load_linux_cgroup_resources
from ..plugins.macos_vm import load_macos_vm_resources
from ..plugins.ocr import OCRActionExecutor
from ..plugins.platforms import current_managed_worker_platform
from ..plugins.sandbox_runtime import SandboxRuntimeError, configured_srt_launcher
from ..plugins.tools import PluginActionError, PluginToolAdapter, build_plugin_tool
from ..store.sqlite import LocalStore
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
from .context_builder import AsyncToolExecutionGate, RuntimeContext, build_default_context
from .subagents import build_subagents

log = logging.getLogger("shejane_runtime.agent.builder")

_AGENT_DEFINITION_CACHE_MAX = 16
_AGENT_STATE_SCHEMA_VERSION = 2
_MAX_SUBAGENT_TASKS_PER_RUN = 5
_PARENT_MODEL_CALL_RESERVE = 5
_APPROVAL_REVIEW_MAX_CALLS = 20
_CLARIFICATION_REVIEW_MAX_CALLS = 4
_COMPLETION_REVIEW_MAX_CALLS = 4
_TITLE_GENERATION_MAX_CALLS = 1
_VISION_MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024
_VISION_MAX_IMAGE_PIXELS = 40_000_000
_VISION_MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

_DEEPAGENTS_TOOL_NAMES = {
    "write_todos",
    "task",
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


def _build_agent_backend(
    *,
    effective_workspace: str,
    skills_dirs: list[Path],
    memory_sources: list[str] | None,
    attachment_bindings: list[dict[str, str]] | None = None,
):
    workspace_root = Path(effective_workspace).expanduser().resolve()
    default = RuntimeLocalShellBackend(
        root_dir=workspace_root,
        virtual_mode=True,
        sandbox_launcher=configured_srt_launcher(),
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
                run_limit=_MAX_SUBAGENT_TASKS_PER_RUN,
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


class RuntimePromptMiddleware(AgentMiddleware):
    """Append model-visible instructions from the invocation context."""

    @staticmethod
    def _request_with_context(request: Any) -> Any:
        context = getattr(getattr(request, "runtime", None), "context", None)
        if not isinstance(context, RuntimeContext):
            return request
        prompt = build_default_context(context)
        repair_instruction = completion_repair_instruction(
            getattr(request, "state", {}),
            run_id=context.run_id,
        )
        if repair_instruction:
            prompt = f"{prompt}\n\n<runtime-repair>\n{repair_instruction}\n</runtime-repair>"
        artifact_instruction = _plugin_artifact_delivery_instruction(
            getattr(request, "messages", ())
        )
        if artifact_instruction:
            prompt = (
                f"{prompt}\n\n<runtime-artifact-delivery>\n"
                f"{artifact_instruction}\n</runtime-artifact-delivery>"
            )
        system_message = request.system_message
        return request.override(
            system_message=SystemMessage(
                content=[
                    {"type": "text", "text": prompt},
                    *system_message.content_blocks,
                ]
            )
        )

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return handler(self._request_with_context(request))

    async def awrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        return await handler(self._request_with_context(request))


def _plugin_artifact_delivery_instruction(messages: Sequence[Any]) -> str | None:
    if delivered_plugin_tool_name(messages) is None:
        return None
    return (
        "The latest plugin Action succeeded. Runtime already persisted its artifacts "
        "and made them available to the user; each artifact_id is a delivered output, "
        "not a host filesystem path. If these artifacts satisfy the request, reply "
        "briefly and stop. Do not read the original attachment, search the filesystem, "
        "call execute or task, or repeat the Action merely to locate or return them. "
        "Call another compatible plugin Action only when the user requested an additional "
        "transformation, passing artifact_id as input_id."
    )


class RuntimeModelMiddleware(AgentMiddleware):
    """Select the model connection owned by this invocation."""

    @staticmethod
    def _request_with_model(request: Any) -> Any:
        context = getattr(getattr(request, "runtime", None), "context", None)
        model = getattr(context, "model", None)
        return request.override(model=model) if model is not None else request

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return handler(self._request_with_model(request))

    async def awrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        return await handler(self._request_with_model(request))


def _build_chat_model(
    settings: Settings,
    run_id: str,
    mode: str,
    *,
    model_binding: dict[str, Any] | None = None,
    model_api_key: str | None = None,
) -> Any:
    """Build the selected BYOK model, or the deterministic test model."""
    if settings.fake_llm:
        from ..llm.fake import FakeBackendChatModel

        return FakeBackendChatModel(
            profile={
                "max_input_tokens": settings.unknown_model_max_input_tokens,
                "max_output_tokens": settings.unknown_model_max_output_tokens,
            }
        )
    if model_binding and model_binding.get("adapter_id") in {
        "openai_chat",
        "anthropic_messages",
    }:
        raw_profile = model_binding.get("profile")
        profile = (
            {
                key: raw_profile[key]
                for key in (
                    "tool_calling",
                    "image_inputs",
                    "max_input_tokens",
                    "max_output_tokens",
                )
                if key in raw_profile and raw_profile[key] is not None
            }
            if isinstance(raw_profile, dict)
            else {}
        )
        profile.setdefault("max_input_tokens", settings.unknown_model_max_input_tokens)
        profile.setdefault("max_output_tokens", settings.unknown_model_max_output_tokens)
        profile.setdefault("image_inputs", False)
        profile["image_tool_message"] = profile["image_inputs"]
        if model_binding["adapter_id"] == "anthropic_messages":
            from langchain_anthropic import ChatAnthropic

            return ChatAnthropic(
                model=str(model_binding["model_id"]),
                base_url=str(model_binding["base_url"]),
                api_key=model_api_key or "local",
                streaming=True,
                stream_usage=True,
                max_retries=0,
                max_tokens=int(profile["max_output_tokens"]),
                timeout=settings.model_request_timeout_seconds,
                profile=profile,
            )

        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=str(model_binding["model_id"]),
            base_url=str(model_binding["base_url"]),
            api_key=model_api_key or "local",
            http_client=httpx.Client(),
            http_async_client=httpx.AsyncClient(),
            streaming=True,
            stream_usage=True,
            max_retries=0,
            max_tokens=int(profile["max_output_tokens"]),
            timeout=settings.model_request_timeout_seconds,
            profile=profile,
        )
    raise RuntimeError("Runtime BYOK model binding is required")


async def _invoke_plugin_vision(
    model_binding: Mapping[str, Any],
    params: dict[str, Any],
    input_root: Path,
    inputs: tuple[dict[str, Any], ...],
    *,
    store: LocalStore,
    principal_id: str,
    settings: Settings,
) -> dict[str, Any]:
    connection_id = str(model_binding["connection_id"])
    connection = await store.get_model_connection(
        principal_id=principal_id,
        connection_id=connection_id,
    )
    try:
        models = json.loads(connection.get("models_json") or "[]") if connection else []
    except (json.JSONDecodeError, TypeError):
        models = []
    current_profile = next(
        (
            item
            for item in models
            if isinstance(item, dict) and item.get("model_id") == model_binding["model_id"]
        ),
        None,
    )
    if isinstance(current_profile, dict):
        current_profile = apply_known_model_profile_defaults(
            current_profile,
            service_base_url=str(connection.get("base_url") or "") if connection else "",
        )
    if (
        connection is None
        or int(connection.get("version") or 1) != int(model_binding["connection_version"])
        or str(connection.get("adapter_id")) != str(model_binding["adapter_id"])
        or str(connection.get("base_url")) != str(model_binding["base_url"])
        or str(connection.get("credential_ref")) != str(model_binding["credential_ref"])
        or current_profile != dict(model_binding["profile"])
        or not bool(current_profile.get("image_inputs"))
    ):
        raise PluginActionError(
            "model_binding_unavailable",
            "configured Vision model binding changed or is unavailable",
        )
    try:
        api_key = await get_model_api_key(
            principal_id,
            connection_id,
            str(model_binding["credential_ref"]),
        )
    except CredentialStoreError as exc:
        raise PluginActionError(
            "model_credential_store_unavailable",
            "Vision model credential store is unavailable",
        ) from exc
    if not api_key:
        raise PluginActionError(
            "model_binding_unavailable",
            "configured Vision model credential is unavailable",
        )

    blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": sanitize_outbound_text(
                params["prompt"],
                secrets=(api_key,) if api_key else (),
                pii_types=_outbound_pii_types(settings.pii_redact_types),
                external=_outbound_is_external(settings, dict(model_binding)),
            ),
        }
    ]
    references = {str(item["id"]): item for item in inputs}
    total_bytes = 0
    input_root = input_root.resolve(strict=True)
    for input_id in params["input_ids"]:
        reference = references[input_id]
        media_type = str(reference["media_type"])
        if media_type not in _VISION_MEDIA_TYPES:
            raise PluginActionError("invalid_invocation", "Vision input media type is unsupported")
        try:
            relative = PurePosixPath(str(reference["path"])).relative_to("/input")
        except ValueError as exc:
            raise PluginActionError("invalid_invocation", "Vision input path is invalid") from exc
        candidate = input_root.joinpath(*relative.parts)
        try:
            candidate.resolve(strict=True).relative_to(input_root)
        except (FileNotFoundError, ValueError) as exc:
            raise PluginActionError("invalid_invocation", "Vision input is unavailable") from exc
        body = candidate.read_bytes()
        total_bytes += len(body)
        if (
            len(body) != int(reference["size_bytes"])
            or hashlib.sha256(body).hexdigest() != reference["sha256"]
            or total_bytes > _VISION_MAX_TOTAL_IMAGE_BYTES
        ):
            raise PluginActionError("resource_exhausted", "Vision input byte limit exceeded")
        try:
            with Image.open(candidate) as image:
                if (
                    image.width <= 0
                    or image.height <= 0
                    or image.width * image.height > _VISION_MAX_IMAGE_PIXELS
                    or int(getattr(image, "n_frames", 1)) != 1
                ):
                    raise PluginActionError(
                        "resource_exhausted",
                        "Vision image dimensions or frame count are unsupported",
                    )
                image.verify()
        except (Image.DecompressionBombError, UnidentifiedImageError, OSError) as exc:
            raise PluginActionError("invalid_invocation", "Vision input image is invalid") from exc
        encoded = base64.b64encode(body).decode("ascii")
        if model_binding["adapter_id"] == "anthropic_messages":
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": encoded,
                    },
                }
            )
        else:
            image_url: dict[str, Any] = {"url": f"data:{media_type};base64,{encoded}"}
            if params.get("detail") is not None:
                image_url["detail"] = params["detail"]
            blocks.append({"type": "image_url", "image_url": image_url})

    model = _build_chat_model(
        settings,
        "plugin-vision",
        "vision",
        model_binding=dict(model_binding),
        model_api_key=api_key,
    ).bind(
        max_tokens=int(params["max_output_tokens"]),
        temperature=float(params.get("temperature", 0)),
    )
    try:
        response = await model.ainvoke([HumanMessage(content=blocks)])
    except Exception as exc:
        log.warning(
            "plugin vision model request failed connection=%s model=%s error=%s",
            connection_id,
            model_binding["model_id"],
            type(exc).__name__,
        )
        raise PluginActionError(
            "vision_model_service_failed",
            "configured Vision model service request failed",
        ) from exc
    content = response.content
    text = (
        content
        if isinstance(content, str)
        else "".join(
            str(item["text"])
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    )
    if not text or len(text) > 262_144:
        raise PluginActionError(
            "vision_model_service_failed",
            "Vision model service returned invalid text",
        )
    raw_usage = getattr(response, "usage_metadata", None)
    usage = {
        key: int(value)
        for key, value in (raw_usage.items() if isinstance(raw_usage, dict) else ())
        if key in {"input_tokens", "output_tokens", "total_tokens"}
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    }
    return {
        "text": text,
        "model": {
            "connection_id": connection_id,
            "connection_version": int(model_binding["connection_version"]),
            "model_id": str(model_binding["model_id"]),
        },
        "usage": usage,
    }


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


def _outbound_is_external(
    settings: Settings,
    model_binding: dict[str, Any] | None,
) -> bool:
    if settings.fake_llm or (model_binding or {}).get("provider") == "fake":
        return False
    if model_binding is None:
        return True
    raw_url = str((model_binding or {}).get("base_url") or "")
    hostname = (urlparse(raw_url).hostname or "").strip().lower()
    if hostname == "localhost":
        return False
    try:
        return not ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return True


def _outbound_pii_types(spec: str) -> tuple[str, ...]:
    valid = {"email", "credit_card", "ip", "mac_address", "url"}
    return tuple(
        dict.fromkeys(
            item for item in (part.strip().lower() for part in spec.split(",")) if item in valid
        )
    )


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
        skills_enabled:  When False, passes `skills=None` to deepagents so
                         no skill instructions get injected into the prompt
                         and the agent doesn't see them. Mirrors the
                         memory toggle pattern.
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

    tools = await build_tools()
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
                    workspace_root=workspace_root or settings.data_dir,
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
    model = (
        LedgerChatModel(
            delegate=provider_model,
            store=store,
            run_id=run_id,
            execution_attempt_id=execution_attempt_id,
            model_name=mode,
            max_calls=settings.max_model_calls,
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
    definition_model = RuntimeModelProxy(profile=getattr(model, "profile", None))
    subagent_model = RuntimeModelProxy(
        profile=getattr(model, "profile", None),
        max_model_calls=max(1, settings.max_model_calls - _PARENT_MODEL_CALL_RESERVE),
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
    )

    middleware = _custom_middleware(
        settings,
        deferred_tool_names=deferred_tool_names,
    )
    middleware.insert(3, SteeringMiddleware())

    if extra_middleware:
        middleware.extend(extra_middleware)

    subagents_arg = (
        build_subagents(
            main_tools=tools,
            main_model=subagent_model,
            deferred_tool_names=deferred_tool_names,
        )
        if settings.enable_subagents
        else None
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
            approval_model=approval_model,
            clarification_model=clarification_model,
            completion_model=completion_model,
            title_model=title_model,
            dynamic_tools=dynamic_tool_map,
            execution_attempt_id=execution_attempt_id,
            subagents_enabled=settings.enable_subagents,
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
        runtime_context.subagents_enabled = settings.enable_subagents
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
    )
    runtime_context.graph_definition_id = fingerprint

    def compile_definition() -> Any:
        return create_deep_agent(
            model=definition_model,
            tools=tools,
            middleware=middleware,
            subagents=subagents_arg,
            skills=skills_arg,
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


def _register_model_cleanup(model: Any, stack: AsyncExitStack | None) -> None:
    """Close provider clients when the owning execution attempt ends."""
    if stack is None:
        return
    seen: set[int] = set()
    for name in ("root_async_client", "_async_client"):
        client = getattr(model, name, None)
        close = getattr(client, "close", None)
        if callable(close) and id(client) not in seen:
            seen.add(id(client))
            stack.push_async_callback(close)
    for name in ("root_client", "_client"):
        client = getattr(model, name, None)
        close = getattr(client, "close", None)
        if callable(close) and id(client) not in seen:
            seen.add(id(client))
            stack.callback(close)


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
