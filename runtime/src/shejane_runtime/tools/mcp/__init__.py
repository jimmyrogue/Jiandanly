"""Runtime-owned MCP configuration, catalog validation, and tool adapters."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool

from ...store.sqlite import LocalStore
from .config import (  # noqa: F401 - compatibility exports
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
from .loader import (
    build_mcp_tools_from_config as _build_mcp_tools_from_config,
)
from .search import (
    MCP_TOOL_SEARCH_DESCRIPTION_CHARS as MCP_TOOL_SEARCH_DESCRIPTION_CHARS,
)
from .search import (
    MCP_TOOL_SEARCH_NAME as MCP_TOOL_SEARCH_NAME,
)
from .search import (
    MCP_TOOL_SEARCH_QUERY_CHARS as MCP_TOOL_SEARCH_QUERY_CHARS,
)
from .search import (
    MCP_TOOL_SEARCH_RESULT_KIND as MCP_TOOL_SEARCH_RESULT_KIND,
)
from .search import (
    MCP_TOOL_SEARCH_THRESHOLD as MCP_TOOL_SEARCH_THRESHOLD,
)
from .search import (
    make_mcp_tool_search as make_mcp_tool_search,
)
from .session import (
    MCP_DISCOVERY_TIMEOUT_SECONDS as MCP_DISCOVERY_TIMEOUT_SECONDS,
)
from .session import (
    MCPServerSupervisor,
)
from .transport import (
    MAX_MCP_HTTP_BYTES as MAX_MCP_HTTP_BYTES,
)
from .transport import (
    MAX_MCP_STDIO_FRAME_BYTES as MAX_MCP_STDIO_FRAME_BYTES,
)
from .transport import (
    _bounded_http_client as _bounded_http_client,
)
from .transport import (
    _bounded_mcp_connection as _bounded_mcp_connection,
)
from .transport import (
    _discover_live_mcp_tools as _discover_live_mcp_tools,
)
from .transport import (
    _tools_from_persisted_descriptors as _tools_from_persisted_descriptors,
)

log = logging.getLogger("shejane_runtime.tools.mcp")


def _bounded_timeout_from_env(name: str, *, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return min(max(value, 0.01), 300.0)


MCP_TOOL_TIMEOUT_SECONDS = _bounded_timeout_from_env(
    "SHEJANE_MCP_TOOL_TIMEOUT_SECONDS",
    default=60.0,
)
MCP_RETRY_BACKOFF_SECONDS = 30


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


class _MCPServerSupervisor(MCPServerSupervisor):
    def __init__(self, server_name: str, connection: dict[str, Any]) -> None:
        super().__init__(
            server_name,
            connection,
            tool_timeout=lambda: MCP_TOOL_TIMEOUT_SECONDS,
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
