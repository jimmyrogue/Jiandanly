from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ExecutionIdentityError(RuntimeError):
    """A durable job cannot prove that it belongs to its Run owner."""


class ExecutionWorkspaceError(RuntimeError):
    """A durable job can no longer use its Run workspace."""


class ExecutionModelBindingError(RuntimeError):
    """A frozen model binding can no longer resolve its credential reference."""


class ExecutionSkillBindingError(RuntimeError):
    """A Run can no longer prove that its admitted Skill catalog is unchanged."""


class ExecutionSettlementError(RuntimeError):
    """Authoritative execution records cannot prove a safe terminal result."""


class ChildCoordinationError(RuntimeError):
    """Required child work could not satisfy the parent's completion policy."""


class ExecutionLeaseExpiredError(RuntimeError):
    """An execution lost ownership before it could publish a result."""


class ExecutionShutdownError(RuntimeError):
    """The Runtime stopped an execution during a controlled shutdown."""


RUN_SHUTDOWN_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class RunOutcome:
    """Authoritative terminal or suspended result produced by one execution attempt."""

    status: str
    event_type: str
    payload: dict[str, Any]


class RunNotFoundError(Exception):
    """Raised when an operation references an unknown run."""


class CheckpointNotFoundError(Exception):
    """Raised when a checkpoint fork references an unknown checkpoint."""
