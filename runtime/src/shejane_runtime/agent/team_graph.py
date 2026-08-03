"""Checkpointed, same-Run orchestration for bounded subagent teams."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Annotated, Any, Literal, TypedDict

from langchain.agents.middleware import ToolCallRequest
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Checkpointer, Command, Send
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..middleware.tool_execution import ToolExecutionMiddleware
from ..store.sqlite import MAX_ARTIFACT_BYTES, LocalStore
from ..tools.runtime import current_runtime_tool_execution_or_none
from .context_builder import RuntimeContext
from .subagents import DEEPAGENT_SUBAGENT_BASE_TOOL_NAMES

TEAM_ASSIGNMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MAX_TEAM_ASSIGNMENTS = 5
MAX_INLINE_FINDING_BYTES = 64 * 1024
MAX_FINDING_PREVIEW_CHARS = 8_000

log = logging.getLogger("shejane_runtime.agent.team_graph")


class TeamPlanError(ValueError):
    """The requested team topology is invalid or cannot make progress."""


class TeamMemberError(RuntimeError):
    """A team member ended without a usable successful finding."""


@dataclass(frozen=True, slots=True)
class TeamMemberDefinition:
    """P6-frozen team member surface used by the P7 graph."""

    name: str
    description: str
    tool_names: tuple[str, ...]
    allowed_handoffs: tuple[str, ...]


class TeamAssignment(BaseModel):
    """One bounded unit of work inside the same Runtime Run."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    member: str = Field(min_length=1, max_length=64)
    task: str = Field(min_length=1, max_length=16_000)
    output_kind: Literal["finding", "claim", "review"] = "finding"
    depends_on: list[str] = Field(default_factory=list, max_length=MAX_TEAM_ASSIGNMENTS)
    handoff_from: str | None = Field(default=None, max_length=64)


class TeamRunRequest(BaseModel):
    """Model-authored plan accepted by the ``team.run`` control tool."""

    # ToolNode injects its trusted ToolRuntime into the raw input before
    # StructuredTool applies this schema. Keep the model-facing JSON schema
    # closed while permitting exactly that one runtime-only extra value.
    model_config = ConfigDict(
        extra="allow",
        arbitrary_types_allowed=True,
        json_schema_extra={"additionalProperties": False},
    )

    objective: str = Field(min_length=1, max_length=16_000)
    assignments: list[TeamAssignment] = Field(
        min_length=1,
        max_length=MAX_TEAM_ASSIGNMENTS,
    )

    @model_validator(mode="after")
    def reject_untrusted_extras(self) -> TeamRunRequest:
        extras = self.__pydantic_extra__ or {}
        unknown = set(extras) - {"runtime"}
        if unknown:
            raise ValueError(f"unknown team.run fields: {', '.join(sorted(unknown))}")
        runtime = extras.get("runtime")
        if runtime is not None and not isinstance(runtime, ToolRuntime):
            raise ValueError("team.run runtime must be injected by ToolNode")
        return self


class TeamFinding(TypedDict):
    assignment_id: str
    member: str
    kind: str
    summary: str
    artifact_id: str | None


class TeamGraphState(TypedDict, total=False):
    objective: str
    assignments: list[dict[str, Any]]
    team_namespace: str
    plan_hash: str
    findings: Annotated[list[TeamFinding], lambda left, right: _merge_findings(left, right)]
    result: dict[str, Any]


class TeamWorkerState(TypedDict):
    objective: str
    assignment: dict[str, Any]
    dependency_findings: list[TeamFinding]
    team_namespace: str


TeamMemberInvoker = Callable[
    [TeamAssignment, str, RuntimeContext, RunnableConfig],
    Awaitable[str],
]


def validate_team_request(
    request: TeamRunRequest,
    roster: Sequence[TeamMemberDefinition],
) -> None:
    """Validate member identities, graph acyclicity, and handoff edges."""

    members = {member.name: member for member in roster}
    if len(members) != len(roster):
        raise TeamPlanError("team roster contains duplicate member names")
    if not members:
        raise TeamPlanError("team roster is empty")

    assignments = {assignment.id: assignment for assignment in request.assignments}
    if len(assignments) != len(request.assignments):
        raise TeamPlanError("team assignment IDs must be unique")

    for assignment in request.assignments:
        if TEAM_ASSIGNMENT_ID_RE.fullmatch(assignment.id) is None:
            raise TeamPlanError(f"invalid team assignment ID: {assignment.id}")
        if assignment.member not in members:
            raise TeamPlanError(f"unknown team member: {assignment.member}")
        if len(set(assignment.depends_on)) != len(assignment.depends_on):
            raise TeamPlanError(f"assignment {assignment.id} has duplicate dependencies")
        for dependency_id in assignment.depends_on:
            if dependency_id not in assignments:
                raise TeamPlanError(
                    f"assignment {assignment.id} depends on unknown assignment {dependency_id}"
                )
            if dependency_id == assignment.id:
                raise TeamPlanError(f"assignment {assignment.id} depends on itself")

        source_id = assignment.handoff_from
        if source_id is None:
            continue
        if source_id not in assignment.depends_on:
            raise TeamPlanError(
                f"handoff source {source_id} must also be a dependency of {assignment.id}"
            )
        source_member = assignments[source_id].member
        if assignment.member not in members[source_member].allowed_handoffs:
            raise TeamPlanError(f"handoff {source_member} -> {assignment.member} is not allowed")

    remaining = {assignment.id: set(assignment.depends_on) for assignment in request.assignments}
    while remaining:
        ready = {assignment_id for assignment_id, deps in remaining.items() if not deps}
        if not ready:
            raise TeamPlanError("team assignment dependency cycle detected")
        for assignment_id in ready:
            remaining.pop(assignment_id)
        for dependencies in remaining.values():
            dependencies.difference_update(ready)


def team_graph_input(
    request: TeamRunRequest,
    *,
    team_namespace: str,
) -> TeamGraphState:
    """Create the canonical checkpoint input for one team operation."""

    canonical = json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "objective": request.objective,
        "assignments": [item.model_dump(mode="json") for item in request.assignments],
        "team_namespace": team_namespace,
        "plan_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "findings": [],
    }


def build_team_graph(
    *,
    roster: Sequence[TeamMemberDefinition],
    member_invoker: TeamMemberInvoker,
    checkpointer: Checkpointer | None,
) -> Any:
    """Compile the P7 Team Graph using Send, a reducer, and explicit Command routing."""

    frozen_roster = tuple(roster)

    def prepare(state: TeamGraphState) -> dict[str, Any]:
        request = TeamRunRequest.model_validate(
            {
                "objective": state["objective"],
                "assignments": state["assignments"],
            }
        )
        validate_team_request(request, frozen_roster)
        return {}

    def dispatch(_state: TeamGraphState) -> dict[str, Any]:
        return {}

    def route_dispatch(state: TeamGraphState) -> str | list[Send]:
        completed = {item["assignment_id"] for item in state.get("findings", [])}
        assignments = [TeamAssignment.model_validate(item) for item in state["assignments"]]
        if len(completed) == len(assignments):
            return "finish"

        ready = [
            assignment
            for assignment in assignments
            if assignment.id not in completed
            and all(dependency in completed for dependency in assignment.depends_on)
        ]
        if not ready:
            raise TeamPlanError("team graph cannot make progress")
        findings = {item["assignment_id"]: item for item in state.get("findings", [])}
        return [
            Send(
                "member",
                {
                    "objective": state["objective"],
                    "assignment": assignment.model_dump(mode="json"),
                    "dependency_findings": [findings[item] for item in assignment.depends_on],
                    "team_namespace": state["team_namespace"],
                },
            )
            for assignment in ready
        ]

    async def run_member(
        state: TeamWorkerState,
        config: RunnableConfig,
        runtime: Runtime[RuntimeContext],
    ) -> Command[Literal["dispatch"]]:
        assignment = TeamAssignment.model_validate(state["assignment"])
        prompt = _member_prompt(
            objective=state["objective"],
            assignment=assignment,
            dependency_findings=state["dependency_findings"],
        )
        summary = await member_invoker(assignment, prompt, runtime.context, config)
        finding = await _project_finding(
            assignment=assignment,
            summary=summary,
            team_namespace=state["team_namespace"],
            context=runtime.context,
        )
        return Command(update={"findings": [finding]}, goto="dispatch")

    def finish(state: TeamGraphState) -> dict[str, Any]:
        assignments = {
            item.id: item
            for item in (TeamAssignment.model_validate(raw) for raw in state["assignments"])
        }
        findings = sorted(state.get("findings", []), key=lambda item: item["assignment_id"])
        handoffs = []
        for target in assignments.values():
            if target.handoff_from is None:
                continue
            source = assignments[target.handoff_from]
            handoffs.append(
                {
                    "from_assignment_id": source.id,
                    "from_member": source.member,
                    "to_assignment_id": target.id,
                    "to_member": target.member,
                }
            )
        return {
            "result": {
                "status": "completed",
                "objective": state["objective"],
                "findings": findings,
                "handoffs": handoffs,
            }
        }

    graph = StateGraph(TeamGraphState, context_schema=RuntimeContext)
    graph.add_node("prepare", prepare)
    graph.add_node("dispatch", dispatch)
    graph.add_node("member", run_member)
    graph.add_node("finish", finish)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "dispatch")
    graph.add_conditional_edges("dispatch", route_dispatch)
    graph.add_edge("finish", END)

    return graph.compile(checkpointer=checkpointer)


def build_team_roster(subagents: Sequence[dict[str, Any]]) -> tuple[TeamMemberDefinition, ...]:
    """Freeze names, descriptions, tool surfaces, and legal handoff edges at P6."""

    names = tuple(str(item.get("name") or "") for item in subagents)
    return tuple(
        TeamMemberDefinition(
            name=name,
            description=str(item.get("description") or ""),
            tool_names=_effective_member_tool_names(item),
            # A handoff transfers only the prior structured finding. The target
            # still executes with its own frozen, attenuated tool surface.
            allowed_handoffs=tuple(sorted(other for other in names if other != name)),
        )
        for item, name in zip(subagents, names, strict=True)
    )


def _effective_member_tool_names(subagent: dict[str, Any]) -> tuple[str, ...]:
    names = set(DEEPAGENT_SUBAGENT_BASE_TOOL_NAMES)
    names.update(
        str(getattr(tool, "name", ""))
        for tool in subagent.get("tools", [])
        if getattr(tool, "name", None)
    )
    for middleware in subagent.get("middleware", []):
        blocked = getattr(middleware, "blocked_tool_names", None)
        if isinstance(blocked, set):
            names.difference_update(str(name) for name in blocked)
    return tuple(sorted(names))


def build_team_tool(
    *,
    roster: Sequence[TeamMemberDefinition],
    checkpointer: Checkpointer,
) -> BaseTool:
    """Build the parent-only control tool that enters the checkpointed Team Graph."""

    frozen_roster = tuple(roster)
    graph = build_team_graph(
        roster=frozen_roster,
        member_invoker=_invoke_member_via_task,
        checkpointer=checkpointer,
    )
    roster_description = "\n".join(
        f"- {member.name}: {member.description}" for member in frozen_roster
    )
    description = f"""Run a bounded team workflow inside the current Runtime Run.

Use this when two or more assignments can run independently, or when a later
assignment must review or transform structured findings from an earlier one.
Independent assignments fan out concurrently. `depends_on` creates fan-in;
`handoff_from` explicitly transfers one dependency's finding to another member.
At most {MAX_TEAM_ASSIGNMENTS} assignments are allowed. Members never share raw
transcripts or hidden reasoning, and every member keeps its own frozen tools.
The Runtime validates member names, dependencies, permissions, and handoff edges.

Available team members:
{roster_description}"""

    def run_team(
        objective: str,
        assignments: list[TeamAssignment],
        runtime: ToolRuntime[Any] = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        raise RuntimeError("team.run requires the async Runtime executor")

    async def arun_team(
        objective: str,
        assignments: list[TeamAssignment],
        runtime: ToolRuntime[Any] = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        request = TeamRunRequest(objective=objective, assignments=assignments)
        validate_team_request(request, frozen_roster)
        context = getattr(runtime, "context", None)
        if not isinstance(context, RuntimeContext) or not context.run_id:
            raise TeamPlanError("team.run is missing Runtime execution context")
        execution = current_runtime_tool_execution_or_none()
        if execution is None:
            raise TeamPlanError("team.run is missing its durable tool operation")

        namespace_hash = hashlib.sha256(execution.operation_id.encode("utf-8")).hexdigest()
        team_namespace = f"team_{namespace_hash[:24]}"
        team_thread_id = f"{context.run_id}:team:{namespace_hash[:24]}"
        graph_input = team_graph_input(request, team_namespace=team_namespace)
        config: RunnableConfig = {
            "configurable": {
                # This is a LangGraph checkpoint thread, not a Runtime child Run.
                "thread_id": team_thread_id,
                "checkpoint_ns": "",
                "ls_agent_type": "team",
            }
        }
        snapshot = await graph.aget_state(config)
        values = snapshot.values if isinstance(snapshot.values, dict) else {}
        invocation_input: TeamGraphState | None = graph_input
        if values:
            if values.get("plan_hash") != graph_input["plan_hash"]:
                raise TeamPlanError("team checkpoint does not match the current plan")
            result = values.get("result")
            if isinstance(result, dict):
                return result
            invocation_input = None

        state = await graph.ainvoke(invocation_input, config, context=context)
        result = state.get("result")
        if not isinstance(result, dict):
            raise TeamPlanError("team graph ended without a result")
        return result

    return StructuredTool.from_function(
        name="team.run",
        func=run_team,
        coroutine=arun_team,
        description=description,
        args_schema=TeamRunRequest,
        infer_schema=False,
    )


async def _invoke_member_via_task(
    assignment: TeamAssignment,
    prompt: str,
    context: RuntimeContext,
    config: RunnableConfig,
) -> str:
    """Invoke the existing task tool through the shared durable Receipt boundary."""

    task_tool = context.tool_registry.get("task")
    coroutine = getattr(task_tool, "coroutine", None)
    if not isinstance(task_tool, BaseTool) or coroutine is None:
        raise TeamPlanError("the frozen Agent definition has no task tool")

    configurable = config.get("configurable") if isinstance(config, dict) else None
    parent_namespace = (
        str(configurable.get("checkpoint_ns") or "team")
        if isinstance(configurable, dict)
        else "team"
    )
    team_thread_id = (
        str(configurable.get("thread_id") or context.run_id)
        if isinstance(configurable, dict)
        else str(context.run_id)
    )
    team_token = hashlib.sha256(team_thread_id.encode()).hexdigest()[:24]
    member_namespace = f"{parent_namespace}|team_{team_token}|member_{assignment.id}"
    member_config: RunnableConfig = {
        "configurable": {
            "thread_id": str(context.run_id),
            "checkpoint_ns": member_namespace,
            "ls_agent_type": "subagent",
        }
    }
    call_hash = hashlib.sha256(
        f"{member_namespace}\0{assignment.id}\0{assignment.member}".encode()
    ).hexdigest()
    call_id = f"team_task_{call_hash[:24]}"
    arguments = {
        "description": prompt,
        "subagent_type": assignment.member,
    }
    tool_runtime = SimpleNamespace(
        context=context,
        config=member_config,
        state={"messages": []},
        tool_call_id=call_id,
    )
    request = ToolCallRequest(
        tool_call={
            "id": call_id,
            "name": "task",
            "args": arguments,
            "type": "tool_call",
        },
        tool=task_tool,
        state={"messages": []},
        runtime=tool_runtime,
    )

    async def invoke(_request: ToolCallRequest) -> ToolMessage | Command[Any]:
        try:
            outcome = await coroutine(
                description=prompt,
                subagent_type=assignment.member,
                runtime=tool_runtime,
            )
        except Exception:
            log.exception("team member task invocation failed", extra={"member": assignment.member})
            raise
        if isinstance(outcome, (ToolMessage, Command)):
            return outcome
        return ToolMessage(
            content=str(outcome),
            name="task",
            tool_call_id=call_id,
        )

    outcome = await ToolExecutionMiddleware().awrap_tool_call(request, invoke)
    message = _task_result_message(outcome)
    if str(message.status or "") == "error":
        raise TeamMemberError(message.text or str(message.content))
    text = message.text.rstrip() if message.text else ""
    if not text:
        raise TeamMemberError(f"team member {assignment.member} returned no summary")
    return text


def _task_result_message(outcome: ToolMessage | Command[Any]) -> ToolMessage:
    if isinstance(outcome, ToolMessage):
        return outcome
    update = outcome.update
    if isinstance(update, dict):
        messages = update.get("messages")
        if isinstance(messages, list):
            for message in reversed(messages):
                if isinstance(message, ToolMessage):
                    return message
    raise TeamMemberError("task tool returned no ToolMessage")


def _merge_findings(
    existing: list[TeamFinding] | None,
    updates: list[TeamFinding] | None,
) -> list[TeamFinding]:
    merged = {item["assignment_id"]: item for item in existing or []}
    for item in updates or []:
        prior = merged.get(item["assignment_id"])
        if prior is not None and prior != item:
            raise TeamPlanError(
                f"assignment {item['assignment_id']} produced conflicting replay results"
            )
        merged[item["assignment_id"]] = item
    return [merged[item] for item in sorted(merged)]


def _member_prompt(
    *,
    objective: str,
    assignment: TeamAssignment,
    dependency_findings: Sequence[TeamFinding],
) -> str:
    payload = [
        {
            "assignment_id": item["assignment_id"],
            "member": item["member"],
            "kind": item["kind"],
            "summary": item["summary"],
            "artifact_id": item["artifact_id"],
        }
        for item in dependency_findings
    ]
    sections = [
        f"Team objective:\n{objective}",
        f"Your assignment ({assignment.output_kind}):\n{assignment.task}",
    ]
    if payload:
        sections.append(
            "Dependency findings (summaries and Artifact references only):\n"
            + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )
    sections.append(
        "Return one self-contained final summary. Do not include hidden reasoning or raw scratch work."
    )
    return "\n\n".join(sections)


async def _project_finding(
    *,
    assignment: TeamAssignment,
    summary: str,
    team_namespace: str,
    context: RuntimeContext,
) -> TeamFinding:
    encoded = summary.encode("utf-8")
    if len(encoded) <= MAX_INLINE_FINDING_BYTES:
        return {
            "assignment_id": assignment.id,
            "member": assignment.member,
            "kind": assignment.output_kind,
            "summary": summary,
            "artifact_id": None,
        }
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise TeamPlanError(f"assignment {assignment.id} result exceeds the Runtime Artifact limit")

    store = context.store
    run_id = str(context.run_id or "")
    if not isinstance(store, LocalStore) or not run_id:
        raise TeamPlanError("large team finding is missing Runtime Artifact storage")
    digest = hashlib.sha256(f"{team_namespace}\0{assignment.id}".encode()).hexdigest()
    artifact_id = f"art_team_{digest[:32]}"
    existing = await store.get_artifact(artifact_id)
    if existing is None:
        artifact = await store.create_artifact(
            artifact_id=artifact_id,
            run_id=run_id,
            kind="team_finding",
            title=f"{assignment.member}: {assignment.id}",
            content=summary,
            content_type="text/plain",
            tool_name="team.run",
            metadata={
                "team_namespace": team_namespace,
                "assignment_id": assignment.id,
                "member": assignment.member,
                "output_kind": assignment.output_kind,
            },
        )
    else:
        if existing.get("run_id") != run_id or existing.get("kind") != "team_finding":
            raise TeamPlanError(f"artifact identity conflict for assignment {assignment.id}")
        artifact = existing
        stored = artifact.get("content")
        if isinstance(stored, str):
            summary = stored

    preview = summary[:MAX_FINDING_PREVIEW_CHARS].rstrip()
    return {
        "assignment_id": assignment.id,
        "member": assignment.member,
        "kind": assignment.output_kind,
        "summary": f"{preview}\n\n[Full finding: Artifact {artifact['id']}]",
        "artifact_id": str(artifact["id"]),
    }
