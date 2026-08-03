"""Phase 1: same-Run Team Graph orchestration."""

from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command
from pydantic import BaseModel

from shejane_runtime.agent.builder import open_checkpointer
from shejane_runtime.agent.context_builder import RuntimeContext
from shejane_runtime.agent.team_graph import (
    TeamAssignment,
    TeamMemberDefinition,
    TeamPlanError,
    TeamRunRequest,
    build_team_graph,
    build_team_roster,
    build_team_tool,
    team_graph_input,
    validate_team_request,
)
from shejane_runtime.auth import LOCAL_OWNER_PRINCIPAL_ID
from shejane_runtime.config import reset_settings_for_tests
from shejane_runtime.store.sqlite import LocalStore
from shejane_runtime.tools.runtime import (
    RuntimeToolExecution,
    bind_runtime_tool_execution,
)


def _roster() -> tuple[TeamMemberDefinition, ...]:
    return (
        TeamMemberDefinition(
            name="researcher",
            description="Research evidence.",
            tool_names=("web.search",),
            allowed_handoffs=("writer",),
        ),
        TeamMemberDefinition(
            name="writer",
            description="Write from supplied evidence.",
            tool_names=(),
            allowed_handoffs=(),
        ),
    )


def test_team_roster_matches_enforced_subagent_tool_surfaces() -> None:
    from shejane_runtime.agent.subagents import build_subagents

    roster = {
        member.name: member
        for member in build_team_roster(
            build_subagents(main_tools=[], main_model="model", agent_roots=[])
        )
    }

    assert roster["writer"].tool_names == ()
    assert "read_file" in roster["researcher"].tool_names
    assert "execute" not in roster["researcher"].tool_names
    assert "write_file" in roster["general-purpose"].tool_names
    assert roster["researcher"].allowed_handoffs == ("general-purpose", "writer")


@pytest.mark.asyncio
async def test_team_graph_fans_out_and_reduces_structured_findings() -> None:
    started: set[str] = set()
    both_started = asyncio.Event()
    release = asyncio.Event()

    async def invoke_member(assignment, _prompt, _context, _config):
        started.add(assignment.id)
        if len(started) == 2:
            both_started.set()
        await release.wait()
        return f"result for {assignment.id}"

    graph = build_team_graph(
        roster=_roster(),
        member_invoker=invoke_member,
        checkpointer=InMemorySaver(),
    )
    request = TeamRunRequest(
        objective="Compare two independent sources.",
        assignments=[
            TeamAssignment(
                id="source-a",
                member="researcher",
                task="Research source A.",
                output_kind="finding",
            ),
            TeamAssignment(
                id="source-b",
                member="researcher",
                task="Research source B.",
                output_kind="claim",
            ),
        ],
    )
    config = {"configurable": {"thread_id": "run-team", "checkpoint_ns": "team-op"}}
    running = asyncio.create_task(
        graph.ainvoke(
            team_graph_input(request, team_namespace="team-op"),
            config,
            context=RuntimeContext(run_id="run-team"),
        )
    )

    await asyncio.wait_for(both_started.wait(), timeout=1)
    release.set()
    result = await running

    assert [item["assignment_id"] for item in result["result"]["findings"]] == [
        "source-a",
        "source-b",
    ]
    assert [item["kind"] for item in result["result"]["findings"]] == [
        "finding",
        "claim",
    ]
    assert result["result"]["status"] == "completed"


@pytest.mark.asyncio
async def test_team_graph_handoff_passes_only_structured_findings() -> None:
    order: list[str] = []
    writer_prompt = ""

    async def invoke_member(assignment, prompt, _context, _config):
        nonlocal writer_prompt
        order.append(assignment.id)
        if assignment.id == "draft":
            writer_prompt = prompt
            return "Final draft"
        return "Evidence summary"

    graph = build_team_graph(
        roster=_roster(),
        member_invoker=invoke_member,
        checkpointer=InMemorySaver(),
    )
    request = TeamRunRequest(
        objective="Research, then write.",
        assignments=[
            TeamAssignment(
                id="research",
                member="researcher",
                task="Collect evidence.",
                output_kind="finding",
            ),
            TeamAssignment(
                id="draft",
                member="writer",
                task="Write the answer.",
                output_kind="review",
                depends_on=["research"],
                handoff_from="research",
            ),
        ],
    )
    result = await graph.ainvoke(
        team_graph_input(request, team_namespace="team-handoff"),
        {"configurable": {"thread_id": "run-handoff", "checkpoint_ns": "team-handoff"}},
        context=RuntimeContext(run_id="run-handoff"),
    )

    assert order == ["research", "draft"]
    assert "Evidence summary" in writer_prompt
    assert "intermediate messages" not in writer_prompt
    assert result["result"]["handoffs"] == [
        {
            "from_assignment_id": "research",
            "from_member": "researcher",
            "to_assignment_id": "draft",
            "to_member": "writer",
        }
    ]


def test_team_plan_rejects_cycles_and_disallowed_handoffs() -> None:
    cyclic = TeamRunRequest(
        objective="Cycle.",
        assignments=[
            TeamAssignment(
                id="a",
                member="researcher",
                task="A",
                depends_on=["b"],
            ),
            TeamAssignment(
                id="b",
                member="writer",
                task="B",
                depends_on=["a"],
            ),
        ],
    )
    with pytest.raises(TeamPlanError, match="cycle"):
        validate_team_request(cyclic, _roster())

    forbidden = TeamRunRequest(
        objective="Wrong-way handoff.",
        assignments=[
            TeamAssignment(id="draft", member="writer", task="Draft"),
            TeamAssignment(
                id="research",
                member="researcher",
                task="Research",
                depends_on=["draft"],
                handoff_from="draft",
            ),
        ],
    )
    with pytest.raises(TeamPlanError, match=r"handoff.*not allowed"):
        validate_team_request(forbidden, _roster())

    with pytest.raises(ValueError, match=r"unknown team.run fields"):
        TeamRunRequest.model_validate(
            {
                "objective": "Reject unknown input.",
                "assignments": [{"id": "a", "member": "researcher", "task": "Research"}],
                "unexpected": True,
            }
        )


@pytest.mark.asyncio
async def test_team_graph_resumes_after_checkpointer_reopen_without_repeating_handoff(
    tmp_path: Path,
) -> None:
    calls: Counter[str] = Counter()

    async def invoke_member(assignment, _prompt, _context, _config):
        calls[assignment.id] += 1
        if assignment.id == "draft" and calls[assignment.id] == 1:
            raise RuntimeError("worker stopped")
        return f"result for {assignment.id}"

    reset_settings_for_tests(data_dir=tmp_path)
    request = TeamRunRequest(
        objective="Resume a handoff.",
        assignments=[
            TeamAssignment(id="research", member="researcher", task="Research"),
            TeamAssignment(
                id="draft",
                member="writer",
                task="Draft",
                depends_on=["research"],
                handoff_from="research",
            ),
        ],
    )
    config = {"configurable": {"thread_id": "run-resume:team:one", "checkpoint_ns": ""}}

    saver, stack = await open_checkpointer()
    try:
        graph = build_team_graph(
            roster=_roster(),
            member_invoker=invoke_member,
            checkpointer=saver,
        )
        with pytest.raises(RuntimeError, match="worker stopped"):
            await graph.ainvoke(
                team_graph_input(request, team_namespace="team-resume"),
                config,
                context=RuntimeContext(run_id="run-resume"),
            )
    finally:
        await stack.aclose()

    reopened_saver, reopened_stack = await open_checkpointer()
    try:
        reopened_graph = build_team_graph(
            roster=_roster(),
            member_invoker=invoke_member,
            checkpointer=reopened_saver,
        )
        result = await reopened_graph.ainvoke(
            None,
            config,
            context=RuntimeContext(run_id="run-resume"),
        )
    finally:
        await reopened_stack.aclose()

    assert calls == Counter({"draft": 2, "research": 1})
    assert result["result"]["status"] == "completed"


@pytest.mark.asyncio
async def test_large_team_finding_is_replaced_by_runtime_artifact_reference(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    run = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="large team finding",
        workspace_path=None,
    )
    full_finding = "evidence" * 9_000

    async def invoke_member(_assignment, _prompt, _context, _config):
        return full_finding

    graph = build_team_graph(
        roster=_roster(),
        member_invoker=invoke_member,
        checkpointer=InMemorySaver(),
    )
    request = TeamRunRequest(
        objective="Store a large finding.",
        assignments=[TeamAssignment(id="large", member="researcher", task="Return evidence.")],
    )
    try:
        result = await graph.ainvoke(
            team_graph_input(request, team_namespace="team-large"),
            {"configurable": {"thread_id": "run-large", "checkpoint_ns": "team-large"}},
            context=RuntimeContext(run_id=str(run["id"]), store=store),
        )
        finding = result["result"]["findings"][0]
        assert finding["artifact_id"].startswith("art_team_")
        assert "Full finding: Artifact" in finding["summary"]
        artifact = await store.get_artifact(finding["artifact_id"])
        assert artifact is not None
        assert artifact["content"] == full_finding
        assert artifact["kind"] == "team_finding"
    finally:
        await store.close()


class _TaskArgs(BaseModel):
    description: str
    subagent_type: str


@pytest.mark.asyncio
async def test_team_member_reuses_task_receipt_and_lifecycle_projection(tmp_path: Path) -> None:
    store = await LocalStore.open(tmp_path / "runtime.db")
    run = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="team lifecycle",
        workspace_path=None,
    )
    run_id = str(run["id"])
    outer_operation_id = "toolop_team_outer"
    await store.prepare_tool_receipt(
        operation_id=outer_operation_id,
        run_id=run_id,
        execution_attempt_id="job-team:1",
        execution_namespace="main",
        tool_call_id="call-team",
        tool_name="team.run",
        tool_version="graph-v1",
        arguments_hash="team-args",
        arguments_json="{}",
        risk="control_flow",
    )
    await store.begin_tool_receipt(
        operation_id=outer_operation_id,
        run_id=run_id,
        execution_attempt_id="job-team:1",
    )

    def task(
        description: str,
        subagent_type: str,
        runtime: ToolRuntime,
    ) -> Command:
        raise RuntimeError("async only")

    async def atask(
        description: str,
        subagent_type: str,
        runtime: ToolRuntime,
    ) -> Command:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=f"{subagent_type}: finished",
                        name="task",
                        tool_call_id=runtime.tool_call_id,
                    )
                ]
            }
        )

    task_tool = StructuredTool.from_function(
        name="task",
        func=task,
        coroutine=atask,
        description="Run one subagent.",
        args_schema=_TaskArgs,
        infer_schema=False,
    )
    context = RuntimeContext(
        run_id=run_id,
        store=store,
        execution_attempt_id="job-team:1",
        graph_definition_id="graph-v1",
        tool_registry={"task": task_tool},
    )
    team_tool = build_team_tool(
        roster=(
            TeamMemberDefinition(
                name="researcher",
                description="Research evidence.",
                tool_names=(),
                allowed_handoffs=(),
            ),
        ),
        checkpointer=InMemorySaver(),
    )
    runtime = SimpleNamespace(context=context, config={}, state={}, tool_call_id="call-team")

    try:
        with bind_runtime_tool_execution(
            RuntimeToolExecution(
                context=context,
                operation_id=outer_operation_id,
                tool_call_id="call-team",
            )
        ):
            result = await team_tool.coroutine(
                objective="Research one item.",
                assignments=[
                    TeamAssignment(
                        id="research",
                        member="researcher",
                        task="Collect evidence.",
                    )
                ],
                runtime=runtime,
            )

        assert result["status"] == "completed"
        receipts = await store.list_tool_receipts_for_run(run_id)
        child = next(item for item in receipts if item["tool_name"] == "task")
        assert child["parent_operation_id"] == outer_operation_id
        assert child["status"] == "completed"
        assert child["attempt_count"] == 1
        events = await store.events_since(run_id)
        assert [
            event["event_type"]
            for event in events
            if str(event["event_type"]).startswith("subagent.")
        ] == ["subagent.spawned", "subagent.started", "subagent.completed"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_team_tool_schema_reaches_handler_through_real_tool_node() -> None:
    team_tool = build_team_tool(
        roster=(
            TeamMemberDefinition(
                name="researcher",
                description="Research evidence.",
                tool_names=(),
                allowed_handoffs=(),
            ),
        ),
        checkpointer=InMemorySaver(),
    )
    builder = StateGraph(MessagesState, context_schema=RuntimeContext)
    builder.add_node("tools", ToolNode([team_tool], handle_tool_errors=False))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    graph = builder.compile()

    with pytest.raises(TeamPlanError, match="durable tool operation"):
        await graph.ainvoke(
            {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "call-team-schema",
                                "name": "team.run",
                                "args": {
                                    "objective": "Research one item.",
                                    "assignments": [
                                        {
                                            "id": "research",
                                            "member": "researcher",
                                            "task": "Collect evidence.",
                                        }
                                    ],
                                },
                            }
                        ],
                    )
                ]
            },
            context=RuntimeContext(run_id="run-team-schema"),
        )
