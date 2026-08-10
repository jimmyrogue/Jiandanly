"""Runtime-owned SQLite persistence.

``LocalStore`` is the stable facade; domain implementations are split across
this package. LangGraph checkpoints and Store data use separate databases.
See ``docs/runtime-store.md`` for ownership and transaction rules.
"""

from .sqlite import LocalStore

__all__ = ["LocalStore"]
