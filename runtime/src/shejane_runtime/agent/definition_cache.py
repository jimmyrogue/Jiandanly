"""Deterministic Agent definition identity and bounded process cache."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from ..config import Settings
from ..tools.registry import tool_definition

_AGENT_DEFINITION_CACHE_MAX = 16
_AGENT_STATE_SCHEMA_VERSION = 2


def _agent_definition_fingerprint(
    *,
    settings: Settings,
    model_profile: Any,
    tools: list[Any],
    subagents: list[Any] | None,
    skills: list[str] | None,
    skill_catalog_hash: str | None,
    memory: list[str] | None,
    plugin_catalog_hash: str | None,
    agent_definition_id: str = "shejane.default",
    agent_definition_version: str = "1",
    agent_role_prompt: str | None = None,
    allowed_tool_names: tuple[str, ...] = (),
) -> str:
    payload = {
        "version": _AGENT_STATE_SCHEMA_VERSION,
        "model_profile": model_profile,
        "tools": [tool_definition(tool) for tool in tools],
        "subagents": [
            {
                "name": item.get("name"),
                "description": item.get("description"),
                "system_prompt": item.get("system_prompt"),
                "tools": [tool_definition(tool) for tool in item.get("tools", [])],
                "middleware": [type(value).__qualname__ for value in item.get("middleware", [])],
            }
            for item in (subagents or [])
            if isinstance(item, dict)
        ],
        "skills": skills or [],
        "skill_catalog_hash": skill_catalog_hash,
        "memory": memory or [],
        "plugin_catalog_hash": plugin_catalog_hash,
        "durable_agent": {
            "id": agent_definition_id,
            "version": agent_definition_version,
            "role_prompt": agent_role_prompt,
            "allowed_tools": sorted(allowed_tool_names),
        },
        "middleware": {
            "max_model_calls": settings.max_model_calls,
            "input_guard": settings.input_guard_mode,
            "plan_first": settings.plan_first_mode,
            "research_limit": settings.research_search_limit,
            "tool_retries": settings.max_tool_retries,
            "verification_repairs": settings.verification_repair_max,
            "subagents": settings.enable_subagents,
            "browser_headless": settings.browser_headless,
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _cached_agent_definition(
    cache: dict[str, Any],
    fingerprint: str,
    compile_definition: Callable[[], Any],
) -> Any:
    if fingerprint in cache:
        definition = cache.pop(fingerprint)
        cache[fingerprint] = definition
        return definition
    definition = compile_definition()
    cache[fingerprint] = definition
    # ponytail: bounded process-local LRU; add durable cache only if compile
    # time remains material across runtime restarts.
    if len(cache) > _AGENT_DEFINITION_CACHE_MAX:
        cache.pop(next(iter(cache)))
    return definition
