"""Runtime-owned MCP configuration, catalog validation, and tool adapters."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from contextvars import copy_context
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import httpx
from langchain_core.tools import BaseTool
from langchain_core.tools import tool as langchain_tool
from langgraph.config import get_stream_writer

from ..store.sqlite import LocalStore
from .mcp_config import (  # noqa: F401 - compatibility exports
    MAX_MCP_DESCRIPTION_CHARS,
    MAX_MCP_SCHEMA_BYTES,
    MAX_MCP_SCHEMA_DEPTH,
    MAX_MCP_SCHEMA_NODES,
    MAX_MCP_SERVERS,
    MAX_MCP_TOOLS,
    MAX_MCP_TOTAL_SCHEMA_BYTES,
    SOURCE_DATA_DIR,
    SOURCE_ENV,
    SOURCE_SHEJANE,
    ValidatedMCPTool,
    _candidate_source_files,
    _load_mcp_config,
    _normalize_entry,
    _sensitive_values_from_config,
    _validate_schema_tree,
    discover_servers,
    mcp_sensitive_values,
    validate_mcp_tools,
)
from .mcp_stdio import bounded_stdio_client
from .runtime import current_runtime_tool_execution

log = logging.getLogger("shejane_runtime.tools.mcp")


def _bounded_timeout_from_env(name: str, *, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return min(max(value, 0.01), 300.0)


MAX_MCP_HTTP_BYTES = 4 * 1_024 * 1_024
MAX_MCP_STDIO_FRAME_BYTES = 4 * 1_024 * 1_024
MCP_DISCOVERY_TIMEOUT_SECONDS = 15
MCP_TOOL_TIMEOUT_SECONDS = _bounded_timeout_from_env(
    "SHEJANE_MCP_TOOL_TIMEOUT_SECONDS",
    default=60.0,
)
MCP_RETRY_BACKOFF_SECONDS = 30
MCP_TOOL_SEARCH_NAME = "mcp.search_tools"
MCP_TOOL_SEARCH_RESULT_KIND = "mcp_tool_search_results"
MCP_TOOL_SEARCH_THRESHOLD = 12
MCP_TOOL_SEARCH_DESCRIPTION_CHARS = 512
MCP_TOOL_SEARCH_QUERY_CHARS = 512


def make_mcp_tool_search(tools: Sequence[BaseTool]) -> BaseTool:
    """Expose a compact, provider-independent MCP tool directory."""
    directory = tuple(
        {
            "name": item.name,
            "description": (item.description or "").strip()[:MCP_TOOL_SEARCH_DESCRIPTION_CHARS],
        }
        for item in tools
    )

    @langchain_tool(MCP_TOOL_SEARCH_NAME)
    def search_tools(query: str, limit: int = 5) -> dict[str, Any]:
        """Search available MCP integrations by capability before using one."""
        bounded_query = query.strip()[:MCP_TOOL_SEARCH_QUERY_CHARS]
        normalized_query = bounded_query.lower()
        bounded_limit = max(1, min(int(limit), 8))
        ranked = sorted(
            directory,
            key=lambda item: (
                _mcp_tool_search_score(normalized_query, item),
                item["name"],
            ),
            reverse=True,
        )
        return {
            "kind": MCP_TOOL_SEARCH_RESULT_KIND,
            "query": bounded_query,
            "tools": list(ranked[:bounded_limit]),
        }

    return search_tools


def _mcp_tool_search_score(query: str, item: dict[str, str]) -> float:
    if not query:
        return 0
    name = item["name"].lower()
    description = item["description"].lower()
    corpus = f"{name} {description}"
    query_tokens = set(re.findall(r"[\w.-]+", query))
    corpus_tokens = set(re.findall(r"[\w.-]+", corpus))
    score = 8.0 if query in name else 3.0 if query in description else 0.0
    score += 2.0 * len(query_tokens & corpus_tokens)
    score += SequenceMatcher(None, query, name).ratio()
    return score


@dataclass
class _CatalogEntry:
    config_fingerprint: str
    tools: tuple[BaseTool, ...]
    supervisor: _MCPServerSupervisor | None = None
    leases: int = 0
    retired: bool = False
    error_type: str | None = None
    retry_at: float = 0
    refresh_required: bool = False


class _LiveSessionProxy:
    def __init__(self, supervisor: _MCPServerSupervisor) -> None:
        self._supervisor = supervisor

    async def call_tool(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        return await self._supervisor.call_tool(name, arguments, **kwargs)


class _MCPServerSupervisor:
    """Own one MCP session in the same task for its full lifetime."""

    def __init__(self, server_name: str, connection: dict[str, Any]) -> None:
        self.server_name = server_name
        self.connection = dict(connection)
        self._ready: asyncio.Future[tuple[BaseTool, ...]] | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._session: Any | None = None
        self._on_tools_changed: Callable[[_MCPServerSupervisor], None] | None = None

    def set_tools_changed_callback(
        self,
        callback: Callable[[_MCPServerSupervisor], None],
    ) -> None:
        self._on_tools_changed = callback

    async def start(self) -> tuple[BaseTool, ...]:
        if self._task is None:
            self._ready = asyncio.get_running_loop().create_future()
            self._task = asyncio.create_task(
                self._serve(),
                name=f"mcp-server:{self.server_name}",
            )
        assert self._ready is not None
        return await self._ready

    async def call_tool(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        session = self._session
        if session is None:
            raise RuntimeError(f"MCP server {self.server_name!r} is not connected")
        previous_progress_callback = kwargs.get("progress_callback")
        try:
            execution = current_runtime_tool_execution()
            stream_writer = get_stream_writer()
            stream_context = copy_context()
        except RuntimeError:
            execution = None
            stream_writer = None
            stream_context = None
        if execution is not None and stream_writer is not None and stream_context is not None:

            async def report_progress(
                progress: float,
                total: float | None,
                message: str | None,
            ) -> None:
                stream_context.run(
                    stream_writer,
                    {
                        "event": "tool.progress",
                        "data": {
                            "tool_call_id": execution.tool_call_id,
                            "tool": f"{self.server_name}_{name}",
                            "progress": progress,
                            "total": total,
                            "message": message,
                        },
                    },
                )
                if previous_progress_callback is not None:
                    await previous_progress_callback(progress, total, message)

            kwargs["progress_callback"] = report_progress
        try:
            async with asyncio.timeout(MCP_TOOL_TIMEOUT_SECONDS):
                return await session.call_tool(name, arguments, **kwargs)
        except asyncio.CancelledError:
            self._retire_session()
            raise
        except Exception:
            self._retire_session()
            raise

    def _retire_session(self) -> None:
        self._session = None
        self._stop.set()
        if self._on_tools_changed is not None:
            self._on_tools_changed(self)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task

    async def _serve(self) -> None:
        context: Any | None = None
        entered = False
        try:
            import langchain_mcp_adapters.sessions as mcp_sessions

            _install_bounded_stdio_transport(mcp_sessions)
            connection = _bounded_mcp_connection(self.connection)
            session_kwargs = dict(connection.get("session_kwargs") or {})
            previous_handler = session_kwargs.get("message_handler")

            async def message_handler(message: Any) -> None:
                from mcp.types import ServerNotification, ToolListChangedNotification

                if (
                    isinstance(message, ServerNotification)
                    and isinstance(message.root, ToolListChangedNotification)
                    and self._on_tools_changed is not None
                ):
                    self._on_tools_changed(self)
                if previous_handler is not None:
                    await previous_handler(message)

            session_kwargs["message_handler"] = message_handler
            connection["session_kwargs"] = session_kwargs
            context = mcp_sessions.create_session(connection)
            async with asyncio.timeout(MCP_DISCOVERY_TIMEOUT_SECONDS):
                session = await context.__aenter__()
                entered = True
                self._session = session
                await session.initialize()
                tools = await _discover_live_mcp_tools(
                    session,
                    server_name=self.server_name,
                    execution_session=_LiveSessionProxy(self),
                )
            assert self._ready is not None
            self._ready.set_result(tuple(tools))
            await self._stop.wait()
        except Exception as exc:
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(exc)
            else:
                log.warning(
                    "MCP server %r session failed: %s",
                    self.server_name,
                    type(exc).__name__,
                )
                if self._on_tools_changed is not None:
                    self._on_tools_changed(self)
        finally:
            self._session = None
            if context is not None and entered:
                try:
                    async with asyncio.timeout(MCP_DISCOVERY_TIMEOUT_SECONDS):
                        await context.__aexit__(None, None, None)
                except Exception as exc:
                    log.warning(
                        "MCP server %r cleanup failed: %s",
                        self.server_name,
                        type(exc).__name__,
                    )


async def _open_mcp_server(
    server_name: str,
    connection: dict[str, Any],
) -> tuple[_MCPServerSupervisor | None, tuple[BaseTool, ...], str | None]:
    supervisor = _MCPServerSupervisor(server_name, connection)
    try:
        version = f"mcp-v1:{_mcp_config_fingerprint(connection)}"
        tools = tuple(_with_tool_version(tool, version) for tool in await supervisor.start())
        return supervisor, tools, None
    except Exception as exc:
        log.warning(
            "MCP server %r discovery failed: %s",
            server_name,
            type(exc).__name__,
        )
        await supervisor.stop()
        return None, (), type(exc).__name__


class MCPToolCatalog:
    """Runtime-owned MCP tool definitions, refreshed per changed server."""

    def __init__(self, data_dir: Path | None, *, store: LocalStore | None = None) -> None:
        self._data_dir = data_dir
        self._store = store
        self._entries: dict[str, _CatalogEntry] = {}
        self._retired: list[_CatalogEntry] = []
        self._lock = asyncio.Lock()
        self._closed = False
        self._refresh_task: asyncio.Task[None] | None = None
        self._refresh_pending = False

    async def get_tools(
        self,
        *,
        disabled_servers: set[str] | None = None,
        reserved_names: set[str] | None = None,
    ) -> list[ValidatedMCPTool]:
        tools, _entries = await self._snapshot(
            disabled_servers=disabled_servers,
            reserved_names=reserved_names,
            lease=False,
        )
        return tools

    @asynccontextmanager
    async def acquire_tools(
        self,
        *,
        disabled_servers: set[str] | None = None,
        reserved_names: set[str] | None = None,
    ) -> AsyncIterator[list[ValidatedMCPTool]]:
        tools, entries = self._cached_snapshot(
            disabled_servers=disabled_servers,
            reserved_names=reserved_names,
        )
        try:
            yield tools
        finally:
            await self._release(entries)

    def request_refresh(self, *, disabled_servers: set[str] | None = None) -> None:
        if self._closed:
            return
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_pending = True
            return

        async def refresh() -> None:
            try:
                await self.get_tools(disabled_servers=disabled_servers)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("MCP background refresh failed: %s", type(exc).__name__)

        task = asyncio.create_task(refresh(), name="mcp-catalog-refresh")
        self._refresh_task = task

        def refresh_done(done: asyncio.Task[None]) -> None:
            if self._refresh_task is done:
                self._refresh_task = None
            if self._refresh_pending and not self._closed:
                self._refresh_pending = False
                self.request_refresh(disabled_servers=disabled_servers)

        task.add_done_callback(refresh_done)

    def _server_tools_changed(self, supervisor: _MCPServerSupervisor) -> None:
        entry = self._entries.get(supervisor.server_name)
        if entry is None or entry.supervisor is not supervisor:
            return
        entry.refresh_required = True
        self.request_refresh()

    def _cached_snapshot(
        self,
        *,
        disabled_servers: set[str] | None,
        reserved_names: set[str] | None,
    ) -> tuple[list[ValidatedMCPTool], tuple[_CatalogEntry, ...]]:
        if self._closed:
            raise RuntimeError("MCP tool catalog is closed")
        config = _load_mcp_config(self._data_dir)
        if disabled_servers:
            config = {name: value for name, value in config.items() if name not in disabled_servers}
        config = dict(list(config.items())[:MAX_MCP_SERVERS])
        entries: list[_CatalogEntry] = []
        needs_refresh = False
        for name, connection in config.items():
            entry = self._entries.get(name)
            fingerprint = _mcp_config_fingerprint(connection)
            if entry is None or entry.config_fingerprint != fingerprint:
                needs_refresh = True
                continue
            if entry.error_type is not None:
                needs_refresh = needs_refresh or time.monotonic() >= entry.retry_at
                continue
            if entry.supervisor is None:
                needs_refresh = needs_refresh or time.monotonic() >= entry.retry_at
            entry.leases += 1
            entries.append(entry)
        if needs_refresh:
            self.request_refresh(disabled_servers=disabled_servers)
        tools = [tool for entry in entries for tool in entry.tools]
        return (
            validate_mcp_tools(
                tools,
                sensitive_values=_sensitive_values_from_config(config),
                reserved_names=reserved_names,
            ),
            tuple(entries),
        )

    async def hydrate(self) -> None:
        if self._store is None:
            return
        records = {item["server_name"]: item for item in await self._store.list_mcp_catalogs()}
        config = dict(list(_load_mcp_config(self._data_dir).items())[:MAX_MCP_SERVERS])
        async with self._lock:
            if self._closed:
                raise RuntimeError("MCP tool catalog is closed")
            for name, connection in config.items():
                record = records.get(name)
                fingerprint = _mcp_config_fingerprint(connection)
                if (
                    record is None
                    or record["status"] != "ready"
                    or record["config_fingerprint"] != fingerprint
                ):
                    continue
                tools = _tools_from_persisted_descriptors(
                    name,
                    connection,
                    record["tools"],
                )
                version = f"mcp-v1:{fingerprint}"
                tools = [_with_tool_version(tool, version) for tool in tools]
                accepted = validate_mcp_tools(
                    tools,
                    sensitive_values=_sensitive_values_from_config({name: connection}),
                )
                self._entries[name] = _CatalogEntry(
                    config_fingerprint=fingerprint,
                    tools=tuple(item.tool for item in accepted),
                    retry_at=0,
                )

    def server_statuses(self) -> dict[str, dict[str, Any]]:
        return {
            name: {
                "status": (
                    "ready"
                    if entry.supervisor is not None
                    else "error"
                    if entry.error_type is not None
                    else "idle"
                ),
                "tool_count": len(entry.tools),
                "error_type": entry.error_type,
            }
            for name, entry in self._entries.items()
        }

    async def _snapshot(
        self,
        *,
        disabled_servers: set[str] | None,
        reserved_names: set[str] | None,
        lease: bool,
    ) -> tuple[list[ValidatedMCPTool], tuple[_CatalogEntry, ...]]:
        to_close: list[_MCPServerSupervisor] = []
        async with self._lock:
            if self._closed:
                raise RuntimeError("MCP tool catalog is closed")
            full_config = _load_mcp_config(self._data_dir)
            config = full_config
            if disabled_servers:
                config = {
                    name: value for name, value in config.items() if name not in disabled_servers
                }
            config = dict(list(config.items())[:MAX_MCP_SERVERS])
            for inactive_name in self._entries.keys() - config.keys():
                self._retire(self._entries.pop(inactive_name), to_close)
            fingerprints = {
                name: _mcp_config_fingerprint(connection) for name, connection in config.items()
            }
            stale = [
                (name, connection)
                for name, connection in config.items()
                if (entry := self._entries.get(name)) is None
                or entry.config_fingerprint != fingerprints[name]
                or entry.refresh_required
                or (entry.supervisor is None and time.monotonic() >= entry.retry_at)
            ]
            if stale:
                loaded = await asyncio.gather(
                    *(_open_mcp_server(name, connection) for name, connection in stale)
                )
                for (name, _connection), (supervisor, tools, error_type) in zip(
                    stale, loaded, strict=True
                ):
                    previous = self._entries.get(name)
                    if previous is not None:
                        self._retire(previous, to_close)
                    if supervisor is not None:
                        set_callback = getattr(supervisor, "set_tools_changed_callback", None)
                        if set_callback is not None:
                            set_callback(self._server_tools_changed)
                    self._entries[name] = _CatalogEntry(
                        config_fingerprint=fingerprints[name],
                        tools=tuple(tools),
                        supervisor=supervisor,
                        error_type=error_type,
                        retry_at=(
                            time.monotonic() + MCP_RETRY_BACKOFF_SECONDS
                            if supervisor is None
                            else 0
                        ),
                    )
                    await self._persist_entry(
                        name=name,
                        connection=config[name],
                        entry=self._entries[name],
                    )
            entries = tuple(self._entries[name] for name in config)
            if lease:
                for entry in entries:
                    entry.leases += 1
            tools = [tool for entry in entries for tool in entry.tools]

        await _stop_mcp_servers(to_close)

        return (
            validate_mcp_tools(
                tools,
                sensitive_values=_sensitive_values_from_config(config),
                reserved_names=reserved_names,
            ),
            entries,
        )

    async def _persist_entry(
        self,
        *,
        name: str,
        connection: dict[str, Any],
        entry: _CatalogEntry,
    ) -> None:
        if self._store is None:
            return
        try:
            accepted = validate_mcp_tools(
                list(entry.tools),
                sensitive_values=_sensitive_values_from_config({name: connection}),
            )
            prefix = f"{name}_"
            tools = [
                {
                    "name": item.name,
                    "raw_name": (
                        item.name[len(prefix) :] if item.name.startswith(prefix) else item.name
                    ),
                    "description": item.description,
                    "args_schema": item.args_schema,
                }
                for item in accepted
            ]
            if entry.supervisor is None:
                previous = await self._store.get_mcp_catalog(name)
                if previous is not None:
                    tools = previous["tools"]
            await self._store.upsert_mcp_catalog(
                server_name=name,
                config_fingerprint=entry.config_fingerprint,
                tools=tools,
                status="ready" if entry.supervisor is not None else "error",
                error_type=entry.error_type,
            )
        except Exception as exc:
            log.warning("MCP catalog persistence failed for %r: %s", name, type(exc).__name__)

    async def invalidate(self, server_name: str | None = None) -> None:
        to_close: list[_MCPServerSupervisor] = []
        async with self._lock:
            if server_name is None:
                entries = tuple(self._entries.values())
                self._entries.clear()
                for entry in entries:
                    self._retire(entry, to_close)
            else:
                entry = self._entries.pop(server_name, None)
                if entry is not None:
                    self._retire(entry, to_close)
        await _stop_mcp_servers(to_close)

    async def close(self) -> None:
        refresh_task = self._refresh_task
        if refresh_task is not None and not refresh_task.done():
            await asyncio.gather(refresh_task, return_exceptions=True)
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            entries = [*self._entries.values(), *self._retired]
            self._entries.clear()
            self._retired.clear()
        await _stop_mcp_servers(
            [entry.supervisor for entry in entries if entry.supervisor is not None]
        )

    def _retire(
        self,
        entry: _CatalogEntry,
        to_close: list[_MCPServerSupervisor],
    ) -> None:
        entry.retired = True
        if entry.leases:
            self._retired.append(entry)
        elif entry.supervisor is not None:
            to_close.append(entry.supervisor)

    async def _release(self, entries: tuple[_CatalogEntry, ...]) -> None:
        to_close: list[_MCPServerSupervisor] = []
        async with self._lock:
            for entry in entries:
                entry.leases -= 1
                if entry.leases < 0:
                    raise RuntimeError("MCP catalog lease underflow")
                if entry.retired and entry.leases == 0:
                    if entry in self._retired:
                        self._retired.remove(entry)
                    if entry.supervisor is not None:
                        to_close.append(entry.supervisor)
        await _stop_mcp_servers(to_close)


async def _stop_mcp_servers(supervisors: list[_MCPServerSupervisor]) -> None:
    if supervisors:
        await asyncio.gather(*(supervisor.stop() for supervisor in supervisors))


def _mcp_config_fingerprint(config: dict[str, Any]) -> str:
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _with_tool_version(tool: BaseTool, version: str) -> BaseTool:
    return tool.model_copy(
        update={"metadata": {**(tool.metadata or {}), "shejane_tool_version": version}}
    )


class _LimitedResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, budget: list[int]) -> None:
        self._stream = stream
        self._budget = budget

    async def __aiter__(self):
        async for chunk in self._stream:
            self._budget[0] -= len(chunk)
            if self._budget[0] < 0:
                raise httpx.HTTPError("MCP response byte limit exceeded")
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


class _LimitedHTTPTransport(httpx.AsyncBaseTransport):
    def __init__(self, max_bytes: int) -> None:
        self._transport = httpx.AsyncHTTPTransport()
        self._budget = [max_bytes]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._transport.handle_async_request(request)
        content_length = response.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > self._budget[0]:
            await response.aclose()
            raise httpx.HTTPError("MCP response byte limit exceeded")
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_LimitedResponseStream(response.stream, self._budget),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._transport.aclose()


def _bounded_http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=_LimitedHTTPTransport(MAX_MCP_HTTP_BYTES),
        follow_redirects=True,
        headers=headers,
        timeout=timeout or httpx.Timeout(30, read=300),
        auth=auth,
    )


def _install_bounded_stdio_transport(mcp_sessions: Any) -> None:
    def bounded_adapter_stdio_client(server: Any, errlog: Any = sys.stderr):
        return bounded_stdio_client(
            server,
            errlog,
            max_frame_bytes=MAX_MCP_STDIO_FRAME_BYTES,
        )

    mcp_sessions.stdio_client = bounded_adapter_stdio_client


def _bounded_mcp_connection(raw_connection: dict[str, Any]) -> dict[str, Any]:
    connection = dict(raw_connection)
    transport = connection.get("transport")
    if transport == "websocket":
        raise ValueError("websocket MCP transport is not bounded")
    if transport in {"sse", "http", "streamable-http", "streamable_http"}:
        connection["httpx_client_factory"] = _bounded_http_client
    return connection


async def _discover_live_mcp_tools(
    session: Any,
    *,
    server_name: str,
    execution_session: Any,
) -> list[BaseTool]:
    from langchain_mcp_adapters.tools import convert_mcp_tool_to_langchain_tool

    tools: list[BaseTool] = []
    candidates_seen = 0
    raw_schema_bytes = 0
    cursor: str | None = None
    while candidates_seen < MAX_MCP_TOOLS:
        page = await session.list_tools(cursor=cursor)
        for raw_tool in page.tools:
            candidates_seen += 1
            if candidates_seen > MAX_MCP_TOOLS:
                break
            try:
                schema = raw_tool.inputSchema
                _validate_schema_tree(schema)
                schema_size = len(
                    json.dumps(
                        schema,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode()
                )
            except Exception:
                continue
            if schema_size > MAX_MCP_SCHEMA_BYTES:
                continue
            if raw_schema_bytes + schema_size > MAX_MCP_TOTAL_SCHEMA_BYTES:
                return tools
            tools.append(
                convert_mcp_tool_to_langchain_tool(
                    execution_session,
                    raw_tool,
                    server_name=server_name,
                    tool_name_prefix=True,
                )
            )
            raw_schema_bytes += schema_size
        cursor = page.nextCursor
        if not cursor:
            break
    return tools


def _tools_from_persisted_descriptors(
    server_name: str,
    raw_connection: dict[str, Any],
    descriptors: list[Any],
) -> list[BaseTool]:
    from langchain_mcp_adapters.tools import convert_mcp_tool_to_langchain_tool
    from mcp.types import Tool

    connection = _bounded_mcp_connection(raw_connection)
    tools: list[BaseTool] = []
    for descriptor in descriptors[:MAX_MCP_TOOLS]:
        if not isinstance(descriptor, dict):
            continue
        raw_name = descriptor.get("raw_name")
        schema = descriptor.get("args_schema")
        if not isinstance(raw_name, str) or not isinstance(schema, dict):
            continue
        try:
            _validate_schema_tree(schema)
            raw_tool = Tool(
                name=raw_name,
                description=str(descriptor.get("description") or ""),
                inputSchema=schema,
            )
            tool = convert_mcp_tool_to_langchain_tool(
                None,
                raw_tool,
                connection=connection,
                server_name=server_name,
                tool_name_prefix=True,
            )
        except Exception:
            continue
        if tool.name == descriptor.get("name"):
            tools.append(tool)
    return tools


async def build_mcp_tools(
    data_dir: Path | None,
    *,
    disabled_servers: set[str] | None = None,
) -> list[BaseTool]:
    """Connect to every configured MCP server and return their tools.

    Failure to connect to any one server does NOT abort the boot — we
    log the error and continue with the others. This matches the
    runtime's desire to stay up even when an MCP server is misconfigured
    (a very common state given that users routinely point Claude
    Client at half-broken commands during dev).

    `disabled_servers` is a per-user opt-out set — names in here are
    dropped before MultiServerMCPClient sees them, so we never spawn
    the subprocess or open the WebSocket. The user toggles individual
    rows off from the MCP tab; the client sends the disabled-name
    list with every run.
    """
    config = _load_mcp_config(data_dir)
    if disabled_servers:
        config = {name: cfg for name, cfg in config.items() if name not in disabled_servers}
    return await _build_mcp_tools_from_config(config)


async def _build_mcp_tools_from_config(
    config: dict[str, dict[str, Any]],
) -> list[BaseTool]:
    if not config:
        return []

    try:
        import langchain_mcp_adapters.sessions as mcp_sessions
        from langchain_mcp_adapters.tools import convert_mcp_tool_to_langchain_tool
    except ImportError:
        log.warning("langchain-mcp-adapters not installed; skipping MCP")
        return []

    # The adapter resolves this module-level transport both during discovery
    # and when a converted tool later opens its own session.
    def bounded_adapter_stdio_client(server, errlog=sys.stderr):
        return bounded_stdio_client(
            server,
            errlog,
            max_frame_bytes=MAX_MCP_STDIO_FRAME_BYTES,
        )

    mcp_sessions.stdio_client = bounded_adapter_stdio_client
    create_session = mcp_sessions.create_session

    tools: list[BaseTool] = []
    candidates_seen = 0
    raw_schema_bytes = 0
    servers = list(config.items())
    if len(servers) > MAX_MCP_SERVERS:
        log.warning(
            "MCP server limit reached; ignoring %d excess servers", len(servers) - MAX_MCP_SERVERS
        )
    for server_index, (server_name, raw_connection) in enumerate(servers[:MAX_MCP_SERVERS]):
        if candidates_seen >= MAX_MCP_TOOLS:
            break
        connection = dict(raw_connection)
        transport = connection.get("transport")
        if transport == "websocket":
            log.warning(
                "MCP server candidate %d skipped because websocket discovery is not bounded",
                server_index,
            )
            continue
        if transport in {"sse", "http", "streamable-http", "streamable_http"}:
            connection["httpx_client_factory"] = _bounded_http_client
        try:
            async with asyncio.timeout(MCP_DISCOVERY_TIMEOUT_SECONDS):
                async with create_session(connection) as session:
                    await session.initialize()
                    cursor: str | None = None
                    while candidates_seen < MAX_MCP_TOOLS:
                        page = await session.list_tools(cursor=cursor)
                        for raw_tool in page.tools:
                            candidates_seen += 1
                            if candidates_seen > MAX_MCP_TOOLS:
                                break
                            try:
                                schema = raw_tool.inputSchema
                                _validate_schema_tree(schema)
                                schema_size = len(
                                    json.dumps(
                                        schema,
                                        ensure_ascii=False,
                                        sort_keys=True,
                                        separators=(",", ":"),
                                        allow_nan=False,
                                    ).encode()
                                )
                            except Exception:
                                continue
                            if schema_size > MAX_MCP_SCHEMA_BYTES:
                                continue
                            if raw_schema_bytes + schema_size > MAX_MCP_TOTAL_SCHEMA_BYTES:
                                candidates_seen = MAX_MCP_TOOLS
                                break
                            tools.append(
                                convert_mcp_tool_to_langchain_tool(
                                    None,
                                    raw_tool,
                                    connection=connection,
                                    server_name=server_name,
                                    tool_name_prefix=True,
                                )
                            )
                            raw_schema_bytes += schema_size
                        cursor = page.nextCursor
                        if not cursor:
                            break
        except Exception as exc:
            log.warning(
                "MCP server candidate %d discovery failed: %s",
                server_index,
                type(exc).__name__,
            )
    log.info("loaded %d MCP tools across %d servers", len(tools), len(config))
    return tools
