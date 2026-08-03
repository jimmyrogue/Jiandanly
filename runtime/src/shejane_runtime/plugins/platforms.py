"""Canonical platform and entrypoint rules for native Managed Worker packages."""

from __future__ import annotations

import os
import platform
import stat
import sys
from pathlib import Path

_SYSTEMS = {
    "darwin": "darwin",
    "linux": "linux",
    "win32": "windows",
}
_MACHINES = {
    "aarch64": "arm64",
    "arm64": "arm64",
    "amd64": "amd64",
    "x86_64": "amd64",
}
_SUPPORTS_EXECUTABLE_MODE = os.name != "nt"


def current_managed_worker_platform(
    *,
    system: str | None = None,
    machine: str | None = None,
) -> str | None:
    """Return the public manifest target for this process, or None if unsupported."""

    operating_system = _SYSTEMS.get((system or sys.platform).lower())
    architecture = _MACHINES.get((machine or platform.machine()).lower())
    if operating_system is None or architecture is None:
        return None
    return f"{operating_system}/{architecture}"


def managed_worker_execution_platform(host_platform: str) -> str:
    """Return the package ABI executed by one host sandbox backend."""

    operating_system, separator, architecture = host_platform.partition("/")
    if separator != "/" or architecture not in {"arm64", "amd64"}:
        raise ValueError("managed worker platform is unsupported")
    if operating_system in {"darwin", "windows"}:
        return f"linux/{architecture}"
    if operating_system == "linux":
        return host_platform
    raise ValueError("managed worker platform is unsupported")


def current_managed_worker_execution_platform(
    *,
    system: str | None = None,
    machine: str | None = None,
) -> str | None:
    host = current_managed_worker_platform(system=system, machine=machine)
    return managed_worker_execution_platform(host) if host is not None else None


def prepare_managed_worker_entrypoint(package_root: Path, relative_path: str) -> Path:
    """Validate the selected entrypoint and set its executable mode where supported."""

    from .package import InvalidPluginPackage

    if package_root.is_symlink() or not package_root.is_dir():
        raise InvalidPluginPackage("managed worker package root is invalid")
    root = package_root.resolve(strict=True)
    entrypoint = root / relative_path
    if entrypoint.is_symlink():
        raise InvalidPluginPackage("managed worker entrypoint cannot be a link")
    try:
        entrypoint.resolve(strict=True).relative_to(root)
        mode = entrypoint.stat(follow_symlinks=False).st_mode
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise InvalidPluginPackage("managed worker entrypoint is invalid") from exc
    if not stat.S_ISREG(mode):
        raise InvalidPluginPackage("managed worker entrypoint must be a regular file")
    if _SUPPORTS_EXECUTABLE_MODE:
        try:
            os.chmod(entrypoint, 0o500, follow_symlinks=False)
        except OSError as exc:
            raise InvalidPluginPackage("managed worker entrypoint cannot be prepared") from exc
    return entrypoint
