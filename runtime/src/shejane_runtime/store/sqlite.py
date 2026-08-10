"""Compatibility facade and startup assembly for the modular SQLite store.

Callers keep importing ``LocalStore`` and stable errors/constants from this
module. Domain behavior lives beside it; see ``docs/runtime-store.md``.
LangGraph checkpoints and Store data remain in their separate databases.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from .artifacts import MAX_ARTIFACT_BYTES as MAX_ARTIFACT_BYTES
from .artifacts import MAX_BLOB_ARTIFACT_BYTES as MAX_BLOB_ARTIFACT_BYTES
from .artifacts import MAX_PRINCIPAL_ARTIFACT_BYTES as MAX_PRINCIPAL_ARTIFACT_BYTES
from .artifacts import MAX_RUN_ARTIFACT_BYTES as MAX_RUN_ARTIFACT_BYTES
from .artifacts import MAX_RUN_INPUT_BYTES as MAX_RUN_INPUT_BYTES
from .artifacts import MAX_SETTLEMENT_ARTIFACT_REFS as MAX_SETTLEMENT_ARTIFACT_REFS
from .artifacts import MAX_TOTAL_ARTIFACT_BYTES as MAX_TOTAL_ARTIFACT_BYTES
from .artifacts import ArtifactConflictError as ArtifactConflictError
from .artifacts import ArtifactQuotaError as ArtifactQuotaError
from .artifacts import ArtifactStore
from .artifacts import RunInputQuotaError as RunInputQuotaError
from .artifacts import RunInputSnapshotError as RunInputSnapshotError
from .collaboration import MAX_AGENT_MAILBOX_PENDING as MAX_AGENT_MAILBOX_PENDING
from .collaboration import (
    MAX_DURABLE_CHILD_DEPENDENCIES as MAX_DURABLE_CHILD_DEPENDENCIES,
)
from .collaboration import MAX_DURABLE_CHILD_DEPTH as MAX_DURABLE_CHILD_DEPTH
from .collaboration import (
    MAX_DURABLE_CHILD_RESOURCE_CLAIMS as MAX_DURABLE_CHILD_RESOURCE_CLAIMS,
)
from .collaboration import (
    MAX_DURABLE_CHILDREN_PER_RUN as MAX_DURABLE_CHILDREN_PER_RUN,
)
from .collaboration import CollaborationStore
from .configuration import ConfigurationStore
from .database import ExecutionLease as ExecutionLease
from .database import LeaseFenceError as LeaseFenceError
from .database import configure_connection as _configure_connection
from .errors import CommandConflictError as CommandConflictError
from .errors import GraphDefinitionMismatchError as GraphDefinitionMismatchError
from .errors import GraphHeadConflictError as GraphHeadConflictError
from .errors import ModelCallBudgetExceeded as ModelCallBudgetExceeded
from .errors import ParentRunAdmissionError as ParentRunAdmissionError
from .errors import PermissionDecisionConflictError as PermissionDecisionConflictError
from .errors import PluginStateError as PluginStateError
from .errors import PluginVersionConflictError as PluginVersionConflictError
from .errors import RunAdmissionError as RunAdmissionError
from .errors import RunResultConflictError as RunResultConflictError
from .errors import ThreadAdmissionError as ThreadAdmissionError
from .errors import ToolOutcomeUnknownError as ToolOutcomeUnknownError
from .errors import ToolReceiptConflictError as ToolReceiptConflictError
from .errors import ToolReceiptStateError as ToolReceiptStateError
from .errors import WaitDecisionConflictError as WaitDecisionConflictError
from .errors import WorkspaceAdmissionError as WorkspaceAdmissionError
from .events import TRANSIENT_RUN_EVENT_TYPES as TRANSIENT_RUN_EVENT_TYPES
from .migrations import (
    _delete_legacy_model_provider_credentials,
    _ensure_columns,
    _ensure_model_connection_constraints,
    _ensure_plugin_execution_kinds,
)
from .model_calls import ModelCallStore
from .plugins import PluginStore
from .run_commands import RunCommandStore
from .run_jobs import RunJobStore
from .run_state import RunStateStore
from .schedules import ScheduledRunStore
from .schema import SCHEMA
from .tool_receipts import ToolReceiptStore
from .waits import WaitStore
from .workspaces import WorkspaceStore


class LocalStore(
    RunStateStore,
    RunCommandStore,
    RunJobStore,
    PluginStore,
    WaitStore,
    CollaborationStore,
    ToolReceiptStore,
    WorkspaceStore,
    ConfigurationStore,
    ModelCallStore,
    ScheduledRunStore,
    ArtifactStore,
):
    """Runtime-owned persistence facade over the modular SQLite store."""

    @classmethod
    async def open(cls, db_path: Path) -> LocalStore:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(db_path), isolation_level=None)
        try:
            await _configure_connection(conn)
            await conn.executescript(SCHEMA)
            await _ensure_model_connection_constraints(conn)
            await _ensure_plugin_execution_kinds(conn)
            await _delete_legacy_model_provider_credentials(conn)
            await conn.execute("BEGIN IMMEDIATE")
            await _ensure_columns(conn)
            await conn.commit()
            store = cls(conn, db_path)
            await store.gc_orphan_bodies()
            return store
        except BaseException:
            if conn.in_transaction:
                await conn.rollback()
            await conn.close()
            raise
