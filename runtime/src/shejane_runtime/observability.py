"""Observability layer — structlog + LangChain callback handler.

Two-part design:

1. `configure_logging()` sets up structlog once at runtime boot. All module
   logs (loggers obtained via `structlog.get_logger(__name__)`) flow
   through the same JSON/console renderer.

2. `RuntimeObserver(AsyncCallbackHandler)` hooks into LangChain's callback
   system. Every chat-model call, tool call, chain step, retriever call,
   and error gets a structured log line tagged with `run_id` + `module`
   for cross-correlation with the runtime's other logs.

`SHEJANE_DISABLE_OBSERVABILITY=1` turns the whole layer into no-ops —
  useful for benchmarking the cold-path overhead.

The handler is intentionally metadata-only. External tracing inherited from a
developer shell is disabled; centralized diagnostics use SheJane's explicit,
redacted relay instead of vendor callbacks that can capture prompt or tool
content.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any
from uuid import UUID

import structlog
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult

_configured = False


def configure_logging(*, json_output: bool | None = None) -> None:
    """Idempotent structlog setup. Safe to call multiple times."""
    global _configured
    if _configured:
        return
    _configured = True

    if json_output is None:
        # Default to JSON in production-ish runs (no TTY), pretty when interactive.
        json_output = not sys.stderr.isatty()

    timestamper = structlog.processors.TimeStamper(fmt="iso")
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if json_output:
        renderer: Any = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.UnicodeDecoder(),
            renderer,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Plain stdlib logging stays plain — anything calling `logging.info(...)`
    # gets a simple one-line stderr render. The full structlog renderer
    # only applies to code that calls `structlog.get_logger(...)`. This
    # avoids double-JSON-encoding when LangChain's own modules emit logs.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )


def is_disabled() -> bool:
    return os.environ.get("SHEJANE_DISABLE_OBSERVABILITY", "").lower() in {
        "1",
        "true",
        "yes",
    }


def build_callbacks() -> list[AsyncCallbackHandler]:
    """Construct the metadata-only callback list used at agent invocation time."""
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ.pop("LANGSMITH_API_KEY", None)
    os.environ.pop("LANGCHAIN_API_KEY", None)
    if is_disabled():
        return []
    return [RuntimeObserver()]


class RuntimeObserver(AsyncCallbackHandler):
    """Emit one structured log line per LangChain lifecycle event.

    We only hook the events that move the run forward — chat model
    start/end/error, tool start/end/error, agent action/finish. The rest
    (on_text, on_retriever_*, on_retry, on_custom_event) we leave to
    LangChain's default no-op so a noisy retriever doesn't drown out the
    main timeline.
    """

    def __init__(self) -> None:
        super().__init__()
        self._log = structlog.get_logger("agent")
        self._timers: dict[UUID, float] = {}

    # --- chat model lifecycle ---

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **_: Any,
    ) -> None:
        self._timers[run_id] = time.perf_counter()
        self._log.info(
            "llm.start",
            run_id=str(run_id),
            parent_run_id=str(parent_run_id) if parent_run_id else None,
            model_name=_extract_model_name(serialized),
            message_count=sum(len(m) if isinstance(m, list) else 1 for m in messages),
            tags=tags,
        )

    async def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **_: Any,
    ) -> None:
        started = self._timers.pop(run_id, None)
        elapsed_ms = (time.perf_counter() - started) * 1000 if started else None
        usage: dict[str, Any] = {}
        if response.llm_output:
            usage = response.llm_output.get("token_usage", {}) or response.llm_output.get(
                "usage", {}
            )
        self._log.info(
            "llm.end",
            run_id=str(run_id),
            parent_run_id=str(parent_run_id) if parent_run_id else None,
            elapsed_ms=round(elapsed_ms, 2) if elapsed_ms is not None else None,
            input_tokens=usage.get("input_tokens") or usage.get("prompt_tokens"),
            output_tokens=usage.get("output_tokens") or usage.get("completion_tokens"),
        )

    async def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **_: Any,
    ) -> None:
        self._timers.pop(run_id, None)
        self._log.warning(
            "llm.error",
            run_id=str(run_id),
            parent_run_id=str(parent_run_id) if parent_run_id else None,
            error_type=type(error).__name__,
            error_message=str(error),
        )

    # --- tool lifecycle ---

    async def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **_: Any,
    ) -> None:
        self._timers[run_id] = time.perf_counter()
        self._log.info(
            "tool.start",
            run_id=str(run_id),
            parent_run_id=str(parent_run_id) if parent_run_id else None,
            tool=serialized.get("name", "unknown"),
        )

    async def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **_: Any,
    ) -> None:
        started = self._timers.pop(run_id, None)
        elapsed_ms = (time.perf_counter() - started) * 1000 if started else None
        self._log.info(
            "tool.end",
            run_id=str(run_id),
            parent_run_id=str(parent_run_id) if parent_run_id else None,
            elapsed_ms=round(elapsed_ms, 2) if elapsed_ms is not None else None,
        )

    async def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **_: Any,
    ) -> None:
        self._timers.pop(run_id, None)
        self._log.warning(
            "tool.error",
            run_id=str(run_id),
            parent_run_id=str(parent_run_id) if parent_run_id else None,
            error_type=type(error).__name__,
        )

    # --- agent lifecycle (high level signal only) ---

    async def on_agent_action(self, action: Any, *, run_id: UUID, **_: Any) -> None:
        self._log.debug(
            "agent.action",
            run_id=str(run_id),
            tool=getattr(action, "tool", None),
        )

    async def on_agent_finish(self, finish: Any, *, run_id: UUID, **_: Any) -> None:
        self._log.info(
            "agent.finish",
            run_id=str(run_id),
        )


# ---- helpers ----


def _extract_model_name(serialized: dict[str, Any]) -> str:
    if "name" in serialized:
        return str(serialized["name"])
    if "id" in serialized and isinstance(serialized["id"], list):
        return ".".join(str(x) for x in serialized["id"])
    return "unknown"
