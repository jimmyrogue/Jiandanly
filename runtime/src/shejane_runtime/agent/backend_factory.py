"""Execution workspace and read-only Agent backend routing."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deepagents.backends import CompositeBackend, FilesystemBackend

from ..config import Settings
from ..plugins.sandbox_runtime import configured_srt_launcher
from ..store.sqlite import LocalStore
from .backends import (
    ATTACHMENT_FILE_READ_MAX_MB,
    MODEL_FILE_READ_MAX_MB,
    ReadOnlyBackend,
    ReadOnlyFileBackend,
    RuntimeFilesystemBackend,
    RuntimeLocalShellBackend,
)


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


@dataclass(frozen=True)
class _StoreSandboxLedger:
    """Bind sandbox process records to the attempt that owns them."""

    store: LocalStore
    run_id: str
    execution_attempt_id: str

    async def record(self, *, pid: int, process_started_at: str, settings_path: str) -> str:
        return await self.store.record_sandbox_process(
            run_id=self.run_id,
            execution_attempt_id=self.execution_attempt_id,
            pid=pid,
            process_started_at=process_started_at,
            settings_path=settings_path,
        )

    async def forget(self, record_id: str) -> None:
        await self.store.forget_sandbox_process(record_id)


def _build_agent_backend(
    *,
    effective_workspace: str,
    skills_dirs: list[Path],
    memory_sources: list[str] | None,
    attachment_bindings: list[dict[str, str]] | None = None,
    sandbox_ledger: _StoreSandboxLedger | None = None,
):
    workspace_root = Path(effective_workspace).expanduser().resolve()
    default = RuntimeLocalShellBackend(
        root_dir=workspace_root,
        virtual_mode=True,
        sandbox_launcher=configured_srt_launcher(),
        sandbox_ledger=sandbox_ledger,
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
