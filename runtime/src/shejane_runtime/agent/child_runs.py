"""Runtime-owned durable child Run control tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..store.sqlite import (
    MAX_DURABLE_CHILD_DEPENDENCIES,
    MAX_DURABLE_CHILD_RESOURCE_CLAIMS,
    MAX_DURABLE_CHILDREN_PER_RUN,
)
from ..tools.runtime import current_runtime_tool_execution_or_none
from .context_builder import RuntimeContext

ChildStatus = Literal[
    "queued",
    "running",
    "waiting_permission",
    "waiting_input",
    "cleanup_required",
    "completed",
    "canceled",
    "failed",
]


@dataclass(frozen=True, slots=True)
class ChildRunControl:
    """Narrow coordinator authority injected into one Runtime execution."""

    spawn: Callable[
        [str, str, str, dict[str, Any], dict[str, Any]],
        Awaitable[dict[str, Any]],
    ]
    list: Callable[[str], Awaitable[list[dict[str, Any]]]]
    check: Callable[[str, Sequence[str]], Awaitable[list[dict[str, Any]]]]
    wait: Callable[
        [str, Sequence[str], Literal["all", "any"], float],
        Awaitable[list[dict[str, Any]]],
    ]
    cancel: Callable[[str, Sequence[str]], Awaitable[list[dict[str, Any]]]]


class ChildRunControlError(RuntimeError):
    pass


class _ChildToolRequest(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        arbitrary_types_allowed=True,
        json_schema_extra={"additionalProperties": False},
    )

    @model_validator(mode="after")
    def reject_untrusted_extras(self) -> _ChildToolRequest:
        extras = self.__pydantic_extra__ or {}
        unknown = set(extras) - {"runtime"}
        if unknown:
            raise ValueError(f"unknown child control fields: {', '.join(sorted(unknown))}")
        runtime = extras.get("runtime")
        if runtime is not None and not isinstance(runtime, ToolRuntime):
            raise ValueError("child control runtime must be injected by ToolNode")
        return self


class SpawnChildRequest(_ChildToolRequest):
    agent: str = Field(min_length=1, max_length=128)
    task: str = Field(min_length=1, max_length=32 * 1024)
    completion_mode: Literal["required", "best_effort", "quorum"] = "required"
    depends_on: list[str] = Field(default_factory=list, max_length=MAX_DURABLE_CHILD_DEPENDENCIES)
    resource_claims: list[str] = Field(
        default_factory=list,
        max_length=MAX_DURABLE_CHILD_RESOURCE_CLAIMS,
    )
    quorum_group: str | None = Field(default=None, min_length=1, max_length=128)
    quorum_required: int | None = Field(
        default=None,
        ge=1,
        le=MAX_DURABLE_CHILDREN_PER_RUN,
    )

    @model_validator(mode="after")
    def coordination_is_consistent(self) -> SpawnChildRequest:
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("child depends_on must be unique")
        if len(set(self.resource_claims)) != len(self.resource_claims):
            raise ValueError("child resource_claims must be unique")
        if self.completion_mode == "quorum":
            if self.quorum_group is None or self.quorum_required is None:
                raise ValueError("quorum children require quorum_group and quorum_required")
        elif self.quorum_group is not None or self.quorum_required is not None:
            raise ValueError("quorum fields are only valid for quorum children")
        return self


class ListChildrenRequest(_ChildToolRequest):
    statuses: list[ChildStatus] = Field(default_factory=list, max_length=8)


class ChildIdsRequest(_ChildToolRequest):
    run_ids: list[str] = Field(
        min_length=1,
        max_length=MAX_DURABLE_CHILDREN_PER_RUN,
    )

    @model_validator(mode="after")
    def child_ids_are_unique(self) -> ChildIdsRequest:
        if len(set(self.run_ids)) != len(self.run_ids):
            raise ValueError("child run_ids must be unique")
        return self


class WaitChildrenRequest(ChildIdsRequest):
    condition: Literal["all", "any"] = "all"
    timeout_seconds: float = Field(default=30.0, ge=0.0, le=30.0)


def build_child_run_tools(
    definitions: dict[str, dict[str, object]],
) -> list[BaseTool]:
    frozen_definitions = {key: dict(value) for key, value in definitions.items()}
    roster = "\n".join(
        f"- {definition_id} ({definition['name']}): {definition['description']}"
        for definition_id, definition in sorted(frozen_definitions.items())
    )

    async def spawn_child(
        agent: str,
        task: str,
        completion_mode: Literal["required", "best_effort", "quorum"] = "required",
        depends_on: list[str] | None = None,
        resource_claims: list[str] | None = None,
        quorum_group: str | None = None,
        quorum_required: int | None = None,
        runtime: ToolRuntime[Any] = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        context, control = _context_and_control(runtime)
        execution = current_runtime_tool_execution_or_none()
        if execution is None:
            raise ChildRunControlError("child.spawn is missing its durable tool operation")
        definition = frozen_definitions.get(agent)
        if definition is None:
            matches = [
                item for item in frozen_definitions.values() if str(item.get("name") or "") == agent
            ]
            if len(matches) != 1:
                raise ChildRunControlError(f"unknown durable child Agent definition: {agent}")
            definition = matches[0]
        return await control.spawn(
            str(context.run_id),
            execution.operation_id,
            task,
            definition,
            {
                "completion_mode": completion_mode,
                "depends_on": depends_on or [],
                "resource_claims": resource_claims or [],
                "quorum_group": quorum_group,
                "quorum_required": quorum_required,
            },
        )

    async def list_children(
        statuses: list[ChildStatus] | None = None,
        runtime: ToolRuntime[Any] = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        context, control = _context_and_control(runtime)
        children = await control.list(str(context.run_id))
        selected = set(statuses or [])
        if selected:
            children = [child for child in children if child.get("status") in selected]
        return {"children": children}

    async def check_children(
        run_ids: list[str],
        runtime: ToolRuntime[Any] = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        context, control = _context_and_control(runtime)
        return {"children": await control.check(str(context.run_id), run_ids)}

    async def wait_children(
        run_ids: list[str],
        condition: Literal["all", "any"] = "all",
        timeout_seconds: float = 30.0,
        runtime: ToolRuntime[Any] = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        context, control = _context_and_control(runtime)
        children = await control.wait(
            str(context.run_id),
            run_ids,
            condition,
            timeout_seconds,
        )
        terminal = {"completed", "failed", "canceled", "cleanup_required"}
        satisfied = (
            all(child.get("status") in terminal for child in children)
            if condition == "all"
            else any(child.get("status") in terminal for child in children)
        )
        return {"condition": condition, "satisfied": satisfied, "children": children}

    async def cancel_children(
        run_ids: list[str],
        runtime: ToolRuntime[Any] = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        context, control = _context_and_control(runtime)
        return {"children": await control.cancel(str(context.run_id), run_ids)}

    return [
        StructuredTool.from_function(
            name="child.spawn",
            coroutine=spawn_child,
            description=(
                "Start an independently durable child Run. It has its own Run/Job/Attempt, "
                "checkpoint, usage, cancellation, restart recovery, dependency gating, and "
                "explicit required/best-effort/quorum completion policy. resource_claims assign "
                "exclusive ownership of exact workspace files. Use child.wait or child.check "
                "to collect its result. Available definitions:\n" + roster
            ),
            args_schema=SpawnChildRequest,
            infer_schema=False,
        ),
        StructuredTool.from_function(
            name="child.list",
            coroutine=list_children,
            description="List durable children directly owned by this Run.",
            args_schema=ListChildrenRequest,
            infer_schema=False,
        ),
        StructuredTool.from_function(
            name="child.check",
            coroutine=check_children,
            description="Read authoritative snapshots for selected durable child Run IDs.",
            args_schema=ChildIdsRequest,
            infer_schema=False,
        ),
        StructuredTool.from_function(
            name="child.wait",
            coroutine=wait_children,
            description=(
                "Wait up to 30 seconds for all or any selected durable children, then return "
                "their authoritative snapshots. A timeout does not cancel them."
            ),
            args_schema=WaitChildrenRequest,
            infer_schema=False,
        ),
        StructuredTool.from_function(
            name="child.cancel",
            coroutine=cancel_children,
            description="Persistently request cancellation for selected durable child Runs.",
            args_schema=ChildIdsRequest,
            infer_schema=False,
        ),
    ]


def _context_and_control(
    runtime: ToolRuntime[Any] | None,
) -> tuple[RuntimeContext, ChildRunControl]:
    context = getattr(runtime, "context", None)
    if not isinstance(context, RuntimeContext) or not context.run_id:
        raise ChildRunControlError("child control is missing Runtime execution context")
    control = context.child_run_control
    if not isinstance(control, ChildRunControl):
        raise ChildRunControlError("durable child Run control is unavailable")
    return context, control
