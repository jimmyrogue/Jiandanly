"""P9 incremental-plan and budget-convergence acceptance routes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.messages import ToolMessage

from .completion_evidence import _int_state, _route_attempts_for_run, _terminal_route


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


def _budget_convergence_active(state: Any, run_id: str) -> bool:
    value = state.get("budget_control") if isinstance(state, Mapping) else None
    if not isinstance(value, dict) or value.get("mode") != "converge":
        return False
    scoped_run = str(value.get("run_id") or "")
    return not scoped_run or not run_id or scoped_run == run_id


def _budget_tool_call_route(state: Any, message: Any, run_id: str) -> dict[str, Any]:
    reason = "budget_convergence_tool_call"
    attempts = _route_attempts_for_run(state, run_id, reason)
    if attempts >= 1:
        return _terminal_route(
            "blocked",
            reason,
            "The model repeated a tool call after Runtime entered budget convergence.",
            recoverable=True,
            run_id=run_id,
        )
    calls = list(getattr(message, "tool_calls", ()) or ())
    return {
        "messages": [
            ToolMessage(
                content=(
                    "Runtime budget convergence is active. This tool was not executed. "
                    "Produce the best final answer from existing evidence."
                ),
                name=str(call.get("name") or "unknown"),
                tool_call_id=str(call.get("id") or ""),
                status="error",
            )
            for call in calls
            if isinstance(call, dict)
        ],
        "completion_route": {
            "decision": "repair_requested",
            "reason": reason,
            "message": "Tool calls are unavailable during budget convergence.",
            "recoverable": True,
            "attempts": 1,
            "max_attempts": 1,
            "run_id": run_id,
            "instruction": "Do not call tools. Produce the best available final answer now.",
        },
        "jump_to": "model",
    }


def _convergence_blocked_route(route: dict[str, Any], run_id: str) -> dict[str, Any]:
    completion = route.get("completion_route")
    details = completion if isinstance(completion, dict) else {}
    return _terminal_route(
        "blocked",
        str(details.get("reason") or "acceptance_incomplete"),
        str(details.get("message") or "Budget convergence could not satisfy completion checks."),
        recoverable=True,
        run_id=run_id,
    )


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
