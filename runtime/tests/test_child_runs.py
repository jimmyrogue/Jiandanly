from __future__ import annotations

from typing import Any

import pytest
from langchain.tools import ToolRuntime

from shejane_runtime.agent.child_runs import ChildRunControl, build_child_run_tools
from shejane_runtime.agent.context_builder import RuntimeContext
from shejane_runtime.tools.runtime import RuntimeToolExecution, bind_runtime_tool_execution

DEFINITIONS: dict[str, dict[str, object]] = {
    "subagent:researcher": {
        "id": "subagent:researcher",
        "version": "sha256:researcher-v1",
        "name": "researcher",
        "description": "Research with primary sources.",
        "system_prompt": "Research the task.",
        "allowed_tools": ["web.fetch", "web.search"],
    }
}


def _runtime(control: ChildRunControl) -> tuple[RuntimeContext, ToolRuntime[Any, Any]]:
    context = RuntimeContext(run_id="parent-run", child_run_control=control)
    runtime = ToolRuntime(
        state={},
        context=context,
        config={},
        stream_writer=lambda _chunk: None,
        tool_call_id="call-child",
        store=None,
    )
    return context, runtime


@pytest.mark.asyncio
async def test_child_control_tools_use_the_frozen_definition_and_parent_scope() -> None:
    calls: list[tuple[str, object]] = []
    child = {"id": "child-1", "status": "queued"}

    async def spawn(
        parent_run_id: str,
        operation_id: str,
        task: str,
        definition: dict[str, Any],
        coordination: dict[str, Any],
    ) -> dict[str, Any]:
        calls.append(
            (
                "spawn",
                (
                    parent_run_id,
                    operation_id,
                    task,
                    definition["id"],
                    definition["version"],
                    coordination,
                ),
            )
        )
        return child

    async def list_children(parent_run_id: str) -> list[dict[str, Any]]:
        calls.append(("list", parent_run_id))
        return [child]

    async def check(
        parent_run_id: str,
        run_ids: list[str],
    ) -> list[dict[str, Any]]:
        calls.append(("check", (parent_run_id, tuple(run_ids))))
        return [{**child, "status": "running"}]

    async def wait(
        parent_run_id: str,
        run_ids: list[str],
        condition: str,
        timeout_seconds: float,
    ) -> list[dict[str, Any]]:
        calls.append(("wait", (parent_run_id, tuple(run_ids), condition, timeout_seconds)))
        return [{**child, "status": "completed", "result": "done"}]

    async def cancel(
        parent_run_id: str,
        run_ids: list[str],
    ) -> list[dict[str, Any]]:
        calls.append(("cancel", (parent_run_id, tuple(run_ids))))
        return [{**child, "status": "canceled"}]

    control = ChildRunControl(
        spawn=spawn,
        list=list_children,
        check=check,
        wait=wait,  # type: ignore[arg-type]
        cancel=cancel,
    )
    context, runtime = _runtime(control)
    tools = {tool.name: tool for tool in build_child_run_tools(DEFINITIONS)}

    with bind_runtime_tool_execution(
        RuntimeToolExecution(
            context=context,
            operation_id="toolop-spawn",
            tool_call_id="call-child",
        )
    ):
        assert (
            await tools["child.spawn"].coroutine(  # type: ignore[misc]
                agent="researcher",
                task="Find the source",
                runtime=runtime,
            )
            == child
        )
    assert await tools["child.list"].coroutine(runtime=runtime) == {  # type: ignore[misc]
        "children": [child]
    }
    assert await tools["child.check"].coroutine(  # type: ignore[misc]
        run_ids=["child-1"],
        runtime=runtime,
    ) == {"children": [{**child, "status": "running"}]}
    waited = await tools["child.wait"].coroutine(  # type: ignore[misc]
        run_ids=["child-1"],
        condition="all",
        timeout_seconds=5,
        runtime=runtime,
    )
    assert waited["satisfied"] is True
    assert await tools["child.cancel"].coroutine(  # type: ignore[misc]
        run_ids=["child-1"],
        runtime=runtime,
    ) == {"children": [{**child, "status": "canceled"}]}
    assert calls == [
        (
            "spawn",
            (
                "parent-run",
                "toolop-spawn",
                "Find the source",
                "subagent:researcher",
                "sha256:researcher-v1",
                {
                    "completion_mode": "required",
                    "depends_on": [],
                    "resource_claims": [],
                    "quorum_group": None,
                    "quorum_required": None,
                },
            ),
        ),
        ("list", "parent-run"),
        ("check", ("parent-run", ("child-1",))),
        ("wait", ("parent-run", ("child-1",), "all", 5)),
        ("cancel", ("parent-run", ("child-1",))),
    ]


def test_child_control_schemas_reject_unknown_fields_and_duplicate_ids() -> None:
    tools = {tool.name: tool for tool in build_child_run_tools(DEFINITIONS)}

    with pytest.raises(ValueError, match="unknown child control fields"):
        tools["child.spawn"].args_schema.model_validate(  # type: ignore[union-attr]
            {"agent": "researcher", "task": "x", "untrusted": True}
        )
    with pytest.raises(ValueError, match="must be unique"):
        tools["child.wait"].args_schema.model_validate(  # type: ignore[union-attr]
            {"run_ids": ["child-1", "child-1"]}
        )
    with pytest.raises(ValueError, match="require quorum_group"):
        tools["child.spawn"].args_schema.model_validate(  # type: ignore[union-attr]
            {"agent": "researcher", "task": "x", "completion_mode": "quorum"}
        )
    with pytest.raises(ValueError, match="only valid for quorum"):
        tools["child.spawn"].args_schema.model_validate(  # type: ignore[union-attr]
            {
                "agent": "researcher",
                "task": "x",
                "completion_mode": "required",
                "quorum_group": "review",
                "quorum_required": 1,
            }
        )
