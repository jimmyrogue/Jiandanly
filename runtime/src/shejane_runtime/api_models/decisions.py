"""Human permission, question, and Tool reconciliation schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

PermissionDecision = Literal["approve", "edit", "deny"]
PermissionScope = Literal["once", "run"]


class EditedToolAction(BaseModel):
    name: str = Field(min_length=1)
    args: dict[str, Any]


class ResolvePermissionRequest(BaseModel):
    decision: PermissionDecision
    scope: PermissionScope = "once"
    edited_action: EditedToolAction | None = None

    @model_validator(mode="after")
    def validate_edit(self) -> ResolvePermissionRequest:
        if self.decision == "edit" and self.edited_action is None:
            raise ValueError("edited_action is required when decision is edit")
        if self.decision != "edit" and self.edited_action is not None:
            raise ValueError("edited_action is only valid when decision is edit")
        if self.decision == "edit" and self.scope != "once":
            raise ValueError("edited tool calls can only be approved once")
        return self


class PermissionResolution(BaseModel):
    permission_id: str
    resolved: Literal[True] = True
    decision: PermissionDecision
    scope: PermissionScope
    resumed: bool


# ---------------------------------------------------------------------------
# Questions (user.ask)
# ---------------------------------------------------------------------------


class AnswerQuestionRequest(BaseModel):
    # answers: { <question_id>: [text, ...] }
    answers: dict[str, list[str]]


class QuestionAnswer(BaseModel):
    question_id: str
    answered: Literal[True] = True
    resumed: bool


class ReconcileToolRequest(BaseModel):
    decision: Literal["confirmed_completed", "retry_not_executed", "abort"]


class ToolReconciliationResolution(BaseModel):
    operation_id: str
    resolved: Literal[True] = True
    decision: Literal["confirmed_completed", "retry_not_executed", "abort"]
    resumed: bool


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------
