from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool

log = logging.getLogger("shejane_runtime.tools.mcp")

SOURCE_SHEJANE = "shejane"
SOURCE_DATA_DIR = "shejane-legacy"
SOURCE_ENV = "env"

MAX_MCP_TOOLS = 64
MAX_MCP_SERVERS = 32
MAX_MCP_DESCRIPTION_CHARS = 4_096
MAX_MCP_SCHEMA_BYTES = 65_536
MAX_MCP_TOTAL_SCHEMA_BYTES = 524_288
MAX_MCP_SCHEMA_DEPTH = 16
MAX_MCP_SCHEMA_NODES = 4_096

_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_CREDENTIAL_PATTERN_RE = re.compile(
    r"(?:\bBearer\s+[^\s]{8,}|\bsk-[A-Za-z0-9_-]{8,})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ValidatedMCPTool:
    """An MCP implementation plus metadata safe to cache and show a model."""

    tool: BaseTool
    name: str
    description: str
    args_schema: dict[str, Any]


@dataclass(frozen=True)
class _SourceFile:
    """A potential on-disk MCP config file we'll try to read."""

    source: str
    path: Path


@dataclass(frozen=True)
class DiscoveredServer:
    """One normalized MCP server entry plus where it came from.

    `config` is the MultiServerMCPClient-compatible dict (no `name`
    inside — name is the map key). `source` is one of the SOURCE_*
    constants above. `source_path` is the absolute path of the config
    file we read this entry from (for UI display).
    """

    name: str
    config: dict[str, Any]
    source: str
    source_path: str


def _candidate_source_files(data_dir: Path | None) -> list[_SourceFile]:
    """Return every config-file candidate in priority order.

    We don't check existence here — `_read_config_file` handles missing
    files gracefully. Returning the full ordered list keeps the logic
    declarative and the source priority easy to read.
    """
    home = Path.home()
    out = [_SourceFile(SOURCE_SHEJANE, home / ".shejane" / "mcp-servers.json")]
    if data_dir is not None:
        out.append(_SourceFile(SOURCE_DATA_DIR, data_dir / "mcp-servers.json"))
    return out


def _read_config_file(src: _SourceFile) -> dict[str, Any]:
    """Read one source file and return its top-level dict.

    Missing files yield `{}` (silent — they're optional). Malformed
    files yield `{}` with a warning. Permission errors yield `{}` with
    a debug log (don't spam users who chmod'd their config).
    """
    try:
        if not src.path.is_file():
            return {}
        return json.loads(src.path.read_text(encoding="utf-8"))
    except (OSError, PermissionError) as exc:
        log.debug("MCP source unreadable %s: %s", src.path, exc)
        return {}
    except json.JSONDecodeError as exc:
        log.warning("MCP source malformed %s: %s", src.path, exc)
        return {}
    return {}


def _extract_servers_map(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Pull the server map out of one config file's parsed contents.

    Runtime accepts either the canonical top-level `mcpServers` key or a
    bare map, so manually managed files stay simple.
    """
    if not isinstance(raw, dict):
        return {}
    servers = raw.get("mcpServers")
    if isinstance(servers, dict):
        return servers
    # Fallback: treat the whole object as the server map, but only if
    # every value looks like a server config (has `command` or `url`).
    # Otherwise unrelated top-level metadata could be misread as servers.
    if all(
        isinstance(v, dict) and ("command" in v or "url" in v)
        for v in raw.values()
        if v is not None
    ):
        return raw  # type: ignore[return-value]
    return {}


def _normalize_entry(name: str, raw: Any) -> dict[str, Any] | None:
    """Normalize one server entry to MultiServerMCPClient format.

    Returns None if the entry is unusable (no transport-determining
    field). Always strips unknown keys so MultiServerMCPClient doesn't
    receive unsupported configuration metadata.
    """
    if not isinstance(raw, dict):
        return None
    # Honor a disabled marker in manually managed Runtime configuration.
    if raw.get("disabled") is True:
        return None

    has_command = isinstance(raw.get("command"), str) and raw["command"].strip()
    has_url = isinstance(raw.get("url"), str) and raw["url"].strip()
    if not has_command and not has_url:
        log.warning("MCP server %r missing both command and url; skipping", name)
        return None

    declared_transport = raw.get("transport")
    if isinstance(declared_transport, str) and declared_transport.strip():
        transport = declared_transport.strip().lower()
        if transport in {"http", "streamable-http"}:
            transport = "streamable_http"
    elif has_url:
        # Sniff the URL: ws:// → websocket, otherwise default to streamable_http
        # which MultiServerMCPClient maps to plain HTTP transport.
        url_lower = raw["url"].lower()
        if url_lower.startswith(("ws://", "wss://")):
            transport = "websocket"
        else:
            transport = "streamable_http"
    else:
        transport = "stdio"

    out: dict[str, Any] = {"transport": transport}
    if has_command:
        out["command"] = raw["command"]
        if isinstance(raw.get("args"), list):
            # Subprocess arguments must be strings.
            out["args"] = [str(a) for a in raw["args"]]
        if isinstance(raw.get("env"), dict):
            # All values must be strings for env. Drop None / non-strings.
            env_clean = {
                str(k): str(v)
                for k, v in raw["env"].items()
                if v is not None and not isinstance(v, dict | list)
            }
            if env_clean:
                out["env"] = env_clean
        if isinstance(raw.get("cwd"), str):
            out["cwd"] = raw["cwd"]
    if has_url:
        out["url"] = raw["url"]
        if isinstance(raw.get("headers"), dict):
            out["headers"] = {str(k): str(v) for k, v in raw["headers"].items()}

    return out


def _disk_scan_enabled() -> bool:
    """Tests set `SHEJANE_RUNTIME_MCP_DISCOVERY=off` (via the autouse
    fixture in tests/conftest.py) to keep their environment hermetic.
    Production leaves it unset → scan runs."""
    flag = os.environ.get("SHEJANE_RUNTIME_MCP_DISCOVERY", "").strip().lower()
    return flag != "off"


def discover_servers(data_dir: Path | None) -> list[DiscoveredServer]:
    """Read Runtime-owned sources in priority order and normalize servers.

    Dedupes by `name` — the FIRST source that defines a given server
    wins. The explicit environment override takes precedence over the
    Runtime-owned files.

    Env override (`SHEJANE_RUNTIME_MCP_SERVERS`) is treated as its own
    "source" at the head of the priority list. When the env var is
    set, on-disk sources are STILL consulted afterwards (so a test or
    debug var augments rather than replaces) — except names that
    collide with the env, which the env wins.

    Disk scanning is suppressed when `SHEJANE_RUNTIME_MCP_DISCOVERY` is
    `off` — used by the test suite via conftest.py to avoid loading the
    dev machine's real MCP configs.
    Other clients' global configuration is intentionally ignored. Importing
    an external configuration must be an explicit user action.
    """
    out: list[DiscoveredServer] = []
    seen: set[str] = set()

    # 1. env override goes first. Always honored regardless of the
    # disk-scan flag — it's the explicit-config path.
    env_raw = os.environ.get("SHEJANE_RUNTIME_MCP_SERVERS", "").strip()
    if env_raw:
        try:
            env_map = json.loads(env_raw)
            if isinstance(env_map, dict):
                # Allow either wrapped or bare-map form.
                if isinstance(env_map.get("mcpServers"), dict):
                    env_map = env_map["mcpServers"]
                for name, raw in env_map.items():
                    norm = _normalize_entry(name, raw)
                    if norm is None or name in seen:
                        continue
                    seen.add(name)
                    out.append(
                        DiscoveredServer(
                            name=name,
                            config=norm,
                            source=SOURCE_ENV,
                            source_path="<env SHEJANE_RUNTIME_MCP_SERVERS>",
                        )
                    )
        except json.JSONDecodeError as exc:
            log.warning("ignoring malformed SHEJANE_RUNTIME_MCP_SERVERS: %s", exc)

    # 2. then each on-disk source in priority order.
    if not _disk_scan_enabled():
        return out
    for src in _candidate_source_files(data_dir):
        raw_obj = _read_config_file(src)
        if not raw_obj:
            continue
        servers_map = _extract_servers_map(raw_obj)
        for name, raw_entry in servers_map.items():
            if name in seen:
                continue
            norm = _normalize_entry(name, raw_entry)
            if norm is None:
                continue
            seen.add(name)
            out.append(
                DiscoveredServer(
                    name=name,
                    config=norm,
                    source=src.source,
                    source_path=str(src.path),
                )
            )

    return out


def _load_mcp_config(data_dir: Path | None) -> dict[str, dict[str, Any]]:
    """Public-ish helper kept for back-compat. Returns the normalized
    config map ready to feed into MultiServerMCPClient."""
    return {srv.name: srv.config for srv in discover_servers(data_dir)}


def mcp_sensitive_values(
    data_dir: Path | None,
    *,
    disabled_servers: set[str] | None = None,
) -> tuple[str, ...]:
    """Return configured MCP credentials for metadata leak detection.

    Values are never logged or copied into the reusable graph definition.
    """
    config = _load_mcp_config(data_dir)
    if disabled_servers:
        config = {name: cfg for name, cfg in config.items() if name not in disabled_servers}
    return _sensitive_values_from_config(config)


def _sensitive_values_from_config(config: dict[str, dict[str, Any]]) -> tuple[str, ...]:
    configured_values = {
        value
        for server in config.values()
        for section_name in ("headers", "env")
        for value in _string_values(server.get(section_name))
        if len(value) >= 4
    }
    values = {
        variant
        for value in configured_values
        for variant in _credential_variants(value)
        if len(variant) >= 4
    }
    return tuple(sorted(values, key=len, reverse=True))


def validate_mcp_tools(
    tools: list[BaseTool],
    *,
    sensitive_values: tuple[str, ...] = (),
    reserved_names: set[str] | None = None,
) -> list[ValidatedMCPTool]:
    """Validate untrusted MCP metadata before graph compilation.

    MCP servers control tool names, descriptions, and JSON schemas. Only
    bounded, JSON-only metadata crosses into the cached definition and model
    request; actual tool objects remain execution-local.
    """
    accepted: list[ValidatedMCPTool] = []
    seen = set(reserved_names or ())
    total_schema_bytes = 0
    if len(tools) > MAX_MCP_TOOLS:
        log.warning("MCP tool limit reached; ignoring %d excess tools", len(tools) - MAX_MCP_TOOLS)
    for index, tool in enumerate(tools[:MAX_MCP_TOOLS]):
        try:
            name = str(getattr(tool, "name", ""))
        except Exception:
            log.warning("MCP tool candidate %d rejected because its name is unreadable", index)
            continue
        if _contains_sensitive_metadata((name,), sensitive_values=sensitive_values):
            log.warning(
                "MCP tool candidate %d rejected because its name contains credential material",
                index,
            )
            continue
        if not _TOOL_NAME_RE.fullmatch(name) or name in seen:
            log.warning("MCP tool candidate %d rejected due to invalid or reserved name", index)
            continue
        description = str(getattr(tool, "description", "") or "").strip()
        if len(description) > MAX_MCP_DESCRIPTION_CHARS:
            log.warning("MCP tool candidate %d rejected due to oversized description", index)
            continue
        try:
            schema = _safe_tool_schema(tool)
            schema_strings = _validate_schema_tree(schema)
            encoded = json.dumps(
                schema,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        except Exception as exc:
            log.warning(
                "MCP tool candidate %d rejected due to invalid schema: %s",
                index,
                type(exc).__name__,
            )
            continue
        if len(encoded) > MAX_MCP_SCHEMA_BYTES:
            log.warning("MCP tool candidate %d rejected due to oversized schema", index)
            continue
        if total_schema_bytes + len(encoded) > MAX_MCP_TOTAL_SCHEMA_BYTES:
            log.warning("MCP aggregate schema limit reached; skipping remaining tools")
            break
        if _contains_sensitive_metadata(
            (description, *schema_strings),
            sensitive_values=sensitive_values,
        ):
            log.warning(
                "MCP tool candidate %d rejected because its metadata contains credential material",
                index,
            )
            continue
        # JSON round-trip severs references to server-owned mutable objects.
        safe_schema = json.loads(encoded)
        accepted.append(ValidatedMCPTool(tool, name, description, safe_schema))
        seen.add(name)
        total_schema_bytes += len(encoded)
    return accepted


def _string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for nested in value.values() for item in _string_values(nested)]
    if isinstance(value, list):
        return [item for nested in value for item in _string_values(nested)]
    return []


def _credential_variants(value: str) -> tuple[str, ...]:
    scheme, separator, credential = value.partition(" ")
    if separator and scheme.lower() in {"bearer", "basic", "token"}:
        return value, credential.strip()
    return (value,)


def _safe_tool_schema(tool: BaseTool) -> dict[str, Any]:
    schema_source = getattr(tool, "tool_call_schema", None) or tool.args_schema
    if schema_source is None:
        return {"type": "object", "properties": {}}
    if isinstance(schema_source, dict):
        return schema_source
    schema = schema_source.model_json_schema()
    if not isinstance(schema, dict):
        raise TypeError("tool schema must be an object")
    return schema


def _validate_schema_tree(value: Any) -> list[str]:
    strings: list[str] = []
    nodes = 0

    def visit(node: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_MCP_SCHEMA_NODES:
            raise ValueError("schema node limit exceeded")
        if depth > MAX_MCP_SCHEMA_DEPTH:
            raise ValueError("schema depth limit exceeded")
        if isinstance(node, dict):
            for key, child in node.items():
                if not isinstance(key, str):
                    raise TypeError("schema keys must be strings")
                strings.append(key)
                visit(child, depth + 1)
            return
        if isinstance(node, list):
            for child in node:
                visit(child, depth + 1)
            return
        if isinstance(node, str):
            strings.append(node)
            return
        if node is None or isinstance(node, bool | int | float):
            return
        raise TypeError("schema contains a non-JSON value")

    visit(value, 0)
    return strings


def _contains_sensitive_metadata(
    values: tuple[str, ...],
    *,
    sensitive_values: tuple[str, ...],
) -> bool:
    for value in values:
        if _CREDENTIAL_PATTERN_RE.search(value):
            return True
        if any(secret in value for secret in sensitive_values):
            return True
    return False
