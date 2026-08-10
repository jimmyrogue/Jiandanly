"""Bounded HTTP/stdio transports and descriptor conversion for MCP."""

from __future__ import annotations

import json
import sys
from typing import Any

import httpx
from langchain_core.tools import BaseTool

from .mcp_config import (
    MAX_MCP_SCHEMA_BYTES,
    MAX_MCP_TOOLS,
    MAX_MCP_TOTAL_SCHEMA_BYTES,
    _validate_schema_tree,
)
from .mcp_stdio import bounded_stdio_client

MAX_MCP_HTTP_BYTES = 4 * 1_024 * 1_024
MAX_MCP_STDIO_FRAME_BYTES = 4 * 1_024 * 1_024


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
