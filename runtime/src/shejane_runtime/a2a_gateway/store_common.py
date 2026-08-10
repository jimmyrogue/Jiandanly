from __future__ import annotations

from datetime import UTC, datetime


class A2AMessageConflictError(RuntimeError):
    """A peer reused a message id with different immutable content."""


class A2APushConfigConflictError(RuntimeError):
    """A peer reused a push config id with different immutable content."""


def _now() -> str:
    return datetime.now(UTC).isoformat()
