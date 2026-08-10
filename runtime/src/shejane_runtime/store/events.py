"""Event persistence constants shared across Runtime store boundaries."""

from __future__ import annotations

TRANSIENT_RUN_EVENT_TYPES = frozenset(
    {
        "llm.delta",
        "llm.round.closed",
        "llm.round.started",
        "llm.reasoning",
        "llm.usage",
        "llm.tool_call_chunk",
        "tool.progress",
    }
)

TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "canceled"})
