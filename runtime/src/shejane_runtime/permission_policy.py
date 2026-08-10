"""Shared permission-scope rules owned by Runtime."""

from __future__ import annotations

from dataclasses import dataclass

IRREVERSIBLE_TOOLS = {
    "office.delete_paragraph",
    "office.delete_slide",
}

RUN_GRANT_RISKS = {
    "control_flow",
    "read_only",
    "runtime_state",
    "sandboxed_command",
    "workspace_write",
}


class PermissionScopeNotAllowedError(ValueError):
    """Raised when a concrete approval cannot safely become a run grant."""


@dataclass(frozen=True, slots=True)
class ApprovalPolicyDecision:
    decision: str
    reason: str


def approval_policy_decision(
    tool_name: str,
    risk: str,
    permission_mode: str = "ask",
) -> ApprovalPolicyDecision:
    """Return the Runtime-owned P10 decision before optional model review."""
    if tool_name in IRREVERSIBLE_TOOLS:
        return ApprovalPolicyDecision("ask", "irreversible")
    if tool_name == "image.generate":
        return ApprovalPolicyDecision("allow", "image_generation")
    if permission_mode == "full_access":
        return ApprovalPolicyDecision("allow", "full_access")
    if tool_name == "clipboard.read":
        return ApprovalPolicyDecision("ask", "protected_runtime_state")
    if permission_mode == "auto":
        if risk == "external_or_unknown":
            return ApprovalPolicyDecision("review", "external_or_unknown")
        return ApprovalPolicyDecision("allow", "runtime_safe")
    if risk in {"workspace_write", "sandboxed_command", "external_or_unknown", "plugin_action"}:
        return ApprovalPolicyDecision("ask", risk)
    return ApprovalPolicyDecision("allow", "read_only")


def tool_requires_review(tool_name: str, risk: str, permission_mode: str = "ask") -> bool:
    """Return whether policy requires a person before this call executes."""
    return approval_policy_decision(tool_name, risk, permission_mode).decision in {"ask", "review"}


def can_grant_for_run(*, tool_name: str, risk: str | None) -> bool:
    return tool_name not in IRREVERSIBLE_TOOLS and risk in RUN_GRANT_RISKS


def require_allowed_permission_scope(
    *,
    tool_name: str,
    risk: str | None,
    status: str,
    scope: str,
) -> None:
    if (
        status == "approved"
        and scope == "run"
        and not can_grant_for_run(
            tool_name=tool_name,
            risk=risk,
        )
    ):
        raise PermissionScopeNotAllowedError(
            "this operation cannot be approved for the rest of the run"
        )
