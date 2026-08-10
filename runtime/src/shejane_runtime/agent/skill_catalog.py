"""Skill discovery, catalog identity, and prompt source resolution."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from ..config import Settings


def _resolve_skills_dirs() -> list[Path]:
    """Return every existing skills directory the runtime should scan.

    We deliberately accept multiple roots so the agent can see skills
    from several ecosystems at once:

      1. `SHEJANE_RUNTIME_SKILLS_PATH` env var (comma-separated for
         multiple paths) — full override; when set, the defaults below
         are NOT consulted.
      2. Defaults (used when the env var is unset):
         - `~/.shejane/skills/` — our own canonical location
         - `~/.claude/skills/`  — Claude Code / skills.sh default install
           target (skills.sh CLI installs here when run with
           `--agent claude-code -g`, the most common case)

    Each entry is a `Path` that exists and is a directory. Missing
    paths are silently dropped so an unset Claude install doesn't error.
    """
    custom = os.environ.get("SHEJANE_RUNTIME_SKILLS_PATH", "").strip()
    if custom:
        raw_paths = [p.strip() for p in custom.split(",") if p.strip()]
    else:
        raw_paths = [
            str(Path.home() / ".shejane" / "skills"),
            str(Path.home() / ".claude" / "skills"),
        ]
    out: list[Path] = []
    for raw in raw_paths:
        candidate = Path(raw).expanduser()
        if candidate.is_dir():
            out.append(candidate)
    return out


def skill_catalog_fingerprint() -> str:
    """Hash the complete discovery tree visible to SkillsMiddleware.

    Discovery probes every directory directly below each configured root for
    ``SKILL.md``. Hashing the whole dedicated root therefore covers active
    packages, their supporting files, and directories that can enter or leave
    the catalog. Symlinks are hashed as links and are never traversed; the
    virtual backend rejects links that escape their configured root.
    """
    digest = hashlib.sha256(b"shejane-skill-catalog-v1\0")
    for root_index, root in enumerate(_resolve_skills_dirs()):
        resolved_root = root.resolve(strict=False)
        _update_catalog_digest(
            digest,
            "root",
            str(root_index),
            str(resolved_root),
        )
        for directory, child_dirs, file_names in os.walk(resolved_root, followlinks=False):
            child_dirs.sort()
            file_names.sort()
            directory_path = Path(directory)
            relative_directory = directory_path.relative_to(resolved_root)
            _update_catalog_digest(digest, "directory", relative_directory.as_posix())
            symlink_dirs = [name for name in child_dirs if (directory_path / name).is_symlink()]
            child_dirs[:] = [name for name in child_dirs if name not in symlink_dirs]
            for name in symlink_dirs:
                path = directory_path / name
                _update_catalog_digest(
                    digest,
                    "symlink",
                    (relative_directory / name).as_posix(),
                    os.readlink(path),
                )
            for name in file_names:
                path = directory_path / name
                relative_path = (relative_directory / name).as_posix()
                if path.is_symlink():
                    _update_catalog_digest(
                        digest,
                        "symlink",
                        relative_path,
                        os.readlink(path),
                    )
                    continue
                if not path.is_file():
                    _update_catalog_digest(
                        digest,
                        "special",
                        relative_path,
                        str(path.stat().st_mode),
                    )
                    continue
                _update_catalog_digest(digest, "file", relative_path)
                with path.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
                digest.update(b"\0")
    return digest.hexdigest()


def _update_catalog_digest(digest: Any, *parts: str) -> None:
    for part in parts:
        digest.update(part.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")


def _active_skill_names(skills_arg: list[str] | None) -> list[str]:
    """Best-effort: enumerate installed skill names from the skills
    directory so the ContextBuilder can hint the model that they're
    available. Empty list when skills are off / unresolved.

    The full SKILL.md bodies are loaded into the prompt by deepagents'
    SkillsMiddleware — this layer just primes the model that the
    skills exist (deepagents lists them too but earlier in the loop
    we want our own short echo so the `enabled_skills` priority sits
    above runtime context)."""
    if not skills_arg:
        return []
    names: list[str] = []
    for path_str in skills_arg:
        path = Path(path_str)
        if not path.is_dir():
            continue
        for entry in sorted(path.iterdir()):
            if entry.is_dir() and not entry.name.startswith("_"):
                names.append(entry.name)
    return names


def _resolve_memory_sources(settings: Settings) -> list[str] | None:
    """Parse SHEJANE_RUNTIME_MEMORY_PATHS (comma-separated paths) into the
    `memory=` argument of `create_deep_agent`. Each path is typically an
    `AGENTS.md` file or a directory of such files — `MemoryMiddleware`
    loads them into the system prompt at run start.

    None ⇒ memory loader skipped (MemoryMiddleware no-ops).
    """
    spec = (settings.memory_sources or "").strip()
    if not spec:
        return None
    items = [Path(p.strip()).expanduser() for p in spec.split(",") if p.strip()]
    # Deep Agents expects file paths. Preserve missing paths so its own
    # diagnostics remain useful, but normalize existing directories.
    expanded = [str(path / "AGENTS.md" if path.is_dir() else path) for path in items]
    return expanded or None
