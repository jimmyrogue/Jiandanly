"""Frozen team plan schema, topology validation, and canonical checkpoint input."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from langchain.tools import ToolRuntime
from pydantic import BaseModel, ConfigDict, Field, model_validator

TEAM_ASSIGNMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MAX_TEAM_ASSIGNMENTS = 5


class TeamPlanError(ValueError):
    """The requested team topology is invalid or cannot make progress."""


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
) -> dict[str, Any]:
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
