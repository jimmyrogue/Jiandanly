"""Single deterministic gate for model outputs that are candidates to finish."""

from __future__ import annotations

import json
import re
from typing import Any, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain_core.messages import ToolMessage

from ..failure_policy import classify_failure_payload
from ..tool_outcomes import tool_result_envelope, tool_result_envelope_failed
from .clarification_reviewer import (
    ClarificationReviewUnavailable,
    review_clarification_batch,
)
from .completion_reviewer import (
    CompletionReviewUnavailable,
    review_completion_candidate,
)


class CompletionRouterState(AgentState):
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

        if _is_prose_clarification(text):
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
            return incremental

        context = getattr(runtime, "context", None)
        required_tools = tuple(getattr(context, "required_tools", ()) or ())
        missing_tools = _missing_required_tools(messages, required_tools, run_id)
        if missing_tools:
            attempts = _route_attempts_for_run(state, run_id, "required_tool_missing")
            names = ", ".join(missing_tools)
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
                "reason": "complete_model_message",
                "message": "Model produced a complete final candidate.",
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
                return await self._review_final_candidate(state, runtime, deterministic)
            return deterministic
        messages = list(state.get("messages") or [])
        if not messages:
            return None
        last = messages[-1]
        if getattr(last, "type", None) != "ai":
            return None
        ask_calls = [
            call
            for call in (getattr(last, "tool_calls", None) or [])
            if str(call.get("name") or "") == "user.ask"
        ]
        if not ask_calls:
            return None

        run_id = _current_run_id(runtime, messages)
        previous = state.get("clarification_review_state")
        review_state = previous if isinstance(previous, dict) else {}
        previous_decisions = (
            dict(review_state.get("decisions") or {})
            if review_state.get("run_id") == run_id
            else {}
        )
        call_ids = [str(call.get("id") or "") for call in ask_calls]
        if all(previous_decisions.get(call_id) == "allow" for call_id in call_ids):
            return None

        context = getattr(runtime, "context", None)
        questions = [
            {
                "tool_call_id": str(call.get("id") or ""),
                "question": str((call.get("args") or {}).get("question") or ""),
                "options": list((call.get("args") or {}).get("options") or []),
            }
            for call in ask_calls
        ]
        try:
            reviewed = await review_clarification_batch(
                model=getattr(context, "clarification_model", None),
                task_goal=str(getattr(context, "task_goal", None) or ""),
                messages=messages,
                questions=questions,
                runtime_facts={
                    "workspace_configured": bool(getattr(context, "workspace_root", None)),
                    "attachments": list(getattr(context, "attachments", ()) or ()),
                },
            )
            source = "llm"
        except ClarificationReviewUnavailable:
            # The question UI is the safe fallback. A reviewer outage must not
            # turn optional semantic checking into a new deadlock.
            reviewed = {
                call_id: {
                    "decision": "allow",
                    "reason": "Reviewer unavailable; the question UI remains available.",
                }
                for call_id in call_ids
            }
            source = "fallback"

        merged_decisions = {
            **previous_decisions,
            **{call_id: value["decision"] for call_id, value in reviewed.items()},
        }
        repairs = (
            int(review_state.get("repairs") or 0) if review_state.get("run_id") == run_id else 0
        )
        rejected = [call_id for call_id, value in reviewed.items() if value["decision"] == "repair"]
        if not rejected or repairs >= self.max_clarification_repairs:
            # Bounded repair: after the one corrective loop, fail open to the
            # visible question card instead of cycling invisibly forever.
            if rejected:
                merged_decisions.update({call_id: "allow" for call_id in rejected})
            return {
                "clarification_review_state": {
                    "run_id": run_id,
                    "decisions": merged_decisions,
                    "repairs": repairs,
                    "source": source,
                }
            }

        tool_messages: list[ToolMessage] = []
        for call in getattr(last, "tool_calls", None) or []:
            call_id = str(call.get("id") or "")
            if str(call.get("name") or "") == "user.ask":
                content = (
                    "Runtime P9 review found that this question is already answered by the "
                    "conversation or runtime evidence. Use that evidence and continue."
                )
            else:
                content = (
                    "Not executed because a sibling clarification was rejected. Reissue this "
                    "tool call only if it is still needed after using the existing evidence."
                )
            tool_messages.append(
                ToolMessage(
                    content=content,
                    name=str(call.get("name") or "unknown"),
                    tool_call_id=call_id,
                    status="error",
                )
            )
        return {
            "messages": tool_messages,
            "completion_route": {
                "decision": "repair_requested",
                "reason": "unnecessary_clarification",
                "message": "The proposed question is already answered by available evidence.",
                "recoverable": True,
                "attempts": repairs + 1,
                "max_attempts": self.max_clarification_repairs,
                "run_id": run_id,
                "instruction": (
                    "Use the existing conversation evidence to continue the current task. "
                    "Do not ask the rejected question again. If a different fact is genuinely "
                    "blocking, call user.ask with only that missing fact."
                ),
            },
            "clarification_review_state": {
                "run_id": run_id,
                "decisions": merged_decisions,
                "repairs": repairs + 1,
                "source": source,
                "reasons": {call_id: reviewed[call_id]["reason"] for call_id in rejected},
            },
            "jump_to": "model",
        }

    async def _review_final_candidate(
        self,
        state: Any,
        runtime: Any,
        deterministic: dict[str, Any],
    ) -> dict[str, Any]:
        messages = list(state.get("messages") or [])
        run_id = _current_run_id(runtime, messages)
        if _latest_memory_write_failed(messages, run_id):
            context = getattr(runtime, "context", None)
            task_goal = str(getattr(context, "task_goal", None) or "")
            final_candidate = _assistant_text(getattr(messages[-1], "content", None))
            if re.search(r"[\u3400-\u9fff]", task_goal + final_candidate):
                corrected = "这次没有保存到长期记忆。请明确告诉我要记录的完整内容。"
            else:
                corrected = (
                    "This was not saved to long-term memory. "
                    "Please tell me the complete fact you want me to record."
                )
            return {
                **deterministic,
                "messages": [messages[-1].model_copy(update={"content": corrected})],
                "completion_review_state": {
                    "run_id": run_id,
                    "attempts": 0,
                    "decision": "allow",
                    "source": "memory_write_receipt",
                },
            }
        if not _has_current_tool_evidence(messages, run_id):
            return deterministic

        context = getattr(runtime, "context", None)
        try:
            reviewed = await review_completion_candidate(
                model=getattr(context, "completion_model", None),
                task_goal=str(getattr(context, "task_goal", None) or ""),
                messages=messages,
                final_candidate=_assistant_text(getattr(messages[-1], "content", None)),
            )
            source = "llm"
        except CompletionReviewUnavailable:
            # Semantic review is defense in depth. Provider or parser failure
            # must not deadlock an otherwise deterministically valid run.
            return {
                **deterministic,
                "completion_review_state": {
                    "run_id": run_id,
                    "attempts": 0,
                    "decision": "allow",
                    "source": "fallback",
                },
            }

        previous = state.get("completion_review_state")
        review_state = previous if isinstance(previous, dict) else {}
        attempts = (
            int(review_state.get("attempts") or 0) if review_state.get("run_id") == run_id else 0
        )
        if reviewed["decision"] == "allow":
            return {
                **deterministic,
                "completion_review_state": {
                    "run_id": run_id,
                    "attempts": attempts,
                    "decision": "allow",
                    "source": source,
                    "reason": reviewed["reason"],
                },
            }

        if attempts >= self.max_completion_repairs:
            blocked = _terminal_route(
                "blocked",
                "completion_review_failed",
                "The final answer still omitted or contradicted required task evidence.",
                recoverable=True,
                run_id=run_id,
            )
            blocked["completion_review_state"] = {
                "run_id": run_id,
                "attempts": attempts,
                "decision": "repair",
                "source": source,
                "reason": reviewed["reason"],
            }
            return blocked

        attempt = attempts + 1
        return {
            "completion_route": {
                "decision": "repair_requested",
                "reason": "completion_review_failed",
                "message": "The final candidate did not preserve required task evidence.",
                "recoverable": True,
                "attempts": attempt,
                "max_attempts": self.max_completion_repairs,
                "run_id": run_id,
                "instruction": (
                    "Re-read the current task goal and the latest completed ToolMessages. "
                    "Produce one corrected final answer that includes every explicitly "
                    "requested result, exact value, and selected user answer. Do not repeat "
                    "successful tools unless required evidence is genuinely absent."
                ),
            },
            "completion_review_state": {
                "run_id": run_id,
                "attempts": attempt,
                "decision": "repair",
                "source": source,
                "reason": reviewed["reason"],
            },
            "jump_to": "model",
        }


def _incremental_tool_route(state: Any, last: Any, run_id: str) -> dict[str, Any] | None:
    config = _incremental_config(state, run_id)
    if config is None:
        return None
    calls = list(getattr(last, "tool_calls", None) or [])
    if not calls:
        return None
    names = [str(call.get("name") or "") for call in calls]
    current_todos = _todo_items(state.get("todos") if isinstance(state, dict) else None)

    # Missing information may be gathered before planning. It is still
    # reviewed by the clarification gate below. Everything else must begin
    # with one isolated, valid write_todos transition.
    if not current_todos:
        if names and all(name == "user.ask" for name in names):
            return None
        if len(calls) != 1 or names != ["write_todos"]:
            return _incremental_repair_route(
                state,
                config,
                run_id=run_id,
                reason="incremental_plan_required",
                message="Complex tool work must start with a small executable plan.",
                instruction=(
                    "Call write_todos before any work tool. Use 2 to 8 independently "
                    "verifiable tasks and mark exactly one task in_progress."
                ),
                calls=calls,
            )
        proposed = _todo_items((calls[0].get("args") or {}).get("todos"))
        error = _todo_plan_error(proposed, mode=str(config.get("mode") or "auto"), initial=True)
        if error is not None:
            return _incremental_repair_route(
                state,
                config,
                run_id=run_id,
                reason="incremental_plan_invalid",
                message=error,
                instruction=(
                    "Rewrite the todo list as 2 to 8 small, independently verifiable tasks. "
                    "Mark exactly one task in_progress and the rest pending."
                ),
                calls=calls,
            )
        return None

    if "write_todos" in names:
        if len(calls) != 1 or names != ["write_todos"]:
            return _incremental_repair_route(
                state,
                config,
                run_id=run_id,
                reason="incremental_plan_invalid",
                message="A todo transition must be the only tool call in its model round.",
                instruction="Call write_todos alone, then continue work in the next model round.",
                calls=calls,
            )
        proposed = _todo_items((calls[0].get("args") or {}).get("todos"))
        error = _todo_plan_error(proposed, mode=str(config.get("mode") or "auto"), initial=False)
        if error is None:
            error = _todo_transition_error(current_todos, proposed)
        if error is not None:
            return _incremental_repair_route(
                state,
                config,
                run_id=run_id,
                reason="incremental_plan_invalid",
                message=error,
                instruction=(
                    "Preserve completed tasks and keep exactly one task in_progress until all "
                    "tasks finish."
                ),
                calls=calls,
            )
        return None

    status_error = _todo_plan_error(
        current_todos,
        mode=str(config.get("mode") or "auto"),
        initial=False,
    )
    if status_error is not None:
        return _incremental_repair_route(
            state,
            config,
            run_id=run_id,
            reason="incremental_plan_invalid",
            message=status_error,
            instruction="Repair the todo state with write_todos before calling another work tool.",
            calls=calls,
        )
    if all(item["status"] == "completed" for item in current_todos) and not all(
        name == "user.ask" for name in names
    ):
        return _incremental_repair_route(
            state,
            config,
            run_id=run_id,
            reason="incremental_plan_stale",
            message="The completed plan does not cover the newly proposed work.",
            instruction="Add the newly discovered work as a small in_progress todo before doing it.",
            calls=calls,
        )
    return None


def _incremental_final_route(state: Any, run_id: str) -> dict[str, Any] | None:
    config = _incremental_config(state, run_id)
    if config is None:
        return None
    todos = _todo_items(state.get("todos") if isinstance(state, dict) else None)
    if not todos:
        return _incremental_repair_route(
            state,
            config,
            run_id=run_id,
            reason="incremental_plan_required",
            message="A complex task cannot finalize before its small-task plan exists.",
            instruction=(
                "Call write_todos with 2 to 8 independently verifiable tasks, mark exactly "
                "one in_progress, and execute them before finalizing."
            ),
        )
    if not all(item["status"] == "completed" for item in todos):
        return _incremental_repair_route(
            state,
            config,
            run_id=run_id,
            reason="incremental_plan_incomplete",
            message="The model tried to finalize while planned tasks remain unfinished.",
            instruction=(
                "Continue the single in_progress task. After obtaining evidence, update "
                "write_todos, advance one task, and finalize only when every task is completed."
            ),
        )
    return None


def _incremental_config(state: Any, run_id: str) -> dict[str, Any] | None:
    value = state.get("incremental_execution") if isinstance(state, dict) else None
    if not isinstance(value, dict) or value.get("required") is not True:
        return None
    scoped_run = str(value.get("run_id") or "")
    if scoped_run and run_id and scoped_run != run_id:
        return None
    return value


def _todo_items(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    items: list[dict[str, str]] = []
    for raw in value:
        if not isinstance(raw, dict):
            return []
        content = " ".join(str(raw.get("content") or "").split())
        status = str(raw.get("status") or "")
        if not content or status not in {"pending", "in_progress", "completed"}:
            return []
        items.append({"content": content, "status": status})
    return items


def _todo_plan_error(
    todos: list[dict[str, str]],
    *,
    mode: str,
    initial: bool,
) -> str | None:
    minimum = 1 if mode == "always" else 2
    if len(todos) < minimum or len(todos) > 8:
        return f"Incremental execution requires {minimum} to 8 small tasks."
    active = sum(item["status"] == "in_progress" for item in todos)
    completed = sum(item["status"] == "completed" for item in todos)
    if initial and completed:
        return "A new plan cannot claim tasks were completed before execution."
    if completed == len(todos):
        return None
    if active != 1:
        return "Incremental execution requires exactly one in_progress task."
    return None


def _todo_transition_error(
    previous: list[dict[str, str]],
    proposed: list[dict[str, str]],
) -> str | None:
    prior_completed = {item["content"] for item in previous if item["status"] == "completed"}
    next_completed = {item["content"] for item in proposed if item["status"] == "completed"}
    if not prior_completed.issubset(next_completed):
        return "Previously completed tasks cannot be removed or reopened."
    return None


def _incremental_repair_route(
    state: Any,
    config: dict[str, Any],
    *,
    run_id: str,
    reason: str,
    message: str,
    instruction: str,
    calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    repairs = dict(config.get("repairs") or {})
    attempts = _int_state(repairs.get(reason))
    if attempts >= 1:
        return _terminal_route(
            "blocked",
            reason,
            message,
            recoverable=True,
            run_id=run_id,
        )
    repairs[reason] = attempts + 1
    update: dict[str, Any] = {
        "completion_route": {
            "decision": "repair_requested",
            "reason": reason,
            "message": message,
            "recoverable": True,
            "attempts": attempts + 1,
            "max_attempts": 1,
            "run_id": run_id,
            "instruction": instruction,
        },
        "incremental_execution": {**config, "run_id": run_id, "repairs": repairs},
        "jump_to": "model",
    }
    if calls:
        update["messages"] = [
            ToolMessage(
                content=(
                    "Runtime P9 did not execute this call because the incremental task state "
                    "must be repaired first. Follow the runtime repair instruction."
                ),
                name=str(call.get("name") or "unknown"),
                tool_call_id=str(call.get("id") or ""),
                status="error",
            )
            for call in calls
        ]
    return update


def completion_repair_instruction(state: Any, *, run_id: str | None = None) -> str | None:
    if not isinstance(state, dict):
        return None
    route = state.get("completion_route")
    if not isinstance(route, dict) or route.get("decision") != "repair_requested":
        return None
    if run_id is not None and route.get("run_id") != run_id:
        return None
    value = route.get("instruction")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _terminal_route(
    decision: str,
    reason: str,
    message: str,
    *,
    recoverable: bool,
    run_id: str,
) -> dict[str, Any]:
    return {
        "completion_route": {
            "decision": decision,
            "reason": reason,
            "message": message,
            "recoverable": recoverable,
            "run_id": run_id,
        },
        "jump_to": "end",
    }


def _assistant_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            value = block.get("text")
            if isinstance(value, str):
                parts.append(value)
    return "".join(parts)


def _repeated_deterministic_tool_failure(messages: list[Any]) -> tuple[str, str] | None:
    latest: tuple[str, str] | None = None
    for message in reversed(messages):
        message_type = getattr(message, "type", None)
        if message_type == "human":
            return None
        if message_type != "tool":
            continue
        if str(getattr(message, "status", "") or "").lower() != "error":
            return None
        tool = str(getattr(message, "name", "") or "unknown")
        error = " ".join(str(getattr(message, "content", "") or "").split())
        classification = classify_failure_payload(
            "tool.failed",
            {"tool": tool, "content": error, "retryable": False},
        )
        if classification["category"] not in {
            "auth",
            "configuration",
            "fatal",
            "permission",
            "quota",
            "validation",
            "workspace",
        }:
            return None
        signature = (tool, error)
        if latest is None:
            latest = signature
            continue
        return signature if signature == latest else None
    return None


_PROSE_CLARIFICATION = re.compile(
    r"(?:你|您)(?:指的是|希望(?:按|用|选择|采用)|想(?:要|选择|使用)|需要(?:提供|选择|确认)|偏好)"
    r"|请(?:提供|告诉|选择|确认|说明|指定|补充)"
    r"|\b(?:which|what|how|where|when|would)\b.{0,80}\b(?:you|your)\b"
    r"|\bplease\s+(?:provide|choose|confirm|specify|tell)\b",
    re.IGNORECASE,
)


def _is_prose_clarification(text: str) -> bool:
    value = " ".join(text.split())
    return ("?" in value or "？" in value) and _PROSE_CLARIFICATION.search(value) is not None


def _finish_reason(message: Any) -> str:
    metadata = getattr(message, "response_metadata", None)
    if not isinstance(metadata, dict):
        return ""
    value = metadata.get("finish_reason") or metadata.get("stop_reason")
    return str(value or "").strip().lower()


def _latest_task_verification(messages: list[Any]) -> dict[str, Any] | None:
    # A graph fork may inherit old ToolMessages. Verification belongs to the
    # current user turn only; otherwise a failed check from an ancestor Run can
    # permanently block an unrelated follow-up.
    explicit_turn_start = [
        index
        for index, message in enumerate(messages)
        if getattr(message, "type", None) == "human"
        and isinstance(getattr(message, "additional_kwargs", None), dict)
        and message.additional_kwargs.get("runtime_kind") == "task_input"
    ]
    fallback_turn_start = max(
        (
            index
            for index, message in enumerate(messages)
            if getattr(message, "type", None) == "human"
            and not (
                isinstance(getattr(message, "additional_kwargs", None), dict)
                and message.additional_kwargs.get("runtime_kind") == "steering"
            )
        ),
        default=0,
    )
    turn_start = explicit_turn_start[-1] if explicit_turn_start else fallback_turn_start
    scoped = messages[turn_start:]
    for reverse_index, message in enumerate(reversed(scoped)):
        if getattr(message, "type", None) != "tool":
            continue
        if getattr(message, "name", "") != "task.verify":
            continue
        payload = _parse_tool_content(getattr(message, "content", ""))
        if not isinstance(payload, dict):
            return {
                "ok": False,
                "reason": "task.verify returned an unreadable result",
                "tool_call_id": str(getattr(message, "tool_call_id", "")),
            }
        ok = _truthy(payload.get("ok"))
        verification_index = len(scoped) - reverse_index - 1
        if ok:
            stale_tool = _verification_invalidating_tool(scoped[verification_index + 1 :])
            if stale_tool is not None:
                return {
                    "ok": False,
                    "reason": f"verification became stale after tool {stale_tool}",
                    "tool_call_id": str(getattr(message, "tool_call_id", "")),
                }
        return {
            "ok": ok,
            "reason": "verification passed" if ok else _verification_reason(payload),
            "tool_call_id": str(getattr(message, "tool_call_id", "")),
        }
    return None


_VERIFICATION_PRESERVING_TOOLS = {
    "clipboard.read",
    "environment.observe",
    "glob",
    "grep",
    "ls",
    "memory.search",
    "office.outline",
    "office.read",
    "office.read_range",
    "office.read_slides",
    "open.file",
    "open.url",
    "pdf.inspect",
    "read_file",
    "task.progress",
    "time.now",
    "web.fetch",
    "web.search",
}


def _verification_invalidating_tool(messages: list[Any]) -> str | None:
    for message in messages:
        if getattr(message, "type", None) != "tool":
            continue
        name = str(getattr(message, "name", "") or "unknown")
        status = str(getattr(message, "status", "") or "").lower()
        if status == "error" or name not in _VERIFICATION_PRESERVING_TOOLS:
            return name
    return None


def _has_current_tool_evidence(messages: list[Any], run_id: str) -> bool:
    """Limit semantic review to tool evidence produced for the current task."""
    start = 0
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        kwargs = getattr(message, "additional_kwargs", None)
        if not isinstance(kwargs, dict) or kwargs.get("runtime_kind") != "task_input":
            continue
        message_run_id = str(kwargs.get("runtime_run_id") or "")
        if not run_id or not message_run_id or message_run_id == run_id:
            start = index
            break
    return any(getattr(message, "type", None) == "tool" for message in messages[start:-1])


def _latest_memory_write_failed(messages: list[Any], run_id: str) -> bool:
    """Trust the latest memory.write receipt, never the model's success claim."""
    start = 0
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        kwargs = getattr(message, "additional_kwargs", None)
        if not isinstance(kwargs, dict) or kwargs.get("runtime_kind") != "task_input":
            continue
        message_run_id = str(kwargs.get("runtime_run_id") or "")
        if not run_id or not message_run_id or message_run_id == run_id:
            start = index
            break
    for message in reversed(messages[start:-1]):
        if getattr(message, "type", None) != "tool":
            continue
        if str(getattr(message, "name", "") or "") != "memory.write":
            continue
        status = str(getattr(message, "status", "") or "").lower()
        envelope = tool_result_envelope(getattr(message, "content", None))
        return status == "error" or tool_result_envelope_failed(envelope)
    return False


def _current_run_id(runtime: Any, messages: list[Any]) -> str:
    context = getattr(runtime, "context", None)
    value = getattr(context, "run_id", None)
    if isinstance(value, str) and value:
        return value
    for message in reversed(messages):
        kwargs = getattr(message, "additional_kwargs", None)
        if isinstance(kwargs, dict) and kwargs.get("runtime_kind") == "task_input":
            candidate = kwargs.get("runtime_run_id")
            if isinstance(candidate, str):
                return candidate
    return ""


def _repair_attempts_for_run(state: Any, run_id: str) -> int:
    repair_state = state.get("verification_repair_state") if isinstance(state, dict) else None
    if not isinstance(repair_state, dict) or repair_state.get("run_id") != run_id:
        return 0
    return _int_state(repair_state.get("attempts"))


def _route_attempts_for_run(state: Any, run_id: str, reason: str) -> int:
    route = state.get("completion_route") if isinstance(state, dict) else None
    if not isinstance(route, dict):
        return 0
    if route.get("run_id") != run_id or route.get("reason") != reason:
        return 0
    return _int_state(route.get("attempts"))


def _missing_required_tools(
    messages: list[Any],
    required_tools: tuple[str, ...],
    run_id: str,
) -> list[str]:
    if not required_tools:
        return []
    turn_messages = messages
    for index in range(len(messages) - 1, -1, -1):
        kwargs = getattr(messages[index], "additional_kwargs", None)
        if (
            isinstance(kwargs, dict)
            and kwargs.get("runtime_kind") == "task_input"
            and (not run_id or kwargs.get("runtime_run_id") == run_id)
        ):
            turn_messages = messages[index + 1 :]
            break
    completed: set[str] = set()
    for message in turn_messages:
        if not isinstance(message, ToolMessage):
            continue
        name = str(getattr(message, "name", "") or "")
        # A denied or failed paid image request still satisfies the requirement
        # to attempt the selected tool. Other completion checks prevent a false
        # success claim without automatically repeating a paid side effect.
        if name in required_tools:
            completed.add(name)
    return [name for name in required_tools if name not in completed]


def _parse_tool_content(content: Any) -> Any:
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return None
    return None


def _verification_reason(payload: dict[str, Any]) -> str:
    results = payload.get("results")
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict) or _truthy(item.get("ok")):
                continue
            detail = item.get("detail")
            if isinstance(detail, str) and detail.strip():
                return detail.strip()
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        return error.strip()
    return "task.verify returned ok=false"


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "ok", "passed"}
    if isinstance(value, (int, float)):
        return value != 0
    return bool(value)


def _int_state(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
