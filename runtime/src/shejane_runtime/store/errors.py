"""Stable persistence errors shared across Runtime store modules."""

from __future__ import annotations


class RunResultConflictError(RuntimeError):
    """A persisted run result cannot be replaced by a different result."""


class CommandConflictError(RuntimeError):
    """A command id was reused with different immutable content."""


class PluginVersionConflictError(RuntimeError):
    """A plugin identity or version is already bound to different content."""


class PluginStateError(RuntimeError):
    """A plugin state transition failed a stable admission rule."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class WorkspaceAdmissionError(RuntimeError):
    """A command references a workspace the principal cannot use."""


class ParentRunAdmissionError(RuntimeError):
    """A command references a parent Run outside the principal scope."""


class ThreadAdmissionError(RuntimeError):
    """A command references a product thread outside the principal scope."""


class RunAdmissionError(RuntimeError):
    """A deterministic Runtime prerequisite is not satisfied."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class GraphHeadConflictError(RuntimeError):
    """A stale execution attempted to move a product Run's graph head."""


class GraphDefinitionMismatchError(RuntimeError):
    """A checkpoint was opened with an incompatible Agent definition."""


class ModelCallBudgetExceeded(RuntimeError):
    """A run tried to reserve more model calls than its frozen budget."""

    code = "model_call_budget_exhausted"
    retryable = False


class ToolReceiptConflictError(RuntimeError):
    """A tool call id was reused with different arguments or identity."""

    code = "tool_receipt_conflict"
    retryable = False


class ToolOutcomeUnknownError(RuntimeError):
    """A prior side-effecting tool attempt may have executed."""

    code = "tool_outcome_unknown"
    retryable = False


class ToolReceiptStateError(RuntimeError):
    """A tool operation cannot transition from its durable state."""

    code = "tool_receipt_state_invalid"
    retryable = False


class PermissionDecisionConflictError(RuntimeError):
    """A resolved permission received a different decision."""

    code = "permission_decision_conflict"
    retryable = False


class WaitDecisionConflictError(RuntimeError):
    """A durable wait candidate received a conflicting second answer."""

    code = "wait_decision_conflict"
    retryable = False
