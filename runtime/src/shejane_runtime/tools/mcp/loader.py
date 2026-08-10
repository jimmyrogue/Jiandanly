"""Bounded one-shot MCP discovery used by the compatibility loader."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from langchain_core.tools import BaseTool

from .config import (
    MAX_MCP_SCHEMA_BYTES,
    MAX_MCP_SERVERS,
    MAX_MCP_TOOLS,
    MAX_MCP_TOTAL_SCHEMA_BYTES,
    _validate_schema_tree,
)
from .session import MCP_DISCOVERY_TIMEOUT_SECONDS
from .transport import _bounded_http_client, _install_bounded_stdio_transport

log = logging.getLogger("shejane_runtime.tools.mcp")


async def build_mcp_tools_from_config(
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

    _install_bounded_stdio_transport(mcp_sessions)
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
