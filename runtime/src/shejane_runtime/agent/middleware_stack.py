"""Runtime middleware ordering and retry policy."""

from __future__ import annotations

from langchain.agents.middleware import (
    AgentMiddleware,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
)

from ..config import Settings
from ..middleware.budget_control import DynamicBudgetControlMiddleware
from ..middleware.completion_router import CompletionRouterMiddleware
from ..middleware.file_write_conflict import FileWriteConflictMiddleware
from ..middleware.input_guard import InputGuardMiddleware
from ..middleware.outbound_policy import OutboundPolicyMiddleware
from ..middleware.plan_first import PlanFirstMiddleware
from ..middleware.tool_execution import ToolExecutionMiddleware
from ..middleware.tool_result_retry import ToolResultRetryMiddleware
from ..middleware.tool_review import ToolReviewMiddleware
from ..middleware.tool_visibility import ToolVisibilityMiddleware
from .model_runtime import RuntimeModelMiddleware
from .prompt_middleware import RuntimePromptMiddleware

MAX_SUBAGENT_TASKS_PER_RUN = 2
_MAX_TEAM_RUNS_PER_RUN = 2
_MAX_CHILD_CONTROL_CALLS_PER_RUN = 16
RETRY_ELIGIBLE_TOOLS: list[str] = ["web.fetch", "read_file"]


def _custom_middleware(
    settings: Settings,
    *,
    deferred_tool_names: set[str] | None = None,
) -> list[AgentMiddleware]:
    """Our middleware that deepagents doesn't auto-add.

    Order:
      InputGuard → ToolCallLimit → ToolRetry →
      durable model-call reservation →
      CompletionRouter

    `before_*` fire top-to-bottom, `after_*` fire bottom-to-top —
    CompletionRouter is the only custom after-model hook that may change the
    graph route. Execution settlement and cleanup are owned by RunCoordinator,
    outside the graph middleware chain.
    """
    middleware: list[AgentMiddleware] = [
        RuntimePromptMiddleware(),
        RuntimeModelMiddleware(),
        DynamicBudgetControlMiddleware(),
        ToolVisibilityMiddleware(
            deferred_tool_names=deferred_tool_names,
            blocked_tool_names={"task"} if not settings.enable_subagents else None,
        ),
        OutboundPolicyMiddleware(),
        InputGuardMiddleware(mode=settings.input_guard_mode),  # P1
        # Plan & Execute mode (off | always | auto; auto-skips trivial
        # tasks). Sourced from settings so the Advanced agent-settings
        # panel can override the SHEJANE_PLAN_FIRST env default per-run.
        PlanFirstMiddleware(mode=settings.plan_first_mode),
        ToolReviewMiddleware(),
        ToolExecutionMiddleware(),
        FileWriteConflictMiddleware(),
    ]
    middleware.extend(
        [
            ToolCallLimitMiddleware(  # P8
                tool_name="web.search",
                run_limit=settings.research_search_limit,
            ),
            ToolCallLimitMiddleware(
                tool_name="task",
                run_limit=MAX_SUBAGENT_TASKS_PER_RUN,
            ),
            ToolCallLimitMiddleware(
                tool_name="team.run",
                run_limit=_MAX_TEAM_RUNS_PER_RUN,
            ),
            *(
                ToolCallLimitMiddleware(
                    tool_name=tool_name,
                    run_limit=_MAX_CHILD_CONTROL_CALLS_PER_RUN,
                )
                for tool_name in (
                    "child.spawn",
                    "child.list",
                    "child.check",
                    "child.wait",
                    "child.cancel",
                    "mailbox.send",
                    "mailbox.inbox",
                    "mailbox.reply",
                    "mailbox.ack",
                )
            ),
            # Retry only network/IO-flaky tools, with a tight retryable
            # exception set. We deliberately exclude tools that use
            # `interrupt()` (user.ask, task, etc.) because
            # ToolRetryMiddleware's `_handle_failure` catches *any*
            # Exception (including GraphInterrupt) and converts it to a
            # ToolMessage — that would swallow our pause signals. Only
            # listing the tools we DO want retried (RETRY_ELIGIBLE_TOOLS)
            # keeps GraphInterrupt-flow tools out of its catch path.
            ToolRetryMiddleware(
                max_retries=settings.max_tool_retries,
                tools=list(RETRY_ELIGIBLE_TOOLS),
                retry_on=(
                    ConnectionError,
                    TimeoutError,
                    OSError,
                ),
            ),
            # Some tools return structured envelopes instead of raising.
            # Retry only when the envelope explicitly opts in with
            # `{ok:false, retryable:true}` and the tool is in the same
            # allowlist as exception retries.
            ToolResultRetryMiddleware(
                max_retries=settings.max_tool_retries,
                tools=list(RETRY_ELIGIBLE_TOOLS),
                initial_delay=0.25,
                max_delay=2.0,
            ),
        ]
    )
    middleware.extend(
        [
            CompletionRouterMiddleware(max_verification_repairs=settings.verification_repair_max),
        ]
    )
    return middleware
