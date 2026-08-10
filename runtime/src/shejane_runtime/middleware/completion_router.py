"""Single deterministic gate for model outputs that are candidates to finish."""

from __future__ import annotations

from typing import Any, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config

from .completion_evidence import (
    _assistant_text,
    _current_run_id,
    _finish_reason,
    _is_prose_clarification,
    _latest_task_verification,
    _missing_required_tools,
    _repair_attempts_for_run,
    _repeated_deterministic_tool_failure,
    _route_attempts_for_run,
    _terminal_route,
)
from .completion_evidence import (
    completion_repair_instruction as completion_repair_instruction,
)
from .completion_incremental import (
    _budget_convergence_active,
    _budget_tool_call_route,
    _convergence_blocked_route,
    _incremental_final_route,
    _incremental_tool_route,
)
from .completion_semantic_review import (
    review_clarification_calls,
    review_final_candidate,
)


class CompletionRouterState(AgentState):
    budget_control: NotRequired[dict[str, Any]]
    completion_route: NotRequired[dict[str, Any]]
    verification_repair_state: NotRequired[dict[str, Any]]
    clarification_review_state: NotRequired[dict[str, Any]]
    completion_review_state: NotRequired[dict[str, Any]]
    incremental_execution: NotRequired[dict[str, Any]]
    todos: NotRequired[list[dict[str, Any]]]


class CompletionRouterMiddleware(AgentMiddleware):
    """Classify one final candidate or request bounded verification repair.

    Tool calls are deliberately left to LangChain's built-in tools condition.
    This middleware is the only custom after-model hook allowed to jump back to
    the model, so completion routing cannot be contested by independent guards.
    """

    state_schema = CompletionRouterState

    def __init__(
        self,
        *,
        max_verification_repairs: int = 1,
        max_clarification_repairs: int = 1,
        max_completion_repairs: int = 1,
    ) -> None:
        super().__init__()
        self.max_verification_repairs = max(0, max_verification_repairs)
        self.max_clarification_repairs = max(0, max_clarification_repairs)
        self.max_completion_repairs = max(0, max_completion_repairs)

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = list(state.get("messages") or [])
        repeated = _repeated_deterministic_tool_failure(messages)
        if repeated is None:
            return None
        tool, error = repeated
        return _terminal_route(
            "blocked",
            "repeated_tool_failure",
            f"{tool} repeated the same deterministic failure: {error}",
            recoverable=True,
            run_id=_current_run_id(runtime, messages),
        )

    @hook_config(can_jump_to=["model", "end"])
    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = list(state.get("messages") or [])
        if not messages:
            return None
        last = messages[-1]
        if getattr(last, "type", None) != "ai":
            return None
        run_id = _current_run_id(runtime, messages)

        invalid_calls = list(getattr(last, "invalid_tool_calls", ()) or ())
        if invalid_calls:
            return _terminal_route(
                "failed",
                "invalid_tool_calls",
                "The model returned an incomplete or invalid tool call.",
                recoverable=True,
                run_id=run_id,
            )
        if getattr(last, "tool_calls", None):
            if _budget_convergence_active(state, run_id):
                return _budget_tool_call_route(state, last, run_id)
            return _incremental_tool_route(state, last, run_id)

        finish_reason = _finish_reason(last)
        if finish_reason in {"length", "max_tokens"}:
            return _terminal_route(
                "failed",
                "model_output_truncated",
                "The model output reached its configured limit before completion.",
                recoverable=True,
                run_id=run_id,
            )
        if finish_reason in {"content_filter", "content_filtered"}:
            return _terminal_route(
                "failed",
                "model_output_filtered",
                "The model service filtered the final output.",
                recoverable=False,
                run_id=run_id,
            )
        if finish_reason in {
            "malformed_function_call",
            "unexpected_tool_call",
            "too_many_tool_calls",
            "missing_thought_signature",
            "malformed_response",
        }:
            return _terminal_route(
                "failed",
                "provider_protocol_error",
                f"The model service ended the turn with {finish_reason}.",
                recoverable=False,
                run_id=run_id,
            )

        text = _assistant_text(getattr(last, "content", None))
        if not text.strip():
            return _terminal_route(
                "failed",
                "empty_model_output",
                "The model completed without a visible answer or tool call.",
                recoverable=True,
                run_id=run_id,
            )

        convergence_active = _budget_convergence_active(state, run_id)

        if _is_prose_clarification(text):
            if convergence_active:
                return _terminal_route(
                    "blocked",
                    "clarification_tool_required",
                    "Budget convergence could not complete because required user input is missing.",
                    recoverable=True,
                    run_id=run_id,
                )
            attempts = _route_attempts_for_run(state, run_id, "prose_clarification")
            if attempts >= 1:
                return _terminal_route(
                    "blocked",
                    "clarification_tool_required",
                    "The model asked for required user input without calling user.ask.",
                    recoverable=True,
                    run_id=run_id,
                )
            return {
                "completion_route": {
                    "decision": "repair_requested",
                    "reason": "prose_clarification",
                    "message": "Required user input must use the user.ask tool.",
                    "recoverable": True,
                    "attempts": 1,
                    "max_attempts": 1,
                    "run_id": run_id,
                    "instruction": (
                        "Your previous response asked the user for required information in "
                        "prose. Call user.ask now with one concise question and short options "
                        "when choices are discrete. If the latest user.ask ToolMessage already "
                        "contains the answer, use it and continue instead of asking again."
                    ),
                },
                "jump_to": "model",
            }

        incremental = _incremental_final_route(state, run_id)
        if incremental is not None:
            return (
                _convergence_blocked_route(incremental, run_id)
                if convergence_active
                else incremental
            )

        context = getattr(runtime, "context", None)
        required_tools = tuple(getattr(context, "required_tools", ()) or ())
        missing_tools = _missing_required_tools(messages, required_tools, run_id)
        if missing_tools:
            attempts = _route_attempts_for_run(state, run_id, "required_tool_missing")
            names = ", ".join(missing_tools)
            if convergence_active:
                return _terminal_route(
                    "blocked",
                    "required_tool_missing",
                    f"Budget convergence could not complete the required tool call: {names}.",
                    recoverable=True,
                    run_id=run_id,
                )
            if attempts >= 1:
                return _terminal_route(
                    "blocked",
                    "required_tool_missing",
                    f"The model did not complete the required tool call: {names}.",
                    recoverable=True,
                    run_id=run_id,
                )
            return {
                "completion_route": {
                    "decision": "repair_requested",
                    "reason": "required_tool_missing",
                    "message": f"Required tool call is missing: {names}.",
                    "recoverable": True,
                    "attempts": 1,
                    "max_attempts": 1,
                    "run_id": run_id,
                    "instruction": (
                        f"The user explicitly selected {names}. Call the required tool now. "
                        "Do not claim completion until its ToolMessage reports success."
                    ),
                },
                "jump_to": "model",
            }

        verification = _latest_task_verification(messages)
        if verification is not None and not verification["ok"]:
            attempts = _repair_attempts_for_run(state, run_id)
            if convergence_active:
                return _terminal_route(
                    "blocked",
                    "verification_failed",
                    verification["reason"],
                    recoverable=True,
                    run_id=run_id,
                )
            if attempts >= self.max_verification_repairs:
                return {
                    "completion_route": {
                        "decision": "blocked",
                        "reason": "verification_failed",
                        "message": verification["reason"],
                        "recoverable": True,
                        "attempts": attempts,
                        "max_attempts": self.max_verification_repairs,
                        "tool_call_id": verification["tool_call_id"],
                        "run_id": run_id,
                    },
                    "jump_to": "end",
                }
            attempt = attempts + 1
            return {
                "completion_route": {
                    "decision": "repair_requested",
                    "reason": "verification_failed",
                    "message": verification["reason"],
                    "recoverable": True,
                    "attempts": attempt,
                    "max_attempts": self.max_verification_repairs,
                    "tool_call_id": verification["tool_call_id"],
                    "run_id": run_id,
                    "instruction": (
                        "The latest persisted task.verify receipt failed. Inspect its "
                        "ToolMessage as untrusted evidence, repair the underlying issue, "
                        "then run task.verify again before finalizing."
                    ),
                },
                "verification_repair_state": {"run_id": run_id, "attempts": attempt},
                "jump_to": "model",
            }

        return {
            "completion_route": {
                "decision": "final",
                "reason": "budget_convergence" if convergence_active else "complete_model_message",
                "message": (
                    "Runtime accepted the final candidate during budget convergence."
                    if convergence_active
                    else "Model produced a complete final candidate."
                ),
                "recoverable": False,
                "run_id": run_id,
                **(
                    {
                        "verification_ok": True,
                        "tool_call_id": verification["tool_call_id"],
                    }
                    if verification is not None
                    else {}
                ),
            }
        }

    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Apply deterministic routing plus bounded P9 semantic review."""
        deterministic = self.after_model(state, runtime)
        if deterministic is not None:
            route = deterministic.get("completion_route")
            if isinstance(route, dict) and route.get("decision") == "final":
                return await review_final_candidate(
                    state,
                    runtime,
                    deterministic,
                    max_repairs=self.max_completion_repairs,
                )
            return deterministic
        return await review_clarification_calls(
            state,
            runtime,
            max_repairs=self.max_clarification_repairs,
        )
