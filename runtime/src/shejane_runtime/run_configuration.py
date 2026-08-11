from __future__ import annotations

from typing import Any

from .config import (
    MAX_CONCURRENT_SUBAGENT_TASKS,
    PREFERRED_SUBAGENT_CONCURRENCY,
    Settings,
    clamp_run_budget,
)
from .middleware.tool_visibility import execution_policy_for_task

SIMPLE_MODEL_CALL_LIMIT = 12
COMPLEX_MODEL_CALL_SOFT_LIMIT = 24
RUNTIME_PROTOCOL_VERSION = 1
RUNTIME_CAPABILITIES = frozenset(
    {
        "agent.run",
        "agent.stream",
        "attachments",
        "workspace.files",
        "memory",
        "skills",
        "mcp",
        "plugins",
        "subagents",
        "durable-child-runs",
        "multi-agent-coordination",
        "schedules",
        "hitl",
    }
)


def _agent_model_call_limit(configured_limit: int, task_goal: str | None) -> int:
    del task_goal
    return max(1, int(configured_limit))


def _agent_soft_model_call_limit(configured_limit: int, task_goal: str | None) -> int:
    policy = execution_policy_for_task(task_goal)
    return _agent_soft_model_call_limit_for_complexity(
        configured_limit,
        str(policy["complexity"]),
    )


def _agent_soft_model_call_limit_for_complexity(
    configured_limit: int,
    complexity: str,
) -> int:
    baseline = SIMPLE_MODEL_CALL_LIMIT if complexity == "simple" else COMPLEX_MODEL_CALL_SOFT_LIMIT
    return max(1, min(int(configured_limit), baseline))


def _agent_model_call_final_reserve(hard_limit: int) -> int:
    return min(2, max(1, hard_limit))


def _execution_policy_snapshot(
    goal: str,
    settings_snapshot: dict[str, Any],
) -> dict[str, Any]:
    stored = settings_snapshot.get("_execution_policy")
    policy = (
        dict(stored)
        if isinstance(stored, dict) and stored.get("complexity") in {"simple", "complex"}
        else execution_policy_for_task(goal)
    )
    configured = settings_snapshot.get("max_model_calls")
    configured_limit = int(configured) if isinstance(configured, int) and configured > 0 else 100
    stored_hard = policy.get("max_model_calls")
    hard_limit = (
        int(stored_hard)
        if isinstance(stored_hard, int) and stored_hard > 0
        else _agent_model_call_limit(configured_limit, goal)
    )
    stored_soft = policy.get("soft_model_call_limit")
    soft_limit = (
        max(1, min(hard_limit, int(stored_soft)))
        if isinstance(stored_soft, int) and stored_soft > 0
        else _agent_soft_model_call_limit_for_complexity(
            hard_limit,
            str(policy["complexity"]),
        )
    )
    stored_reserve = policy.get("final_model_call_reserve")
    final_reserve = (
        max(1, min(hard_limit, int(stored_reserve)))
        if isinstance(stored_reserve, int) and stored_reserve > 0
        else _agent_model_call_final_reserve(hard_limit)
    )
    subagent_allowed = (
        bool(policy.get("subagent_allowed")) and settings_snapshot.get("subagents") is not False
    )
    stored_max_concurrency = policy.get("max_concurrent_subagent_tasks")
    max_concurrent_subagent_tasks = (
        int(stored_max_concurrency)
        if isinstance(stored_max_concurrency, int)
        and not isinstance(stored_max_concurrency, bool)
        and stored_max_concurrency >= 0
        else MAX_CONCURRENT_SUBAGENT_TASKS
    )
    stored_preferred_concurrency = policy.get("preferred_subagent_concurrency")
    preferred_subagent_concurrency = min(
        int(stored_preferred_concurrency)
        if isinstance(stored_preferred_concurrency, int)
        and not isinstance(stored_preferred_concurrency, bool)
        and stored_preferred_concurrency >= 0
        else PREFERRED_SUBAGENT_CONCURRENCY,
        max_concurrent_subagent_tasks,
    )
    if not subagent_allowed:
        max_concurrent_subagent_tasks = 0
        preferred_subagent_concurrency = 0
    return {
        **policy,
        "max_model_calls": hard_limit,
        "soft_model_call_limit": soft_limit,
        "final_model_call_reserve": final_reserve,
        "subagent_budget_mode": "shared_model_budget",
        "preferred_subagent_concurrency": preferred_subagent_concurrency,
        "max_concurrent_subagent_tasks": max_concurrent_subagent_tasks,
    }


def runtime_capabilities(settings: Settings) -> frozenset[str]:
    """Return capabilities backed by resources that are available now."""
    return RUNTIME_CAPABILITIES


def _apply_advanced_overrides(base: Settings, run_settings: dict[str, Any]) -> Settings:
    """Fold the client's "Advanced" agent-settings knobs onto a copy of the
    base Settings.

    Knobs absent from `run_settings` keep the runtime's env/default value, so
    legacy callers (curl, tests, pre-panel client builds) are unaffected.
    `model_copy(update=...)` does NOT re-validate, so each value is coerced to
    its field's type here; unknown keys and unparseable / out-of-range values
    are ignored rather than crashing the run.

    The input guard is a security-posture knob: a per-run override may only
    strengthen the machine/env baseline, never weaken it.
    """
    if run_settings.get("_snapshot_version") == 1:
        snapshot_fields = {
            "max_model_calls": "max_model_calls",
            "max_tool_retries": "max_tool_retries",
            "research_search_limit": "research_search_limit",
            "subagents": "enable_subagents",
            "browser_headless": "browser_headless",
            "input_guard": "input_guard_mode",
            "plan_first": "plan_first_mode",
            "pii_redact": "pii_redact_types",
            "verification_repair_max": "verification_repair_max",
            "repair_workflow_max": "repair_workflow_max",
            "memory_sources": "memory_sources",
        }
        snapshot = {
            field: run_settings[key]
            for key, field in snapshot_fields.items()
            if key in run_settings
        }
        rank = {"off": 0, "observe": 1, "block": 2}
        if rank.get(str(base.input_guard_mode), 0) > rank.get(
            str(snapshot.get("input_guard_mode")), 0
        ):
            snapshot["input_guard_mode"] = base.input_guard_mode
        return base.model_copy(update=snapshot)

    overrides: dict[str, Any] = {}
    # Integer knobs.
    for key, field in (
        ("max_model_calls", "max_model_calls"),
        ("max_tool_retries", "max_tool_retries"),
        ("research_search_limit", "research_search_limit"),
    ):
        raw = run_settings.get(key)
        if raw is None:
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        overrides[field] = clamp_run_budget(field, value)
    # Boolean knobs (accept real bools or "on"/"true"/"1"/"yes").
    for key, field in (
        ("subagents", "enable_subagents"),
        ("browser_headless", "browser_headless"),
    ):
        raw = run_settings.get(key)
        if raw is None:
            continue
        overrides[field] = (
            raw if isinstance(raw, bool) else str(raw).strip().lower() in {"1", "true", "yes", "on"}
        )
    # Enumerated string knobs — only accepted from a fixed allow-list.
    # NOTE: input_guard is a security-posture knob handled separately below
    # (a per-run override may only strengthen it, never weaken it).
    for key, field, allowed in (("plan_first", "plan_first_mode", {"off", "auto", "always"}),):
        raw = run_settings.get(key)
        if raw is None:
            continue
        val = str(raw).strip().lower()
        if val in allowed:
            overrides[field] = val
    # Security-posture knob — input guard. A per-run override may only RAISE the
    # guard, never lower the machine/env baseline (strength: off < observe <
    # block). A client sending "observe" against a base of "block" is ignored.
    raw = run_settings.get("input_guard")
    if raw is not None:
        val = str(raw).strip().lower()
        rank = {"off": 0, "observe": 1, "block": 2}
        base_rank = rank.get(str(base.input_guard_mode).strip().lower(), 0)
        # Strictly-greater: only a real strengthening is applied; same-or-lower
        # is left at the baseline (same level would be a no-op copy anyway).
        if val in rank and rank[val] > base_rank:
            overrides["input_guard_mode"] = val
    return base.model_copy(update=overrides) if overrides else base


_PUBLIC_RUN_SETTING_KEYS = frozenset(
    {
        "memory",
        "skills",
        "mcp",
        "mcp_disabled",
        "max_model_calls",
        "max_tool_retries",
        "research_search_limit",
        "subagents",
        "browser_headless",
        "input_guard",
        "plan_first",
        "permission_mode",
    }
)


def public_run_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    return {key: value for key, value in (raw or {}).items() if key in _PUBLIC_RUN_SETTING_KEYS}


def sanitize_run_metadata(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only Runtime-owned workflow metadata; never persist arbitrary bags."""
    metadata = raw or {}
    clean: dict[str, Any] = {}
    intent = str(metadata.get("intent") or "").strip().lower()
    if intent in {"repair", "retry"}:
        clean["intent"] = intent
    for key in ("source_run_id", "source_message_id"):
        value = metadata.get(key)
        if isinstance(value, str) and 0 < len(value) <= 128:
            clean[key] = value
    attempt = metadata.get("attempt")
    if isinstance(attempt, int) and not isinstance(attempt, bool) and 1 <= attempt <= 100:
        clean["attempt"] = attempt
    for key in ("failure_category", "failure_action_kind"):
        value = metadata.get(key)
        if (
            isinstance(value, str)
            and 0 < len(value) <= 64
            and all(char.isalnum() or char in "_-" for char in value)
        ):
            clean[key] = value
    return clean


def freeze_run_settings(base: Settings, raw: dict[str, Any] | None) -> dict[str, Any]:
    public = public_run_settings(raw)
    effective = _apply_advanced_overrides(base, public)
    raw_disabled = public.get("mcp_disabled")
    disabled = (
        sorted(
            {
                str(name).strip()
                for name in raw_disabled
                if isinstance(name, str) and str(name).strip()
            }
        )
        if isinstance(raw_disabled, list)
        else []
    )

    def toggle(name: str, default: str = "on") -> str:
        return "off" if str(public.get(name, default)).strip().lower() == "off" else "on"

    permission_mode = str(public.get("permission_mode") or "ask").strip().lower()
    if permission_mode not in {"ask", "auto", "full_access"}:
        permission_mode = "ask"

    return {
        "_snapshot_version": 1,
        "memory": toggle("memory"),
        "skills": toggle("skills"),
        "mcp": toggle("mcp"),
        "mcp_disabled": disabled,
        "permission_mode": permission_mode,
        "max_model_calls": effective.max_model_calls,
        "max_tool_retries": effective.max_tool_retries,
        "research_search_limit": effective.research_search_limit,
        "subagents": effective.enable_subagents,
        "browser_headless": effective.browser_headless,
        "input_guard": effective.input_guard_mode,
        "plan_first": effective.plan_first_mode,
        "pii_redact": effective.pii_redact_types,
        "verification_repair_max": effective.verification_repair_max,
        "repair_workflow_max": effective.repair_workflow_max,
        "memory_sources": effective.memory_sources,
    }
