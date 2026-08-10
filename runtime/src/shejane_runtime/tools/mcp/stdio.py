"""Size-bounded MCP stdio transport.

The upstream MCP SDK parses one newline-delimited JSON-RPC message at a time,
but it does not bound the line before JSON decoding.  A local MCP subprocess
is outside the runtime trust boundary, so cap each frame before parsing it.
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from typing import TextIO

import anyio
import anyio.lowlevel
import mcp.types as types
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from anyio.streams.text import TextReceiveStream
from mcp.client.stdio import (
    PROCESS_TERMINATION_TIMEOUT,
    StdioServerParameters,
    _create_platform_compatible_process,
    _get_executable_command,
    _terminate_process_tree,
    get_default_environment,
)
from mcp.shared.message import SessionMessage


class MCPStdioFrameTooLargeError(ValueError):
    """Raised before decoding an oversized MCP stdio JSON-RPC frame."""


def _encoded_size(value: str, server: StdioServerParameters) -> int:
    return len(value.encode(server.encoding, errors=server.encoding_error_handler))


@asynccontextmanager
async def bounded_stdio_client(
    server: StdioServerParameters,
    errlog: TextIO = sys.stderr,
    *,
    max_frame_bytes: int = 4 * 1_024 * 1_024,
):
    """Connect to an MCP subprocess while bounding every inbound frame."""

    read_stream: MemoryObjectReceiveStream[SessionMessage | Exception]
    read_writer: MemoryObjectSendStream[SessionMessage | Exception]
    write_stream: MemoryObjectSendStream[SessionMessage]
    write_reader: MemoryObjectReceiveStream[SessionMessage]
    read_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_reader = anyio.create_memory_object_stream(0)

    try:
        command = _get_executable_command(server.command)
        process = await _create_platform_compatible_process(
            command=command,
            args=server.args,
            env=(
                {**get_default_environment(), **server.env}
                if server.env is not None
                else get_default_environment()
            ),
            errlog=errlog,
            cwd=server.cwd,
        )
    except OSError:
        await read_stream.aclose()
        await write_stream.aclose()
        await read_writer.aclose()
        await write_reader.aclose()
        raise

    async def stdout_reader() -> None:
        assert process.stdout, "Opened process is missing stdout"
        try:
            async with read_writer:
                buffer = ""
                async for chunk in TextReceiveStream(
                    process.stdout,
                    encoding=server.encoding,
                    errors=server.encoding_error_handler,
                ):
                    lines = (buffer + chunk).split("\n")
                    buffer = lines.pop()
                    if _encoded_size(buffer, server) > max_frame_bytes:
                        await read_writer.send(
                            MCPStdioFrameTooLargeError("MCP stdio frame byte limit exceeded")
                        )
                        return
                    for line in lines:
                        if _encoded_size(line, server) > max_frame_bytes:
                            await read_writer.send(
                                MCPStdioFrameTooLargeError("MCP stdio frame byte limit exceeded")
                            )
                            return
                        try:
                            message = types.JSONRPCMessage.model_validate_json(line)
                        except Exception as exc:
                            await read_writer.send(exc)
                            continue
                        await read_writer.send(SessionMessage(message))
        except anyio.ClosedResourceError:  # pragma: no cover
            await anyio.lowlevel.checkpoint()

    async def stdin_writer() -> None:
        assert process.stdin, "Opened process is missing stdin"
        try:
            async with write_reader:
                async for session_message in write_reader:
                    payload = session_message.message.model_dump_json(
                        by_alias=True, exclude_none=True
                    )
                    await process.stdin.send(
                        (payload + "\n").encode(
                            encoding=server.encoding,
                            errors=server.encoding_error_handler,
                        )
                    )
        except anyio.ClosedResourceError:  # pragma: no cover
            await anyio.lowlevel.checkpoint()

    async with anyio.create_task_group() as task_group, process:
        task_group.start_soon(stdout_reader)
        task_group.start_soon(stdin_writer)
        try:
            yield read_stream, write_stream
        finally:
            if process.stdin:
                try:
                    await process.stdin.aclose()
                except Exception:  # pragma: no cover
                    pass
            try:
                with anyio.fail_after(PROCESS_TERMINATION_TIMEOUT):
                    await process.wait()
            except TimeoutError:
                await _terminate_process_tree(process)
            except ProcessLookupError:  # pragma: no cover
                pass
            await read_stream.aclose()
            await write_stream.aclose()
            await read_writer.aclose()
            await write_reader.aclose()
