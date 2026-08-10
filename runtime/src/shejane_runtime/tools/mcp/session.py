"""One supervised live MCP session and its progress bridge."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextvars import copy_context
from typing import Any

from langchain_core.tools import BaseTool
from langgraph.config import get_stream_writer

from ..runtime import current_runtime_tool_execution
from .transport import (
    _bounded_mcp_connection,
    _discover_live_mcp_tools,
    _install_bounded_stdio_transport,
)

log = logging.getLogger("shejane_runtime.tools.mcp")
MCP_DISCOVERY_TIMEOUT_SECONDS = 15


class _LiveSessionProxy:
    def __init__(self, supervisor: MCPServerSupervisor) -> None:
        self._supervisor = supervisor

    async def call_tool(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        return await self._supervisor.call_tool(name, arguments, **kwargs)


class MCPServerSupervisor:
    """Own one MCP session in the same task for its full lifetime."""

    def __init__(
        self,
        server_name: str,
        connection: dict[str, Any],
        *,
        tool_timeout: Callable[[], float],
    ) -> None:
        self.server_name = server_name
        self.connection = dict(connection)
        self._tool_timeout = tool_timeout
        self._ready: asyncio.Future[tuple[BaseTool, ...]] | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._session: Any | None = None
        self._on_tools_changed: Callable[[MCPServerSupervisor], None] | None = None

    def set_tools_changed_callback(
        self,
        callback: Callable[[MCPServerSupervisor], None],
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
            async with asyncio.timeout(self._tool_timeout()):
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
