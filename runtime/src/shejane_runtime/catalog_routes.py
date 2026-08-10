"""Runtime-owned MCP server and personal Skill catalog routes."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Request

from .api_schemas import (
    McpServerCatalog,
    McpServerDeleteResponse,
    McpServerInfo,
    McpServerWriteRequest,
    McpServerWriteResponse,
    SkillDeleteResponse,
    SkillFile,
    SkillWriteRequest,
    SkillWriteResponse,
)

catalog_router = APIRouter()
_SAFE_CATALOG_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


def _list_skill_files() -> list[dict[str, str]]:
    """List directory-based SKILL.md files visible to the Runtime."""
    from .agent.builder import _resolve_skills_dirs

    out: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for root in _resolve_skills_dirs():
        source = (root.parent.name or root.name).lstrip(".")
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name.startswith(("_", ".")):
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.is_file():
                continue
            try:
                text = skill_md.read_text(encoding="utf-8")
            except OSError:
                continue
            title, description = _parse_frontmatter_minimal(text)
            display_name = entry.name
            if display_name in seen_names:
                continue
            seen_names.add(display_name)
            out.append(
                {
                    "name": display_name,
                    "title": title or display_name,
                    "description": description,
                    "path": str(skill_md),
                    "source": source,
                    "root_path": str(root),
                }
            )
    return out


def _parse_frontmatter_minimal(text: str) -> tuple[str, str]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.DOTALL)
    if match is None:
        return "", ""
    try:
        metadata = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return "", ""
    if not isinstance(metadata, dict):
        return "", ""
    title = metadata.get("title") or metadata.get("name") or ""
    description = metadata.get("description") or ""
    return str(title), str(description)


def _safe_catalog_name(raw: str | None) -> str:
    name = (raw or "").strip()
    if not _SAFE_CATALOG_NAME_RE.fullmatch(name):
        raise HTTPException(
            status_code=400,
            detail="name must start with a letter or number and contain only letters, numbers, '.', '_' or '-'",
        )
    return name


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    _write_text_atomic(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _shejane_mcp_config_path() -> Path:
    return Path.home() / ".shejane" / "mcp-servers.json"


def _read_shejane_mcp_config() -> dict[str, Any]:
    path = _shejane_mcp_config_path()
    if not path.exists():
        return {"mcpServers": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=400, detail=f"shejane MCP config is not readable JSON: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        return {"mcpServers": {}}
    servers = raw.get("mcpServers")
    if isinstance(servers, dict):
        return raw
    if all(
        isinstance(value, dict) and ("command" in value or "url" in value) for value in raw.values()
    ):
        return {"mcpServers": raw}
    raw["mcpServers"] = {}
    return raw


def _mcp_info_from_config(name: str, config: dict[str, Any]) -> McpServerInfo:
    return McpServerInfo(
        name=name,
        transport=str(config.get("transport") or "stdio"),
        source="shejane",
        source_path=str(_shejane_mcp_config_path()),
        command=config.get("command") if isinstance(config.get("command"), str) else None,
        args=[str(arg) for arg in config.get("args", []) or []],
        url=config.get("url") if isinstance(config.get("url"), str) else None,
        env_keys=sorted(str(key) for key in (config.get("env") or {}).keys()),
        cwd=config.get("cwd") if isinstance(config.get("cwd"), str) else None,
    )


def _write_mcp_server(
    route_name: str | None, request: McpServerWriteRequest
) -> McpServerWriteResponse:
    from .tools.mcp import _normalize_entry

    name = _safe_catalog_name(route_name or request.name)
    raw: dict[str, Any] = {"transport": request.transport}
    if request.command is not None:
        raw["command"] = request.command
    if request.args:
        raw["args"] = request.args
    if request.url is not None:
        raw["url"] = request.url
    if request.env:
        raw["env"] = request.env
    if request.cwd is not None:
        raw["cwd"] = request.cwd

    normalized = _normalize_entry(name, raw)
    if normalized is None:
        raise HTTPException(status_code=400, detail="MCP server must include command or url")
    config = _read_shejane_mcp_config()
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        servers = {}
        config["mcpServers"] = servers
    servers[name] = normalized
    _write_json_atomic(_shejane_mcp_config_path(), config)
    return McpServerWriteResponse(server=_mcp_info_from_config(name, normalized))


def _personal_skills_root() -> Path:
    root = Path.home() / ".shejane" / "skills"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _skill_md_path(name: str) -> Path:
    root = _personal_skills_root().resolve()
    skill_dir = (root / name).resolve()
    if root not in skill_dir.parents:
        raise HTTPException(status_code=400, detail="skill path escapes personal skills root")
    return skill_dir / "SKILL.md"


def _default_skill_content(name: str, description: str) -> str:
    lines = ["---", f"name: {name}"]
    description = description.strip()
    if description:
        lines.append(f"description: {description}")
    lines.extend(["---", "", f"# {name}", ""])
    if description:
        lines.extend([description, ""])
    return "\n".join(lines)


def _skill_file_from_path(name: str, path: Path) -> SkillFile:
    if not path.is_file():
        raise HTTPException(status_code=404, detail="skill not found")
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"failed to read skill: {exc}") from exc
    _, description = _parse_frontmatter_minimal(content)
    return SkillFile(
        name=name,
        description=description,
        path=str(path),
        root_path=str(_personal_skills_root()),
        content=content,
    )


def _normalize_local_skill_content(name: str, description: str, content: str) -> str:
    match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", content, re.DOTALL)
    body = content
    metadata: dict[str, Any] = {}
    if match is not None:
        try:
            parsed = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            raise HTTPException(status_code=422, detail="invalid Skill YAML frontmatter") from exc
        if parsed is not None and not isinstance(parsed, dict):
            raise HTTPException(status_code=422, detail="Skill frontmatter must be an object")
        metadata = dict(parsed or {})
        body = content[match.end() :]
    metadata["name"] = name
    requested_description = description.strip()
    if requested_description:
        metadata["description"] = requested_description
    elif not str(metadata.get("description") or "").strip():
        metadata["description"] = name
    header = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).rstrip()
    body = body.lstrip("\n")
    return f"---\n{header}\n---\n{body}".rstrip() + "\n"


def _write_local_skill(route_name: str | None, request: SkillWriteRequest) -> SkillWriteResponse:
    name = _safe_catalog_name(route_name or request.name)
    path = _skill_md_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = request.content
    if content is None:
        content = _default_skill_content(name, request.description)
    content = _normalize_local_skill_content(name, request.description, content)
    try:
        _write_text_atomic(path, content)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"failed to write skill: {exc}") from exc
    return SkillWriteResponse(skill=_skill_file_from_path(name, path))


@catalog_router.get("/v1/mcp-servers", response_model=McpServerCatalog)
async def list_mcp_servers(request: Request) -> McpServerCatalog:
    """List MCP Servers explicitly owned by this Runtime."""
    from .config import get_settings
    from .tools.mcp import _candidate_source_files, discover_servers

    settings = get_settings()
    discovered = discover_servers(settings.data_dir)
    statuses = request.app.state.mcp_catalog.server_statuses()
    sources_scanned: list[str] = ["env"]
    for source_file in _candidate_source_files(settings.data_dir):
        if source_file.source not in sources_scanned:
            sources_scanned.append(source_file.source)
    servers = []
    for server in discovered:
        status = statuses.get(server.name, {})
        servers.append(
            McpServerInfo(
                name=server.name,
                transport=server.config.get("transport", "stdio"),
                source=server.source,
                source_path=server.source_path,
                command=server.config.get("command"),
                args=list(server.config.get("args", []) or []),
                url=server.config.get("url"),
                env_keys=sorted(list((server.config.get("env") or {}).keys())),
                cwd=server.config.get("cwd"),
                status=status.get("status", "idle"),
                tool_count=int(status.get("tool_count", 0)),
                error_type=status.get("error_type"),
            )
        )
    return McpServerCatalog(servers=servers, sources_scanned=sources_scanned)


@catalog_router.post("/v1/mcp-servers", response_model=McpServerWriteResponse)
async def create_mcp_server(
    request: Request, body: McpServerWriteRequest
) -> McpServerWriteResponse:
    response = _write_mcp_server(body.name, body)
    await request.app.state.mcp_catalog.invalidate(response.server.name)
    request.app.state.mcp_catalog.request_refresh()
    return response


@catalog_router.put("/v1/mcp-servers/{server_name}", response_model=McpServerWriteResponse)
async def update_mcp_server(
    server_name: str, request: Request, body: McpServerWriteRequest
) -> McpServerWriteResponse:
    response = _write_mcp_server(server_name, body)
    await request.app.state.mcp_catalog.invalidate(response.server.name)
    request.app.state.mcp_catalog.request_refresh()
    return response


@catalog_router.delete("/v1/mcp-servers/{server_name}", response_model=McpServerDeleteResponse)
async def delete_mcp_server(server_name: str, request: Request) -> McpServerDeleteResponse:
    name = _safe_catalog_name(server_name)
    config = _read_shejane_mcp_config()
    servers = config.setdefault("mcpServers", {})
    if isinstance(servers, dict):
        servers.pop(name, None)
    _write_json_atomic(_shejane_mcp_config_path(), config)
    await request.app.state.mcp_catalog.invalidate(name)
    await request.app.state.store.delete_mcp_catalog(name)
    return McpServerDeleteResponse(name=name)


@catalog_router.get("/v1/skills")
async def list_local_skills() -> dict[str, Any]:
    """Catalog of every SKILL.md the runtime can see across all
    configured skill roots (`~/.shejane/skills/`, `~/.claude/skills/`,
    or `SHEJANE_RUNTIME_SKILLS_PATH` overrides). Skills are managed
    out-of-band — the user drops directories into a root themselves
    (or installs via the skills.sh CLI into `~/.claude/skills/`) and
    the runtime picks them up on next scan.

    Also surfaces the roots themselves under `roots` so the UI can
    render section headers (e.g. "Personal" for shejane) even when
    a root is empty — otherwise the user has no idea where to drop
    their SKILL.md directories.
    """
    from .agent.builder import _resolve_skills_dirs

    roots = [
        {"source": (directory.parent.name or directory.name).lstrip("."), "path": str(directory)}
        for directory in _resolve_skills_dirs()
    ]
    return {"skills": _list_skill_files(), "roots": roots}


@catalog_router.post("/v1/skills", response_model=SkillWriteResponse)
async def create_local_skill(request: SkillWriteRequest) -> SkillWriteResponse:
    return _write_local_skill(request.name, request)


@catalog_router.get("/v1/skills/{skill_name}", response_model=SkillFile)
async def get_local_skill(skill_name: str) -> SkillFile:
    name = _safe_catalog_name(skill_name)
    return _skill_file_from_path(name, _skill_md_path(name))


@catalog_router.put("/v1/skills/{skill_name}", response_model=SkillWriteResponse)
async def update_local_skill(skill_name: str, request: SkillWriteRequest) -> SkillWriteResponse:
    return _write_local_skill(skill_name, request)


@catalog_router.delete("/v1/skills/{skill_name}", response_model=SkillDeleteResponse)
async def delete_local_skill(skill_name: str) -> SkillDeleteResponse:
    name = _safe_catalog_name(skill_name)
    path = _skill_md_path(name)
    if not path.exists():
        raise HTTPException(status_code=404, detail="skill not found")
    shutil.rmtree(path.parent)
    return SkillDeleteResponse(name=name)
