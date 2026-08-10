"""Memory, MCP, and Skill catalog schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ClearMemoryResponse(BaseModel):
    """DELETE /v1/memory — wipes the agent's long-term notes.

    Reported back so the renderer can show "cleared N memories" toast
    instead of a generic "done" message.
    """

    cleared: Literal[True] = True
    deleted_count: int


# ---------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------


class McpServerInfo(BaseModel):
    """One MCP Server configured for this Runtime.

    `name` is the unique key the user (or installer tool) gave it.
    `transport` is the normalized transport — `stdio` / `streamable_http`
    / `sse` / `websocket`. `command` / `args` / `url` / `env_keys` are
    descriptive only — we never echo env *values* (could be secrets).
    `source` is one of `shejane` / `shejane-legacy` / `env` — used by
    the UI to group Runtime-owned configuration by provenance.
    `source_path` is the absolute path of the config file the entry was
    read from, displayed in the settings panel so the user knows where
    to go edit it.
    """

    name: str
    transport: str
    source: str
    source_path: str
    command: str | None = None
    args: list[str] = []
    url: str | None = None
    env_keys: list[str] = []
    cwd: str | None = None
    status: Literal["idle", "ready", "error"] = "idle"
    tool_count: int = 0
    error_type: str | None = None


class McpServerCatalog(BaseModel):
    """GET /v1/mcp-servers — Runtime-owned MCP Servers and sources."""

    servers: list[McpServerInfo]
    sources_scanned: list[str]


class McpServerWriteRequest(BaseModel):
    """Create/update one SheJane-managed MCP server.

    This writes only `~/.shejane/mcp-servers.json`.
    """

    name: str | None = None
    transport: str = "stdio"
    command: str | None = None
    args: list[str] = []
    url: str | None = None
    env: dict[str, str] = {}
    cwd: str | None = None


class McpServerWriteResponse(BaseModel):
    server: McpServerInfo


class McpServerDeleteResponse(BaseModel):
    deleted: bool = True
    name: str


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


class SkillWriteRequest(BaseModel):
    """Create/update one SheJane-managed skill under `~/.shejane/skills`."""

    name: str | None = None
    description: str = ""
    content: str | None = None


class SkillFile(BaseModel):
    name: str
    description: str = ""
    path: str
    root_path: str
    content: str


class SkillWriteResponse(BaseModel):
    skill: SkillFile


class SkillDeleteResponse(BaseModel):
    deleted: bool = True
    name: str
