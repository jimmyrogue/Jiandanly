"""Dynamic soft-budget guidance and deterministic loop convergence."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Annotated, Any, NotRequired, cast

from langchain.agents.middleware import AgentMiddleware, AgentState, ToolCallRequest
from langchain.agents.middleware.types import PrivateStateAttr
from langchain_core.messages import SystemMessage, ToolMessage
from langgraph.types import Command

from ..agent.context_builder import RuntimeContext
from ..config import MAX_CONCURRENT_SUBAGENT_TASKS
from ..llm.ledger import MODEL_RETRY_ATTEMPTS
from .tool_execution_identity import READ_ONLY_TOOLS

_RECENT_TOOL_CALL_WINDOW = 12
_SYNCHRONOUS_DELEGATION_TOOLS = frozenset({"task", "team.run"})


def finalization_attempt_reserve(hard_limit: int, final_turns: int) -> int:
    """Reserve retry-safe provider attempts without suppressing all useful work."""
    hard = max(1, int(hard_limit))
    requested = max(1, int(final_turns)) * (MODEL_RETRY_ATTEMPTS + 1)
    return min(requested, hard if hard == 1 else hard - 1)


class DynamicBudgetControlState(AgentState):
    budget_control: NotRequired[Annotated[dict[str, Any], PrivateStateAttr]]


class DynamicBudgetControlMiddleware(AgentMiddleware):
    """Warn past a soft budget and reserve the final calls for synthesis."""

    state_schema = DynamicBudgetControlState

    def __init__(self, *, convergence_lead: int = 0) -> None:
        super().__init__()
        self.convergence_lead = max(0, int(convergence_lead))

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any]:
        return {
            "budget_control": await _budget_decision(
                state,
                runtime,
                convergence_lead=self.convergence_lead,
            )
        }

    async def awrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        state = getattr(request, "state", None)
        control = state.get("budget_control") if isinstance(state, Mapping) else None
        if not isinstance(control, dict):
            control = await _budget_decision(
                state if isinstance(state, Mapping) else {"messages": request.messages},
                request.runtime,
                convergence_lead=self.convergence_lead,
            )
        controlled = _controlled_request(request, control)
        return await handler(controlled)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        blocked = _blocked_tool_result(request)
        return blocked if blocked is not None else handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        blocked = _blocked_tool_result(request)
        return blocked if blocked is not None else await handler(request)


async def _budget_decision(
    state: Any,
    runtime: Any,
    *,
    convergence_lead: int = 0,
) -> dict[str, Any]:
    context = getattr(runtime, "context", None)
    if not isinstance(context, RuntimeContext) or not context.run_id:
        return {"mode": "normal"}
    store = context.store
    status_reader = getattr(store, "model_call_budget_status", None)
    if not callable(status_reader):
        return {"mode": "normal"}

    read_status = cast(
        Callable[..., Awaitable[dict[str, int]]],
        status_reader,
    )
    status = await read_status(context.run_id, purpose="agent")
    used = max(0, int(status.get("model_calls", 0)))
    hard_limit = max(1, int(context.model_call_hard_limit or 1))
    soft_limit = max(1, min(hard_limit, int(context.model_call_soft_limit or hard_limit)))
    reserve = max(1, min(hard_limit, int(context.model_call_final_reserve or 1)))
    remaining = max(0, hard_limit - used)
    repeated = _repeated_read_tool(state.get("messages", ()) if isinstance(state, Mapping) else ())
    reserved_attempts = finalization_attempt_reserve(hard_limit, reserve)
    policy_limit = context.execution_policy.get("max_concurrent_subagent_tasks")
    max_concurrent_subagent_tasks = (
        max(0, policy_limit)
        if isinstance(policy_limit, int) and not isinstance(policy_limit, bool)
        else MAX_CONCURRENT_SUBAGENT_TASKS
        if context.subagents_enabled
        else 0
    )
    force_threshold = max(0, hard_limit - reserved_attempts - convergence_lead)
    force_final = used >= force_threshold or (repeated is not None and repeated[1] >= 3)
    soft_exhausted = used >= soft_limit
    delegation_closed = soft_exhausted or used >= max(0, force_threshold - 1)
    warn = (
        force_final
        or delegation_closed
        or soft_exhausted
        or (repeated is not None and repeated[1] >= 2)
    )
    control = {
        "mode": (
            "converge"
            if force_final
            else "delegate_closed"
            if delegation_closed
            else "warn"
            if warn
            else "normal"
        ),
        "run_id": context.run_id,
        "used": used,
        "remaining": remaining,
        "soft_limit": soft_limit,
        "hard_limit": hard_limit,
        "delegation_model_call_limit": hard_limit - reserved_attempts,
        "max_concurrent_subagent_tasks": max_concurrent_subagent_tasks,
        "soft_exhausted": soft_exhausted,
        "repeated_tool": repeated[0] if repeated is not None else None,
        "repeat_count": repeated[1] if repeated is not None else 0,
    }
    return control


def _controlled_request(request: Any, control: dict[str, Any]) -> Any:
    mode = str(control.get("mode") or "normal")
    if mode == "normal":
        return request
    remaining = max(0, int(control.get("remaining") or 0))
    repeated_tool = control.get("repeated_tool")
    repeat_count = max(0, int(control.get("repeat_count") or 0))

    if mode == "converge":
        repeat_note = (
            f" The read-only tool {repeated_tool} was requested {repeat_count} times "
            "with the same arguments in the recent action window."
            if repeated_tool
            else ""
        )
        guidance = (
            "Runtime convergence mode is active. "
            f"{remaining} model calls remain before the hard limit.{repeat_note} "
            "Tools are unavailable for this call. Produce the best available final answer "
            "from evidence already collected, state material limitations, and do not ask for "
            "more research or repeat earlier actions."
        )
    elif mode == "delegate_closed":
        budget_note = (
            f"The task has crossed its {int(control.get('soft_limit') or 0)}-call soft budget. "
            if control.get("soft_exhausted") is True
            else ""
        )
        guidance = (
            f"{budget_note}{remaining} model calls remain before the hard limit. "
            "Synchronous delegation is unavailable because it would consume "
            "the final answer reserve. Continue directly with available tools or synthesize "
            "the best answer from existing evidence."
        )
    else:
        repeat_note = (
            f"The read-only tool {repeated_tool} has repeated with identical arguments; "
            "do not call it again unless its underlying data changed."
            if repeated_tool
            else ""
        )
        budget_note = (
            f"The task has crossed its {int(control.get('soft_limit') or 0)}-call soft budget; "
            f"{remaining} model calls remain before the hard limit. "
            if control.get("soft_exhausted") is True
            else ""
        )
        guidance = (
            f"{budget_note}{repeat_note} Prefer synthesis over broad new exploration, and "
            "finish now when the collected evidence is sufficient."
        ).strip()

    system_message = request.system_message
    controlled = request.override(
        system_message=SystemMessage(
            content=[
                *system_message.content_blocks,
                {
                    "type": "text",
                    "text": f"<runtime-budget>\n{guidance}\n</runtime-budget>",
                },
            ]
        )
    )
    if mode == "converge":
        return controlled.override(tools=[])
    if mode == "delegate_closed":
        return controlled.override(
            tools=[
                tool
                for tool in request.tools
                if _request_tool_name(tool) not in _SYNCHRONOUS_DELEGATION_TOOLS
            ]
        )
    return controlled


def _blocked_tool_result(request: Any) -> ToolMessage | None:
    state = getattr(request, "state", None)
    control = state.get("budget_control") if isinstance(state, Mapping) else None
    if not isinstance(control, dict):
        return None
    mode = str(control.get("mode") or "normal")
    tool_name = str(request.tool_call.get("name") or "")
    capacity_exhausted = False
    parallel_capacity_exhausted = False
    if tool_name in _SYNCHRONOUS_DELEGATION_TOOLS and "delegation_model_call_limit" in control:
        used = max(0, int(control.get("used") or 0))
        delegation_limit = max(0, int(control.get("delegation_model_call_limit") or 0))
        available = max(0, delegation_limit - used - 1)
        _, member_end = _synchronous_member_span(cast(Mapping[str, Any], state), request.tool_call)
        capacity_exhausted = member_end > available
        max_concurrent = control.get("max_concurrent_subagent_tasks")
        parallel_capacity_exhausted = (
            isinstance(max_concurrent, int)
            and not isinstance(max_concurrent, bool)
            and member_end > max(0, max_concurrent)
        )
    if (
        mode != "converge"
        and not capacity_exhausted
        and not parallel_capacity_exhausted
        and not (mode == "delegate_closed" and tool_name in _SYNCHRONOUS_DELEGATION_TOOLS)
    ):
        return None
    if parallel_capacity_exhausted:
        content = (
            f"Tool {tool_name} was not executed because this batch exceeds Runtime's "
            f"parallel delegation capacity of {int(control['max_concurrent_subagent_tasks'])}. "
            "Wait for the accepted subagents, then delegate remaining independent work only "
            "if the Runtime still exposes this tool."
        )
    else:
        content = (
            f"Tool {tool_name} was not executed because Runtime budget convergence is active. "
            "Use existing evidence to produce the best available final answer."
        )
    return ToolMessage(
        content=content,
        name=tool_name,
        tool_call_id=str(request.tool_call.get("id") or ""),
        status="error",
    )


def _request_tool_name(tool: Any) -> str:
    if isinstance(tool, str):
        return tool
    if isinstance(tool, dict):
        function = tool.get("function")
        if isinstance(function, dict):
            return str(function.get("name") or "")
        return str(tool.get("name") or "")
    return str(getattr(tool, "name", "") or "")


def _synchronous_member_span(state: Mapping[str, Any], current_call: Any) -> tuple[int, int]:
    calls: Sequence[Any] = ()
    messages = state.get("messages")
    if isinstance(messages, Sequence) and messages:
        calls = getattr(messages[-1], "tool_calls", ()) or ()
    if not calls:
        calls = (current_call,)
    current_id = str(current_call.get("id") or "") if isinstance(current_call, Mapping) else ""
    position = 0
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        name = str(call.get("name") or "")
        members = 0
        if name == "task":
            members = 1
        elif name == "team.run":
            args = call.get("args")
            assignments = args.get("assignments") if isinstance(args, Mapping) else None
            members = len(assignments) if isinstance(assignments, list) else 1
        if str(call.get("id") or "") == current_id:
            return position, position + members
        position += members
    return 0, 1


def _repeated_read_tool(messages: Sequence[Any]) -> tuple[str, int] | None:
    current_turn: list[Any] = []
    for message in reversed(messages):
        current_turn.append(message)
        if (
            getattr(message, "type", None) == "human"
            and isinstance(getattr(message, "additional_kwargs", None), dict)
            and message.additional_kwargs.get("runtime_kind") == "task_input"
        ):
            break

    signatures: list[tuple[str, str]] = []
    for message in reversed(current_turn):
        if getattr(message, "type", None) != "ai":
            continue
        for call in getattr(message, "tool_calls", ()) or ():
            if not isinstance(call, dict):
                continue
            name = str(call.get("name") or "")
            if name not in READ_ONLY_TOOLS:
                signatures.clear()
                continue
            args = call.get("args")
            signatures.append(
                (
                    name,
                    json.dumps(
                        args, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
                    ),
                )
            )
    counts = Counter(signatures[-_RECENT_TOOL_CALL_WINDOW:])
    if not counts:
        return None
    (name, _), count = max(counts.items(), key=lambda item: (item[1], item[0]))
    return (name, count) if count >= 2 else None
