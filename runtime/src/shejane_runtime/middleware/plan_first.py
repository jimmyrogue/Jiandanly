"""Classify runs that require Runtime-enforced incremental execution.

This middleware owns only the stable complexity decision. P9's
``CompletionRouterMiddleware`` enforces the resulting state before tool work
or finalization; no injected prose is treated as a control boundary.

Modes (env SHEJANE_PLAN_FIRST):
  off / 0           — disabled
  on / always / 1   — every run requires incremental execution
  auto              — only complex tasks require it
                      (heuristic: user message length / word count)

TodoListMiddleware still exposes ``write_todos``. This module decides when
that tool becomes a required state transition rather than a suggestion.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Annotated, Any, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import PrivateStateAttr

log = logging.getLogger("shejane_runtime.middleware.plan_first")


# Words that strongly suggest a non-trivial task — "auto" mode treats
# their presence as a signal to enable plan-first for this run.
COMPLEXITY_HINTS = (
    "research",
    "search the internet",
    "search the web",
    "compare",
    "analyze",
    "summarize",
    "plan",
    "design",
    "investigate",
    "draft",
    "write",
    "build",
    "implement",
    "创建",
    "编写",
    "实现",
    "调研",
    "查一下互联网",
    "查查互联网",
    "上网查",
    "联网查",
    "搜索互联网",
    "比较",
    "分析",
    "设计",
    "修复",
    "重构",
    "构建",
    "写一个",
)

# Min user-message length (chars) that "auto" mode treats as complex.
AUTO_COMPLEX_CHARS = 140

_EXPLICIT_SINGLE_STEP = re.compile(
    r"\bcall\s+exactly\s+the\s+[a-z0-9_.-]+\s+tool\s+once\b|只调用[^。\n]{0,80}工具一次",
    re.IGNORECASE,
)
_EXPLICIT_TOOL_DIRECTIVE = re.compile(
    r"\b(?:call|use)\s+(?:exactly\s+)?(?:the\s+)?([a-z][a-z0-9_.-]*)(?:\s+tool)?\b",
    re.IGNORECASE,
)
_NEGATED_DIRECTIVE = re.compile(r"(?:do\s+not|don't|never)\s*$", re.IGNORECASE)
_INFORMATIONAL_HOW_TO = re.compile(
    r"\bhow\s+to\s+(?:write|build|implement|design)\b"
    r"|(?:如何|怎么|怎样)[^，。！？?\n]{0,48}(?:创建|编写|实现|设计|构建)",
    re.IGNORECASE,
)


class PlanFirstState(AgentState):
    incremental_execution: NotRequired[Annotated[dict[str, Any], PrivateStateAttr]]


class PlanFirstMiddleware(AgentMiddleware):
    """Mark qualifying runs for P9-enforced small-step execution."""

    state_schema = PlanFirstState

    def __init__(self, mode: str | None = None) -> None:
        super().__init__()
        # Lock the mode at construction time so a single run sees a
        # consistent decision — env reads at before_agent could race.
        resolved = (mode or os.environ.get("SHEJANE_PLAN_FIRST", "off")).lower().strip()
        if resolved in {"1", "true", "yes", "on", "always"}:
            self.mode = "always"
        elif resolved == "auto":
            self.mode = "auto"
        else:
            self.mode = "off"

    def before_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        if self.mode == "off":
            return None
        messages = list(state.get("messages") or [])
        if not messages:
            return None

        context = getattr(runtime, "context", None)
        policy = getattr(context, "execution_policy", None)
        frozen_complexity = policy.get("complexity") if isinstance(policy, dict) else None
        is_complex = (
            frozen_complexity == "complex"
            if frozen_complexity in {"simple", "complex"}
            else _looks_complex(messages)
        )
        if self.mode == "auto" and not is_complex:
            log.debug("plan-first auto skipped: task looks trivial")
            return None

        run_id = str(getattr(context, "run_id", None) or "")
        log.info("incremental execution required (mode=%s)", self.mode)
        return {
            "incremental_execution": {
                "required": True,
                "mode": self.mode,
                "run_id": run_id,
                "repairs": {},
            }
        }


def _looks_complex(messages: list[Any]) -> bool:
    """Cheap heuristic scoped to the current Runtime task input."""
    task_inputs = [
        message
        for message in messages
        if getattr(message, "type", None) == "human"
        and isinstance(getattr(message, "additional_kwargs", None), dict)
        and message.additional_kwargs.get("runtime_kind") == "task_input"
    ]
    current_user = (
        task_inputs[-1]
        if task_inputs
        else next(
            (
                message
                for message in reversed(messages)
                if getattr(message, "type", None) == "human"
            ),
            None,
        )
    )
    if current_user is None:
        return False
    text = getattr(current_user, "content", "")
    if not isinstance(text, str):
        return False
    if _is_explicit_one_tool_contract(text):
        return False
    if len(text) >= AUTO_COMPLEX_CHARS:
        return True
    lower = _INFORMATIONAL_HOW_TO.sub("", text.lower())
    return any(hint in lower for hint in COMPLEXITY_HINTS)


def _is_explicit_one_tool_contract(text: str) -> bool:
    """Recognize one-tool user contracts without interpreting them as projects.

    The final prose response is not a second task. Negative constraints such as
    ``do not call task.verify`` also do not add another requested tool.
    """
    if _EXPLICIT_SINGLE_STEP.search(text):
        return True
    requested: set[str] = set()
    for match in _EXPLICIT_TOOL_DIRECTIVE.finditer(text):
        prefix = text[max(0, match.start() - 16) : match.start()]
        if _NEGATED_DIRECTIVE.search(prefix):
            continue
        requested.add(match.group(1).lower())
    return len(requested) == 1
